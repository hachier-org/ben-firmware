#!/usr/bin/env bash
# install.sh — BEN device setup script
# Usage: sudo ./install.sh <model> <hw-revision> <device-id>
# Example: sudo ./install.sh pi0-wired rev01 ben-0042
#
# Must be run as root (via sudo).
# Prerequisites:
#   - Network connectivity
#   - /tmp/ben-certs/ contains: device.crt, device.key, root-ca.crt
#     (staged by operator via ben-ops provisioning scripts)

set -euo pipefail

REPO_URL="https://github.com/xdegenne/ben-firmware.git"
REPO_PATH="/opt/ben/repo"
# == latest. §8 écrit un device.json CAPABILITIES-based (via caps_for_model) → un device neuf naît
# directement en capabilities (+ watchdog durci pour un lora). Le parc EXISTANT migre par OTA
# (0.5.0/0.6.0 → 0.6.1 → 0.7.0). Le chantier ben-ops (workflow opérateur) reste à part.
INITIAL_TAG="pi-0.15.0"
# Version écrite dans device.json — DOIT correspondre au tag checkout, sinon
# l'OTA re-grimpe depuis une version périmée. Dérivée de INITIAL_TAG pour
# qu'elles ne puissent jamais diverger (ex. pi-0.0.28 → 0.0.28).
INITIAL_VERSION="${INITIAL_TAG#pi-}"

if [ $# -ne 3 ]; then
    echo "Usage: sudo ./install.sh <model> <hw-revision> <device-id>" >&2
    exit 1
fi

MODEL=$1
HW_REV=$2
DEVICE_ID=$3

# --------------------------------------------------------------------------
# 1. Validate arguments
# --------------------------------------------------------------------------
if [[ ! "$MODEL" =~ ^(pi0-wired|pi0-lora|pi0-lora-wired)$ ]]; then
    echo "ERROR: MODEL must be pi0-wired, pi0-lora, or pi0-lora-wired (got: $MODEL)" >&2
    exit 1
fi
if [[ ! "$HW_REV" =~ ^rev[0-9]{2}$ ]]; then
    echo "ERROR: HW_REV must match rev[0-9]{2} (got: $HW_REV)" >&2
    exit 1
fi
if [[ ! "$DEVICE_ID" =~ ^ben-[0-9]{4}$ ]]; then
    echo "ERROR: DEVICE_ID must match ben-[0-9]{4} (got: $DEVICE_ID)" >&2
    exit 1
fi

echo "[1/13] Arguments: $DEVICE_ID  model=$MODEL  hw=$HW_REV"

# --------------------------------------------------------------------------
# 2. Install system dependencies + locale
# --------------------------------------------------------------------------
apt-get update -qq
# mosquitto : broker MQTT LOCAL du bus façade radio (ben-radio ↔ ben-telemetry).
# python3-paho-mqtt : lib CLIENTE MQTT dont ben-radio ET ben-telemetry ont besoin (import paho).
#   SANS elle → ben-radio crashe à l'import → crash-loop → escalade reboot → BOUCLE INFERNALE
#   (incident device-neuf ben-0011, 2026-07-22). Les deux sont requis dès qu'il y a lora-tic-receiver.
apt-get install -y git python3 python3-pip python3-dbus python3-gi mosquitto python3-paho-mqtt
timedatectl set-timezone Europe/Paris
echo "[2/13] System dependencies installed (incl. BLE provisioning + mosquitto + paho-mqtt), timezone=Europe/Paris"

# --------------------------------------------------------------------------
# 2b. Configure UART (models with wired TIC) + LED RGB boot indicator
# --------------------------------------------------------------------------
# Pi OS Bookworm/Trixie : /boot/firmware/config.txt est le vrai fichier.
# /boot/config.txt n'est qu'un stub ("DO NOT EDIT, moved to /boot/firmware/config.txt").
# Sur Bullseye et antérieur seul /boot/config.txt existait — fallback si firmware/ absent.
if [ -f /boot/firmware/config.txt ]; then
    CONFIG_TXT="/boot/firmware/config.txt"
else
    CONFIG_TXT="/boot/config.txt"
fi

# Strip les gpio= R (12) et B (16) baked dans le golden pour la LED blanche du ben-zero.
# Le vert (GPIO13) reste comme boot indicator firmware (allumé avant l'OS).
# Le service ben-led-release.service libère ensuite ce pin du firmware → permet aux
# services BEN (check_network, tic-reader, …) de faire leur setup PWM proprement.
sed -i '/^gpio=12=op,dh$/d; /^gpio=16=op,dh$/d' "$CONFIG_TXT"
grep -q "gpio=13=op,dh" "$CONFIG_TXT" || echo "gpio=13=op,dh" >> "$CONFIG_TXT"
echo "[2b/13] LED RGB boot indicator GPIO13 conservé, GPIO12/16 strippés"

if [ "$MODEL" = "pi0-wired" ] || [ "$MODEL" = "pi0-lora-wired" ]; then
    # miniuart-bt déplace le BT sur ttyS0 et libère le PL011 (ttyAMA0) pour la TIC.
    grep -q "dtoverlay=miniuart-bt" "$CONFIG_TXT" || echo "dtoverlay=miniuart-bt" >> "$CONFIG_TXT"
    grep -q "enable_uart=1"         "$CONFIG_TXT" || echo "enable_uart=1"         >> "$CONFIG_TXT"

    # cmdline.txt : retire la directive `console=serial0,*` (et son alias `ttyAMA0`).
    # Sans ce patch, le kernel claim /dev/ttyAMA0 comme console série debug
    # → crw------- root root → SerialException Permission denied côté agent.
    CMDLINE_TXT="${CONFIG_TXT%/config.txt}/cmdline.txt"
    sed -i 's/console=serial0,[0-9]* *//g; s/console=ttyAMA0,[0-9]* *//g' "$CMDLINE_TXT"
    echo "[2b/13] UART configuré (ttyAMA0 TIC, BT sur ttyS0, kernel console libérée)"
fi

if [ "$MODEL" = "pi0-lora" ] || [ "$MODEL" = "pi0-lora-wired" ]; then
    # SPI requis pour parler à la RFM95 (module LoRa). Sans ça : /dev/spidev* absent
    # → raspi_lora throw "Failed to add edge detection" → mode sans radio.
    grep -q "^dtparam=spi=on" "$CONFIG_TXT" || echo "dtparam=spi=on" >> "$CONFIG_TXT"
    echo "[2b/13] SPI activé pour LoRa (dtparam=spi=on)"
fi

# --------------------------------------------------------------------------
# 3. Create ben user + group memberships
# --------------------------------------------------------------------------
if ! id -u ben &>/dev/null; then
    useradd --system --create-home --shell /bin/bash ben
fi
# dialout : /dev/ttyAMA0 (TIC). gpio + spi : LoRa receiver via raspi_lora + RPi.GPIO.
#
# 🚨 systemd-journal : SANS LUI, `journalctl` NE REND RIEN à `ben`. Mesuré le 2026-10-01 sur
#    un boîtier du parc — « No journal files were opened due to insufficient permissions »,
#    sur stderr, donc invisible pour qui ne lit que stdout. L'instantané de santé du hello en
#    dépendait : son champ `errors` était mort-né sur TOUS les boîtiers, et les essais
#    passaient parce qu'on les lançait en `pi`, qui est dans `adm`.
#    ⭐ C'est là qu'on perd les lignes NOYAU — blocages SPI, sous-tensions, et le
#       `brcmfmac: resumed on timeout` du pilote WiFi qu'on a découvert ce jour-là.
usermod -aG dialout,gpio,spi,systemd-journal ben
echo "[3/13] User ben OK (dialout, gpio, spi, systemd-journal)"

# --------------------------------------------------------------------------
# 4. Configure sudo rights
# --------------------------------------------------------------------------
echo "ben ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/ben-firmware
chmod 440 /etc/sudoers.d/ben-firmware
echo "[4/13] Sudo rights OK"

# --------------------------------------------------------------------------
# 5. Clone repo and checkout initial version
# --------------------------------------------------------------------------
mkdir -p /opt/ben
git clone "$REPO_URL" "$REPO_PATH"
git -C "$REPO_PATH" fetch --tags
git -C "$REPO_PATH" checkout "$INITIAL_TAG"
chown -R ben:ben /opt/ben
echo "[5/13] Repo cloned and checked out at $INITIAL_TAG"

# --------------------------------------------------------------------------
# 6. Create directory structure
# --------------------------------------------------------------------------
mkdir -p /etc/ben-firmware/certs
mkdir -p /etc/ben-firmware/gpg
mkdir -p /var/lib/ben-firmware
mkdir -p /var/log/ben-firmware
chown -R ben:ben /etc/ben-firmware /var/lib/ben-firmware /var/log/ben-firmware
echo "[6/13] Directories OK"

# --------------------------------------------------------------------------
# 7. Install certificates and HMAC key
# --------------------------------------------------------------------------
cp /tmp/ben-certs/device.crt              /etc/ben-firmware/certs/
cp /tmp/ben-certs/device.key              /etc/ben-firmware/certs/
cp "$REPO_PATH/config/etc/ca/root-ca.crt" /etc/ben-firmware/certs/
chown -R ben:ben /etc/ben-firmware/certs
chmod 600 /etc/ben-firmware/certs/device.key
chmod 644 /etc/ben-firmware/certs/device.crt /etc/ben-firmware/certs/root-ca.crt

cp /tmp/ben-certs/hmac.key /etc/ben-firmware/hmac.key
chown ben:ben /etc/ben-firmware/hmac.key
chmod 600 /etc/ben-firmware/hmac.key
echo "[7/13] Certificates and HMAC key installed"

# --------------------------------------------------------------------------
# 8. Write device.json — CAPABILITIES-based (dérivé du MODEL)
# --------------------------------------------------------------------------
# Le device naît en capabilities, comme un device migré : source de vérité UNIQUE
# model→caps = src/pi/capabilities.py (idem migration OTA). `model`/`hardwareRevision`
# conservés le temps de la transition (code local_api/db encore model-based). Le fw de
# l'émetteur est porté par la capa lora-tic-receiver (fini le champ arduinoFirmwareVersion).
DJ_TMP=$(mktemp)
python3 "$REPO_PATH/src/pi/capabilities.py" device-json \
    "$DEVICE_ID" "$MODEL" "$HW_REV" "$INITIAL_VERSION" > "$DJ_TMP"
mv "$DJ_TMP" /etc/ben-firmware/device.json
chown ben:ben /etc/ben-firmware/device.json

# sources.json — PDL source routing (lora_address → pdl_index)
# Arduino CLIENT_ADDRESS is always 0x1f (31) on first provisioning.
if [ "$MODEL" = "pi0-lora" ]; then
    cat > /etc/ben-firmware/sources.json <<'EOF'
{
  "sources": [
    {"type": "lora", "lora_address": "0x1f", "index": 0}
  ]
}
EOF
elif [ "$MODEL" = "pi0-lora-wired" ]; then
    cat > /etc/ben-firmware/sources.json <<'EOF'
{
  "sources": [
    {"type": "wired", "index": 0},
    {"type": "lora", "lora_address": "0x1f", "index": 1}
  ]
}
EOF
else
    cat > /etc/ben-firmware/sources.json <<'EOF'
{
  "sources": [
    {"type": "wired", "index": 0}
  ]
}
EOF
fi
chown ben:ben /etc/ben-firmware/sources.json
echo "[8/13] device.json and sources.json written"

# --------------------------------------------------------------------------
# 9. Import GPG release signing key
# --------------------------------------------------------------------------
# Public key ships with the repo — no secret.
cp "$REPO_PATH/config/etc/gpg/ben-releases.pub" /etc/ben-firmware/gpg/
chown ben:ben /etc/ben-firmware/gpg/ben-releases.pub
sudo -u ben gpg --import /etc/ben-firmware/gpg/ben-releases.pub
echo "[9/13] GPG key imported"

# --------------------------------------------------------------------------
# 10. Install Python dependencies
# --------------------------------------------------------------------------
pip3 install --break-system-packages -r "$REPO_PATH/src/pi/requirements.txt"
echo "[10/13] Python dependencies installed"

# Force uninstall RPi.GPIO : raspi_lora le déclare en dépendance setup.py, donc
# pip l'installe automatiquement même si requirements.txt a rpi-lgpio. Le pip
# RPi.GPIO finit dans /usr/local et shadow le rpi-lgpio système (apt) qui est
# le seul compatible avec le chardev GPIO du kernel Trixie 6.12+. Sans cette
# désinstallation, ben-radio crashe avec "Failed to add edge detection".
if [ "$MODEL" = "pi0-lora" ] || [ "$MODEL" = "pi0-lora-wired" ]; then
    pip3 uninstall -y --break-system-packages RPi.GPIO 2>/dev/null || true
    echo "[10a/13] RPi.GPIO transitif uninstall (rpi-lgpio prend la main)"
fi

# --------------------------------------------------------------------------
# 10b. Patch raspi_lora SNR sign-handling (modèles LoRa uniquement)
# --------------------------------------------------------------------------
# Bug upstream raspi_lora (lora.py:248) : `snr = _spi_read(...) / 4` ne gère
# pas le signe du byte. Le registre 0x19 PKT_SNR_VALUE du SX1276 est signé
# 8-bit, mais `_spi_read` retourne un unsigned 0-255. Pour SNR négatifs (signal
# distant), la lib produit une valeur positive aberrante au lieu de négatif
# → tout le calcul RSSI (qui dépend du signe du SNR) part en vrille.
# Le firmware ben détecte le cas via SNR_MAX_PLAUSIBLE et logge un warning.
# Ce patch fixe l'interprétation du byte signé.
if [ "$MODEL" = "pi0-lora" ] || [ "$MODEL" = "pi0-lora-wired" ]; then
    RASPI_LORA_PY=$(python3 -c "import raspi_lora, os; print(os.path.join(os.path.dirname(raspi_lora.__file__), 'lora.py'))" 2>/dev/null || true)
    if [ -n "$RASPI_LORA_PY" ] && [ -f "$RASPI_LORA_PY" ] && ! grep -q "snr -= 256" "$RASPI_LORA_PY"; then
        cp "$RASPI_LORA_PY" "${RASPI_LORA_PY}.bak"
        sed -i 's|            snr = self._spi_read(REG_19_PKT_SNR_VALUE) / 4|            snr = self._spi_read(REG_19_PKT_SNR_VALUE)\n            if snr > 127:\n                snr -= 256\n            snr = snr / 4|' "$RASPI_LORA_PY"
        echo "[10b/13] raspi_lora SNR sign-handling patché ($RASPI_LORA_PY)"
    fi
fi

# --------------------------------------------------------------------------
# 11. Install systemd units
# --------------------------------------------------------------------------
cp "$REPO_PATH/config/systemd/"*.service /etc/systemd/system/
cp "$REPO_PATH/config/systemd/"*.timer   /etc/systemd/system/
systemctl daemon-reload
# Annonce mDNS du boîtier (_ben._tcp :8087) → découverte par l'app sur le LAN.
mkdir -p /etc/avahi/services
cp "$REPO_PATH/config/avahi/ben.service" /etc/avahi/services/ben.service
echo "[11/13] Systemd units + mDNS service installed"

# --------------------------------------------------------------------------
# 12. Rename hostname to match deviceId
# --------------------------------------------------------------------------
hostnamectl set-hostname "$DEVICE_ID"
sed -i '/^127\.0\.1\.1/d' /etc/hosts
echo -e "127.0.1.1\t$DEVICE_ID" >> /etc/hosts
echo "[12/13] Hostname renamed: $DEVICE_ID"

# --------------------------------------------------------------------------
# 13. Enable and start services (model-dependent)
# --------------------------------------------------------------------------
systemctl enable ben-update.timer
systemctl start  ben-update.timer

# Broker MQTT LOCAL (bus façade radio ben-radio ↔ ben-telemetry) — 127.0.0.1, persistence off
# (zéro écriture SD). Ordering au boot géré par After=/Wants=mosquitto dans ben-radio.service.
install -m 644 "$REPO_PATH/config/mosquitto/ben.conf" /etc/mosquitto/conf.d/ben.conf
systemctl enable mosquitto
systemctl restart mosquitto || true

# Libération des pins LED RGB (12/13/16) du firmware au boot, avant tous les
# autres services BEN. Sans ça check_network rate son PWM setup.
systemctl enable ben-led-release.service

# Décideur boot-time : ping Internet → start ben-ble-provisioner si KO.
# ben-ble-provisioner.service reste 'static' (démarré on-demand).
systemctl enable ben-network-check.service

# API locale read-only (:8087) lue par l'app — tous modèles.
# ⚠️ CE FICHIER N'ÉTAIT POSÉ PAR PERSONNE ICI, et c'est un défaut latent trouvé le 2026-10-07 :
#    il avait été posé au parc par `updates/0.9.15_to_0.9.16/update.sh`, donc tout boîtier
#    provisionné DEPUIS en était dépourvu. Sans lui, `ben-certd` ne peut pas redémarrer ce qui
#    tient le certificat après l'avoir remplacé — et le boîtier servirait un certificat périmé
#    EN SILENCE. Même classe de défaut que le watchdog : un fichier dans le dépôt que la liste
#    explicite d'`install.sh` avait oublié.
# 🚨 `visudo -c` AVANT de poser : un /etc/sudoers.d/ cassé casse sudo pour tout le monde.
visudo -c -f "$REPO_PATH/config/etc/sudoers.d/ben-certd" >/dev/null \
  || { echo "sudoers ben-certd INVALIDE — non posé"; exit 1; }
install -m 440 -o root -g root "$REPO_PATH/config/etc/sudoers.d/ben-certd" /etc/sudoers.d/ben-certd

systemctl enable ben-local-api.service
systemctl start  ben-local-api.service || true

# Profileur du niveau de conso (percentiles PAPP → table level_profile), 1×/jour.
# 🚨 LE WATCHDOG RÉSEAU — et l'ORDRE DES DEUX GESTES N'EST PAS INDIFFÉRENT.
#
#   `cp config/systemd/*` ci-dessus a déjà posé `wifi-watchdog.service` et son timer : l'unité est
#   donc `loaded`, et son `ExecStart` pointe vers `/usr/local/bin/wifi_watchdog.sh`. Activer le
#   timer AVANT de copier le script produirait un échec toutes les 2 minutes, indéfiniment.
#
# ⚠️ C'EST LA LIGNE QUI MANQUAIT, et elle a coûté 8 h 40 de cécité sur ben-0005 le 2026-10-06
#    (`ben-docs#17`) : le `cp` ratissait les unités, mais la liste des `enable` est explicite — un
#    par un — et le watchdog n'y était pas. D'où une unité `loaded` sur les 8 boîtiers et un timer
#    `disabled` sur les 8. Les deux faits avaient l'air de se contredire.
#
# ⓘ Le script ne fait RIEN tant que le boîtier n'est pas déballé : sa garde ① sort immédiatement
#   s'il n'y a pas de connexion `ben-provisioned`. Il est donc sans danger de l'activer ici, avant
#   même le provisioning.
# ⚠️ Et ses deux autres gardes couvrent la radio sur un boîtier DÉJÀ déballé — la radio est partagée
#    WiFi/BLE sur Pi Zero W. ⚠️ Un drapeau « téléphone connecté » n'y suffisait PAS : pendant les
#    300 s où `ben-network-recovery` ne fait qu'OFFRIR le BLE, personne n'est connecté et il n'y a
#    donc aucun drapeau. ⇒ la garde ③ s'abstient dès que `ben-ble-provisioner` ou
#    `ben-network-recovery` est actif.
install -m 755 "$REPO_PATH/src/pi/wifi-watchdog/wifi_watchdog.sh" /usr/local/bin/wifi_watchdog.sh
systemctl enable wifi-watchdog.timer
systemctl start  wifi-watchdog.timer || true

systemctl enable ben-level-profiler.timer
systemctl start  ben-level-profiler.timer || true

# Pont MQTT → Home Assistant : activé PARTOUT, mais inerte tant que
# /etc/ben-firmware/mqtt-ha.json n'existe pas (ConditionPathExists → unité sautée).
systemctl enable ben-mqtt-ha.service

# Les agents de mesure (voir capabilities.CAP_SERVICES) ne sont PAS enable :
# pas de WantedBy → pas d'autostart au boot. C'est ben-network-check.service
# (check_network.py) qui les démarre, et seulement si le réseau est up. Sinon
# ils tournaient au boot en doublon et tuaient ben-ble-provisioner (Conflicts)
# → bug "code couleur en boucle". Après ce provisioning, le device reboot puis
# part en BLE (jamais provisionné → pas de connexion ben-provisioned).

echo "[13/13] Services enabled (readers gérés par check_network, pas d'autostart)"

# --------------------------------------------------------------------------
# Verify
# --------------------------------------------------------------------------
echo ""
echo "--- Status ---"
systemctl is-active ben-update.timer && echo "ben-update.timer: active"
echo ""
echo "BEN device $DEVICE_ID ($MODEL / $HW_REV) provisioned successfully."
