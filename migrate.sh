#!/bin/bash
# =============================================================================
# migrate.sh — Trading Bot Migration auf aktuellen Stand
#
# Verwendung:  bash migrate.sh
# Idempotent: kann mehrfach ausgefuehrt werden ohne Schaden.
# Bestehende .env und scanner.conf werden NICHT ueberschrieben.
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()      { echo -e "${GREEN}  ok${RESET}  $*"; }
info()    { echo -e "${CYAN}  ->$RESET $*"; }
warn()    { echo -e "${YELLOW}  ! ${RESET} $*"; }
fail()    { echo -e "${RED}  FEHLER:${RESET} $*"; exit 1; }
section() { echo -e "\n${BOLD}${CYAN}=== $* ===${RESET}"; }

# ── Voraussetzungen ──────────────────────────────────────────────────────────
section "Voraussetzungen"
[[ $EUID -eq 0 ]] || fail "Bitte als root ausfuehren: sudo bash migrate.sh"
ok "root"
command -v git      >/dev/null 2>&1 || fail "git nicht gefunden"
command -v python3  >/dev/null 2>&1 || fail "python3 nicht gefunden"
command -v systemctl >/dev/null 2>&1 || fail "systemd nicht gefunden"
ok "git, python3, systemd vorhanden"

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info "Bot-Verzeichnis: $BOT_DIR"
[[ -f "$BOT_DIR/main.py" ]] || fail "main.py nicht gefunden — Script im Bot-Verzeichnis ausfuehren"

# ── 1. Git Pull ──────────────────────────────────────────────────────────────
section "1. Code aktualisieren (git pull)"
cd "$BOT_DIR"
BEFORE=$(git rev-parse --short HEAD 2>/dev/null || echo "unbekannt")
git pull origin main 2>&1 | sed 's/^/     /'
AFTER=$(git rev-parse --short HEAD 2>/dev/null || echo "unbekannt")
if [[ "$BEFORE" == "$AFTER" ]]; then
    ok "Bereits aktuell ($AFTER)"
else
    ok "Aktualisiert: $BEFORE -> $AFTER"
fi

# ── 2. Python venv + Pakete ──────────────────────────────────────────────────
section "2. Python-Umgebung"
VENV="$BOT_DIR/botvenv"
if [[ ! -d "$VENV" ]]; then
    info "Erstelle venv ..."
    python3 -m venv "$VENV"
    ok "venv erstellt"
else
    ok "venv vorhanden"
fi
info "Installiere/aktualisiere Pakete ..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$BOT_DIR/requirements.txt"
ok "Pakete aktuell"

# ── 3. .env Datei ────────────────────────────────────────────────────────────
section "3. Konfiguration (.env)"
ENV_FILE="$BOT_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    ok ".env existiert — wird nicht ueberschrieben"
    MISSING=()
    for key in KRAKEN_API_KEY KRAKEN_SECRET TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do
        grep -q "^${key}=" "$ENV_FILE" || MISSING+=("$key")
    done
    if [[ ${#MISSING[@]} -gt 0 ]]; then
        warn "Fehlende Felder in .env: ${MISSING[*]}"
        warn "Bitte manuell in $ENV_FILE erganzen"
    fi
else
    info ".env wird angelegt — bitte Zugangsdaten eingeben:"
    echo ""
    read -r -p "  Kraken API Key:       " KRAKEN_KEY
    read -r -s -p "  Kraken Secret:        " KRAKEN_SEC; echo ""
    read -r -p "  Telegram Bot Token:   " TG_TOKEN
    read -r -p "  Telegram Chat ID:     " TG_CHAT
    printf "KRAKEN_API_KEY=%s\nKRAKEN_SECRET=%s\nTELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n" \
        "$KRAKEN_KEY" "$KRAKEN_SEC" "$TG_TOKEN" "$TG_CHAT" > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok ".env angelegt (chmod 600)"
fi

# ── 4. Verzeichnisse ─────────────────────────────────────────────────────────
section "4. Verzeichnisstruktur"
for dir in "$BOT_DIR/bot.conf.d" "$BOT_DIR/db" "$BOT_DIR/db/archive" "$BOT_DIR/logs" "$BOT_DIR/run"; do
    mkdir -p "$dir"
    ok "$dir"
done

# ── 5. scanner.conf ──────────────────────────────────────────────────────────
section "5. scanner.conf"
SCANNER_CONF="$BOT_DIR/scanner.conf"
if [[ -f "$SCANNER_CONF" ]]; then
    ok "scanner.conf existiert — wird nicht ueberschrieben"
    DRY=$(grep -E '^SCAN_DRY_RUN=' "$SCANNER_CONF" | tr -d '"' | cut -d= -f2 || echo "true")
    if [[ "$DRY" == "true" ]]; then
        warn "SCAN_DRY_RUN=true — Scanner fuehrt noch keine echten Aktionen aus"
    else
        ok "SCAN_DRY_RUN=false — Scanner ist live"
    fi
else
    info "scanner.conf wird mit sicheren Defaults angelegt (DRY_RUN=true) ..."
    cat > "$SCANNER_CONF" << 'SCANNER_CONF_EOF'
# scanner.conf — CAT-TRADING Trend Scanner Konfiguration
SCAN_INTERVAL_SECONDS="1800"
SCAN_MIN_VOLUME_EUR="500000"
SCAN_MIN_SCORE="4"
SCAN_MAX_BOTS="10"
SCAN_MIN_CAPITAL_PER_BOT="20"
SCAN_CONSECUTIVE_SL_THRESHOLD="3"
SCAN_CANDLE_TIMEFRAME="1h"
SCAN_CANDLE_LIMIT="250"
SCAN_RATE_LIMIT_SLEEP="0.5"
SCAN_WEB_API_URL="http://localhost:5001"
SCAN_DRY_RUN="true"
SCAN_LOG_LEVEL="INFO"
SCAN_BOT_ARGS="--live --sl 0.015 --tp 0.50 --trailing-sl --trailing-sl-pct 0.03 --breakeven --breakeven-pct 0.008 --partial-tp --partial-tp-fraction 0.5 --htf-timeframe 1h --htf-fast 21 --htf-slow 55 --startup-delay 60 --volume-factor 1.5"
SCAN_GRID_STEP=0.008
SCAN_GRID_LEVELS=3
SCAN_SAFETY_BUFFER=0.10
SCANNER_CONF_EOF
    ok "scanner.conf angelegt (SCAN_DRY_RUN=true)"
    warn "Nach 2-3 Testlaeufen SCAN_DRY_RUN auf false setzen"
fi

# ── 6. Alte systemd-Units deaktivieren ──────────────────────────────────────
section "6. Alte tradingbot@*.service deaktivieren"
OLD_UNITS=$(systemctl list-units --type=service --all --no-legend 2>/dev/null \
    | grep -E 'tradingbot@.+\.service' | awk '{print $1}' || true)
if [[ -n "$OLD_UNITS" ]]; then
    while IFS= read -r unit; do
        warn "Stoppe + deaktiviere: $unit"
        systemctl stop    "$unit" 2>/dev/null || true
        systemctl disable "$unit" 2>/dev/null || true
    done <<< "$OLD_UNITS"
    ok "Alte Units deaktiviert (Bots werden jetzt vom Scanner verwaltet)"
else
    ok "Keine alten tradingbot@*.service Units gefunden"
fi

# ── 7. Systemd-Services installieren ────────────────────────────────────────
section "7. Systemd-Services installieren/aktualisieren"
SYSTEMD_SRC="$BOT_DIR/systemd"
[[ -d "$SYSTEMD_SRC" ]] || fail "systemd/ Verzeichnis nicht gefunden in $BOT_DIR"

for svc in tradingbot.target tradingbot@.service tradingbot-web.service \
           tradingbot-supervisor.service tradingbot-scanner.service \
           tradingbot-grid@.service news-agent.service; do
    src="$SYSTEMD_SRC/$svc"
    dst="/etc/systemd/system/$svc"
    if [[ ! -f "$src" ]]; then
        warn "$svc nicht in $SYSTEMD_SRC — uebersprungen"
        continue
    fi
    if [[ -f "$dst" ]] && diff -q "$src" "$dst" >/dev/null 2>&1; then
        ok "$svc (unveraendert)"
    else
        cp "$src" "$dst"
        ok "$svc (installiert/aktualisiert)"
    fi
done
systemctl daemon-reload
ok "daemon-reload"

# ── 8. DB-Migrationen ───────────────────────────────────────────────────────
section "8. Datenbank-Migrationen"
DB_COUNT=0; MIGRATED=0
for db in "$BOT_DIR"/db/*.db; do
    [[ -f "$db" ]] || continue
    base=$(basename "$db")
    [[ "$base" =~ ^(scanner|supervisor|news|candles)\.db$ ]] && continue
    DB_COUNT=$((DB_COUNT + 1))
    result=$("$VENV/bin/python3" -c "
import sys
sys.path.insert(0, '$BOT_DIR')
try:
    from bot.persistence import StateDB
    StateDB('$db').close()
    print('ok')
except Exception as e:
    print('fehler: ' + str(e))
" 2>&1)
    if [[ "$result" == "ok" ]]; then
        MIGRATED=$((MIGRATED + 1))
        ok "$base"
    else
        warn "$base: $result"
    fi
done
if [[ $DB_COUNT -eq 0 ]]; then
    ok "Keine Bot-DBs vorhanden (Neuinstallation)"
else
    ok "$MIGRATED/$DB_COUNT Bot-DBs migriert"
fi

# ── 9. Services aktivieren + starten ────────────────────────────────────────
section "9. Services aktivieren und starten"
systemctl enable tradingbot.target tradingbot-web.service \
    tradingbot-supervisor.service tradingbot-scanner.service >/dev/null 2>&1

info "Starte tradingbot-web + tradingbot-supervisor ..."
systemctl restart tradingbot-web.service
systemctl restart tradingbot-supervisor.service
sleep 3

if curl -sf http://localhost:5001/api/bots >/dev/null 2>&1; then
    ok "Web-Dashboard erreichbar (http://localhost:5001)"
else
    warn "Web-Dashboard noch nicht erreichbar — evtl. kurz warten"
fi

info "Starte tradingbot-scanner ..."
systemctl restart tradingbot-scanner.service
sleep 2

# ── 10. Statusuebersicht ─────────────────────────────────────────────────────
section "10. Status"
for svc in tradingbot-web tradingbot-supervisor tradingbot-scanner; do
    status=$(systemctl is-active "$svc" 2>/dev/null || echo "unknown")
    if [[ "$status" == "active" ]]; then
        ok "$svc: $status"
    else
        warn "$svc: $status"
    fi
done

# ── Abschluss ────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}Migration abgeschlossen.${RESET}"
echo ""
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
echo -e "  Dashboard:    ${CYAN}http://${LOCAL_IP}:5001${RESET}"
echo -e "  Scanner-Log:  ${CYAN}tail -f $BOT_DIR/logs/scanner/scanner.log${RESET}"
echo -e "  Supervisor:   ${CYAN}journalctl -fu tradingbot-supervisor${RESET}"
echo ""
DRY=$(grep -E '^SCAN_DRY_RUN=' "$SCANNER_CONF" 2>/dev/null | tr -d '"' | cut -d= -f2 || echo "true")
if [[ "$DRY" == "true" ]]; then
    echo -e "${YELLOW}  NAECHSTER SCHRITT:${RESET} Scanner laeuft im Dry-Run-Modus."
    echo -e "  Nach 2-3 Zyklen (je 30 Min) in scanner.conf setzen:"
    echo -e "  ${CYAN}SCAN_DRY_RUN=\"false\"${RESET}  dann: ${CYAN}systemctl restart tradingbot-scanner${RESET}"
    echo ""
fi
