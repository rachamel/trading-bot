# main.py
# =============================================================================
# SERVERLESS QUANT BOT (UNIFIED YFINANCE -> TELEGRAM)
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

ACCOUNT_BALANCE = 10000.0
RISK_PER_TRADE = 0.01
RR_RATIO = 2.5
ATR_MULTIPLIER_SL = 1.5
ZSCORE_ENTRY_THRESHOLD = 2.0
LOOKBACK_PERIOD = 20

# --- ASSETS ---
# Yahoo Finance format for Forex & Metals
YFINANCE_PAIRS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "USDCAD=X", "AUDUSD=X", "NZDUSD=X",
    "GC=F", "SI=F",  # Gold and Silver Futures 
    "EURGBP=X", "EURJPY=X", "GBPJPY=X", "AUDJPY=X", "CHFJPY=X", "EURAUD=X", "GBPAUD=X"
]

# Yahoo Finance format for Crypto (No more geo-blocking!)
CRYPTO_PAIRS = [
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "ADA-USD",
    "AVAX-USD", "LINK-USD", "DOT-USD", "LTC-USD", "NEAR-USD"
]

# --- TELEGRAM SENDER ---
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code == 200:
            print("✅ Telegram message sent successfully.")
        else:
            print(f"❌ Telegram API error {r.status_code}: {r.text}")
    except Exception as e:
        print(f"❌ Telegram connection error: {e}")

# --- DATA FETCHER (Unified for Forex, Metals, and Crypto) ---
def get_yfinance_data(symbol):
    """Fetches 1H data from Yahoo Finance and resamples to 4H and 1D locally."""
    try:
        # Download 1H data (YF allows max 730 days for 1H, we only need 60 days)
        raw = yf.download(symbol, period="60d", interval="1h", progress=False)
        if raw.empty: 
            print(f"[YFinance] No data returned for {symbol}")
            return None, None
        
        # Handle MultiIndex columns if YF returns them
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.droplevel(1)
            
        # Clean up column names to lowercase
        raw.columns = [c.lower() for c in raw.columns]
        
        # Resample 1H data into 4H candles
        df_4h = raw.resample('4h').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
        }).dropna().reset_index()
        
        # Resample 1H data into Daily (1D) candles
        df_1d = raw.resample('1D').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
        }).dropna().reset_index()
        
        return df_4h, df_1d
        
    except Exception as e:
        print(f"[YFinance] Error for {symbol}: {e}")
        return None, None

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
    if df is None or df.empty: return False
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
    print("🤖 Starting Serverless Scan (Unified YFinance)...")
    send_telegram("🟢 *Quant Bot Online*\nStarting scheduled 4H scan...")

    signals_found = []

    # 1. Scan Forex & Metals
    for pair in YFINANCE_PAIRS:
        df_4h, df_1d = get_yfinance_data(pair)
        signal = evaluate_signal(pair, df_4h, df_1d)
        if signal: signals_found.append(signal)
        time.sleep(0.1) 

    # 2. Scan Crypto (Now using YFinance to avoid geo-blocking)
    for pair in CRYPTO_PAIRS:
        df_4h, df_1d = get_yfinance_data(pair)
        signal = evaluate_signal(pair, df_4h, df_1d)
        if signal: signals_found.append(signal)
        time.sleep(0.1)

    # 3. Send Results
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