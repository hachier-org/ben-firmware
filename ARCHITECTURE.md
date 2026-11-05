# BEN Device — Architecture

Reference document for the embedded software architecture running on BEN devices (Raspberry Pi Zero and Arduino).

---

## 1. Device Models

| Model | Hardware | Description |
|---|---|---|
| `pi0-lora` | Arduino Pro Mini + Raspberry Pi Zero W | Arduino reads TIC, sends over LoRa to Pi. Pi publishes to cloud. |
| `pi0-wired` | Raspberry Pi Zero W only | Pi reads TIC directly (wired), publishes to cloud. |
| `pi0-lora-wired` | Arduino Pro Mini + Raspberry Pi Zero W | Pi reads TIC directly (wired) AND receives LoRa from one or more Arduino emitters. Handles multiple PDLs simultaneously. |

Each model has a hardware revision (`rev01`, `rev02`, ...) tracked in `device.json`.

---

## 2. Repository Structure

```
ben-firmware/
  src/
    arduino/
      tic-reader/            # Arduino sketch (pi0-lora only)
    pi/
      lora-receiver/         # LoRa frame/curve codecs, shared by ben-radio & ben-telemetry
      ben-radio/             # SX127x owner: RX/TX + LED (lora-tic-receiver capability)
      ben-telemetry/         # Decodes & stores frames, no radio (lora-tic-receiver capability)
      tic-reader/            # Direct TIC reader (pi0-wired only)
      publisher/             # Cloud publisher (all models)
      ben-mqtt-ha/           # Opt-in push to Home Assistant over MQTT (all models)
      updater/
        check_update.py      # Permanent OTA agent
        update_lib.py        # Compatibility parsing, version comparison
      provisioner/
        check_network.py     # Boot-time connectivity check + BLE provisioning trigger
      registrar/
        registrar.py         # One-shot: registers device with backend, drives RGB LED verification

  updates/
    pi0-lora/
      1.0.0_to_1.1.0/
        update.sh
    pi0-wired/
      1.0.0_to_1.1.0/
        update.sh

  config/
    systemd/                 # .service and .timer unit files
    etc/                     # templates deployed to /etc/ben-firmware/

  tools/
    find_next_update.py      # CLI helper: find next applicable transition

  install.sh                 # Device setup script — run once at provisioning time
  compatibility.yaml         # Hardware/software compatibility matrix
  ARCHITECTURE.md
  LICENSE
```

---

## 3. Device Identity — `device.json`

Stored at `/etc/ben-firmware/device.json` on each device.

```json
{
  "deviceId": "ben-0042",
  "model": "pi0-lora",
  "hardwareRevision": "rev01",
  "softwareVersion": "1.1.0"
}
```

- `hardwareRevision` follows `rev01`, `rev02`, ... convention
- `softwareVersion` refers to the Pi agent version
- 🚨 `arduinoFirmwareVersion` **is gone, and must not come back** (#28). It was a
  hand-maintained, box-wide claim about a firmware that ships by **physical reflash** — so it
  could not stay true, and it did not: it still read `0.1.3` while the fleet ran `0.1.8`. The
  emitter firmware version is now **measured**, reported by the emitter itself in its boot
  frame (TLV `T_FW`), stored **per emitter** in `emitter.fw_version`, and surfaced to the
  cloud in `health.emitter[].fw`. A box may listen to **several** emitters, each with its own
  version — which is the other reason a single box-level field was wrong.

---

## 4. Versioning and Tagging

Pi agent and Arduino firmware are versioned independently.

Git tags:
```
pi-1.0.0
pi-1.1.0
pi-1.2.0
arduino-1.0.0
arduino-1.1.0
```

**All tags must be GPG-signed.** Devices verify the signature before applying any update.
The GPG public key is embedded on each device at provisioning time.

Release workflow:
```
1. git tag -s pi-1.2.0 -m "release pi-1.2.0"
2. git push origin pi-1.2.0
3. bump `latest` in compatibility.yaml
4. git commit -am "release: pi-1.2.0"
5. git push origin main
```

Devices detect the update within one polling cycle (default: 10 minutes).

---

## 5. `compatibility.yaml`

Lives in `main`. Source of truth for all versions and update paths.

```yaml
devices:

  pi0-lora:
    rev01:
      pi:
        latest: "1.2.0"
        history:
          - version: "1.0.0"
            released: "2026-05-01"
          - version: "1.1.0"
            released: "2026-05-10"
          - version: "1.2.0"
            released: "2026-05-18"
      arduino:
        minimum: "1.0.0"
        latest: "1.1.0"
        history:
          - version: "1.0.0"
            released: "2026-05-01"
          - version: "1.1.0"
            released: "2026-05-15"

  pi0-wired:
    rev01:
      pi:
        latest: "1.0.0"
        history:
          - version: "1.0.0"
            released: "2026-05-18"

updates:

  pi0-lora:
    - from: "1.0.0"
      to: "1.1.0"
      tag: "pi-1.1.0"
      script: "updates/pi0-lora/1.0.0_to_1.1.0/update.sh"
      requires:
        hardwareRevision:
          minimum: "rev01"

    - from: "1.1.0"
      to: "1.2.0"
      tag: "pi-1.2.0"
      script: "updates/pi0-lora/1.1.0_to_1.2.0/update.sh"
      requires:
        hardwareRevision:
          minimum: "rev01"

  pi0-wired:
    []
```

---

## 6. OTA Update Agent — `check_update.py`

Permanent Python agent on every device. Triggered by a systemd timer once a day.

```ini
[Timer]
OnBootSec=5min           # first run 5 min after boot
OnUnitActiveSec=24h      # then once every 24h
RandomizedDelaySec=1h    # random offset to avoid thundering herd across the fleet
```

### One transition per tick

The agent applies **at most one transition per run**. A device that is 3 versions behind catches up over 3 ticks (~30 minutes). This ensures:
- the service is restarted cleanly between migrations
- the lockfile is released between ticks
- failures are isolated to a single transition

### Full sequence

```
1. Acquire lockfile /var/run/ben-update.lock (exit immediately if locked)
2. Read /etc/ben-firmware/device.json
3. Fetch compatibility.yaml from origin/main
4. Find next applicable transition:
   - model and hardwareRevision must match
   - `from` must equal current softwareVersion
   - hardwareRevision must satisfy `requires.hardwareRevision.minimum`
5. If no transition → exit (already up to date or hardware incompatible)
6. Verify GPG signature of the target tag
7. Verify SHA256 checksum of update.sh
8. Execute update.sh
9. If success → update softwareVersion in device.json
10. Release lockfile
```

### Lockfile

```python
import fcntl

lock = open("/var/run/ben-update.lock", "w")
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("[check_update] Already running, exiting")
    exit(0)
```

### Failure handling

- If `update.sh` exits non-zero → `device.json` is NOT updated
- Device stays on current version
- Next tick will retry the same transition
- Errors are logged to `/var/log/ben-firmware/update.log`

---

## 7. `update.sh` — Transition Script

Each transition provides its own `update.sh`. Runs as the `ben` user with full sudo rights (see §11).

### Local repo strategy

The device keeps a **single permanent Git repo** at `/opt/ben/repo/`. Updates are applied via `git fetch` + `git checkout` — only the diff is downloaded, not the full codebase. This is critical for Pi Zero on residential connections.

```bash
#!/usr/bin/env bash
set -euo pipefail

cd /opt/ben/repo
git fetch --tags
git verify-tag pi-1.2.0
git checkout pi-1.2.0

# Install new dependencies if needed
sudo pip install -r requirements.txt

# Restart service
sudo systemctl restart ben-agent.service
```

Rollback is simply `git checkout pi-1.1.0` + service restart.

### Golden rule: never modify `/opt/ben/repo/`

`update.sh` must **never write files inside `/opt/ben/repo/`**. Any write would conflict with the next `git checkout` and either fail or silently overwrite changes.

Files that change at runtime must live outside the repo:

| Path | Purpose |
|---|---|
| `/etc/ben-firmware/` | config, `device.json`, certificates |
| `/var/lib/ben-firmware/` | persistent state |
| `/var/log/ben-firmware/` | logs |

### Installing new services

A transition script can install new systemd services. Service unit files live in the repo and are copied to `/etc/systemd/system/` — outside the repo, respecting the golden rule.

```bash
sudo cp /opt/ben/repo/pi/new-service/new-service.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable new-service.service
sudo systemctl start new-service.service
```

Add `systemctl daemon-reload` and `systemctl enable/start` to the allowed sudo commands in `/etc/sudoers.d/ben-firmware` if needed.

### Runtime detection

`update.sh` can detect the hardware platform and OS version at runtime and adapt accordingly. This avoids over-engineering `compatibility.yaml` for minor differences.

```bash
# Detect OS
OS_VERSION=$(. /etc/os-release && echo "$VERSION_CODENAME")
# → "bullseye", "bookworm", etc.

# Detect hardware
HW_MODEL=$(cat /proc/device-tree/model)
# → "Raspberry Pi Zero W Rev 1.1"
# → "Raspberry Pi Zero 2 W Rev 1.0"
```

**Rule of thumb:**
- Use `compatibility.yaml` constraints (`requires.hardwareRevision`) for **hard blockers** — a transition that cannot run at all on a given hardware/OS combination.
- Use runtime detection inside `update.sh` for **soft adaptations** — different package names, different config paths, minor behavioural differences.

### Idempotency

`update.sh` must be idempotent — it may run twice if the process crashes between the script completing and `device.json` being updated. Every operation must be safe to run a second time without error or inconsistent state.

### Example: local-to-cloud migration

Early prototype devices ship with a local InfluxDB + Grafana stack (data visible locally, no cloud push). A future OTA transition will migrate them to the cloud publisher model. This is a concrete example of what `update.sh` must be able to do:

```bash
# Stop and disable local stack
sudo systemctl stop grafana-server influxdb || true
sudo systemctl disable grafana-server influxdb || true

# Remove packages
sudo apt-get remove -y grafana influxdb

# Install SQLite (local buffer for offline resilience)
sudo apt-get install -y python3-sqlite3

# Install and enable cloud publisher
sudo cp /opt/ben/repo/config/systemd/ben-publisher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ben-publisher.service
sudo systemctl start ben-publisher.service
```

Key points:
- `|| true` on stop/disable makes the script safe to run twice (service may already be stopped)
- `apt-get remove` is idempotent if the package is already absent
- The local InfluxDB data is left in place (not deleted) — the customer's history is not destroyed

---

## 8. Self-update of the Updater

`check_update.py` updates itself automatically as part of the normal `git checkout` in `update.sh` — no special mechanism needed. The new version is active at the next timer tick.

---

## 9. Filesystem Layout on Device

```
/etc/ben-firmware/
  device.json
  sources.json               # PDL sources: maps pdl_index to source type + LoRa address
  certs/
    device.crt
    device.key
    root-ca.crt
  gpg/
    ben-releases.pub          # GPG public key for tag verification
  hmac.key                    # HMAC-SHA256 key shared with all LoRa emitters of this device (32 bytes hex)

/opt/ben/
  repo/                                 # permanent git repo, checked out on active tag

/var/log/ben-firmware/
  update.log
  agent.log
```

---

## 10. Systemd Units

One service per agent. Only the services relevant to the device model are enabled at provisioning time.

| Service | Models | Needs network | Description |
|---|---|---|---|
| `ben-provision.service` | all | no | One-shot at boot: checks connectivity, triggers BLE provisioning if needed |
| `ben-registrar.service` | all | yes | One-shot: registers device with backend, drives RGB LED verification sequence |
| `ben-tic-reader.service` | `pi0-wired` only | no | Reads TIC directly from Linky |
| `ben-radio.service` | `lora-tic-receiver` cap | no | Sole owner of the SX127x: RX/TX + LED |
| `ben-telemetry.service` | `lora-tic-receiver` cap | no | Decodes and stores frames (no radio) |
| `ben-publisher.service` | all | yes | Publishes data to cloud API |
| `ben-mqtt-ha.service` | all, **opt-in** | yes | Pushes state to Home Assistant over MQTT; skipped unless `/etc/ben-firmware/mqtt-ha.json` exists — see §15 |
| `ben-update.service` | all | yes | One-shot: runs `check_update.py` |
| `ben-update.timer` | all | yes | Triggers `ben-update.service` once a day (randomized) |

Each service has its own log in `/var/log/ben-firmware/`. A crash in one service does not affect the others.

### Dependencies

```
ben-provision.service     (Before=network-online.target)
       ↓
network-online.target
       ↓
ben-registrar.service     (one-shot, skipped if already registered)
       ↓
ben-publisher.service
ben-update.timer
```

Readers and receivers do not depend on the network — they start as soon as the system is up and run independently:

```ini
# ben-radio.service / ben-telemetry.service / ben-tic-reader.service
[Unit]
After=basic.target
```

Only the publisher depends on the network:

```ini
# ben-publisher.service
[Unit]
After=network-online.target
```

### Provisioning service — `ben-provision.service`

One-shot service that runs at every boot before `network-online.target`. It checks whether the device can reach the BEN API server (not just whether the network interface is up — a device can have an IP but no route to the server).

```python
def can_reach_server():
    try:
        requests.get("https://api.ben-firmware.io/health", timeout=5)
        return True
    except Exception:
        return False
```

Logic:
```
1. Can reach API server?
   → yes: reset failure counter, exit 0

2. No: increment /var/lib/ben-firmware/boot_failures
   → counter < 3: exit (retry next boot)
   → counter >= 3: clear WiFi config, start BLE provisioning mode
```

The failure counter handles the moving/new-router case — after 3 failed boots the device automatically re-enters BLE provisioning mode, without any physical intervention.

The provisioner does not know about other services. Each service declares its own dependencies — separation of concerns.

---

## 11. Sudo Rights

`/etc/sudoers.d/ben-firmware`:

```
ben ALL=(ALL) NOPASSWD: ALL
```

The real security boundary is GPG tag signing — only scripts from a verified signed tag can run. A granular sudo list gives a false sense of security and causes hard-to-debug failures in `update.sh`. Since the device is dedicated (not a multi-user server) and all executed code is cryptographically verified, `NOPASSWD: ALL` is the honest and pragmatic choice.

---

## 12. Arduino Firmware

Arduino cannot self-update (no OTA): it ships by **physical reflash**.

Its version is therefore **not declared** anywhere on the Pi — it is **measured**. Since
emitter `0.1.10` each boot frame carries TLV `T_FW` (three bytes: major, minor, patch); the
receiver stores it per emitter in `emitter.fw_version` and the daily heartbeat carries it in
`health.emitter[].fw`. That is the only way to know, remotely, what a given satellite runs —
before this, it took an FTDI in hand, in front of the box.

⚠️ An emitter predating `0.1.10` emits no such TLV. Its absence is recorded as
`anterieur-campagne` ("predates the campaign") rather than as a missing value, which is what
makes a reflash campaign steerable from facts. See `docs/lora-frame-format.md` §7.

The `compatibility.yaml` expresses the minimum Arduino firmware version required by a given Pi software version:

```yaml
arduino:
  minimum: "1.0.0"
```

If the Arduino firmware is below the required minimum, the Pi agent logs a warning but does not block its own update.

### HMAC key storage — EEPROM

The HMAC key is stored in the Arduino's EEPROM (ATmega328P: 1KB), not hardcoded in the firmware binary. This means:

- **One compiled binary** for all devices of the same model — no per-device recompilation
- **Key survives firmware updates** — EEPROM is not touched by a reflash
- **Key rotation** without firmware change — write new key to EEPROM only

#### Provisioning flow (future — not yet implemented)

```
1. flash-arduino.sh flashes the production firmware binary
2. flash-arduino.sh sends the HMAC key over Serial to the Arduino
3. Arduino writes the key to EEPROM (addresses 0x00–0x1F, 32 bytes)
4. On every subsequent boot, Arduino reads the key from EEPROM before sending any frame
```

The provisioning sketch (or a dedicated boot mode in the production sketch) listens on Serial for a key write command, validates it, and writes to EEPROM. Subsequent boots skip this step and read directly.

#### Sketch changes required (future)

The production sketch needs to:
1. On boot: read 32 bytes from EEPROM starting at address 0x00
2. Verify the key is not all-zeros or all-0xFF (unprogrammed EEPROM) — halt with error LED if so
3. Use the key for HMAC-SHA256 on every outgoing frame

```cpp
#include <EEPROM.h>

#define HMAC_KEY_ADDR 0x00
#define HMAC_KEY_LEN  32

uint8_t hmac_key[HMAC_KEY_LEN];

void setup() {
    for (int i = 0; i < HMAC_KEY_LEN; i++) {
        hmac_key[i] = EEPROM.read(HMAC_KEY_ADDR + i);
    }
    // halt if key looks unprogrammed
    if (is_blank(hmac_key, HMAC_KEY_LEN)) {
        error_halt(); // blink error LED forever
    }
    // ...
}
```

#### Atomic dual provisioning (future)

When the device includes an Arduino (models `pi0-lora`, `pi0-lora-wired`), the operator connects both at the same time:

```
Mac opérateur
├── USB → FTDI adapter → Arduino Pro Mini   (/dev/tty.usbserial-XXXX)
└── USB → USB data port → Pi Zero W         (raspberrypi.local)
```

A single `provision.sh` call handles both in sequence:

```bash
./scripts/provision.sh pi0-lora rev01 ben-0042 /dev/tty.usbserial-XXXX
```

Sequence:
1. Generate device cert + HMAC key
2. Flash Arduino: `arduino-cli compile` → `arduino-cli upload` → send HMAC key over Serial → Arduino writes to EEPROM
3. Provision Pi: SSH → stage certs + HMAC key → run `install.sh`
4. Record in registry

#### ben-ops scripts required (future)

- `scripts/flash-arduino.sh <device-id> <port>` — compile, upload, write HMAC key to EEPROM
- Uses `arduino-cli` + `pyserial`
- Reads HMAC key from `keys/<device-id>.key`
- `provision.sh` gains an optional `<arduino-port>` argument — skipped for `pi0-wired`

---

## 13. PDL (Point de Livraison)

The PDL is the unique identifier of the Linky meter. It is sensitive data — it must never be stored or transmitted in clear text, and must never appear in measurement data.

### Device vs PDL

These are two distinct entities:
- **Device** (`ben-0042`) — the physical box, follows the customer
- **PDL** — tied to the Linky meter, tied to the physical address

A device can change PDL (customer moves). A device can also be connected to multiple PDLs simultaneously (e.g., `pi0-lora-wired` reads its own Linky wired + receives LoRa from one or more other meters).

### PDL index

Each PDL source on a device is identified by an integer **`pdl_index`**, local to the device. The index is stable and assigned at provisioning time via `sources.json`.

Default assignment by model:

| Model | pdl_index 0 | pdl_index 1+ |
|---|---|---|
| `pi0-wired` | TIC wired | — |
| `pi0-lora` | LoRa (first emitter) | additional LoRa emitters |
| `pi0-lora-wired` | TIC wired | LoRa emitters |

### `sources.json`

Stored at `/etc/ben-firmware/sources.json`. **Kept separate from `device.json` intentionally** — they have different writers and different lifecycles:

| File | Owner | Trigger |
|---|---|---|
| `device.json` | `check_update.py` | OTA software update |
| `sources.json` | `install.sh` at provisioning, future `config-agent` | Operator command / app action |

Merging them would create two concurrent writers on the same file — a source of race conditions and data loss.

Initial content written by `install.sh` at provisioning time:

```json
{
  "sources": [
    { "index": 0, "type": "wired" },
    { "index": 1, "type": "lora", "lora_address": "0x1F" }
  ]
}
```

The LoRa receiver reads the RadioHead source address of each received frame and looks up the corresponding `pdl_index` in `sources.json`. Frames from unknown addresses are ignored and logged as warnings.

### Adding a new LoRa emitter after provisioning (future)

A customer can receive a new LoRa emitter by mail and plug it into a second Linky themselves (e.g., a workshop meter, a building common meter). The emitter is pre-flashed by the operator with the correct HMAC key and a pre-assigned LoRa address.

The reconfiguration flow:

```
1. Operator flashes the Arduino with HMAC + assigned LoRa address
2. Ships it to the customer
3. Customer plugs it into their Linky (no Pi access required)
4. Operator or app triggers: PATCH /api/devices/ben-0042/sources
   → adds { "index": 2, "type": "lora", "lora_address": "0x2A" }
5. Device fetches the new config (config-agent polls backend)
6. Device writes updated sources.json
7. LoRa receiver reloads config → starts accepting frames from 0x2A
8. New PDL is discovered via boot frame → registered with backend
9. User names the new PDL in the app ("Atelier", "Copro"...)
```

This flow requires a **`config-agent`** service (not yet implemented) that:
- Polls the backend for pending config changes for this device
- Applies them atomically to `sources.json`
- Deploys the new HMAC key to `/etc/ben-firmware/hmac/<lora_address>.key`
- Signals the LoRa receiver to reload (SIGHUP or systemd reload)

The `config-agent` is a future service — not part of the current scope. The reconfiguration use case is documented here to ensure `sources.json` remains a standalone file with a clear ownership model.

### HMAC key for LoRa emitters

Each device has a single HMAC-SHA256 key shared with all its LoRa emitters (Arduinos). The key proves "this frame comes from a legitimate emitter associated with this device" — individual emitter identification is handled by the RadioHead source address and `sources.json` routing.

**Key generation** is part of the device provisioning process, managed in ben-ops:

```bash
# ben-ops
./scripts/gen-hmac-key.sh ben-0042    # generates keys/ben-0042.key
```

The key is then:
1. Deployed to the Pi at `/etc/ben-firmware/hmac.key` during provisioning
2. Embedded in each Arduino firmware associated with this device at flash time (build-time constant or written to EEPROM)

**Key storage in ben-ops**: `keys/` directory, gitignored. One file per device, named by device ID. Must be backed up alongside the CA private key and GPG private key.

**Key rotation** is not yet defined — treated as a future concern if a key is compromised.

### How the PDL is discovered

| Source type | How |
|---|---|
| `wired` | Pi reads PDL directly from TIC stream (`ADCO`/`ADSC`) |
| `lora` | Arduino reads PDL from TIC and sends a dedicated **boot frame** (type `0x01`) at startup |

The boot frame is separate from measurement frames (type `0x02`). It contains the PDL and is sent once at startup. If the PDL changes (customer moves, new meter), a reboot triggers a new boot frame.

### Registration with backend

When a PDL is discovered (on first boot or after a change), the device sends:

```
POST /api/pdl/register
{
  "deviceId": "ben-0042",
  "pdlIndex": 1,
  "encryptedPdl": "<PDL encrypted with server public key>"
}
```

The backend stores the association `(deviceId, pdlIndex) → PDL`. The PDL is transmitted only once, encrypted. Subsequent measurement pushes reference only `(deviceId, pdlIndex)` — the backend resolves the PDL internally. The PDL never appears in measurement data.

### Local storage — Phase 1 (InfluxDB)

In Phase 1, data is written locally to InfluxDB tagged with `pdl_index`:

```
linkyEvents,pdl_index=0 BASE=12345,PAPP=430,IINST=3
linkyEvents,pdl_index=1 BASE=6789,PAPP=120,IINST=1
```

### User-facing naming

The `pdl_index` is an opaque integer on the device. The user assigns human-readable names ("Maison", "Atelier", "Copro") in the app. Names are stored in the backend as metadata on the `(deviceId, pdlIndex)` association — the device is never aware of them.

---

## 14. TIC Reader — Implementation Notes

### UART configuration — 8N1 + parity mask

**Never configure the Pi Zero UART in 7E1.** The mini-UART (`/dev/ttyS0`) on the Pi Zero does not support hardware parity — attempting 7E1 raises `termios EINVAL` and the serial port fails to open.

The correct approach, validated on hardware:

```python
s.baudrate = 1200
s.bytesize = serial.EIGHTBITS   # 8N1 — only reliable mode on mini-UART
s.parity   = serial.PARITY_NONE
s.stopbits = serial.STOPBITS_ONE
s.timeout  = 2                  # never block indefinitely
```

Then mask the parity bit on every byte read:

```python
line = bytes(b & 0x7F for b in raw).decode('ascii', 'replace')
```

The TIC signal is 7E1 at the source (Linky meter), but reading in 8N1 and masking the MSB produces identical ASCII output. This was discovered in the field on `pi10jd75` after pigpio failed.

### PDL label

The PDL is carried in the `ADCO` label (mode historique) or `ADSC` label (mode standard). It must be read from the TIC stream and transmitted encrypted to the backend — never stored or logged in clear text. See §13.

### Labels read

| Label | Description |
|---|---|
| `ADCO` / `ADSC` | PDL — read once at startup and on change |
| `BASE` | Total index (Wh) |
| `HCHC` / `HCHP` | Off-peak / peak index (if tarif HC) |
| `PAPP` | Apparent power (VA) |
| `IINST` | Instantaneous current (A) |

---

## 15. Home Assistant (MQTT push)

`ben-mqtt-ha` publishes the box state to Home Assistant's MQTT broker using **MQTT Discovery**,
so HA's built-in MQTT integration creates the entities — nothing to install on the HA side.
Full guide (setup, entities, topics, Energy dashboard): [docs/home-assistant.md](docs/home-assistant.md).

- **Push, not polling.** Readers publish `ben/local/stored` on the local bus after each commit
  (`store/bus.py`, fire-and-forget: it can never cost a measurement). The bridge then re-reads
  the DB read-only and publishes. Latency = reader write cadence (~15 s wired, ~60 s LoRa).
- **One source for the `/live` shape**: `store/live.py`, used by both `local_api.py` and the bridge.
- **Opt-in**: the unit is enabled everywhere but gated by `ConditionPathExists`.
- **The ADCO is never published.** Entities are keyed `<deviceId>_pdl<n>`.
- The local mosquitto stays bound to `127.0.0.1`; the bridge is a *client* of the remote broker.

---

## 16. What is NOT in this repo

| Component | Location |
|---|---|
| Backend API | private repo |
| Mobile app | private repo |
| Certificate authority | offline, never committed |
| Device private keys | on device only, never committed |
| Provisioning scripts with secrets | private repo |
