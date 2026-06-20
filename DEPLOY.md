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

## Activity logging + /admin dashboard (optional)

Set `DATABASE_URL` (Postgres, e.g. Neon) and the app records every analysed video and
its full generated briefing. View them at **`/admin`** (password = `ADMIN_PASSWORD`).

Env vars:
```
DATABASE_URL   = postgresql://user:pass@host/db?sslmode=require   # enables logging + /admin
ADMIN_PASSWORD = your-admin-password                              # gate for /admin (default: noise@123 — change it)
SECRET_KEY     = any long random string                          # optional; signs the admin session
```

Notes:
- **Logging never blocks or slows users.** Writes go to a background queue; if the DB is
  slow or down, items are dropped and the request is unaffected. The app runs fine with
  `DATABASE_URL` unset (logging just no-ops).
- **No API keys are stored** — only a one-way hash, so the admin can count distinct users
  without ever seeing a key.
- **Change `ADMIN_PASSWORD`** from the default before sharing the link, and treat
  `DATABASE_URL` as a secret (env only — never commit it).

## The API-key model (read this)

Every visitor enters **their own** Anthropic key in the browser; it's stored in their
localStorage and sent per request. So:

- **Do NOT set `ANTHROPIC_API_KEY` on the host** for a public link — leave it unset and
  each visitor pays for their own usage. (If it's set, anyone without their own key
  spends *your* money.)
- Traffic is over HTTPS (all hosts below provide it), so keys aren't sent in cleartext.

---

## ⚠️ YouTube blocks datacenter IPs — fix with a residential proxy

`yt-dlp` / the transcript API work from a home IP but are blocked from cloud hosts
("Sign in to confirm you're not a bot" / HTTP 429). If transcripts fail after deploy
with "couldn't get a transcript … the request was blocked", that's why — not your code.

### ✅ Free fix — Supadata transcript fallback (recommended)

The app falls back to the **Supadata** API, which fetches the transcript via its own
infrastructure, so it works even though Render's IP is blocked. Free tier ~**100
transcripts/month, no credit card**.

1. Sign up at **supadata.ai** → copy your API key (Dashboard).
2. In Render → your service → **Environment** → add:
   ```
   SUPADATA_API_KEY = <your supadata key>
   ```
3. Save — Render redeploys. Transcripts now work, $0.

The transcript chain is: direct youtube-transcript-api → yt-dlp → Supadata. So locally it
uses the direct path (free, unlimited) and only spends a Supadata credit when the direct
path is blocked (i.e. on the cloud host). Leave the key unset locally.

### Alternative — residential proxy

The app routes through a proxy when these env vars are set. **Residential** proxies work;
datacenter proxies (incl. free tiers) are blocked too.

**Recommended — Webshare residential** (cheap, pay-per-GB; transcripts are tiny):
1. Sign up at webshare.io → buy a **Residential** plan → copy the proxy username/password.
2. In Render → your service → **Environment** → add:
   ```
   WEBSHARE_PROXY_USERNAME = <your webshare username>
   WEBSHARE_PROXY_PASSWORD = <your webshare password>
   ```
3. Save — Render redeploys. Done.

**Any other proxy** — set a single var instead:
```
PROXY_URL = http://user:pass@host:port
```

With either set, the transcript API and all `yt-dlp` calls (transcript fallback, channel
listing, video metadata) go through the proxy automatically. Leave them unset locally —
the app runs proxy-free from your home IP.

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
