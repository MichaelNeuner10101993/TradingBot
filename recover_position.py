"""
recover_position.py – Trägt eine bestehende Position manuell in die DB ein.

Verwendung wenn der Bot eine Position hält die nicht in der DB steht
(z.B. nach Wechsel von state.db auf SNX_EUR.db):

  python recover_position.py
"""
import os
import sys
from dotenv import load_dotenv
import ccxt

load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))
from bot.persistence import StateDB
from bot.sl_tp import calc_levels
from bot.config import RiskConfig

def main():
    print("=== Position Recovery ===\n")

    # Exchange verbinden um aktuelle Balance zu lesen
    ex = ccxt.kraken({
        "apiKey":    os.getenv("KRAKEN_API_KEY"),
        "secret":    os.getenv("KRAKEN_API_SECRET"),
        "enableRateLimit": True,
    })

    # Alle Paare mit offener Base-Balance anzeigen
    print("Lade aktuelle Balance von Kraken...")
    balance = ex.fetch_balance()
    holdings = {
        coin: float(info.get("free", 0) or 0)
        for coin, info in balance.items()
        if isinstance(info, dict)
        and float(info.get("free", 0) or 0) > 0
        and coin not in ("EUR", "USD", "USDT", "USDC", "info", "free", "used", "total")
    }

    if not holdings:
        print("Keine offenen Positionen gefunden.")
        return

    print("\nGefundene Positionen:")
    for i, (coin, amount) in enumerate(holdings.items(), 1):
        print(f"  [{i}] {coin}: {amount:.6f}")

    choice = input("\nWelche Position eintragen? Nummer eingeben: ").strip()
    coin_list = list(holdings.items())
    try:
        idx   = int(choice) - 1
        coin, amount = coin_list[idx]
    except (ValueError, IndexError):
        print("Ungültige Auswahl.")
        return

    quote      = input(f"Quote-Währung (default: EUR): ").strip() or "EUR"
    symbol     = f"{coin}/{quote}"
    db_path    = f"db/{coin}_{quote}.db"

    # Aktuellen Preis holen
    ticker     = ex.fetch_ticker(symbol)
    curr_price = float(ticker["last"])
    print(f"\nAktueller Preis: {curr_price:.6f} {quote}")

    entry_input = input(f"Entry-Preis eingeben (Enter = aktuellen Preis {curr_price:.6f} verwenden): ").strip()
    entry_price = float(entry_input) if entry_input else curr_price

    # SL/TP berechnen
    risk = RiskConfig()
    sl_pct_input = input(f"Stop-Loss %% (Enter = {risk.stop_loss_pct*100:.1f}%): ").strip()
    tp_pct_input = input(f"Take-Profit %% (Enter = {risk.take_profit_pct*100:.1f}%): ").strip()

    if sl_pct_input: risk.stop_loss_pct   = float(sl_pct_input) / 100
    if tp_pct_input: risk.take_profit_pct = float(tp_pct_input) / 100

    sl_price, tp_price = calc_levels(entry_price, risk)

    print(f"\n--- Zusammenfassung ---")
    print(f"Symbol:      {symbol}")
    print(f"Amount:      {amount:.6f} {coin}")
    print(f"Entry:       {entry_price:.6f} {quote}")
    print(f"Stop-Loss:   {sl_price:.6f} {quote}  (-{risk.stop_loss_pct*100:.1f}%)")
    print(f"Take-Profit: {tp_price:.6f} {quote}  (+{risk.take_profit_pct*100:.1f}%)")
    print(f"DB:          {db_path}")

    confirm = input("\nIn DB eintragen? (j/n): ").strip().lower()
    if confirm != "j":
        print("Abgebrochen.")
        return

    db = StateDB(db_path)

    # Prüfen ob bereits ein offener Trade existiert
    existing = db.get_open_trades(symbol)
    if existing:
        print(f"Achtung: {len(existing)} offener Trade bereits vorhanden.")
        overwrite = input("Trotzdem eintragen? (j/n): ").strip().lower()
        if overwrite != "j":
            db.close()
            return

    client_id = f"recovered-{coin.lower()}-manual"
    db.open_trade(
        client_id=client_id,
        symbol=symbol,
        amount=amount,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_price=tp_price,
    )

    # State aktualisieren damit Dashboard stimmt
    db.set_state("symbol", symbol)
    db.set_state("last_price", str(curr_price))
    db.set_state("base_currency", coin)
    db.set_state("quote_currency", quote)
    db.set_state("balance_base", f"{amount:.6f}")

    db.close()
    print(f"\n✓ Position eingetragen. SL/TP-Monitoring aktiv ab nächstem Bot-Zyklus.")


if __name__ == "__main__":
    main()
