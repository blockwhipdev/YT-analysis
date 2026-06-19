# Deploying Signal / Noise

The app is a Flask backend + a single HTML page. Hosting = running the backend on a
public server. These files prepare it for production:

- `Procfile` / `render.yaml` — start it with **gunicorn** (the Flask dev server is not for public traffic)
- `requirements.txt` — pins `gunicorn`
- `runtime.txt` — pins Python 3.12

Start command (used by every host below):

```
gunicorn app:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT
```

`--timeout 120` matters: a long video can take 30–60s to analyze, and gunicorn's
default 30s timeout would kill the request mid-flight.

---

## The API-key model (read this)

Every visitor enters **their own** Anthropic key in the browser; it's stored in their
localStorage and sent per request. So:

- **Do NOT set `ANTHROPIC_API_KEY` on the host** for a public link — leave it unset and
  each visitor pays for their own usage. (If it's set, anyone without their own key
  spends *your* money.)
- Traffic is over HTTPS (all hosts below provide it), so keys aren't sent in cleartext.

---

## ⚠️ The one real risk: YouTube blocks datacenter IPs

`yt-dlp` / the transcript API work from a home IP but are frequently blocked from cloud
hosts ("Sign in to confirm you're not a bot" / HTTP 429). If transcripts fail after
deploy, that's why — it's not your code. Mitigations: a residential/rotating proxy, or
run via a tunnel from a home machine instead (see the project chat).

---

## Option A — Render (recommended, has a free tier)

1. Put the code on GitHub:
   ```sh
   git init && git add -A && git commit -m "Signal/Noise"
   git branch -M main
   git remote add origin https://github.com/<you>/signal-noise.git
   git push -u origin main
   ```
2. On https://render.com → **New + → Blueprint** → pick the repo. `render.yaml` is
   detected; click **Apply**.
3. First build takes a few minutes. You get a URL like
   `https://signal-noise.onrender.com` — that's the link you share.

Free instances sleep after ~15 min idle (first hit after that is a ~30–60s cold start).

## Option B — Railway (no GitHub needed)

```sh
npm i -g @railway/cli
railway login
railway init
railway up
```
Railway auto-detects the `Procfile`. Open the generated domain in the dashboard.

## Option C — Fly.io

```sh
fly launch        # detects Python; accept defaults, don't set a DB
fly deploy
```

---

## Verify after deploy

```sh
# should print HTTP 200
curl -s -o /dev/null -w "%{http_code}\n" https://<your-url>/

# should print the friendly 401 (proves the backend + key flow work)
curl -s -X POST https://<your-url>/api/analyze \
  -H "Content-Type: application/json" -d '{"url":"https://youtu.be/dQw4w9WgXcQ"}'
```

Then open the URL, paste an Anthropic key, and analyze a real video to confirm the
transcript path works from the host's IP.
