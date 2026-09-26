# api/brain.py — the 4H Bot engine (Path B + Gate A), ported 1:1 from main.py,
# plus a replay simulator that fast-forwards a prop challenge through history.
import pandas as pd
import numpy as np
import yfinance as yf
import time
from datetime import timedelta

# --- LAB2 + GATE A PARAMETERS (identical to main.py) ---
ZSCORE_ENTRY = 1.5
LOOKBACK_1H = 50
ATR_MULTIPLIER_SL = 2.5
TREND_EMA_4H = 200
Z_MILD_MAX = 2.0        # only take stretches between 1.5 and 2.0
VOL_REGIME_MAX = 1.2    # skip HIGH volatility (ATR50 / ATR200 > 1.2)
MAX_OPEN = 5            # terminal-side prop protection (challenge only)

ALL_PAIRS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "USDCAD=X", "AUDUSD=X", "NZDUSD=X",
    "GC=F", "SI=F", "EURGBP=X", "EURJPY=X", "GBPJPY=X", "AUDJPY=X", "CHFJPY=X", "EURAUD=X", "GBPAUD=X",
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "ADA-USD",
    "AVAX-USD", "LINK-USD", "DOT-USD", "LTC-USD", "NEAR-USD",
]

_cache = {}  # (symbol, period) -> (df, fetched_at)


def _clean(raw):
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.droplevel(1)
    raw.columns = [str(c).lower() for c in raw.columns]
    return raw


def get_history(symbol, period="120d", use_cache=True):
    key = (symbol, period)
    now = time.time()
    if use_cache and key in _cache and now - _cache[key][1] < 1800:
        return _cache[key][0]
    try:
        raw = yf.download(symbol, period=period, interval="1h", progress=False)
        if raw is None or raw.empty:
            return None
        raw = _clean(raw)
        if use_cache:
            _cache[key] = (raw, now)
        return raw
    except Exception as e:
        print(f"[yf] {symbol}: {e}")
        return None


def build_features(raw):
    """Precompute 1h indicators; the 4H EMA200 trend is aligned to the last fully CLOSED 4h bar (no lookahead)."""
    df = raw.copy()
    c = df["close"]
    df["sma"] = c.rolling(LOOKBACK_1H).mean()
    std = c.rolling(LOOKBACK_1H).std()
    df["z"] = (c - df["sma"]) / std.replace(0, np.nan)
    pc = c.shift(1)
    tr = np.maximum(df["high"] - df["low"],
                    np.maximum((df["high"] - pc).abs(), (df["low"] - pc).abs()))
    df["atr"] = tr.rolling(LOOKBACK_1H).mean()
    df["atr_long"] = tr.rolling(200).mean()
    df4 = df.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    ema4 = df4["close"].ewm(span=TREND_EMA_4H, adjust=False).mean()
    trend4 = pd.Series(np.where(df4["close"] > ema4, 1, -1), index=df4.index)
    # shift(1) -> only closed 4h bars drive the trend flag (mirrors main.py's +4h filter)
    df["trend"] = trend4.shift(1).reindex(df.index, method="ffill")
    return df


def _signal_from_row(symbol, row, z_prev):
    """Gate A entry logic for one precomputed bar — identical gates to main.py's evaluate()."""
    z = row["z"]
    if pd.isna(z) or pd.isna(z_prev) or pd.isna(row["atr"]) or pd.isna(row["atr_long"]):
        return None
    a, al = row["atr"], row["atr_long"]
    if a <= 0 or al <= 0:
        return None
    if a / al > VOL_REGIME_MAX:          # Gate A, part 1: vol regime must not be HIGH
        return None
    if abs(z) >= Z_MILD_MAX:             # Gate A, part 2: MILD stretch only
        return None
    entry, tp = row["close"], row["sma"]
    risk_dist = ATR_MULTIPLIER_SL * a
    if pd.isna(tp) or pd.isna(entry):
        return None
    trend = row["trend"]
    if z_prev >= -ZSCORE_ENTRY and z < -ZSCORE_ENTRY and trend == 1:
        if tp <= entry:
            return None
        return {"symbol": symbol, "dir": "LONG", "entry": float(entry),
                "sl": float(entry - risk_dist), "tp": float(tp),
                "z": float(z), "r_target": float((tp - entry) / risk_dist)}
    if z_prev <= ZSCORE_ENTRY and z > ZSCORE_ENTRY and trend == -1:
        if tp >= entry:
            return None
        return {"symbol": symbol, "dir": "SHORT", "entry": float(entry),
                "sl": float(entry + risk_dist), "tp": float(tp),
                "z": float(z), "r_target": float((entry - tp) / risk_dist)}
    return None


# ---------------- LIVE SCAN (same behaviour as main.py's loop) ----------------

def scan_signals(pairs=None):
    """Scan every pair on the last fully CLOSED 1h bar and return fresh Gate A signals."""
    now = pd.Timestamp.now(tz="UTC")
    out = []
    for sym in (pairs or ALL_PAIRS):
        raw = get_history(sym)
        if raw is None or len(raw) < 210:
            continue
        raw = raw[raw.index + pd.Timedelta(hours=1) <= now]  # closed candles only — no lookahead
        f = build_features(raw)
        if len(f) < 3:
            continue
        row, prev = f.iloc[-1], f.iloc[-2]
        sig = _signal_from_row(sym, row, prev["z"])
        if sig:
            sig["opened_at"] = now.isoformat()
            out.append(sig)
        time.sleep(0.15)
    return out


def book_open_trades(open_list):
    """Close any open sim trades whose SL/TP was touched (SL counted first — conservative, same as main.py)."""
    still, closed = [], []
    by_sym = {}
    for t in open_list:
        by_sym.setdefault(t["symbol"], []).append(t)
    for sym, trades in by_sym.items():
        raw = get_history(sym, "14d", use_cache=False)
        if raw is None:
            still += trades
            continue
        for t in trades:
            opened = pd.Timestamp(t["opened_at"])
            if opened.tz is None:
                opened = opened.tz_localize("UTC")
            start = opened.floor("h") + timedelta(hours=1)
            booked = False
            for ts, row in raw[raw.index >= start].iterrows():
                if t["dir"] == "LONG":
                    hit_sl, hit_tp = row["low"] <= t["sl"], row["high"] >= t["tp"]
                else:
                    hit_sl, hit_tp = row["high"] >= t["sl"], row["low"] <= t["tp"]
                if hit_sl or hit_tp:
                    r = -1.0 if hit_sl else t["r_target"]
                    closed.append({**t, "r": round(float(r), 3),
                                   "dollars": round(float(r) * t["risk_dollars"], 2),
                                   "result": "SL" if hit_sl else "TP", "closed_at": str(ts)})
                    booked = True
                    break
            if not booked:
                still.append(t)
    return still, closed


# ---------------- REPLAY — fast-forward a challenge through history ----------------

def replay(size, rules, risk_pct, months=4, pairs=None):
    """Run the exact Gate A engine over history through the full prop career:
    Phase 1 -> Phase 2 -> Funded. rules = {target_pct, target2_pct, funded_pct,
    daily_pct, total_pct, min_days, min_days2}. Each phase resets the account
    to its starting size when passed, like a real evaluation."""
    days = min(int(months * 30), 720)
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    data = {}
    for s in (pairs or ALL_PAIRS):
        raw = get_history(s, f"{days}d")
        if raw is None or len(raw) < 260:
            continue
        f = build_features(raw)
        f = f[f.index >= cutoff]  # yfinance can return MORE than requested (165d for "120d") — honor the window exactly
        f = f.dropna(subset=["z", "sma", "atr", "atr_long", "trend"])
        f["z_prev"] = f["z"].shift(1)
        # to_dict('index') is ~20x faster than iterrows — the walk below then runs
        # on plain dicts (CPU is the bottleneck on Render's free 0.1-CPU tier).
        recs = {ts: r for ts, r in f.to_dict("index").items() if not pd.isna(r["z_prev"])}
        if recs:
            data[s] = recs
        time.sleep(0.1)

    idx = sorted(set().union(*[set(d.keys()) for d in data.values()])) if data else []
    daily_pct = float(rules.get("daily_pct", 2.5))
    total_pct = float(rules.get("total_pct", 5.0))
    # The full prop career, in order: pass Phase 1, the account resets for Phase 2;
    # pass Phase 2, the funded account trades toward the typed funded target.
    stages = [
        {"name": "Phase 1", "target": float(rules.get("target_pct", 10)), "min_days": int(rules.get("min_days", 5))},
        {"name": "Phase 2", "target": float(rules.get("target2_pct", 5)), "min_days": int(rules.get("min_days2", 5))},
        {"name": "Funded", "target": float(rules.get("funded_pct", 10)), "min_days": 0},
    ]

    open_trades, closed = [], []
    balance = peak = float(size)
    stage_i = 0
    stage_start = idx[0] if idx else None
    day_pnl, cur_day = 0.0, None
    equity = []
    status, reason = "expired", "replay window ended"
    start = idx[0] if idx else None
    elapsed = 0

    for ts in idx:
        d = ts.date()
        if d != cur_day:
            cur_day, day_pnl = d, 0.0
        elapsed = (ts - start).days

        # 1) book open trades on this bar (SL first)
        still = []
        for t in open_trades:
            rec = data.get(t["symbol"], {}).get(ts)
            if rec is None:
                still.append(t)
                continue
            if t["dir"] == "LONG":
                hit_sl, hit_tp = rec["low"] <= t["sl"], rec["high"] >= t["tp"]
            else:
                hit_sl, hit_tp = rec["high"] >= t["sl"], rec["low"] <= t["tp"]
            if hit_sl or hit_tp:
                r = -1.0 if hit_sl else t["r_target"]
                trade = {**t, "stage": stages[stage_i]["name"], "r": round(float(r), 3),
                        "dollars": round(float(r) * t["risk_dollars"], 2),
                        "result": "SL" if hit_sl else "TP", "closed_at": str(ts)}
                closed.append(trade)
                balance += trade["dollars"]
                day_pnl += trade["dollars"]
            else:
                still.append(t)
        open_trades = still

        # 2) guardrails — typed max daily / max total drawdown, per phase
        peak = max(peak, balance)
        if daily_pct and -day_pnl >= size * daily_pct / 100:
            status, reason = "breached", f"{stages[stage_i]['name']} — max daily drawdown"
            break
        if total_pct and (peak - balance) >= size * total_pct / 100:
            status, reason = "breached", f"{stages[stage_i]['name']} — max total drawdown"
            break

        # 3) new Gate A signals this hour (one open per symbol, MAX_OPEN cap)
        if len(open_trades) < MAX_OPEN:
            busy = {t["symbol"] for t in open_trades}
            for sym, recs in data.items():
                if sym in busy:
                    continue
                rec = recs.get(ts)
                if rec is None:
                    continue
                sig = _signal_from_row(sym, rec, rec["z_prev"])
                if not sig:
                    continue
                open_trades.append({**sig, "risk_dollars": round(balance * risk_pct / 100, 2),
                                    "opened_at": str(ts)})

        # 4) phase pass check — typed target and min days for the current phase
        st = stages[stage_i]
        stage_days = (ts - stage_start).days if stage_start is not None else 0
        if (balance - size) >= size * st["target"] / 100 and stage_days >= st["min_days"]:
            # open positions close at the phase boundary (0R) — the account resets
            for t in open_trades:
                closed.append({**t, "stage": st["name"], "r": 0.0, "dollars": 0.0,
                               "result": "PHASE-END", "closed_at": str(ts)})
            open_trades = []
            if stage_i == len(stages) - 1:
                status, reason = "funded", f"{st['name']} target reached (+{100 * (balance - size) / size:.1f}%)"
                break
            stage_i += 1
            balance = peak = float(size)
            stage_start = ts
            day_pnl = 0.0
            equity.append([str(ts)[:16], round(balance, 2)])
            continue

        equity.append([str(ts)[:16], round(balance, 2)])

    if start and status == "expired":
        elapsed = (idx[-1] - start).days
        reason = f"window ended in {stages[stage_i]['name']}"

    wins = [t for t in closed if t["r"] > 0]
    gw = sum(t["r"] for t in wins)
    gl = abs(sum(t["r"] for t in closed if t["r"] <= 0))

    # per-phase aggregates from the trade ledger
    stage_rows = []
    for si, st in enumerate(stages):
        tr = [t for t in closed if t.get("stage") == st["name"]]
        sw = [t for t in tr if t["r"] > 0]
        tw = sum(t["r"] for t in sw)
        tl = abs(sum(t["r"] for t in tr if t["r"] <= 0))
        st_status = "passed" if (si < stage_i or (status == "funded" and si == stage_i)) else ("active" if si == stage_i else "pending")
        stage_rows.append({
            "name": st["name"], "target_pct": st["target"], "status": st_status,
            "n_trades": len(tr),
            "winrate": round(100 * len(sw) / len(tr), 1) if tr else None,
            "pf": round(tw / tl, 2) if tl > 0 else (99.9 if tw > 0 else None),
            "r_total": round(tw - tl, 2),
        })

    return {
        "status": status, "reason": reason,
        "stage": stages[stage_i]["name"], "stages": stage_rows,
        "size": size, "risk_pct": risk_pct, "rules": rules,
        "balance": round(balance, 2), "peak": round(peak, 2),
        "profit": round(balance - size, 2),
        "profit_pct": round(100 * (balance - size) / size, 2),
        "days": elapsed, "n_trades": len(closed) + len(open_trades),
        "winrate": round(100 * len(wins) / len(closed), 1) if closed else None,
        "pf": round(gw / gl, 2) if gl > 0 else (99.9 if gw > 0 else None),
        "r_total": round(gw - gl, 2),
        "closed": closed, "open_left": open_trades,
        "equity": equity[::max(1, len(equity) // 240)],
    }
