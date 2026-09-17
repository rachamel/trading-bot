# main.py
# =============================================================================
# SERVERLESS QUANT BOT - 1H IN & OUT MODEL (LAB2-VALIDATED FINAL CONFIG)
# Z=1.5 | SL=2.5xATR(50) | TP=50-SMA static limit | tailcap |Z|<=3.5
# Trend filter: 4H EMA200 | Risk: 0.5% | Scan: hourly at :05 UTC
# =============================================================================

import requests
import pandas as pd
import numpy as np
import yfinance as yf
import time
import os

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or "8712031624:AAH8GKakWgeuFaR8VvKeox2TbusGdZwE_xE"
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or "5858961660"

ACCOUNT_BALANCE = 1000.0     # UPDATE MONTHLY to your real balance (this is how compounding works)
RISK_PER_TRADE = 0.005       # 0.5% risk per trade ($5 on a $1,000 account)

# --- LAB2-VALIDATED PARAMETERS ---
ZSCORE_ENTRY = 1.5           # Z-Score crossover threshold
TAIL_CAP = 3.5               # reject entries beyond |Z| 3.5 (crashes, not pullbacks)
LOOKBACK_1H = 50             # Z-Score & ATR lookback on 1H
ATR_MULTIPLIER_SL = 2.5      # Stop Loss = 2.5 x ATR(50)
TREND_EMA_4H = 200           # 4H EMA200 trend filter

ALL_PAIRS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "USDCAD=X", "AUDUSD=X", "NZDUSD=X",
    "GC=F", "SI=F", "EURGBP=X", "EURJPY=X", "GBPJPY=X", "AUDJPY=X", "CHFJPY=X", "EURAUD=X", "GBPAUD=X",
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "ADA-USD",
    "AVAX-USD", "LINK-USD", "DOT-USD", "LTC-USD", "NEAR-USD"
]

# --- TELEGRAM ---
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code == 200: print("✅ Telegram sent")
        else: print(f"❌ Telegram {r.status_code}: {r.text}")
    except Exception as e:
        print(f"❌ Telegram error: {e}")

def fmt(p, s):
    if p >= 100: return f"{p:.2f}"
    if p >= 10: return f"{p:.3f}"
    if "JPY" in s: return f"{p:.3f}"
    return f"{p:.5f}"

# --- DATA (closed candles only - no lookahead) ---
def get_data(symbol):
    try:
        raw = yf.download(symbol, period="120d", interval="1h", progress=False)
        if raw.empty: return None, None
        if isinstance(raw.columns, pd.MultiIndex): raw.columns = raw.columns.droplevel(1)
        raw.columns = [c.lower() for c in raw.columns]

        now = pd.Timestamp.now(tz='UTC')
        raw = raw[raw.index + pd.Timedelta(hours=1) <= now]      # closed 1H only
        if len(raw) < LOOKBACK_1H + 2: return None, None

        df4 = raw.resample('4h').agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()
        df4 = df4[df4.index + pd.Timedelta(hours=4) <= now]      # closed 4H only
        if len(df4) < TREND_EMA_4H: return None, None
        return raw, df4
    except Exception as e:
        print(f"[YF] {symbol}: {e}")
        return None, None

# --- SIGNAL ENGINE (mirrors lab2.py simulation exactly) ---
def evaluate(symbol, df1, df4):
    if df1 is None or df4 is None: return None

    c = df1['close']
    sma = c.rolling(LOOKBACK_1H).mean()
    std = c.rolling(LOOKBACK_1H).std()
    z = (c - sma) / std.replace(0, np.nan)

    pc = c.shift(1)
    tr = np.maximum(df1['high'] - df1['low'],
                    np.maximum(abs(df1['high'] - pc), abs(df1['low'] - pc)))
    atr = tr.rolling(LOOKBACK_1H).mean()

    ema4 = df4['close'].ewm(span=TREND_EMA_4H, adjust=False).mean()
    uptrend = df4['close'].iloc[-1] > ema4.iloc[-1]
    downtrend = df4['close'].iloc[-1] < ema4.iloc[-1]

    zl, zp = z.iloc[-1], z.iloc[-2]
    a = atr.iloc[-1]
    if pd.isna(zl) or pd.isna(zp) or pd.isna(a) or a <= 0: return None

    # TAILCAP FILTER: skip crashes/squeezes, only trade statistical stretches
    if abs(zl) > TAIL_CAP: return None

    entry = c.iloc[-1]
    risk_dist = ATR_MULTIPLIER_SL * a
    tp_price = sma.iloc[-1]                     # static mean-reversion target
    if pd.isna(tp_price): return None

    # LONG: Z crosses below -1.5 during 4H uptrend
    if zp >= -ZSCORE_ENTRY and zl < -ZSCORE_ENTRY and uptrend:
        sl = entry - risk_dist
        if tp_price <= entry: return None       # no room to the mean: skip
        r_target = (tp_price - entry) / risk_dist
        return {"symbol": symbol, "direction": "🟢 LONG", "entry": entry,
                "sl": sl, "tp": tp_price, "z": zl, "r_target": r_target}

    # SHORT: Z crosses above +1.5 during 4H downtrend
    if zp <= ZSCORE_ENTRY and zl > ZSCORE_ENTRY and downtrend:
        sl = entry + risk_dist
        if tp_price >= entry: return None
        r_target = (entry - tp_price) / risk_dist
        return {"symbol": symbol, "direction": "🔴 SHORT", "entry": entry,
                "sl": sl, "tp": tp_price, "z": zl, "r_target": r_target}

    return None

# --- MAIN ---
def main():
    print("🤖 1H In & Out scan (Lab2 final config)...")
    signals = []
    for pair in ALL_PAIRS:
        df1, df4 = get_data(pair)
        sig = evaluate(pair, df1, df4)
        if sig: signals.append(sig)
        time.sleep(0.2)

    risk_amount = round(ACCOUNT_BALANCE * RISK_PER_TRADE, 2)

    if signals:
        send_telegram(f"🚨 *{len(signals)} NEW 1H SIGNAL(S)* 🚨")
        for s in signals:
            msg = (
                f"⏱️ *Model:* 1H IN & OUT (Lab2 config)\n"
                f"📊 *Asset:* `{s['symbol']}`\n"
                f"🧭 *Direction:* {s['direction']}\n"
                f"🎯 *Entry:* ~`{fmt(s['entry'], s['symbol'])}` (market)\n"
                f"🛑 *Stop Loss:* `{fmt(s['sl'], s['symbol'])}` (2.5×ATR)\n"
                f"💰 *Take Profit:* `{fmt(s['tp'], s['symbol'])}` (50-SMA limit)\n"
                f"📐 *Target:* +{s['r_target']:.2f}R\n\n"
                f"🛡️ *Risk (0.5%):* `${risk_amount}`\n"
                f"🧠 *Reason:* 1H Z crossed to {s['z']:.2f} | 4H trend aligned | tail OK\n"
                f"⚠️ Set SL+TP as bracket immediately. Never widen the stop."
            )
            send_telegram(msg)
    else:
        # Silent unless daily heartbeat
        if pd.Timestamp.now(tz='UTC').hour == 20:
            send_telegram("🫀 *Daily Heartbeat (20:05 UTC)*\nBot alive • 1H Lab2 config • 26 assets scanned • no setups this hour.")

    print(f"✅ Scan complete. Signals: {len(signals)}")

if __name__ == "__main__":
    main()