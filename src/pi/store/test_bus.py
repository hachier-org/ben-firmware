#!/usr/bin/env python3
"""Banc du signal « mesures stockées » (bus.py) — le contrat FIRE-AND-FORGET.

Les lecteurs (ben-telemetry, ben-tic-reader) publient ce signal APRÈS chaque commit. Le
seul contrat qui compte : il ne doit JAMAIS coûter une mesure à celui qui le publie.

  · `publish_stored(None, …)` doit être un no-op — c'est l'état d'un lecteur dont paho
    est absent ou dont le broker n'est pas encore là ;
  · un client qui lève (broker tombé en plein vol) ne doit rien faire remonter ;
  · QoS 0 et sujet figé : ben-mqtt-ha s'abonne à ce sujet exact ;
  · paho n'est PAS installé en CI ni forcément sur le boîtier : `local_client` rend None
    plutôt que de planter à l'import.

    python3 src/pi/store/test_bus.py
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import bus  # noqa: E402

CAS = []


def cas(fn):
    CAS.append(fn)
    return fn


class ClientEnregistreur:
    def __init__(self):
        self.appels = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.appels.append((topic, payload, qos, retain))


class ClientCasse:
    def publish(self, *a, **k):
        raise OSError("broker tombé")


@cas
def le_payload_est_un_json_pdl_ts():
    """⚖️ Le témoin : la forme exacte lue par ben-mqtt-ha."""
    assert json.loads(bus.payload(2, 1793659677)) == {"pdl_index": 2, "ts": 1793659677}


@cas
def ts_absent_devient_null():
    assert json.loads(bus.payload(0, None)) == {"pdl_index": 0, "ts": None}


@cas
def les_entiers_sont_normalises():
    """Un `ts` flottant (time.time()) ne doit pas fuiter en `1793659677.9` dans le JSON."""
    assert json.loads(bus.payload(1, 1793659677.9)) == {"pdl_index": 1, "ts": 1793659677}


@cas
def sans_client_c_est_un_no_op():
    bus.publish_stored(None, 0, 1)


@cas
def un_client_qui_leve_ne_fait_rien_remonter():
    """🚨 Le contrat : une exception ici coûterait une mesure au lecteur."""
    bus.publish_stored(ClientCasse(), 0, 1)


@cas
def publie_sur_le_sujet_stored_en_qos_0():
    c = ClientEnregistreur()
    bus.publish_stored(c, 3, 42)
    assert len(c.appels) == 1
    topic, payload, qos, retain = c.appels[0]
    assert topic == bus.TOPIC_STORED == "ben/local/stored"
    assert qos == 0
    assert not retain, "un signal retenu serait rejoué à chaque nouvel abonné"
    assert json.loads(payload) == {"pdl_index": 3, "ts": 42}


@cas
def local_client_rend_none_sans_paho():
    """paho absent (CI, boîtier sans le paquet) → None, pas ImportError."""
    cles = ("paho", "paho.mqtt", "paho.mqtt.client")
    absent = object()
    avant = {k: sys.modules.get(k, absent) for k in cles}
    try:
        for k in cles:
            sys.modules[k] = None      # force l'ImportError à `import paho.mqtt.client`
        assert bus.local_client("test") is None
    finally:
        for k, v in avant.items():
            if v is absent:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


if __name__ == "__main__":
    ko = 0
    for fn in CAS:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            ko += 1
            print(f"  KO   {fn.__name__} : {e!r}")
    print(f"\n{len(CAS) - ko}/{len(CAS)}")
    sys.exit(1 if ko else 0)
