# api/app.py — FastAPI backend for the 4H Bot Prop Terminal (deploy to Render).
# The brain (brain.py) is the same Gate A engine as main.py; this service arms
# challenges, runs live scans, replays history, and bridges to real prop accounts.
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
import json
import os

import store
import brain
import brokers
import telegram

app = FastAPI(title="4H Bot Prop Terminal", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

UI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "index.html")


@app.get("/")
def ui():
    """Serve the terminal UI at the API root (same-origin /api calls, no proxy needed)."""
    if os.path.exists(UI_PATH):
        from fastapi.responses import FileResponse
        return FileResponse(UI_PATH, media_type="text/html")
    return {"ok": True, "note": "4H Bot Prop Terminal API"}

ENGINE_PRIOR = {"winrate": 58.2, "pf": 1.22, "expected_edge": 0.092, "freq": 4.4}


@app.on_event("startup")
def _startup():
    store.init()


# ---------------- models ----------------

class Rules(BaseModel):
    target_pct: float = 10.0
    daily_pct: float = 2.5
    total_pct: float = 5.0
    min_days: int = 5


class ChallengeIn(BaseModel):
    size: float
    risk_pct: float = 0.25          # from the bot config (main.py: RISK_PER_TRADE = 0.0025)
    mode: str = "live"               # "live" | "replay"
    months: float = 4               # replay window
    rules: Rules = Rules()


class SettingsIn(BaseModel):
    telegram_enabled: Optional[bool] = None


class ConnectIn(BaseModel):
    platform: str
    server: Optional[str] = None
    login: Optional[str] = None
    password: Optional[str] = None
    meta_token: Optional[str] = None
    ct_client_id: Optional[str] = None
    ct_secret: Optional[str] = None
    ct_account_id: Optional[str] = None


# ---------------- helpers ----------------

def _summary(ch):
    if not ch:
        return None
    rules = json.loads(ch.get("rules") or "{}")
    closed = store.closed_trades(ch["id"], 10000)  # full set — stats must not be sliced
    open_ = store.open_trades(ch["id"])
    wins = [t for t in closed if (t["r"] or 0) > 0]
    gw = sum((t["r"] or 0) for t in wins)
    gl = abs(sum((t["r"] or 0) for t in closed if (t["r"] or 0) <= 0))
    size = ch["size"]
    profit = ch["balance"] - size
    target_amt = size * rules.get("target_pct", 10) / 100
    try:
        started = datetime.fromisoformat(ch["started_at"]) if ch.get("started_at") else None
    except Exception:
        started = None
    days = (datetime.now(timezone.utc) - started).days if started else 0
    return {
        "id": ch["id"], "size": size, "risk_pct": ch["risk_pct"], "mode": ch["mode"],
        "rules": rules, "status": ch["status"],
        "reason": ch.get("reason"),
        "equity": (json.loads(ch["equity"]) if ch.get("equity") else None),
        "balance": round(ch["balance"], 2), "peak_balance": round(ch["peak_balance"], 2),
        "profit": round(profit, 2),
        "progress_pct": min(100.0, round(100 * profit / target_amt, 1)) if target_amt > 0 else 0,
        "days": days, "n_trades": len(closed) + len(open_),
        "winrate": round(100 * len(wins) / len(closed), 1) if closed else None,
        "pf": round(gw / gl, 2) if gl > 0 else (99.9 if gw > 0 else None),
        "r_total": round(gw - gl, 2),
        "started_at": ch.get("started_at"), "ended_at": ch.get("ended_at"),
    }


def _display_challenge():
    """The most relevant run: an active challenge first, else the latest run, else the live bot account."""
    ch = store.active_challenge()
    if ch:
        return ch
    latest = store.latest_challenge()
    if latest and latest["id"] != 0:
        return latest
    return store.get_challenge(0)


# ---------------- endpoints ----------------

@app.get("/api/health")
def health():
    return {"ok": True, "server_time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/state")
def state():
    ch = _display_challenge()
    challenges = []
    for c in store.past_challenges(8):
        if c["id"] == 0:
            continue
        s = _summary(c)
        if s:
            challenges.append(s)
    return {
        "online": True,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "engine": ENGINE_PRIOR,
        "settings": {"telegram_enabled": store.get_setting("telegram_enabled", "true") == "true"},
        "challenge": _summary(ch),
        "challenges": challenges,
        "open_trades": store.open_trades(ch["id"]) if ch else [],
        "closed_trades": store.closed_trades(ch["id"], 500) if ch else [],
        "monthly": store.monthly_wr(),
        "sessions": store.session_stats(),
    }


@app.post("/api/challenge")
def arm_challenge(body: ChallengeIn):
    rules = body.rules.dict()
    store.cancel_running()

    if body.mode == "replay":
        run = brain.replay(body.size, rules, body.risk_pct, body.months)
        cid = store.create_challenge(body.size, body.risk_pct, "replay", rules)
        for t in run["closed"]:
            tid = store.add_trade(cid, t)
            store.close_trade(tid, t["closed_at"], t["r"], t["dollars"], t["result"])
        for t in run["open_left"]:
            store.add_trade(cid, t)
        store.build_equity(cid, body.size)
        status = {"passed": "passed", "breached": "breached"}.get(run["status"], "expired")
        store.complete_challenge(cid, status, run["balance"], run.get("peak", run["balance"]), run.get("reason"))
        store.backdate_start(cid, run["days"])
        telegram.notify(
            f"⚡ *Replay complete* — ${body.size:,.0f} @ {body.risk_pct}% risk\n"
            f"Result: *{status.upper()}* ({run['reason']})\n"
            f"{run['n_trades']} trades · WR {run['winrate'] or 0}% · PF {run['pf'] or 0} · "
            f"P&L {run['profit']:+,.0f} in {run['days']}d"
        )
        return {"challenge": _summary(store.get_challenge(cid)), "run": run}

    cid = store.create_challenge(body.size, body.risk_pct, "live", rules)
    telegram.notify(
        f"🎯 *Challenge armed* — ${body.size:,.0f} @ {body.risk_pct}% risk\n"
        f"Target {rules['target_pct']}% · Daily {rules['daily_pct']}% · Total {rules['total_pct']}% · "
        f"Min {rules['min_days']}d\nEngine scans hourly on the 1H close."
    )
    return {"challenge": _summary(store.get_challenge(cid))}


@app.post("/api/challenge/cancel")
def cancel_challenge():
    store.cancel_running()
    return {"ok": True}


@app.post("/api/scan")
def scan():
    """One full engine cycle: book opens -> guardrails -> new Gate A signals -> bridge dispatch."""
    now = datetime.now(timezone.utc)
    result = {"booked": [], "opened": [], "signals": 0, "scan_time": now.isoformat()}

    ch = store.active_challenge()
    if not ch or ch["mode"] != "live":
        signals = brain.scan_signals()
        result["signals"] = len(signals)
        result["candidates"] = signals[:8]
        result["note"] = "No live challenge armed — signals listed only."
        return result

    rules = json.loads(ch["rules"] or "{}")
    size = ch["size"]
    balance = ch["balance"]

    # 1) book open trades on their SL/TP touches
    still, closed = brain.book_open_trades(store.open_trades(ch["id"]))
    for t in closed:
        store.close_trade(t["id"], t["closed_at"], t["r"], t["dollars"], t["result"])
        balance += t["dollars"]
        result["booked"].append({k: t[k] for k in ("symbol", "dir", "r", "dollars", "result", "closed_at")})
        telegram.notify(
            f"✅ CLOSED {t['dir']} {t['symbol']} — {t['result']} {t['r']:+.3f}R (${t['dollars']:+,.0f})"
        )
    store.update_balance(ch["id"], balance)
    store.build_equity(ch["id"], size)

    # 2) guardrails from the typed-in rules
    today = now.date().isoformat()
    day_pnl = sum(t["dollars"] for t in store.closed_trades(ch["id"]) if str(t["closed_at"])[:10] == today)
    peak = max(ch["peak_balance"], balance)
    breached = None
    if rules.get("daily_pct") and -day_pnl >= size * rules["daily_pct"] / 100:
        breached = "daily loss limit"
    elif rules.get("total_pct") and (peak - balance) >= size * rules["total_pct"] / 100:
        breached = "total drawdown limit"
    if breached:
        store.complete_challenge(ch["id"], "breached", balance, None, f"{breached} — balance ${balance:,.0f}")
        telegram.notify(f"⛔ *CHALLENGE BREACHED* — {breached}. Balance ${balance:,.0f}. Engine paused for this run.")
        result["challenge_status"] = "breached"
        return result

    # 3) new Gate A signals (dedupe by symbol+hour, same as main.py)
    signals = brain.scan_signals()
    result["signals"] = len(signals)
    known = store.trade_hours(ch["id"])
    hour_key = now.replace(minute=0, second=0, microsecond=0).isoformat()[:13]
    for s in signals:
        if (s["symbol"], hour_key) in known:
            continue
        if len(store.open_trades(ch["id"])) >= brain.MAX_OPEN:
            break
        s["risk_dollars"] = round(balance * ch["risk_pct"] / 100, 2)
        tid = store.add_trade(ch["id"], s)
        s["id"] = tid
        result["opened"].append({k: s[k] for k in ("symbol", "dir", "entry", "sl", "tp", "r_target", "risk_dollars")})
        telegram.notify(
            f"🟢 NEW {s['dir']} {s['symbol']} @ {s['entry']:.5g}\n"
            f"SL {s['sl']:.5g} · TP {s['tp']:.5g} · +{s['r_target']:.2f}R tgt · risk ${s['risk_dollars']}"
        )
        # 4) mirror to a real prop bridge if one is connected
        for plat in ("MetaTrader 5", "MetaTrader 4", "cTrader"):
            conn = store.get_connection(plat)
            if conn and conn["status"] in ("connected", "queued", "dry_run"):
                try:
                    dispatch = brokers.dispatch_signal(conn, s, s["risk_dollars"])
                    result.setdefault("bridge", []).append({"platform": plat, **dispatch})
                except Exception as e:
                    result.setdefault("bridge_errors", []).append({"platform": plat, "error": str(e)})
                break

    # 5) pass check
    profit = balance - size
    days = (now - datetime.fromisoformat(ch["started_at"])).days if ch.get("started_at") else 0
    if profit >= size * rules.get("target_pct", 10) / 100 and days >= int(rules.get("min_days", 5)):
        store.complete_challenge(ch["id"], "passed", balance, None, f"target reached in {days}d")
        telegram.notify(f"🎉 *CHALLENGE PASSED* — target reached in {days}d. Balance ${balance:,.0f}.")
        result["challenge_status"] = "passed"

    result["challenge"] = _summary(store.get_challenge(ch["id"]))
    return result


@app.post("/api/settings")
def settings(body: SettingsIn):
    if body.telegram_enabled is not None:
        store.set_setting("telegram_enabled", "true" if body.telegram_enabled else "false")
        telegram.notify("🔔 Telegram notifications enabled from the Prop Terminal.") if body.telegram_enabled else None
    return {"telegram_enabled": store.get_setting("telegram_enabled", "true") == "true"}


@app.post("/api/connect")
def connect(body: ConnectIn):
    try:
        if body.platform.startswith("MetaTrader"):
            if not body.meta_token:
                raise RuntimeError("MetaAPI token is required for MetaTrader bridges.")
            res = brokers.meta_connect(body.meta_token, body.login, body.password, body.server)
            cfg = {"meta_token": body.meta_token, "login": body.login, "server": body.server}
            status = res.get("status", "dry_run")
            store.save_connection(body.platform, cfg, status)
            msg = res.get("message") or f"Bridge verified for {body.platform}."
            return {"status": status, "message": msg}

        if body.platform == "cTrader":
            if not (body.ct_client_id and body.ct_secret):
                raise RuntimeError("cTrader OpenAPI client ID and secret are required.")
            res = brokers.ct_validate(body.ct_client_id, body.ct_secret, body.ct_account_id)
            cfg = {"client_id": body.ct_client_id, "secret": body.ct_secret, "account_id": body.ct_account_id}
            store.save_connection("cTrader", cfg, res.get("status", "dry_run"))
            acct = f" — account {body.ct_account_id}" if body.ct_account_id else ""
            return {"status": res.get("status", "dry_run"),
                    "message": f"cTrader OpenAPI credentials verified{acct}."}

        store.save_connection(body.platform, {"server": body.server, "login": body.login}, "pending")
        return {"status": "pending",
                "message": f"{body.platform} credentials stored — connector ships next."}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/trades")
def trades(challenge_id: Optional[int] = None):
    cid = challenge_id if challenge_id is not None else (_display_challenge() or {}).get("id", 0)
    return {"open": store.open_trades(cid), "closed": store.closed_trades(cid)}
