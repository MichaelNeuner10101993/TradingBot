#!/usr/bin/env bash
# =============================================================================
# install.sh – TradingBot Erstinstallation auf einem frischen Raspberry Pi
#
# Verwendung (frischer Pi, Repo noch nicht vorhanden):
#   curl -fsSL https://raw.githubusercontent.com/MichaelNeuner10101993/TradingBot/main/install.sh | bash
#
# Oder nach manuellem Download:
#   bash install.sh
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Farben
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC}  $*"; }
err()  { echo -e "${RED}  ✗${NC} $*"; }
info() { echo -e "${CYAN}  →${NC} $*"; }
step() { echo -e "\n${BOLD}━━━ $* ━━━${NC}"; }

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
REPO_URL="https://github.com/MichaelNeuner10101993/TradingBot.git"
REPO_BRANCH="main"
DEFAULT_INSTALL_DIR="$HOME/bot"
BOTUSER="$(whoami)"

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}${CYAN}"
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║       TradingBot – Raspberry Pi Setup        ║"
echo "  ║       Kraken · CCXT · SMA-Crossover          ║"
echo "  ╚══════════════════════════════════════════════╝"
echo -e "${NC}"
echo "  Benutzer:  $BOTUSER"
echo "  Datum:     $(date '+%Y-%m-%d %H:%M')"
echo ""

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

ask() {
    # ask <variable> <prompt> [default]
    local varname="$1"
    local prompt="$2"
    local default="${3:-}"
    local value=""
    if [[ -n "$default" ]]; then
        read -rp "  ${prompt} [${default}]: " value
        value="${value:-$default}"
    else
        while [[ -z "$value" ]]; do
            read -rp "  ${prompt}: " value
            [[ -z "$value" ]] && err "Pflichtfeld – bitte ausfüllen."
        done
    fi
    printf -v "$varname" '%s' "$value"
}

ask_secret() {
    local varname="$1"
    local prompt="$2"
    local value=""
    while [[ -z "$value" ]]; do
        read -rsp "  ${prompt}: " value
        echo ""
        [[ -z "$value" ]] && err "Pflichtfeld – bitte ausfüllen."
    done
    printf -v "$varname" '%s' "$value"
}

ask_optional() {
    local varname="$1"
    local prompt="$2"
    local value=""
    read -rsp "  ${prompt} (Enter zum Überspringen): " value
    echo ""
    printf -v "$varname" '%s' "$value"
}

confirm() {
    local prompt="${1:-Fortfahren?}"
    local answer=""
    read -rp "  ${prompt} [j/N]: " answer
    [[ "${answer,,}" =~ ^(j|ja|y|yes)$ ]]
}

# ---------------------------------------------------------------------------
# Schritt 1: Installationspfad
# ---------------------------------------------------------------------------
step "Installationsverzeichnis"

ask INSTALL_DIR "Installationsverzeichnis" "$DEFAULT_INSTALL_DIR"
INSTALL_DIR="${INSTALL_DIR%/}"  # trailing slash entfernen

if [[ -d "$INSTALL_DIR/.git" ]]; then
    warn "Verzeichnis existiert bereits mit Git-Repo."
    EXISTING=true
elif [[ -d "$INSTALL_DIR" ]] && [[ "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
    warn "Verzeichnis existiert bereits und ist nicht leer."
    EXISTING=false
else
    EXISTING=false
fi

ok "Installationsverzeichnis: $INSTALL_DIR"

# ---------------------------------------------------------------------------
# Schritt 2: System-Pakete prüfen und installieren
# ---------------------------------------------------------------------------
step "System-Voraussetzungen"

MISSING_PKGS=()

command -v git  &>/dev/null || MISSING_PKGS+=(git)
command -v curl &>/dev/null || MISSING_PKGS+=(curl)

# Python 3.11+ prüfen
PYTHON_CMD=""
for cmd in python3.13 python3.12 python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
        PY_VER="$($cmd -c 'import sys; print(sys.version_info[:2])')"
        if $cmd -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
            PYTHON_CMD="$cmd"
            break
        fi
    fi
done

if [[ -z "$PYTHON_CMD" ]]; then
    MISSING_PKGS+=(python3.11 python3.11-venv python3.11-dev)
fi

command -v sqlite3 &>/dev/null || MISSING_PKGS+=(sqlite3)

if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
    warn "Fehlende Pakete: ${MISSING_PKGS[*]}"
    info "Führe apt-get install aus..."
    sudo apt-get update -qq
    sudo apt-get install -y "${MISSING_PKGS[@]}"
    ok "Pakete installiert"
    # Python nochmal suchen
    if [[ -z "$PYTHON_CMD" ]]; then
        for cmd in python3.13 python3.12 python3.11 python3; do
            if $cmd -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
                PYTHON_CMD="$cmd"
                break
            fi
        done
    fi
fi

if [[ -z "$PYTHON_CMD" ]]; then
    err "Python 3.11+ nicht gefunden. Bitte manuell installieren."
    exit 1
fi

ok "Python: $($PYTHON_CMD --version)"
ok "Git:    $(git --version)"

# ---------------------------------------------------------------------------
# Schritt 3: Repo klonen oder aktualisieren
# ---------------------------------------------------------------------------
step "Repository"

if [[ "$EXISTING" == "true" ]]; then
    info "Aktualisiere bestehendes Repository..."
    git -C "$INSTALL_DIR" fetch origin
    git -C "$INSTALL_DIR" checkout "$REPO_BRANCH"
    git -C "$INSTALL_DIR" pull origin "$REPO_BRANCH"
    ok "Repository aktualisiert (Branch: $REPO_BRANCH)"
else
    info "Klone $REPO_URL → $INSTALL_DIR ..."
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
    ok "Repository geklont (Branch: $REPO_BRANCH)"
fi

# Ab jetzt ist das Verzeichnis garantiert vorhanden
cd "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# Schritt 4: Credentials abfragen
# ---------------------------------------------------------------------------
step "API-Konfiguration"
echo ""
echo "  Kraken-API: Erstelle Keys unter:"
echo "  https://www.kraken.com/u/security/api"
echo "  Benötigte Rechte: Query Funds, Query Orders, Create Orders"
echo "  KEIN Withdraw-Recht vergeben!"
echo ""

ask_secret KRAKEN_API_KEY    "Kraken API Key"
ask_secret KRAKEN_API_SECRET "Kraken API Secret"

echo ""
echo "  Telegram: Token via @BotFather erstellen."
echo "  Chat-ID:  Schreibe dem Bot eine Nachricht, dann:"
echo "  https://api.telegram.org/bot<TOKEN>/getUpdates"
echo ""

ask_secret TELEGRAM_BOT_TOKEN "Telegram Bot Token"
ask        TELEGRAM_CHAT_ID   "Telegram Chat ID (nur Zahlen)"

echo ""
echo "  CryptoPanic API Key (kostenlos, optional):"
echo "  https://cryptopanic.com/developers/api/"
echo ""
ask_optional CRYPTOPANIC_API_KEY "CryptoPanic API Key"

# ---------------------------------------------------------------------------
# Schritt 5: .env schreiben
# ---------------------------------------------------------------------------
step ".env Datei"

ENV_FILE="$INSTALL_DIR/.env"

cat > "$ENV_FILE" << EOF
# Kraken API Keys – NUR Trade-Rechte, KEIN Withdraw!
KRAKEN_API_KEY=${KRAKEN_API_KEY}
KRAKEN_API_SECRET=${KRAKEN_API_SECRET}

# News-Agent – Telegram
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}

# News-Agent – CryptoPanic (optional, kostenloser Key)
CRYPTOPANIC_API_KEY=${CRYPTOPANIC_API_KEY:-}

# Optional – Twitter/X Basic API (~100$/Monat)
TWITTER_BEARER_TOKEN=
EOF

chmod 600 "$ENV_FILE"
ok ".env geschrieben (chmod 600)"

# ---------------------------------------------------------------------------
# Schritt 6: Verzeichnisse anlegen
# ---------------------------------------------------------------------------
step "Verzeichnisse"

for d in db db/archive logs run bot.conf.d data; do
    mkdir -p "$INSTALL_DIR/$d"
    ok "$d/"
done

# ---------------------------------------------------------------------------
# Schritt 7: Python Virtual Environment
# ---------------------------------------------------------------------------
step "Python Virtual Environment"

VENV_DIR="$INSTALL_DIR/botvenv"

if [[ -d "$VENV_DIR" ]]; then
    info "venv vorhanden – aktualisiere..."
else
    info "Erstelle venv mit $PYTHON_CMD ..."
    "$PYTHON_CMD" -m venv "$VENV_DIR"
    ok "venv erstellt"
fi

info "Installiere Python-Abhängigkeiten ..."
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet
ok "Abhängigkeiten installiert"

# TextBlob Corpora
"$VENV_DIR/bin/python" -m textblob.download_corpora 2>/dev/null || true
ok "TextBlob Corpora OK"

# ---------------------------------------------------------------------------
# Schritt 8: systemd Services installieren
# ---------------------------------------------------------------------------
step "systemd Services"

SYSTEMD_DIR="/etc/systemd/system"

for src in "$INSTALL_DIR/systemd/"*.service "$INSTALL_DIR/systemd/"*.target; do
    [[ -f "$src" ]] || continue
    fname="$(basename "$src")"
    info "Installiere $fname ..."
    sudo sed \
        -e "s|DEIN_USER|$BOTUSER|g" \
        -e "s|DEIN_BOTDIR|$INSTALL_DIR|g" \
        "$src" > "/tmp/$fname"
    sudo mv "/tmp/$fname" "$SYSTEMD_DIR/$fname"
    sudo chmod 644 "$SYSTEMD_DIR/$fname"
    ok "$fname"
done

sudo systemctl daemon-reload
ok "systemd daemon-reload"

# ---------------------------------------------------------------------------
# Schritt 9: sudoers – Bot-Control ohne Passwort
# ---------------------------------------------------------------------------
step "sudoers"

SUDOERS_FILE="/etc/sudoers.d/tradingbot"
SUDOERS_CONTENT="$BOTUSER ALL=(ALL) NOPASSWD: /bin/systemctl stop tradingbot@*
$BOTUSER ALL=(ALL) NOPASSWD: /bin/systemctl start tradingbot@*
$BOTUSER ALL=(ALL) NOPASSWD: /bin/systemctl restart tradingbot@*"

echo "$SUDOERS_CONTENT" | sudo tee "$SUDOERS_FILE" > /dev/null
sudo chmod 440 "$SUDOERS_FILE"

# Syntax validieren
if sudo visudo -cf "$SUDOERS_FILE" &>/dev/null; then
    ok "sudoers eingerichtet ($SUDOERS_FILE)"
else
    err "sudoers Syntax-Fehler – Datei entfernt!"
    sudo rm -f "$SUDOERS_FILE"
fi

# ---------------------------------------------------------------------------
# Schritt 10: Services aktivieren
# ---------------------------------------------------------------------------
step "Services aktivieren"

sudo systemctl enable tradingbot-web.service
ok "tradingbot-web.service aktiviert"

sudo systemctl enable tradingbot-supervisor.service
ok "tradingbot-supervisor.service aktiviert"

sudo systemctl enable news-agent.service
ok "news-agent.service aktiviert"

sudo systemctl enable tradingbot.target
ok "tradingbot.target aktiviert"

# ---------------------------------------------------------------------------
# Schritt 11: Telegram-Verbindung testen
# ---------------------------------------------------------------------------
step "Telegram-Test"

if confirm "Telegram-Verbindung jetzt testen?"; then
    info "Sende Test-Nachricht ..."
    if "$VENV_DIR/bin/python" "$INSTALL_DIR/news_agent.py" --test-telegram 2>&1 | tail -5; then
        ok "Telegram-Verbindung erfolgreich"
    else
        warn "Telegram-Test fehlgeschlagen – bitte Token und Chat-ID prüfen."
    fi
else
    info "Telegram-Test übersprungen."
fi

# ---------------------------------------------------------------------------
# Schritt 12: Services starten
# ---------------------------------------------------------------------------
step "Services starten"

if confirm "Services jetzt starten? (Web-Dashboard + Supervisor + News-Agent)"; then
    sudo systemctl start tradingbot.target
    sleep 3
    echo ""
    SERVICES=(tradingbot-web.service tradingbot-supervisor.service news-agent.service)
    for svc in "${SERVICES[@]}"; do
        if systemctl is-active --quiet "$svc"; then
            ok "$svc läuft"
        else
            err "$svc NICHT gestartet – prüfe: journalctl -u $svc -n 30"
        fi
    done
else
    info "Services nicht gestartet. Manuell starten:"
    info "  sudo systemctl start tradingbot.target"
fi

# ---------------------------------------------------------------------------
# Fertig
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}${GREEN}"
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║           Installation abgeschlossen!        ║"
echo "  ╚══════════════════════════════════════════════╝"
echo -e "${NC}"
echo "  Installationsverzeichnis: $INSTALL_DIR"
echo "  Python venv:              $VENV_DIR"
echo "  .env:                     $ENV_FILE"
echo ""
echo -e "  ${BOLD}Web-Dashboard${NC}"
echo "    http://$(hostname -I | awk '{print $1}'):5001"
echo ""
echo -e "  ${BOLD}Wichtige Befehle${NC}"
echo "    Status:          sudo systemctl status 'tradingbot@*'"
echo "    Web-Log:         journalctl -u tradingbot-web -f"
echo "    News-Log:        journalctl -u news-agent -f"
echo "    Supervisor-Log:  journalctl -u tradingbot-supervisor -f"
echo "    Bot starten:     sudo systemctl start tradingbot.target"
echo "    Bot stoppen:     sudo systemctl stop tradingbot.target"
echo ""
echo -e "  ${BOLD}Bots einrichten${NC}"
echo "    Öffne das Web-Dashboard und füge Bots über die UI hinzu."
echo "    Oder manuell via Telegram: /start_bot BTC/EUR"
echo ""
