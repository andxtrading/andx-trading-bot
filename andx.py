"""Native connector for ANDX Global / ANDX USA (docs.andxus.io).

The exchange runs a GraphQL API (v4):
  spot:   https://vakotrade-andx.cryptosrvc.com/graphql
  margin: https://margin-trading-service-andx.cryptosrvc.com/graphql

Auth: exchange the API key + secret for a JWT via the service_signin
mutation, then send `Authorization: Bearer <jwt>`. The JWT is cached and
refreshed automatically before it expires.

Today the venue trades SPOT (long/flat: buy on long signals, sell to USDT
on short/exit signals). Margin instruments exist on the platform but all
currently report is_trading_on=False; `margin_available()` checks live so
short-with-leverage support can light up when the exchange enables it.
"""

import math
import time
from datetime import datetime, timezone

import requests

SPOT_URL = "https://vakotrade-andx.cryptosrvc.com/graphql"
MARGIN_URL = "https://margin-trading-service-andx.cryptosrvc.com/graphql"
TIMEOUT = 20

# dashboard timeframe -> InstrumentHistoryPeriodicity enum.
# NOTE: no "4h" — ANDX has no 4h periodicity, and silently serving 1h bars
# for a 4h request mislabels every indicator. 4h requests fall through to
# the ccxt fallback venues, which serve real 4h candles.
PERIODICITY = {"15m": "minute15", "1h": "hour", "1d": "day"}
TIMEFRAME_SECONDS = {"15m": 900, "1h": 3600, "1d": 86400}


class AndxError(Exception):
    pass


def market_code(symbol: str) -> str:
    """'BTC/USDT' or 'BTC/USDT:USDT' -> 'BTCUSDT'"""
    return symbol.split(":")[0].replace("/", "").upper()


class AndxClient:
    def __init__(self, api_key="", api_secret="", **_ignored):
        self.api_key = api_key
        self.api_secret = api_secret
        self._jwt = None
        self._jwt_expiry = 0.0
        self._specs: dict[str, dict] = {}

    # ------------------------------------------------------------- plumbing

    def _gql(self, query: str, variables: dict | None = None,
             auth: bool = False, url: str = SPOT_URL):
        headers = {"content-type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._token()}"
        body = {"query": query, "variables": variables or {}}
        # ANDX rate-limits bursts of requests. Derivatives startup fires a big
        # burst (margin broker + a second client for the spot guard + universe
        # ranking) that can trip it, while lighter spot startup slips under.
        # Back off and retry on "too many requests" so a transient limit does
        # not kill the whole start — for us and for students, in any mode.
        delays = (1, 2, 4, 8, 12)
        for i in range(len(delays) + 1):
            resp = requests.post(url, json=body, headers=headers, timeout=TIMEOUT)
            try:
                data = resp.json()
            except ValueError:
                if resp.status_code == 429 and i < len(delays):
                    time.sleep(delays[i]); continue
                raise AndxError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            if data.get("errors"):
                msg = "; ".join(e.get("message", "?") for e in data["errors"])
                if i < len(delays) and ("too many request" in msg.lower()
                                        or "rate limit" in msg.lower()):
                    time.sleep(delays[i]); continue
                raise AndxError(msg[:300])
            return data["data"]
        raise AndxError("too many requests: still rate limited after retries")

    def _token(self) -> str:
        if self._jwt and time.time() < self._jwt_expiry - 60:
            return self._jwt
        data = self._gql(
            """mutation ($k: String!, $s: String!) {
                 service_signin(service_api_key: $k, service_api_secret: $s) {
                   jwt expires_at
                 }
               }""",
            {"k": self.api_key, "s": self.api_secret},
        )
        result = data["service_signin"]
        self._jwt = result["jwt"]
        expires = result.get("expires_at")
        try:  # ISO timestamp or epoch — fall back to 10 minutes
            if isinstance(expires, (int, float)):
                self._jwt_expiry = float(expires) / (1000 if expires > 1e12 else 1)
            else:
                self._jwt_expiry = datetime.fromisoformat(
                    str(expires).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            self._jwt_expiry = time.time() + 600
        return self._jwt

    # --------------------------------------------------------------- public

    def instruments(self) -> list[str]:
        data = self._gql("{ instruments { instrument_id } }")
        return [i["instrument_id"] for i in data["instruments"]]

    # Stable/stable pairs make no sense for momentum strategies
    _STABLE_BASES = {"USDT", "USDC", "USDA1", "DAI", "TUSD"}

    def _code_to_symbol(self, code: str) -> str | None:
        """'XRPUSDT' -> 'XRP/USDT'; None if not USDT-quoted or stable-based."""
        if not code.endswith("USDT"):
            return None  # collateral/sizing is USDT — skip USD/other quotes
        base = code[:-4]
        if not base or base in self._STABLE_BASES:
            return None
        return f"{base}/USDT"

    def spot_symbols(self) -> list[str]:
        """Every tradeable USDT spot pair on ANDX, bot-format."""
        out = [self._code_to_symbol(c) for c in self.instruments()]
        return sorted(s for s in out if s)

    def margin_symbols(self) -> list[str]:
        """Every USDT-margined derivatives instrument on ANDX, bot-format."""
        data = self._gql("{ margin_instruments { margin_instrument_id } }",
                         url=MARGIN_URL)
        out = [self._code_to_symbol(m["margin_instrument_id"])
               for m in data["margin_instruments"]]
        return sorted(s for s in out if s)

    def ticker(self, symbol: str) -> dict:
        data = self._gql(
            """query ($i: String!) {
                 instruments(instrument_id: $i) {
                   price { bid ask ts }
                 }
               }""",
            {"i": market_code(symbol)},
        )
        rows = data["instruments"]
        if not rows or not rows[0].get("price"):
            raise AndxError(f"no price for {symbol}")
        return rows[0]["price"]

    def last_price(self, symbol: str) -> float:
        p = self.ticker(symbol)
        bid, ask = float(p["bid"] or 0), float(p["ask"] or 0)
        if bid and ask:
            return (bid + ask) / 2
        return bid or ask

    def spread_pct(self, symbol: str) -> float:
        """Bid/ask spread as a fraction of mid (0.015 = 1.5%). A one-sided
        book returns 0.05 — effectively 'do not market-order into this'."""
        p = self.ticker(symbol)
        bid, ask = float(p["bid"] or 0), float(p["ask"] or 0)
        if bid and ask and ask >= bid:
            return (ask - bid) / ((ask + bid) / 2)
        return 0.05

    def price_bars(self, symbol: str, timeframe: str, limit: int = 300) -> list:
        """Returns ccxt-style rows: [ms_timestamp, open, high, low, close, volume].
        The API has no volume field on bars, so volume is 0. Without a
        date_range the API caps results at 100 bars, so one is always sent."""
        now = datetime.now(timezone.utc)
        window = TIMEFRAME_SECONDS.get(timeframe, 3600) * (limit + 5)
        date_range = {
            "time_from": datetime.fromtimestamp(now.timestamp() - window, timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "time_to": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        data = self._gql(
            """query ($i: String!, $l: Int, $p: InstrumentHistoryPeriodicity,
                      $d: DateRangeInput) {
                 instrument_price_bars(instrument_id: $i, limit: $l,
                                       periodicity: $p, date_range: $d) {
                   ts open high low close
                 }
               }""",
            {"i": market_code(symbol), "l": limit,
             "p": PERIODICITY.get(timeframe, "hour"), "d": date_range},
        )
        bars = data["instrument_price_bars"] or []
        out = []
        for b in bars:
            ts = datetime.fromisoformat(b["ts"].replace("Z", "+00:00")).timestamp() * 1000
            out.append([ts, float(b["open"]), float(b["high"]),
                        float(b["low"]), float(b["close"]), 0.0])
        out.sort(key=lambda r: r[0])
        return out

    # -------------------------------------------------------------- private

    def balances(self, field: str = "free_balance") -> dict:
        """{currency_id: balance} as floats. The API returns one row per
        wallet per currency, so rows are summed per currency.

        field: 'free_balance' (spendable) or 'total_balance' (free + locked).
        Fill verification must use total_balance: accepting an order LOCKS
        the quantity (free drops immediately), so a free-balance delta reads
        a not-yet-filled FOK sell as already filled."""
        data = self._gql(
            "{ accounts_balances { currency_id free_balance total_balance } }",
            auth=True,
        )
        out: dict[str, float] = {}
        for b in data["accounts_balances"]:
            out[b["currency_id"]] = out.get(b["currency_id"], 0.0) + float(b[field] or 0)
        return out

    def instrument_spec(self, symbol: str) -> dict:
        """Cached {quantity_decimals, min_quantity, max_quantity} for a pair."""
        code = market_code(symbol)
        if code not in self._specs:
            data = self._gql(
                """query ($i: String!) {
                     instruments(instrument_id: $i) {
                       quantity_decimals min_quantity max_quantity
                     }
                   }""",
                {"i": code},
            )
            rows = data["instruments"]
            if not rows:
                raise AndxError(f"unknown instrument {code}")
            self._specs[code] = rows[0]
        return self._specs[code]

    def quantize(self, symbol: str, quantity: float) -> float:
        """Floor quantity to the instrument's allowed decimals and clamp to
        its min/max. Returns 0.0 if the result would be below the minimum."""
        spec = self.instrument_spec(symbol)
        decimals = spec.get("quantity_decimals")
        decimals = 8 if decimals is None else int(decimals)  # 0 is valid (whole coins)
        factor = 10 ** decimals
        # round() after floor snaps float artifacts (1101.3000000000002 -> 1101.3)
        # so the JSON payload carries exactly `decimals` decimal places
        qty = round(math.floor(quantity * factor) / factor, decimals)
        max_q = spec.get("max_quantity")
        if max_q:
            qty = min(qty, float(max_q))
        min_q = float(spec.get("min_quantity") or 0)
        if qty < min_q:
            return 0.0
        return qty

    def base_currency(self, symbol: str) -> str:
        return symbol.split(":")[0].split("/")[0].upper()

    def create_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        """side: 'buy' | 'sell'. quantity in base currency, quantized to the
        instrument's precision.

        ANDX accepts orders and may reject them ASYNCHRONOUSLY (an order_id is
        returned, then the FOK order dies on the book), and its ~1% taker fee
        is deducted from the received coins. So the fill is verified by
        balance diff and the returned dict carries `received` — the base
        quantity that actually landed in (or left) the account."""
        quantity = self.quantize(symbol, quantity)
        if quantity <= 0:
            raise AndxError("quantity below instrument minimum")
        base = self.base_currency(symbol)
        # TOTAL balance, not free: order acceptance locks coins (free drops
        # instantly), but total only moves when the trade actually settles.
        pre = self.balances("total_balance").get(base, 0.0)
        data = self._gql(
            """mutation ($i: String!, $side: OrderSide!, $q: Float!) {
                 create_order(instrument_id: $i, type: market, side: $side,
                              time_in_force: fok, quantity: $q,
                              quantity_mode: base) {
                   order_id status price quantity executed_quantity message
                 }
               }""",
            {"i": market_code(symbol), "side": side, "q": quantity},
            auth=True,
        )
        order = data["create_order"]
        if order.get("status") in ("rejected", "cancelled"):
            raise AndxError(f"order {order.get('status')}: {order.get('message')}")
        # verify the fill actually settled (poll TOTAL balance up to ~8s —
        # a locked-then-killed FOK sell never moves total_balance)
        received = 0.0
        for _ in range(8):
            time.sleep(1)
            post = self.balances("total_balance").get(base, 0.0)
            received = (post - pre) if side == "buy" else (pre - post)
            if received > quantity * 0.5:
                break
        if received <= quantity * 0.5:
            raise AndxError(
                f"order {order.get('order_id', '?')} did not fill "
                f"(likely killed on the book — thin liquidity)")
        order["received"] = received
        return order

    # ------------------------------------------------------------- margin
    # ANDX derivatives (margin) trading: real longs AND shorts with leverage,
    # stop-loss/take-profit enforced by the exchange. NOTE: the
    # is_trading_on flag is unreliable (reads false while orders fill), so
    # availability is decided by config, and failures surface per-order.

    def _mgql(self, query: str, variables: dict | None = None):
        return self._gql(query, variables, auth=True, url=MARGIN_URL)

    def margin_instrument_spec(self, symbol: str) -> dict:
        code = market_code(symbol)
        if not hasattr(self, "_margin_specs"):
            data = self._mgql(
                """{ margin_instruments { margin_instrument_id min_quantity
                     max_quantity quantity_decimals min_leverage max_leverage } }""")
            self._margin_specs = {m["margin_instrument_id"]: m
                                  for m in data["margin_instruments"]}
        if code not in self._margin_specs:
            raise AndxError(f"no margin instrument {code}")
        return self._margin_specs[code]

    def margin_quantize(self, symbol: str, quantity: float) -> float:
        spec = self.margin_instrument_spec(symbol)
        decimals = spec.get("quantity_decimals")
        decimals = 8 if decimals is None else int(decimals)
        factor = 10 ** decimals
        qty = round(math.floor(quantity * factor) / factor, decimals)
        if spec.get("max_quantity"):
            qty = min(qty, float(spec["max_quantity"]))
        if qty < float(spec.get("min_quantity") or 0):
            return 0.0
        return qty

    def margin_prices(self) -> dict:
        """{instrument_id: mid price} from the margin service (cached ~20s) —
        some instruments (e.g. SHIBA) are quoted only there, not on spot."""
        now = time.time()
        if getattr(self, "_margin_px_ts", 0.0) > now - 20:
            return self._margin_px
        data = self._gql(
            "{ margin_instruments { margin_instrument_id price { bid ask } } }",
            url=MARGIN_URL)
        out, spreads = {}, {}
        for m in data["margin_instruments"]:
            p = m.get("price") or {}
            bid, ask = float(p.get("bid") or 0), float(p.get("ask") or 0)
            if bid or ask:
                out[m["margin_instrument_id"]] = (bid + ask) / 2 if bid and ask else (bid or ask)
            if bid and ask and ask >= bid:
                spreads[m["margin_instrument_id"]] = (ask - bid) / ((ask + bid) / 2)
            else:
                spreads[m["margin_instrument_id"]] = 0.05  # one-sided book
        self._margin_px, self._margin_spread, self._margin_px_ts = out, spreads, now
        return out

    def margin_spread_pct(self, symbol: str) -> float:
        """Bid/ask spread on the margin service (the venue where derivatives
        orders execute). Falls back to 0.05 (= skip) when unquoted."""
        self.margin_prices()  # refreshes the 20s cache, including spreads
        return getattr(self, "_margin_spread", {}).get(market_code(symbol), 0.05)

    def margin_positions(self) -> list[dict]:
        data = self._mgql(
            """query ($p: PagerInput) {
                 open_margin_positions(pager: $p) {
                   margin_position_id instrument_id side leverage amount
                   entry_price pnl stop_loss take_profit start_ts_timestamp
                 }
               }""", {"p": {"offset": 0, "limit": 100}})
        return data["open_margin_positions"] or []

    def closed_margin_positions(self, limit: int = 20) -> list[dict]:
        data = self._mgql(
            """query ($p: PagerInput) {
                 closed_margin_positions(pager: $p) {
                   margin_position_id instrument_id side amount entry_price
                   end_bid_price end_ask_price pnl close_reason
                 }
               }""", {"p": {"offset": 0, "limit": limit}})
        return data["closed_margin_positions"] or []

    def open_margin(self, symbol: str, side: int, quantity: float, leverage: int,
                    stop_loss: float | None = None,
                    take_profit: float | None = None) -> dict:
        """side: +1 long / -1 short. Verifies the position actually opened
        (orders can be accepted then rejected async) and returns the server
        position row."""
        quantity = self.margin_quantize(symbol, quantity)
        if quantity <= 0:
            raise AndxError("quantity below instrument minimum")
        before = {p["margin_position_id"] for p in self.margin_positions()}
        self._mgql(
            """mutation ($i: String!, $side: MarginPositionSide!, $lev: Int!,
                         $q: Float!, $sl: Float, $tp: Float) {
                 create_margin_order(instrument_id: $i, side: $side,
                                     type: market, leverage: $lev, quantity: $q,
                                     stop_loss: $sl, take_profit: $tp) {
                   margin_order_id
                 }
               }""",
            {"i": market_code(symbol), "side": "buy" if side == 1 else "sell",
             "lev": int(leverage), "q": quantity,
             "sl": stop_loss, "tp": take_profit})
        for _ in range(8):
            time.sleep(1)
            for p in self.margin_positions():
                if (p["margin_position_id"] not in before
                        and p["instrument_id"] == market_code(symbol)):
                    return p
        raise AndxError("margin order did not open a position "
                        "(rejected or unfilled — thin book?)")

    def close_margin(self, position_id: str):
        self._mgql("mutation ($id: String!) { close_margin_position(margin_position_id: $id) }",
                   {"id": position_id})
        for _ in range(8):
            time.sleep(1)
            if position_id not in {p["margin_position_id"] for p in self.margin_positions()}:
                return
        raise AndxError("close accepted but position still open — check the exchange")

    def update_margin(self, position_id: str, leverage: int,
                      stop_loss: float | None = None,
                      take_profit: float | None = None):
        self._mgql(
            """mutation ($id: String!, $lev: Int!, $sl: Float, $tp: Float) {
                 update_open_margin_position(margin_position_id: $id,
                     leverage: $lev, stop_loss: $sl, take_profit: $tp) {
                   margin_position_id
                 }
               }""",
            {"id": position_id, "lev": int(leverage),
             "sl": stop_loss, "tp": take_profit})


def check_credentials(creds: dict) -> dict:
    try:
        client = AndxClient(api_key=creds.get("api_key", ""),
                            api_secret=creds.get("api_secret", ""))
        balances = client.balances()
        usdt = balances.get("USDT", 0.0)
        held = {k: v for k, v in balances.items() if v > 0}
        return {"ok": True,
                "detail": f"Connected to ANDX. Free USDT: {usdt:,.2f}"
                          + (f" (also holding: {', '.join(sorted(set(held) - {'USDT'}))})"
                             if set(held) - {"USDT"} else "")}
    except Exception as e:
        return {"ok": False, "detail": f"ANDX: {e}"}
