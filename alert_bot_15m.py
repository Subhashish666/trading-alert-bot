#!/usr/bin/env python3
"""
BTCUSDT 15m EMA7/EMA21 + ADX(14) alert bot -> Telegram
Author: (you)
Notes:
- Uses Binance public websocket for 15m klines.
- Implements EMA7, EMA21, ADX(14) (manual calc), and the 1.5% sideways filter.
- Sends two kinds of Telegram messages:
    ALERT 1: ADX crossed ABOVE 20 🔥
    ALERT 2: EMA7 & EMA21 CROSSED while ADX ≥ 20 ✅
- Configure BOT_TOKEN and CHAT_ID below or via environment variables.
"""

import os
import asyncio
import json
import math
import statistics
from collections import deque

import aiohttp
import websockets

# ========== CONFIG ==========
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "<PUT_YOUR_BOT_TOKEN_HERE>")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "<PUT_YOUR_CHAT_ID_HERE>")

SYMBOL = os.environ.get("SYMBOL", "btcusdt")     # lowercase for stream e.g. btcusdt, ethusdt
INTERVAL = os.environ.get("INTERVAL", "15m")     # 15m
FAST_EMA = int(os.environ.get("FAST_EMA", 7))
SLOW_EMA = int(os.environ.get("SLOW_EMA", 21))
ADX_LEN  = int(os.environ.get("ADX_LEN", 14))
ADX_LEVEL= float(os.environ.get("ADX_LEVEL", 20.0))
PERCENT_DIFF = float(os.environ.get("PERCENT_DIFF", 1.5))  # 1.5%

# WebSocket endpoint for Binance public stream
WS_URL = f"wss://stream.binance.com:9443/ws/{SYMBOL}@kline_{INTERVAL}"

# Keep last N candles; ADX needs several (we'll store 200 to be safe)
MAX_CANDLES = 200

# ========== HELPERS ==========
def ema_from_series(prev_ema, price, period):
    """single-step EMA update"""
    alpha = 2.0 / (period + 1.0)
    return (price - prev_ema) * alpha + prev_ema

def compute_ema_list(closes, period):
    """Return EMA series (same length as closes). First EMA uses SMA seed."""
    if len(closes) < period:
        return [None] * len(closes)
    emas = [None] * len(closes)
    sma = sum(closes[:period]) / period
    emas[period-1] = sma
    for i in range(period, len(closes)):
        emas[i] = (closes[i] - emas[i-1]) * (2/(period+1)) + emas[i-1]
    return emas

# Manual ADX implementation based on Wilder's smoothing
def compute_adx(highs, lows, closes, period=14):
    n = len(highs)
    if n < period + 1:
        return [None] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]),
                    abs(lows[i] - closes[i-1]))
    # Wilder smoothing (RMA)
    def rma(series, per):
        out = [None] * len(series)
        # seed with simple average of first `per` valid entries (skip index 0)
        seed_sum = sum(series[1:per+1])
        out[per] = seed_sum / per
        for i in range(per+1, len(series)):
            out[i] = (out[i-1] * (per - 1) + series[i]) / per
        return out

    atr = rma(tr, period)
    plus_r = rma(plus_dm, period)
    minus_r = rma(minus_dm, period)

    plus_di = [None] * n
    minus_di = [None] * n
    dx = [None] * n
    adx = [None] * n

    for i in range(n):
        if atr[i] is None or atr[i] == 0:
            continue
        plus_di[i] = 100.0 * (plus_r[i] / atr[i])
        minus_di[i] = 100.0 * (minus_r[i] / atr[i])
        if plus_di[i] is not None and minus_di[i] is not None and (plus_di[i] + minus_di[i]) != 0:
            dx[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / (plus_di[i] + minus_di[i])
    # ADX is RMA of DX
    adx = rma([0.0 if v is None else v for v in dx], period)
    return adx

async def telegram_send(session, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            if resp.status != 200:
                data = await resp.text()
                print("Telegram send failed:", resp.status, data)
            else:
                pass
    except Exception as e:
        print("Telegram error:", e)

# ========== CORE ==========
async def run_bot():
    print("Starting bot for", SYMBOL.upper(), INTERVAL)
    candles = deque(maxlen=MAX_CANDLES)  # each item: dict with high/low/open/close/volume/timestamp
    async with aiohttp.ClientSession() as http_session:
        async for ws in websockets.connect(WS_URL):
            try:
                async for msg in ws:
                    data = json.loads(msg)
                    # kline data lives at data['k']
                    k = data.get("k", {})
                    is_closed = k.get("x", False)
                    open_p = float(k["o"])
                    high_p = float(k["h"])
                    low_p = float(k["l"])
                    close_p = float(k["c"])
                    vol = float(k.get("v", 0))
                    start_ts = int(k["t"])
                    # Append or update latest candle
                    if not candles or candles[-1]["start_ts"] != start_ts:
                        candles.append({
                            "start_ts": start_ts,
                            "open": open_p,
                            "high": high_p,
                            "low": low_p,
                            "close": close_p,
                            "volume": vol
                        })
                    else:
                        candles[-1].update({
                            "open": open_p,
                            "high": high_p,
                            "low": low_p,
                            "close": close_p,
                            "volume": vol
                        })
                    # Proceed only when we have enough candles
                    if len(candles) < max(ADX_LEN + 5, SLOW_EMA + 5):
                        continue

                    # Build arrays
                    highs = [c["high"] for c in candles]
                    lows = [c["low"] for c in candles]
                    closes = [c["close"] for c in candles]

                    # Compute EMA series (we only need last values)
                    ema_fast_list = compute_ema_list(closes, FAST_EMA)
                    ema_slow_list = compute_ema_list(closes, SLOW_EMA)
                    ema_fast = ema_fast_list[-1]
                    ema_slow = ema_slow_list[-1]

                    if ema_fast is None or ema_slow is None:
                        continue

                    # Sideways percent check
                    ema_diff_percent = abs(ema_fast - ema_slow) / closes[-1] * 100.0
                    valid_cross_pct = ema_diff_percent >= PERCENT_DIFF

                    # detect cross on the last bar (compare last two EMA values)
                    prev_ema_fast = ema_fast_list[-2]
                    prev_ema_slow = ema_slow_list[-2]
                    crossed = False
                    if prev_ema_fast is not None and prev_ema_slow is not None:
                        # crossover or crossunder
                        crossed = (prev_ema_fast <= prev_ema_slow and ema_fast > ema_slow) or \
                                  (prev_ema_fast >= prev_ema_slow and ema_fast < ema_slow)

                    # ADX calc
                    adx_series = compute_adx(highs, lows, closes, period=ADX_LEN)
                    adx_now = adx_series[-1]
                    adx_prev = adx_series[-2] if len(adx_series) >= 2 else None

                    # Conditions:
                    adx_cross_up = False
                    if adx_prev is not None and adx_now is not None:
                        adx_cross_up = (adx_prev < ADX_LEVEL) and (adx_now >= ADX_LEVEL)

                    adx_above = adx_now is not None and adx_now >= ADX_LEVEL

                    # ALERTS: Send messages ONLY on bar close to avoid duplicate spam.
                    # We use the klines 'x' field; earlier we also check whether is_closed is True.
                    # But to be safe, only send when kline is closed (is_closed True).
                    if is_closed:
                        if adx_cross_up:
                            text = f"ALERT 1: ADX crossed ABOVE {int(ADX_LEVEL)} 🔥\nPair: {SYMBOL.upper()}  Interval: {INTERVAL}"
                            print(text)
                            await telegram_send(http_session, text)

                        if crossed and valid_cross_pct and adx_above:
                            # Determine direction
                            direction = "BUY" if ema_fast > ema_slow else "SELL"
                            text = (f"ALERT 2: EMA{FAST_EMA} & EMA{SLOW_EMA} CROSSED while ADX ≥ {int(ADX_LEVEL)} ✅\n"
                                    f"Pair: {SYMBOL.upper()}  Interval: {INTERVAL}\nDirection: {direction}\n"
                                    f"Price: {closes[-1]:.2f}")
                            print(text)
                            await telegram_send(http_session, text)

            except websockets.ConnectionClosed:
                print("Websocket closed, reconnecting...")
                await asyncio.sleep(1)
            except Exception as e:
                print("Exception in websocket loop:", e)
                await asyncio.sleep(1)


if __name__ == "__main__":
    if "<PUT_YOUR_BOT_TOKEN_HERE>" in BOT_TOKEN or "<PUT_YOUR_CHAT_ID_HERE>" in CHAT_ID:
        print("ERROR: Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables or edit the script.")
        raise SystemExit(1)
    asyncio.run(run_bot())
