"""
Signal / Noise — YouTube content analyzer (local backend)

Takes a channel or video URL, pulls the real transcript, and uses Claude to
extract the substantive value while sidelining the noise (sponsor reads, hype,
filler, repeated CTAs).

Run:
    export ANTHROPIC_API_KEY=sk-ant-...
    python app.py
Then open http://localhost:5000
"""

import os
import re
import json
import html
import time
import hmac
import hashlib

from flask import (
    Flask, request, jsonify, send_from_directory, session, redirect, Response,
)
from yt_dlp import YoutubeDL
from youtube_transcript_api import YouTubeTranscriptApi
import anthropic

import db

app = Flask(__name__, static_folder=None)

# Admin auth — both configurable via env; never hardcode the real values in the repo.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "noise@123")
# Stable session-signing secret (so the admin login survives restarts on one worker).
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(
    ("signal-noise::" + ADMIN_PASSWORD).encode()
).hexdigest()

db.init_db()


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or ""


MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
TRANSCRIPT_CHAR_CAP = 120_000  # ~30k tokens; keeps long videos affordable


def _build_proxy():
    """YouTube blocks most datacenter IPs, so on a cloud host route requests
    through a residential proxy. Configure via env:
      WEBSHARE_PROXY_USERNAME + WEBSHARE_PROXY_PASSWORD  (recommended residential)
      or PROXY_URL=http://user:pass@host:port            (any generic proxy)
    Returns (transcript_api_proxy_config, yt_dlp_proxy_url) — (None, None) if unset.
    """
    ws_user = os.environ.get("WEBSHARE_PROXY_USERNAME")
    ws_pass = os.environ.get("WEBSHARE_PROXY_PASSWORD")
    generic = os.environ.get("PROXY_URL", "").strip()
    try:
        if ws_user and ws_pass:
            from youtube_transcript_api.proxies import WebshareProxyConfig
            cfg = WebshareProxyConfig(proxy_username=ws_user, proxy_password=ws_pass)
            return cfg, cfg.url  # cfg.url -> http://<user>-rotate:<pass>@p.webshare.io:80/
        if generic:
            from youtube_transcript_api.proxies import GenericProxyConfig
            return GenericProxyConfig(http_url=generic, https_url=generic), generic
    except Exception:
        pass
    return None, None


TRANSCRIPT_PROXY, YDLP_PROXY = _build_proxy()

# ----------------------------- URL handling ------------------------------- #

VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})")


def extract_video_id(url: str):
    m = VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def is_video_url(url: str) -> bool:
    return extract_video_id(url) is not None


def normalize_channel_url(url: str) -> str:
    """Point yt-dlp at the channel's Videos tab so we get newest uploads."""
    url = url.split("?")[0].rstrip("/")
    if url.endswith("/videos") or "/playlist" in url:
        return url
    return url + "/videos"


# --------------------------- YouTube fetching ----------------------------- #

def list_channel_videos(url: str, n: int = 5):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": n,
        "skip_download": True,
    }
    if YDLP_PROXY:
        opts["proxy"] = YDLP_PROXY
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(normalize_channel_url(url), download=False)

    entries = info.get("entries") or []
    channel_name = info.get("channel") or info.get("title") or "Channel"
    videos = []
    for e in entries[:n]:
        if not e:
            continue
        vid = e.get("id")
        if not vid:
            continue
        dur = e.get("duration")
        videos.append({
            "id": vid,
            "title": e.get("title") or "Untitled",
            "duration": fmt_duration(dur),
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    return channel_name, videos


def get_video_meta(url_or_id: str):
    vid = extract_video_id(url_or_id) or url_or_id
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": True}
    if YDLP_PROXY:
        opts["proxy"] = YDLP_PROXY
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
        return {
            "id": vid,
            "title": info.get("title") or "Untitled",
            "duration": fmt_duration(info.get("duration")),
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            "url": f"https://www.youtube.com/watch?v={vid}",
        }
    except Exception:
        return {
            "id": vid, "title": "Video", "duration": "",
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            "url": f"https://www.youtube.com/watch?v={vid}",
        }


def get_transcript(video_id: str) -> str:
    """Primary: youtube-transcript-api. Fallback: yt-dlp auto subtitles."""
    # Primary
    try:
        api = YouTubeTranscriptApi(proxy_config=TRANSCRIPT_PROXY)
        data = api.fetch(video_id, languages=["en", "en-US", "en-GB"]).to_raw_data()
        text = " ".join(seg["text"] for seg in data if seg.get("text"))
        if text.strip():
            return clean_transcript(text)
    except Exception:
        pass

    # Fallback: yt-dlp writes a VTT we parse in-memory
    try:
        return _yt_dlp_subs(video_id)
    except Exception as e:
        hint = "" if (TRANSCRIPT_PROXY or YDLP_PROXY) else (
            " On a hosted server this usually means YouTube is blocking the server's IP — "
            "set a residential proxy (see DEPLOY.md)."
        )
        raise RuntimeError(
            "Couldn't get a transcript for this video — captions may be disabled, "
            f"or the request was blocked.{hint} ({type(e).__name__})"
        )


def _yt_dlp_subs(video_id: str) -> str:
    import tempfile, glob
    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "writeautomaticsub": True, "writesubtitles": True,
            "subtitleslangs": ["en", "en-US", "en-GB"], "subtitlesformat": "vtt",
            "outtmpl": os.path.join(tmp, "%(id)s"),
        }
        if YDLP_PROXY:
            opts["proxy"] = YDLP_PROXY
        with YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        vtts = glob.glob(os.path.join(tmp, "*.vtt"))
        if not vtts:
            raise RuntimeError("no subtitle file produced")
        with open(vtts[0], encoding="utf-8") as f:
            raw = f.read()
    return clean_transcript(parse_vtt(raw))


def parse_vtt(raw: str) -> str:
    lines, seen = [], set()
    for line in raw.splitlines():
        line = line.strip()
        if (not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:"))
                or line.isdigit()):
            continue
        line = re.sub(r"<[^>]+>", "", line)  # strip inline timing tags
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    return " ".join(lines)


def clean_transcript(text: str) -> str:
    text = re.sub(r"\[(?:music|applause|laughter|inaudible)\]", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fmt_duration(secs):
    if not secs:
        return ""
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ----------------------------- Analysis ----------------------------------- #

SYSTEM = """You analyze a YouTube video transcript and extract only the substantive value, deliberately sidelining the noise.

Noise = sponsor reads, self-promotion, channel/CTA plugs (like/subscribe/notification), filler and verbal padding, manufactured hype or urgency, repetition, off-topic tangents, and engagement-bait that carries no information.

Value = concrete claims, data, mechanisms, arguments, frameworks, predictions with reasoning, specific examples, and actionable takeaways.

You will receive the video title and transcript. Read the whole thing, then produce a clean, value-only digest.

Return ONLY raw JSON (no markdown, no code fences, no preamble) in exactly this shape:
{
  "tldr": "2-4 sentences capturing the real substance, no fluff",
  "value_score": 0-100 integer (share of the video that was genuine value vs noise),
  "key_insights": [{"title": "short label", "detail": "1-2 sentences of the actual point, specific"}],
  "detailed_points": [{"section": "theme or segment name", "points": ["substantive point", "..."]}],
  "noise": ["short description of each thing you sidelined, e.g. '~90s sponsor read for X'"],
  "verdict": "one sentence: worth watching in full, skim, or skip — and why"
}

Be specific and concrete — name the actual claims and numbers from the transcript, never generic summaries. Keep key_insights to the 4-8 that genuinely matter. Drop anything you classified as noise from tldr/insights/points entirely; the 'noise' array is only an audit trail of what you removed."""


def analyze_transcript(title: str, transcript: str, api_key: str):
    if len(transcript) > TRANSCRIPT_CHAR_CAP:
        transcript = transcript[:TRANSCRIPT_CHAR_CAP] + " …[transcript truncated]"

    client = anthropic.Anthropic(api_key=api_key)  # key supplied per-request by the browser
    msg = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Title: {title}\n\nTranscript:\n{transcript}",
        }],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    return parse_json(text)


def parse_json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            return json.loads(text[a:b + 1])
        raise


# ------------------------------- Routes ----------------------------------- #

@app.route("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.route("/api/resolve", methods=["POST"])
def resolve():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "Paste a YouTube channel or video link first."}), 400
    t0 = time.time()
    try:
        if is_video_url(url):
            meta = get_video_meta(url)
            db.log_event(kind="resolve", status="ok", video_id=meta.get("id"),
                         title=meta.get("title"), url=url, ip=client_ip(),
                         user_agent=request.headers.get("User-Agent", "")[:300],
                         duration_ms=int((time.time() - t0) * 1000))
            return jsonify({"type": "video", "video": meta})
        name, videos = list_channel_videos(url, n=5)
        if not videos:
            db.log_event(kind="resolve", status="error", url=url, ip=client_ip(),
                         error="no videos found", duration_ms=int((time.time() - t0) * 1000))
            return jsonify({"error": "Couldn't find recent videos at that link."}), 404
        db.log_event(kind="resolve", status="ok", title=name, url=url, ip=client_ip(),
                     user_agent=request.headers.get("User-Agent", "")[:300],
                     duration_ms=int((time.time() - t0) * 1000))
        return jsonify({"type": "channel", "channel": name, "videos": videos})
    except Exception as e:
        db.log_event(kind="resolve", status="error", url=url, ip=client_ip(),
                     error=f"{type(e).__name__}: {e}"[:500],
                     duration_ms=int((time.time() - t0) * 1000))
        return jsonify({"error": f"Couldn't read that link: {type(e).__name__}: {e}"}), 500


@app.route("/api/analyze", methods=["POST"])
def analyze():
    body = request.json or {}
    url = (body.get("url") or "").strip()
    title = (body.get("title") or "").strip()
    vid = extract_video_id(url) or (url if re.fullmatch(r"[A-Za-z0-9_-]{11}", url) else None)
    if not vid:
        return jsonify({"error": "No valid video to analyze."}), 400

    # Key comes from the browser (header), falling back to the server env if present.
    api_key = (
        request.headers.get("X-Anthropic-Key", "").strip()
        or os.environ.get("ANTHROPIC_API_KEY", "")
    )
    if not api_key:
        return jsonify({"error": "Add your Anthropic API key first (the key button, top right)."}), 401

    t0 = time.time()
    khash = db.key_fingerprint(api_key)
    vurl = f"https://www.youtube.com/watch?v={vid}"

    def _log(status, score=None, chars=None, err=None, result=None):
        db.log_event(kind="analyze", status=status, video_id=vid, title=title or None,
                     url=vurl, value_score=score, transcript_chars=chars, model=MODEL,
                     key_hash=khash, ip=client_ip(),
                     user_agent=request.headers.get("User-Agent", "")[:300],
                     error=(err[:500] if err else None), result=result,
                     duration_ms=int((time.time() - t0) * 1000))

    try:
        transcript = get_transcript(vid)
        if not title:
            title = get_video_meta(vid)["title"]
        result = analyze_transcript(title, transcript, api_key)
        result["title"] = title
        result["url"] = vurl
        result["transcript_chars"] = len(transcript)
        _log("ok", score=result.get("value_score"), chars=len(transcript), result=result)
        return jsonify(result)
    except RuntimeError as e:
        _log("error", err=str(e))
        return jsonify({"error": str(e)}), 422
    except anthropic.AuthenticationError:
        _log("error", err="anthropic auth rejected")
        return jsonify({"error": "That API key was rejected. Check it and try again."}), 401
    except anthropic.APIStatusError as e:
        _log("error", err=f"anthropic {e.status_code}: {e.message}")
        return jsonify({"error": f"Anthropic API error ({e.status_code}): {e.message}"}), 502
    except Exception as e:
        _log("error", err=f"{type(e).__name__}: {e}")
        return jsonify({"error": f"Analysis failed: {type(e).__name__}: {e}"}), 500


# ------------------------------- Admin ------------------------------------ #

ADMIN_CSS = """
  :root{--bg:#0F141B;--panel:#171E27;--line:#2A3540;--text:#E8EDF2;--muted:#8696A7;
    --signal:#3FD3A8;--noise:#F0883E}
  *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
    font-family:'Inter',system-ui,sans-serif;line-height:1.5}
  .wrap{max-width:1200px;margin:0 auto;padding:36px 22px 80px}
  a{color:var(--signal)}
  h1{font-size:24px;margin:0 0 4px;letter-spacing:-.01em}
  .sub{color:var(--muted);font-size:13px;margin:0 0 26px;font-family:ui-monospace,monospace}
  .top{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:0 0 26px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
  .card .n{font-size:28px;font-weight:700;font-family:ui-monospace,monospace;letter-spacing:-.02em}
  .card .l{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-top:4px}
  .card.ok .n{color:var(--signal)} .card.err .n{color:var(--noise)}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em;position:sticky;top:0;background:var(--bg)}
  td.mono,th.mono{font-family:ui-monospace,monospace}
  .pill{font-size:10px;font-weight:700;padding:2px 8px;border-radius:6px;text-transform:uppercase;letter-spacing:.04em}
  .pill.ok{background:rgba(63,211,168,.13);color:var(--signal)}
  .pill.error{background:rgba(240,136,62,.13);color:var(--noise)}
  .tablewrap{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow-x:auto}
  .err{color:var(--noise);max-width:320px}
  .ttl{max-width:280px;display:inline-block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom}
  .btn{background:var(--signal);color:#06231B;border:none;border-radius:9px;padding:10px 18px;font-weight:600;cursor:pointer;font-size:14px;text-decoration:none;display:inline-block}
  .ghost{background:transparent;border:1px solid var(--line);color:var(--muted);border-radius:9px;padding:8px 14px;font-size:12px;cursor:pointer;text-decoration:none}
  input[type=password]{background:var(--bg);border:1px solid var(--line);color:var(--text);
    border-radius:9px;padding:12px 14px;font-size:15px;width:100%}
  .login{max-width:360px;margin:14vh auto 0;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:26px}
  .login h1{font-size:20px}.login p{color:var(--muted);font-size:13px;margin:0 0 18px}
  .login .row{display:flex;gap:10px;margin-top:14px}
  .warn{color:var(--noise);font-size:12px}
  .seclabel{font-family:ui-monospace,monospace;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin:30px 0 14px}
  .feed{display:grid;gap:14px}
  .brief{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:18px 20px}
  .brief .bh{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:10px}
  .brief h3{font-size:16px;margin:0;flex:1;min-width:200px}
  .brief h3 a{color:var(--text);text-decoration:none}.brief h3 a:hover{color:var(--signal)}
  .brief .score{font-family:ui-monospace,monospace;font-weight:700;color:var(--signal);font-size:18px}
  .brief .meta{color:var(--muted);font-size:11px;font-family:ui-monospace,monospace}
  .brief .tldr{font-size:14px;color:var(--text);margin:0 0 12px;padding:12px 14px;background:rgba(63,211,168,.07);border:1px solid rgba(63,211,168,.25);border-radius:10px}
  .brief .ins{list-style:none;padding:0;margin:0 0 4px;display:grid;gap:8px}
  .brief .ins li{border-left:2px solid var(--signal);padding-left:11px}
  .brief .ins b{font-size:13.5px}.brief .ins span{display:block;color:var(--muted);font-size:13px}
  .brief details{margin-top:10px}.brief summary{cursor:pointer;color:var(--muted);font-size:12px;font-family:ui-monospace,monospace}
  .brief details h4{font-size:13px;margin:12px 0 6px}.brief details ul{margin:0 0 8px;padding-left:18px}
  .brief details li{font-size:13px;margin-bottom:4px}
  .brief .noise li{color:var(--noise)}
  details.log summary{cursor:pointer;color:var(--muted);font-family:ui-monospace,monospace;font-size:12px;margin:30px 0 12px}
"""


def _admin_page(body):
    return f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Signal/Noise · admin</title><style>{ADMIN_CSS}</style></head><body>{body}</body></html>"


def _login_view(error=""):
    msg = f"<p class='warn'>{html.escape(error)}</p>" if error else ""
    body = f"""
    <div class='login'>
      <h1>Admin access</h1>
      <p>Enter the admin password to view activity.</p>
      {msg}
      <form method='post' action='/admin'>
        <input type='password' name='password' placeholder='password' autofocus />
        <div class='row'><button class='btn' type='submit'>Enter</button></div>
      </form>
    </div>"""
    return _admin_page(body)


def _dashboard_view():
    if not db.enabled():
        return _admin_page(
            "<div class='wrap'><h1>Admin</h1><p class='sub'>DATABASE_URL is not set, so nothing is being logged. "
            "Set it in the environment to start recording activity.</p>"
            "<a class='ghost' href='/admin/logout'>log out</a></div>"
        )
    s = db.fetch_stats()
    analyses = db.fetch_analyses(200)
    rows = db.fetch_events(250)

    def card(n, label, cls=""):
        val = "—" if n is None else n
        return f"<div class='card {cls}'><div class='n'>{html.escape(str(val))}</div><div class='l'>{html.escape(label)}</div></div>"

    cards = "".join([
        card(s.get("analyses"), "analyses"),
        card(s.get("analyses_ok"), "succeeded", "ok"),
        card(s.get("analyses_err"), "failed", "err"),
        card(s.get("users"), "distinct users"),
        card(s.get("videos"), "videos"),
        card(s.get("avg_score"), "avg signal %"),
        card(s.get("resolves"), "link lookups"),
        card(s.get("last_24h"), "events · 24h"),
    ])

    # --- the review feed: every analysed video + its stored insights ---
    briefs = []
    for a in analyses:
        res = a.get("result") or {}
        ts = a.get("created_at")
        ts = ts.strftime("%b %d, %H:%M") if ts else ""
        title = res.get("title") or a.get("title") or "Untitled"
        url = res.get("url") or a.get("url") or ""
        score = a.get("value_score")
        score = "" if score is None else f"{score}%"
        head = (f"<a href='{html.escape(url)}' target='_blank'>{html.escape(title)}</a>"
                if url else html.escape(title))

        tldr = res.get("tldr")
        tldr_html = f"<div class='tldr'>{html.escape(tldr)}</div>" if tldr else ""

        ins = res.get("key_insights") if isinstance(res.get("key_insights"), list) else []
        ins_html = ""
        if ins:
            items = "".join(
                f"<li><b>{html.escape(str(k.get('title','')))}</b><span>{html.escape(str(k.get('detail','')))}</span></li>"
                for k in ins if isinstance(k, dict)
            )
            ins_html = f"<ul class='ins'>{items}</ul>"

        # expandable: detailed points + verdict + what was filtered
        det_parts = []
        verdict = res.get("verdict")
        if verdict:
            det_parts.append(f"<h4>Verdict</h4><p style='font-size:13px;margin:0 0 8px'>{html.escape(verdict)}</p>")
        secs = res.get("detailed_points") if isinstance(res.get("detailed_points"), list) else []
        for sec in secs:
            if not isinstance(sec, dict):
                continue
            pts = sec.get("points") if isinstance(sec.get("points"), list) else []
            lis = "".join(f"<li>{html.escape(str(p))}</li>" for p in pts)
            det_parts.append(f"<h4>{html.escape(str(sec.get('section','')))}</h4><ul>{lis}</ul>")
        noise = res.get("noise") if isinstance(res.get("noise"), list) else []
        if noise:
            lis = "".join(f"<li>{html.escape(str(n))}</li>" for n in noise)
            det_parts.append(f"<h4>Filtered out as noise</h4><ul class='noise'>{lis}</ul>")
        det_html = (f"<details><summary>full breakdown</summary>{''.join(det_parts)}</details>"
                    if det_parts else "")

        briefs.append(
            "<div class='brief'>"
            f"<div class='bh'><h3>{head}</h3><span class='score'>{html.escape(score)}</span></div>"
            f"<div class='meta'>{html.escape(ts)} · {html.escape(str(a.get('video_id') or ''))} · "
            f"{html.escape(str(a.get('transcript_chars') or '?'))} chars · user {html.escape(str(a.get('key_hash') or '—'))}</div>"
            f"{tldr_html}{ins_html}{det_html}"
            "</div>"
        )
    feed = ("<div class='feed'>" + "".join(briefs) + "</div>") if briefs else \
        "<p class='sub'>No analyses stored yet — run one from the app and it'll appear here.</p>"

    trs = []
    for r in rows:
        ts = r.get("created_at")
        ts = ts.strftime("%m-%d %H:%M:%S") if ts else ""
        status = r.get("status") or ""
        title = r.get("title") or ""
        url = r.get("url") or ""
        title_cell = (f"<a class='ttl' href='{html.escape(url)}' target='_blank' title='{html.escape(title)}'>{html.escape(title)}</a>"
                      if url else f"<span class='ttl'>{html.escape(title)}</span>")
        trs.append(
            "<tr>"
            f"<td class='mono'>{html.escape(ts)}</td>"
            f"<td>{html.escape(r.get('kind') or '')}</td>"
            f"<td><span class='pill {html.escape(status)}'>{html.escape(status)}</span></td>"
            f"<td>{title_cell}</td>"
            f"<td class='mono'>{html.escape(str(r.get('video_id') or ''))}</td>"
            f"<td class='mono'>{html.escape(str(r.get('value_score') if r.get('value_score') is not None else ''))}</td>"
            f"<td class='mono'>{html.escape(str(r.get('transcript_chars') or ''))}</td>"
            f"<td class='mono'>{html.escape(str(r.get('key_hash') or ''))}</td>"
            f"<td class='mono'>{html.escape(str(r.get('ip') or ''))}</td>"
            f"<td class='mono'>{html.escape(str(r.get('duration_ms') or ''))}</td>"
            f"<td class='err'>{html.escape((r.get('error') or '')[:200])}</td>"
            "</tr>"
        )
    table = (
        "<details class='log'><summary>Raw event log (all requests, incl. lookups &amp; errors)</summary>"
        "<div class='tablewrap'><table><thead><tr>"
        "<th class='mono'>time</th><th>kind</th><th>status</th><th>title</th>"
        "<th class='mono'>video</th><th class='mono'>score</th><th class='mono'>chars</th>"
        "<th class='mono'>user</th><th class='mono'>ip</th><th class='mono'>ms</th><th>error</th>"
        "</tr></thead><tbody>" + "".join(trs) + "</tbody></table></div></details>"
    )

    body = f"""
    <div class='wrap'>
      <div class='top'>
        <div><h1>Signal/Noise · activity</h1>
        <p class='sub'>every analysed video &amp; its insights · keys stored as one-way hashes, never in the clear</p></div>
        <div><a class='ghost' href='/admin'>refresh</a> &nbsp; <a class='ghost' href='/admin/logout'>log out</a></div>
      </div>
      <div class='cards'>{cards}</div>
      <p class='seclabel'>Analysed videos &amp; insights · {len(analyses)}</p>
      {feed}
      {table}
    </div>"""
    return _admin_page(body)


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        supplied = (request.form.get("password") or "")
        if hmac.compare_digest(supplied, ADMIN_PASSWORD):
            session["admin"] = True
            return redirect("/admin")
        return Response(_login_view("Wrong password."), status=401, mimetype="text/html")
    if not session.get("admin"):
        return Response(_login_view(), mimetype="text/html")
    return Response(_dashboard_view(), mimetype="text/html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect("/admin")


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("\n  No ANTHROPIC_API_KEY in env — that's fine, enter your key in the browser.")
        print("  (You can still set one server-side as a fallback if you prefer.)\n")
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Signal / Noise running at  http://localhost:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False)
