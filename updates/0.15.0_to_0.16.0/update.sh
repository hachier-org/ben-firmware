#!/usr/bin/env bash
# update.sh — 0.15.0 → pi-0.16.0 : HOME ASSISTANT EN PUSH, PAR MQTT (opt-in).
#
# ═══ CE QUE ÇA CHANGE ═════════════════════════════════════════════════════════════════════════
#
#   Nouveau service `ben-mqtt-ha` : pousse l'état du boîtier vers le broker MQTT de Home
#   Assistant, avec MQTT Discovery — HA crée les entités seul, aucun composant à installer.
#   Les lecteurs publient `ben/local/stored` sur le bus LOCAL après chaque commit ; c'est ce
#   signal qui déclenche la publication (store/bus.py). La forme de `/live` sort de
#   local_api.py vers store/live.py, partagée par l'API et le pont.
#
# ═══ ⭐ RIEN NE CHANGE POUR UN BOÎTIER SANS HOME ASSISTANT ═══════════════════════════════════
#
#   L'unité est installée et ACTIVÉE partout, mais `ConditionPathExists=` la SAUTE tant que
#   /etc/ben-firmware/mqtt-ha.json n'existe pas. Le signal du bus est QoS 0, sans abonné il
#   ne coûte rien, et il ne peut pas faire échouer une écriture (bus.publish_stored ne lève
#   jamais). `/live` rend octet pour octet la même chose (banc test_live.py).
#
#   ⓘ AUCUNE migration de schéma : le pont ouvre la base en LECTURE SEULE.
#
# ═══ RETOUR ARRIÈRE ═══════════════════════════════════════════════════════════════════════════
#
#   git checkout pi-0.15.0, puis :
#     sudo systemctl disable --now ben-mqtt-ha ; sudo rm /etc/systemd/system/ben-mqtt-ha.service
#     sudo systemctl daemon-reload
#     sudo systemctl try-restart ben-telemetry ben-tic-reader ben-local-api
#   Les retained publiés sur le broker HA y restent : les effacer côté broker si besoin.

set -euo pipefail
TR="→ pi-0.16.0"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*" >&2; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
SRC="$REPO/src/pi"
API="http://127.0.0.1:8087"
UNIT=ben-mqtt-ha.service

# ── préflight ⓞ : sudo ───────────────────────────────────────────────────────────────────────
sudo -n true 2>/dev/null \
    || fail "l'agent (ben) n'a pas de sudo sans mot de passe — /etc/sudoers.d/ben-firmware manque"
log "préflight ⓞ OK (sudo sans mot de passe pour $(id -un))"

# ── préflight ① : le code livré est là et compile ──────────────────────────────────────────────
PY="store/live.py store/bus.py store/local_api.py ben-telemetry/ben_telemetry.py
    tic-reader/main_uart.py ben-mqtt-ha/ben_mqtt_ha.py"
for rel in $PY; do
    [ -f "$SRC/$rel" ] || fail "absent du dépôt : $SRC/$rel (checkout pi-0.16.0 incomplet ?)"
done
[ -f "$REPO/config/systemd/$UNIT" ] || fail "absent du dépôt : config/systemd/$UNIT"
# shellcheck disable=SC2086
python3 - "$SRC" $PY <<'PYEOF' || fail 'un fichier Python livré ne compile pas'
import ast, pathlib, sys
base = pathlib.Path(sys.argv[1])
for rel in sys.argv[2:]:
    p = base / rel
    ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
print(f"  {len(sys.argv) - 2} fichiers Python compilent")
PYEOF
log "préflight ① OK (fichiers présents, syntaxe vérifiée)"

# ── préflight ② : les bancs, sur le Python DU BOÎTIER ──────────────────────────────────────────
for banc in store/test_live.py store/test_bus.py ben-mqtt-ha/test_ben_mqtt_ha.py; do
    ( cd "$SRC/$(dirname "$banc")" && TMPDIR=/var/tmp python3 "$(basename "$banc")" >/dev/null 2>&1 ) \
        || fail "le banc $banc échoue sur ce boîtier — NE PAS déployer"
done
log "préflight ② OK (bancs live / bus / ben-mqtt-ha verts)"

# ── 1. L'unité : posée et activée partout, inerte sans config ──────────────────────────────────
sudo install -m 644 "$REPO/config/systemd/$UNIT" "/etc/systemd/system/$UNIT"
sudo systemctl daemon-reload
sudo systemctl enable "$UNIT" >/dev/null 2>&1 || fail "systemctl enable $UNIT a échoué"
log "unité $UNIT posée et activée (sautée tant que /etc/ben-firmware/mqtt-ha.json manque)"

# ── 2. Redémarrages : seulement ce qui TOURNE ──────────────────────────────────────────────────
# 🚨 `try-restart`, PAS `restart`. ben-tic-reader est installé sur TOUS les modèles mais n'est
#    démarré que sur un filaire (par ben-network-check). Un `restart` le DÉMARRERAIT sur un
#    boîtier radio, à se battre pour un UART où rien n'arrive.
for U in ben-telemetry ben-tic-reader ben-local-api; do
    if systemctl is-active --quiet "$U"; then
        sudo systemctl try-restart "$U" && log "$U redémarré"
    fi
done
if [ -f /etc/ben-firmware/mqtt-ha.json ]; then
    sudo systemctl restart "$UNIT" && log "$UNIT (re)démarré — config présente"
fi

# ── contrôle d'effet : l'API sert, et /live a gardé sa forme ───────────────────────────────────
python3 - "$API" <<'PYEOF' || fail ":8087 ne sert pas — l'app du parc et Home Assistant en dépendent"
import json, sys, time, urllib.request
api, dernier = sys.argv[1], "aucune tentative"
for _ in range(12):           # 12 × 5 s = 60 s
    try:
        with urllib.request.urlopen(f"{api}/health", timeout=5) as r:
            h = json.load(r)
        with urllib.request.urlopen(f"{api}/live?pdl_index=0", timeout=10) as r:
            live = json.load(r)
    except Exception as e:
        dernier = f"injoignable : {e}"; time.sleep(5); continue
    if not h.get("db"):
        print(f"  /health répond mais db=false : {h}", file=sys.stderr); sys.exit(1)
    if not isinstance(live, list):
        print(f"  /live ne rend plus un tableau : {live!r:.200}", file=sys.stderr); sys.exit(1)
    print(f"  :8087 OK — db lisible, /live rend {len(live)} ligne(s)")
    sys.exit(0)
print(f"  {dernier}", file=sys.stderr); sys.exit(1)
PYEOF
log "✓ :8087 sert, /live intact"
log "✓ update OK"
