"""
CryptoTrader - Paper Trading Bot + Dashboard
FastAPI backend with WebSocket price feeds and trading engine
"""

import asyncio
import json
import math
import random
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import os
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="CryptoTrader", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Config / Exchange stubs (swap these for real API keys + exchange SDKs)
# ---------------------------------------------------------------------------
EXCHANGE_CONFIG = {
    "coinbase": {
        "api_key": "YOUR_COINBASE_API_KEY",
        "api_secret": "YOUR_COINBASE_API_SECRET",
        "enabled": False,  # flip to True for live
    },
    "kraken": {
        "api_key": "YOUR_KRAKEN_API_KEY",
        "api_secret": "YOUR_KRAKEN_PRIVATE_KEY",
        "enabled": False,
    },
    "binance": {
        "api_key": "YOUR_BINANCE_API_KEY",
        "api_secret": "YOUR_BINANCE_SECRET_KEY",
        "enabled": False,
    },
}

# ---------------------------------------------------------------------------
# Shared state (in-memory; swap Firestore/Postgres for production)
# ---------------------------------------------------------------------------
SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "MATIC/USD"]

# Simulated base prices
BASE_PRICES = {
    "BTC/USD": 68_000.0,
    "ETH/USD": 3_500.0,
    "SOL/USD": 180.0,
    "DOGE/USD": 0.18,
    "MATIC/USD": 0.95,
}

# Live price state
prices: dict[str, float] = dict(BASE_PRICES)
price_history: dict[str, deque] = {s: deque(maxlen=200) for s in SYMBOLS}

# Wallet
wallet = {
    "cash": 100_000.0,
    "initial_cash": 100_000.0,
    "positions": {},  # symbol -> {qty, avg_price, side}
    "daily_pnl": 0.0,
    "total_pnl": 0.0,
    "peak_value": 100_000.0,
    "day_start_value": 100_000.0,
}

# Trade log
trade_log: list[dict] = []

# Strategy settings
strategy_settings = {
    "active_strategy": "momentum",  # momentum | mean_reversion | grid | dca
    "risk_pct": 2.0,           # % of portfolio risked per trade
    "leverage": 1.0,           # 1x-10x
    "max_daily_loss_pct": 5.0, # halt trading if daily loss exceeds this %
    "max_position_size_pct": 20.0,  # max % of portfolio per position
    "trade_frequency": 30,     # seconds between bot cycles
    "enabled": True,           # bot on/off
}

# Connected WebSocket clients
ws_clients: list[WebSocket] = []

# Bot state
bot_last_run = 0.0
trading_halted = False


# ---------------------------------------------------------------------------
# Price simulation (replace with real websocket feed from exchange)
# ---------------------------------------------------------------------------
def simulate_price(symbol: str) -> float:
    """Random walk with mean reversion."""
    base = BASE_PRICES[symbol]
    current = prices[symbol]
    drift = 0.0001 * (base - current) / base   # mean revert
    shock = random.gauss(0, 0.002)             # 0.2% vol per tick
    new_price = current * (1 + drift + shock)
    new_price = max(new_price, base * 0.3)      # floor at 30% of base
    return round(new_price, 6)


async def price_feed_loop():
    """Tick every 1 second; broadcast via WebSocket."""
    while True:
        for symbol in SYMBOLS:
            new_price = simulate_price(symbol)
            prices[symbol] = new_price
            price_history[symbol].append({
                "t": int(time.time() * 1000),
                "p": new_price,
            })

        payload = json.dumps({
            "type": "prices",
            "data": {s: round(prices[s], 6) for s in SYMBOLS},
            "ts": int(time.time() * 1000),
        })
        dead = []
        for ws in ws_clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            ws_clients.remove(ws)

        await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# Trading strategies
# ---------------------------------------------------------------------------
def portfolio_value() -> float:
    total = wallet["cash"]
    for sym, pos in wallet["positions"].items():
        total += pos["qty"] * prices[sym]
    return total


def available_for_trade(symbol: str) -> float:
    """Cash available considering max position size."""
    pv = portfolio_value()
    max_pos_value = pv * strategy_settings["max_position_size_pct"] / 100.0
    current_pos_value = 0.0
    if symbol in wallet["positions"]:
        p = wallet["positions"][symbol]
        current_pos_value = p["qty"] * prices[symbol]
    return max(0, min(wallet["cash"], max_pos_value - current_pos_value))


def execute_buy(symbol: str, usd_amount: float, reason: str):
    price = prices[symbol]
    qty = (usd_amount * strategy_settings["leverage"]) / price
    if usd_amount <= 0 or qty <= 0:
        return
    wallet["cash"] -= usd_amount
    pos = wallet["positions"].get(symbol, {"qty": 0, "avg_price": price, "side": "long"})
    total_qty = pos["qty"] + qty
    avg = (pos["qty"] * pos["avg_price"] + qty * price) / total_qty
    wallet["positions"][symbol] = {"qty": total_qty, "avg_price": avg, "side": "long"}
    trade = {
        "id": str(uuid.uuid4())[:8],
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "side": "BUY",
        "qty": round(qty, 6),
        "price": price,
        "value": round(usd_amount, 2),
        "reason": reason,
        "leverage": strategy_settings["leverage"],
    }
    trade_log.append(trade)
    asyncio.create_task(broadcast_trade(trade))


def execute_sell(symbol: str, qty_pct: float, reason: str):
    if symbol not in wallet["positions"]:
        return
    pos = wallet["positions"][symbol]
    price = prices[symbol]
    qty = pos["qty"] * qty_pct
    proceeds = qty * price
    pnl = (price - pos["avg_price"]) * qty
    wallet["cash"] += proceeds
    wallet["total_pnl"] += pnl
    wallet["daily_pnl"] += pnl
    pos["qty"] -= qty
    if pos["qty"] < 1e-9:
        del wallet["positions"][symbol]
    else:
        wallet["positions"][symbol] = pos
    trade = {
        "id": str(uuid.uuid4())[:8],
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "side": "SELL",
        "qty": round(qty, 6),
        "price": price,
        "value": round(proceeds, 2),
        "pnl": round(pnl, 2),
        "reason": reason,
        "leverage": strategy_settings["leverage"],
    }
    trade_log.append(trade)
    asyncio.create_task(broadcast_trade(trade))


# --- Strategy implementations ---

def strategy_momentum():
    """Buy when price is above 20-tick SMA; sell when below."""
    for symbol in SYMBOLS:
        hist = list(price_history[symbol])
        if len(hist) < 20:
            continue
        sma = sum(h["p"] for h in hist[-20:]) / 20
        price = prices[symbol]
        budget = available_for_trade(symbol)
        risk_budget = portfolio_value() * strategy_settings["risk_pct"] / 100.0

        if price > sma * 1.005 and budget > 10:
            execute_buy(symbol, min(budget, risk_budget), "momentum_buy")
        elif price < sma * 0.995 and symbol in wallet["positions"]:
            execute_sell(symbol, 0.5, "momentum_sell")


def strategy_mean_reversion():
    """Buy oversold (price below lower Bollinger band); sell overbought."""
    for symbol in SYMBOLS:
        hist = list(price_history[symbol])
        if len(hist) < 20:
            continue
        prices_20 = [h["p"] for h in hist[-20:]]
        mean = sum(prices_20) / 20
        std = math.sqrt(sum((p - mean) ** 2 for p in prices_20) / 20)
        price = prices[symbol]
        lower_band = mean - 2 * std
        upper_band = mean + 2 * std
        budget = available_for_trade(symbol)
        risk_budget = portfolio_value() * strategy_settings["risk_pct"] / 100.0

        if price < lower_band and budget > 10:
            execute_buy(symbol, min(budget, risk_budget), "mean_rev_buy")
        elif price > upper_band and symbol in wallet["positions"]:
            execute_sell(symbol, 0.75, "mean_rev_sell")


def strategy_grid():
    """Grid: place buys every 1% below current price; sell every 1% above avg."""
    for symbol in SYMBOLS:
        price = prices[symbol]
        budget = available_for_trade(symbol)
        risk_budget = portfolio_value() * strategy_settings["risk_pct"] / 100.0

        if symbol in wallet["positions"]:
            pos = wallet["positions"][symbol]
            if price >= pos["avg_price"] * 1.01:
                execute_sell(symbol, 0.33, "grid_take_profit")
            elif price <= pos["avg_price"] * 0.99 and budget > 10:
                execute_buy(symbol, min(budget * 0.25, risk_budget), "grid_add")
        elif budget > 10:
            execute_buy(symbol, min(budget * 0.1, risk_budget), "grid_entry")


def strategy_dca():
    """Dollar Cost Average: buy fixed amount every cycle."""
    for symbol in SYMBOLS[:3]:  # Only top 3
        budget = available_for_trade(symbol)
        pv = portfolio_value()
        dca_amount = pv * 0.005  # 0.5% per cycle

        if budget > dca_amount and dca_amount > 5:
            execute_buy(symbol, dca_amount, "dca")


STRATEGIES = {
    "momentum": strategy_momentum,
    "mean_reversion": strategy_mean_reversion,
    "grid": strategy_grid,
    "dca": strategy_dca,
}


# ---------------------------------------------------------------------------
# Bot loop
# ---------------------------------------------------------------------------
async def bot_loop():
    global bot_last_run, trading_halted
    while True:
        now = time.time()
        freq = strategy_settings["trade_frequency"]

        if now - bot_last_run >= freq and strategy_settings["enabled"]:
            bot_last_run = now

            # Check daily loss halt
            pv = portfolio_value()
            day_loss_pct = (wallet["day_start_value"] - pv) / wallet["day_start_value"] * 100
            if day_loss_pct >= strategy_settings["max_daily_loss_pct"]:
                trading_halted = True
            else:
                trading_halted = False

            if not trading_halted:
                strategy_fn = STRATEGIES.get(strategy_settings["active_strategy"], strategy_momentum)
                try:
                    strategy_fn()
                except Exception as e:
                    print(f"[bot] strategy error: {e}")

            # Broadcast wallet update
            await broadcast_wallet()

        await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# Broadcast helpers
# ---------------------------------------------------------------------------
async def broadcast(payload: dict):
    msg = json.dumps(payload)
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.remove(ws)


async def broadcast_wallet():
    pv = portfolio_value()
    await broadcast({
        "type": "wallet",
        "data": {
            "cash": round(wallet["cash"], 2),
            "portfolio_value": round(pv, 2),
            "total_pnl": round(wallet["total_pnl"], 2),
            "daily_pnl": round(wallet["daily_pnl"], 2),
            "pnl_pct": round((pv - wallet["initial_cash"]) / wallet["initial_cash"] * 100, 3),
            "positions": {
                sym: {
                    "qty": round(p["qty"], 6),
                    "avg_price": round(p["avg_price"], 4),
                    "current_price": round(prices[sym], 4),
                    "value": round(p["qty"] * prices[sym], 2),
                    "pnl": round((prices[sym] - p["avg_price"]) * p["qty"], 2),
                    "pnl_pct": round((prices[sym] - p["avg_price"]) / p["avg_price"] * 100, 2),
                }
                for sym, p in wallet["positions"].items()
            },
            "trading_halted": trading_halted,
        },
    })


async def broadcast_trade(trade: dict):
    await broadcast({"type": "trade", "data": trade})


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------
@app.get("/api/prices")
def get_prices():
    return {s: round(prices[s], 6) for s in SYMBOLS}


@app.get("/api/wallet")
def get_wallet():
    pv = portfolio_value()
    return {
        "cash": round(wallet["cash"], 2),
        "portfolio_value": round(pv, 2),
        "total_pnl": round(wallet["total_pnl"], 2),
        "daily_pnl": round(wallet["daily_pnl"], 2),
        "pnl_pct": round((pv - wallet["initial_cash"]) / wallet["initial_cash"] * 100, 3),
        "positions": {
            sym: {
                "qty": round(p["qty"], 6),
                "avg_price": round(p["avg_price"], 4),
                "current_price": round(prices[sym], 4),
                "value": round(p["qty"] * prices[sym], 2),
                "pnl": round((prices[sym] - p["avg_price"]) * p["qty"], 2),
                "pnl_pct": round((prices[sym] - p["avg_price"]) / p["avg_price"] * 100, 2),
            }
            for sym, p in wallet["positions"].items()
        },
        "trading_halted": trading_halted,
    }


@app.get("/api/trades")
def get_trades(limit: int = 100):
    return list(reversed(trade_log[-limit:]))


@app.get("/api/price_history/{symbol}")
def get_price_history(symbol: str):
    safe = symbol.replace("-", "/")
    if safe not in price_history:
        return []
    return list(price_history[safe])


@app.get("/api/strategy")
def get_strategy():
    return strategy_settings


class StrategyUpdate(BaseModel):
    active_strategy: Optional[str] = None
    risk_pct: Optional[float] = None
    leverage: Optional[float] = None
    max_daily_loss_pct: Optional[float] = None
    max_position_size_pct: Optional[float] = None
    trade_frequency: Optional[int] = None
    enabled: Optional[bool] = None


class SetBalanceRequest(BaseModel):
    amount: float


class DepositRequest(BaseModel):
    amount: float


@app.post("/api/strategy")
async def update_strategy(update: StrategyUpdate):
    if update.active_strategy is not None:
        strategy_settings["active_strategy"] = update.active_strategy
    if update.risk_pct is not None:
        strategy_settings["risk_pct"] = max(0.1, min(10.0, update.risk_pct))
    if update.leverage is not None:
        strategy_settings["leverage"] = max(1.0, min(10.0, update.leverage))
    if update.max_daily_loss_pct is not None:
        strategy_settings["max_daily_loss_pct"] = max(1.0, min(50.0, update.max_daily_loss_pct))
    if update.max_position_size_pct is not None:
        strategy_settings["max_position_size_pct"] = max(5.0, min(100.0, update.max_position_size_pct))
    if update.trade_frequency is not None:
        strategy_settings["trade_frequency"] = max(5, min(3600, update.trade_frequency))
    if update.enabled is not None:
        strategy_settings["enabled"] = update.enabled
    await broadcast({"type": "strategy", "data": strategy_settings})
    return strategy_settings


@app.post("/api/reset")
async def reset_wallet():
    global trading_halted
    wallet["cash"] = 100_000.0
    wallet["initial_cash"] = 100_000.0
    wallet["positions"] = {}
    wallet["daily_pnl"] = 0.0
    wallet["total_pnl"] = 0.0
    wallet["peak_value"] = 100_000.0
    wallet["day_start_value"] = 100_000.0
    trade_log.clear()
    trading_halted = False
    for sym in SYMBOLS:
        prices[sym] = BASE_PRICES[sym]
        price_history[sym].clear()
    await broadcast_wallet()
    return {"status": "reset"}


@app.post("/api/set-balance")
async def set_balance(req: SetBalanceRequest):
    global trading_halted
    amount = round(req.amount, 2)
    if amount <= 0:
        return {"error": "Amount must be positive"}, 400
    wallet["cash"] = amount
    wallet["initial_cash"] = amount
    wallet["positions"] = {}
    wallet["daily_pnl"] = 0.0
    wallet["total_pnl"] = 0.0
    wallet["peak_value"] = amount
    wallet["day_start_value"] = amount
    trade_log.clear()
    trading_halted = False
    await broadcast_wallet()
    await broadcast({"type": "trades_reset"})
    return {"status": "ok", "balance": amount}


@app.post("/api/deposit")
async def deposit(req: DepositRequest):
    amount = round(req.amount, 2)
    if amount <= 0:
        return {"error": "Amount must be positive"}, 400
    wallet["cash"] += amount
    wallet["initial_cash"] += amount
    wallet["peak_value"] = max(wallet["peak_value"], portfolio_value())
    deposit_entry = {
        "id": str(uuid.uuid4())[:8],
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": "—",
        "side": "DEPOSIT",
        "qty": 0,
        "price": 0,
        "value": amount,
        "reason": f"Manual deposit",
    }
    trade_log.append(deposit_entry)
    await broadcast_wallet()
    await broadcast({"type": "trade", "data": deposit_entry})
    return {"status": "ok", "deposited": amount, "new_cash": round(wallet["cash"], 2)}


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.append(websocket)
    # Send initial snapshot
    await websocket.send_text(json.dumps({
        "type": "snapshot",
        "prices": {s: round(prices[s], 6) for s in SYMBOLS},
        "wallet": (await _wallet_dict()),
        "strategy": strategy_settings,
        "trades": list(reversed(trade_log[-50:])),
        "history": {s: list(price_history[s])[-100:] for s in SYMBOLS},
    }))
    try:
        while True:
            await websocket.receive_text()  # keep alive
    except WebSocketDisconnect:
        ws_clients.remove(websocket)


async def _wallet_dict():
    pv = portfolio_value()
    return {
        "cash": round(wallet["cash"], 2),
        "portfolio_value": round(pv, 2),
        "total_pnl": round(wallet["total_pnl"], 2),
        "daily_pnl": round(wallet["daily_pnl"], 2),
        "pnl_pct": round((pv - wallet["initial_cash"]) / wallet["initial_cash"] * 100, 3),
        "positions": {
            sym: {
                "qty": round(p["qty"], 6),
                "avg_price": round(p["avg_price"], 4),
                "current_price": round(prices[sym], 4),
                "value": round(p["qty"] * prices[sym], 2),
                "pnl": round((prices[sym] - p["avg_price"]) * p["qty"], 2),
                "pnl_pct": round((prices[sym] - p["avg_price"]) / p["avg_price"] * 100, 2),
            }
            for sym, p in wallet["positions"].items()
        },
        "trading_halted": trading_halted,
    }


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    # Seed price history
    for symbol in SYMBOLS:
        for _ in range(100):
            prices[symbol] = simulate_price(symbol)
            price_history[symbol].append({
                "t": int((time.time() - (100 - len(price_history[symbol]))) * 1000),
                "p": prices[symbol],
            })
    asyncio.create_task(price_feed_loop())
    asyncio.create_task(bot_loop())


@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


# Serve frontend
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

@app.get("/")
def serve_index():
    return FileResponse(FRONTEND_DIR / "index.html")

# Mount after all API routes
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
