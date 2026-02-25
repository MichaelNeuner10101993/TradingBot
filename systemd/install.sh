#!/usr/bin/env bash
# =============================================================
# install.sh – Richtet systemd-Services auf dem Pi ein
#
# Verwendung:
#   cd /home/xxx/bot
#   bash systemd/install.sh
# =============================================================
set -euo pipefail

BOTDIR="$(cd "$(dirname "$0")/.." && pwd)"
BOTUSER="$(whoami)"
SYSTEMD_DIR="/etc/systemd/system"

echo "=== Trading Bot systemd Setup ==="
echo "  Bot-Verzeichnis : $BOTDIR"
echo "  Benutzer        : $BOTUSER"
echo ""

# --- Service-Dateien kopieren und Platzhalter ersetzen -------
for src in "$BOTDIR/systemd/"*.service "$BOTDIR/systemd/"*.target; do
    fname="$(basename "$src")"
    dest="$SYSTEMD_DIR/$fname"
    echo "  Installiere $fname → $dest"
    sudo sed \
        -e "s|DEIN_USER|$BOTUSER|g" \
        -e "s|DEIN_BOTDIR|$BOTDIR|g" \
        "$src" > "/tmp/$fname"
    sudo mv "/tmp/$fname" "$dest"
    sudo chmod 644 "$dest"
done

# --- bot.conf.d kopieren (falls noch nicht vorhanden) ---------
if [[ ! -d "$BOTDIR/bot.conf.d" ]]; then
    echo "  WARNUNG: bot.conf.d nicht gefunden – übersprungen"
fi

# --- systemd neu laden und Services aktivieren ----------------
sudo systemctl daemon-reload

echo ""
echo "=== Services aktivieren ==="

BOTS=(SNX_EUR BTC_EUR TRUMP_EUR PEPE_EUR XRP_EUR ETH_EUR)

for bot in "${BOTS[@]}"; do
    if [[ -f "$BOTDIR/bot.conf.d/$bot.conf" ]]; then
        sudo systemctl enable "tradingbot@$bot.service"
        echo "  [OK] tradingbot@$bot aktiviert"
    else
        echo "  [SKIP] $BOTDIR/bot.conf.d/$bot.conf nicht gefunden"
    fi
done

sudo systemctl enable tradingbot-web.service
sudo systemctl enable tradingbot.target
sudo systemctl enable news-agent.service

echo ""
echo "=== Fertig! ==="
echo ""
echo "Starten:       sudo systemctl start tradingbot.target"
echo "Stoppen:       sudo systemctl stop tradingbot.target"
echo "Status:        sudo systemctl status 'tradingbot@*'"
echo "Logs Bot:      journalctl -u tradingbot@SNX_EUR -f"
echo "Logs Web:      journalctl -u tradingbot-web -f"
echo "News-Agent:    sudo systemctl start news-agent"
echo "Logs News:     journalctl -u news-agent -f"
