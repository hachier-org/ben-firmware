#!/usr/bin/env python3
"""
ben-mqtt-ha — pousse la conso du boîtier vers Home Assistant, en MQTT.

OPT-IN : le service ne démarre que si /etc/ben-firmware/mqtt-ha.json existe
(`ConditionPathExists` dans ben-mqtt-ha.service). Sans ce fichier, rien ne change
sur le boîtier.

═══ POURQUOI DU PUSH, ET PAS HOME ASSISTANT QUI SONDE :8087 ══════════════════

Un Home Assistant qui sonde `/live` toutes les N secondes réveille le Pi Zero pour
rien la plupart du temps — une trame LoRa n'arrive que toutes les ~60 s — et
rate quand même le moment où elle arrive. Ici c'est la mesure qui déclenche :

    lecteur ── commit ──► ben/local/stored (bus LOCAL, cf. store/bus.py)
                               │
                          ben-mqtt-ha ── relit la base (lecture seule) ──► broker HA

⭐ MQTT DISCOVERY : le boîtier publie lui-même la description de ses entités
   (`<prefix>/device/<id>/config`). L'intégration MQTT native de Home Assistant
   les crée seule — AUCUN composant à installer côté HA.

═══ CE QUI EST PUBLIÉ ════════════════════════════════════════════════════════

    ben/<id>/status                 online|offline   retenu, dernier testament
    ben/<id>/pdl/<n>/live           forme de /live (live.py) + `time` ISO   retenu
    ben/<id>/pdl/<n>/registers      forme de /registers + `wh` + `total_wh` retenu
    ben/<id>/pdl/<n>/link           dernier {rssi, snr} LoRa (modèles radio) retenu
    ben/<id>/pdl/<n>/event          événement compteur (`event_type` = type)
    ben/<id>/led_level              réglage LED   retenu
    ben/<id>/led_level/set          ← commande depuis HA (0..5)

🔒 L'ADCO (le PDL) n'est JAMAIS publié : c'est une donnée sensible (ARCHITECTURE
   §13), et un broker domestique la recopierait dans l'historique et les
   sauvegardes de HA. Les entités sont identifiées par `<deviceId>_pdl<n>`.

═══ CE QUE CE SERVICE NE FAIT PAS ════════════════════════════════════════════

Il n'écrit RIEN sur la SD (hormis `settings.json` sur commande LED, comme
`POST /settings`). Il ne met rien en tampon quand le broker est injoignable : les
index d'énergie sont CUMULÉS, la statistique de HA reprend au point suivant sans
perdre un Wh. À la reconnexion, tout l'état est republié.

Config (/etc/ben-firmware/mqtt-ha.json) :

    {
      "host": "192.168.1.10",          requis
      "port": 1883,
      "username": "ben", "password": "…",
      "tls": false, "ca_file": null,   tls=true sans ca_file → CA système
      "discovery_prefix": "homeassistant",
      "base_topic": "ben"
    }
"""

import contextlib
import json
import logging
import os
import queue
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "store"))
import bus  # noqa: E402
import db  # noqa: E402
import live  # noqa: E402
import settings  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("BEN_MQTT_HA_CONFIG", "/etc/ben-firmware/mqtt-ha.json")
DEVICE_JSON = os.environ.get("BEN_DEVICE_JSON", "/etc/ben-firmware/device.json")

# Filet de sécurité : un signal perdu (bus redémarré, lecteur sans paho) ne retarde
# l'état que de ça. Ce n'est PAS la cadence normale — c'est la mesure qui déclenche.
REFRESH_SEC = 300

# Une valeur LIVE plus vieille que ça devient « indisponible » dans HA plutôt que de
# mentir. 15 min : une trame LoRa ratée de temps en temps ne fait pas clignoter.
# Les INDEX n'expirent pas : un cumul reste vrai même vieux.
LIVE_EXPIRE_SEC = 900

# Types émis par db.record_ngtf / db.record_tic_mode. HA refuse un `event_type` hors
# de cette liste — en ajouter un ici quand db.py en émet un nouveau.
EVENT_TYPES = ["changement_offre", "changement_mode"]

ORIGIN = {"name": "ben-firmware", "url": "https://github.com/xdegenne/ben-firmware"}

CONFIG_DEFAULTS = {
    "port": 1883,
    "username": None,
    "password": None,
    "tls": False,
    "ca_file": None,
    "discovery_prefix": "homeassistant",
    "base_topic": "ben",
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path) as f:
        raw = json.load(f)
    if not isinstance(raw, dict) or not raw.get("host"):
        raise ValueError(f"{path} : `host` requis")
    cfg = {**CONFIG_DEFAULTS, **raw}
    cfg["port"] = int(cfg["port"])
    return cfg


def load_device(path: str = DEVICE_JSON) -> dict:
    with open(path) as f:
        d = json.load(f)
    if not d.get("deviceId"):
        raise ValueError(f"{path} : `deviceId` absent")
    return d


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------
class Topics:
    def __init__(self, base: str, prefix: str, device_id: str):
        self.root = f"{base}/{device_id}"
        self.prefix = prefix
        self.status = f"{self.root}/status"
        self.led_state = f"{self.root}/led_level"
        self.led_set = f"{self.root}/led_level/set"
        self.ha_status = f"{prefix}/status"

    def pdl(self, n: int, leaf: str) -> str:
        return f"{self.root}/pdl/{n}/{leaf}"

    def discovery(self, object_id: str) -> str:
        return f"{self.prefix}/device/{object_id}/config"


# ---------------------------------------------------------------------------
# Payloads d'état
# ---------------------------------------------------------------------------
def iso(ts) -> str | None:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat() if ts is not None else None


def register_key(contract: str | None, src_standard, index_id) -> str:
    """Clé d'un registre, stable tant que le CONTRAT ne change pas.

    🚨 Le contrat FAIT PARTIE de la clé. En standard, `index_id` est une POSITION dans le
       calendrier du fournisseur : après un changement d'offre, le n°2 désigne un autre
       registre physique (cf. db.registers). Sans le contrat, le même capteur HA verrait
       son index sauter — et un `total_increasing` qui descend est lu comme une remise à
       zéro : HA compterait TOUT le nouvel index comme consommé en une fois.
       Avec, un changement d'offre crée de nouveaux capteurs, qu'on rebranche dans le
       tableau Énergie.
    """
    slug = "".join(c if c.isalnum() else "_" for c in (contract or "na").lower()).strip("_")
    return f"{slug or 'na'}_{int(src_standard)}_{int(index_id)}"


def registers_payload(contract: str | None, regs: list, live_row: dict | None) -> dict:
    wh = {register_key(contract, r["src_standard"], r["index_id"]): r["index_value"]
          for r in regs if r.get("index_value") is not None}
    # Total = registres du MODE COURANT seulement : un boîtier passé d'historique à
    # standard sans changer de contrat garde les deux familles, qui comptent la même énergie.
    mode = live_row.get("src_standard") if live_row else None
    vals = [r["index_value"] for r in regs if r.get("index_value") is not None
            and (mode is None or r["src_standard"] == mode)]
    return {"contract": contract, "registers": regs, "wh": wh,
            "total_wh": sum(vals) if vals else None}


def live_payload(row: dict) -> dict:
    return {**row, "time": iso(row.get("ts"))}


def event_payload(ev: dict) -> dict:
    return {"event_type": ev["type"], "id": ev["id"], "ts": ev["ts"],
            "time": iso(ev["ts"]), "severite": ev.get("severite"),
            "titre": ev.get("titre"), "corps": ev.get("corps")}


# ---------------------------------------------------------------------------
# Discovery (format « device » de Home Assistant, ≥ 2024.12)
# ---------------------------------------------------------------------------
def _with_availability(cmps: dict, topics: Topics) -> dict:
    for c in cmps.values():
        c["availability_topic"] = topics.status
    return cmps


def box_discovery(device: dict, topics: Topics) -> dict:
    device_id = device["deviceId"]
    return {
        "device": {
            "identifiers": [device_id],
            "name": f"BEN {device_id}",
            "manufacturer": "BEN",
            "model": device.get("model"),
            "sw_version": device.get("softwareVersion"),
            "serial_number": device_id,
        },
        "origin": ORIGIN,
        "components": _with_availability({
            f"{device_id}_led_level": {
                "platform": "number",
                "unique_id": f"{device_id}_led_level",
                "name": "Luminosité LED",
                "icon": "mdi:led-on",
                "entity_category": "config",
                "command_topic": topics.led_set,
                "state_topic": topics.led_state,
                "min": settings.LED_LEVEL_MIN,
                "max": settings.LED_LEVEL_MAX,
                "step": 1,
                "mode": "slider",
            },
        }, topics),
    }


def pdl_discovery(device: dict, topics: Topics, n: int, live_row: dict | None,
                  contract: str | None, regs: list) -> dict:
    device_id = device["deviceId"]
    oid = f"{device_id}_pdl{n}"
    t_live, t_regs = topics.pdl(n, "live"), topics.pdl(n, "registers")

    def sensor(key, name, tpl, topic=t_live, **extra):
        return key, {"platform": "sensor", "unique_id": f"{oid}_{key}", "name": name,
                     "state_topic": topic, "value_template": tpl, **extra}

    def lv(key, name, field, **extra):
        return sensor(key, name, "{{ value_json.%s }}" % field,
                      expire_after=LIVE_EXPIRE_SEC, **extra)

    def energy(key, name, tpl, topic=t_regs):
        return sensor(key, name, tpl, topic, device_class="energy",
                      unit_of_measurement="Wh", state_class="total_increasing")

    cmps = dict([
        lv("papp", "Puissance apparente", "papp", device_class="apparent_power",
           unit_of_measurement="VA", state_class="measurement"),
        lv("iinst", "Intensité", "iinst", device_class="current",
           unit_of_measurement="A", state_class="measurement"),
        lv("level", "Niveau de consommation", "level", icon="mdi:gauge",
           state_class="measurement"),
        lv("tariff_label", "Tarif en cours", "tariff_label", icon="mdi:cash-clock"),
        lv("isousc", "Intensité souscrite", "isousc", device_class="current",
           unit_of_measurement="A"),
        lv("max_va", "Puissance souscrite", "maxVa", device_class="apparent_power",
           unit_of_measurement="VA"),
        lv("contract", "Contrat", "contract", icon="mdi:file-document-outline",
           entity_category="diagnostic"),
        lv("tic_mode", "Mode TIC", "tic_mode", entity_category="diagnostic"),
        sensor("last_reading", "Dernière mesure", "{{ value_json.time }}",
               device_class="timestamp", entity_category="diagnostic"),
        ("producer", {"platform": "binary_sensor", "unique_id": f"{oid}_producer",
                      "name": "Production constatée", "state_topic": t_live,
                      "value_template": "{{ 'ON' if value_json.producer else 'OFF' }}",
                      "entity_category": "diagnostic"}),
        ("event", {"platform": "event", "unique_id": f"{oid}_event",
                   "name": "Événement compteur", "state_topic": topics.pdl(n, "event"),
                   "event_types": EVENT_TYPES, "icon": "mdi:bell-outline"}),
        energy("energy_total", "Index total", "{{ value_json.total_wh }}"),
    ])

    tempo = ("TEMPO" in (contract or "").upper()
             or (live_row or {}).get("tempo_color") is not None
             or any(r.get("tempo_color") for r in regs))
    if tempo:
        k, c = lv("tempo_color", "Couleur Tempo", "tempo_color", icon="mdi:palette")
        cmps[k] = {**c, "device_class": "enum", "options": ["bleu", "blanc", "rouge"]}

    for r in regs:
        key = register_key(contract, r["src_standard"], r["index_id"])
        label = r.get("label") or f"registre {r['index_id']}"
        k, c = energy(f"energy_{key}", f"Index {label}", "{{ value_json.wh['%s'] }}" % key)
        cmps[k] = c

    if (live_row or {}).get("inject_total") is not None:
        k, c = energy("energy_injected", "Index injection",
                      "{{ value_json.inject_total }}", topic=t_live)
        cmps[k] = c

    if "lora-tic-receiver" in (device.get("capabilities") or {}):
        t_link = topics.pdl(n, "link")
        cmps.update(dict([
            sensor("lora_rssi", "Signal LoRa", "{{ value_json.rssi }}", t_link,
                   device_class="signal_strength", unit_of_measurement="dBm",
                   state_class="measurement", entity_category="diagnostic"),
            sensor("lora_snr", "SNR LoRa", "{{ value_json.snr }}", t_link,
                   unit_of_measurement="dB", state_class="measurement",
                   entity_category="diagnostic", icon="mdi:signal"),
        ]))

    return {
        "device": {
            "identifiers": [oid],
            "name": f"BEN {device_id} Linky {n}",
            "manufacturer": "Enedis",
            "model": "Linky",
            "via_device": device_id,
        },
        "origin": ORIGIN,
        "components": _with_availability(cmps, topics),
    }


# ---------------------------------------------------------------------------
# Pont : bus local → broker Home Assistant
# ---------------------------------------------------------------------------
def _mqtt_client(client_id: str):
    import paho.mqtt.client as mqtt
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except (AttributeError, TypeError):
        return mqtt.Client(client_id=client_id)     # paho 1.x


class Bridge:
    """Tout ce qui touche la base ou publie se fait dans UN fil (`run`) : les
    callbacks paho ne font que déposer une tâche dans la file."""

    def __init__(self, cfg: dict, device: dict, remote=None, local=None,
                 open_db=lambda: db.connect(read_only=True)):
        self.cfg = cfg
        self.device = device
        self.topics = Topics(cfg["base_topic"], cfg["discovery_prefix"], device["deviceId"])
        self.remote = remote
        self.local = local
        self.open_db = open_db
        self.tasks: queue.Queue = queue.Queue()
        self.discovery_sent: dict = {}     # object_id → payload publié (republié s'il change)
        self.events_primed = False
        self.last_event_id = None
        self.lora = "lora-tic-receiver" in (device.get("capabilities") or {})

    # ── paho ────────────────────────────────────────────────────────────────
    def start(self) -> None:
        cfg, t = self.cfg, self.topics
        device_id = self.device["deviceId"]
        if self.remote is None:
            self.remote = _mqtt_client(f"ben-mqtt-ha-{device_id}")
            if cfg["username"]:
                self.remote.username_pw_set(cfg["username"], cfg["password"])
            if cfg["tls"]:
                self.remote.tls_set(ca_certs=cfg["ca_file"])
            self.remote.will_set(t.status, "offline", qos=1, retain=True)
            self.remote.reconnect_delay_set(min_delay=1, max_delay=120)
            self.remote.on_connect = self._on_remote_connect
            self.remote.on_message = self._on_remote_message
            self.remote.connect_async(cfg["host"], cfg["port"], keepalive=60)
            self.remote.loop_start()
        if self.local is None:
            self.local = _mqtt_client(f"ben-mqtt-ha-local-{device_id}")
            self.local.on_connect = self._on_local_connect
            self.local.on_message = self._on_local_message
            self.local.connect_async(bus.MQTT_HOST, bus.MQTT_PORT, keepalive=30)
            self.local.loop_start()

    def _on_remote_connect(self, client, userdata, flags, *args) -> None:
        reason = args[0] if args else 0
        if getattr(reason, "is_failure", reason not in (0, None)):
            log.error(f"broker HA {self.cfg['host']}:{self.cfg['port']} refuse : {reason}")
            return
        client.subscribe([(self.topics.ha_status, 1), (self.topics.led_set, 1)])
        log.info(f"broker HA connecté ({self.cfg['host']}:{self.cfg['port']})")
        self.tasks.put(("full", None))

    def _on_remote_message(self, client, userdata, msg) -> None:
        payload = msg.payload.decode(errors="replace").strip()
        if msg.topic == self.topics.ha_status:
            # HA redémarré : il a oublié la discovery non retenue de ses sessions
            # passées — on republie tout (recommandation HA, « birth message »).
            if payload == "online":
                self.tasks.put(("full", None))
        elif msg.topic == self.topics.led_set:
            self.tasks.put(("led", payload))

    def _on_local_connect(self, client, userdata, flags, *args) -> None:
        client.subscribe(bus.TOPIC_STORED, qos=0)
        log.info(f"bus local connecté, abonné {bus.TOPIC_STORED}")

    def _on_local_message(self, client, userdata, msg) -> None:
        try:
            self.tasks.put(("pdl", int(json.loads(msg.payload)["pdl_index"])))
        except Exception:  # noqa: BLE001
            log.warning(f"signal illisible sur {msg.topic} : {msg.payload[:80]!r}")

    # ── publication ─────────────────────────────────────────────────────────
    def _pub(self, topic: str, payload, retain: bool = True, qos: int = 0) -> None:
        body = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
        self.remote.publish(topic, body, qos=qos, retain=retain)

    def _discovery(self, object_id: str, payload: dict, force: bool) -> None:
        if not force and self.discovery_sent.get(object_id) == payload:
            return
        self._pub(self.topics.discovery(object_id), payload, qos=1)
        self.discovery_sent[object_id] = payload

    def publish_pdl(self, conn, n: int, force: bool = False) -> None:
        rows = live.rows(conn, n)
        row = rows[0] if rows else None
        regs = db.registers(conn, n)
        contract = db.get_ngtf(conn, n)
        # La discovery D'ABORD : un nouveau registre (bascule HC→HP vue pour la première
        # fois, offre changée) doit exister dans HA avant que sa valeur n'arrive.
        self._discovery(f"{self.device['deviceId']}_pdl{n}",
                        pdl_discovery(self.device, self.topics, n, row, contract, regs), force)
        if row is not None:
            self._pub(self.topics.pdl(n, "live"), live_payload(row))
        self._pub(self.topics.pdl(n, "registers"), registers_payload(contract, regs, row))
        if self.lora:
            link = conn.execute(
                "SELECT ts, rssi, snr FROM lora_link WHERE pdl_index=? "
                "ORDER BY ts DESC LIMIT 1", (n,)).fetchone()
            if link is not None:
                self._pub(self.topics.pdl(n, "link"),
                          {"ts": link[0], "rssi": link[1], "snr": link[2]})

    def publish_events(self, conn) -> None:
        last_id, _ = db.last_event(conn)
        if not self.events_primed:
            # Premier passage : on AMORCE sans rejouer l'historique — HA déclencherait
            # des automatisations pour des faits vieux de plusieurs semaines.
            self.events_primed = True
            self.last_event_id = last_id
            return
        if last_id is None or last_id == self.last_event_id:
            return
        # Ids `<deviceId>:<UUIDv7>` : même préfixe, horodatage en tête → l'ordre
        # lexicographique EST l'ordre chronologique (cf. db._uuid7).
        fresh = sorted((e for e in db.get_events(conn, limit=50)
                        if self.last_event_id is None or e["id"] > self.last_event_id),
                       key=lambda e: e["id"])
        for ev in fresh:
            if ev.get("pdl_index") is None:
                continue
            if ev["type"] not in EVENT_TYPES:
                log.warning(f"événement {ev['type']!r} inconnu de EVENT_TYPES — non publié")
                continue
            self._pub(self.topics.pdl(ev["pdl_index"], "event"), event_payload(ev),
                      retain=False, qos=1)
        self.last_event_id = last_id

    def publish_full(self, force: bool) -> None:
        """Tout l'état. `force` (connexion, redémarrage de HA) republie aussi la
        discovery ; le rafraîchissement périodique ne la renvoie que si elle a changé."""
        self._pub(self.topics.status, "online", qos=1)
        self._discovery(self.device["deviceId"], box_discovery(self.device, self.topics), force)
        self._pub(self.topics.led_state, str(settings.load()["led_level"]))
        with contextlib.closing(self.open_db()) as conn:
            for n in db.pdl_list(conn):
                self.publish_pdl(conn, n, force=force)
            self.publish_events(conn)

    def set_led(self, payload: str) -> None:
        try:
            level = int(float(payload))
        except (ValueError, OverflowError):     # "abc", "inf"
            log.warning(f"led_level/set illisible : {payload!r}")
            return
        cur = settings.save({"led_level": level})   # clamp 0..5, écriture atomique
        self._pub(self.topics.led_state, str(cur["led_level"]))
        log.info(f"luminosité LED → {cur['led_level']} (depuis Home Assistant)")

    # ── boucle ──────────────────────────────────────────────────────────────
    def handle(self, batch: list) -> None:
        """Traite un LOT de tâches : les signaux d'un même PDL sont fusionnés (une
        rafale de commits ne republie qu'une fois)."""
        kinds = {kind for kind, _ in batch}
        if "full" in kinds or "refresh" in kinds:
            self.publish_full(force="full" in kinds)
        else:
            pdls = sorted({arg for kind, arg in batch if kind == "pdl"})
            if pdls:
                with contextlib.closing(self.open_db()) as conn:
                    for n in pdls:
                        self.publish_pdl(conn, n)
                    self.publish_events(conn)
        for kind, arg in batch:
            if kind == "led":
                self.set_led(arg)

    def run(self) -> None:
        while True:
            try:
                batch = [self.tasks.get(timeout=REFRESH_SEC)]
            except queue.Empty:
                batch = [("refresh", None)]
            while True:
                try:
                    batch.append(self.tasks.get_nowait())
                except queue.Empty:
                    break
            if not self.remote.is_connected():
                continue                    # la reconnexion reposera ("full", …)
            try:
                self.handle(batch)
            except Exception as e:  # noqa: BLE001
                # Base verrouillée, en reconstruction, absente… : on attend le signal suivant.
                log.warning(f"publication échouée : {e}")

    def stop(self) -> None:
        if self.remote is not None and self.remote.is_connected():
            self.remote.publish(self.topics.status, "offline", qos=1, retain=True).wait_for_publish(2)
        for c in (self.remote, self.local):
            if c is not None:
                c.disconnect()
                c.loop_stop()


def main() -> None:
    cfg = load_config()
    device = load_device()
    bridge = Bridge(cfg, device)

    def _on_sigterm(signum, frame):
        log.info("SIGTERM — offline puis arrêt")
        bridge.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)
    log.info(f"{device['deviceId']} → {cfg['host']}:{cfg['port']} "
             f"(discovery {cfg['discovery_prefix']}/, état {cfg['base_topic']}/)")
    bridge.start()
    bridge.run()


if __name__ == "__main__":
    main()
