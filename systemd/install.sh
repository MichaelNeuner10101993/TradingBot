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
WG_CONF="/etc/wireguard/wg0.conf"
WG_PORT=""

# --- Argumente parsen -------------------------------------------------------
for arg in "$@"; do
    case "$arg" in
        --wg-port=*)
            WG_PORT="${arg#*=}"
            ;;
    esac
done

echo "=== Trading Bot systemd Setup ==="
echo "  Bot-Verzeichnis : $BOTDIR"
echo "  Benutzer        : $BOTUSER"
echo ""

# --- Anthropic API-Key (optional) --------------------------------------------
ENV_FILE="$BOTDIR/.env"
if ! grep -q "ANTHROPIC_API_KEY" "$ENV_FILE" 2>/dev/null; then
    echo "──────────────────────────────────────────────────────────────"
    echo "  ANTHROPIC_API_KEY (optional, aber empfohlen)"
    echo ""
    echo "  Ohne diesen Key versteht der Telegram-Bot nur eingeschränkte"
    echo "  Freitext-Befehle (Regex-Muster). Mit dem Key versteht er"
    echo "  beliebigen deutschen/englischen Freitext via Claude Haiku."
    echo ""
    echo "  Key erstellen: console.anthropic.com → API Keys → Create Key"
    echo "──────────────────────────────────────────────────────────────"
    read -rp "  Anthropic API-Key eingeben [Enter = überspringen]: " ANTHROPIC_KEY </dev/tty
    if [[ -n "$ANTHROPIC_KEY" ]]; then
        echo "" >> "$ENV_FILE"
        echo "ANTHROPIC_API_KEY=$ANTHROPIC_KEY" >> "$ENV_FILE"
        echo "  [OK] ANTHROPIC_API_KEY in $ENV_FILE eingetragen"
    else
        echo "  [SKIP] Kein Key – Telegram-Bot läuft mit eingeschränktem Freitext-Verständnis"
    fi
    echo ""
fi

# --- WireGuard-Port setzen (optional) ----------------------------------------
if [[ -z "$WG_PORT" ]]; then
    read -rp "WireGuard-Port setzen? [Enter = überspringen, sonst Port eingeben]: " WG_PORT </dev/tty
fi

if [[ -n "$WG_PORT" ]]; then
    if ! [[ "$WG_PORT" =~ ^[0-9]+$ ]] || (( WG_PORT < 1 || WG_PORT > 65535 )); then
        echo "  FEHLER: Ungültiger Port '$WG_PORT' – muss 1–65535 sein." >&2
        exit 1
    fi
    if [[ -f "$WG_CONF" ]]; then
        echo "  Setze WireGuard ListenPort auf $WG_PORT in $WG_CONF"
        sudo sed -i "s/^ListenPort\s*=.*/ListenPort = $WG_PORT/" "$WG_CONF"
        sudo systemctl restart wg-quick@wg0 && echo "  [OK] wg-quick@wg0 neu gestartet"
    else
        echo "  WARNUNG: $WG_CONF nicht gefunden – WireGuard-Port übersprungen"
    fi
    echo ""
fi

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
sudo systemctl enable tradingbot-supervisor.service
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
echo "Supervisor:    sudo systemctl start tradingbot-supervisor"
echo "Logs Supv:     journalctl -u tradingbot-supervisor -f"
