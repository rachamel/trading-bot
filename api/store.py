# api/store.py — SQLite persistence for the Prop Terminal
import sqlite3, json, os, threading

DB_PATH = os.environ.get(
    "TERMINAL_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "terminal.db"),
)
LOG_PATH = os.environ.get(
    "TRADE_LOG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trade_log.json"),
)

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS challenge (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT, size REAL, risk_pct REAL, mode TEXT, rules TEXT,
  status TEXT, balance REAL, peak_balance REAL, started_at TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  challenge_id INTEGER, symbol TEXT, dir TEXT,
  entry REAL, sl REAL, tp REAL, r_target REAL, risk_dollars REAL,
  opened_at TEXT, closed_at TEXT, r REAL, dollars REAL, result TEXT
);
CREATE TABLE IF NOT EXISTS connections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT, config TEXT, status TEXT, created_at TEXT
);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init():
    with _lock, db() as c:
        c.executescript(SCHEMA)
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('telegram_enabled','true')")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('default_risk_pct','0.25')")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('default_mode','replay')")
    seed_from_log()


def get_setting(key, default=None):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with _lock, db() as c:
        c.execute(
            "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


# ---------------- challenges ----------------

def _rules_json(rules):
    if isinstance(rules, str):
        return rules
    return json.dumps(rules)


def create_challenge(size, risk_pct, mode, rules):
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with _lock, db() as c:
        cur = c.execute(
            "INSERT INTO challenge(created_at,size,risk_pct,mode,rules,status,balance,peak_balance,started_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (now, size, risk_pct, mode, _rules_json(rules), "running", size, size, now),
        )
        return cur.lastrowid


def cancel_running():
    with _lock, db() as c:
        c.execute("UPDATE challenge SET status='cancelled', ended_at=datetime('now') WHERE status='running' AND id<>0")


def get_challenge(cid):
    with db() as c:
        row = c.execute("SELECT * FROM challenge WHERE id=?", (cid,)).fetchone()
        return dict(row) if row else None


def active_challenge():
    with db() as c:
        row = c.execute(
            "SELECT * FROM challenge WHERE status='running' AND id<>0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def latest_challenge():
    with db() as c:
        row = c.execute("SELECT * FROM challenge ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def complete_challenge(cid, status, balance, peak_balance=None):
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with _lock, db() as c:
        c.execute(
            "UPDATE challenge SET status=?, balance=?, peak_balance=COALESCE(?,peak_balance), ended_at=? WHERE id=?",
            (status, balance, peak_balance, now, cid),
        )


def update_balance(cid, balance):
    with _lock, db() as c:
        c.execute(
            "UPDATE challenge SET balance=?, peak_balance=MAX(peak_balance,?) WHERE id=?",
            (balance, balance, cid),
        )


def backdate_start(cid, days):
    """Replay runs: backdate started_at so 'days elapsed' reflects the simulated window."""
    with _lock, db() as c:
        c.execute(
            "UPDATE challenge SET started_at = datetime(started_at, ?) WHERE id=?",
            (f'-{int(days)} days', cid),
        )


def past_challenges(limit=10):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM challenge WHERE id<>0 ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------- trades ----------------

def add_trade(challenge_id, t):
    with _lock, db() as c:
        cur = c.execute(
            "INSERT INTO trades(challenge_id,symbol,dir,entry,sl,tp,r_target,risk_dollars,opened_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (challenge_id, t["symbol"], t["dir"], t["entry"], t["sl"], t["tp"],
             t.get("r_target"), t.get("risk_dollars"), t["opened_at"]),
        )
        return cur.lastrowid


def close_trade(trade_id, closed_at, r, dollars, result):
    with _lock, db() as c:
        c.execute(
            "UPDATE trades SET closed_at=?, r=?, dollars=?, result=? WHERE id=?",
            (closed_at, r, dollars, result, trade_id),
        )


def open_trades(challenge_id):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM trades WHERE challenge_id=? AND closed_at IS NULL ORDER BY id", (challenge_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def closed_trades(challenge_id, limit=200):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM trades WHERE challenge_id=? AND closed_at IS NOT NULL ORDER BY id DESC LIMIT ?",
            (challenge_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def trade_hours(challenge_id):
    """Set of (symbol, hour-key) for dedupe — mirrors main.py existing check."""
    with db() as c:
        rows = c.execute(
            "SELECT symbol, opened_at FROM trades WHERE challenge_id=?", (challenge_id,)
        ).fetchall()
        return {(r["symbol"], str(r["opened_at"])[:13]) for r in rows}


# ---------------- connections ----------------

def save_connection(platform, config, status):
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with _lock, db() as c:
        c.execute("DELETE FROM connections WHERE platform=?", (platform,))
        c.execute(
            "INSERT INTO connections(platform,config,status,created_at) VALUES (?,?,?,?)",
            (platform, json.dumps(config), status, now),
        )


def get_connection(platform):
    with db() as c:
        row = c.execute("SELECT * FROM connections WHERE platform=? ORDER BY id DESC LIMIT 1", (platform,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["config"] = json.loads(d["config"])
        return d


# ---------------- seed from the bot's real trade_log.json ----------------

def seed_from_log():
    """One-time import of the bot's real trade history as run #0 ('Live bot account')."""
    try:
        with db() as c:
            if c.execute("SELECT COUNT(*) FROM trades WHERE challenge_id=0").fetchone()[0]:
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
                "INSERT OR IGNORE INTO challenge(id,created_at,size,risk_pct,mode,rules,status,balance,peak_balance) "
                "VALUES (0,datetime('now'),200000,0.25,'live','{}','running',?,?)",
                (balance, balance),
            )
            for t in closed:
                c.execute(
                    "INSERT INTO trades(challenge_id,symbol,dir,entry,sl,tp,r_target,risk_dollars,opened_at,closed_at,r,dollars,result) "
                    "VALUES (0,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t["symbol"], t["dir"], t["entry"], t["sl"], t["tp"], t.get("r_target"),
                     t.get("risk_dollars", 500), t.get("opened_at"), t.get("closed_at"),
                     t.get("r"), t.get("dollars"), t.get("result", "TP")),
                )
            for t in open_:
                c.execute(
                    "INSERT INTO trades(challenge_id,symbol,dir,entry,sl,tp,r_target,risk_dollars,opened_at) "
                    "VALUES (0,?,?,?,?,?,?,?)",
                    (t["symbol"], t["dir"], t["entry"], t["sl"], t["tp"], t.get("r_target"),
                     t.get("risk_dollars", 500), t.get("opened_at")),
                )
    except Exception as e:
        print(f"[store] seed skipped: {e}")
