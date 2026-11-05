#!/usr/bin/env python3
"""Banc de ben-mqtt-ha — ce que le boîtier raconte à Home Assistant, et ce qu'il tait.

Pas un banc d'accesseurs : chaque cas protège une promesse faite à l'utilisateur.

  · 🔒 l'ADCO n'est JAMAIS publié, ni dans un sujet, ni dans une valeur, ni dans la
    discovery. C'est une donnée sensible, et un broker domestique la recopierait dans
    l'historique et les sauvegardes de HA. Les entités s'identifient par `<deviceId>_pdl<n>` ;
  · le CONTRAT fait partie de la clé d'un registre : sans lui, un changement d'offre
    ferait sauter l'index d'un même capteur, et un `total_increasing` qui descend est lu
    par HA comme une remise à zéro — il compterait tout le nouvel index comme consommé ;
  · `unique_id` uniques : HA écrase silencieusement une entité qui en réutilise un ;
  · la discovery n'est renvoyée que si elle a changé (sinon chaque trame LoRa la
    republierait), et toujours sur `full` (HA redémarré a oublié la sienne) ;
  · les événements : le premier passage AMORCE sans rejouer l'historique (HA
    déclencherait des automatisations pour des faits vieux de semaines), puis chacun n'est
    publié qu'une fois ;
  · paho n'est pas installé en CI : le pont est testé avec de faux clients.

    python3 src/pi/ben-mqtt-ha/test_ben_mqtt_ha.py
"""
import json
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ben_mqtt_ha as m  # noqa: E402  (insère ../store dans sys.path)
import db  # noqa: E402
import settings  # noqa: E402

CAS = []

ADCO = "032164393511"      # matricule INVENTÉ — ce dépôt est PUBLIC, un ADCO identifie un foyer
DEVICE_ID = "ben-0042"
LABELS = {"PAPP": 2290, "IINST": 10, "BASE": 28883000,
          "_src_standard": 0, "_index_id": 0, "_index_value": 28883000}


def cas(fn):
    CAS.append(fn)
    return fn


# ── Faux clients et fabriques ────────────────────────────────────────────────

class FauxRemote:
    """Enregistre chaque publication ; ne parle à aucun broker."""

    def __init__(self):
        self.pubs = []          # (topic, payload, qos, retain)

    def publish(self, topic, payload, qos=0, retain=False):
        self.pubs.append((topic, payload, qos, retain))

    def is_connected(self):
        return True

    def subscribe(self, *a, **k):
        pass

    def topics(self):
        return [p[0] for p in self.pubs]

    def sur(self, topic):
        return [p for p in self.pubs if p[0] == topic]

    def vider(self):
        self.pubs.clear()


def device(lora=False):
    d = {"deviceId": DEVICE_ID, "model": "ben-1", "softwareVersion": "1.2.3",
         "capabilities": {"wired-tic": {}}}
    if lora:
        d["capabilities"] = {"lora-tic-receiver": {}}
    return d


def topics():
    return m.Topics("ben", "homeassistant", DEVICE_ID)


def cfg():
    return {**m.CONFIG_DEFAULTS, "host": "ha.local"}


def base_neuve():
    """(chemin, connexion en écriture) d'une base neuve."""
    path = str(pathlib.Path(tempfile.mkdtemp()) / "measurements.db")
    return path, db.connect(path)


def pont(path, *, lora=False):
    remote = FauxRemote()
    b = m.Bridge(cfg(), device(lora), remote=remote, local=object(),
                 open_db=lambda: db.connect(path, read_only=True))
    return b, remote


def reglages_temporaires():
    settings.SETTINGS_PATH = str(pathlib.Path(tempfile.mkdtemp()) / "settings.json")


def tout_texte(remote):
    """Tout ce que le pont a émis, sujets ET valeurs, en une chaîne."""
    return json.dumps([[t, p] for t, p, _, _ in remote.pubs], default=str)


def ecrire_json(obj):
    p = pathlib.Path(tempfile.mkdtemp()) / "cfg.json"
    p.write_text(json.dumps(obj))
    return str(p)


# ── Configuration ────────────────────────────────────────────────────────────

@cas
def config_sans_host_est_refusee():
    """Sans `host` le service ne saurait pas où pousser : mieux vaut échouer au
    démarrage (systemd le dit) que tourner à vide."""
    for brut in ({}, {"host": ""}, {"port": 1883}, []):
        try:
            m.load_config(ecrire_json(brut))
        except ValueError:
            continue
        raise AssertionError(f"{brut!r} accepté sans host")


@cas
def config_applique_les_defauts():
    """⚖️ Le témoin : un fichier minimal suffit."""
    c = m.load_config(ecrire_json({"host": "ha.local"}))
    assert c["host"] == "ha.local"
    assert c["port"] == 1883
    assert c["discovery_prefix"] == "homeassistant"
    assert c["base_topic"] == "ben"
    assert c["tls"] is False and c["username"] is None


@cas
def config_force_le_port_en_entier():
    """Un port saisi à la main entre guillemets ne doit pas atteindre paho en `str`."""
    c = m.load_config(ecrire_json({"host": "ha.local", "port": "8883"}))
    assert c["port"] == 8883 and isinstance(c["port"], int)


@cas
def config_n_ecrase_pas_ce_qui_est_donne():
    c = m.load_config(ecrire_json({"host": "h", "base_topic": "maison", "discovery_prefix": "ha"}))
    assert c["base_topic"] == "maison" and c["discovery_prefix"] == "ha"


@cas
def device_sans_device_id_est_refuse():
    try:
        m.load_device(ecrire_json({"model": "x"}))
    except ValueError:
        return
    raise AssertionError("device.json sans deviceId accepté")


# ── Clé de registre ──────────────────────────────────────────────────────────

@cas
def la_cle_de_registre_est_stable():
    assert m.register_key("TEMPO", 1, 2) == m.register_key("TEMPO", 1, 2)
    assert m.register_key("TEMPO", 1, 2) == "tempo_1_2"


@cas
def le_contrat_fait_partie_de_la_cle():
    """🚨 Même mode, même position, autre offre → autre capteur HA."""
    assert m.register_key("BASE", 1, 2) != m.register_key("TEMPO", 1, 2)
    assert m.register_key("TEMPO", 1, 2) != m.register_key("TEMPO", 0, 2)
    assert m.register_key("TEMPO", 1, 2) != m.register_key("TEMPO", 1, 3)


@cas
def le_contrat_est_slugge():
    """La clé finit dans un gabarit Jinja (`value_json.wh['…']`) et dans un unique_id :
    ni espace, ni ponctuation, ni majuscule."""
    k = m.register_key("Base / HC-Été 2", 1, 4)
    assert k.endswith("_1_4")
    assert all(c.isalnum() or c == "_" for c in k), k
    assert k == k.lower()
    assert not k.startswith("_")


@cas
def contrat_inconnu_donne_na():
    assert m.register_key(None, 0, 1) == "na_0_1"
    assert m.register_key("", 0, 1) == "na_0_1"
    assert m.register_key("///", 0, 1) == "na_0_1", "contrat sans aucun alphanumérique"


# ── Payloads d'état ──────────────────────────────────────────────────────────

REGS = [
    {"src_standard": 0, "index_id": 0, "index_value": 1000, "label": "Base", "last_ts": 1},
    {"src_standard": 1, "index_id": 1, "index_value": 2000, "label": "HC", "last_ts": 1},
    {"src_standard": 1, "index_id": 2, "index_value": 3000, "label": "HP", "last_ts": 1},
]


@cas
def registers_payload_donne_la_carte_wh():
    p = m.registers_payload("TEMPO", REGS, {"src_standard": 1})
    assert p["contract"] == "TEMPO"
    assert p["registers"] == REGS
    assert p["wh"] == {m.register_key("TEMPO", 0, 0): 1000,
                       m.register_key("TEMPO", 1, 1): 2000,
                       m.register_key("TEMPO", 1, 2): 3000}


@cas
def le_total_ne_somme_que_le_mode_courant():
    """Un boîtier passé d'historique à standard garde les deux familles de registres, qui
    comptent la MÊME énergie : les additionner la compterait deux fois."""
    assert m.registers_payload("TEMPO", REGS, {"src_standard": 1})["total_wh"] == 5000
    assert m.registers_payload("BASE", REGS, {"src_standard": 0})["total_wh"] == 1000


@cas
def le_total_sans_mesure_courante_somme_tout():
    assert m.registers_payload("X", REGS, None)["total_wh"] == 6000


@cas
def le_total_est_none_sans_valeur():
    """None et non 0 : un 0 ferait chuter un `total_increasing` déjà haut."""
    assert m.registers_payload("X", [], None)["total_wh"] is None
    vides = [{"src_standard": 0, "index_id": 0, "index_value": None}]
    p = m.registers_payload("X", vides, {"src_standard": 0})
    assert p["total_wh"] is None and p["wh"] == {}


@cas
def live_payload_ajoute_l_heure_iso():
    p = m.live_payload({"ts": 0, "papp": 5})
    assert p["time"] == "1970-01-01T00:00:00+00:00"
    assert p["papp"] == 5 and p["ts"] == 0
    assert m.live_payload({"ts": None})["time"] is None
    assert m.iso(None) is None


@cas
def event_payload_porte_le_type():
    p = m.event_payload({"type": "changement_offre", "id": "d:1", "ts": 0,
                         "severite": "info", "titre": "t", "corps": "c"})
    assert p["event_type"] == "changement_offre"
    assert p["id"] == "d:1" and p["time"] == "1970-01-01T00:00:00+00:00"


# ── Discovery ────────────────────────────────────────────────────────────────

def discovery(live_row=None, contract="BASE", regs=(), dev=None, n=0):
    return m.pdl_discovery(dev or device(), topics(), n, live_row, contract, list(regs))


ROW = {"ts": 10, "papp": 1, "src_standard": 0, "tempo_color": None, "inject_total": None}


@cas
def chaque_composant_a_plateforme_unique_id_et_disponibilite():
    """⚖️ Le témoin de la discovery."""
    d = discovery(ROW, regs=REGS)
    assert d["components"]
    for cle, c in d["components"].items():
        assert c["platform"], cle
        assert c["unique_id"], cle
        assert c["availability_topic"] == topics().status, cle
    ids = [c["unique_id"] for c in d["components"].values()]
    assert len(ids) == len(set(ids)), "unique_id dupliqué"


@cas
def les_unique_id_sont_uniques_entre_boitier_et_pdl():
    """Deux compteurs et le boîtier lui-même : HA rejette tout recoupement."""
    ids = [c["unique_id"] for c in m.box_discovery(device(), topics())["components"].values()]
    for n in (0, 1):
        ids += [c["unique_id"] for c in discovery(ROW, regs=REGS, n=n)["components"].values()]
    assert len(ids) == len(set(ids)), ids


@cas
def les_composants_energie_sont_cumulatifs_en_wh():
    """`total_increasing` + `energy` + Wh : c'est ce qui range le capteur dans le tableau
    Énergie de HA."""
    d = discovery(ROW, regs=REGS)
    energie = {k: c for k, c in d["components"].items() if k.startswith("energy")}
    assert "energy_total" in energie
    assert len(energie) >= 1 + len(REGS)
    for k, c in energie.items():
        assert c["device_class"] == "energy", k
        assert c["unit_of_measurement"] == "Wh", k
        assert c["state_class"] == "total_increasing", k


@cas
def un_composant_par_registre_cle_par_contrat():
    d = discovery(ROW, contract="TEMPO", regs=REGS)
    for r in REGS:
        k = m.register_key("TEMPO", r["src_standard"], r["index_id"])
        assert f"energy_{k}" in d["components"], k
        assert f"wh['{k}']" in d["components"][f"energy_{k}"]["value_template"]


@cas
def l_appareil_pdl_est_rattache_au_boitier():
    d = discovery(ROW)
    assert d["device"]["via_device"] == DEVICE_ID
    assert d["device"]["identifiers"] == [f"{DEVICE_ID}_pdl0"]
    assert m.box_discovery(device(), topics())["device"]["identifiers"] == [DEVICE_ID]


@cas
def la_discovery_ne_contient_pas_l_adco():
    """🔒 Même avec un ADCO connu de la base, rien dans la discovery ne le porte."""
    d = discovery(ROW, regs=REGS)
    assert ADCO not in json.dumps(d)
    assert "adco" not in json.dumps(d).lower()


@cas
def tempo_seulement_si_contrat_ou_couleur():
    assert "tempo_color" not in discovery(ROW, contract="BASE")["components"]
    assert "tempo_color" not in discovery(None, contract=None)["components"]
    assert "tempo_color" in discovery(ROW, contract="Tempo 2024")["components"]
    assert "tempo_color" in discovery({**ROW, "tempo_color": "rouge"}, contract="BASE")["components"]
    reg_couleur = [{**REGS[0], "tempo_color": "bleu"}]
    assert "tempo_color" in discovery(ROW, contract="BASE", regs=reg_couleur)["components"]
    c = discovery(ROW, contract="TEMPO")["components"]["tempo_color"]
    assert c["options"] == ["bleu", "blanc", "rouge"]


@cas
def lora_seulement_si_le_boitier_recoit_du_lora():
    sans = discovery(ROW)["components"]
    assert "lora_rssi" not in sans and "lora_snr" not in sans
    avec = discovery(ROW, dev=device(lora=True))["components"]
    assert "lora_rssi" in avec and "lora_snr" in avec
    assert avec["lora_rssi"]["state_topic"] == topics().pdl(0, "link")


@cas
def injection_seulement_si_un_index_d_injection_existe():
    assert "energy_injected" not in discovery(ROW)["components"]
    assert "energy_injected" not in discovery(None)["components"]
    c = discovery({**ROW, "inject_total": 1234})["components"]["energy_injected"]
    assert c["state_class"] == "total_increasing" and c["device_class"] == "energy"
    assert c["state_topic"] == topics().pdl(0, "live")


@cas
def les_types_d_evenement_annonces_sont_ceux_de_db():
    """HA refuse un `event_type` hors de la liste annoncée : si db.py en émet un nouveau,
    ce banc doit le dire (cf. commentaire de EVENT_TYPES)."""
    src = pathlib.Path(db.__file__).read_text()
    for t in m.EVENT_TYPES:
        assert f'"{t}"' in src, f"{t} n'est plus émis par db.py"
    c = discovery(ROW)["components"]["event"]
    assert c["event_types"] == m.EVENT_TYPES


# ── Le pont ──────────────────────────────────────────────────────────────────

@cas
def un_signal_pdl_publie_discovery_puis_live_puis_registres():
    """⚖️ Témoin du pont. La discovery d'ABORD : un registre doit exister dans HA avant que
    sa valeur n'arrive."""
    path, conn = base_neuve()
    db.record_measurement(conn, 0, LABELS, ts=1_000_000)
    b, r = pont(path)
    b.handle([("pdl", 0)])
    disco = f"homeassistant/device/{DEVICE_ID}_pdl0/config"
    t_live = f"ben/{DEVICE_ID}/pdl/0/live"
    t_regs = f"ben/{DEVICE_ID}/pdl/0/registers"
    ts = r.topics()
    assert disco in ts and t_live in ts and t_regs in ts, ts
    assert ts.index(disco) < ts.index(t_live) < ts.index(t_regs)
    _, payload, qos, retain = r.sur(disco)[0]
    assert retain is True and qos == 1
    assert json.loads(payload)["device"]["via_device"] == DEVICE_ID
    _, payload, _, retain = r.sur(t_live)[0]
    live = json.loads(payload)
    assert retain is True
    assert live["papp"] == 2290
    assert live["time"] == m.iso(1_000_000) == "1970-01-12T13:46:40+00:00"
    _, payload, _, retain = r.sur(t_regs)[0]
    regs = json.loads(payload)
    assert retain is True
    assert regs["total_wh"] == 28883000 and len(regs["wh"]) == 1


@cas
def jamais_d_adco_dans_ce_que_le_pont_publie():
    """🔒 Le cas qui compte : un PDL réellement créé AVEC son ADCO dans la table `pdl`,
    puis tout l'état poussé (full + signal) — ni sujet ni valeur ne le contient."""
    path, conn = base_neuve()
    assert db.resolve_pdl(conn, ADCO, graine=0) == 0
    db.record_measurement(conn, 0, LABELS, ts=1_000_000)
    db.record_ngtf(conn, 0, "TEMPO", DEVICE_ID)
    db.record_ngtf(conn, 0, "BASE", DEVICE_ID)       # un événement existe
    assert conn.execute("SELECT adco FROM pdl").fetchone()[0] == ADCO, "précondition"
    reglages_temporaires()
    b, r = pont(path, lora=True)
    b.handle([("full", None)])
    b.handle([("pdl", 0)])
    assert r.pubs
    assert ADCO not in tout_texte(r), "ADCO publié"
    assert "adco" not in tout_texte(r).lower()


@cas
def deuxieme_signal_identique_ne_republie_pas_la_discovery():
    path, conn = base_neuve()
    db.record_measurement(conn, 0, LABELS, ts=1_000_000)
    b, r = pont(path)
    disco = f"homeassistant/device/{DEVICE_ID}_pdl0/config"
    b.handle([("pdl", 0)])
    assert len(r.sur(disco)) == 1
    r.vider()
    b.handle([("pdl", 0)])
    assert r.sur(disco) == [], "discovery renvoyée alors que rien n'a changé"
    assert r.sur(f"ben/{DEVICE_ID}/pdl/0/live"), "l'état, lui, doit repartir"


@cas
def full_republie_la_discovery_et_refresh_non():
    path, conn = base_neuve()
    db.record_measurement(conn, 0, LABELS, ts=1_000_000)
    reglages_temporaires()
    b, r = pont(path)
    d_pdl = f"homeassistant/device/{DEVICE_ID}_pdl0/config"
    d_box = f"homeassistant/device/{DEVICE_ID}/config"
    b.handle([("full", None)])
    assert len(r.sur(d_pdl)) == 1 and len(r.sur(d_box)) == 1
    assert r.sur(f"ben/{DEVICE_ID}/status")[0][1] == "online"
    r.vider()
    b.handle([("refresh", None)])
    assert r.sur(d_pdl) == [] and r.sur(d_box) == [], "refresh a renvoyé la discovery"
    assert r.sur(f"ben/{DEVICE_ID}/pdl/0/live"), "refresh doit rafraîchir l'état"
    r.vider()
    b.handle([("full", None)])
    assert len(r.sur(d_pdl)) == 1 and len(r.sur(d_box)) == 1, "full doit forcer"
    assert r.sur(d_pdl)[0][3] is True


@cas
def la_discovery_est_renvoyee_quand_elle_change():
    """Un nouveau registre (bascule HC→HP vue pour la première fois) change la discovery :
    elle doit repartir sans attendre un `full`."""
    path, conn = base_neuve()
    db.record_measurement(conn, 0, LABELS, ts=1_000_000)
    b, r = pont(path)
    disco = f"homeassistant/device/{DEVICE_ID}_pdl0/config"
    b.handle([("pdl", 0)])
    r.vider()
    db.record_measurement(conn, 0, {**LABELS, "_index_id": 1, "_index_value": 5}, ts=1_000_060)
    b.handle([("pdl", 0)])
    assert len(r.sur(disco)) == 1
    assert len(json.loads(r.sur(disco)[0][1])["components"]) > 0


@cas
def les_signaux_d_un_meme_pdl_sont_fusionnes():
    path, conn = base_neuve()
    db.record_measurement(conn, 0, LABELS, ts=1_000_000)
    b, r = pont(path)
    b.handle([("pdl", 0), ("pdl", 0), ("pdl", 0)])
    assert len(r.sur(f"ben/{DEVICE_ID}/pdl/0/live")) == 1


@cas
def deux_pdl_distincts_sont_publies_chacun_une_fois():
    path, conn = base_neuve()
    db.record_measurement(conn, 0, LABELS, ts=1_000_000)
    db.record_measurement(conn, 1, {**LABELS, "PAPP": 42}, ts=1_000_000)
    b, r = pont(path)
    b.handle([("pdl", 1), ("pdl", 0), ("pdl", 1)])
    assert len(r.sur(f"ben/{DEVICE_ID}/pdl/0/live")) == 1
    assert len(r.sur(f"ben/{DEVICE_ID}/pdl/1/live")) == 1
    assert json.loads(r.sur(f"ben/{DEVICE_ID}/pdl/1/live")[0][1])["papp"] == 42


@cas
def sans_mesure_pas_de_live_mais_les_registres_partent():
    """Un PDL connu mais muet ne doit pas publier un `live` vide qui écraserait le dernier
    état retenu."""
    path, conn = base_neuve()
    db.resolve_pdl(conn, ADCO, graine=0)
    b, r = pont(path)
    b.handle([("pdl", 0)])
    assert r.sur(f"ben/{DEVICE_ID}/pdl/0/live") == []
    assert r.sur(f"ben/{DEVICE_ID}/pdl/0/registers")


@cas
def le_lien_lora_est_publie_seulement_sur_un_boitier_lora():
    path, conn = base_neuve()
    db.record_measurement(conn, 0, LABELS, ts=1_000_000)
    db.record_lora_link(conn, 0, -97, 7.5, ts=1_000_000)
    t_link = f"ben/{DEVICE_ID}/pdl/0/link"
    b, r = pont(path, lora=False)
    b.handle([("pdl", 0)])
    assert r.sur(t_link) == []
    b, r = pont(path, lora=True)
    b.handle([("pdl", 0)])
    lien = json.loads(r.sur(t_link)[0][1])
    assert lien == {"ts": 1_000_000, "rssi": -97, "snr": 7.5}


# ── Événements ───────────────────────────────────────────────────────────────

@cas
def le_premier_passage_amorce_sans_rejouer_l_historique():
    path, conn = base_neuve()
    db.record_measurement(conn, 0, LABELS, ts=1_000_000)
    db.record_ngtf(conn, 0, "BASE", DEVICE_ID)
    db.record_ngtf(conn, 0, "TEMPO", DEVICE_ID)      # événement ANCIEN, avant le pont
    assert db.last_event(conn)[0] is not None, "précondition : un événement existe"
    b, r = pont(path)
    b.handle([("pdl", 0)])
    assert r.sur(f"ben/{DEVICE_ID}/pdl/0/event") == [], "l'historique a été rejoué"
    assert b.events_primed


@cas
def un_changement_d_offre_est_publie_une_fois():
    path, conn = base_neuve()
    db.record_measurement(conn, 0, LABELS, ts=1_000_000)
    db.record_ngtf(conn, 0, "BASE", DEVICE_ID)
    b, r = pont(path)
    b.handle([("pdl", 0)])                           # amorçage
    time.sleep(0.01)                                 # uuid7 : id strictement croissant
    db.record_ngtf(conn, 0, "TEMPO", DEVICE_ID)
    b.handle([("pdl", 0)])
    evs = r.sur(f"ben/{DEVICE_ID}/pdl/0/event")
    assert len(evs) == 1, evs
    _, payload, qos, retain = evs[0]
    ev = json.loads(payload)
    assert ev["event_type"] == "changement_offre"
    assert ev["id"].startswith(f"{DEVICE_ID}:")
    assert retain is False, "un événement retenu serait rejoué à chaque connexion HA"
    assert qos == 1
    r.vider()
    b.handle([("pdl", 0)])
    assert r.sur(f"ben/{DEVICE_ID}/pdl/0/event") == [], "événement republié"


@cas
def un_changement_de_mode_est_publie_sur_son_pdl():
    path, conn = base_neuve()
    db.record_measurement(conn, 1, LABELS, ts=1_000_000)
    db.record_tic_mode(conn, 1, 0, DEVICE_ID)
    b, r = pont(path)
    b.handle([("pdl", 1)])
    time.sleep(0.01)
    db.record_tic_mode(conn, 1, 1, DEVICE_ID)
    b.handle([("pdl", 1)])
    evs = r.sur(f"ben/{DEVICE_ID}/pdl/1/event")
    assert len(evs) == 1
    assert json.loads(evs[0][1])["event_type"] == "changement_mode"
    assert r.sur(f"ben/{DEVICE_ID}/pdl/0/event") == []


@cas
def un_type_inconnu_ou_sans_pdl_n_est_pas_publie():
    """HA refuse un `event_type` hors liste ; un événement sans compteur n'a pas de sujet."""
    path, conn = base_neuve()
    db.record_measurement(conn, 0, LABELS, ts=1_000_000)
    b, r = pont(path)
    b.handle([("pdl", 0)])
    time.sleep(0.01)
    db.record_event(conn, DEVICE_ID, "type_exotique", "t", "c", pdl_index=0)
    time.sleep(0.01)
    db.record_event(conn, DEVICE_ID, "changement_offre", "t", "c", pdl_index=None)
    b.handle([("pdl", 0)])
    assert r.sur(f"ben/{DEVICE_ID}/pdl/0/event") == []
    assert not [t for t in r.topics() if t.endswith("/event")]


# ── LED ──────────────────────────────────────────────────────────────────────

@cas
def set_led_borne_a_cinq():
    reglages_temporaires()
    b, r = pont(base_neuve()[0])
    b.set_led("9")
    pubs = r.sur(f"ben/{DEVICE_ID}/led_level")
    assert [p[1] for p in pubs] == ["5"]
    assert pubs[0][3] is True
    assert settings.load()["led_level"] == 5


@cas
def set_led_accepte_un_decimal_et_borne_a_zero():
    reglages_temporaires()
    b, r = pont(base_neuve()[0])
    b.set_led("2.7")
    b.set_led("-4")
    assert [p[1] for p in r.sur(f"ben/{DEVICE_ID}/led_level")] == ["2", "0"]


@cas
def set_led_illisible_ne_publie_rien_et_ne_leve_pas():
    reglages_temporaires()
    b, r = pont(base_neuve()[0])
    b.set_led("abc")
    b.set_led("")
    b.set_led("inf")      # int(float("inf")) lève OverflowError, pas ValueError
    assert r.pubs == []
    assert not pathlib.Path(settings.SETTINGS_PATH).exists(), "réglage écrit pour une commande illisible"


@cas
def la_commande_led_passe_par_handle():
    reglages_temporaires()
    b, r = pont(base_neuve()[0])
    b.handle([("led", "4")])
    assert [p[1] for p in r.sur(f"ben/{DEVICE_ID}/led_level")] == ["4"]


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
