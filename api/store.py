# api/store.py — persistence for the Prop Terminal.
# SQLite by default (local + zero-config). Set DATABASE_URL (e.g. Neon Postgres)
# on the host and every table moves to Postgres automatically — data survives
# redeploys. Same function signatures either way.
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

PG_URL = os.environ.get("DATABASE_URL", "").strip()
PG = bool(PG_URL)

DB_PATH = os.environ.get(
    "TERMINAL_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "terminal.db"),
)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Render deploys with Root Directory = api/, where the repo root is not on disk.
# Try, in order: env override -> api/trade_log.json -> repo root trade_log.json.
_LOG_CANDIDATES = [
    os.environ.get("TRADE_LOG"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_log.json"),
    os.path.join(_REPO_ROOT, "trade_log.json"),
]
LOG_PATH = next((p for p in _LOG_CANDIDATES if p and os.path.exists(p)), _LOG_CANDIDATES[-1])

_lock = threading.Lock()

if PG:
    import psycopg
    from psycopg.rows import dict_row

SCHEMA_SQLITE = [
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS challenge (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT, size REAL, risk_pct REAL, mode TEXT, rules TEXT,
        status TEXT, balance REAL, peak_balance REAL, started_at TEXT, ended_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        challenge_id INTEGER, symbol TEXT, dir TEXT,
        entry REAL, sl REAL, tp REAL, r_target REAL, risk_dollars REAL,
        opened_at TEXT, closed_at TEXT, r REAL, dollars REAL, result TEXT)""",
    """CREATE TABLE IF NOT EXISTS connections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT, config TEXT, status TEXT, created_at TEXT)""",
]

SCHEMA_PG = [
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS challenge (
        id BIGSERIAL PRIMARY KEY,
        created_at TEXT, size DOUBLE PRECISION, risk_pct DOUBLE PRECISION, mode TEXT, rules TEXT,
        status TEXT, balance DOUBLE PRECISION, peak_balance DOUBLE PRECISION, started_at TEXT, ended_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS trades (
        id BIGSERIAL PRIMARY KEY,
        challenge_id BIGINT, symbol TEXT, dir TEXT,
        entry DOUBLE PRECISION, sl DOUBLE PRECISION, tp DOUBLE PRECISION, r_target DOUBLE PRECISION, risk_dollars DOUBLE PRECISION,
        opened_at TEXT, closed_at TEXT, r DOUBLE PRECISION, dollars DOUBLE PRECISION, result TEXT)""",
    """CREATE TABLE IF NOT EXISTS connections (
        id BIGSERIAL PRIMARY KEY,
        platform TEXT, config TEXT, status TEXT, created_at TEXT)""",
]


@contextmanager
def db():
    """One connection per operation: commit on success, rollback on error, always close."""
    if PG:
        conn = psycopg.connect(PG_URL, row_factory=dict_row)
    else:
        import sqlite3
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
    try:
        with conn:  # transaction scope: commit / rollback
            yield conn
    finally:
        conn.close()


def q(sql):
    """Queries are written with %s placeholders; translated to ? for SQLite."""
    return sql if PG else sql.replace("%s", "?")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row(r):
    return dict(r) if r is not None else None


def init():
    with _lock, db() as c:
        for stmt in (SCHEMA_PG if PG else SCHEMA_SQLITE):
            c.execute(stmt)
        c.execute(q("INSERT INTO settings(key,value) VALUES ('telegram_enabled','true') ON CONFLICT (key) DO NOTHING"))
        c.execute(q("INSERT INTO settings(key,value) VALUES ('default_risk_pct','0.25') ON CONFLICT (key) DO NOTHING"))
        c.execute(q("INSERT INTO settings(key,value) VALUES ('default_mode','replay') ON CONFLICT (key) DO NOTHING"))
    seed_from_log()


# ---------------- settings ----------------

def get_setting(key, default=None):
    with db() as c:
        row = c.execute(q("SELECT value FROM settings WHERE key=%s"), (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with _lock, db() as c:
        c.execute(
            q("INSERT INTO settings(key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=excluded.value"),
            (key, str(value)),
        )


# ---------------- challenges ----------------

def _rules_json(rules):
    return rules if isinstance(rules, str) else json.dumps(rules)


def create_challenge(size, risk_pct, mode, rules):
    now = _now()
    with _lock, db() as c:
        cur = c.execute(
            q("INSERT INTO challenge(created_at,size,risk_pct,mode,rules,status,balance,peak_balance,started_at) "
              "VALUES (%s,%s,%s,%s,%s,'running',%s,%s,%s) RETURNING id"),
            (now, size, risk_pct, mode, _rules_json(rules), size, size, now),
        )
        return cur.fetchone()["id"]


def cancel_running():
    with _lock, db() as c:
        c.execute(q("UPDATE challenge SET status='cancelled', ended_at=%s WHERE status='running' AND id<>0"), (_now(),))


def get_challenge(cid):
    with db() as c:
        return _row(c.execute(q("SELECT * FROM challenge WHERE id=%s"), (cid,)).fetchone())


def active_challenge():
    with db() as c:
        return _row(c.execute(
            q("SELECT * FROM challenge WHERE status='running' AND id<>0 ORDER BY id DESC LIMIT 1")).fetchone())


def latest_challenge():
    with db() as c:
        return _row(c.execute(q("SELECT * FROM challenge ORDER BY id DESC LIMIT 1")).fetchone())


def complete_challenge(cid, status, balance, peak_balance=None):
    with _lock, db() as c:
        c.execute(
            q("UPDATE challenge SET status=%s, balance=%s, peak_balance=COALESCE(%s, peak_balance), ended_at=%s WHERE id=%s"),
            (status, balance, peak_balance, _now(), cid),
        )


def update_balance(cid, balance):
    with _lock, db() as c:
        row = c.execute(q("SELECT peak_balance FROM challenge WHERE id=%s"), (cid,)).fetchone()
        peak = max(row["peak_balance"], balance) if row else balance
        c.execute(q("UPDATE challenge SET balance=%s, peak_balance=%s WHERE id=%s"), (balance, peak, cid))


def backdate_start(cid, days):
    started = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
    with _lock, db() as c:
        c.execute(q("UPDATE challenge SET started_at=%s WHERE id=%s"), (started, cid))


def past_challenges(limit=10):
    with db() as c:
        rows = c.execute(q("SELECT * FROM challenge WHERE id<>0 ORDER BY id DESC LIMIT %s"), (limit,)).fetchall()
        return [_row(r) for r in rows]


# ---------------- trades ----------------

def add_trade(challenge_id, t):
    with _lock, db() as c:
        cur = c.execute(
            q("INSERT INTO trades(challenge_id,symbol,dir,entry,sl,tp,r_target,risk_dollars,opened_at) "
              "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id"),
            (challenge_id, t["symbol"], t["dir"], t["entry"], t["sl"], t["tp"],
             t.get("r_target"), t.get("risk_dollars"), t["opened_at"]),
        )
        return cur.fetchone()["id"]


def close_trade(trade_id, closed_at, r, dollars, result):
    with _lock, db() as c:
        c.execute(
            q("UPDATE trades SET closed_at=%s, r=%s, dollars=%s, result=%s WHERE id=%s"),
            (closed_at, r, dollars, result, trade_id),
        )


def open_trades(challenge_id):
    with db() as c:
        rows = c.execute(
            q("SELECT * FROM trades WHERE challenge_id=%s AND closed_at IS NULL ORDER BY id"), (challenge_id,)).fetchall()
        return [_row(r) for r in rows]


def closed_trades(challenge_id, limit=200):
    with db() as c:
        rows = c.execute(
            q("SELECT * FROM trades WHERE challenge_id=%s AND closed_at IS NOT NULL ORDER BY id DESC LIMIT %s"),
            (challenge_id, limit)).fetchall()
        return [_row(r) for r in rows]


def trade_hours(challenge_id):
    """Set of (symbol, hour-key) for dedupe — mirrors main.py existing check."""
    with db() as c:
        rows = c.execute(q("SELECT symbol, opened_at FROM trades WHERE challenge_id=%s"), (challenge_id,)).fetchall()
        return {(r["symbol"], str(r["opened_at"])[:13]) for r in rows}


# ---------------- connections ----------------

def save_connection(platform, config, status):
    with _lock, db() as c:
        c.execute(q("DELETE FROM connections WHERE platform=%s"), (platform,))
        c.execute(
            q("INSERT INTO connections(platform,config,status,created_at) VALUES (%s,%s,%s,%s)"),
            (platform, json.dumps(config), status, _now()),
        )


def get_connection(platform):
    with db() as c:
        d = _row(c.execute(
            q("SELECT * FROM connections WHERE platform=%s ORDER BY id DESC LIMIT 1"), (platform,)).fetchone())
        if d:
            d["config"] = json.loads(d["config"])
        return d


# ---------------- seed from the bot's real trade_log.json ----------------

def seed_from_log():
    """One-time import of the bot's real trade history as run #0 ('Live bot account')."""
    try:
        with db() as c:
            if c.execute("SELECT COUNT(*) AS n FROM trades WHERE challenge_id=0").fetchone()["n"]:
                return
        if not os.path.exists(LOG_PATH):
            return
        with open(LOG_PATH) as f:
            log = json.load(f)
        closed = log.get("closed", [])
        open_ = log.get("open", [])
        balance = 200000.0 + sum(float(t.get("dollars", 0)) for t in closed)
        with _lock, db() as c:
            c.execute(
                q("INSERT INTO challenge(id,created_at,size,risk_pct,mode,rules,status,balance,peak_balance) "
                  "VALUES (0,%s,200000,0.25,'live','{}','running',%s,%s) ON CONFLICT (id) DO NOTHING"),
                (_now(), balance, balance),
            )
            for t in closed:
                c.execute(
                    q("INSERT INTO trades(challenge_id,symbol,dir,entry,sl,tp,r_target,risk_dollars,opened_at,closed_at,r,dollars,result) "
                      "VALUES (0,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"),
                    (t["symbol"], t["dir"], t["entry"], t["sl"], t["tp"], t.get("r_target"),
                     t.get("risk_dollars", 500), t.get("opened_at"), t.get("closed_at"),
                     t.get("r"), t.get("dollars"), t.get("result", "TP")),
                )
            for t in open_:
                c.execute(
                    q("INSERT INTO trades(challenge_id,symbol,dir,entry,sl,tp,r_target,risk_dollars,opened_at) "
                      "VALUES (0,%s,%s,%s,%s,%s,%s,%s,%s)"),
                    (t["symbol"], t["dir"], t["entry"], t["sl"], t["tp"], t.get("r_target"),
                     t.get("risk_dollars", 500), t.get("opened_at")),
                )
    except Exception as e:
        print(f"[store] seed skipped: {e}")
