"""
Data Ingestion: OHLCV-Daten und Kontostand von der Exchange holen.
"""
import logging
import ccxt
from bot.config import ExchangeConfig, BotConfig
from bot.ops import retry_backoff

log = logging.getLogger("tradingbot.data_feed")


def build_exchange(cfg: ExchangeConfig) -> ccxt.Exchange:
    exchange = getattr(ccxt, cfg.exchange_id)({
        "apiKey": cfg.api_key,
        "secret": cfg.api_secret,
        "enableRateLimit": cfg.enable_rate_limit,
    })
    log.info(f"Exchange initialisiert: {cfg.exchange_id}")
    return exchange


class DataFeed:
    def __init__(self, exchange: ccxt.Exchange, bot_cfg: BotConfig):
        self.ex = exchange
        self.cfg = bot_cfg

    @retry_backoff(retries=3, base_delay=2.0, exceptions=(ccxt.NetworkError,), no_retry=(ccxt.DDoSProtection,))
    def fetch_ohlcv(self) -> list[list]:
        """Gibt OHLCV-Candles zurück. Limit = slow + Puffer."""
        limit = max(self.cfg.slow_period + 10, 100)
        candles = self.ex.fetch_ohlcv(
            self.cfg.symbol,
            timeframe=self.cfg.timeframe,
            limit=limit,
        )
        log.debug(f"OHLCV: {len(candles)} Candles geladen ({self.cfg.symbol} {self.cfg.timeframe})")
        return candles

    @retry_backoff(retries=3, base_delay=2.0, exceptions=(ccxt.NetworkError,), no_retry=(ccxt.DDoSProtection,))
    def fetch_balance(self) -> dict:
        balance = self.ex.fetch_balance()
        base, quote = self.cfg.symbol.split("/")
        base_free = float(balance.get(base, {}).get("free", 0.0) or 0.0)
        quote_free = float(balance.get(quote, {}).get("free", 0.0) or 0.0)
        log.debug(f"Balance: {base}={base_free}, {quote}={quote_free}")
        return {"base": base_free, "quote": quote_free, "base_currency": base, "quote_currency": quote}

    @retry_backoff(retries=3, base_delay=2.0, exceptions=(ccxt.NetworkError,), no_retry=(ccxt.DDoSProtection,))
    def fetch_open_orders(self) -> list:
        orders = self.ex.fetch_open_orders(self.cfg.symbol)
        log.debug(f"Offene Orders: {len(orders)}")
        return orders
