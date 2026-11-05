#!/usr/bin/env python3
"""
Banc du TRIPHASÉ — le courant doit être le TOTAL des trois phases.

⭐ CE BANC EXISTE PARCE QUE LE DÉFAUT ÉTAIT SILENCIEUX EN STANDARD. En historique
   un triphasé ne produisait RIEN (`IINST` n'est jamais émis, c'est `IINST1/2/3`)
   et ça se voyait. En standard `IRMS1` existe, il était mappé vers `IINST`, et le
   boîtier publiait tranquillement **le courant d'une phase sur trois**. Aucune
   alerte, une courbe plausible, une donnée fausse. C'est ce cas-là que ce banc
   garde en premier.

🚨 TRAMES FORGÉES, PAS LA CAPTURE RÉELLE. Le dépôt est PUBLIC : un ADCO réel n'y
   entre pas. Les trames ci-dessous reproduisent la structure observée le
   2026-10-17 sur un vrai triphasé historique (IINST1/2/3, IMAX1/2/3, PPOT) avec
   une adresse inventée.

⭐ Et le critère de la somme est la SIMULTANÉITÉ, pas la nature de la grandeur :
   `IINST1/2/3` sont trois mesures du MÊME instant, donc `230 × ΣI` est bien la
   puissance de cet instant. `IMAX1/2/3` sont trois extrêmes datés séparément —
   `Σ max ≠ max Σ` — donc ils ne s'agrègent pas, et le banc le garde aussi.

Lancement :  python3 test_triphase.py
"""
import sys
from unittest.mock import MagicMock

for _n in ("RPi", "RPi.GPIO", "serial", "settings"):
    sys.modules.setdefault(_n, MagicMock())
sys.modules["RPi"].GPIO = sys.modules["RPi.GPIO"]

import main_uart as m  # noqa: E402

echecs = []


def verifie(nom, obtenu, attendu):
    if obtenu == attendu:
        print(f"  ✓ {nom}")
    else:
        print(f"  ✗ {nom}\n      attendu : {attendu!r}\n      obtenu  : {obtenu!r}")
        echecs.append(nom)


# ── Fabrication de groupes VALIDES ──────────────────────────────────────────
#
# 🚨 Le checksum se calcule, il ne se recopie pas : un banc dont les groupes
#    seraient refusés au checksum passerait « vert » en ne testant RIEN.
def groupe_histo(label, valeur):
    """'LABEL VALEUR CHK' — checksum sur label+SP+valeur, SP de queue EXCLU."""
    corps = f"{label} {valeur}"
    return corps + " " + chr((sum(ord(c) for c in corps) & 0x3F) + 0x20)


def groupe_std(label, donnee):
    """'LABEL<HT>DONNEE<HT>CHK' — checksum HT de queue INCLUS."""
    corps = f"{label}\t{donnee}\t"
    return corps + chr((sum(ord(c) for c in corps) & 0x3F) + 0x20)


def lire_histo(groupes):
    out = {}
    for label, valeur in groupes:
        ligne = groupe_histo(label, valeur)
        assert m.tic_checksum_ok(ligne), f"groupe forgé invalide : {ligne!r}"
        m._parse_label(ligne, out)
    return out


def lire_std(groupes):
    out = {}
    for label, donnee in groupes:
        ligne = groupe_std(label, donnee)
        assert m.tic_checksum_std_ok(ligne), f"groupe forgé invalide : {ligne!r}"
        m._parse_label_std(ligne, out)
    return out


# ── Les trames ──────────────────────────────────────────────────────────────
TRI_HISTO = [
    ("ADCO", "000000000000"), ("OPTARIF", "HC.."), ("ISOUSC", "20"),
    ("HCHC", "030046428"), ("HCHP", "040529209"), ("PTEC", "HP.."),
    ("IINST1", "012"), ("IINST2", "004"), ("IINST3", "007"),
    ("IMAX1", "060"), ("IMAX2", "060"), ("IMAX3", "060"),
    ("PMAX", "05070"), ("PAPP", "05290"), ("HHPHC", "A"),
    ("MOTDETAT", "000000"), ("PPOT", "00"),
]
MONO_HISTO = [
    ("ADCO", "000000000000"), ("OPTARIF", "HC.."), ("ISOUSC", "30"),
    ("HCHC", "030046428"), ("HCHP", "040529209"), ("PTEC", "HP.."),
    ("IINST", "009"), ("IMAX", "090"), ("PAPP", "02070"),
    ("HHPHC", "A"), ("MOTDETAT", "000000"),
]
TRI_STD = [
    ("ADSC", "000000000000"), ("NGTF", "BASE"), ("SINSTS", "005290"),
    ("IRMS1", "012"), ("IRMS2", "004"), ("IRMS3", "007"),
    ("URMS1", "233"), ("URMS2", "232"), ("URMS3", "234"),
    ("EAST", "070575637"), ("PREF", "12"),
]
MONO_STD = [
    ("ADSC", "000000000000"), ("NGTF", "BASE"), ("SINSTS", "002070"),
    ("IRMS1", "009"), ("URMS1", "233"), ("EAST", "070575637"), ("PREF", "09"),
]

print("── historique ──────────────────────────────────────────────")
tri = lire_histo(TRI_HISTO)
verifie("triphasé : IINST = 12 + 4 + 7 (⚖️ DOIT TOMBER sans le correctif)",
        tri.get("IINST"), 23)
verifie("triphasé : PHASES = 3", tri.get("PHASES"), 3)
verifie("triphasé : PAPP inchangé", tri.get("PAPP"), 5290)
verifie("triphasé : ISOUSC lu BRUT, par phase", tri.get("ISOUSC"), 20)
verifie("⚖️ IMAX n'est PAS agrégé (Σ max ≠ max Σ)", tri.get("IMAX"), None)

mono = lire_histo(MONO_HISTO)
verifie("⚖️ CONTRE-TÉMOIN mono : IINST inchangé", mono.get("IINST"), 9)
verifie("⚖️ CONTRE-TÉMOIN mono : pas de PHASES=3", mono.get("PHASES"), None)

print("── standard ────────────────────────────────────────────────")
tri_s = lire_std(TRI_STD)
verifie("triphasé : IINST = 12 + 4 + 7, PAS 12 (⚖️ le cas SILENCIEUX)",
        tri_s.get("IINST"), 23)
verifie("triphasé : PHASES = 3", tri_s.get("PHASES"), 3)
verifie("triphasé : PAPP = SINSTS (déjà un total)", tri_s.get("PAPP"), 5290)
verifie("triphasé : PREF lu brut (déjà un TOTAL en kVA)", tri_s.get("PREF"), 12)

mono_s = lire_std(MONO_STD)
verifie("⚖️ CONTRE-TÉMOIN mono : IINST = IRMS1", mono_s.get("IINST"), 9)
verifie("⚖️ CONTRE-TÉMOIN mono : pas de PHASES=3", mono_s.get("PHASES"), None)

print("── cohérence puissance ─────────────────────────────────────")
# 🚨 LE VRAI ENJEU : `db.estimer_papp` fait 230 × IINST quand PAPP vaut 0.
#    Avec le max au lieu de la somme, ce secours sous-estimerait d'un facteur 3.
verifie("230 × IINST ≈ PAPP (23×230=5290)", 230 * (tri.get("IINST") or 0), 5290)

print("── trame INCOMPLÈTE : une somme partielle est une donnée FAUSSE ──")
# 🚨 LE DÉFAUT QUE CES CAS GARDENT : si `IINST2` tombe au checksum, `I1 + I3` est
#    un nombre PLAUSIBLE et FAUX, et il serait écrit — là où le monophasé perdant
#    son `IINST` voyait sa trame REJETÉE. Une donnée fausse à la place d'une
#    donnée perdue, c'est l'inversion exacte que ce projet combat.
sans_phase2 = [g for g in TRI_HISTO if g[0] != "IINST2"]
inc = lire_histo(sans_phase2)
verifie("histo, IINST2 perdu → IINST ABSENT (⚖️ et pas 12+7=19)",
        inc.get("IINST"), None)
verifie("   la trame reste reconnue triphasée", inc.get("PHASES"), 3)
verifie("   deux phases vues sur trois", len(inc.get("_phases") or {}), 2)

sans_phase3 = [g for g in TRI_HISTO if g[0] != "IINST3"]
verifie("histo, IINST3 perdu → IINST ABSENT",
        lire_histo(sans_phase3).get("IINST"), None)

sans_irms2 = [g for g in TRI_STD if g[0] != "IRMS2"]
verifie("standard, IRMS2 perdu → IINST ABSENT",
        lire_std(sans_irms2).get("IINST"), None)

verifie("⚖️ CONTRE-TÉMOIN : les trois présentes → la somme revient",
        lire_histo(TRI_HISTO).get("IINST"), 23)
verifie("⚖️ CONTRE-TÉMOIN : mono standard (IRMS1 SEUL) n'est PAS amputé",
        lire_std(MONO_STD).get("IINST"), 9)

print("── le cas 1-phase-sur-3, que seule la MÉMOIRE sépare ───────")
# ⚠️ Une trame qui perd IINST2 ET IINST3 est indiscernable d'un monophasé au
#    décodage : `PHASES` n'est pas posé, `IINST` vaut I1. C'est la garde de trame
#    (`last_phases == 3`, rechargée depuis la base) qui la rejette, pas le
#    décodeur. On vérifie donc ici ce que le décodeur EXPOSE pour la rendre
#    possible — le compteur de phases vues.
seule1 = [g for g in TRI_HISTO if g[0] not in ("IINST2", "IINST3")]
s1 = lire_histo(seule1)
verifie("décodeur seul : indiscernable d'un mono (IINST = 12)", s1.get("IINST"), 12)
verifie("   et PHASES n'est PAS posé", s1.get("PHASES"), None)
verifie("⭐ mais `_phases` expose 1/3 — c'est ce qui rend la garde possible",
        len(s1.get("_phases") or {}), 1)

print("── inspection : les deux gardes de trame existent ──────────")
import io as _io, os as _os, pathlib as _pl, tokenize as _tk  # noqa: E402
_src = _pl.Path(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                              "main_uart.py")).read_text()
_code = " ".join(tok.string for tok in _tk.generate_tokens(_io.StringIO(_src).readline)
                 if tok.type not in (_tk.COMMENT, _tk.STRING))
verifie("main_uart garde les trames triphasées incomplètes (×2 : histo + std)",
        _code.count("last_phases == 3 and len"), 2)
verifie("le raccordement est relu depuis la base (get_phases)",
        "db . get_phases ( measurements_db , pdl )" in _code, True)

print("── stockage : record_phases / get_phases ───────────────────")
import io, os, pathlib, tempfile, tokenize  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "store"))
import db  # noqa: E402

with tempfile.TemporaryDirectory() as rep:
    # ⚠️ Passer le chemin EN ARGUMENT : `connect(path=DB_PATH)` fixe son défaut
    #    à l'import, donc réassigner `db.DB_PATH` n'a aucun effet — le banc
    #    tenterait d'écrire dans /var/lib et échouerait hors du Pi.
    conn = db.connect(os.path.join(rep, "m.db"))

    verifie("rien d'enregistré → get_phases rend 1 (un ENTIER, pas None)",
            db.get_phases(conn, 0), 1)
    verifie("⚖️ record_phases(1) N'ÉCRIT RIEN — le mono est l'absence de preuve",
            db.record_phases(conn, 0, 1), False)
    verifie("   et get_phases vaut toujours 1", db.get_phases(conn, 0), 1)
    verifie("record_phases(3) écrit", db.record_phases(conn, 0, 3), True)
    verifie("   get_phases rend 3", db.get_phases(conn, 0), 3)
    verifie("⚖️ 2e appel identique → write-on-change, aucune écriture",
            db.record_phases(conn, 0, 3), False)
    # 🚨 LE CAS QUI COMPTE : une trame abîmée qui perd IINST2/IINST3 ne doit PAS
    #    faire retomber un triphasé en monophasé — la jauge repartirait au tiers.
    db.record_phases(conn, 0, 1)
    verifie("⚖️ une trame qui paraît mono NE DÉFAIT PAS le triphasé",
            db.get_phases(conn, 0), 3)
    verifie("un autre PDL reste à 1", db.get_phases(conn, 1), 1)

    # L'arithmétique de la jauge, telle que /live l'applique.
    isousc = 20
    verifie("maxVa triphasé = 20 × 230 × 3", isousc * 230 * db.get_phases(conn, 0), 13800)
    verifie("maxVa mono     = 20 × 230 × 1", isousc * 230 * db.get_phases(conn, 1), 4600)
    conn.close()

print("── inspection : /live multiplie bien par phases ────────────")
# ⚠️ COMMENTAIRES RETIRÉS AVANT DE CHERCHER. La prose qui explique la règle cite
#    `isousc * 230 * phases` et suffirait à faire passer ce contrôle sur un code
#    qui ne l'applique plus. On lit le CODE, pas ce qu'il raconte.
# ⓘ La forme de /live vit dans store/live.py depuis 0.16.0 (partagée avec ben-mqtt-ha).
src = pathlib.Path(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "store", "live.py")).read_text()
sans_commentaires = []
for tok in tokenize.generate_tokens(io.StringIO(src).readline):
    if tok.type not in (tokenize.COMMENT, tokenize.STRING):
        sans_commentaires.append(tok.string)
code = " ".join(sans_commentaires)
verifie("le CODE de /live (live.py) contient `isousc * 230 * phases`",
        "isousc * 230 * phases" in code, True)
verifie("⚖️ et plus l'ancien `isousc * 230 if`",
        "isousc * 230 if isousc" in code, False)

print()
if echecs:
    print(f"❌ {len(echecs)} cas en échec : " + ", ".join(echecs))
    sys.exit(1)
print("✅ tous les cas passent")
