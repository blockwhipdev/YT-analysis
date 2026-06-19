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

from flask import Flask, request, jsonify, send_from_directory
from yt_dlp import YoutubeDL
from youtube_transcript_api import YouTubeTranscriptApi
import anthropic

app = Flask(__name__, static_folder=None)

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
TRANSCRIPT_CHAR_CAP = 120_000  # ~30k tokens; keeps long videos affordable

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
        api = YouTubeTranscriptApi()
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
        raise RuntimeError(
            "No transcript available for this video (captions may be disabled). "
            f"({type(e).__name__})"
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
    try:
        if is_video_url(url):
            return jsonify({"type": "video", "video": get_video_meta(url)})
        name, videos = list_channel_videos(url, n=5)
        if not videos:
            return jsonify({"error": "Couldn't find recent videos at that link."}), 404
        return jsonify({"type": "channel", "channel": name, "videos": videos})
    except Exception as e:
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

    try:
        transcript = get_transcript(vid)
        if not title:
            title = get_video_meta(vid)["title"]
        result = analyze_transcript(title, transcript, api_key)
        result["title"] = title
        result["url"] = f"https://www.youtube.com/watch?v={vid}"
        result["transcript_chars"] = len(transcript)
        return jsonify(result)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 422
    except anthropic.AuthenticationError:
        return jsonify({"error": "That API key was rejected. Check it and try again."}), 401
    except anthropic.APIStatusError as e:
        return jsonify({"error": f"Anthropic API error ({e.status_code}): {e.message}"}), 502
    except Exception as e:
        return jsonify({"error": f"Analysis failed: {type(e).__name__}: {e}"}), 500


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("\n  No ANTHROPIC_API_KEY in env — that's fine, enter your key in the browser.")
        print("  (You can still set one server-side as a fallback if you prefer.)\n")
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Signal / Noise running at  http://localhost:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False)
