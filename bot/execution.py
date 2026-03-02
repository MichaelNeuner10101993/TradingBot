"""
Execution Layer: Orders platzieren, Status prüfen.
Unterstützt Dry-Run (kein echter Order-Submit).
"""
import uuid
import logging
import ccxt
from bot.config import BotConfig, RiskConfig
from bot.persistence import StateDB
from bot.ops import retry_backoff
from bot.sl_tp import calc_levels
from bot import notify

log = logging.getLogger("tradingbot.execution")

# Fee-Gate Konstanten (Kraken: 0.26% Taker-Fee pro Order, 0.52% Roundtrip)
_MIN_STOP_DIST_PCT = 0.40  # mind. 0.40% SL-Abstand
_MIN_TP_GROSS_PCT  = 0.78  # mind. 0.78% TP-Abstand (≈ 1.5× Roundtrip-Fee)


def _make_client_id() -> str:
    """Eindeutige Client-Order-ID für Idempotenz."""
    return f"bot-{uuid.uuid4().hex[:12]}"


class Executor:
    def __init__(self, exchange: ccxt.Exchange, bot_cfg: BotConfig, risk_cfg: RiskConfig, db: StateDB):
        self.ex = exchange
        self.cfg = bot_cfg
        self.risk_cfg = risk_cfg
        self.db = db

    def _precision_amount(self, amount: float) -> float:
        try:
            return float(self.ex.amount_to_precision(self.cfg.symbol, amount))
        except Exception:
            return round(amount, 8)

    def _meets_exchange_minimum(self, amount: float, price: float) -> tuple[bool, str]:
        """Prüft gegen Krakenss eigene Mindestmengen (aus CCXT market info)."""
        try:
            self.ex.load_markets()
            market = self.ex.markets.get(self.cfg.symbol, {})
            limits = market.get("limits", {})
            min_amount = (limits.get("amount") or {}).get("min") or 0
            min_cost   = (limits.get("cost")   or {}).get("min") or 0
            cost = amount * price
            if min_amount and amount < min_amount:
                return False, f"Menge zu klein: {amount:.6f} < {min_amount} (Kraken-Minimum)"
            if min_cost and cost < min_cost:
                return False, f"Orderwert zu klein: {cost:.2f} EUR < {min_cost} EUR (Kraken-Minimum)"
        except Exception as e:
            log.debug(f"Minimum-Prüfung übersprungen: {e}")
        return True, "ok"

    def _fee_gate(self, sl_price: float, tp_price: float, entry_price: float) -> tuple[bool, str]:
        """
        Prüft ob der Trade nach Gebühren noch profitabel sein kann.
        SL-Abstand muss >= _MIN_STOP_DIST_PCT%, TP-Abstand >= _MIN_TP_GROSS_PCT%.
        """
        if entry_price <= 0:
            return True, "ok"
        stop_dist_pct = abs(entry_price - sl_price) / entry_price * 100
        tp_gross_pct  = abs(tp_price   - entry_price) / entry_price * 100
        if stop_dist_pct < _MIN_STOP_DIST_PCT:
            return False, (
                f"FEE_GATE: Trade abgelehnt | SL-Abstand {stop_dist_pct:.2f}% "
                f"< {_MIN_STOP_DIST_PCT}% Minimum"
            )
        if tp_gross_pct < _MIN_TP_GROSS_PCT:
            return False, (
                f"FEE_GATE: Trade abgelehnt | TP-Abstand {tp_gross_pct:.2f}% "
                f"< {_MIN_TP_GROSS_PCT}% Minimum (< 1.5× Roundtrip-Fee)"
            )
        return True, "ok"

    @retry_backoff(retries=2, base_delay=3.0, exceptions=(ccxt.NetworkError,))
    def _submit_order(self, side: str, amount: float) -> dict:
        if side == "buy":
            return self.ex.create_market_buy_order(self.cfg.symbol, amount)
        return self.ex.create_market_sell_order(self.cfg.symbol, amount)

    def _fetch_order_status(self, order_id: str, fallback: dict) -> dict:
        """
        Versucht den Order-Status zu holen. Kraken archiviert Market-Orders
        sofort nach Ausführung – fetch_order schlägt dann fehl. In diesem Fall
        wird die ursprüngliche Order-Response (fallback) als 'filled' markiert
        zurückgegeben, damit die DB-Speicherung trotzdem stattfindet.
        """
        try:
            for attempt in range(3):
                try:
                    return self.ex.fetch_order(order_id, self.cfg.symbol)
                except ccxt.NetworkError:
                    import time; time.sleep(2 ** attempt)
            return self.ex.fetch_order(order_id, self.cfg.symbol)
        except (ccxt.ExchangeError, ccxt.NetworkError) as e:
            log.warning(f"fetch_order fehlgeschlagen ({e}) – verwende Order-Response als Fallback")
            return {**fallback, "status": "closed", "filled": fallback.get("amount")}

    def buy(self, amount: float, last_price: float, candles: list | None = None) -> dict | None:
        amount = self._precision_amount(amount)
        if amount <= 0:
            log.warning("BUY abgebrochen: amount <= 0 nach Precision-Rounding")
            return None

        ok, reason = self._meets_exchange_minimum(amount, last_price)
        if not ok:
            log.warning(f"BUY abgebrochen: {reason}")
            return None

        # Fee-Gate: SL/TP-Level schätzen, prüfen ob Trade nach Gebühren noch profitabel
        _sl_est, _tp_est = calc_levels(last_price, self.risk_cfg, candles)
        ok_fee, fee_reason = self._fee_gate(_sl_est, _tp_est, last_price)
        if not ok_fee:
            log.warning(fee_reason)
            return None

        client_id = _make_client_id()
        base = self.cfg.symbol.split("/")[0]

        if self.cfg.dry_run:
            log.info(f"[DRY] BUY {amount} {base} @ ~{last_price:.2f} (client_id={client_id})")
            order = {"id": client_id, "symbol": self.cfg.symbol, "side": "buy",
                     "amount": amount, "price": last_price, "status": "dry_run"}
        else:
            log.info(f"BUY {amount} {base} @ ~{last_price:.2f} (client_id={client_id})")
            order = self._submit_order("buy", amount)
            order = self._fetch_order_status(order["id"], fallback=order)
            log.info(f"BUY Order-Status: {order.get('status')} | filled={order.get('filled')}")
            # Kaufpreis sicherstellen bevor er in die DB geschrieben wird.
            # Kraken archiviert Market-Orders sofort → fetch_order schlägt fehl →
            # Fallback hat average=None. Wir injizieren last_price als Fallback.
            if not order.get("average") and not order.get("price"):
                order = {**order, "average": last_price}

        self.db.upsert_order(client_id, order)

        # SL/TP-Level berechnen und Trade in DB speichern
        # Kraken Market-Orders: Kaufpreis steht in average, nicht in price
        entry_price = (
            order.get("average")
            or order.get("price")
            or (order.get("cost") / order.get("filled") if order.get("cost") and order.get("filled") else None)
            or last_price
        )
        sl_price, tp_price = calc_levels(entry_price, self.risk_cfg, candles)
        self.db.open_trade(
            client_id=client_id,
            symbol=self.cfg.symbol,
            amount=amount,
            entry_price=entry_price,
            sl_price=sl_price,
            tp_price=tp_price,
        )
        notify.send_trade_buy(self.cfg.symbol, amount, entry_price, sl_price, tp_price, self.cfg.dry_run)
        return order

    def pyramid_buy(self, amount: float, last_price: float) -> dict | None:
        """
        Führt einen Pyramid-Nachkauf durch, ohne einen neuen Trade in der DB zu eröffnen.
        Gibt die Order zurück (inkl. tatsächlichem Kaufpreis in 'average'), oder None bei Fehler.
        """
        amount = self._precision_amount(amount)
        if amount <= 0:
            log.warning("PYRAMID-BUY abgebrochen: amount <= 0 nach Precision-Rounding")
            return None

        ok, reason = self._meets_exchange_minimum(amount, last_price)
        if not ok:
            log.warning(f"PYRAMID-BUY abgebrochen: {reason}")
            return None

        client_id = _make_client_id()
        base = self.cfg.symbol.split("/")[0]

        if self.cfg.dry_run:
            log.info(f"[DRY] PYRAMID-BUY {amount} {base} @ ~{last_price:.4f} (client_id={client_id})")
            order = {"id": client_id, "symbol": self.cfg.symbol, "side": "buy",
                     "amount": amount, "price": last_price, "average": last_price, "status": "dry_run"}
        else:
            log.info(f"PYRAMID-BUY {amount} {base} @ ~{last_price:.4f} (client_id={client_id})")
            order = self._submit_order("buy", amount)
            order = self._fetch_order_status(order["id"], fallback=order)
            log.info(f"PYRAMID-BUY Order-Status: {order.get('status')} | filled={order.get('filled')}")
            if not order.get("average") and not order.get("price"):
                order = {**order, "average": last_price}

        self.db.upsert_order(client_id, order)
        return order

    def sell(self, amount: float, last_price: float, trade_client_id: str | None = None, reason: str = "signal_close", override_amount: float | None = None) -> dict | None:
        effective_amount = self._precision_amount(override_amount if override_amount is not None else amount)
        if effective_amount <= 0:
            log.warning("SELL abgebrochen: amount <= 0 nach Precision-Rounding")
            return None

        ok, reason_min = self._meets_exchange_minimum(effective_amount, last_price)
        if not ok:
            log.warning(f"SELL abgebrochen (Staub-Position): {reason_min}")
            return None

        client_id = _make_client_id()
        base = self.cfg.symbol.split("/")[0]

        if self.cfg.dry_run:
            log.info(f"[DRY] SELL {effective_amount} {base} @ ~{last_price:.2f} (client_id={client_id})")
            order = {"id": client_id, "symbol": self.cfg.symbol, "side": "sell",
                     "amount": effective_amount, "price": last_price, "status": "dry_run"}
        else:
            log.info(f"SELL {effective_amount} {base} @ ~{last_price:.2f} (client_id={client_id})")
            order = self._submit_order("sell", effective_amount)
            order = self._fetch_order_status(order["id"], fallback=order)
            log.info(f"SELL Order-Status: {order.get('status')} | filled={order.get('filled')}")
            if not order.get("average") and not order.get("price"):
                order = {**order, "average": last_price}

        self.db.upsert_order(client_id, order)

        # Trade in DB schließen (SL/TP oder Signal)
        if trade_client_id:
            self.db.close_trade(trade_client_id, reason)

        return order
