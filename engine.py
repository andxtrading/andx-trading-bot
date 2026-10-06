"""The trading engine: a background loop that, every poll interval,
fetches candles for each symbol, asks the strategy for a desired position,
and reconciles reality to it (open long / open short / flip / flatten),
with ATR stops, trailing stops, and a daily-loss kill switch."""

import importlib
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import pandas as pd

import risk as risk_mod
import store
import strategies as strategy_module   # reloaded on each start() so edits to
                                       # strategies.py apply without a full quit
from exchange import (MarketData, PaperBroker, LiveBroker, AndxBroker,
                      AndxMarginBroker, Position)
from strategies import make_strategy, atr, htf_bias

TIMEFRAME_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


class BotEngine:
    def __init__(self):
        self.thread: threading.Thread | None = None
        self.running = False
        self.status = "stopped"
        self.logs: deque[dict] = deque(maxlen=300)
        self.broker = None
        self.market: MarketData | None = None
        self.strategies: dict[str, object] = {}
        self.last_signals: dict[str, str] = {}
        self.prices: dict[str, float] = {}
        self.day_start_equity: float | None = None
        self.day_key: str | None = None
        self.kill_switch_tripped = False
        self.error: str | None = None
        self.spot_only = False
        self.volume_target_hit = False
        self.unavailable: dict[str, float] = {}  # symbol -> ts of "pair disabled"
        self._gen = 0  # loop generation — stale loops from rapid restarts must die
        self._lock = threading.Lock()
        # serializes every broker order against the web thread (manual close /
        # flatten) — concurrent orders corrupt balance-diff fill verification
        self._order_lock = threading.RLock()
        # legacy spot positions being guarded while the engine trades margin
        self.spot_guard: AndxBroker | None = None
        self.htf: dict[str, tuple[float, int, float]] = {}  # sym -> (ts, bias, adx)
        self._perf: dict = {}
        self._perf_ts = 0.0
        self._governor = {"stopouts": {}, "streak": 0, "ts": 0.0}
        # who pulls the trigger: auto | approve | manual. Runtime-settable
        # (the loop's cfg is frozen at start, so this lives on the engine).
        self.trade_mode = str(store.load_config().get("trade_mode", "auto"))
        # tap-to-approve proposals. _prop_lock is a LEAF lock: held only for
        # dict access, never across network calls or other locks.
        self.proposals: dict[int, dict] = {}
        self._prop_lock = threading.Lock()
        self._prop_seq = 0
        # symbol -> ts until which we won't re-propose (decline/ignore respect)
        self._prop_cooldown: dict[str, float] = {}
        self._notify_last: dict[str, float] = {}
        # routine id -> last fire ts (in-memory double-fire guard)
        self._routine_fired: dict[int, float] = {}

    # ------------------------------------------------------------- control

    def log(self, msg: str, level: str = "info"):
        self.logs.appendleft({"ts": time.time(), "level": level, "msg": msg})

    def notify(self, title: str, text: str):
        """macOS banner via osascript — fire-and-forget Popen so a hung
        osascript can never stall the tick. Same text deduped for 60s."""
        try:
            if not store.load_config().get("notifications", True):
                return
            now = time.time()
            if now - self._notify_last.get(text, 0) < 60:
                return  # same text within a minute — once is enough. No global
                # gap: two DIFFERENT events in one tick both deserve banners
                # (a dropped one — alert, kill switch — never gets re-sent).
            self._notify_last[text] = now
            if len(self._notify_last) > 400:  # keep the dedupe map bounded
                cutoff = now - 3600
                self._notify_last = {k: v for k, v in self._notify_last.items()
                                     if v > cutoff}

            def esc(s):
                return str(s).replace("\\", "").replace('"', "'")[:180]

            script = f'display notification "{esc(text)}" with title "{esc(title)}"'
            subprocess.Popen(["osascript", "-e", script],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self.running:
                return False, "already running"
            cfg = store.load_config()
            if cfg.get("slow_mode"):
                # Conservative mode: profit-focused. Fewer, higher-conviction
                # trades on 1h candles; smaller risk per trade; wider stops so
                # winners aren't shaken out; hard 5% daily-loss circuit
                # breaker; no volume-target churn.
                cfg = dict(cfg)
                cfg["timeframe"] = "1h"
                cfg["poll_seconds"] = max(int(cfg.get("poll_seconds", 60)), 120)
                cfg["volume_target_usd"] = 0
                r = dict(cfg.get("risk", {}))
                r["risk_per_trade_pct"] = min(float(r.get("risk_per_trade_pct", 1) or 1), 0.5)
                r["max_position_pct"] = min(float(r.get("max_position_pct", 25) or 25), 20)
                r["max_open_positions"] = min(int(r.get("max_open_positions", 4) or 4), 3)
                r["daily_loss_limit_pct"] = min(float(r.get("daily_loss_limit_pct", 5) or 5), 5)
                r["atr_stop_mult"] = max(float(r.get("atr_stop_mult", 2.5) or 2.5), 3.0)
                cfg["risk"] = r
                cfg["strategy_params"] = {
                    **(cfg.get("strategy_params") or {}),
                    # only take strong setups
                    "rsi_buy": 25, "rsi_sell": 75,
                    "adx_min": 25, "adx_trend": 28, "adx_range": 18,
                }
            mode = cfg["mode"]
            is_andx = cfg["exchange"] == "andx"
            if is_andx and mode == "testnet":
                return False, "ANDX Global has no testnet — use paper mode to rehearse, then live"
            try:
                self.market = MarketData(cfg["exchange"])
                if mode == "paper":
                    # sim charges the same per-side fee the entry gate assumes,
                    # so paper results don't flatter what live would do
                    fee_side = float(cfg.get("fee_pct_per_side", 0.0)) / 100.0
                    self.broker = PaperBroker(float(cfg.get("paper_balance", 10000)),
                                              taker_fee=fee_side)
                else:
                    creds = store.load_secrets().get(cfg["exchange"])
                    if not creds or not creds.get("api_key"):
                        return False, f"no API keys saved for {cfg['exchange']} — add them in Settings"
                    if is_andx:
                        if cfg.get("derivatives", False):
                            lev = int(float(cfg.get("risk", {}).get("max_leverage", 2) or 2))
                            try:
                                self.broker = AndxMarginBroker(creds, leverage=lev)
                            except Exception as de:
                                if "derivative" in str(de).lower() or "limit group" in str(de).lower():
                                    self.log("Your ANDX account is not enabled for derivatives - running in spot mode instead.", "warn")
                                    cfg = dict(cfg); cfg["derivatives"] = False
                                    self.broker = AndxBroker(creds)
                                else:
                                    raise
                        else:
                            self.broker = AndxBroker(creds)
                    else:
                        self.broker = LiveBroker(cfg["exchange"], creds, testnet=(mode == "testnet"))
            except Exception as e:
                return False, f"startup failed: {e}"

            # Derivatives trade long AND short; spot trades long/flat.
            derivatives = cfg.get("derivatives", False)
            self.spot_only = is_andx and not derivatives
            if is_andx and derivatives:
                self.log("ANDX derivatives mode — trading long AND short with "
                         f"leverage up to {cfg.get('risk', {}).get('max_leverage', 2)}x; "
                         "stop-loss/take-profit enforced by the exchange")
            elif self.spot_only:
                self.log("ANDX spot mode: long/flat — short signals sell back to USDT")

            # Resume managing positions from the previous live run (spot only;
            # derivatives positions sync straight from the exchange).
            def _restore(rec: dict) -> Position:
                pos = Position(rec["symbol"],
                               1 if rec["side"] == "long" else -1,
                               rec["qty"], rec["entry"], rec["stop"],
                               rec.get("take_profit"), rec.get("strategy", ""))
                pos.opened_at = rec.get("opened_at", time.time())
                pos.r_value = rec.get("r_value") or abs(rec["entry"] - (rec["stop"] or rec["entry"]))
                pos.high_water = rec.get("high_water")
                return pos

            if mode == "paper":
                # The practice account persists across stops/restarts so
                # progress is trackable. Same configured starting balance ->
                # resume balance and open positions; a changed starting
                # balance (or the Reset button) starts a fresh account.
                saved = store.load_paper_state()
                cfg_bal = float(cfg.get("paper_balance", 10000))
                if saved and abs(float(saved.get("initial_balance", -1.0)) - cfg_bal) < 1e-6:
                    self.broker.balance = float(saved.get("balance", cfg_bal))
                    for rec in saved.get("positions", []):
                        self.broker.positions[rec["symbol"]] = _restore(rec)
                    self.log(f"practice account resumed at ${self.broker.balance:,.2f}"
                             + (f" with {len(self.broker.positions)} open position(s)"
                                if self.broker.positions else "")
                             + " — progress carries over (use Reset practice "
                               "account to start fresh)")
                else:
                    store.save_paper_state(self.broker.balance, cfg_bal, [])
                    if saved:
                        self.log(f"fresh practice account at ${cfg_bal:,.2f} "
                                 "(starting balance changed)")
            if mode != "paper" and not hasattr(self.broker, "sync"):
                for rec in store.load_open_positions(mode, cfg["exchange"]):
                    self.broker.positions[rec["symbol"]] = _restore(rec)
                if self.broker.positions:
                    self.log(f"restored {len(self.broker.positions)} open position(s) "
                             "from previous run")

            # In derivatives mode the margin broker cannot see SPOT positions
            # left over from a previous spot-mode run — without this guard
            # their stops would never be checked and the coins would sit
            # unmanaged (this exact orphan happened with ALGO/USDT live).
            self.spot_guard = None
            if mode != "paper" and is_andx and hasattr(self.broker, "sync"):
                leftovers = store.load_open_positions(mode, cfg["exchange"])
                if leftovers:
                    try:
                        guard = AndxBroker(creds)
                    except Exception as e:
                        return False, f"legacy spot positions exist but the spot guard failed to start: {e}"
                    for rec in leftovers:
                        guard.positions[rec["symbol"]] = _restore(rec)
                    self.spot_guard = guard
                    self.log("guarding legacy spot position(s) from a previous "
                             f"spot-mode run: {', '.join(r['symbol'] for r in leftovers)}"
                             " — stops stay enforced; the record clears when they close",
                             "warn")

            # "ALL" expands to every tradeable ANDX pair for the active market
            if is_andx and any(str(s).strip().upper() == "ALL" for s in cfg["symbols"]):
                try:
                    import andx as andx_mod
                    discovery = andx_mod.AndxClient()
                    cfg = dict(cfg)
                    cfg["symbols"] = (discovery.margin_symbols() if derivatives
                                      else discovery.spot_symbols())
                    self.log(f"trading ALL ANDX pairs — {len(cfg['symbols'])} "
                             f"instruments: {', '.join(s.split('/')[0] for s in cfg['symbols'])}")
                except Exception as e:
                    self.running = False
                    self.status = "stopped"
                    return False, f"could not list ANDX pairs: {e}"

            # Rank the universe by tradability (ATR% vs spread; flat-bar and
            # dead/wild-volatility rejects) and keep only the best pairs —
            # polling thin, flat, wide-spread instruments is how the rate
            # budget and the fee budget both die.
            if is_andx and len(cfg["symbols"]) > 4:
                try:
                    cfg = dict(cfg)
                    cfg["symbols"] = self._rank_universe(cfg["symbols"], derivatives)
                except Exception as e:
                    self.log(f"universe ranking skipped ({e}) — trading the full list", "warn")

            # Everyone starts from the same tuned strategy; students compete by
            # EDITING the code (see the Edit Strategy tab / strategies.py), not
            # by luck of a random seed. Reload the module so a student's saved
            # edits take effect on this start (no full app relaunch needed).
            try:
                importlib.reload(strategy_module)
            except Exception as e:
                self.running = False
                self.status = "stopped"
                return False, f"your strategy code has an error: {e}"
            self.strategies = {
                s: strategy_module.make_strategy(cfg["strategy"], cfg.get("strategy_params"))
                for s in cfg["symbols"]
            }
            self.volume_target_hit = False
            self.error = None
            self.trade_mode = str(cfg.get("trade_mode", "auto"))
            # a proposal minted under an old broker must never be approvable
            # into a new one; cooldowns from the old session reset too
            self._clear_proposals()
            self._prop_cooldown.clear()
            # routines missed while the engine was off wait for their NEXT
            # slot — never a surprise catch-up burst on startup
            try:
                now_ts = time.time()
                for r in store.list_routines():
                    if (r["enabled"] and r["next_run_ts"]
                            and r["next_run_ts"] <= now_ts):
                        store.reschedule_routine(
                            r["id"],
                            self.next_routine_run(r["schedule"], now_ts),
                            "missed while the engine was off — waiting for "
                            "the next slot")
            except Exception:
                pass
            # Restore today's loss baseline and breaker state: a restart must
            # NOT re-arm a tripped kill switch or re-baseline the day's losses
            # (that would hand the bot a fresh daily-loss budget every restart).
            self.kill_switch_tripped = False
            self.day_start_equity = None
            self.day_key = None
            state = store.load_day_state(mode)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if state and state.get("day_key") == today:
                self.day_key = today
                self.day_start_equity = state.get("day_start_equity")
                self.kill_switch_tripped = bool(state.get("kill_switch_tripped"))
                if self.kill_switch_tripped:
                    self.log("kill switch from earlier today is still tripped — "
                             "no new entries until the next UTC day", "warn")

            if mode != "paper":
                ok, why = self._selftest(cfg)
                if not ok:
                    self.running = False
                    self.status = "stopped"
                    return False, f"pre-flight failed: {why}"
            # retire any previous loop still finishing a tick, then start fresh
            self._gen += 1
            gen = self._gen
            old_thread = self.thread
            if old_thread and old_thread.is_alive():
                old_thread.join(timeout=10)
            self.running = True
            self.status = "running"
            self.thread = threading.Thread(target=self._loop, args=(cfg, gen), daemon=True)
            self.thread.start()
            self.log(f"engine started — mode={mode}, exchange={cfg['exchange']}, "
                     f"strategy={cfg['strategy']}, symbols={', '.join(cfg['symbols'])}")
            if cfg.get("slow_mode"):
                self.log("conservative mode ON — 1h candles, 0.5% risk/trade, "
                         "strong-signal entries only, wider trailing stops, "
                         "5% daily-loss circuit breaker, max 3 positions")
            return True, "started"

    def stop(self, flatten: bool = False):
        self.running = False
        self._gen += 1  # invalidate any loop still mid-tick
        self.status = "stopping"
        # Let an in-flight tick finish its network call before flattening —
        # a flatten racing a tick's order corrupts fill verification.
        t = self.thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=30)
            if t.is_alive():
                self.log("engine thread still mid network call — flatten "
                         "proceeds under the order lock", "warn")
        if flatten and self.broker:
            self._flatten_all("manual stop")
        self._persist_positions()  # snapshot final state (paper progress incl.)
        self._clear_proposals()
        self.status = "stopped"
        self.log("engine stopped" + (" (positions flattened)" if flatten else ""))

    def _flatten_all(self, reason: str):
        for broker in (b for b in (self.broker, self.spot_guard) if b):
            for symbol in list(broker.positions.keys()):
                try:
                    price = self.prices.get(symbol) or self.market.last_price(symbol)
                    with self._order_lock:
                        trade = broker.close(symbol, price)
                    if trade:
                        store.record_trade(trade, broker.mode, reason)
                        store.record_fill(symbol, "sell", trade["qty"], trade["exit"], broker.mode)
                        self.log(f"closed {symbol} ({reason}) pnl={trade['pnl']:+.2f}")
                except Exception as e:
                    self.log(f"failed to close {symbol}: {e}", "error")
        self._persist_positions()

    def _persist_positions(self, cfg: dict | None = None):
        if not self.broker:
            return
        if self.broker.mode == "paper":
            try:  # practice account survives stops/restarts
                store.save_paper_state(
                    self.broker.balance,
                    float((cfg or store.load_config()).get("paper_balance", 10000)),
                    [p.to_dict() for p in self.broker.positions.values()])
            except Exception:
                pass
            return
        exchange_id = (cfg or store.load_config())["exchange"]
        if hasattr(self.broker, "sync"):
            # The exchange is the source of truth for derivatives; the spot
            # positions file tracks only legacy spot-guard positions (and is
            # cleared once the guard empties out).
            if self.spot_guard is not None:
                store.save_open_positions(
                    self.broker.mode, exchange_id,
                    [p.to_dict() for p in self.spot_guard.positions.values()])
            return
        store.save_open_positions(
            self.broker.mode, exchange_id,
            [p.to_dict() for p in self.broker.positions.values()])

    def close_position(self, symbol: str) -> bool:
        for broker in (b for b in (self.broker, self.spot_guard) if b):
            if symbol not in broker.positions:
                continue
            try:
                price = self.market.last_price(symbol)
            except Exception:
                price = self.prices.get(symbol, 0)
            with self._order_lock:
                trade = broker.close(symbol, price)
            if trade:
                store.record_trade(trade, broker.mode, "manual close")
                store.record_fill(symbol, "sell", trade["qty"], trade["exit"], broker.mode)
                self.log(f"manually closed {symbol} pnl={trade['pnl']:+.2f}")
                self._persist_positions()
                return True
            return False
        return False

    # ------------------------------------------------------- manual control
    # The "user takeover" surface: the assistant (or a dashboard control)
    # opens positions, sets exits, and closes on the user's explicit word.
    # Hard rails still apply — kill switch, watched universe, leverage cap,
    # max positions, instant-trigger exit checks — but the trade itself is
    # the user's call and the strategy loop won't second-guess it.

    def _validate_exits(self, side: int, price: float,
                        stop: float | None, take_profit: float | None) -> str | None:
        if stop is not None:
            if side == 1 and stop >= price:
                return (f"a stop at {stop:.6g} is above the current price "
                        f"{price:.6g} — it would close instantly")
            if side == -1 and stop <= price:
                return (f"a stop at {stop:.6g} is below the current price "
                        f"{price:.6g} — it would close instantly")
        if take_profit is not None:
            if side == 1 and take_profit <= price:
                return (f"a take-profit at {take_profit:.6g} is below the "
                        f"current price {price:.6g} — it would close instantly")
            if side == -1 and take_profit >= price:
                return (f"a take-profit at {take_profit:.6g} is above the "
                        f"current price {price:.6g} — it would close instantly")
        return None

    def manual_open(self, symbol: str, side: int, usd_size: float,
                    stop: float | None = None,
                    take_profit: float | None = None) -> dict:
        if not self.running or not self.broker:
            return {"error": "the engine isn't running"}
        if self.kill_switch_tripped:
            return {"error": "the daily loss breaker is tripped — "
                             "no new positions until the next UTC day"}
        watched = list(self.strategies.keys()) if self.strategies else []
        if symbol not in watched or symbol not in self.prices:
            return {"error": "I only trade what I'm watching right now: "
                             + (", ".join(watched) or "nothing yet")}
        if symbol in self.broker.positions:
            return {"error": f"already holding {symbol} — "
                             "adjust its exits or close it first"}
        if side not in (1, -1):
            return {"error": "side must be long or short"}
        if side == -1 and self.spot_only:
            return {"error": "shorting isn't possible on the spot market"}
        try:
            price = self.market.last_price(symbol)
        except Exception:
            price = self.prices.get(symbol)
        if not price or price <= 0:
            return {"error": f"no live price for {symbol} yet"}
        try:
            usd_size = float(usd_size)
        except (TypeError, ValueError):
            return {"error": "size must be a dollar amount"}
        if usd_size < 10:
            return {"error": "minimum position size is $10"}
        cfg = store.load_config()
        risk_cfg = risk_mod.RiskConfig.from_dict(cfg.get("risk", {}))
        if len(self.broker.positions) >= risk_cfg.max_open_positions:
            return {"error": f"already at the max of "
                             f"{risk_cfg.max_open_positions} open positions"}
        equity = self.broker.equity(self.prices) + self._guard_value()
        notional = self.broker.open_notional(self.prices) + self._guard_value()
        lev = 1.0 if self.spot_only else max(1.0, risk_cfg.max_leverage)
        room = equity * lev - notional
        if usd_size > room:
            return {"error": f"too big — that would pass the {lev:g}x account "
                             f"cap; the most I can open right now is "
                             f"${max(0.0, room):,.0f}"}
        if self.spot_only and usd_size > self.broker.balance * 0.995:
            return {"error": f"free USDT only covers "
                             f"${self.broker.balance * 0.995:,.0f}"}
        bad = self._validate_exits(side, price, stop, take_profit)
        if bad:
            return {"error": bad}
        stop_defaulted = False
        if stop is None:
            stop = price * (1 - 0.03 * side)  # protective default: 3% adverse
            stop_defaulted = True
        qty = usd_size / price
        SLICE_MIN, SLICES = 5000.0, 3
        receipt = None
        side_word = "buy" if side == 1 else "sell"
        try:
            with self._order_lock:
                can_slice = (self.broker.mode == "paper"
                             and hasattr(self.broker, "slip_for"))
                if can_slice and usd_size >= SLICE_MIN:
                    # smart execution: work the order in slices — smaller
                    # clips pay less impact in the demo book model, so the
                    # receipt is a real saving WITHIN the sim (and says so)
                    per = usd_size / SLICES
                    single_slip = self.broker.slip_for(usd_size)
                    slice_slip = self.broker.slip_for(per)
                    pos = self.broker.open(symbol, side, per / price, price,
                                           float(stop),
                                           float(take_profit) if take_profit else None,
                                           "manual")
                    store.record_fill(symbol, side_word, pos.qty, pos.entry,
                                      self.broker.mode)
                    for _ in range(SLICES - 1):
                        fill = price * (1 + slice_slip * side)
                        q_i = per / fill
                        fee = fill * q_i * self.broker.taker_fee
                        self.broker.balance -= fee
                        new_qty = pos.qty + q_i
                        pos.entry = (pos.entry * pos.qty + fill * q_i) / new_qty
                        pos.qty = new_qty
                        store.record_fill(symbol, side_word, q_i, fill,
                                          self.broker.mode)
                    saved = usd_size * (single_slip - slice_slip)
                    receipt = {"sliced_into": SLICES,
                               "modeled_saving_usd": round(saved, 2),
                               "note": "impact saved vs a one-shot fill — "
                                       "demo book model"}
                else:
                    pos = self.broker.open(symbol, side, qty, price, float(stop),
                                           float(take_profit) if take_profit else None,
                                           "manual")
                    store.record_fill(symbol, side_word, pos.qty, pos.entry,
                                      self.broker.mode)
        except Exception as e:
            self.log(f"manual open failed for {symbol}: {e}", "error")
            return {"error": f"the order didn't go through: {e}"}
        if receipt:
            self.log(f"smart execution: {symbol} worked in {SLICES} slices — "
                     f"~${receipt['modeled_saving_usd']:,.2f} impact saved "
                     "(demo model)")
            self.notify("ANDX AI — execution receipt",
                        f"Sliced your {symbol.split('/')[0]} order into "
                        f"{SLICES} — saved ~${receipt['modeled_saving_usd']:,.2f} "
                        "of impact.")
        pos.r_value = abs(pos.entry - pos.stop) if pos.stop else 0.0
        pos.high_water = pos.entry
        self._persist_positions(cfg)
        side_txt = "LONG" if side == 1 else "SHORT"
        self.log(f"MANUAL {side_txt} {symbol} qty={pos.qty:.6g} @ "
                 f"{pos.entry:.6g} stop={pos.stop:.6g}"
                 + (f" tp={pos.take_profit:.6g}" if pos.take_profit else "")
                 + " — user takeover")
        out = pos.to_dict(price)
        out["usd_size"] = usd_size
        out["stop_defaulted"] = stop_defaulted
        if receipt:
            out["execution_receipt"] = receipt
        return {"opened": out}

    def manual_propose(self, symbol: str, side: int, usd_size: float,
                       stop: float | None = None,
                       take_profit: float | None = None) -> dict:
        """The live-account chat path: same rails as manual_open, but ends
        in a tap-to-approve proposal instead of an immediate fill — one
        human tap stands between a sentence and real money."""
        if not self.running or not self.broker:
            return {"error": "the engine isn't running"}
        if self.kill_switch_tripped:
            return {"error": "the daily loss breaker is tripped — "
                             "no new positions until the next UTC day"}
        watched = list(self.strategies.keys()) if self.strategies else []
        if symbol not in watched or symbol not in self.prices:
            return {"error": "I only trade what I'm watching right now: "
                             + (", ".join(watched) or "nothing yet")}
        if symbol in self.broker.positions:
            return {"error": f"already holding {symbol} — "
                             "adjust its exits or close it first"}
        if side not in (1, -1):
            return {"error": "side must be long or short"}
        if side == -1 and self.spot_only:
            return {"error": "shorting isn't possible on the spot market"}
        try:
            price = self.market.last_price(symbol)
        except Exception:
            price = self.prices.get(symbol)
        if not price or price <= 0:
            return {"error": f"no live price for {symbol} yet"}
        try:
            usd_size = float(usd_size)
        except (TypeError, ValueError):
            return {"error": "size must be a dollar amount"}
        if usd_size < 10:
            return {"error": "minimum position size is $10"}
        cfg = store.load_config()
        risk_cfg = risk_mod.RiskConfig.from_dict(cfg.get("risk", {}))
        if len(self.broker.positions) >= risk_cfg.max_open_positions:
            return {"error": f"already at the max of "
                             f"{risk_cfg.max_open_positions} open positions"}
        equity = self.broker.equity(self.prices) + self._guard_value()
        notional = self.broker.open_notional(self.prices) + self._guard_value()
        lev = 1.0 if self.spot_only else max(1.0, risk_cfg.max_leverage)
        room = equity * lev - notional
        if usd_size > room:
            return {"error": f"too big — that would pass the {lev:g}x account "
                             f"cap; the most I can open right now is "
                             f"${max(0.0, room):,.0f}"}
        bad = self._validate_exits(side, price, stop, take_profit)
        if bad:
            return {"error": bad}
        if stop is None:
            stop = price * (1 - 0.03 * side)
        self._create_proposal(symbol, side, usd_size / price, price,
                              float(stop),
                              float(take_profit) if take_profit else None,
                              "manual")
        pending = [p for p in self._proposals_snapshot()
                   if p["symbol"] == symbol]
        return {"proposed": pending[-1] if pending else True,
                "note": "REAL account: this is now a proposal — nothing "
                        "opens until the user taps Approve (or says "
                        "approve, which I may relay). It expires in ~3 "
                        "minutes if untouched."}

    def set_exit(self, symbol: str,
                 stop: float | None = None,
                 take_profit: float | None = None) -> dict:
        if stop is None and take_profit is None:
            return {"error": "give me a stop, a take-profit, or both"}
        for broker in (b for b in (self.broker, self.spot_guard) if b):
            pos = broker.positions.get(symbol)
            if not pos:
                continue
            price = self.prices.get(symbol) or pos.entry
            bad = self._validate_exits(pos.side, price, stop, take_profit)
            if bad:
                return {"error": bad}
            if stop is not None:
                old_stop = pos.stop
                pos.stop = float(stop)
                pos.r_value = abs(pos.entry - pos.stop)
                # the coach's raw material: every stop move is journaled as
                # loosening (more risk) or tightening — computed, not vibes
                try:
                    if old_stop and broker.mode == "paper":
                        loosened = ((pos.side == 1 and pos.stop < old_stop) or
                                    (pos.side == -1 and pos.stop > old_stop))
                        store.add_discipline_event(
                            "loosened_stop" if loosened else "tightened_stop",
                            symbol, f"{old_stop:.6g} -> {pos.stop:.6g}",
                            broker.mode)
                except Exception:
                    pass
                if hasattr(broker, "push_stop"):
                    try:
                        broker.push_stop(symbol, pos.stop)
                    except Exception as e:
                        self.log(f"manual stop push failed for {symbol}: {e}",
                                 "warn")
            if take_profit is not None:
                pos.take_profit = float(take_profit)
            # setting your own exits takes the position over: the engine
            # keeps enforcing them but stops trailing/moving them itself
            pos.strategy = "manual"
            self._persist_positions()
            self.log(f"manual exits on {symbol}: stop={pos.stop:.6g}"
                     + (f" tp={pos.take_profit:.6g}" if pos.take_profit
                        else " tp=none") + " — user set")
            return {"updated": pos.to_dict(price)}
        return {"error": f"no open position on {symbol}"}

    # ------------------------------------------------ portfolio management
    # Sphinx (or any designer) writes the spec; this engine runs it. The
    # design is never edited here — only bought to target, kept in band,
    # and guarded. Demo-account only until live portfolios are approved.

    PF_MAX_LEGS = 8
    PF_MIN_TRADE_USD = 25.0

    @staticmethod
    def _pf_targets(spec: dict) -> list[dict]:
        return [t for t in (spec.get("targets") or [])
                if str(t.get("symbol", "")).upper() not in ("USDT", "CASH")]

    def adopt_portfolio(self, portfolio_id: int) -> dict:
        if not self.running or not self.broker:
            return {"error": "the engine isn't running"}
        if self.broker.mode != "paper":
            return {"refused": "portfolio adoption runs on the demo account "
                               "for now — live portfolios come with the "
                               "production rollout"}
        if self.kill_switch_tripped:
            return {"error": "daily loss breaker is tripped — tomorrow"}
        rec = next((p for p in store.list_portfolios()
                    if p["id"] == int(portfolio_id)), None)
        if not rec:
            return {"error": "no portfolio with that id — list them first"}
        spec = rec["spec"]
        targets = self._pf_targets(spec)
        if not targets:
            return {"error": "that spec has no coin targets"}
        if len(targets) > self.PF_MAX_LEGS:
            return {"error": f"too many legs — I run up to {self.PF_MAX_LEGS}"}
        total_w = sum(float(t.get("weight", 0)) for t in (spec.get("targets") or []))
        if not 0.9 <= total_w <= 1.05:
            return {"error": f"weights sum to {total_w:.2f} — they must add "
                             "up to about 1.0"}
        watched = set(self.strategies.keys())
        equity = self.broker.equity(self.prices) + self._guard_value()
        bought, skipped = [], []
        from exchange import PAPER_SLIPPAGE
        for t in targets:
            symbol = str(t["symbol"]).upper()
            if "/" not in symbol:
                symbol += "/USDT"
            w = float(t.get("weight", 0))
            usd = equity * w
            px = self.prices.get(symbol)
            if symbol not in watched or not px:
                skipped.append({"symbol": symbol, "why": "not watched / no price"})
                continue
            if usd < 10:
                skipped.append({"symbol": symbol, "why": "leg under $10"})
                continue
            with self._order_lock:
                pos = self.broker.positions.get(symbol)
                if pos and pos.side == -1:
                    skipped.append({"symbol": symbol, "why": "short open here"})
                    continue
                if pos:  # absorb an existing long into the portfolio
                    pos.strategy = "portfolio"
                    delta = usd - pos.qty * px
                    if delta > self.PF_MIN_TRADE_USD:
                        fill = px * (1 + PAPER_SLIPPAGE)
                        q_i = delta / fill
                        self.broker.balance -= fill * q_i * self.broker.taker_fee
                        pos.entry = (pos.entry * pos.qty + fill * q_i) / (pos.qty + q_i)
                        pos.qty += q_i
                        store.record_fill(symbol, "buy", q_i, fill, self.broker.mode)
                    elif delta < -self.PF_MIN_TRADE_USD:
                        self._reduce_position(symbol, -delta / px, px, "adopt trim")
                else:
                    pos = self.broker.open(symbol, 1, usd / px, px,
                                           px * 0.85, None, "portfolio")
                    store.record_fill(symbol, "buy", pos.qty, pos.entry,
                                      self.broker.mode)
                pos.stop = max(pos.stop or 0.0, pos.entry * 0.85)
                if pos.stop >= px:
                    pos.stop = px * 0.85
                pos.r_value = abs(pos.entry - pos.stop)
                pos.high_water = pos.entry
            bought.append({"symbol": symbol, "weight": w,
                           "usd": round(usd, 2)})
        if not bought:
            return {"error": "no legs could be bought", "skipped": skipped}
        store.set_portfolio_status(rec["id"], "active", adopted=True)
        store.portfolio_rebalanced(rec["id"])
        self._persist_positions()
        self.log(f"PORTFOLIO adopted: '{rec['name']}' ({rec['source']}) — "
                 f"{len(bought)} legs, ${sum(b['usd'] for b in bought):,.0f} "
                 "deployed — I'll keep it on target")
        self.notify("ANDX AI — portfolio live",
                    f"'{rec['name']}' is built — {len(bought)} legs on the "
                    "demo account. I'll keep it balanced.")
        return {"adopted": rec["name"], "legs": bought, "skipped": skipped,
                "note": "kept on target automatically; cash weight stays as "
                        "free USDT by design"}

    def _reduce_position(self, symbol: str, qty_out: float, price: float,
                         reason: str):
        """Paper partial close: sells qty_out at market, books the P&L slice
        to the journal. Caller holds _order_lock."""
        from exchange import PAPER_SLIPPAGE
        pos = self.broker.positions.get(symbol)
        if not pos or qty_out <= 0:
            return
        qty_out = min(qty_out, pos.qty)
        fill = price * (1 - PAPER_SLIPPAGE * pos.side)
        pnl = (fill - pos.entry) * qty_out * pos.side
        fee = fill * qty_out * self.broker.taker_fee
        self.broker.balance += pnl - fee
        pos.qty -= qty_out
        store.record_trade({"symbol": symbol, "side": pos.side,
                            "qty": qty_out, "entry": pos.entry, "exit": fill,
                            "pnl": pnl - fee, "strategy": pos.strategy,
                            "opened_at": pos.opened_at},
                           self.broker.mode, reason)
        store.record_fill(symbol, "sell", qty_out, fill, self.broker.mode)
        if pos.qty * price < 5:  # dust — close it out entirely
            self.broker.positions.pop(symbol, None)

    def rebalance_portfolio(self, force: bool = False) -> dict:
        if not self.broker or self.broker.mode != "paper":
            return {"error": "portfolio management runs on the demo account"}
        rec = store.get_active_portfolio()
        if not rec:
            return {"error": "no active portfolio"}
        spec = rec["spec"]
        band = float((spec.get("rebalance") or {}).get("band_rel_pct", 20)) / 100
        min_int = float((spec.get("rebalance") or {}).get("min_interval_h", 24)) * 3600
        now = time.time()
        if not force and rec["last_rebalance_ts"] and \
                now - rec["last_rebalance_ts"] < min_int:
            return {"skipped": "inside the minimum rebalance interval"}
        equity = self.broker.equity(self.prices) + self._guard_value()
        moves, drifted = [], False
        for t in self._pf_targets(spec):
            symbol = str(t["symbol"]).upper()
            if "/" not in symbol:
                symbol += "/USDT"
            w = float(t.get("weight", 0))
            px = self.prices.get(symbol)
            if not px or w <= 0:
                continue
            pos = self.broker.positions.get(symbol)
            actual = (pos.qty * px) if pos else 0.0
            target = equity * w
            if target > 0 and abs(actual - target) / target > band:
                drifted = True
            moves.append((symbol, px, actual, target))
        if not force and not drifted:
            return {"skipped": "everything inside its band"}
        from exchange import PAPER_SLIPPAGE
        done = []
        with self._order_lock:
            # trims first so the adds have cash to work with
            for symbol, px, actual, target in moves:
                delta = target - actual
                if delta < -self.PF_MIN_TRADE_USD:
                    self._reduce_position(symbol, -delta / px, px,
                                          "rebalance trim")
                    done.append({"symbol": symbol, "action": "trim",
                                 "usd": round(-delta, 2)})
            for symbol, px, actual, target in moves:
                delta = target - actual
                if delta > self.PF_MIN_TRADE_USD:
                    pos = self.broker.positions.get(symbol)
                    fill = px * (1 + PAPER_SLIPPAGE)
                    q_i = delta / fill
                    fee = fill * q_i * self.broker.taker_fee
                    self.broker.balance -= fee
                    if pos:
                        pos.entry = (pos.entry * pos.qty + fill * q_i) / (pos.qty + q_i)
                        pos.qty += q_i
                    else:
                        pos = self.broker.open(symbol, 1, q_i, px,
                                               px * 0.85, None, "portfolio")
                        self.broker.balance += fee  # open() charged it already
                    pos.strategy = "portfolio"
                    cand = max(pos.stop or 0.0, pos.entry * 0.85)
                    pos.stop = cand if cand < px else px * 0.85
                    pos.r_value = abs(pos.entry - pos.stop)
                    store.record_fill(symbol, "buy", q_i, fill,
                                      self.broker.mode)
                    done.append({"symbol": symbol, "action": "add",
                                 "usd": round(delta, 2)})
        if not done:
            return {"skipped": "drift too small to beat the fees — holding"}
        store.portfolio_rebalanced(rec["id"])
        self._persist_positions()
        summary = ", ".join(f"{d['action']} {d['symbol'].split('/')[0]} "
                            f"${d['usd']:,.0f}" for d in done)
        self.log(f"PORTFOLIO rebalanced ('{rec['name']}'): {summary}")
        self.notify("ANDX AI — rebalanced",
                    f"'{rec['name']}' back on target: {summary}")
        return {"rebalanced": done}

    def _check_portfolio(self):
        if not self.broker or self.broker.mode != "paper":
            return
        rec = store.get_active_portfolio()
        if not rec:
            return
        if self.trade_mode == "auto":
            self.rebalance_portfolio(force=False)
        # in approve/manual the user rebalances by saying so — no nagging

    def apply_protection(self, plan: dict) -> dict:
        if not self.broker or self.broker.mode != "paper":
            return {"refused": "protection plans apply to the demo account "
                               "for now"}
        applied, skipped = [], []
        for s in (plan.get("stops") or []):
            symbol = str(s.get("symbol", "")).upper()
            if "/" not in symbol:
                symbol += "/USDT"
            try:
                r = self.set_exit(symbol, stop=float(s.get("stop")))
            except (TypeError, ValueError):
                r = {"error": "bad stop"}
            (applied if "updated" in r else skipped).append(
                {"symbol": symbol, **({"stop": s.get("stop")} if "updated" in r
                                      else {"why": r.get("error", "failed")})})
        for a in (plan.get("alerts") or []):
            symbol = str(a.get("symbol", "")).upper()
            if "/" not in symbol:
                symbol += "/USDT"
            direction = str(a.get("direction", "")).lower()
            try:
                price = float(a.get("price"))
            except (TypeError, ValueError):
                skipped.append({"symbol": symbol, "why": "bad alert price"})
                continue
            if direction in ("above", "below") and symbol in self.strategies:
                store.add_alert(symbol, direction, price, "sphinx protection")
                applied.append({"symbol": symbol, "alert": f"{direction} {price}"})
            else:
                skipped.append({"symbol": symbol, "why": "not watched or bad direction"})
        if applied:
            self.log(f"protection plan applied: {len(applied)} item(s)")
        return {"applied": applied, "skipped": skipped,
                "note": ("drawdown auto-flatten rules arrive with guardian "
                         "rules — noted but not enforced yet"
                         if plan.get("flatten_if") else "")}

    def _portfolio_snapshot(self) -> dict | None:
        try:
            rec = store.get_active_portfolio()
        except Exception:
            return None
        if not rec or not self.broker:
            return None
        spec = rec["spec"]
        equity = None
        try:
            equity = self.broker.equity(self.prices) + self._guard_value()
        except Exception:
            return None
        rows = []
        # cash = what ISN'T deployed (the paper ledger only moves on fees,
        # so broker.balance would lie here — compute from notional instead)
        deployed = sum(p.qty * self.prices.get(s, p.entry)
                       for s, p in list(self.broker.positions.items()))
        for t in (spec.get("targets") or []):
            symbol = str(t.get("symbol", "")).upper()
            w = float(t.get("weight", 0))
            if symbol in ("USDT", "CASH"):
                actual = max(0.0, equity - deployed)
            else:
                if "/" not in symbol:
                    symbol += "/USDT"
                pos = self.broker.positions.get(symbol)
                px = self.prices.get(symbol)
                actual = (pos.qty * px) if (pos and px) else 0.0
            rows.append({"symbol": symbol, "target_w": w,
                         "actual_w": round(actual / equity, 4) if equity else 0,
                         "actual_usd": round(actual, 2)})
        return {"id": rec["id"], "name": rec["name"], "source": rec["source"],
                "targets": rows, "last_rebalance_ts": rec["last_rebalance_ts"],
                "chart_embed": spec.get("chart_embed"),
                "band_rel_pct": (spec.get("rebalance") or {}).get("band_rel_pct", 20)}

    # ---------------------------------------------------------------- loop

    def _loop(self, cfg: dict, gen: int):
        poll = max(15, int(cfg.get("poll_seconds", 60)))
        # ~2 API calls per symbol per tick; stay well under ANDX's 60/min
        min_poll = int(len(cfg["symbols"]) * 3)
        if min_poll > poll:
            poll = min_poll
            self.log(f"polling every {poll}s to respect the API rate limit "
                     f"across {len(cfg['symbols'])} pairs")
        risk_cfg = risk_mod.RiskConfig.from_dict(cfg.get("risk", {}))
        if self.spot_only:
            risk_cfg.max_leverage = min(risk_cfg.max_leverage, 1.0)
        while self.running and self._gen == gen:
            try:
                self._tick(cfg, risk_cfg, gen)
                self.error = None
            except Exception as e:
                self.error = str(e)
                self.log(f"tick error: {type(e).__name__}: {e}", "error")
            if self._gen != gen:
                break  # a newer loop owns the engine now
            self._persist_positions(cfg)
            for _ in range(poll):
                if not self.running or self._gen != gen:
                    break
                time.sleep(1)

    def _tick(self, cfg: dict, risk_cfg: risk_mod.RiskConfig, gen: int | None = None):
        # -- reconcile derivatives positions with the exchange first:
        #    SL/TP the exchange executed since last tick become closed trades
        if hasattr(self.broker, "sync"):
            try:
                for ev in self.broker.sync():
                    reason = ev.pop("reason", "exchange close")
                    store.record_trade(ev, self.broker.mode, reason)
                    store.record_fill(ev["symbol"], "sell" if ev["side"] == 1 else "buy",
                                      ev["qty"], ev["exit"], self.broker.mode)
                    self.log(f"exchange closed {ev['symbol']} ({reason}) "
                             f"pnl={ev['pnl']:+.2f}")
            except Exception as e:
                self.log(f"position sync failed: {e}", "warn")

        # -- refresh prices, balance & equity. Positioned symbols are ALWAYS
        #    refreshed (never cooled down): a stop check running on a
        #    30-minute-stale price is no stop at all.
        watch = list(cfg["symbols"])
        for broker in (self.broker, self.spot_guard):
            if broker:
                watch += [s for s in broker.positions if s not in watch]
        for symbol in watch:
            has_pos = any(b and symbol in b.positions
                          for b in (self.broker, self.spot_guard))
            if not has_pos and time.time() - self.unavailable.get(symbol, 0) < 1800:
                continue  # no data / disabled — quiet until cooldown ends
            try:
                self.prices[symbol] = self.market.last_price(symbol)
            except Exception as e:
                if has_pos:
                    self.log(f"price refresh failed for {symbol} — stop checks "
                             f"use the last known price ({str(e)[:60]})", "warn")
                else:
                    self.unavailable[symbol] = time.time()
                    self.log(f"no market data for {symbol} — pausing it for "
                             f"30 minutes ({str(e)[:80]})", "warn")
        # -- price alerts: user-set levels checked against this tick's fresh
        #    prices. Own try/except — an alert bug must never abort the tick
        #    before the stop checks below run.
        try:
            self._check_alerts()
        except Exception as e:
            self.log(f"alert check failed: {e}", "warn")

        # -- scheduled routines (DCA buys, briefing pings, flatten, reminders)
        try:
            self._check_routines(gen)
        except Exception as e:
            self.log(f"routine check failed: {e}", "warn")

        # -- adopted portfolio: keep it on the designer's targets
        try:
            self._check_portfolio()
        except Exception as e:
            self.log(f"portfolio check failed: {e}", "warn")

        if hasattr(self.broker, "_sync_balance"):
            try:  # keeps equity honest across settlement lag & external transfers
                self.broker._sync_balance()
            except Exception:
                pass
        equity = self.broker.equity(self.prices) + self._guard_value()

        # -- daily kill switch (baseline + tripped state persist across restarts)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day_state_dirty = False
        if self.day_key != today:
            self.day_key = today
            self.day_start_equity = equity
            day_state_dirty = True
            if self.kill_switch_tripped:
                self.kill_switch_tripped = False
                self.log("new UTC day — kill switch reset, trading resumed")
        if not self.kill_switch_tripped and risk_mod.daily_loss_breached(
            equity, self.day_start_equity, risk_cfg
        ):
            self.kill_switch_tripped = True
            day_state_dirty = True
            self.log(
                f"KILL SWITCH: daily loss exceeded {risk_cfg.daily_loss_limit_pct}% — "
                "flattening all positions and pausing until next UTC day", "error")
            self._flatten_all("kill switch")
            self._clear_proposals()
            self.notify("ANDX AI — daily loss breaker",
                        "I hit today's loss limit, flattened everything, and "
                        "stopped opening anything new until tomorrow.")
        if day_state_dirty:
            try:
                store.save_day_state(self.broker.mode, self.day_key,
                                     self.day_start_equity, self.kill_switch_tripped)
            except Exception:
                pass
        store.record_equity(equity, self.broker.balance, self.broker.mode)

        # -- refresh risk governors (stop-out cooldowns, loss streak, expectancy)
        self._refresh_governors()

        # -- legacy spot positions under guard: enforce their stops
        if self.spot_guard is not None:
            self._guard_tick(cfg, risk_cfg)

        # -- per-symbol logic
        for symbol in cfg["symbols"]:
            if gen is not None and self._gen != gen:
                return  # engine restarted mid-tick — abort immediately
            if symbol not in self.prices:
                continue
            if (symbol not in self.broker.positions
                    and time.time() - self.unavailable.get(symbol, 0) < 1800):
                continue  # cooling down (no data / pair disabled)
            price = self.prices[symbol]
            pos = self.broker.positions.get(symbol)

            # stop-loss / take-profit checks on every tick. Every close is
            # wrapped: one symbol's exchange error must never abort the tick
            # and starve the remaining symbols' stop checks.
            if pos:
                if pos.stop and risk_mod.stop_hit(pos.side, price, pos.stop):
                    try:
                        with self._order_lock:
                            trade = self.broker.close(symbol, price)
                    except Exception as e:
                        self.log(f"stop close retry for {symbol}: {e}", "warn")
                        continue
                    if trade:
                        store.record_trade(trade, self.broker.mode, "stop loss")
                        store.record_fill(symbol, "sell", trade["qty"], trade["exit"], self.broker.mode)
                        self.log(f"stop hit on {symbol} pnl={trade['pnl']:+.2f}", "warn")
                        self.notify("ANDX AI — stop hit",
                                    f"{symbol.split('/')[0]} closed, "
                                    f"{trade['pnl']:+,.2f}")
                        self._persist_positions(cfg)
                    pos = None
                elif pos.take_profit and risk_mod.stop_hit(-pos.side, price, pos.take_profit):
                    try:
                        with self._order_lock:
                            trade = self.broker.close(symbol, price)
                    except Exception as e:
                        self.log(f"take-profit close retry for {symbol}: {e}", "warn")
                        continue
                    if trade:
                        store.record_trade(trade, self.broker.mode, "take profit")
                        store.record_fill(symbol, "sell", trade["qty"], trade["exit"], self.broker.mode)
                        self.log(f"take-profit on {symbol} pnl={trade['pnl']:+.2f}")
                        self.notify("ANDX AI — take-profit",
                                    f"{symbol.split('/')[0]} banked "
                                    f"{trade['pnl']:+,.2f}")
                        self._persist_positions(cfg)
                    pos = None

            # candles + signal
            try:
                raw = self.market.ohlcv(symbol, cfg["timeframe"], limit=300)
            except Exception as e:
                if symbol not in self.broker.positions:
                    self.unavailable[symbol] = time.time()
                    self.log(f"no candles for {symbol} — pausing it for "
                             f"30 minutes ({str(e)[:80]})", "warn")
                else:
                    self.log(f"candle fetch failed for {symbol}: {e}", "warn")
                continue
            df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
            if len(df) < 60:
                self.log(f"not enough history for {symbol}", "warn")
                continue
            closed = df.iloc[:-1]  # drop the still-forming candle
            strategy = self.strategies.get(symbol)
            if strategy is None:
                continue  # symbol not part of this engine run
            # An unseeded 55-EMA/ADX on short history produces confident
            # nonsense — new entries wait for the strategy's stated warm-up.
            if symbol not in self.broker.positions and len(closed) < strategy.min_bars():
                self.last_signals[symbol] = f"warming up ({len(closed)}/{strategy.min_bars()} bars)"
                continue
            current = pos.side if pos else 0
            desired = strategy.signal(closed, current)
            self.last_signals[symbol] = {1: "long", -1: "short", 0: "flat"}[desired]
            if self.spot_only and desired == -1:
                desired = 0  # spot venue: a short signal means "be in USDT"
                self.last_signals[symbol] = "short → flat (spot)"
            atr_val = float(atr(closed, 14).iloc[-1])
            if atr_val < price * 0.0005:
                # Flat/stale candles (thin instrument): ATR~0 would put the
                # stop at the entry price and churn fees on instant stop-outs.
                self.last_signals[symbol] = "skipped (no volatility)"
                continue

            # positions adopted from the exchange can arrive stopless — give
            # them an ATR stop now rather than trading naked
            if pos and not pos.stop:
                rescue = risk_mod.stop_price(pos.side, pos.entry, atr_val, risk_cfg)
                if hasattr(self.broker, "push_stop"):
                    try:
                        self.broker.push_stop(symbol, rescue)
                    except Exception as e:
                        self.log(f"could not push rescue stop for {symbol}: {e}", "warn")
                        pos.stop = rescue  # engine-side enforcement as fallback
                else:
                    pos.stop = rescue
                pos.r_value = abs(pos.entry - rescue)
                self.log(f"assigned missing stop for adopted {symbol} @ {rescue:.6g}", "warn")
            if pos and pos.stop and not getattr(pos, "r_value", 0.0):
                pos.r_value = abs(pos.entry - pos.stop)

            # user-commanded (manual) and portfolio legs: the stop/TP checks
            # above still protect them, but the strategy loop never
            # signal-exits them, never moves their levels, and never stacks
            # its own trade on the same symbol
            _tag = getattr(pos, "strategy", "") if pos else ""
            if pos and _tag in ("manual", "portfolio"):
                self.last_signals[symbol] = (
                    "portfolio leg (managed to target)" if _tag == "portfolio"
                    else ("manual long (yours)" if pos.side == 1
                          else "manual short (yours)"))
                continue

            # smart exits: break-even (plus fees) after +1R for every
            # position; ATR trail + chandelier ratchet for trend positions
            # (pushed to the exchange in derivatives mode)
            if pos and desired == pos.side:
                fee_rt = 0.02 if self.spot_only else 0.005
                new_stop, hw = risk_mod.manage_stop(
                    pos.side, pos.entry, pos.stop, getattr(pos, "r_value", 0.0),
                    getattr(pos, "high_water", None), float(closed["close"].iloc[-1]),
                    atr_val, risk_cfg, fee_roundtrip=fee_rt,
                    trailing=bool(strategy.trailing))
                pos.high_water = hw
                if new_stop and new_stop != pos.stop:
                    if hasattr(self.broker, "push_stop"):
                        try:
                            self.broker.push_stop(symbol, new_stop)
                        except Exception as e:
                            self.log(f"stop update failed for {symbol}: {e}", "warn")
                    else:
                        pos.stop = new_stop

            if desired == current:
                continue

            # close what no longer matches
            if pos:
                try:
                    with self._order_lock:
                        trade = self.broker.close(symbol, price)
                except Exception as e:
                    self.log(f"signal-exit close retry for {symbol}: {e}", "warn")
                    continue
                if trade:
                    store.record_trade(trade, self.broker.mode, "signal exit")
                    store.record_fill(symbol, "sell", trade["qty"], trade["exit"], self.broker.mode)
                    self.log(f"signal exit {symbol} pnl={trade['pnl']:+.2f}")
                    self._persist_positions(cfg)

            # open the new side
            if desired != 0 and time.time() - self.unavailable.get(symbol, 0) < 1800:
                continue  # pair reported "unavailable for trading" — cooling down
            if desired != 0 and not self.kill_switch_tripped:
                if len(self.broker.positions) >= risk_cfg.max_open_positions:
                    self.log(f"skip {symbol}: max open positions reached", "warn")
                    continue
                gate = self._entry_gate(symbol, desired, price, atr_val, cfg, risk_cfg)
                if gate:
                    self.last_signals[symbol] = f"skipped ({gate})"
                    continue
                # the user's trading mode: auto opens, approve proposes,
                # manual holds fire entirely (exits stay automatic above)
                if self.trade_mode == "manual":
                    self.last_signals[symbol] = (
                        f"setup found ({'long' if desired == 1 else 'short'}) "
                        "— manual mode, holding fire")
                    continue
                if self.trade_mode == "approve":
                    if time.time() < self._prop_cooldown.get(symbol, 0):
                        self.last_signals[symbol] = ("setup live — holding off "
                                                     "(you passed on it recently)")
                        continue
                    if self._has_proposal(symbol):
                        self.last_signals[symbol] = "proposal pending — your call"
                        continue
                equity = self.broker.equity(self.prices) + self._guard_value()
                notional = self.broker.open_notional(self.prices) + self._guard_value()
                qty, reason = risk_mod.position_size(
                    equity, price, atr_val, risk_cfg, notional,
                    risk_mult=self._risk_multiplier(symbol))
                if qty > 0 and self.spot_only:
                    # spot buys are limited by actual free USDT (0.5% fee buffer)
                    qty = min(qty, self.broker.balance * 0.995 / price)
                    if qty * price < 10:
                        qty, reason = 0.0, "free USDT below exchange minimum"
                if qty <= 0:
                    self.log(f"skip {symbol}: {reason}", "warn")
                    continue
                stop = risk_mod.stop_price(desired, price, atr_val, risk_cfg)
                tp = risk_mod.take_profit_price(desired, price, atr_val, risk_cfg)
                if self.trade_mode == "approve":
                    self._create_proposal(symbol, desired, qty, price, stop,
                                          tp, strategy.name)
                    continue
                try:
                    with self._order_lock:
                        new_pos = self.broker.open(
                            symbol, desired, qty, price, stop, tp, strategy.name)
                    store.record_fill(symbol, "buy" if desired == 1 else "sell",
                                      new_pos.qty, new_pos.entry, self.broker.mode)
                    # anchor risk to the ACTUAL fill, not the pre-trade mid:
                    # slippage on a thin book can move the entry enough that
                    # the planned stop no longer risks what was budgeted
                    true_stop = risk_mod.stop_price(desired, new_pos.entry, atr_val, risk_cfg)
                    if abs(true_stop - stop) > price * 0.0005:
                        if hasattr(self.broker, "push_stop"):
                            try:
                                self.broker.push_stop(symbol, true_stop)
                            except Exception as e:
                                self.log(f"fill-anchored stop update failed for {symbol}: {e}", "warn")
                        else:
                            new_pos.stop = true_stop
                    new_pos.r_value = abs(new_pos.entry - (new_pos.stop or true_stop))
                    new_pos.high_water = new_pos.entry
                    self._persist_positions(cfg)
                    side_txt = "LONG" if desired == 1 else "SHORT"
                    self.log(f"opened {side_txt} {symbol} qty={new_pos.qty:.6g} "
                             f"@ {new_pos.entry:.6g} stop={new_pos.stop:.6g}")
                    self.notify("ANDX AI — position opened",
                                f"{side_txt} {symbol.split('/')[0]} "
                                f"@ {new_pos.entry:.6g}, stop {new_pos.stop:.6g}")
                except Exception as e:
                    msg = str(e)
                    self.log(f"order failed for {symbol}: {msg}", "error")
                    if "disabled" in msg.lower() or "unavailable" in msg.lower():
                        self.unavailable[symbol] = time.time()
                        self.log(f"{symbol} is unavailable for trading — "
                                 "pausing attempts for 30 minutes", "warn")

        self._turnover(cfg)

    def _turnover(self, cfg: dict):
        """Volume-target mode: while cumulative traded volume is below the
        configured target, periodically rotate the oldest position (sell it;
        the freed USDT redeploys on the next tick's signals). Stops by itself
        once the target is reached."""
        target = float(cfg.get("volume_target_usd") or 0)
        if (target <= 0 or not self.spot_only or self.kill_switch_tripped
                or self.trade_mode != "auto"):
            return  # rotation without redeploy is pure liquidation + fees
        since = float(cfg.get("volume_target_since") or 0)
        done = store.cumulative_volume(self.broker.mode, since)
        if done >= target:
            if not self.volume_target_hit:
                self.volume_target_hit = True
                self.log(f"volume target reached: ${done:,.0f} of ${target:,.0f} "
                         "— turnover rotation off")
            return
        rotate_secs = max(5, float(cfg.get("rotate_minutes") or 15)) * 60
        candidates = [
            (s, p) for s, p in self.broker.positions.items()
            if time.time() - p.opened_at > rotate_secs
            and self.last_signals.get(s) == "long"  # re-entry is likely
        ]
        if not candidates:
            ages = {s.split("/")[0]: int((time.time() - p.opened_at) / 60)
                    for s, p in self.broker.positions.items()}
            self.log(f"turnover: waiting — volume ${done:,.0f}/${target:,.0f}, "
                     f"position ages(min)={ages}, rotate at {rotate_secs/60:.0f}m")
            return
        symbol, pos = min(candidates, key=lambda kv: kv[1].opened_at)
        price = self.prices.get(symbol)
        if not price:
            return
        try:
            trade = self.broker.close(symbol, price)
            if trade:
                store.record_trade(trade, self.broker.mode, "turnover")
                store.record_fill(symbol, "sell", trade["qty"], trade["exit"], self.broker.mode)
                done += trade["qty"] * trade["exit"]
                self.log(f"turnover: rotated {symbol} pnl={trade['pnl']:+.2f} "
                         f"(volume ${done:,.0f} / ${target:,.0f})")
        except Exception as e:
            self.log(f"turnover close failed for {symbol}: {e}", "warn")

    # -------------------------------------------------- alerts & proposals

    def _clear_proposals(self):
        """Drop all pending proposals AND scrub their stale 'proposal
        pending' signal labels — a label promising a decision that no
        longer exists is a lie the UI and the brain would keep serving."""
        with self._prop_lock:
            self.proposals.clear()
        for s, v in list(self.last_signals.items()):
            if isinstance(v, str) and v.startswith("proposal pending"):
                self.last_signals[s] = "flat"

    def _check_alerts(self):
        """Fire user-set price alerts against this tick's fresh prices."""
        for a in store.list_alerts():
            px = self.prices.get(a["symbol"])
            if px is None:
                continue
            hit = (px >= a["price"]) if a["direction"] == "above" else (px <= a["price"])
            if hit:
                store.trigger_alert(a["id"])
                msg = (f"{a['symbol'].split('/')[0]} is {a['direction']} "
                       f"{a['price']:.6g} (now {px:.6g})")
                self.log(f"ALERT: {msg}")
                self.notify("ANDX AI — price alert",
                            msg + (f" — {a['note']}" if a.get("note") else ""))

    # ------------------------------------------------------------ routines
    # The user's clock: recurring actions the engine runs on schedule.
    # Kinds: dca (buy $X of a coin), briefing (Mac ping to come read the
    # day), flatten (close all practice positions), reminder (custom ping).
    # All execution is PAPER-ONLY — routines never touch a real broker.

    @staticmethod
    def next_routine_run(schedule: dict, after_ts: float) -> float:
        """Next occurrence in LOCAL time. Schedules:
        {"every":"hours","n":4} | {"every":"day","at":"08:00"} |
        {"every":"week","weekday":0,"at":"09:00"}  (weekday 0 = Monday)"""
        every = str(schedule.get("every", "day"))
        if every == "hours":
            return after_ts + max(1, int(schedule.get("n", 4))) * 3600
        try:
            hh, mm = (int(x) for x in str(schedule.get("at", "09:00")).split(":"))
        except ValueError:
            hh, mm = 9, 0
        cand = datetime.fromtimestamp(after_ts).replace(
            hour=max(0, min(23, hh)), minute=max(0, min(59, mm)),
            second=0, microsecond=0)
        if every == "week":
            target = int(schedule.get("weekday", 0)) % 7
            cand += timedelta(days=(target - cand.weekday()) % 7)
            if cand.timestamp() <= after_ts:
                cand += timedelta(days=7)
        else:
            if cand.timestamp() <= after_ts:
                cand += timedelta(days=1)
        return cand.timestamp()

    def _check_routines(self, gen: int | None = None):
        now = time.time()
        for r in store.list_routines():
            if not r["enabled"] or not r["next_run_ts"] or r["next_run_ts"] > now:
                continue
            if gen is not None and self._gen != gen:
                return  # engine restarted — the new loop owns the schedule
            if now - self._routine_fired.get(r["id"], 0) < 55:
                continue  # in-memory guard: never double-fire inside a minute
            self._routine_fired[r["id"]] = now
            try:
                result = self._run_routine(r)
            except Exception as e:
                result = f"failed: {str(e)[:120]}"
                self.log(f"routine #{r['id']} ({r['kind']}) failed: {e}", "warn")
            # the schedule advance MUST be as guarded as the run — if it
            # can't be persisted, pause the routine rather than letting it
            # machine-gun every tick
            try:
                nxt = self.next_routine_run(r["schedule"], now)
                store.routine_ran(r["id"], nxt, result)
            except Exception as e:
                self.log(f"routine #{r['id']} bookkeeping failed ({e}) — "
                         "pausing it for safety", "warn")
                try:
                    store.set_routine_enabled(r["id"], False)
                except Exception:
                    pass

    def _run_routine(self, r: dict) -> str:
        kind, params = r["kind"], r["params"]
        if kind == "reminder":
            text = str(params.get("text", "you asked me to remind you"))[:160]
            self.log(f"routine reminder: {text}")
            self.notify("ANDX AI — reminder", text)
            return "reminded"
        if kind == "briefing":
            self.notify("ANDX AI — briefing time",
                        "Your briefing is ready — open me and I'll walk you "
                        "through the day.")
            self.log("routine: briefing ping sent")
            return "pinged"
        # trading kinds below are hard-gated to the practice account
        if not self.broker or self.broker.mode != "paper":
            return "skipped — practice account only"
        if kind == "flatten":
            n = len(self.broker.positions)
            if not n:
                return "nothing open"
            self._flatten_all("routine flatten")
            # "be flat at 21:00" includes pending proposals — a 20:59
            # proposal must not reopen exposure at 21:01
            self._clear_proposals()
            self.notify("ANDX AI — routine",
                        f"Scheduled flatten done — closed {n} practice "
                        f"position{'s' if n != 1 else ''}.")
            return f"flattened {n}"
        if kind == "dca":
            return self._run_dca(params)
        return f"unknown routine kind: {kind}"

    def _run_dca(self, params: dict) -> str:
        from exchange import PAPER_SLIPPAGE
        symbol = str(params.get("symbol", ""))
        usd = float(params.get("usd", 0) or 0)
        if usd < 10:
            return "skipped — size under $10"
        if self.kill_switch_tripped:
            self.log(f"DCA {symbol} skipped — daily breaker tripped", "warn")
            return "skipped — daily breaker tripped"
        if symbol not in self.strategies:
            # universe re-ranked the coin out: its stop would never be
            # checked and its price would go stale — refuse to accumulate
            return ("skipped — I no longer watch this coin (the universe "
                    "changed); delete or re-create the routine")
        px = self.prices.get(symbol)
        if not px:
            return "skipped — no live price"
        # the SAME rails every other entry path honors — DCA is not exempt
        cfg = store.load_config()
        risk_cfg = risk_mod.RiskConfig.from_dict(cfg.get("risk", {}))
        equity = self.broker.equity(self.prices) + self._guard_value()
        notional = self.broker.open_notional(self.prices) + self._guard_value()
        lev = 1.0 if self.spot_only else max(1.0, risk_cfg.max_leverage)
        room = equity * lev - notional
        if usd > room:
            return (f"skipped — the {lev:g}x account cap is full "
                    f"(${max(0.0, room):,.0f} of room left)")
        if self.spot_only and usd > self.broker.balance * 0.995:
            return "skipped — not enough free USDT"
        fill = px * (1 + PAPER_SLIPPAGE)
        qty = usd / fill
        with self._order_lock:
            # position read INSIDE the lock: a concurrent manual/approved
            # open on this symbol must never be silently overwritten
            pos = self.broker.positions.get(symbol)
            if pos and pos.side == -1:
                return "skipped — a short is open on this coin"
            if (pos is None
                    and len(self.broker.positions) >= risk_cfg.max_open_positions):
                return (f"skipped — already at the max of "
                        f"{risk_cfg.max_open_positions} open positions")
            if pos:
                # average into the existing long, honest fee charged
                fee = fill * qty * self.broker.taker_fee
                self.broker.balance -= fee
                new_qty = pos.qty + qty
                pos.entry = (pos.entry * pos.qty + fill * qty) / new_qty
                pos.qty = new_qty
                pos.strategy = "manual"  # user-owned: engine won't signal-exit
                # wide 15% accumulation stop — never LOOSEN a tighter user
                # stop, and never raise a stop ABOVE the live price (that
                # would manufacture an instant full stop-out)
                cand = max(pos.stop or 0.0, pos.entry * 0.85)
                if cand < px:
                    pos.stop = cand
                pos.r_value = abs(pos.entry - pos.stop) if pos.stop else 0.0
                avg = pos.entry
            else:
                pos = self.broker.open(symbol, 1, qty, px,
                                       px * 0.85, None, "manual")
                pos.r_value = abs(pos.entry - pos.stop)
                pos.high_water = pos.entry
                avg = pos.entry
        store.record_fill(symbol, "buy", qty, fill, self.broker.mode)
        coin = symbol.split("/")[0]
        self.log(f"DCA: bought ${usd:,.0f} of {coin} @ {fill:.6g} "
                 f"(position {pos.qty:.6g} @ avg {avg:.6g})")
        self.notify("ANDX AI — DCA buy",
                    f"Bought ${usd:,.0f} of {coin} @ {fill:.6g} as scheduled.")
        return f"bought ${usd:,.0f} @ {fill:.6g}"

    def _has_proposal(self, symbol: str) -> bool:
        """Prune expired proposals, report whether one is live for symbol."""
        now = time.time()
        with self._prop_lock:
            expired = [pid for pid, p in self.proposals.items()
                       if p["expires"] <= now]
            for pid in expired:
                p = self.proposals.pop(pid)
                # unanswered — don't nag: no requote for 10 minutes
                self._prop_cooldown[p["symbol"]] = now + 600
                self.log(f"proposal #{pid} for {p['symbol']} expired "
                         "unanswered — I'll requote in ~10 minutes if the "
                         "setup still holds")
            return any(p["symbol"] == symbol for p in self.proposals.values())

    def _create_proposal(self, symbol, side, qty, price, stop, tp, strat_name):
        with self._prop_lock:
            self._prop_seq += 1
            pid = self._prop_seq
            self.proposals[pid] = {
                "id": pid, "symbol": symbol,
                "side": "long" if side == 1 else "short",
                "qty": qty, "price": price, "stop": stop, "take_profit": tp,
                "strategy": strat_name, "notional": qty * price,
                "created": time.time(), "expires": time.time() + 180,
            }
        self.last_signals[symbol] = "proposal pending — your call"
        side_txt = "LONG" if side == 1 else "SHORT"
        self.log(f"PROPOSAL #{pid}: {side_txt} {symbol} ${qty * price:,.0f} "
                 f"@ {price:.6g} stop={stop:.6g}"
                 + (f" tp={tp:.6g}" if tp else "")
                 + " — waiting for your approval")
        self.notify("ANDX AI — trade for your approval",
                    f"{side_txt} {symbol.split('/')[0]} ${qty * price:,.0f} "
                    "— open the app to approve")

    def approve_proposal(self, pid) -> dict:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return {"error": "bad proposal id"}
        # atomic claim: pop under the leaf lock so two approvals (dashboard +
        # chat on separate threads) can never both open the position
        with self._prop_lock:
            p = self.proposals.pop(pid, None)
        if not p:
            return {"error": "that proposal is gone — approved, declined, or expired"}
        if p["expires"] <= time.time():
            self.log(f"proposal #{pid} expired before approval")
            return {"error": "too late — that quote went stale and the proposal expired"}
        if not self.running or not self.broker:
            return {"error": "the engine isn't running"}
        gen0 = self._gen  # re-checked under the order lock: a stop()/restart
        # during our price fetch must void the order, not orphan a position
        if self.kill_switch_tripped:
            return {"error": "the daily loss breaker tripped — nothing new opens today"}
        symbol = p["symbol"]
        if symbol in self.broker.positions:
            return {"error": f"already holding {symbol}"}
        cfg = store.load_config()
        risk_cfg = risk_mod.RiskConfig.from_dict(cfg.get("risk", {}))
        if len(self.broker.positions) >= risk_cfg.max_open_positions:
            return {"error": f"already at the max of "
                             f"{risk_cfg.max_open_positions} open positions"}
        try:
            price = self.market.last_price(symbol)  # outside any lock
        except Exception:
            price = self.prices.get(symbol)
        if not price:
            return {"error": f"no live price for {symbol}"}
        side = 1 if p["side"] == "long" else -1
        moved = (price / p["price"] - 1) * 100
        if abs(price - p["price"]) / p["price"] > 0.01:
            self.log(f"proposal #{pid} cancelled — {symbol} moved "
                     f"{moved:+.2f}% since I proposed it", "warn")
            return {"error": f"the price moved {moved:+.2f}% since I proposed "
                             "it — cancelled; I'll re-propose if the setup holds"}
        if p["stop"] and risk_mod.stop_hit(side, price, p["stop"]):
            self.log(f"proposal #{pid} cancelled — {symbol} already through "
                     f"the planned stop", "warn")
            return {"error": "the price already crossed the planned stop — "
                             "cancelled; that trade no longer makes sense"}
        try:
            with self._order_lock:
                if not self.running or self._gen != gen0:
                    self.log(f"proposal #{pid} voided — the engine stopped "
                             "or restarted while I was filling", "warn")
                    return {"error": "the engine stopped while I was filling "
                                     "— order cancelled, nothing opened"}
                new_pos = self.broker.open(symbol, side, p["qty"], price,
                                           p["stop"], p["take_profit"],
                                           p["strategy"])
            store.record_fill(symbol, "buy" if side == 1 else "sell",
                              new_pos.qty, new_pos.entry, self.broker.mode)
            new_pos.r_value = (abs(new_pos.entry - new_pos.stop)
                               if new_pos.stop else 0.0)
            new_pos.high_water = new_pos.entry
            self._persist_positions(cfg)
            side_txt = "LONG" if side == 1 else "SHORT"
            self.log(f"APPROVED #{pid}: opened {side_txt} {symbol} "
                     f"qty={new_pos.qty:.6g} @ {new_pos.entry:.6g} "
                     f"stop={new_pos.stop:.6g}")
            self.notify("ANDX AI — order filled",
                        f"{side_txt} {symbol.split('/')[0]} @ {new_pos.entry:.6g}")
            return {"opened": new_pos.to_dict(price)}
        except Exception as e:
            self.log(f"approved order failed for {symbol}: {e}", "error")
            return {"error": f"the order didn't go through: {e}"}

    def decline_proposal(self, pid) -> dict:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return {"error": "bad proposal id"}
        with self._prop_lock:
            p = self.proposals.pop(pid, None)
        if not p:
            return {"error": "that proposal is gone already"}
        # a decline means NO — leave the symbol alone for half an hour
        self._prop_cooldown[p["symbol"]] = time.time() + 1800
        if str(self.last_signals.get(p["symbol"], "")).startswith("proposal pending"):
            self.last_signals[p["symbol"]] = "flat"
        self.log(f"proposal #{pid} ({p['side']} {p['symbol']}) declined — "
                 "no order placed; I'll leave it alone for 30 minutes")
        return {"declined": True, "symbol": p["symbol"]}

    def set_trade_mode(self, mode: str) -> dict:
        mode = str(mode).strip().lower()
        if mode not in ("auto", "approve", "manual"):
            return {"error": "trade_mode must be auto, approve, or manual"}
        self.trade_mode = mode
        cfg = store.load_config()
        cfg["trade_mode"] = mode
        store.save_config(cfg)
        if mode != "approve":
            self._clear_proposals()
        label = {"auto": "AUTO — I trade by my rules",
                 "approve": "APPROVE — I propose, you decide",
                 "manual": "MANUAL — only trades you order"}[mode]
        self.log(f"trading mode set: {label}")
        return {"trade_mode": mode}

    def _proposals_snapshot(self) -> list[dict]:
        now = time.time()
        with self._prop_lock:
            return [dict(p) for p in self.proposals.values()
                    if p["expires"] > now]

    def _routines_snapshot(self) -> list[dict]:
        try:
            routines = store.list_routines()
        except Exception:
            return []
        for r in routines:
            # honest flag: enabled AND the engine is on to run it
            r["active"] = bool(self.running and r["enabled"])
        return routines

    def _alerts_snapshot(self) -> list[dict]:
        try:
            alerts = store.list_alerts()
        except Exception:
            return []
        for a in alerts:
            # a stopped engine watches nothing — saying "watching" with a
            # stale price would promise a ping that can never come
            a["watched"] = self.running and a["symbol"] in self.strategies
            a["last_price"] = self.prices.get(a["symbol"])
        return alerts

    # ------------------------------------------------------- intelligence

    def _guard_value(self) -> float:
        """Market value of legacy spot positions under guard — counted into
        equity/notional so the kill switch and sizing see the WHOLE account,
        not just what the margin broker knows about."""
        if not self.spot_guard:
            return 0.0
        return sum(p.qty * self.prices.get(s, p.entry)
                   for s, p in self.spot_guard.positions.items())

    def _guard_tick(self, cfg: dict, risk_cfg):
        """Enforce stops/TP/trailing on legacy spot positions the margin
        broker cannot see. No new entries here — guard positions only close."""
        guard = self.spot_guard
        for symbol in list(guard.positions.keys()):
            pos = guard.positions.get(symbol)
            price = self.prices.get(symbol)
            if not pos or not price:
                continue
            # trail with fresh candles when possible (best-effort)
            try:
                raw = self.market.ohlcv(symbol, cfg["timeframe"], limit=100)
                dfg = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
                if len(dfg) > 20:
                    closed = dfg.iloc[:-1]
                    atr_val = float(atr(closed, 14).iloc[-1])
                    if atr_val > 0:
                        new_stop, hw = risk_mod.manage_stop(
                            pos.side, pos.entry, pos.stop, getattr(pos, "r_value", 0.0),
                            getattr(pos, "high_water", None),
                            float(closed["close"].iloc[-1]), atr_val, risk_cfg,
                            fee_roundtrip=0.02, trailing=True)
                        pos.high_water = hw
                        if new_stop and new_stop != pos.stop:
                            pos.stop = new_stop
            except Exception:
                pass  # stale-candle trail is optional; the hard stop below is not
            hit_stop = pos.stop and risk_mod.stop_hit(pos.side, price, pos.stop)
            hit_tp = pos.take_profit and risk_mod.stop_hit(-pos.side, price, pos.take_profit)
            if not (hit_stop or hit_tp):
                continue
            reason = "stop loss" if hit_stop else "take profit"
            try:
                with self._order_lock:
                    trade = guard.close(symbol, price)
            except Exception as e:
                self.log(f"legacy spot close retry for {symbol}: {e}", "warn")
                self._persist_positions(cfg)  # broker may have dropped a dead record
                continue
            if trade:
                store.record_trade(trade, guard.mode, f"{reason} (legacy spot)")
                store.record_fill(symbol, "sell", trade["qty"], trade["exit"], guard.mode)
                self.log(f"{reason} on legacy spot {symbol} pnl={trade['pnl']:+.2f}", "warn")
            self._persist_positions(cfg)
        if not guard.positions:
            self._persist_positions(cfg)  # writes an empty record — file cleared
            self.spot_guard = None
            self.log("all legacy spot positions closed — spot record cleared")

    def _refresh_governors(self):
        """Cheap local-SQLite reads feeding the risk governors."""
        now = time.time()
        if now - self._governor.get("ts", 0) > 300:
            try:
                self._governor = {
                    "stopouts": store.todays_stopouts(self.broker.mode),
                    "streak": store.loss_streak(self.broker.mode),
                    "ts": now,
                }
            except Exception:
                self._governor["ts"] = now
        if now - self._perf_ts > 900:
            try:
                self._perf = store.performance(self.broker.mode)
            except Exception:
                pass
            self._perf_ts = now

    def _risk_multiplier(self, symbol: str) -> float:
        """Shrink (never enlarge) risk while the bot is cold or the symbol
        has poor recorded expectancy."""
        mult = 1.0
        streak = self._governor.get("streak", 0)
        if streak >= 5:
            mult = 0.25
        elif streak >= 3:
            mult = 0.5
        stats = self._perf.get(symbol)
        if stats and stats["n"] >= 8 and stats["total"] < 0:
            mult *= 0.5 if stats["win_rate"] < 0.35 else 0.75
        return mult

    def _entry_gate(self, symbol: str, desired: int, price: float,
                    atr_val: float, cfg: dict, risk_cfg) -> str | None:
        """Pre-entry checks. Returns a human-readable reason to skip, or
        None to trade. Order: cheapest first, network calls last."""
        # 1) per-symbol stop-out cooldown: two losing stop-outs today means
        #    this pair is churning fees, not trending
        if self._governor.get("stopouts", {}).get(symbol, 0) >= 2:
            return "2 stop-outs today — cooling off"
        # 2) multi-timeframe confirmation: don't fight a firm higher-TF trend
        bias, bias_adx = self._htf(symbol, cfg)
        if bias and desired == -bias and bias_adx >= 20:
            return "against the higher-timeframe trend"
        # 3) execution-venue spread
        spread = self.market.spread_pct(symbol, margin=not self.spot_only)
        if spread > 0.015:
            return f"spread {spread * 100:.1f}%"
        # 4) fee-aware minimum edge: the initial stop distance must clear the
        #    round-trip cost with real room, or the trade is a fee donation
        fee_side = float(cfg.get("fee_pct_per_side", 0.0)) / 100.0
        round_trip = 2 * fee_side + spread
        if risk_cfg.atr_stop_mult * atr_val < 1.5 * round_trip * price:
            return "expected move below round-trip fees"
        return None

    def _htf(self, symbol: str, cfg: dict) -> tuple[int, float]:
        """Cached higher-timeframe trend bias: (bias, adx). 15m entries are
        judged against 1h; 1h entries against 1d. Fails open (0, 0.0) so a
        data hiccup never blocks exits or the other gates."""
        htf_tf = {"15m": "1h", "1h": "1d"}.get(cfg.get("timeframe"))
        if not htf_tf:
            return 0, 0.0
        cached = self.htf.get(symbol)
        if cached and time.time() - cached[0] < TIMEFRAME_SECONDS[htf_tf]:
            return cached[1], cached[2]
        bias, strength = 0, 0.0
        try:
            raw = self.market.ohlcv(symbol, htf_tf, limit=120)
            dfh = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
            if len(dfh) >= 60:
                bias, strength = htf_bias(dfh.iloc[:-1])
        except Exception:
            pass
        self.htf[symbol] = (time.time(), bias, strength)
        return bias, strength

    def _rank_universe(self, symbols: list, margin: bool) -> list:
        """Score every candidate pair by tradability and keep the best.
        Symbols with open positions are always kept — their stops must
        stay watched regardless of score."""
        scored, dropped = [], []
        for sym in symbols:
            try:
                raw = self.market.ohlcv(sym, "1h", limit=120)
                dfr = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
                if len(dfr) < 40:
                    dropped.append((sym, "thin history"))
                    continue
                closed = dfr.iloc[:-1]
                px = float(closed["close"].iloc[-1])
                if px <= 0:
                    dropped.append((sym, "no price"))
                    continue
                atr_pct = float(atr(closed, 14).iloc[-1]) / px
                flat_frac = float((closed["high"] == closed["low"]).tail(96).mean())
                spread = self.market.spread_pct(sym, margin=margin)
                if flat_frac > 0.20:
                    dropped.append((sym, f"{flat_frac * 100:.0f}% flat bars"))
                elif atr_pct < 0.002:
                    dropped.append((sym, "dead volatility"))
                elif atr_pct > 0.08:
                    dropped.append((sym, "volatility too wild"))
                elif spread > 0.015:
                    dropped.append((sym, f"spread {spread * 100:.1f}%"))
                else:
                    scored.append((atr_pct / max(spread, 0.0005), sym))
            except Exception as e:
                dropped.append((sym, str(e)[:40]))
            time.sleep(0.3)  # stay polite to the rate limit during the scan
        if not scored:
            self.log("universe scan found nothing tradeable — keeping the "
                     "configured list unfiltered", "warn")
            return symbols
        scored.sort(reverse=True)
        keep = [s for _, s in scored[:10]]
        for sym in symbols:  # never drop a symbol we hold
            if sym not in keep and self.broker and sym in self.broker.positions:
                keep.append(sym)
        if dropped:
            self.log("universe filter dropped: "
                     + ", ".join(f"{s} ({r})" for s, r in dropped[:12]))
        self.log(f"trading universe ranked by ATR%/spread — keeping {len(keep)}: "
                 + ", ".join(k.split("/")[0] for k in keep))
        return keep

    def _selftest(self, cfg: dict) -> tuple[bool, str]:
        """5-second pre-flight before live trading starts: fresh data, a
        funded account, and the effective risk limits logged where the
        operator can see them."""
        risk_cfg = risk_mod.RiskConfig.from_dict(cfg.get("risk", {}))
        self.log("effective risk limits — "
                 f"{risk_cfg.risk_per_trade_pct:g}%/trade, "
                 f"stop {risk_cfg.atr_stop_mult:g}×ATR, "
                 f"max {risk_cfg.max_open_positions} positions × "
                 f"{risk_cfg.max_position_pct:g}% each, "
                 f"{risk_cfg.max_leverage:g}× leverage cap, "
                 f"daily-loss breaker {risk_cfg.daily_loss_limit_pct:g}%")
        symbols = cfg.get("symbols") or []
        if symbols:
            probe = symbols[0]
            try:
                raw = self.market.ohlcv(probe, cfg["timeframe"], limit=60)
            except Exception as e:
                return False, f"no candles for {probe}: {e}"
            if len(raw) < 30:
                return False, f"only {len(raw)} bars of history for {probe}"
            age = time.time() - raw[-1][0] / 1000.0
            max_age = 3 * TIMEFRAME_SECONDS.get(cfg["timeframe"], 3600)
            if age > max_age:
                return False, (f"market data is stale ({age / 60:.0f} min old "
                               f"bar for {probe}) — refusing to trade blind")
        try:
            equity = self.broker.equity(self.prices) + self._guard_value()
        except Exception as e:
            return False, f"could not read account equity: {e}"
        has_positions = bool(self.broker.positions) or bool(
            self.spot_guard and self.spot_guard.positions)
        if equity <= 0 and not has_positions:
            return False, "zero balance — fund the account or check the API keys"
        return True, ""

    # --------------------------------------------------------------- state

    def snapshot(self) -> dict:
        cfg = store.load_config()
        mode = cfg["mode"]
        positions, equity, balance = [], None, None
        if self.broker:
            # list() copies: the engine thread mutates these dicts while the
            # web thread renders — iterating live dicts intermittently 500s
            equity = self.broker.equity(self.prices) + self._guard_value()
            balance = self.broker.balance
            positions = [
                p.to_dict(self.prices.get(s))
                for s, p in list(self.broker.positions.items())
            ]
            if self.spot_guard:
                for s, p in list(self.spot_guard.positions.items()):
                    d = p.to_dict(self.prices.get(s))
                    d["strategy"] = "legacy spot (guarded)"
                    positions.append(d)
        stats = store.trade_stats(mode)
        target = float(cfg.get("volume_target_usd") or 0)
        volume = None
        if target > 0:
            volume = {"target": target,
                      "done": store.cumulative_volume(
                          mode, float(cfg.get("volume_target_since") or 0))}
        return {
            "volume": volume,
            "status": "running" if self.running else "stopped",
            "mode": mode,
            "trade_mode": self.trade_mode,
            "proposals": self._proposals_snapshot(),
            "alerts": self._alerts_snapshot(),
            "routines": self._routines_snapshot(),
            "sphinx_portfolio": self._portfolio_snapshot(),
            "error": self.error,
            "kill_switch": self.kill_switch_tripped,
            "data_source": self.market.active_source if self.market else None,
            "equity": equity,
            "balance": balance,
            "positions": positions,
            "signals": self.last_signals,
            "prices": self.prices,
            "stats": stats,
            "equity_curve": store.equity_curve(mode),
            "trades": store.recent_trades(50),
            "logs": list(self.logs)[:80],
        }


ENGINE = BotEngine()
