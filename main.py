# main.py
# =============================================================================
# SERVERLESS QUANT BOT - PATH B + GATE A (LAB4-VALIDATED)
# Config: Z=1.5 | SL=2.5xATR(50) | TP=50-SMA static limit | 4H EMA200 trend
# GATE A: only |Z| 1.5-2.0 (MILD) AND vol regime != HIGH (ATR50/ATR200 <= 1.2)
# Validated: 58.2% WR | PF 1.22 | MDD -13.0R | ~4.4 trades/day
# Memory: trade_log.json (committed back to repo by the workflow each run)
# =============================================================================

import requests
import pandas as pd
import numpy as np
import yfinance as yf
import time
import os
import json
from datetime import timedelta

# --- CONFIGURATION (HARDCODED BY OWNER'S CHOICE - REPO IS PRIVATE) ---
TELEGRAM_BOT_TOKEN = "8712031624:AAH8GKakWgeuFaR8VvKeox2TbusGdZwE_xE"
TELEGRAM_CHAT_ID = "5858961660"

# Prop firm accounts: 200000.0 / 0.0025 (0.25% risk)
# Personal $1,000 account: change to 1000.0 / 0.005 (0.5% risk)
ACCOUNT_BALANCE = 200000.0
RISK_PER_TRADE = 0.0025

# --- LAB2 PARAMETERS ---
ZSCORE_ENTRY = 1.5
LOOKBACK_1H = 50
ATR_MULTIPLIER_SL = 2.5
TREND_EMA_4H = 200

# --- GATE A PARAMETERS (LAB4-VALIDATED) ---
Z_MILD_MAX = 2.0        # only take stretches between 1.5 and 2.0
VOL_REGIME_MAX = 1.2    # skip HIGH volatility (ATR50 / ATR200 > 1.2)

LOG_FILE = "trade_log.json"
MIN_ASSET_SAMPLE = 8

ALL_PAIRS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "USDCAD=X", "AUDUSD=X", "NZDUSD=X",
    "GC=F", "SI=F", "EURGBP=X", "EURJPY=X", "GBPJPY=X", "AUDJPY=X", "CHFJPY=X", "EURAUD=X", "GBPAUD=X",
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "ADA-USD",
    "AVAX-USD", "LINK-USD", "DOT-USD", "LTC-USD", "NEAR-USD"
]

# --- MEMORY ---
def load_log():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE) as f: return json.load(f)
        except Exception: pass
    return {"open": [], "closed": [], "peak_r": 0.0, "guard_flag": False}

def save_log(log):
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=1, default=str)

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
        raw = raw[raw.index + pd.Timedelta(hours=1) <= now]
        if len(raw) < LOOKBACK_1H + 2: return None, None
        df4 = raw.resample('4h').agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()
        df4 = df4[df4.index + pd.Timedelta(hours=4) <= now]
        if len(df4) < TREND_EMA_4H: return None, None
        return raw, df4
    except Exception as e:
        print(f"[YF] {symbol}: {e}")
        return None, None

# --- SIGNAL ENGINE (Lab2 config + Gate A) ---
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
    atr_long = tr.rolling(200).mean()          # for volatility regime (Gate A)
    ema4 = df4['close'].ewm(span=TREND_EMA_4H, adjust=False).mean()
    uptrend = df4['close'].iloc[-1] > ema4.iloc[-1]
    downtrend = df4['close'].iloc[-1] < ema4.iloc[-1]
    zl, zp = z.iloc[-1], z.iloc[-2]
    a = atr.iloc[-1]
    al = atr_long.iloc[-1]
    if pd.isna(zl) or pd.isna(zp) or pd.isna(a) or a <= 0: return None

    # --- GATE A, PART 1: volatility regime must not be HIGH ---
    if pd.isna(al) or al <= 0 or (a / al) > VOL_REGIME_MAX: return None
    # --- GATE A, PART 2: only MILD stretches (|Z| between 1.5 and 2.0) ---
    if abs(zl) >= Z_MILD_MAX: return None

    entry = c.iloc[-1]
    risk_dist = ATR_MULTIPLIER_SL * a
    tp_price = sma.iloc[-1]
    if pd.isna(tp_price): return None
    if zp >= -ZSCORE_ENTRY and zl < -ZSCORE_ENTRY and uptrend:
        if tp_price <= entry: return None
        return {"symbol": symbol, "direction": "🟢 LONG", "dir": "LONG", "entry": entry,
                "sl": entry - risk_dist, "tp": tp_price, "z": zl,
                "r_target": (tp_price - entry) / risk_dist}
    if zp <= ZSCORE_ENTRY and zl > ZSCORE_ENTRY and downtrend:
        if tp_price >= entry: return None
        return {"symbol": symbol, "direction": "🔴 SHORT", "dir": "SHORT", "entry": entry,
                "sl": entry + risk_dist, "tp": tp_price, "z": zl,
                "r_target": (entry - tp_price) / risk_dist}
    return None

# --- AUTO BOOKING: check open trades for SL/TP touches ---
def book_open_trades(log):
    if not log["open"]: return
    by_sym = {}
    for t in log["open"]: by_sym.setdefault(t["symbol"], []).append(t)
    still = []
    for sym, trades in by_sym.items():
        try:
            raw = yf.download(sym, period="14d", interval="1h", progress=False)
            if raw.empty: still += trades; continue
            if isinstance(raw.columns, pd.MultiIndex): raw.columns = raw.columns.droplevel(1)
            raw.columns = [c.lower() for c in raw.columns]
        except Exception:
            still += trades; continue
        for t in trades:
            opened = pd.Timestamp(t["opened_at"])
            if opened.tz is None: opened = opened.tz_localize('UTC')
            start = opened.floor('h') + timedelta(hours=1)
            booked = False
            for ts, row in raw[raw.index >= start].iterrows():
                if t["dir"] == "LONG":
                    hit_sl = row["low"] <= t["sl"]; hit_tp = row["high"] >= t["tp"]
                else:
                    hit_sl = row["high"] >= t["sl"]; hit_tp = row["low"] <= t["tp"]
                if hit_sl or hit_tp:   # conservative: SL counted first
                    r = -1.0 if hit_sl else t["r_target"]
                    log["closed"].append({
                        "symbol": t["symbol"], "dir": t["dir"], "entry": t["entry"],
                        "sl": t["sl"], "tp": t["tp"], "r": round(r, 3),
                        "result": "SL" if hit_sl else "TP",
                        "opened_at": t["opened_at"], "closed_at": str(ts),
                        "dollars": round(r * t["risk_dollars"], 2)})
                    booked = True
                    break
            if not booked: still.append(t)
    log["open"] = still

# --- STATS WINDOWS ---
def window_stats(log, days):
    cut = pd.Timestamp.now(tz='UTC') - timedelta(days=days)
    closed = []
    for t in log["closed"]:
        ct = pd.Timestamp(t["closed_at"])
        if ct.tz is None: ct = ct.tz_localize('UTC')
        if ct >= cut: closed.append(t)
    if not closed: return None
    wins = [t for t in closed if t["r"] > 0]
    losses = [t for t in closed if t["r"] <= 0]
    gw = sum(t["r"] for t in wins); gl = abs(sum(t["r"] for t in losses))
    per = {}
    for t in closed: per.setdefault(t["symbol"], []).append(t)
    rows = [(s, len(ts), sum(x["r"] for x in ts))
            for s, ts in per.items() if len(ts) >= MIN_ASSET_SAMPLE]
    rows.sort(key=lambda x: -x[2])
    return {"n": len(closed), "w": len(wins), "l": len(losses),
            "wr": 100*len(wins)/len(closed),
            "pf": min((gw/gl) if gl > 0 else 99.9, 99.9),
            "r": sum(t["r"] for t in closed),
            "usd": sum(t["dollars"] for t in closed),
            "best": rows[0] if rows else None,
            "worst": rows[-1] if rows else None}

def rolling40_wr(log):
    last = sorted(log["closed"], key=lambda t: t["closed_at"])[-40:]
    if len(last) < 20: return None
    return 100*sum(1 for t in last if t["r"] > 0)/len(last)

# --- MAIN ---
def main():
    log = load_log()
    now = pd.Timestamp.now(tz='UTC')
    print("🤖 Path B + Gate A scan...")

    book_open_trades(log)

    scanned = []
    for pair in ALL_PAIRS:
        df1, df4 = get_data(pair)
        sig = evaluate(pair, df1, df4)
        if sig: scanned.append(sig)
        time.sleep(0.2)

    risk_dollars = round(ACCOUNT_BALANCE * RISK_PER_TRADE, 2)
    hour_key = now.floor('h')
    existing = {(t["symbol"], pd.Timestamp(t["opened_at"]).floor('h'))
                for t in log["open"] + log["closed"]}
    added = []
    for s in scanned:
        if (s["symbol"], hour_key) in existing: continue
        msg = (
            f"⏱️ *Model:* 1H IN & OUT (Gate A config)\n"
            f"📊 *Asset:* `{s['symbol']}`\n"
            f"🧭 *Direction:* {s['direction']}\n"
            f"🎯 *Entry:* ~`{fmt(s['entry'], s['symbol'])}` (market)\n"
            f"🛑 *Stop Loss:* `{fmt(s['sl'], s['symbol'])}` (2.5×ATR)\n"
            f"💰 *Take Profit:* `{fmt(s['tp'], s['symbol'])}` (50-SMA limit)\n"
            f"📐 *Target:* +{s['r_target']:.2f}R\n\n"
            f"🛡️ *Risk:* `${risk_dollars}`\n"
            f"🧠 *Reason:* Z crossed to {s['z']:.2f} (MILD) | vol OK | 4H trend aligned\n"
            f"⚠️ Set SL+TP as bracket immediately. Never widen the stop."
        )
        send_telegram(msg)
        log["open"].append({"symbol": s["symbol"], "dir": s["dir"], "entry": s["entry"],
                            "sl": s["sl"], "tp": s["tp"], "r_target": round(s["r_target"], 3),
                            "opened_at": str(now), "risk_dollars": risk_dollars})
        added.append(s)

    cum_r = sum(t["r"] for t in log["closed"])
    log["peak_r"] = max(log.get("peak_r", 0.0), cum_r)
    dip_pct = (log["peak_r"] - cum_r) * RISK_PER_TRADE * 100
    if dip_pct >= 18 and not log.get("guard_flag"):
        send_telegram(f"⚠️ *GUARDRAIL BREACH*\nDrawdown from peak: -{dip_pct:.1f}% (limit 18%)\nPause new entries and review per operating rules.")
        log["guard_flag"] = True
    if dip_pct < 12: log["guard_flag"] = False

    day = window_stats(log, 1)
    day_line = (f"Last 24h: {day['r']:+.2f}R (${day['usd']:+,.0f}) | {day['w']}W/{day['l']}L"
                if day else "Last 24h: flat | 0 closed")
    send_telegram(
        f"🕐 *{now:%H:%M} UTC SCAN*\n"
        f"Assets: {len(ALL_PAIRS)} | New setups: {len(added)}\n"
        f"Open trades: {len(log['open'])} | Closed (24h): {day['n'] if day else 0}\n"
        f"{day_line}\n"
        f"Cumulative: {cum_r:+.1f}R | Peak dip: -{dip_pct:.1f}%\n"
        f"Next scan: {(now + timedelta(hours=1)):%H:%M} UTC")

    if now.hour == 20:
        lines = [f"📊 *DAILY RECAP (24h) — {now:%Y-%m-%d}*"]
        if day:
            lines += [f"Closed: {day['n']} | {day['w']}W / {day['l']}L | WR {day['wr']:.0f}%",
                      f"P&L: {day['r']:+.2f}R (${day['usd']:+,.0f}) | PF {day['pf']:.2f}",
                      f"Open: {len(log['open'])} | Cumulative: {cum_r:+.1f}R"]
        else:
            lines.append("No closed trades in the last 24h.")
        r40 = rolling40_wr(log)
        if r40 is not None:
            lines.append(f"Rolling 40-trade WR: {r40:.0f}%" +
                         (" ⚠️ below 45% — review" if r40 < 45 else " — OK"))
        send_telegram("\n".join(lines))

        if now.weekday() == 6:
            w = window_stats(log, 7)
            lines = ["🗓️ *WEEKLY RECAP (7 days)*"]
            if w:
                lines += [f"Closed: {w['n']} | WR {w['wr']:.1f}% | PF {w['pf']:.2f}",
                          f"P&L: {w['r']:+.2f}R (${w['usd']:+,.0f})"]
                if w["best"]: lines.append(f"Best (≥{MIN_ASSET_SAMPLE} trades): {w['best'][0]} {w['best'][2]:+.1f}R")
                if w["worst"] and w["worst"][2] < 0:
                    lines.append(f"Worst (≥{MIN_ASSET_SAMPLE} trades): {w['worst'][0]} {w['worst'][2]:+.1f}R")
                lines.append(f"(Assets with <{MIN_ASSET_SAMPLE} trades hidden — small samples lie.)")
            else:
                lines.append("No closed trades this week.")
            send_telegram("\n".join(lines))

    save_log(log)
    print(f"✅ Done. Open: {len(log['open'])} | Closed: {len(log['closed'])}")

if __name__ == "__main__":
    main()