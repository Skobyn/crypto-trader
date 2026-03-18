# CryptoTrader — Paper Trading Bot + Dashboard

A polished, real-time paper trading crypto bot with a dark dashboard UI. No real money. No exchange keys required to run. Trades itself automatically using configurable strategies.

![CryptoTrader Dashboard](docs/screenshot-placeholder.png)

---

## What It Does

| Feature | Details |
|---|---|
| **Live price feed** | Simulated tick every 1s (±0.2% vol + mean reversion). Swap for real WebSocket feed in `price_feed_loop()` |
| **4 strategies** | Momentum (SMA crossover), Mean Reversion (Bollinger), Grid Trading, Dollar Cost Average |
| **User dials** | Risk %, leverage (1–10×), max daily loss halt, max position size, trade frequency |
| **Simulated wallet** | $100k starting cash, real-time P&L, position tracking |
| **Trade log** | Every buy/sell with price, qty, value, P&L, strategy reason |
| **Live charts** | Price history per symbol with Chart.js |
| **WebSocket** | All data pushed to browser in real time — no polling |
| **Reset** | One-click wallet reset |
| **Exchange stubs** | Placeholder config for Coinbase, Kraken, Binance in `backend/main.py` |

---

## Quick Start (Local)

```bash
# 1. Clone / copy the project
cd crypto-trader

# 2. Install Python deps
python3 -m pip install -r backend/requirements.txt --break-system-packages
# or: pip install -r backend/requirements.txt

# 3. Run
./run.sh
# or manually:
uvicorn backend.main:app --host 0.0.0.0 --port 8080 --reload

# 4. Open
open http://localhost:8080
```

---

## Docker

```bash
docker compose up --build
# Opens at http://localhost:8080
```

Or single container:
```bash
docker build -t cryptotrader .
docker run -p 8080:8080 cryptotrader
```

---

## Project Structure

```
crypto-trader/
├── backend/
│   ├── main.py           # FastAPI app — price feed, bot, REST + WebSocket
│   └── requirements.txt
├── frontend/
│   └── index.html        # Full dashboard (vanilla HTML/JS + Chart.js CDN)
├── Dockerfile
├── docker-compose.yml
├── run.sh
└── README.md
```

---

## Strategies

| Strategy | Logic |
|---|---|
| **Momentum** | Buy when price > 20-tick SMA × 1.005; sell when price < SMA × 0.995 |
| **Mean Reversion** | Buy below lower Bollinger band (2σ); sell above upper band |
| **Grid** | Buy entries every 1% drop; take profit every 1% gain |
| **DCA** | Buy 0.5% of portfolio in top 3 coins every cycle |

---

## Controls

| Dial | Range | Effect |
|---|---|---|
| Risk per trade | 0.1%–10% | % of portfolio budget per signal |
| Leverage | 1×–10× | Multiplies position size (simulated) |
| Max daily loss | 1%–50% | Halts bot when breached |
| Max position size | 5%–100% | Caps exposure per coin |
| Trade frequency | 5s–120s | Bot cycle interval |
| Bot toggle | on/off | Pause without losing settings |

---

## API Endpoints

```
GET  /api/prices              — Current prices for all symbols
GET  /api/wallet              — Portfolio value, cash, positions, P&L
GET  /api/trades?limit=100    — Trade log (most recent first)
GET  /api/price_history/{sym} — OHLC-style history for charting
GET  /api/strategy            — Current strategy settings
POST /api/strategy            — Update settings (JSON body)
POST /api/reset               — Reset wallet to $100k
WS   /ws                      — WebSocket: prices, wallet, trades, strategy
GET  /health                  — Health check
```

---

## Live Exchange Integration (Future)

When you're ready to go live, the architecture is exchange-agnostic by design.

### Step 1 — Wire the price feed
In `backend/main.py`, replace `simulate_price()` and `price_feed_loop()` with a real WebSocket feed:

```python
# Example: Coinbase Advanced Trade WebSocket
import coinbase
async def price_feed_loop():
    async with coinbase.WSClient(api_key=..., api_secret=...) as ws:
        await ws.subscribe(product_ids=["BTC-USD", "ETH-USD"], channels=["ticker"])
        async for msg in ws:
            symbol = msg["product_id"].replace("-", "/")
            prices[symbol] = float(msg["price"])
            # ... broadcast to frontend
```

### Step 2 — Wire order execution
Replace `execute_buy()` / `execute_sell()` with real exchange calls:

```python
# Coinbase
from coinbase.rest import RESTClient
client = RESTClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_API_SECRET)
client.market_order_buy(client_order_id=str(uuid4()), product_id="BTC-USD", quote_size=str(usd_amount))

# Kraken
import krakenex
api = krakenex.API(key=KRAKEN_API_KEY, secret=KRAKEN_API_SECRET)
api.query_private("AddOrder", {"pair":"XBTUSD","type":"buy","ordertype":"market","volume":str(qty)})

# Binance
from binance.client import Client
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
client.order_market_buy(symbol="BTCUSDT", quoteOrderQty=usd_amount)
```

### Step 3 — Set env vars
```bash
export COINBASE_API_KEY=your_real_key
export COINBASE_API_SECRET=your_real_secret
# Toggle EXCHANGE_CONFIG["coinbase"]["enabled"] = True in main.py
```

---

## Deploy to Cloud Run (GCP)

```bash
# Build and push
gcloud builds submit --tag gcr.io/YOUR_PROJECT/cryptotrader .

# Deploy
gcloud run deploy cryptotrader \
  --image gcr.io/YOUR_PROJECT/cryptotrader \
  --platform managed \
  --region us-central1 \
  --port 8080 \
  --allow-unauthenticated
```

---

## Tech Stack

- **Backend:** FastAPI + uvicorn + WebSockets
- **Frontend:** Vanilla HTML/CSS/JS (no build step, no npm)
- **Charts:** Chart.js (CDN)
- **Data:** In-memory (Firestore/Postgres drop-in ready)
- **Deploy:** Docker, Cloud Run, or bare metal

---

## Notes

- **Paper trading only** — no real money moves until you flip `EXCHANGE_CONFIG["*"]["enabled"] = True` and wire real order functions
- Price simulation uses a random walk with mean reversion — realistic enough for strategy testing
- Wallet resets on server restart (add Firestore/SQLite for persistence)
- Default starting capital: **$100,000**
