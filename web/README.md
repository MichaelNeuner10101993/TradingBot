# Trading Bot – Web Dashboard

Leichtgewichtiges Multi-Bot-Dashboard auf Flask-Basis.
Läuft auf dem Raspberry Pi, erreichbar im lokalen Netz über den Browser.

---

## Start

```bash
python web/app.py
```

Erreichbar unter: `http://<pi-ip-adresse>:5001`

Die Pi-IP-Adresse findest du mit:
```bash
hostname -I
```

---

## Voraussetzungen

```bash
pip install -r requirements.txt   # flask ist bereits enthalten
```

Das Dashboard liest nur aus den SQLite-DBs in `db/` — es schreibt nichts.
Die Bots müssen laufen damit Daten sichtbar sind.

Chart.js wird lokal aus `web/static/chart.min.js` geladen (kein Internet nötig).
Falls die Datei fehlt:
```bash
curl -sL "https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js" \
  -o web/static/chart.min.js
```

---

## Wie es funktioniert

```
Bot-Instanz (main.py)          SQLite DB (db/SNX_EUR.db)
──────────────────────         ─────────────────────────
Jede Loop-Iteration    ──────► bot_state  (Signal, Preis, Balance)
BUY ausgeführt         ──────► trades     (Entry, SL, TP)
Order platziert        ──────► orders     (Side, Amount, Status)
Fehler aufgetreten     ──────► errors     (Kontext, Meldung)

Web-Dashboard (web/app.py)
──────────────────────────
Scannt db/*.db  ──────────────► liest alle DBs → rendert index.html
```

Jeder Bot hat eine eigene DB-Datei. Das Dashboard erkennt neue Instanzen
automatisch beim nächsten Refresh — kein Neustart nötig.

---

## Oberfläche

### Übersicht-Cards (oben)
Eine Card pro laufendem Bot mit:
- **Signal** – BUY (grün) / SELL (rot) / HOLD (gelb)
- **Aktueller Preis** – dynamische Dezimalstellen (PEPE-kompatibel)
- **Balance** in Quote- und Base-Währung
- **Offene Trades** (Anzahl)
- **Zuletzt aktualisiert** (z.B. „vor 12s")
- **Status-Badge** – LIVE / DRY RUN, ● ON / ■ OFF / ⚠ ERR

Ein Klick auf eine Card scrollt zur Detail-Sektion des jeweiligen Bots.

### Detail-Sektionen (aufklappbar)
Pro Bot fünf Bereiche:

| Bereich | Inhalt |
|---------|--------|
| **Offene Trades** | Entry, SL, TP, erwarteter Gewinn/Verlust (€ + %), aktueller P&L |
| **P&L-Chart** | Kumulativer Gewinn/Verlust über Zeit als Linienchart |
| **Trade-Historie** | Abgeschlossene Trades mit Ergebnis, P&L und Zeitstempel |
| **Letzte Orders** | Die letzten 10 Orders mit Side, Amount, Preis, Status |
| **Fehler** | Die letzten 3 Fehler mit Kontext und Meldung |

Sektionen lassen sich durch Klick auf den Header ein- und ausklappen.

### Offene Trades – Spalten
| Spalte | Beschreibung |
|--------|-------------|
| Amount | Gekaufte Menge in Base-Währung |
| Entry | Einstiegspreis |
| Stop-Loss | SL-Preis + erwarteter Verlust (% / €) |
| Exp. Verlust | Verlust in € wenn SL ausgelöst wird |
| Take-Profit | TP-Preis + erwarteter Gewinn (% / €) |
| Exp. Gewinn | Gewinn in € wenn TP ausgelöst wird |
| P&L | Aktueller unrealisierter Gewinn/Verlust in % |

### P&L-Chart
- Linie **grün** wenn aktuell im Gewinn, **rot** wenn im Verlust
- Punkte grün/rot je nach Einzel-Trade-Ergebnis
- Hover-Tooltip: kumulativer Wert + Trade-P&L + Abschlussgrund

### Auto-Refresh
Die Seite lädt sich automatisch alle **15 Sekunden** neu.
Ein Countdown-Timer oben rechts zeigt wann.

---

## Als systemd-Service (dauerhaft auf dem Pi)

Datei anlegen: `/etc/systemd/system/tradingbot-web.service`

```ini
[Unit]
Description=Trading Bot Web Dashboard
After=network.target

[Service]
User=xxx
WorkingDirectory=/home/xxx/bot
EnvironmentFile=/home/xxx/bot/.env
ExecStart=/home/xxx/bot/botvenv/bin/python /home/xxx/bot/web/app.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now tradingbot-web

# Status prüfen
sudo systemctl status tradingbot-web

# Logs
journalctl -u tradingbot-web -f
```

---

## Port

Aktuell: `5001`. In `web/app.py` ganz unten ändern:

```python
app.run(host="0.0.0.0", port=5001, debug=False)
```

---

## API-Endpunkt

```
GET /api/bots
```

Gibt den aktuellen Zustand aller Bots als JSON zurück:

```bash
curl http://localhost:5001/api/bots
```

```json
[
  {
    "symbol": "SNX/EUR",
    "signal": "HOLD",
    "last_price": 0.3307,
    "status": "running",
    "dry_run": false,
    "open_trades": [...],
    "pnl_history": [...]
  }
]
```
