# main.py
# =============================================================================
# UNIFIED QUANT BOT (4H SWING + 1H IN & OUT) -> TELEGRAM
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

# --- 4H MODEL PARAMETERS ---
ZSCORE_4H = 2.0
LOOKBACK_4H = 20

# --- 1H MODEL PARAMETERS ---
ZSCORE_1H = 2.5
LOOKBACK_1H = 50
LOOKBACK_TREND_4H = 200 # EMA for 1H model's trend filter

# --- ASSETS ---
ALL_PAIRS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "USDCAD=X", "AUDUSD=X", "NZDUSD=X",
    "GC=F", "SI=F", "EURGBP=X", "EURJPY=X", "GBPJPY=X", "AUDJPY=X", "CHFJPY=X", "EURAUD=X", "GBPAUD=X",
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "ADA-USD", "AVAX-USD", "LINK-USD", "DOT-USD", "LTC-USD", "NEAR-USD"
]

# --- TELEGRAM SENDER ---
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code == 200: print("✅ Telegram message sent.")
        else: print(f"❌ Telegram API error {r.status_code}: {r.text}")
    except Exception as e: print(f"❌ Telegram connection error: {e}")

# --- DATA FETCHER (Fetches 1H and resamples everything locally) ---
def get_all_timeframes(symbol):
    """Fetches 1H data and creates 4H and 1D dataframes locally."""
    try:
        raw = yf.download(symbol, period="60d", interval="1h", progress=False)
        if raw.empty: return None, None, None
        if isinstance(raw.columns, pd.MultiIndex): raw.columns = raw.columns.droplevel(1)
        raw.columns = [c.lower() for c in raw.columns]
        
        df_1h = raw.copy()
        
        # Resample to 4H (Used for 4H signal AND 1H trend filter)
        df_4h = raw.resample('4h').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
        }).dropna().reset_index()
        
        # Resample to 1D (Used for 4H trend filter)
        df_1d = raw.resample('1D').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
        }).dropna().reset_index()
        
        return df_1h, df_4h, df_1d
    except Exception as e:
        print(f"[YF] Error {symbol}: {e}")
        return None, None, None

# --- 4H SIGNAL EVALUATOR ---
def check_4h_signal(symbol, df_4h, df_1d):
    if df_4h is None or df_1d is None or len(df_4h) < LOOKBACK_4H + 2 or len(df_1d) < 201: return None
    
    # 4H Indicators
    df_4h['sma'] = df_4h['close'].rolling(window=LOOKBACK_4H).mean()
    df_4h['std'] = df_4h['close'].rolling(window=LOOKBACK_4H).std()
    df_4h['zscore'] = (df_4h['close'] - df_4h['sma']) / df_4h['std'].replace(0, np.nan)
    df_4h['prev_close'] = df_4h['close'].shift(1)
    df_4h['tr'] = np.maximum(df_4h['high'] - df_4h['low'], np.maximum(abs(df_4h['high'] - df_4h['prev_close']), abs(df_4h['low'] - df_4h['prev_close'])))
    df_4h['atr'] = df_4h['tr'].rolling(window=LOOKBACK_4H).mean()
    
    # 1D Trend Filter (EMA 200)
    df_1d['ema_200'] = df_1d['close'].ewm(span=200, adjust=False).mean()
    
    latest = df_4h.iloc[-1]
    prev = df_4h.iloc[-2]
    trend = df_1d.iloc[-1]
    
    is_uptrend = trend['close'] > trend['ema_200']
    is_downtrend = trend['close'] < trend['ema_200']
    
    # Crossover logic for 4H (Only alerts exactly when it crosses)
    if prev['zscore'] >= -ZSCORE_4H and latest['zscore'] < -ZSCORE_4H and is_uptrend:
        sl_dist = ATR_MULTIPLIER_SL * latest['atr']
        entry = latest['close']
        return {"symbol": symbol, "tf": "4H SWING", "direction": "🟢 LONG", "entry": entry, 
                "sl": entry - sl_dist, "tp": entry + (RR_RATIO * sl_dist), "reason": f"4H Z: {latest['zscore']:.2f}"}
                
    if prev['zscore'] <= ZSCORE_4H and latest['zscore'] > ZSCORE_4H and is_downtrend:
        sl_dist = ATR_MULTIPLIER_SL * latest['atr']
        entry = latest['close']
        return {"symbol": symbol, "tf": "4H SWING", "direction": "🔴 SHORT", "entry": entry, 
                "sl": entry + sl_dist, "tp": entry - (RR_RATIO * sl_dist), "reason": f"4H Z: {latest['zscore']:.2f}"}
                
    return None

# --- 1H SIGNAL EVALUATOR ---
def check_1h_signal(symbol, df_1h, df_4h):
    if df_1h is None or df_4h is None or len(df_1h) < LOOKBACK_1H + 1 or len(df_4h) < LOOKBACK_TREND_4H: return None
    
    # 1H Indicators
    df_1h['sma'] = df_1h['close'].rolling(window=LOOKBACK_1H).mean()
    df_1h['std'] = df_1h['close'].rolling(window=LOOKBACK_1H).std()
    df_1h['zscore'] = (df_1h['close'] - df_1h['sma']) / df_1h['std'].replace(0, np.nan)
    df_1h['prev_close'] = df_1h['close'].shift(1)
    df_1h['tr'] = np.maximum(df_1h['high'] - df_1h['low'], np.maximum(abs(df_1h['high'] - df_1h['prev_close']), abs(df_1h['low'] - df_1h['prev_close'])))
    df_1h['atr'] = df_1h['tr'].rolling(window=LOOKBACK_1H).mean()
    
    # 4H Trend Filter (EMA 200)
    df_4h['ema_200'] = df_4h['close'].ewm(span=LOOKBACK_TREND_4H, adjust=False).mean()
    
    latest = df_1h.iloc[-1]
    trend = df_4h.iloc[-1]
    
    is_uptrend = trend['close'] > trend['ema_200']
    is_downtrend = trend['close'] < trend['ema_200']
    
    is_green = latest['close'] > latest['open']
    is_red = latest['close'] < latest['open']
    
    # 1H logic (Threshold + Candle Confirmation)
    if latest['zscore'] < -ZSCORE_1H and is_uptrend and is_green:
        sl_dist = ATR_MULTIPLIER_SL * latest['atr']
        entry = latest['close']
        return {"symbol": symbol, "tf": "1H IN&OUT", "direction": "🟢 LONG", "entry": entry, 
                "sl": entry - sl_dist, "tp": entry + (RR_RATIO * sl_dist), "reason": f"1H Z: {latest['zscore']:.2f} + Green Candle"}
                
    if latest['zscore'] > ZSCORE_1H and is_downtrend and is_red:
        sl_dist = ATR_MULTIPLIER_SL * latest['atr']
        entry = latest['close']
        return {"symbol": symbol, "tf": "1H IN&OUT", "direction": "🔴 SHORT", "entry": entry, 
                "sl": entry + sl_dist, "tp": entry - (RR_RATIO * sl_dist), "reason": f"1H Z: {latest['zscore']:.2f} + Red Candle"}
                
    return None

# --- MAIN EXECUTION ---
def main():
    print("🤖 Starting Unified Scan (4H Swing + 1H In & Out)...")
    send_telegram("🟢 *Unified Quant Bot Online*\nScanning 26 assets for 4H & 1H setups...")

    signals_found = []
    for pair in ALL_PAIRS:
        df_1h, df_4h, df_1d = get_all_timeframes(pair)
        
        # Check 4H Setup
        sig_4h = check_4h_signal(pair, df_4h, df_1d)
        if sig_4h: signals_found.append(sig_4h)
        
        # Check 1H Setup
        sig_1h = check_1h_signal(pair, df_1h, df_4h)
        if sig_1h: signals_found.append(sig_1h)
        
        time.sleep(0.2) # Be polite to Yahoo

    if not signals_found:
        send_telegram("✅ *Scan Complete*\n\nNo high-probability 4H or 1H setups detected.\nPreserving capital.")
    else:
        send_telegram(f"🚨 *{len(signals_found)} NEW SIGNALS DETECTED* 🚨")
        for sig in signals_found:
            msg = (
                f"⏱️ *Timeframe:* `{sig['tf']}`\n"
                f"📊 *Asset:* `{sig['symbol']}`\n"
                f"🧭 *Direction:* {sig['direction']}\n"
                f"🎯 *Entry:* `{sig['entry']:.5f}`\n"
                f"🛑 *Stop Loss:* `{sig['sl']:.5f}`\n"
                f"💰 *Take Profit:* `{sig['tp']:.5f}`\n\n"
                f"🛡️ *Risk (1%):* `${round(ACCOUNT_BALANCE * RISK_PER_TRADE, 2)}`\n"
                f"🧠 *Reason:* {sig['reason']}"
            )
            send_telegram(msg)

    print(f"✅ Scan complete. Signals found: {len(signals_found)}")

if __name__ == "__main__":
    main()