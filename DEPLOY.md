# 4H Bot — Prop Terminal · Deployment Guide (100% free)

The app is two pieces:

| Piece | What it is | Host | Cost |
|---|---|---|---|
| `index.html` | The terminal UI (exact design you approved) | **Vercel** | free |
| `api/` | FastAPI backend — the brain, challenge engine, replay, bridges | **Render** | free |
| `trade_log.json` | The bot's existing memory (seeded into the terminal on boot) | git repo | free |

Your existing hourly bot (`main.py` via `schedule.yml`) keeps running untouched.
`terminal-scan.yml` additionally pings the Render backend each hour so LIVE
challenges keep trading when the tab is closed.

---

## 1. Render (backend) — ~5 minutes

1. Push this repo to GitHub (it already is — just commit these files).
2. Go to <https://render.com> → **New + → Web Service** → connect the repo.
3. Settings:
   - **Name:** `4h-bot-terminal`
   - **Region:** closest to you
   - **Branch:** `main`
   - **Root Directory:** `api`
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - **Instance Type:** Free
4. **Environment variables** (optional but recommended):
   - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — same secrets your GitHub
     Action already uses. Without them the terminal runs fine, just without
     its own Telegram messages (your main.py bot keeps messaging as always).
   - `LIVE_TRADING` — leave unset/`false`. Real orders through MetaAPI /
     cTrader only fire when you deliberately set it to `true`.
5. Deploy. Note your URL, e.g. `https://4h-bot-terminal.onrender.com`.
6. Sanity check: open `https://<your-url>/api/health` → `{"ok": true}`.

Free-tier notes: the service sleeps after ~15 min idle and cold-starts in
~30–60 s. The hourly GitHub Action ping keeps it warm on the hour.

## 2. Vercel (frontend) — ~3 minutes

1. Go to <https://vercel.com> → **Add New → Project** → import the same repo.
2. Framework preset: **Other**. No build command needed — `index.html` is static.
3. Open `vercel.json` and replace `YOUR-RENDER-SERVICE.onrender.com` with your
   real Render URL from step 1. Commit.
4. Deploy. Your terminal is live at `https://<project>.vercel.app`.

`vercel.json` proxies every `/api/*` call to Render, so the UI works with no
CORS setup and gracefully falls back to demo data if the backend is asleep.

## 2b. Neon Postgres — persistent storage (free, recommended)

By default the backend uses SQLite on the service disk, which is **wiped on
every deploy** (challenge runs reset; the bot's trade history re-seeds
automatically, but anything you armed is lost). A free Neon database fixes
that permanently — the store auto-detects it, no code changes:

1. Go to <https://neon.tech> → sign up (free tier: 0.5 GB — plenty).
2. Create a project → **Dashboard → Connection string** → copy it
   (looks like `postgresql://user:pass@ep-xxx.neon.tech/neondb?sslmode=require`).
3. Render → your service → **Environment** → add:
   - Key: `DATABASE_URL` — Value: the connection string
4. Save → Manual deploy. Every table now lives in Postgres and survives
   redeploys, restarts and spin-downs.

Notes: Neon's free tier suspends idle compute — the first query after a long
pause takes ~1–3 s to wake. Also make sure Render's **Auto-Deploy** stays
**Off**, since your bot pushes `trade_log.json` commits a few times a day.

## 3. Heartbeats (built-in — no setup needed)

Two GitHub Actions workflows keep everything alive (free — the repo is public):

- **`keep-alive.yml`** — pings `/api/health` every 10 minutes so the Render
  service never sleeps. No secrets needed; the URL is baked in.
- **`terminal-scan.yml`** — hits `/api/scan` hourly (:10 past the hour) so LIVE
  challenges keep trading even when nobody has the tab open. Uses the
  `RENDER_API_URL` secret if you set it, otherwise the built-in URL.

> Heads-up: keep-alive runs ~144 times/day, so your Actions tab will be busy —
> that's normal. If you ever make the repo private, switch to
> [cron-job.org](https://cron-job.org) (free) pinging the same health URL
> instead, since public-repo schedules cost nothing but private ones burn
> Actions minutes.

## 4. Connect a real prop account (when ready)

In the terminal's **Connect** tab:

- **MetaTrader 4/5** → paste a MetaAPI token from <https://app.metaapi.cloud>
  (free tier covers one account) + login/password/server. The backend bridges
  bracket orders (SL+TP attached) through MetaAPI cloud.
- **cTrader** → create an app at <https://openapi.ctrader.com>, paste the
  client ID + secret + account ID. The backend speaks the Spotware OpenAPI
  (OAuth2 token swap is implemented; order placement goes live with
  `LIVE_TRADING=true`).

Both bridges are **dry-run by default** — they validate credentials and echo
what they *would* place. Nothing real fires until you set `LIVE_TRADING=true`
on Render.

## 5. Local development

```bash
pip install -r api/requirements.txt
cd api
uvicorn app:app --reload
# UI: just open index.html — /api calls fail gracefully and demo data shows.
# To test the full loop locally, serve index.html from the same origin, e.g.:
#   pip install "uvicorn[standard]"  (already included)
#   uvicorn app:app --reload --port 8000
#   then open http://localhost:8000  after placing index.html in api/static (optional)
```

## What each terminal feature maps to

| UI feature | Backend |
|---|---|
| Challenge tiles, risk %, typed-in rules | `POST /api/challenge` → SQLite run |
| Live mode | `POST /api/scan` books fills on real candles, applies your typed guardrails, checks pass/breach |
| Replay fast | `brain.replay()` re-runs the exact Gate A engine over months of 1H candles in one request |
| Dashboard / Trades / History | `GET /api/state` (seeds your real `trade_log.json` trades on first boot) |
| Telegram on/off pill | `POST /api/settings` — the backend's own notifier mutes instantly |
| Connect tab | `POST /api/connect` → MetaAPI / cTrader OpenAPI validation, stored encrypted-at-rest (SQLite on the service disk) |
| Engine pill click | force scan — one full engine cycle on demand |
