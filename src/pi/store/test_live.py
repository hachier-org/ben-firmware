#!/usr/bin/env python3
"""Banc de `live.rows` — la fabrique UNIQUE de la forme de `/live` et de l'état MQTT.

`live.py` a été sorti tel quel du handler `/live` de local_api.py pour servir deux
consommateurs (l'API locale et ben-mqtt-ha). Ce banc fige ce que la fabrique doit
rendre, pour qu'un déplacement de code ne puisse plus changer la forme en silence :

  · ⚖️ le TÉMOIN rend toutes les clés enrichies — sans lui, une fabrique qui ne rendrait
    que les colonnes SQL passerait tous les autres cas ;
  · sans mesure → `[]`, jamais un dict à moitié vide : ben-mqtt-ha s'en sert pour savoir
    s'il y a un état à publier ;
  · le filtre `pdl_index` est ce qui évite le balayage de 3 M de lignes (cf. live.py) ET ce
    qui empêche de publier la conso d'un voisin sous le mauvais compteur ;
  · ISOUSC est PAR PHASE (#50) : sans le facteur, la jauge d'un triphasé sature au tiers ;
  · la jauge se cale sur l'abonnement tant que le foyer n'est pas connu (pas de faux rouge
    à l'unboxing), et sur le plafond observé ensuite.

    python3 src/pi/store/test_live.py
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import db  # noqa: E402
import levels  # noqa: E402
import live  # noqa: E402

CAS = []

CLES = ("ts", "pdl_index", "papp", "iinst", "index_value", "src_standard", "inject_total",
        "level", "tic_mode", "producer", "isousc", "pref", "phases", "maxVa", "plafond",
        "injectMax", "tariff_label", "tempo_color", "contract")

LABELS = {"PAPP": 2290, "IINST": 10, "BASE": 28883000,
          "_src_standard": 0, "_index_id": 0, "_index_value": 28883000}


def cas(fn):
    CAS.append(fn)
    return fn


def neuf():
    return db.connect(str(pathlib.Path(tempfile.mkdtemp()) / "measurements.db"))


# ── Le témoin ────────────────────────────────────────────────────────────────

@cas
def une_mesure_donne_une_ligne_enrichie():
    """⚖️ LE TÉMOIN. Une mesure → une ligne, avec TOUTES les clés que /live et la
    discovery MQTT lisent."""
    conn = neuf()
    db.record_measurement(conn, 0, LABELS, ts=1_000_000)
    out = live.rows(conn, 0)
    assert len(out) == 1, out
    row = out[0]
    for k in CLES:
        assert k in row, f"clé {k!r} absente de la ligne : {sorted(row)}"
    assert row["papp"] == 2290
    assert row["iinst"] == 10
    assert row["index_value"] == 28883000
    assert row["ts"] == 1_000_000
    assert row["tic_mode"] == "historique"


@cas
def sans_mesure_rien_du_tout():
    conn = neuf()
    assert live.rows(conn, 0) == []


@cas
def le_filtre_pdl_ne_rend_que_le_dernier_point_du_bon_compteur():
    """Deux compteurs, plusieurs points : le résultat est LE dernier point (par ts, pas
    par ordre d'insertion) du PDL demandé, jamais celui d'un autre."""
    conn = neuf()
    db.record_measurement(conn, 0, {**LABELS, "PAPP": 111}, ts=3_000_000)
    db.record_measurement(conn, 1, {**LABELS, "PAPP": 500}, ts=1_000_000)
    db.record_measurement(conn, 1, {**LABELS, "PAPP": 700}, ts=2_000_000)
    db.record_measurement(conn, 1, {**LABELS, "PAPP": 600}, ts=1_500_000)   # inséré après, plus ancien
    out = live.rows(conn, 1)
    assert len(out) == 1
    assert out[0]["pdl_index"] == 1
    assert out[0]["papp"] == 700 and out[0]["ts"] == 2_000_000, out[0]
    assert live.rows(conn, 0)[0]["papp"] == 111
    assert live.rows(conn, 2) == []


# ── L'abonnement et la jauge ─────────────────────────────────────────────────

@cas
def maxva_historique_monophase():
    conn = neuf()
    db.record_measurement(conn, 0, LABELS, ts=1_000_000)
    db.record_isousc(conn, 0, 30)
    row = live.rows(conn, 0)[0]
    assert row["isousc"] == 30 and row["phases"] == 1
    assert row["maxVa"] == 30 * 230


@cas
def maxva_historique_triphase_multiplie_par_les_phases():
    """🚨 #50 : 20 A par phase sur un triphasé = 13 800 VA, pas 4 600."""
    conn = neuf()
    db.record_measurement(conn, 0, LABELS, ts=1_000_000)
    db.record_isousc(conn, 0, 20)
    db.record_phases(conn, 0, 3)
    row = live.rows(conn, 0)[0]
    assert row["phases"] == 3
    assert row["maxVa"] == 20 * 230 * 3 == 13800


@cas
def maxva_standard_prend_pref_en_kva():
    """En standard, PREF (kVA) est déjà un total : pas de facteur de phases."""
    conn = neuf()
    db.record_measurement(conn, 0, {**LABELS, "_src_standard": 1}, ts=1_000_000)
    db.record_isousc(conn, 0, 20)
    db.record_pref(conn, 0, 6)
    row = live.rows(conn, 0)[0]
    assert row["tic_mode"] == "standard"
    assert row["maxVa"] == 6000


@cas
def plafond_replie_sur_l_abonnement_tant_que_le_foyer_est_inconnu():
    """Base neuve : le foyer n'est pas CONNU, donc la jauge se cale sur l'abonnement —
    pas sur le high-water mark (= la conso courante), qui peindrait tout en rouge."""
    conn = neuf()
    db.record_measurement(conn, 0, LABELS, ts=1_000_000)
    db.record_isousc(conn, 0, 20)
    assert not levels.is_known(conn, 0)
    row = live.rows(conn, 0)[0]
    assert row["maxVa"] == 4600
    assert row["plafond"] == 4600, "le plafond observé (2290) a fui avant que le foyer soit connu"
    assert row["injectMax"] == 4600


@cas
def plafond_replie_sur_le_defaut_sans_abonnement():
    conn = neuf()
    db.record_measurement(conn, 0, LABELS, ts=1_000_000)
    row = live.rows(conn, 0)[0]
    assert row["isousc"] is None and row["pref"] is None
    assert row["maxVa"] is None
    assert row["plafond"] == live.DEFAULT_MAXVA == 9000
    assert row["injectMax"] == live.DEFAULT_MAXVA


@cas
def plafond_observe_une_fois_le_foyer_connu():
    """L'autre moitié du gate : profil représentatif → le plafond OBSERVÉ prend le relais."""
    conn = neuf()
    db.record_measurement(conn, 0, LABELS, ts=1_000_000)
    db.record_isousc(conn, 0, 20)
    conn.execute(
        "UPDATE level_profile SET talon=100, papp_max_alltime=3300, n_samples=?, span_sec=? "
        "WHERE pdl_index=0", (levels.MIN_SAMPLES, levels.MIN_SPAN_SEC))
    conn.commit()
    assert levels.is_known(conn, 0)
    row = live.rows(conn, 0)[0]
    assert row["plafond"] == 3300, row
    assert row["maxVa"] == 4600


@cas
def contrat_et_inject_total_viennent_de_la_base():
    conn = neuf()
    db.record_measurement(conn, 0, {**LABELS, "_inject_total": 1234}, ts=1_000_000)
    db.record_ngtf(conn, 0, "TEMPO")
    row = live.rows(conn, 0)[0]
    assert row["contract"] == "TEMPO"
    assert row["inject_total"] == 1234


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
