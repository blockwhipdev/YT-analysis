# Signal / Noise — YouTube content analyzer

Paste a YouTube **channel** or **video** link. It pulls the real transcript and uses Claude to extract the substantive value — key insights, detailed points covered — while sidelining the noise (sponsor reads, hype, filler, repeated CTAs). For a channel, it lists the last 5 uploads and lets you pick one.

No YouTube API key needed (it uses `yt-dlp` to scrape listings and captions). You supply an Anthropic API key for the analysis step.

## Why this is a local app and not a single HTML file

A browser can't do this alone: YouTube's video-listing and caption endpoints are CORS-blocked client-side, and the transcript libraries are server-side only. So the YouTube fetching and the Claude call run in a tiny local Python backend; the page you interact with is served by it.

## Setup

Requires Python 3.9+.

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # Windows: set ANTHROPIC_API_KEY=...
python app.py
```

Open http://localhost:5000

## Use

- **Channel link** (e.g. `https://www.youtube.com/@CoinBureau`) → shows the last 5 uploads → click one to analyze.
- **Video link** (e.g. `https://youtu.be/...` or `watch?v=...`) → analyzed directly.

The report gives you a value score (share of the video that was genuine value), a substance-only TL;DR, key insights, detailed points by section, and a "Sidelined as noise" list so you can see exactly what was filtered.

## Options

- `ANTHROPIC_MODEL` — defaults to `claude-sonnet-4-6` (good for long transcripts). Override if you want a different model.
- `PORT` — defaults to `5000`.

## Limitations & notes

- **Captions must exist.** If a video has no transcript (manual or auto), analysis can't run — you'll get a clear message.
- Long videos are capped at ~120k characters of transcript (~30k tokens) before analysis to keep cost/latency sane; very long videos get truncated with a marker.
- `yt-dlp` occasionally needs updating when YouTube changes things: `pip install -U yt-dlp`.
- This runs a Flask dev server bound to localhost — fine for personal use, not for public deployment.
- Quality of the value/noise split depends on the model reading the transcript; treat the value score as a useful signal, not a precise metric.

## Files

- `app.py` — backend: URL resolve, transcript pull (youtube-transcript-api → yt-dlp fallback), Claude analysis.
- `index.html` — frontend (served at `/`).
- `requirements.txt` — dependencies.
