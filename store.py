"""Persistence: settings (config.json), API credentials (secrets.json,
file mode 0600, never sent back to the browser unmasked), and SQLite for
trades / equity history."""

import json
import os
import sqlite3
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SECRETS_PATH = os.path.join(BASE_DIR, "secrets.json")
DB_PATH = os.path.join(BASE_DIR, "bot.db")
POSITIONS_PATH = os.path.join(BASE_DIR, "positions.json")
DAY_STATE_PATH = os.path.join(BASE_DIR, "day_state.json")
MEMORY_PATH = os.path.join(BASE_DIR, "memory.json")
RUN_STATE_PATH = os.path.join(BASE_DIR, "run_state.json")


def _atomic_write_json(path: str, payload, mode: int | None = None):
    """Write JSON via a temp file + os.replace so a crash mid-write can
    never leave a truncated/corrupt file (positions.json IS the live
    position record — losing it means double-buys on restart)."""
    tmp = path + ".tmp"
    if mode is not None:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        f = os.fdopen(fd, "w")
    else:
        f = open(tmp, "w")
    with f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    if mode is not None:
        os.chmod(path, mode)

DEFAULT_CONFIG = {
    "exchange": "andx",
    "student_id": "",             # competition: unique ID seeds this bot's settings so no two students overlap (blank = operator default)
    "symbols": ["ALL"],  # every tradeable ANDX pair; or list specific ones
    "timeframe": "1h",
    "strategy": "auto",
    "mode": "paper",              # paper | testnet | live
    "trade_mode": "auto",         # auto | approve | manual — who pulls the trigger
    "chat_live_actions": "propose",  # off | propose — how chat acts on a REAL account:
                                     # propose = opens arrive as tap-to-approve cards
    "notifications": True,        # macOS notifications for fills/stops/alerts
    "paper_balance": 10000,
    "poll_seconds": 60,
    "slow_mode": False,           # conservative profit-focused profile
    "derivatives": False,          # trade ANDX margin instruments (longs+shorts)
    "volume_target_usd": 0,
    "volume_target_since": 0,
    "rotate_minutes": 15,
    "fee_pct_per_side": 0,   # fees are off for the competition
    "risk": {
        "risk_per_trade_pct": 1.0,
        "atr_stop_mult": 2.5,
        "atr_takeprofit_mult": 0.0,
        "max_position_pct": 25.0,
        "max_leverage": 2.0,
        "max_open_positions": 4,
        "daily_loss_limit_pct": 5.0,
    },
}

_lock = threading.Lock()


# ------------------------------------------------------------------ config

def load_config() -> dict:
    with _lock:
        if not os.path.exists(CONFIG_PATH):
            return json.loads(json.dumps(DEFAULT_CONFIG))
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.update(cfg)
    merged["risk"] = {**DEFAULT_CONFIG["risk"], **cfg.get("risk", {})}
    return merged


def save_config(cfg: dict):
    with _lock:
        _atomic_write_json(CONFIG_PATH, cfg)


# --------------------------------------------------------------- run state

def load_run_state() -> dict:
    """Did the user have the bot running (so a reboot can resume trading, not
    just the dashboard), and have they confirmed live trading at least once."""
    try:
        with open(RUN_STATE_PATH) as f:
            d = json.load(f)
        return {"running": bool(d.get("running")),
                "live_confirmed": bool(d.get("live_confirmed"))}
    except Exception:
        return {"running": False, "live_confirmed": False}


def save_run_state(running: bool, live_confirmed: bool | None = None):
    """Persist run intent. live_confirmed=None keeps the stored value."""
    if live_confirmed is None:
        live_confirmed = load_run_state()["live_confirmed"]
    with _lock:
        _atomic_write_json(RUN_STATE_PATH,
                           {"running": bool(running),
                            "live_confirmed": bool(live_confirmed)})


# ------------------------------------------------------------------ secrets

def load_secrets() -> dict:
    with _lock:
        if not os.path.exists(SECRETS_PATH):
            return {}
        with open(SECRETS_PATH) as f:
            return json.load(f)


def save_secrets(exchange: str, api_key: str, api_secret: str,
                 api_password: str = "", username: str = ""):
    secrets = load_secrets()
    secrets[exchange] = {
        "api_key": api_key.strip(),
        "api_secret": api_secret.strip(),
        "api_password": api_password.strip(),
        "username": username.strip(),
    }
    with _lock:
        _atomic_write_json(SECRETS_PATH, secrets, mode=0o600)


def delete_secrets(exchange: str):
    secrets = load_secrets()
    secrets.pop(exchange, None)
    with _lock:
        _atomic_write_json(SECRETS_PATH, secrets, mode=0o600)


def masked_key(exchange: str) -> str | None:
    creds = load_secrets().get(exchange)
    if not creds or not creds.get("api_key"):
        return None
    key = creds["api_key"]
    return key[:4] + "•" * 8 + key[-4:] if len(key) > 8 else "•" * 12


# ------------------------------------------------------------ positions
# Live positions are persisted so a bot restart resumes managing them
# instead of forgetting the coins it bought (and double-buying).

def save_open_positions(mode: str, exchange: str, positions: list[dict]):
    with _lock:
        _atomic_write_json(POSITIONS_PATH,
                           {"mode": mode, "exchange": exchange, "positions": positions})


# ------------------------------------------------------------ day state
# The daily-loss baseline and a tripped kill switch survive restarts: a
# restart must NOT re-arm a tripped breaker or re-baseline mid-day losses.

def save_day_state(mode: str, day_key: str, day_start_equity: float,
                   kill_switch_tripped: bool):
    with _lock:
        _atomic_write_json(DAY_STATE_PATH, {
            "mode": mode, "day_key": day_key,
            "day_start_equity": day_start_equity,
            "kill_switch_tripped": bool(kill_switch_tripped),
        })


def load_day_state(mode: str) -> dict | None:
    with _lock:
        if not os.path.exists(DAY_STATE_PATH):
            return None
        try:
            with open(DAY_STATE_PATH) as f:
                data = json.load(f)
        except (ValueError, OSError):
            return None
    if data.get("mode") != mode:
        return None
    return data


def load_open_positions(mode: str, exchange: str) -> list[dict]:
    with _lock:
        if not os.path.exists(POSITIONS_PATH):
            return []
        try:
            with open(POSITIONS_PATH) as f:
                data = json.load(f)
        except (ValueError, OSError):
            return []
    if data.get("mode") != mode or data.get("exchange") != exchange:
        return []
    return data.get("positions", [])


# ------------------------------------------------------ paper account
# The practice account persists across stops and restarts so progress is
# trackable. It only resets when the user explicitly asks (reset button) or
# changes the configured starting balance.
PAPER_STATE_PATH = os.path.join(BASE_DIR, "paper_state.json")


def save_paper_state(balance: float, initial_balance: float, positions: list[dict]):
    with _lock:
        _atomic_write_json(PAPER_STATE_PATH, {
            "balance": balance,
            "initial_balance": initial_balance,
            "positions": positions,
            "saved_at": time.time(),
        })


def load_paper_state() -> dict | None:
    with _lock:
        if not os.path.exists(PAPER_STATE_PATH):
            return None
        try:
            with open(PAPER_STATE_PATH) as f:
                return json.load(f)
        except (ValueError, OSError):
            return None


def reset_paper_state():
    with _lock:
        try:
            os.remove(PAPER_STATE_PATH)
        except FileNotFoundError:
            pass


# ------------------------------------------------------------------ sqlite

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, symbol TEXT, side TEXT, qty REAL,
            entry REAL, exit_price REAL, pnl REAL,
            strategy TEXT, mode TEXT, reason TEXT)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS equity (
            ts REAL, equity REAL, balance REAL, mode TEXT)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS fills (
            ts REAL, symbol TEXT, side TEXT, qty REAL,
            price REAL, notional REAL, mode TEXT)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, direction TEXT, price REAL, note TEXT,
            created_ts REAL, triggered_ts REAL)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS routines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT, params TEXT, schedule TEXT, enabled INTEGER,
            next_run_ts REAL, last_run_ts REAL, last_result TEXT,
            created_ts REAL)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS convictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, symbol TEXT, klass TEXT, phrase TEXT, mode TEXT)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS discipline_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, kind TEXT, symbol TEXT, detail TEXT, mode TEXT)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS portfolios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, source TEXT, spec TEXT, status TEXT,
            created_ts REAL, adopted_ts REAL, last_rebalance_ts REAL)"""
    )
    return conn


def record_fill(symbol: str, side: str, qty: float, price: float, mode: str):
    with _lock, _db() as conn:
        conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?)",
                     (time.time(), symbol, side, qty, price, qty * price, mode))


def cumulative_volume(mode: str, since_ts: float = 0.0) -> float:
    with _lock, _db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(notional),0) FROM fills WHERE mode=? AND ts>=?",
            (mode, since_ts)).fetchone()
    return float(row[0])


def record_trade(trade: dict, mode: str, reason: str):
    with _lock, _db() as conn:
        conn.execute(
            "INSERT INTO trades (ts, symbol, side, qty, entry, exit_price, pnl, strategy, mode, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), trade["symbol"],
             "long" if trade["side"] == 1 else "short",
             trade["qty"], trade["entry"], trade["exit"], trade["pnl"],
             trade.get("strategy", ""), mode, reason),
        )


def record_equity(equity: float, balance: float, mode: str):
    with _lock, _db() as conn:
        conn.execute("INSERT INTO equity VALUES (?,?,?,?)",
                     (time.time(), equity, balance, mode))


def trades_since(mode: str, since_ts: float) -> list[dict]:
    """All closed trades for one mode since a timestamp — the insights
    engine needs full history, not recent_trades' capped window."""
    with _lock, _db() as conn:
        rows = conn.execute(
            "SELECT ts, symbol, side, qty, entry, exit_price, pnl, strategy, mode, reason "
            "FROM trades WHERE mode=? AND ts>=? ORDER BY ts ASC",
            (mode, since_ts)).fetchall()
    cols = ["ts", "symbol", "side", "qty", "entry", "exit", "pnl", "strategy", "mode", "reason"]
    return [dict(zip(cols, r)) for r in rows]


# ------------------------------------------------------------------ alerts

def add_alert(symbol: str, direction: str, price: float, note: str = "") -> int:
    with _lock, _db() as conn:
        cur = conn.execute(
            "INSERT INTO alerts (symbol, direction, price, note, created_ts, triggered_ts) "
            "VALUES (?,?,?,?,?,NULL)",
            (symbol, direction, float(price), note, time.time()))
        return cur.lastrowid


def list_alerts(include_triggered: bool = False, limit: int = 50) -> list[dict]:
    q = ("SELECT id, symbol, direction, price, note, created_ts, triggered_ts "
         "FROM alerts ")
    if not include_triggered:
        q += "WHERE triggered_ts IS NULL "
    q += "ORDER BY id DESC LIMIT ?"
    with _lock, _db() as conn:
        rows = conn.execute(q, (limit,)).fetchall()
    cols = ["id", "symbol", "direction", "price", "note", "created_ts", "triggered_ts"]
    return [dict(zip(cols, r)) for r in rows]


def delete_alert(alert_id: int) -> bool:
    with _lock, _db() as conn:
        cur = conn.execute("DELETE FROM alerts WHERE id=?", (int(alert_id),))
        return cur.rowcount > 0


def trigger_alert(alert_id: int):
    with _lock, _db() as conn:
        conn.execute("UPDATE alerts SET triggered_ts=? WHERE id=? AND triggered_ts IS NULL",
                     (time.time(), int(alert_id)))


# ---------------------------------------------------------------- routines

def add_routine(kind: str, params: dict, schedule: dict, next_run_ts: float) -> int:
    with _lock, _db() as conn:
        cur = conn.execute(
            "INSERT INTO routines (kind, params, schedule, enabled, next_run_ts, "
            "last_run_ts, last_result, created_ts) VALUES (?,?,?,1,?,NULL,NULL,?)",
            (kind, json.dumps(params), json.dumps(schedule),
             float(next_run_ts), time.time()))
        return cur.lastrowid


def list_routines(limit: int = 30) -> list[dict]:
    with _lock, _db() as conn:
        rows = conn.execute(
            "SELECT id, kind, params, schedule, enabled, next_run_ts, "
            "last_run_ts, last_result FROM routines ORDER BY id LIMIT ?",
            (limit,)).fetchall()
    out = []
    for r in rows:
        try:
            params, schedule = json.loads(r[2]), json.loads(r[3])
        except ValueError:
            params, schedule = {}, {}
        out.append({"id": r[0], "kind": r[1], "params": params,
                    "schedule": schedule, "enabled": bool(r[4]),
                    "next_run_ts": r[5], "last_run_ts": r[6],
                    "last_result": r[7]})
    return out


def set_routine_enabled(routine_id: int, enabled: bool) -> bool:
    with _lock, _db() as conn:
        cur = conn.execute("UPDATE routines SET enabled=? WHERE id=?",
                           (1 if enabled else 0, int(routine_id)))
        return cur.rowcount > 0


def delete_routine(routine_id: int) -> bool:
    with _lock, _db() as conn:
        cur = conn.execute("DELETE FROM routines WHERE id=?", (int(routine_id),))
        return cur.rowcount > 0


def reschedule_routine(routine_id: int, next_run_ts: float, note: str = ""):
    """Move a routine's next run WITHOUT marking it as executed — used to
    skip missed slots instead of firing surprise catch-up runs."""
    with _lock, _db() as conn:
        if note:
            conn.execute(
                "UPDATE routines SET next_run_ts=?, last_result=? WHERE id=?",
                (float(next_run_ts), str(note)[:200], int(routine_id)))
        else:
            conn.execute("UPDATE routines SET next_run_ts=? WHERE id=?",
                         (float(next_run_ts), int(routine_id)))


def routine_ran(routine_id: int, next_run_ts: float, result: str):
    with _lock, _db() as conn:
        conn.execute(
            "UPDATE routines SET last_run_ts=?, next_run_ts=?, last_result=? "
            "WHERE id=?",
            (time.time(), float(next_run_ts), str(result)[:200], int(routine_id)))


# ---------------------------------------------------------- portfolios
# Sphinx (or any designer) offers a portfolio spec; adoption makes it the
# single ACTIVE portfolio the engine keeps on target. Spec is stored as
# JSON exactly as offered — the engine never edits a design, only runs it.

def add_portfolio(name: str, source: str, spec: dict) -> int:
    with _lock, _db() as conn:
        cur = conn.execute(
            "INSERT INTO portfolios (name, source, spec, status, created_ts, "
            "adopted_ts, last_rebalance_ts) VALUES (?,?,?,?,?,NULL,NULL)",
            (str(name)[:80], str(source)[:40], json.dumps(spec), "offered",
             time.time()))
        return cur.lastrowid


def list_portfolios(limit: int = 20) -> list[dict]:
    with _lock, _db() as conn:
        rows = conn.execute(
            "SELECT id, name, source, spec, status, created_ts, adopted_ts, "
            "last_rebalance_ts FROM portfolios ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
    out = []
    for r in rows:
        try:
            spec = json.loads(r[3])
        except ValueError:
            spec = {}
        out.append({"id": r[0], "name": r[1], "source": r[2], "spec": spec,
                    "status": r[4], "created_ts": r[5], "adopted_ts": r[6],
                    "last_rebalance_ts": r[7]})
    return out


def get_active_portfolio() -> dict | None:
    for p in list_portfolios():
        if p["status"] == "active":
            return p
    return None


def set_portfolio_status(portfolio_id: int, status: str,
                         adopted: bool = False) -> bool:
    with _lock, _db() as conn:
        if status == "active":
            # exactly one active portfolio at a time
            conn.execute(
                "UPDATE portfolios SET status='retired' WHERE status='active'")
        if adopted:
            cur = conn.execute(
                "UPDATE portfolios SET status=?, adopted_ts=? WHERE id=?",
                (status, time.time(), int(portfolio_id)))
        else:
            cur = conn.execute(
                "UPDATE portfolios SET status=? WHERE id=?",
                (status, int(portfolio_id)))
        return cur.rowcount > 0


def portfolio_rebalanced(portfolio_id: int):
    with _lock, _db() as conn:
        conn.execute(
            "UPDATE portfolios SET last_rebalance_ts=? WHERE id=?",
            (time.time(), int(portfolio_id)))


# ------------------------------------------------- coach: raw material
# Convictions: the user's own confidence words attached to their trades.
# Discipline events: stop moves and other rule-bends, recorded as they
# happen so the coach's reports are computed, never guessed.

def add_conviction(symbol: str, klass: str, phrase: str, mode: str):
    with _lock, _db() as conn:
        conn.execute(
            "INSERT INTO convictions (ts, symbol, klass, phrase, mode) "
            "VALUES (?,?,?,?,?)",
            (time.time(), symbol, klass, str(phrase)[:120], mode))


def list_convictions(mode: str, limit: int = 300) -> list[dict]:
    with _lock, _db() as conn:
        rows = conn.execute(
            "SELECT ts, symbol, klass, phrase FROM convictions "
            "WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, limit)).fetchall()
    return [dict(zip(["ts", "symbol", "klass", "phrase"], r)) for r in rows]


def add_discipline_event(kind: str, symbol: str, detail: str, mode: str):
    with _lock, _db() as conn:
        conn.execute(
            "INSERT INTO discipline_events (ts, kind, symbol, detail, mode) "
            "VALUES (?,?,?,?,?)",
            (time.time(), kind, symbol, str(detail)[:300], mode))


def list_discipline_events(mode: str, since_ts: float = 0.0) -> list[dict]:
    with _lock, _db() as conn:
        rows = conn.execute(
            "SELECT ts, kind, symbol, detail FROM discipline_events "
            "WHERE mode=? AND ts>=? ORDER BY ts ASC", (mode, since_ts)).fetchall()
    return [dict(zip(["ts", "kind", "symbol", "detail"], r)) for r in rows]


def fills_since(mode: str, since_ts: float) -> list[dict]:
    with _lock, _db() as conn:
        rows = conn.execute(
            "SELECT ts, symbol, side, qty, price, notional FROM fills "
            "WHERE mode=? AND ts>=? ORDER BY ts ASC", (mode, since_ts)).fetchall()
    return [dict(zip(["ts", "symbol", "side", "qty", "price", "notional"], r))
            for r in rows]


# ------------------------------------------------------------------ memory
# The assistant's persistent notebook about its owner. One flat list of
# short facts; _mem_lock covers the whole read-modify-write so two
# concurrent chat requests can't drop each other's writes.

_mem_lock = threading.Lock()
MEMORY_CAP = 40


def load_memory() -> list[dict]:
    with _mem_lock:
        return _read_memory()


def _read_memory() -> list[dict]:
    if not os.path.exists(MEMORY_PATH):
        return []
    try:
        with open(MEMORY_PATH) as f:
            data = json.load(f)
        facts = data.get("facts", [])
        return facts if isinstance(facts, list) else []
    except (ValueError, OSError):
        return []


def remember(text: str) -> dict:
    text = str(text).strip()[:300]
    with _mem_lock:
        facts = _read_memory()
        for fact in facts:
            if fact.get("text", "").lower() == text.lower():
                return fact  # already known — don't duplicate
        fact = {"id": (max((f.get("id", 0) for f in facts), default=0) + 1),
                "text": text, "ts": time.time()}
        facts.append(fact)
        facts = facts[-MEMORY_CAP:]  # newest facts win the cap
        _atomic_write_json(MEMORY_PATH, {"facts": facts}, mode=0o600)
    return fact


def forget(match: str) -> int:
    match = str(match).strip().lower()
    if not match:
        return 0
    with _mem_lock:
        facts = _read_memory()
        keep = [f for f in facts if match not in f.get("text", "").lower()]
        removed = len(facts) - len(keep)
        if removed:
            _atomic_write_json(MEMORY_PATH, {"facts": keep}, mode=0o600)
    return removed


def recent_trades(limit: int = 50) -> list[dict]:
    with _lock, _db() as conn:
        rows = conn.execute(
            "SELECT ts, symbol, side, qty, entry, exit_price, pnl, strategy, mode, reason "
            "FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    cols = ["ts", "symbol", "side", "qty", "entry", "exit", "pnl", "strategy", "mode", "reason"]
    return [dict(zip(cols, r)) for r in rows]


def equity_curve(mode: str, limit: int = 500) -> list[dict]:
    with _lock, _db() as conn:
        rows = conn.execute(
            "SELECT ts, equity FROM equity WHERE mode=? ORDER BY ts DESC LIMIT ?",
            (mode, limit),
        ).fetchall()
    return [{"ts": r[0], "equity": r[1]} for r in reversed(rows)]


def todays_stopouts(mode: str) -> dict:
    """{symbol: count} of losing stop-outs since UTC midnight — feeds the
    per-symbol cooldown (a pair that stopped out twice today is a churn
    machine, not an opportunity)."""
    day_start = time.time() - (time.time() % 86400)
    with _lock, _db() as conn:
        rows = conn.execute(
            "SELECT symbol, COUNT(*) FROM trades "
            "WHERE mode=? AND ts>=? AND reason LIKE 'stop%' AND pnl<0 "
            "GROUP BY symbol", (mode, day_start)).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def loss_streak(mode: str) -> int:
    """Consecutive most-recent losing trades (capped at 20) — feeds the
    risk governor that halves size while the bot is cold."""
    with _lock, _db() as conn:
        rows = conn.execute(
            "SELECT pnl FROM trades WHERE mode=? ORDER BY id DESC LIMIT 20",
            (mode,)).fetchall()
    streak = 0
    for (pnl,) in rows:
        if pnl is not None and pnl < 0:
            streak += 1
        else:
            break
    return streak


def performance(mode: str, days: int = 14) -> dict:
    """Per-symbol expectancy over the last N days:
    {symbol: {n, win_rate, total}} — feeds journal-based size multipliers."""
    since = time.time() - days * 86400
    with _lock, _db() as conn:
        rows = conn.execute(
            "SELECT symbol, COUNT(*), SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), "
            "SUM(pnl) FROM trades WHERE mode=? AND ts>=? GROUP BY symbol",
            (mode, since)).fetchall()
    return {r[0]: {"n": int(r[1]), "win_rate": (r[2] or 0) / r[1] if r[1] else 0.0,
                   "total": float(r[3] or 0)} for r in rows}


def trade_stats(mode: str) -> dict:
    day_start = time.time() - (time.time() % 86400)  # UTC midnight
    with _lock, _db() as conn:
        rows = conn.execute("SELECT pnl FROM trades WHERE mode=?", (mode,)).fetchall()
        today = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE mode=? AND ts>=?",
            (mode, day_start)).fetchone()[0]
    pnls = [r[0] for r in rows]
    wins = [p for p in pnls if p > 0]
    return {
        "count": len(pnls),
        "total_pnl": sum(pnls),
        "win_rate": (len(wins) / len(pnls) * 100) if pnls else 0.0,
        "today": today,
    }
