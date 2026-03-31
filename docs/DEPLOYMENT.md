# Deployment Guide

Step-by-step instructions for deploying the ALKIMI MM Bot on Railway.

---

## Prerequisites

- Python 3.11+
- A [Railway](https://railway.app) account
- Railway CLI: `npm install -g @railway/cli`
- GitHub repository linked to Railway
- Exchange API keys for KuCoin, Gate.io, MEXC, and Kraken

---

## 1. Local Dry-Run First

Always verify locally before deploying.

### Setup

```bash
# Clone the repo
git clone https://github.com/chorley11/Alkimi-MM-Platform.git
cd Alkimi-MM-Platform

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Configure

```bash
# Copy the example env
cp .env.example .env

# Edit .env — add your exchange API keys
# Leave LIVE_MODE=false for now
nano .env
```

### Verify config

```bash
python3 -c "from config.settings import load_bot_config; cfg = load_bot_config(); print('Config OK:', cfg.dry_run)"
```

### Run in dry-run mode

```bash
python3 main.py
```

Open `http://localhost:8000/health` — should return `{"status":"ok"}`.

Open `http://localhost:8000/docs` to explore the API.

Watch the WebSocket:
```bash
# Install wscat: npm install -g wscat
wscat -c ws://localhost:8000/ws
```

Verify you see `tick_update` events flowing every ~1 second per exchange.

---

## 2. Railway Setup

### 2.1 Create Project

```bash
# Login
railway login

# Link to your Railway project (or create new)
railway link

# Or create a new project
railway init
```

### 2.2 Add a Persistent Volume

The SQLite database must persist across deploys. Add a Railway volume:

1. In Railway dashboard → your service → **Volumes**
2. Add a volume:
   - **Mount path**: `/app/data`
3. Set `DB_PATH=/app/data/mm_bot.db` in environment variables

### 2.3 Set Environment Variables

In Railway dashboard → your service → **Variables**, add all variables from `.env.example`:

```
KUCOIN_API_KEY=...
KUCOIN_API_SECRET=...
KUCOIN_PASSPHRASE=...
GATE_API_KEY=...
GATE_API_SECRET=...
MEXC_API_KEY=...
MEXC_API_SECRET=...
KRAKEN_API_KEY=...
KRAKEN_API_SECRET=...

LIVE_MODE=false
PORT=8000
DB_PATH=/app/data/mm_bot.db
LOG_LEVEL=INFO
ALERT_WEBHOOK_URL=
```

> **Never set `LIVE_MODE=true` until you have verified the bot is working correctly in dry-run mode on Railway.**

### 2.4 Verify `railway.json`

The repo includes a `railway.json` — verify it looks correct:

```json
{
  "$schema": "https://railway.app/railway.schema.json",
  "build": {
    "builder": "NIXPACKS"
  },
  "deploy": {
    "startCommand": "python main.py",
    "healthcheckPath": "/health",
    "healthcheckTimeout": 30,
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 5
  }
}
```

Railway will:
- Build using Nixpacks (auto-detects Python, installs `requirements.txt`)
- Start with `python main.py`
- Health-check at `/health` every 30 seconds
- Restart up to 5 times on failure

---

## 3. Deploy

```bash
git add .
git commit -m "Initial deployment"
git push origin main
```

Railway auto-deploys from `main` branch. Monitor progress:

```bash
railway logs
```

Or watch in the Railway dashboard.

### Verify Deployment

```bash
# Get your Railway URL
railway status

# Check health
curl https://your-app.railway.app/health

# Check status
curl https://your-app.railway.app/api/status
```

---

## 4. Monitor

### Railway Logs

```bash
railway logs --tail
```

Look for:
- `Bot started` messages for all 4 exchanges
- `global_mid` price updates every ~1 second
- No `ERROR` or `CRITICAL` log lines

### API Status

```bash
curl https://your-app.railway.app/api/status | python3 -m json.tool
```

All 4 exchanges should show `"running": true`.

### WebSocket

```bash
wscat -c wss://your-app.railway.app/ws
```

---

## 5. Go Live

> ⚠️ **Only proceed after confirming dry-run mode is working correctly for 24+ hours.**

### Pre-live Checklist

- [ ] All 4 exchanges show `"running": true` in `/api/status`
- [ ] `tick_update` events are flowing in WebSocket
- [ ] Exchange balances shown in `/api/balances` match actual exchange balances
- [ ] No `ERROR` logs in the last 24 hours
- [ ] `ALERT_WEBHOOK_URL` is set (for Q-Switch alerts)
- [ ] `min_balance_usd` and `min_balance_token` thresholds are correctly set in `bot.json`
- [ ] Initial balances in `bot.json → initial_balances` match current actual balances

### Enable Live Trading

In Railway dashboard → Variables:

1. Change `LIVE_MODE` from `false` to `true`
2. Trigger a redeploy (Railway will restart automatically when a variable changes)

Then in `bot.json`, change `dry_run` to `false` and push:

```bash
# Edit bot.json locally
# Change "dry_run": true → "dry_run": false

git add bot.json
git commit -m "Enable live trading"
git push origin main
```

Or use the hot-reload API (no restart needed for `dry_run` toggle? — actually `dry_run` changes require restart for safety):

```bash
# Via Railway variables change - restart is triggered automatically
```

### Verify Live Mode

```bash
# Watch for real order placements
wscat -c wss://your-app.railway.app/ws
# Should see "order_placed" events within seconds

# Check open orders
curl https://your-app.railway.app/api/orders
```

---

## 6. Operations

### Pause Trading

```bash
curl -X POST https://your-app.railway.app/api/control/pause
```

### Resume Trading

```bash
curl -X POST https://your-app.railway.app/api/control/resume
```

### Emergency Stop (cancel all orders immediately)

```bash
curl -X POST https://your-app.railway.app/api/control/emergency_stop
```

After an emergency stop, orders are cancelled and the bot halts. Resume with:

```bash
curl -X POST https://your-app.railway.app/api/control/resume
```

### Update Config (no restart)

```bash
# Fetch current config
curl https://your-app.railway.app/api/config > config.json

# Edit config.json (e.g. tighten spreads)

# Push updated config
curl -X PUT https://your-app.railway.app/api/config \
  -H "Content-Type: application/json" \
  -d @config.json
```

### View Fills

```bash
curl "https://your-app.railway.app/api/fills?limit=50"
```

---

## 7. Database Backup

The SQLite database at `/app/data/mm_bot.db` is stored on the Railway volume. To back it up:

```bash
# Download via Railway CLI
railway run -- cp /app/data/mm_bot.db /tmp/mm_bot_backup.db
# Then use railway shell or similar to retrieve it
```

Or query it directly via the API endpoints (`/api/fills`, `/api/orders`, `/api/metrics`).

---

## 8. Upgrading

### Rolling Upgrade (recommended)

1. Push new code to `main`
2. Railway will deploy without downtime (if health check passes)
3. The bot restarts — orders on exchanges remain open during the brief restart

### If Startup Fails

Railway will roll back to the previous deployment automatically (due to healthcheck). Check logs:

```bash
railway logs --deployment previous
```

---

## 9. Troubleshooting

### Bot says "running: false" for an exchange

1. Check logs: `railway logs | grep ERROR`
2. Check exchange API key is valid (not expired, has trading permissions)
3. Check exchange is not under maintenance
4. Try restarting the specific exchange via config: set `enabled: false` then `enabled: true` via hot-reload

### "Q-Switch triggered" in logs

Balance on an exchange dropped below minimum threshold. Check:
1. `/api/balances` — which exchange is under threshold
2. Deposit funds to that exchange
3. Update `min_balance_usd` / `min_balance_token` in config if thresholds need adjusting
4. Resume: `POST /api/control/resume`

### "Circuit breaker tripped"

Daily loss or drawdown exceeded threshold. Review:
1. `/api/fills` — review fill history
2. `/api/metrics` — check P&L
3. After reviewing: `POST /api/control/reset_circuit_breaker`

### High API error rate

1. Check exchange status pages
2. Reduce `max_requests_per_second` in `bot.json → safety`
3. CCXT rate limiting should handle this automatically, but lower the limit if needed

### Database missing after redeploy

Volume wasn't mounted correctly. Verify:
1. Railway volume is attached with mount path `/app/data`
2. `DB_PATH=/app/data/mm_bot.db` is set in env vars
3. Volume persists between deploys (check Railway volumes tab)

---

## 10. Resource Requirements

Railway's free tier may not be sufficient for production. Recommended:

| Resource | Recommended | Notes |
|----------|-------------|-------|
| RAM | 512MB | Python asyncio is lightweight; 256MB minimum |
| CPU | 0.5 vCPU | I/O bound; minimal CPU needed |
| Disk | 1GB volume | SQLite DB grows ~10MB/day with rl_features |
| Network | Unlimited | 4 exchanges × ~1 req/s each |

Typical Railway plan: **Hobby** ($5/mo) or **Pro** for production use.
