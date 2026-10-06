"""Dashboard server. Run with:  python app.py  (from the trading_bot folder)

Binds to 127.0.0.1 only — API keys and controls never leave your machine.
"""

import os
import time

from flask import Flask, jsonify, render_template, request, send_file

import store
from engine import ENGINE
from exchange import SUPPORTED_EXCHANGES, check_credentials
from strategies import STRATEGIES

app = Flask(__name__)

_LOCAL_ORIGINS = {"http://127.0.0.1:8300", "http://localhost:8300"}


@app.before_request
def _block_cross_origin_writes():
    """The server binds to 127.0.0.1, but any website open in the browser can
    still fire cross-origin POSTs at localhost. State-changing requests must
    come from our own dashboard page (or a non-browser client with no Origin)."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    origin = request.headers.get("Origin")
    if origin and origin not in _LOCAL_ORIGINS:
        return jsonify({"ok": False, "detail": "cross-origin request blocked"}), 403
    return None


@app.get("/")
def dashboard():
    return render_template("dashboard.html")


@app.get("/ai")
def ai_surface():
    """The ANDX AI assistant surface, served same-origin so its live link can
    read /api/state (and close sim positions) from the running engine."""
    path = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "andi_preview.html"))
    return send_file(path)


@app.get("/api/state")
def state():
    snap = ENGINE.snapshot()
    cfg = store.load_config()
    snap["config"] = cfg
    snap["exchanges"] = SUPPORTED_EXCHANGES
    snap["strategies"] = {name: cls.label for name, cls in STRATEGIES.items()}
    snap["saved_key"] = store.masked_key(cfg["exchange"])
    return jsonify(snap)


@app.post("/api/settings")
def save_settings():
    body = request.get_json(force=True)
    cfg = store.load_config()
    if ENGINE.running:
        return jsonify({"ok": False, "detail": "stop the bot before changing settings"}), 400
    for key in ("exchange", "timeframe", "strategy", "mode"):
        if key in body:
            cfg[key] = body[key]
    if "student_id" in body:
        cfg["student_id"] = str(body["student_id"]).strip()
    if "symbols" in body:
        symbols = [s.strip() for s in body["symbols"] if s.strip()]
        if symbols:
            cfg["symbols"] = symbols
    if "paper_balance" in body:
        cfg["paper_balance"] = float(body["paper_balance"])
    if "poll_seconds" in body:
        cfg["poll_seconds"] = int(body["poll_seconds"])
    if "risk" in body:
        cfg["risk"] = {**cfg["risk"], **{k: v for k, v in body["risk"].items() if v not in ("", None)}}
    store.save_config(cfg)
    return jsonify({"ok": True})


@app.post("/api/keys")
def save_keys():
    body = request.get_json(force=True)
    exchange = body.get("exchange") or store.load_config()["exchange"]
    api_key = body.get("api_key", "").strip()
    api_secret = body.get("api_secret", "").strip()
    api_password = body.get("api_password", "").strip()
    username = body.get("username", "").strip()
    if not api_key or not api_secret:
        return jsonify({"ok": False, "detail": "API key and secret are both required"}), 400
    store.save_secrets(exchange, api_key, api_secret, api_password, username)
    result = check_credentials(
        exchange,
        {"api_key": api_key, "api_secret": api_secret,
         "api_password": api_password, "username": username},
        testnet=(store.load_config()["mode"] == "testnet"),
    )
    result["masked"] = store.masked_key(exchange)
    return jsonify(result)


_BASE = os.path.dirname(os.path.abspath(__file__))
STRATEGIES_FILE = os.path.join(_BASE, "strategies.py")
STRATEGIES_ORIGINAL = os.path.join(_BASE, "strategies_original.py")


@app.get("/api/code")
def get_code():
    """Return the student's editable strategy code."""
    try:
        with open(STRATEGIES_FILE) as f:
            return jsonify({"ok": True, "code": f.read(),
                            "has_backup": os.path.exists(STRATEGIES_ORIGINAL)})
    except OSError as e:
        return jsonify({"ok": False, "detail": str(e)}), 500


@app.post("/api/code")
def save_code():
    """Save edited strategy code — but ONLY if it's valid Python that imports
    cleanly, so a typo can never brick the bot. Keeps a one-time backup of the
    original, then applies the change live if the bot is running."""
    import subprocess
    import sys
    import tempfile
    import shutil
    body = request.get_json(force=True)
    code = body.get("code", "")
    if not code.strip():
        return jsonify({"ok": False, "detail": "The code is empty."}), 400
    # 1) syntax check
    try:
        compile(code, "strategies.py", "exec")
    except SyntaxError as e:
        return jsonify({"ok": False,
                        "detail": f"Syntax error on line {e.lineno}: {e.msg}"}), 400
    # 2) import check in a throwaway process (catches undefined names, bad
    #    edits to the STRATEGIES table, etc.) BEFORE we touch the real file
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
        tf.write(code)
        tmp = tf.name
    try:
        probe = (
            "import importlib.util,sys;"
            f"spec=importlib.util.spec_from_file_location('t',{tmp!r});"
            "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
            "assert hasattr(m,'make_strategy'),'must keep make_strategy()';"
            "assert hasattr(m,'STRATEGIES'),'must keep the STRATEGIES table'"
        )
        r = subprocess.run([sys.executable, "-c", probe],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            err = (r.stderr.strip().splitlines() or ["import failed"])[-1]
            return jsonify({"ok": False, "detail": f"Code won't run: {err}"}), 400
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "detail": "Code took too long to load — "
                        "check for an infinite loop at the top level."}), 400
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    # 3) one-time backup of the original, then write
    if not os.path.exists(STRATEGIES_ORIGINAL):
        try:
            shutil.copy(STRATEGIES_FILE, STRATEGIES_ORIGINAL)
        except OSError:
            pass
    try:
        with open(STRATEGIES_FILE, "w") as f:
            f.write(code)
    except OSError as e:
        return jsonify({"ok": False, "detail": str(e)}), 500
    # 4) apply live if running (restart reloads the strategy module)
    applied = False
    if ENGINE.running:
        ENGINE.stop(flatten=False)
        ok, detail = ENGINE.start()
        applied = ok
        if not ok:
            return jsonify({"ok": False,
                            "detail": f"Saved, but couldn't apply: {detail}"}), 400
    ENGINE.log("strategy code updated" + (" and applied" if applied else " (saved)"))
    return jsonify({"ok": True, "applied": applied})


@app.post("/api/code/restore")
def restore_code():
    """Put back the original strategy code."""
    import shutil
    if not os.path.exists(STRATEGIES_ORIGINAL):
        return jsonify({"ok": False, "detail": "No original backup found."}), 400
    try:
        shutil.copy(STRATEGIES_ORIGINAL, STRATEGIES_FILE)
    except OSError as e:
        return jsonify({"ok": False, "detail": str(e)}), 500
    applied = False
    if ENGINE.running:
        ENGINE.stop(flatten=False)
        applied = ENGINE.start()[0]
    return jsonify({"ok": True, "applied": applied})


@app.delete("/api/keys")
def delete_keys():
    exchange = request.args.get("exchange") or store.load_config()["exchange"]
    store.delete_secrets(exchange)
    return jsonify({"ok": True})


@app.post("/api/start")
def start():
    cfg = store.load_config()
    if cfg["mode"] == "live":
        confirmed = (request.get_json(silent=True) or {}).get("confirm_live")
        if not confirmed:
            return jsonify({"ok": False, "detail": "live mode requires confirmation",
                            "needs_confirmation": True}), 400
    ok, detail = ENGINE.start()
    if ok:
        store.save_run_state(True, live_confirmed=(cfg["mode"] == "live"))
    return jsonify({"ok": ok, "detail": detail}), (200 if ok else 400)


@app.post("/api/stop")
def stop():
    flatten = (request.get_json(silent=True) or {}).get("flatten", False)
    ENGINE.stop(flatten=flatten)
    store.save_run_state(False)
    return jsonify({"ok": True})


@app.post("/api/slow_mode")
def slow_mode():
    on = bool((request.get_json(silent=True) or {}).get("on"))
    cfg = store.load_config()
    cfg["slow_mode"] = on
    store.save_config(cfg)
    restarted, detail = False, ""
    if ENGINE.running:
        ENGINE.stop(flatten=False)
        restarted, detail = ENGINE.start()
        if not restarted:
            ENGINE.log(f"restart after slow-mode toggle failed: {detail}", "error")
    return jsonify({"ok": True, "slow_mode": on, "restarted": restarted,
                    "detail": detail})


@app.post("/api/reset_paper")
def reset_paper():
    """Explicit practice-account reset — the ONLY way paper progress clears
    besides changing the configured starting balance."""
    cfg = store.load_config()
    if ENGINE.running and cfg.get("mode") == "paper":
        return jsonify({"ok": False,
                        "detail": "stop the bot first, then reset"}), 400
    store.reset_paper_state()
    ENGINE.log("practice account reset — next start begins fresh from the "
               "configured paper balance")
    return jsonify({"ok": True})


@app.post("/api/market_mode")
def market_mode():
    on = bool((request.get_json(silent=True) or {}).get("derivatives"))
    cfg = store.load_config()
    cfg["derivatives"] = on
    store.save_config(cfg)
    restarted, detail = False, ""
    if ENGINE.running:
        # switching markets closes everything first so no position is orphaned
        ENGINE.stop(flatten=True)
        restarted, detail = ENGINE.start()
        if not restarted:
            ENGINE.log(f"restart after market switch failed: {detail}", "error")
    return jsonify({"ok": True, "derivatives": on, "restarted": restarted,
                    "detail": detail})


@app.post("/api/trade_mode")
def trade_mode():
    body = request.get_json(silent=True) or {}
    result = ENGINE.set_trade_mode(body.get("mode", ""))
    ok = "error" not in result
    return jsonify({"ok": ok, **result}), (200 if ok else 400)


@app.post("/api/proposal")
def proposal_action():
    body = request.get_json(silent=True) or {}
    pid, action = body.get("id"), str(body.get("action", "")).lower()
    if action == "approve":
        result = ENGINE.approve_proposal(pid)
    elif action == "decline":
        result = ENGINE.decline_proposal(pid)
    else:
        return jsonify({"ok": False, "error": "action must be approve or decline"}), 400
    ok = "error" not in result
    return jsonify({"ok": ok, **result}), (200 if ok else 400)


@app.post("/api/routine")
def routine_action():
    body = request.get_json(silent=True) or {}
    try:
        rid = int(body.get("id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "detail": "bad routine id"}), 400
    action = str(body.get("action", "")).lower()
    if action == "pause":
        return jsonify({"ok": store.set_routine_enabled(rid, False)})
    if action == "resume":
        ok = store.set_routine_enabled(rid, True)
        if ok:
            # resuming a long-paused routine must NOT fire it instantly —
            # re-anchor a past-due clock to the next scheduled slot
            r = next((x for x in store.list_routines() if x["id"] == rid), None)
            if r and r.get("next_run_ts") and r["next_run_ts"] <= time.time():
                store.reschedule_routine(
                    rid, ENGINE.next_routine_run(r["schedule"], time.time()))
        return jsonify({"ok": ok})
    if action == "delete":
        return jsonify({"ok": store.delete_routine(rid)})
    return jsonify({"ok": False, "detail": "action must be pause, resume, or delete"}), 400


@app.delete("/api/alert")
def delete_alert():
    try:
        alert_id = int(request.args.get("id", ""))
    except ValueError:
        return jsonify({"ok": False, "detail": "bad alert id"}), 400
    return jsonify({"ok": store.delete_alert(alert_id)})


@app.post("/api/sphinx")
def sphinx_intake():
    """The receiving end of the Sphinx handoff. Accepts the three packet
    types; nothing executes here — portfolios wait for adoption, trade
    ideas become tap-to-approve proposals, protection applies stops/alerts."""
    body = request.get_json(silent=True) or {}
    ptype = str(body.get("type", "")).lower()
    payload = body.get("payload") or {}
    if ptype == "portfolio":
        targets = payload.get("targets") or []
        if not targets or not isinstance(targets, list):
            return jsonify({"ok": False, "error": "portfolio needs targets"}), 400
        pid = store.add_portfolio(payload.get("name", "Sphinx portfolio"),
                                  payload.get("source", "sphinx"), payload)
        return jsonify({"ok": True, "portfolio_id": pid,
                        "status": "offered — adopt it via ANDX AI"})
    if ptype == "trade_idea":
        side = 1 if str(payload.get("side", "long")).lower() in ("long", "buy") else -1
        try:
            usd = float(payload.get("usd_amount") or payload.get("notional") or 0)
            stop = payload.get("stop")
            tp = payload.get("target")
            result = ENGINE.manual_propose(
                str(payload.get("symbol", "")).upper(), side, usd,
                float(stop) if stop else None, float(tp) if tp else None)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "bad numbers in trade idea"}), 400
        ok = "error" not in result
        return jsonify({"ok": ok, **result}), (200 if ok else 400)
    if ptype == "protection":
        result = ENGINE.apply_protection(payload)
        ok = "refused" not in result
        return jsonify({"ok": ok, **result}), (200 if ok else 403)
    return jsonify({"ok": False,
                    "error": "type must be portfolio, trade_idea, or protection"}), 400


@app.get("/api/ai_status")
def ai_status():
    import ai_brain
    key = ai_brain.get_api_key()
    return jsonify({"ok": True, "has_key": bool(key),
                    "masked": ai_brain.masked_key(), "model": ai_brain.MODEL})


@app.post("/api/ai_key")
def ai_key():
    body = request.get_json(force=True)
    key = (body.get("api_key") or "").strip()
    if not key.startswith("sk-ant-") or len(key) < 20:
        return jsonify({"ok": False, "detail": "that doesn't look like an Anthropic key (sk-ant-…)"}), 400
    store.save_secrets("anthropic", key, "-")
    import ai_brain
    return jsonify({"ok": True, "masked": ai_brain.masked_key()})


@app.post("/api/chat")
def ai_chat():
    import ai_brain
    body = request.get_json(force=True) or {}
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        return jsonify({"ok": False, "error": "bad_request"}), 400
    result = ai_brain.chat(messages)
    status = 200 if result.get("ok") else (401 if result.get("error") in ("no_key", "bad_key") else 502)
    return jsonify(result), status


@app.post("/api/close")
def close_position():
    symbol = (request.get_json(force=True)).get("symbol", "")
    try:
        ok = ENGINE.close_position(symbol)
        return jsonify({"ok": ok})
    except Exception as e:
        ENGINE.log(f"manual close failed for {symbol}: {e}", "error")
        return jsonify({"ok": False, "detail": str(e)}), 400


def _auto_resume_paper():
    """After a restart, resume PAPER trading by itself if the user had it
    running — so students don't have to re-click Start every reboot. Live is
    never auto-started here: real money always needs an explicit Start with
    confirmation. Runs a few seconds after boot and never blocks the dashboard
    from loading."""
    import threading

    def _go():
        time.sleep(8)
        try:
            rs = store.load_run_state()
            if ENGINE.running or not rs.get("running"):
                return
            if store.load_config().get("mode", "paper") != "paper":
                return
            ok, detail = ENGINE.start()
            if ok:
                ENGINE.log("auto-resumed paper trading after restart")
            else:
                ENGINE.log(f"auto-resume skipped: {detail}", "warn")
        except Exception as e:
            try:
                ENGINE.log(f"auto-resume error: {e}", "warn")
            except Exception:
                pass

    threading.Thread(target=_go, daemon=True).start()


if __name__ == "__main__":
    print("\n  ANDX Trading Bot — dashboard: http://127.0.0.1:8300\n")
    try:
        _auto_resume_paper()
    except Exception:
        pass
    app.run(host="127.0.0.1", port=8300, debug=False)
