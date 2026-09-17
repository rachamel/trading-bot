# main.py
# =============================================================================
# SERVERLESS QUANT BOT (TWELVE DATA + BINANCE -> TELEGRAM) WITH DIAGNOSTICS
# =============================================================================

import requests
import pandas as pd
import numpy as np
import ccxt
import time
import os

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = os.environ.get("8712031624:AAH8GKakWgeuFaR8VvKeox2TbusGdZwE_xE")
TELEGRAM_CHAT_ID = os.environ.get("5858961660")
TWELVE_DATA_API_KEY = os.environ.get("f3245085e2aa49b5a6959ca5e395865a")

ACCOUNT_BALANCE = 10000.0
RISK_PER_TRADE = 0.01
RR_RATIO = 2.5
ATR_MULTIPLIER_SL = 1.5
ZSCORE_ENTRY_THRESHOLD = 2.0
LOOKBACK_PERIOD = 20

# --- ASSETS ---
TWELVE_DATA_PAIRS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "USD/CAD", "AUD/USD", "NZD/USD",
    "XAU/USD", "XAG/USD",
    "EUR/GBP", "EUR/JPY", "GBP/JPY", "AUD/JPY", "CHF/JPY", "EUR/AUD", "GBP/AUD"
]

CRYPTO_PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT",
    "AVAX/USDT", "LINK/USDT", "DOT/USDT", "LTC/USDT", "NEAR/USDT"
]

# --- TELEGRAM SENDER (WITH DIAGNOSTICS) ---
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN:
        print("❌ DIAGNOSTIC: TELEGRAM_BOT_TOKEN is missing from GitHub Secrets.")
        return False
    if not TELEGRAM_CHAT_ID:
        print("❌ DIAGNOSTIC: TELEGRAM_CHAT_ID is missing from GitHub Secrets.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code == 200:
            print("✅ Telegram message sent successfully.")
            return True
        else:
            print(f"❌ Telegram API error {r.status_code}: {r.text}")
            return False
    except Exception as e:
        print(f"❌ Telegram connection error: {e}")
        return False

# --- DATA FETCHERS ---
def get_twelve_data(instrument, interval="4h", outputsize=250):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": instrument,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY,
        "format": "JSON"
    }
    try:
        response = requests.get(url, params=params)
        data = response.json()
        if "status" in data and data["status"] == "error":
            print(f"[12Data] Error for {instrument}: {data.get('message')}")
            return pd.DataFrame()
        values = data.get("values", [])
        if not values: return pd.DataFrame()
        df = pd.DataFrame(reversed(values))
        for col in ['open', 'high', 'low', 'close']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df.dropna()
    except Exception as e:
        print(f"[12Data] Fetch Error {instrument}: {e}")
        return pd.DataFrame()

def get_crypto_data(symbol, timeframe="4h", limit=250):
    try:
        exchange = ccxt.binance()
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        return df
    except Exception as e:
        print(f"[Binance] Error fetching {symbol}: {e}")
        return pd.DataFrame()

# --- MATH ENGINE ---
def calculate_indicators(df):
    if df.empty or len(df) < LOOKBACK_PERIOD: return df
    df['sma'] = df['close'].rolling(window=LOOKBACK_PERIOD).mean()
    df['std'] = df['close'].rolling(window=LOOKBACK_PERIOD).std()
    df['zscore'] = (df['close'] - df['sma']) / df['std'].replace(0, np.nan)
    df['prev_close'] = df['close'].shift(1)
    df['tr'] = np.maximum(df['high'] - df['low'],
                          np.maximum(abs(df['high'] - df['prev_close']), abs(df['low'] - df['prev_close'])))
    df['atr'] = df['tr'].rolling(window=LOOKBACK_PERIOD).mean()
    df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
    return df.dropna()

def is_data_clean(df):
    if df.empty: return False
    latest = df.iloc[-1]
    if latest['high'] == latest['low'] == latest['open'] == latest['close']:
        return False
    return True

def evaluate_signal(symbol, df_4h, df_1d):
    if not is_data_clean(df_4h) or not is_data_clean(df_1d): return None
    df_4h = calculate_indicators(df_4h)
    df_1d = calculate_indicators(df_1d)
    if len(df_4h) < 2 or len(df_1d) < 2: return None
    if pd.isna(df_1d['ema_200'].iloc[-1]): return None

    latest = df_4h.iloc[-1]
    prev = df_4h.iloc[-2]
    htf_latest = df_1d.iloc[-1]

    is_uptrend = htf_latest['close'] > htf_latest['ema_200']
    is_downtrend = htf_latest['close'] < htf_latest['ema_200']
    if not is_uptrend and not is_downtrend: return None

    if prev['zscore'] >= -ZSCORE_ENTRY_THRESHOLD and latest['zscore'] < -ZSCORE_ENTRY_THRESHOLD and is_uptrend:
        risk_amount = ACCOUNT_BALANCE * RISK_PER_TRADE
        sl_distance = ATR_MULTIPLIER_SL * latest['atr']
        entry_price = latest['close']
        return {"symbol": symbol, "direction": "🟢 LONG", "entry": entry_price,
                "sl": entry_price - sl_distance, "tp": entry_price + (RR_RATIO * sl_distance),
                "risk_amount": round(risk_amount, 2), "reason": f"Z-Score: {latest['zscore']:.2f}"}

    elif prev['zscore'] <= ZSCORE_ENTRY_THRESHOLD and latest['zscore'] > ZSCORE_ENTRY_THRESHOLD and is_downtrend:
        risk_amount = ACCOUNT_BALANCE * RISK_PER_TRADE
        sl_distance = ATR_MULTIPLIER_SL * latest['atr']
        entry_price = latest['close']
        return {"symbol": symbol, "direction": "🔴 SHORT", "entry": entry_price,
                "sl": entry_price + sl_distance, "tp": entry_price - (RR_RATIO * sl_distance),
                "risk_amount": round(risk_amount, 2), "reason": f"Z-Score: {latest['zscore']:.2f}"}

    return None

# --- MAIN EXECUTION ---
def main():
    print("🤖 Starting Serverless Scan...")
    send_telegram("🟢 *Quant Bot Online*\nStarting scheduled 4H scan...")

    signals_found = []

    for pair in TWELVE_DATA_PAIRS:
        df_4h = get_twelve_data(pair, interval="4h")
        df_1d = get_twelve_data(pair, interval="1day")
        signal = evaluate_signal(pair, df_4h, df_1d)
        if signal: signals_found.append(signal)
        time.sleep(0.5)

    for pair in CRYPTO_PAIRS:
        df_4h = get_crypto_data(pair, timeframe="4h")
        df_1d = get_crypto_data(pair, timeframe="1d")
        signal = evaluate_signal(pair, df_4h, df_1d)
        if signal: signals_found.append(signal)
        time.sleep(0.2)

    if not signals_found:
        send_telegram("✅ *Auto-Scan Complete*\n\nNo high-probability setups detected.\nThe model is preserving capital.")
    else:
        send_telegram(f"🚨 *{len(signals_found)} NEW SIGNALS DETECTED* 🚨")
        for sig in signals_found:
            msg = (
                f"📊 *Asset:* `{sig['symbol']}`\n"
                f"🧭 *Direction:* {sig['direction']}\n"
                f"🎯 *Entry:* `{sig['entry']:.5f}`\n"
                f"🛑 *Stop Loss:* `{sig['sl']:.5f}`\n"
                f"💰 *Take Profit:* `{sig['tp']:.5f}`\n\n"
                f"🛡️ *Risk (1%):* `${sig['risk_amount']}`\n"
                f"🧠 *Reason:* {sig['reason']}"
            )
            send_telegram(msg)

    print(f"✅ Scan complete. Signals found: {len(signals_found)}")

if __name__ == "__main__":
    main()