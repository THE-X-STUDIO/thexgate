# thexgate — THEXSTUDIO $5 challenge 48h paper margin-lock runner (Render free)

Runs the pinned money-config paper loop (2-6h survivor window, vol5>=800, TP2x/SL-30%/1h, 20% size, -40% daily kill) with state persisted to Supabase so Render restarts lose nothing. `/health` exposes live status; GitHub Actions pings it every 10 min so the free web service never sleeps.

## Deploy (2 min)
1. GitHub repo: this repo on account `TOJI-121`.
2. On render.com: **New + -> Blueprint**, pick this repo -> **Apply** (renders from render.yaml; free instance, Singapore).
3. When prompted, set the env values:
   - `SUPA_STATE_URI` - the postgres connection URI (thexscout\supabase_uri.txt content).
   - `TELEGRAM_BOT_TOKEN` - bot token for trade notifications.
4. After first live deploy: set repo **variable** `PING_URL` = `https://thexgate.onrender.com` (repo Settings -> Secrets and variables -> Actions -> Variables tab). The ping workflow starts keeping it awake.

## Verify
- `GET /health` -> {"ok":true,...} with pot/watch/n/dbg stats.
- Within minutes: `dbg.watch_adds` climbing on every B-scan = new-pool ingestion working from the datacenter IP.
- Telegram pings on every open/close once you message `@faruser121bot` once.

## Paper only
No wallet, no keys, no spending. Real-money executor is built (`executor.py`) but never attached here.
