"""
bus.py — le signal « des mesures viennent d'être stockées », sur le bus MQTT LOCAL.

Publié par les lecteurs (ben-telemetry, ben-tic-reader) APRÈS chaque commit dans
`measurements.db`, consommé par ben-mqtt-ha qui relit alors la base et pousse l'état
vers Home Assistant. C'est ce signal qui rend l'intégration PUSH : sans lui il
faudrait sonder la base en boucle.

    ben/local/stored   {"pdl_index": 0, "ts": 1793659677}

⭐ Le signal ne transporte PAS la mesure. Celui qui l'écoute relit la base : la
   forme de l'état reste celle de `/live` (cf. live.py), écrite une seule fois.

🚨 FIRE-AND-FORGET, et c'est le contrat. QoS 0, non retenu, aucune exception ne
   remonte : un broker absent, un paho manquant ou un abonné planté ne doivent
   JAMAIS coûter une mesure au lecteur. Un signal perdu ne coûte qu'un retard,
   que le rafraîchissement périodique de ben-mqtt-ha rattrape.

Le bus ne sort jamais de la machine (mosquitto écoute sur 127.0.0.1 seulement,
cf. config/mosquitto/ben.conf).
"""

import json
import logging

log = logging.getLogger(__name__)

MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
TOPIC_STORED = "ben/local/stored"


def payload(pdl_index: int, ts: int | None) -> str:
    return json.dumps({"pdl_index": int(pdl_index),
                       "ts": int(ts) if ts is not None else None})


def publish_stored(client, pdl_index: int, ts: int | None) -> None:
    """Signale un commit. Ne lève JAMAIS — cf. en-tête."""
    if client is None:
        return
    try:
        client.publish(TOPIC_STORED, payload(pdl_index, ts), qos=0)
    except Exception as e:  # noqa: BLE001
        log.debug(f"bus: signal {TOPIC_STORED} non publié ({e})")


def local_client(client_id: str):
    """Client du bus local pour un process qui n'en a pas déjà un (ben-tic-reader).

    `connect_async` + `loop_start` : se (re)connecte tout seul si mosquitto n'est pas
    encore up, sans jamais bloquer l'appelant. None si paho est absent — le lecteur
    continue de stocker, simplement sans signal.
    """
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        log.warning("bus: paho absent — pas de signal %s", TOPIC_STORED)
        return None
    try:
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        except (AttributeError, TypeError):
            client = mqtt.Client(client_id=client_id)     # paho 1.x
        client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        client.loop_start()
        return client
    except Exception as e:  # noqa: BLE001
        log.warning(f"bus: client local indisponible ({e}) — pas de signal {TOPIC_STORED}")
        return None
