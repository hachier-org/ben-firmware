"""
tic-reader — BEN Pi wired TIC reader (bi-mode : historique + standard)

Lit la TIC Linky sur l'UART et décode les deux modes Enedis (cf.
Enedis-NOI-CPT_54E v3, §5.2/5.3 et §6) :
  - HISTORIQUE : 1200 baud, séparateur SP (0x20), étiquettes courtes
    (ADCO/BASE/PAPP/IINST/PTEC…), checksum SP de queue EXCLU.
  - STANDARD   : 9600 baud, séparateur HT (0x09), étiquettes longues
    (ADSC/SINSTS/IRMS1/EAST/EASFxx/NTARF…), groupes parfois horodatés,
    checksum HT de queue INCLUS.

Le mode est **auto-détecté au boot** (on sonde chaque débit et on compte les
groupes au checksum valide ; le mode persisté est testé en premier → reboot
rapide). Le compteur sort d'usine en historique ; Enedis le reprogramme en
standard. Si Enedis rebascule le mode, le watchdog relance le process et la
détection re-sonde les deux débits.

v1 : aucun sink (pas d'InfluxDB). Le store SQLite local alimente l'API locale.

Aligné sur src/arduino/tic-reader/tic-reader.ino :
  - même mapping PTEC → index (selectActiveIndex), mode historique
  - même détection DEMAIN / ADPS / PEJP (buildFlags)

Note parité : on lit en 8N1 et on VÉRIFIE le bit de parité avant de masquer, dans
les deux modes. La TIC est 7E1 : sur un port 8N1 le bit de parité arrive en bit 7,
et `tic_parite.octet_valide()` le contrôle.

⚠️ Une version antérieure de ce texte disait que « le masque suffit, le checksum
TIC couvre l'intégrité ». C'EST FAUX, et c'est ce qui a motivé le changement : le
checksum vaut (somme & 0x3F) + 0x20, il ne voit donc la somme QUE MODULO 64 et
est structurellement aveugle aux basculements du bit 6. Trois ADCO fantômes sur
ben-0004 en portent la trace — toujours un chiffre devenu lettre.

⭐ La parité, elle, attrape tout basculement d'un seul bit, dont celui-là.
Marche sur mini-UART (ttyS0, sans parité matérielle) comme sur PL011 : c'est un
contrôle LOGICIEL, il ne dépend d'aucune capacité du port.

Stockage (chantier index bi-mode, docs/chantier-index-energie-bimode.md) : on
remplit les colonnes GÉNÉRIQUES de measurements — (src_standard, index_id,
index_value) + inject_total (EAIT) + meter_ts (horodate compteur). En historique
index_id = rang PTEC ; en standard index_id = NTARF, index_value = EASF[NTARF].
papp (←SINSTS, net signé en standard) + iinst (←IRMS1) restent stockés pour la
courbe + la jauge ; base/hchc/hchp en double-écriture (compat app pas-à-jour).
PREF (kVA) ≠ ISOUSC (A) → pas mappé dans la jauge (chantier ISOUSC standard).

pdl_index : 0  (source câblée — toujours index 0 dans sources.json)
"""

import json
import logging
import os
import signal
import sys
import time
import traceback
from threading import Thread
from time import sleep

import RPi.GPIO as GPIO

from tic_parite import octet_valide, ALPHABET_HISTO, ALPHABET_STD  # noqa: E402
import serial

# Module store partagé (src/pi/store/db.py)
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "store"))
import bus  # noqa: E402
import db  # noqa: E402
import settings  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
UART_DEV          = "/dev/ttyAMA0"
UART_BAUD_HISTO   = 1200    # mode historique
UART_BAUD_STD     = 9600    # mode standard
TIC_TIMEOUT_S     = 12      # max pour lire une trame complète (~4s à 1200 baud)

# Auto-détection du mode au boot : on sonde un débit pendant DETECT_WINDOW_S et
# on compte les groupes au checksum valide. Au mauvais débit on lit du bruit →
# ~0 groupe valide (un faux positif checksum est à ~1/64 par ligne) ; le seuil
# rend la confusion négligeable. La fenêtre couvre plusieurs trames aux deux
# débits (~1 s/trame en standard, ~1,7 s en historique).
DETECT_WINDOW_S    = 6
DETECT_MIN_GROUPS  = 5

# ⚠️ VARIABLES DE RUNTIME, plus des constantes. Assignées UNIQUEMENT dans la boucle
# principale, qui est au niveau module → pas de `global` nécessaire aujourd'hui.
# Si on déplace un jour ce bloc dans une fonction, il FAUDRA y déclarer
# `global PDL_INDEX, _pdl_source_adco` : sans ça Python en fait des locales, la valeur
# du module ne bouge jamais et le boîtier écrit tout sous l'index 0 — en silence.
# Le cache est assumé : la boucle tourne à ~1 trame/s sur un Pi Zero mono-cœur, on ne
# veut pas d'une lecture SQLite par trame. Le chemin LoRa, lui, n'a pas de global :
# il résout par trame et passe le PDL en paramètre.
# 🚨 `None`, et surtout PAS `0` : `0` est l'index du PREMIER compteur de tout boîtier
#    (cf. docs/chantier-pdl-adco.md), donc une amorce à 0 est INDISCERNABLE d'une vraie
#    réponse. Tant qu'aucun ADCO conforme n'a été lu, on ne SAIT pas, et une donnée
#    écrite sous un index deviné est fausse en silence — c'est ce qui arrivait à un
#    boîtier déplacé sur un autre compteur puis redémarré : ses mesures repartaient
#    sous l'index de son ANCIEN compteur jusqu'à la première trame lisible.
# ⭐ Même idiome que la voie LoRa, où `get_pdl_index()` rend déjà `int | None`.
PDL_INDEX         = None    # résolu depuis l'ADCO dès la 1re trame ; None = PAS ENCORE SU
_pdl_source_adco  = ""      # ADCO ayant servi à résoudre PDL_INDEX ci-dessus

# Lecture au fil de l'eau (volet A) : plus de PERIOD_S — on suit la cadence des
# trames du compteur (~1,7 s historique, ~1 s standard).
WATCHDOG_THRESHOLD = 600    # 10 min sans trame valide → relance process

# Écritures BDD batchées (volet B) : un commit par LOT, pas par trame.
BATCH_MAX_AGE_S    = 15     # flush au plus tard toutes les 15 s (= cadence d'hier)
BATCH_MAX_SIZE     = 60     # plafond de sécurité (anti-emballement RAM)
HEARTBEAT_S        = 20     # cadence max du flash LED vert « vivant » (≠ par trame)

STATE_PATH         = "/var/lib/ben-firmware/tic-state.json"

# ---------------------------------------------------------------------------
# LED RGB (cathode commune sur PCB rev01)
# R=GPIO12 (HW PWM0), G=GPIO13 (HW PWM1, boot indicator via gpio=13=op,dh),
# B=GPIO16. Piloté en PWM software (~500 Hz) pour pouvoir varier l'intensité.
# ---------------------------------------------------------------------------
RGB_R = 12
RGB_G = 13
RGB_B = 16

_pwm_r = _pwm_g = _pwm_b = None

def setup_led() -> None:
    """Init pins LED + PWM (~500 Hz) + 3 flashs bleus de boot (signe « démarré »)."""
    global _pwm_r, _pwm_g, _pwm_b
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in (RGB_R, RGB_G, RGB_B):
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, GPIO.LOW)
    _pwm_r = GPIO.PWM(RGB_R, 500); _pwm_r.start(0)
    _pwm_g = GPIO.PWM(RGB_G, 500); _pwm_g.start(0)
    _pwm_b = GPIO.PWM(RGB_B, 500); _pwm_b.start(0)
    # Blink de boot : 3 flashs bleus brefs (idem récepteur LoRa, modèles alignés).
    for _ in range(3):
        _pwm_b.ChangeDutyCycle(20); time.sleep(0.12)
        _pwm_b.ChangeDutyCycle(0);  time.sleep(0.12)

def blink_rgb(r: int, g: int, b: int, duration: float = 0.05,
              bypass: bool = False) -> None:
    """Pulse RGB en PWM. r/g/b = duty cycle 0..100.

    La luminosité réglée par l'utilisateur (led_level) est appliquée ; l'appelant
    passe bypass=True pour les états critiques (erreur) → visibles même LED
    baissée/éteinte."""
    try:
        f = settings.led_factor(bypass)
        _pwm_r.ChangeDutyCycle(max(0, min(100, round(r * f))))
        _pwm_g.ChangeDutyCycle(max(0, min(100, round(g * f))))
        _pwm_b.ChangeDutyCycle(max(0, min(100, round(b * f))))
        sleep(duration)
        _pwm_r.ChangeDutyCycle(0)
        _pwm_g.ChangeDutyCycle(0)
        _pwm_b.ChangeDutyCycle(0)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Protocole TIC (commun aux deux modes — cf. Enedis-NOI-CPT_54E §5.3.6)
# ---------------------------------------------------------------------------
STX = 0x02
ETX = 0x03
LF  = 0x0A
CR  = 0x0D
HT  = 0x09  # séparateur de champ en mode STANDARD (SP 0x20 en historique)

# Miroir de selectActiveIndex() dans tic-reader.ino
# (prefix, longueur_comparaison, index_id, index_name)
PTEC_MAP = [
    ("TH",   2, 0x00, "BASE"),
    ("HC..", 4, 0x01, "HCHC"),
    ("HP..", 4, 0x02, "HCHP"),
    ("HN",   2, 0x03, "EJPHN"),
    ("PM",   2, 0x04, "EJPHPM"),
    ("HCJB", 4, 0x05, "BBRHCJB"),
    ("HPJB", 4, 0x06, "BBRHPJB"),
    ("HCJW", 4, 0x07, "BBRHCJW"),
    ("HPJW", 4, 0x08, "BBRHPJW"),
    ("HCJR", 4, 0x09, "BBRHCJR"),
    ("HPJR", 4, 0x0A, "BBRHPJR"),
]

INDEX_LABELS = {
    "BASE", "HCHC", "HCHP", "EJPHN", "EJPHPM",
    "BBRHCJB", "BBRHPJB", "BBRHCJW", "BBRHPJW", "BBRHCJR", "BBRHPJR",
}

DEMAIN_NAMES = {"BLEU": "BLEU", "BLAN": "BLAN", "ROUG": "ROUG"}

# Étiquettes du mode STANDARD (Enedis-NOI-CPT_54E §6.2.2). On ne garde que
# celles utiles à BEN ; on mappe vers les mêmes clés génériques que l'historique
# (PAPP/IINST/ADCO) pour que le stockage et l'API existants marchent tels quels.
STD_EASF_LABELS = {f"EASF{i:02d}" for i in range(1, 11)}  # EASF01..EASF10

# ---------------------------------------------------------------------------
# TIC — lecture trame
# ---------------------------------------------------------------------------
def tic_checksum_ok(line: str) -> bool:
    """Vérifie le checksum TIC. Format : 'LABEL VALEUR <checksum>'."""
    if len(line) < 3 or line[-2] != ' ':
        return False
    total = sum(ord(c) for c in line[:-2])
    return chr((total & 0x3F) + 0x20) == line[-1]


CONDAMNEE = "_trame_condamnee"   # posé par un décodeur, lu par read_frame

# 🚨 UNE TRAME CONDAMNÉE N'EST PAS UNE TRAME ABSENTE, et les confondre RECRÉE la panne
#    que tout ce fichier cherche à éviter.
#
#    `read_frame` rendait `None` pour deux choses opposées : « rien n'est arrivé »
#    (timeout, liaison morte) et « tout est arrivé, mais l'identité ne tient pas ». La
#    boucle traite le premier cas en laissant `frame_ok` à faux — donc
#    `last_success_time` n'avance plus, donc le watchdog relance le process au bout de
#    10 min, DONC TOUTES LES 10 MINUTES tant que le compteur émet le même ADCO. C'est
#    exactement le mode de panne de pi-0.9.12, par une autre porte que celle qu'on
#    avait bouchée : sortir `frame_ok` du garde de stockage ne couvrait QUE le cas
#    `PDL_INDEX is None`, pas celui d'une trame condamnée.
#
# ⭐ Une trame condamnée prouve au contraire que LA LIAISON EST VIVANTE : ses groupes
#    arrivent, leur parité et leur checksum passent. Le watchdog surveille le FIL, pas
#    notre capacité à nommer le compteur. Redémarrer ne répare pas un compteur qui émet
#    un ADCO difforme — ça ne fait que perdre les mesures des autres lecteurs.
TRAME_CONDAMNEE = object()      # ≠ None : reçue et lisible, mais sans identité valable


# ⓘ Même forme que `_signaler_parite` : un cumul, un message par période, et rien à
#   dire quand il n'y a rien à dire. Indispensable ici, puisqu'on ne redémarre plus :
#   sans ce relevé, un boîtier qui ne stocke plus rien garderait un journal calme.
_CONDAMNEES_PERIODE_S = 300
_condamnees_cumul = {"trames": 0, "debut": 0.0}


def _signaler_condamnees(condamnee: bool) -> None:
    """Accumule les trames condamnées, et n'en parle qu'une fois par période."""
    maintenant = time.monotonic()
    if _condamnees_cumul["debut"] == 0.0:
        _condamnees_cumul["debut"] = maintenant
    if condamnee:
        _condamnees_cumul["trames"] += 1

    ecoule = maintenant - _condamnees_cumul["debut"]
    if ecoule < _CONDAMNEES_PERIODE_S:
        return
    if _condamnees_cumul["trames"]:
        log.warning(
            f"TIC : {_condamnees_cumul['trames']} trame(s) CONDAMNÉE(S) en "
            f"{ecoule / 60:.0f} min — ADCO non conforme alors que parité ET checksum "
            f"passent : des caractères ont été perdus, et rien ne dit que les autres "
            f"groupes y ont échappé")
    _condamnees_cumul.update(trames=0, debut=maintenant)


def garder_adco(brut: str, out: dict) -> None:
    """Ne retient l'ADCO que s'il EN EST un — et CONDAMNE la trame sinon.

    🚨 ET C'EST BIEN LA TRAME ENTIÈRE, PAS LE SEUL GROUPE. Le raisonnement tient à ce
       qu'on N'ARRIVE JAMAIS ICI par une ligne abîmée ordinaire : un groupe ADCO tombé
       sur la parité ou sur le checksum est déjà jeté par `read_frame`, et la clé
       n'existe pas. Le seul cas qui atteint cette fonction est donc un groupe qui a
       passé LES DEUX CONTRÔLES sans avoir la forme d'un ADCO — autrement dit
       L'AMPUTATION DANS L'ANGLE MORT DU CHECKSUM (caractères retirés sommant à un
       multiple de 64 ; cf. le commentaire du rejet de groupe plus bas).

       Or cet angle mort est le MÊME pour tous les groupes de la trame. Des caractères
       perdus ici veulent dire que la ligne en a perdu PENDANT CETTE TRAME, et un index
       ou un PAPP raccourci a pu passer exactement de la même façon — en silence, et
       sans qu'aucune forme permette de s'en apercevoir, puisqu'un nombre raccourci
       reste un nombre. Garder le reste de la trame reviendrait à jeter le seul signal
       qu'on ait.

    ⭐ L'ADCO est le SEUL champ de la TIC dont la forme soit connue d'avance — douze
       chiffres. C'est donc le seul endroit du protocole où l'angle mort du checksum
       devienne OBSERVABLE. Il sert ici de témoin pour toute la trame.

    ⓘ Coût : une trame, soit ~1,7 s de courbe, et la suivante arrive derrière. Mesuré
      sur le parc, l'événement se compte en unités par jour pour ~50 000 trames.
    """
    adco = (brut or "").strip()
    if not adco:
        return
    if not db.adco_valide(adco):
        signaler_adco_refuse(adco)
        out[CONDAMNEE] = True
        return
    out["ADCO"] = adco


def _courant_de_phase(phase: str, brut: str, out: dict) -> None:
    """Agrège `IINST1/2/3` (historique) ou `IRMS1/2/3` (standard) dans `IINST`.

    🚨 CE QU'ON PUBLIE EST LE TOTAL DES TROIS PHASES, et ce n'est pas un détail
       d'affichage. `IINST` n'alimente PAS la jauge — la jauge compare `PAPP` à
       `maxVa`. Son seul usage calculatoire est un SECOURS POUR LA PUISSANCE
       (`db.estimer_papp`, et le même calcul en SQL) :

           si papp == 0 et iinst > 0  →  papp ≈ 230 × iinst

       En triphasé résidentiel français chaque phase est à 230 V par rapport au
       neutre, donc S_totale = 230×I1 + 230×I2 + 230×I3 = 230 × (I1+I2+I3). La
       SOMME rend donc ce secours exact. ⚠️ Le MAX, qui semble plus « physique »,
       le ferait sous-estimer jusqu'à un FACTEUR 3.

    ⭐ LE CRITÈRE EST LA SIMULTANÉITÉ, PAS LA NATURE DE LA GRANDEUR. Les trois
       courants viennent de la MÊME trame, donc du même instant : leur somme
       décrit cet instant. `IMAX1/2/3` sont trois extrêmes datés séparément —
       `Σ max ≠ max Σ` — et les additionner fabriquerait un pic qui n'a jamais eu
       lieu. C'est pourquoi `IMAX` reste hors périmètre, et pas par oubli.

    ⚠️ CE QU'ON PERD, ET IL FAUT LE SAVOIR : le déséquilibre entre phases, donc
       la question « suis-je près de faire disjoncter ? » — c'est la phase la plus
       chargée qui déclenche, pas le total. Rien dans le code n'y répond
       aujourd'hui, même en monophasé : on ne régresse pas, mais si ce besoin
       apparaît il faudra conserver les trois valeurs.

    ⓘ Un dict par phase plutôt qu'un `+=` : une trame corrompue peut répéter un
       groupe, et un cumul aveugle compterait deux fois la même phase.
    """
    try:
        valeur = int(brut)
    except (TypeError, ValueError):
        return
    phases = out.setdefault("_phases", {})
    phases[phase] = valeur
    out["IINST"] = sum(phases.values())
    # 🚨 TROIS PHASES SE DÉCLARENT PAR LA PRÉSENCE DE LA 2e OU DE LA 3e, jamais
    #    par celle de la 1re : un compteur MONOPHASÉ en mode standard émet
    #    `IRMS1` — c'est même tout le piège de ce chantier. Conclure « triphasé »
    #    sur `IRMS1` mettrait les 7 boîtiers monophasés du parc en ISOUSC×3.
    if phase in ("2", "3"):
        out["PHASES"] = 3
    # 🚨 UNE SOMME INCOMPLÈTE EST UNE DONNÉE FAUSSE, PAS UNE DONNÉE PARTIELLE.
    #    Si `IINST2` tombe au checksum, `I1 + I3` est un nombre plausible et faux —
    #    et il serait ÉCRIT, là où le monophasé perdant son `IINST` voyait sa trame
    #    REJETÉE. C'est exactement l'inversion que ce projet combat : *une donnée
    #    fausse là où on voulait une donnée perdue*. ⇒ dès qu'on sait qu'il y a
    #    trois phases, on les exige toutes les trois ; sinon on RETIRE la clé, ce
    #    qui fait tomber la trame dans la garde `IINST absent` déjà en place.
    # ⓘ On retire la clé au lieu de poser None : l'état obtenu est alors
    #    STRICTEMENT celui du monophasé sans IINST, donc aucune nouvelle forme à
    #    gérer en aval (`_inject_va`, `record_measurement`, la garde elle-même).
    if out.get("PHASES") == 3 and len(phases) < 3:
        out.pop("IINST", None)


def _parse_label(line: str, out: dict) -> None:
    """Extrait label et valeur d'une ligne TIC validée."""
    parts = line.split(' ')
    if len(parts) < 3:
        return
    name  = parts[0]
    value = parts[1]

    if name in INDEX_LABELS:
        try:
            out[name] = int(value)
        except ValueError:
            pass
    elif name == "PTEC":
        out["PTEC"] = value
    elif name == "OPTARIF":                     # option tarifaire = le CONTRAT (équiv. NGTF standard)
        out["OPTARIF"] = value.strip()
    elif name == "DEMAIN":
        out["DEMAIN"] = value
    elif name == "IINST":
        try:
            out["IINST"] = int(value)
        except ValueError:
            pass
    elif name in ("IINST1", "IINST2", "IINST3"):
        _courant_de_phase(name[-1], value, out)
    elif name == "PAPP":
        try:
            out["PAPP"] = int(value)
        except ValueError:
            pass
    elif name == "ISOUSC":
        # Intensité souscrite (abonnement, A) — statique. Chantier ISOUSC :
        # sert à l'étalonnage de la jauge (maxVa = ISOUSC×230).
        try:
            out["ISOUSC"] = int(value)
        except ValueError:
            pass
    elif name == "ADCO":
        garder_adco(value, out)
    elif name in ("ADPS", "PEJP"):
        out[name] = True


def tic_checksum_std_ok(line: str) -> bool:
    """Vérifie le checksum TIC en mode STANDARD (Enedis-NOI-CPT_54E §5.3.6).

    `line` = contenu entre LF et CR : "ETIQ <HT> [HORODATE <HT>] DONNEE <HT> CK".
    Le checksum couvre tous les caractères de l'étiquette jusqu'au HT séparateur
    AVANT le checksum, **HT de queue inclus** (≠ historique où le SP est exclu).
    """
    if len(line) < 3 or line[-2] != "\t":
        return False
    total = sum(ord(c) for c in line[:-1])  # tout sauf le checksum → HT de queue inclus
    return chr((total & 0x3F) + 0x20) == line[-1]


def _std_int(value: str) -> int | None:
    """Convertit une donnée standard (zéros d'en-tête possibles) en int, ou None."""
    try:
        return int(value)
    except ValueError:
        return None


def _parse_label_std(line: str, out: dict) -> None:
    """Extrait label/donnée d'une ligne TIC STANDARD validée et la mappe vers les
    clés génériques (PAPP/IINST/ADCO…) + garde les champs standard spécifiques.

    Groupe à 7 ou 9 parties (Enedis §5.3.6) : après split sur HT on a
    [étiquette, donnée, checksum] (3) ou [étiquette, horodate, donnée, checksum]
    (4). La donnée est donc toujours l'avant-dernier champ ; l'horodate (si
    présente) le 2e. On déduit l'horodatage du nombre de champs — rien à coder en
    dur sur quelles étiquettes sont horodatées.
    """
    parts = line.split("\t")
    if len(parts) < 3:
        return
    name = parts[0]
    data = parts[-2]            # checksum = parts[-1] (déjà validé), donnée = parts[-2]

    if name == "ADSC":                       # adresse compteur ≈ ADCO historique
        garder_adco(data, out)
    elif name == "SINSTS":                    # puiss. app. instantanée soutirée (VA) ≈ PAPP
        v = _std_int(data)
        if v is not None:
            out["PAPP"] = v
    elif name in ("IRMS1", "IRMS2", "IRMS3"):  # courant efficace par phase (A) ≈ IINST
        _courant_de_phase(name[-1], data, out)
    # --- Champs standard spécifiques : parsés + loggés, stockage différé ------
    # (chantier index bi-mode : docs/chantier-index-energie-bimode.md)
    elif name == "EAST":                      # énergie active soutirée totale (Wh)
        out["EAST"] = _std_int(data)
    elif name in STD_EASF_LABELS:             # index fournisseur EASF01..10 (Wh)
        out.setdefault("EASF", {})[name] = _std_int(data)
    elif name == "NTARF":                     # n° index tarifaire en cours (1..10)
        out["NTARF"] = _std_int(data)
    elif name == "SINSTI":                    # puiss. app. instantanée injectée (VA)
        out["SINSTI"] = _std_int(data)
    elif name == "EAIT":                      # énergie active injectée totale (Wh)
        out["EAIT"] = _std_int(data)
    elif name == "PREF":                      # puiss. app. de référence (kVA) ≈ ISOUSC (autre unité)
        out["PREF"] = _std_int(data)
    elif name == "LTARF":                     # libellé tarif fournisseur en cours
        out["LTARF"] = data.strip()
    elif name == "NGTF":                      # nom du calendrier tarifaire fournisseur
        out["NGTF"] = data.strip()
    elif name == "VTIC":                      # version de la TIC (« 02 »)
        out["VTIC"] = data.strip()
    elif name == "DATE":                      # horodatée, donnée vide → horodate = 2e champ
        if len(parts) >= 4:                   # "SAAMMJJhhmmss" (saison + 12 chiffres)
            out["DATE_HORODATE"] = parts[1]
    elif name == "NJOURF":                    # n° profil jour courant (Tempo std, 0-9) — collecté, pas stocké
        out["NJOURF"] = _std_int(data)
    elif name == "NJOURF+1":                  # n° profil lendemain (Tempo std) — collecté, pas stocké
        out["NJOURF+1"] = _std_int(data)
    elif name == "STGE":                      # registre de statuts (std) — collecté, pas stocké
        # ⚠️ BASE 16 : STGE est le SEUL champ hexadécimal de la TIC standard, tous les autres
        # entiers sont en décimal. Un _std_int() par réflexe lirait « 013A4401 » comme 13.
        try:
            out["STGE"] = int(data.strip(), 16)
        except ValueError:
            pass                              # trame abîmée : on ignore, la suivante arrive en ~1 s


def _std_horodate_to_epoch(h: str | None) -> int | None:
    """Horodate compteur standard 'SAAMMJJhhmmss' → epoch UTC, ou None si absente/dégradée.
    Le compteur DIT la saison (E=été UTC+2 / H=hiver UTC+1) → conversion locale→UTC sans
    base de fuseaux. Saison minuscule (e/h) = horloge dégradée → None (pas fiable)."""
    if not h or len(h) < 13 or h[0] not in ("E", "H"):
        return None
    try:
        yy, mo, da = int(h[1:3]), int(h[3:5]), int(h[5:7])
        hh, mi, se = int(h[7:9]), int(h[9:11]), int(h[11:13])
        offset = 2 if h[0] == "E" else 1
        import calendar
        return calendar.timegm((2000 + yy, mo, da, hh, mi, se, 0, 0, 0)) - offset * 3600
    except (ValueError, OverflowError):
        return None


# ─── Bruit de parité : mesurer sans noyer la carte SD ───────────────────────
#
# 🚨 Une version antérieure émettait un WARNING PAR TRAME dès qu'un caractère
#    était rejeté. Sur une liaison bruyante, à ~1,5 s par trame, ça fait
#    ~57 000 lignes par jour dans journald — SUR LA CARTE SD. Le reste de ce
#    fichier évite précisément ça (cf. `log_uncabled`, qui ne parle qu'aux
#    changements d'état).
#
# ⭐ On accumule et on résume. Le signal reste — une liaison qui se dégrade se
#    voit toujours — mais il tient en une ligne toutes les cinq minutes au lieu
#    de deux cents.
#
# 🔬 CE COMPTEUR EST AUSSI L'INSTRUMENT D'UNE MESURE QUI RESTE À FAIRE.
#
#    Question ouverte : que faire d'un ETX corrompu ? Le fermer ici coûte la
#    fin de la trame courante ; fondre avec la suivante coûte UNE TRAME DE PLUS
#    (on attend l'ETX d'après). Lequel est préférable dépend entièrement du
#    TAUX D'ERREUR réel sur la ligne, et personne ne le connaît.
#
#    ⇒ Laisser tourner cette branche sur un boîtier plusieurs jours et lire ces
#      résumés. Si le taux est ~0, la question ne se pose pas. S'il ne l'est
#      pas, le chiffre tranche — et il tranchera mieux qu'un raisonnement.
_PARITE_PERIODE_S = 300
_parite_cumul = {"car": 0, "alpha": 0, "trames": 0, "debut": 0.0}


def _signaler_octets_rejetes(n_parite: int, n_alphabet: int = 0) -> None:
    """Accumule les octets rejetés et n'en parle qu'une fois par période.

    ⭐ UN SEUL message pour les deux causes, mais qui les NOMME séparément. Deux
       canaux périodiques indépendants diraient deux fois « la liaison est
       bruyante » sans jamais dire laquelle des deux détections a mordu ; un
       message qui fond les causes ne permet pas de distinguer un fil qui prend
       du bruit d'une corruption à deux bits que la parité ne voit pas.
    """
    maintenant = time.monotonic()
    if _parite_cumul["debut"] == 0.0:
        _parite_cumul["debut"] = maintenant

    _parite_cumul["car"] += n_parite
    _parite_cumul["alpha"] += n_alphabet
    if n_parite or n_alphabet:
        _parite_cumul["trames"] += 1

    ecoule = maintenant - _parite_cumul["debut"]
    if ecoule < _PARITE_PERIODE_S:
        return

    # ⓘ Rien à dire quand rien n'est rejeté : le silence EST l'information.
    if _parite_cumul["car"] or _parite_cumul["alpha"]:
        causes = []
        if _parite_cumul["car"]:
            causes.append(f"{_parite_cumul['car']} sur parité")
        if _parite_cumul["alpha"]:
            causes.append(f"{_parite_cumul['alpha']} hors alphabet TIC")
        log.warning(
            f"TIC : octet(s) rejeté(s) — {', '.join(causes)} — "
            f"dans {_parite_cumul['trames']} trame(s) en {ecoule / 60:.0f} min "
            f"— liaison bruyante ?")
    _parite_cumul.update(car=0, alpha=0, trames=0, debut=maintenant)


# ─── Un pdl_index ne se devine pas ──────────────────────────────────────────
#
# 🚨 `0` EST UN pdl_index VALIDE — celui du premier compteur de tout boîtier. Une
#    valeur d'amorce égale à 0 est donc INDISCERNABLE d'une vraie réponse, et rien
#    dans le code ne peut les séparer. D'où la sentinelle `None` (cf. PDL_INDEX),
#    et d'où ce prédicat : `peut_stocker` est le SEUL juge de « ai-je le droit
#    d'écrire cette ligne ».
#
# ⭐ Il prend ses deux arguments au lieu de lire les globales, et ce n'est pas du
#    zèle : c'est ce qui le rend éprouvable sur une machine sans UART — la même
#    leçon que le bloc `if __name__` plus bas, sans lequel `read_frame` est resté
#    intestable pendant toute sa vie.
def peut_stocker(conn, pdl_index) -> bool:
    """Écriture possible : base ouverte ET pdl_index RÉSOLU (jamais deviné)."""
    return conn is not None and pdl_index is not None


# ⓘ Le refus d'un ADCO est une ASSERTION D'INVARIANT, pas un tamis : en amont il y a
#   déjà la parité, le checksum TIC et — côté LoRa — le MAC ChaCha20. En arriver là
#   veut dire qu'autre chose est cassé. Donc ERROR, et la valeur en %r : sans elle on
#   ne saura jamais ce qui a frappé à la porte.
#
# 🚨 Et il FAUT la garde anti-répétition : un ADCO refusé ne met pas à jour
#    `_pdl_source_adco`, donc la résolution est retentée À CHAQUE TRAME (~1/s). Sans
#    garde, journald est noyé par la même ligne — et un journal noyé ne se lit plus.
_dernier_adco_refuse: str | None = None


def signaler_adco_refuse(adco: str) -> None:
    """Crie UNE FOIS par valeur refusée distincte, jamais une fois par trame."""
    global _dernier_adco_refuse
    if adco == _dernier_adco_refuse:
        return
    _dernier_adco_refuse = adco
    log.error("ADCO non conforme refusé : %r — aucun PDL créé, aucune écriture", adco)


# ⓘ Un boîtier qui décode bien mais ne stocke rien montre un heartbeat VERT et un
#   journal CALME : indiscernable d'un boîtier sain. Même forme que `_signaler_parite`
#   — un cumul, un message par période, et rien à dire quand il n'y a rien à dire.
_NON_STOCKE_PERIODE_S = 300
_non_stocke_cumul = {"trames": 0, "debut": 0.0}


def _signaler_non_stocke(saute: bool) -> None:
    """Accumule les trames décodées mais NON stockées faute de pdl_index résolu."""
    maintenant = time.monotonic()
    if _non_stocke_cumul["debut"] == 0.0:
        _non_stocke_cumul["debut"] = maintenant
    if saute:
        _non_stocke_cumul["trames"] += 1

    ecoule = maintenant - _non_stocke_cumul["debut"]
    if ecoule < _NON_STOCKE_PERIODE_S:
        return
    if _non_stocke_cumul["trames"]:
        log.warning(
            f"TIC : {_non_stocke_cumul['trames']} trame(s) décodée(s) mais NON "
            f"stockée(s) en {ecoule / 60:.0f} min — pdl_index non résolu "
            f"(ADCO illisible ?)")
    _non_stocke_cumul.update(trames=0, debut=maintenant)


# ─── Ce que la DERNIÈRE trame a coûté ───────────────────────────────────────
#
# 🚨 POURQUOI CE RELEVÉ EXISTE. Les messages « étiquette absente » disaient
#    « (checksum KO?) » — ils DEVINAIENT la cause. Depuis qu'on rejette aussi sur
#    parité, il y en a trois : checksum réellement invalide, caractère écarté sur
#    parité, ou étiquette qui n'existe pas dans ce mode.
#
# ⚠️ Ce projet s'est déjà brûlé exactement là : sur un compteur TRIPHASÉ, le
#    message accusait le checksum alors qu'`IINST` n'existe tout simplement pas
#    (c'est `IINST1/2/3`). Un après-midi à l'oscilloscope, pour un défaut qui
#    n'était pas analogique. *Un garde-fou qui se trompe de coupable coûte plus
#    cher que pas de garde-fou.*
#
# ⭐ On ne devine donc plus : on rapporte ce qu'on a compté.
#
# ⓘ État de module plutôt qu'une valeur de retour : `read_frame` a un seul
#    appelant, qui lit ce relevé dans la foulée. Le couplage est local et visible.
# 🚨 DEUX COMPTEURS DE PARITÉ, ET C'EST DÉLIBÉRÉ.
#
#    `parite` compte TOUT : la synchronisation sur STX, l'inter-ligne, les
#    lignes. C'est la mesure du BRUIT de la liaison, et elle doit tout voir.
#
#    `parite_groupes` ne compte que les octets tombés À L'INTÉRIEUR d'une ligne,
#    donc les seuls qui ont réellement fait rejeter quelque chose. C'est la
#    mesure de l'IMPUTATION.
#
# ⚠️ Les confondre remet exactement le défaut que ce relevé existe pour tuer.
#    Les octets refusés pendant l'attente du STX sont la QUEUE DE LA TRAME
#    PRÉCÉDENTE, et ceux entre un CR et le LF suivant ne sont dans aucune ligne :
#    ni les uns ni les autres n'ont coûté une étiquette à CETTE trame. Compter
#    l'ensemble fait dire « rejeté sur parité » à une trame dont AUCUNE ligne
#    n'a été rejetée — et c'est précisément le mensonge du triphasé : `IINST`
#    n'est jamais émis, un octet bruité traîne avant le STX, et le message
#    accuse la parité au lieu de dire que le compteur n'émet pas cette étiquette.
# 🚨 ET UNE TROISIÈME CAUSE, QUI NE SE FOND PAS DANS LES DEUX AUTRES.
#
#    `alphabet` compte les octets tombés parce qu'ils ne sont pas dans
#    `0x20–0x7E` (+ `HT` en standard). Par construction ils sont TOUS à
#    l'intérieur d'un groupe : hors groupe, un octet n'est pas accumulé, donc
#    jamais examiné. Il n'y a donc pas de pendant « tout/groupes » comme pour la
#    parité, et c'est normal.
#
#    `groupes_alphabet` compte les GROUPES que cette cause a condamnés.
#
# ⚠️ Fondre les trois dirait « ça décroche » sans dire OÙ — et le fil, le compteur
#    et le montage ne se diagnostiquent pas pareil. Un octet hors parité accuse le
#    BRUIT ; un octet hors alphabet accuse une corruption que la parité n'a pas
#    vue (deux bits dans le même octet) ; un checksum faux sur un groupe
#    intégralement lu accuse autre chose encore.
_derniere_trame = {"gardees": 0, "rejetees": 0, "parite": 0,
                   "parite_groupes": 0, "alphabet": 0, "groupes_alphabet": 0,
                   "etiquettes_rejetees": set()}


def _cause_rejets(etiquette: str = "") -> str:
    """Dit POURQUOI une étiquette manque, sans jamais accuser au hasard.

    `etiquette` : celle que l'appelant cherchait. Les cinq appelants la
    connaissent, donc on peut répondre sur ELLE plutôt que sur la trame entière.
    """
    r  = _derniere_trame["rejetees"]
    pa = _derniere_trame["parite_groupes"]
    al = _derniere_trame["alphabet"]
    vues = _derniere_trame["etiquettes_rejetees"]

    if not r:
        # 🚨 AUCUNE ligne rejetée, et l'étiquette manque : ce n'est donc NI le
        #    checksum NI la parité. C'est le compteur qui ne l'émet pas — mode,
        #    triphasé, trame courte. C'est CE cas que l'ancien message masquait.
        return " — aucun groupe rejeté : cette étiquette n'est pas émise ?"

    if etiquette and etiquette not in vues:
        # Des lignes SONT tombées, mais aucune ne portait celle-ci.
        #
        # 🚨 ET LA CONFIANCE À ACCORDER À CE CONSTAT DÉPEND DE LA CAUSE DU REJET.
        #
        #    Une ligne refusée au CHECKSUM est intégralement lue : tous ses
        #    caractères sont là, donc son étiquette est relevée juste. Dire
        #    « aucune ne portait celle-ci » est alors un indice solide.
        #
        #    Une ligne condamnée par la PARITÉ a perdu des caractères — et s'ils
        #    étaient dans le NOM, l'étiquette relevée est fausse. Conclure
        #    « pas émise » serait alors se tromper dans l'autre sens : elle
        #    était émise, et abîmée. On dit donc ce qu'on sait, et on nomme le
        #    doute au lieu de le taire.
        # Un octet hors ALPHABET fait perdre des caractères exactement comme un
        # octet hors parité : le nom relevé peut donc être faux pour la même
        # raison, et le doute se nomme de la même façon.
        if pa or al:
            return (f" — {r} groupe(s) rejeté(s), dont {_detail_octets(pa, al)} ; "
                    f"aucun ne portait {etiquette}, mais la perte de caractères "
                    f"a pu en abîmer le nom")
        return (f" — {r} groupe(s) rejeté(s) sur checksum, aucun ne portait "
                f"{etiquette} : cette étiquette n'est probablement pas émise")

    if pa or al:
        return f" — {r} groupe(s) rejeté(s), dont {_detail_octets(pa, al)}"
    return f" — {r} groupe(s) rejeté(s) sur checksum"


def _detail_octets(pa: int, al: int) -> str:
    """Nomme les causes présentes, et seulement celles-là."""
    bouts = []
    if pa:
        bouts.append(f"{pa} octet(s) hors parité")
    if al:
        bouts.append(f"{al} octet(s) hors alphabet TIC")
    return " et ".join(bouts)


def _note_groupe_rejete(vues: set, brut: bytes | bytearray | str) -> None:
    """Relève l'étiquette d'une ligne rejetée, pour pouvoir l'imputer.

    ⚠️ `vues` est LOCAL à la trame en cours, jamais l'ensemble du relevé. Une
       première version écrivait directement dans `_derniere_trame` : les
       étiquettes s'accumulaient d'une trame à l'autre, et au bout de quelques
       minutes toute étiquette avait « déjà été rejetée une fois » — le test
       « aucune ne portait celle-ci » ne mordait plus jamais, en silence.
    """
    if isinstance(brut, (bytes, bytearray)):
        brut = brut.decode("ascii", errors="replace")
    # `split()` sans argument coupe sur TOUTE espace : l'espace de l'historique
    # comme la tabulation du standard. Une seule ligne pour les deux modes.
    morceaux = brut.split()
    if morceaux:
        vues.add(morceaux[0])


def _rendre(labels: dict):    # dict | TRAME_CONDAMNEE | None
    """Ce que `read_frame` rend : la trame, ou rien si un décodeur l'a CONDAMNÉE.

    ⭐ `read_frame` reste MODE-AGNOSTIQUE : elle ne connaît aucune étiquette, seulement
       la convention « un décodeur peut condamner sa trame ». C'est `garder_adco` qui
       sait pourquoi, et c'est le bon partage — historique et standard nomment le même
       champ ADCO et ADSC.
    """
    if not labels:
        return None
    if labels.pop(CONDAMNEE, False):
        # Pas de journal ICI : à ~1 trame/s un message par trame noie journald. Le
        # relevé périodique `_signaler_condamnees` s'en charge, comme pour la parité.
        return TRAME_CONDAMNEE
    return labels


def read_frame(ser: serial.Serial, checksum_ok, parse_label,
               alphabet=ALPHABET_HISTO):    # dict | TRAME_CONDAMNEE | None
    """
    Lit une trame TIC complète (STX..ETX), mode-agnostique.
    `checksum_ok(line)` valide la ligne ; `parse_label(line, labels)` la décode.
    Retourne un dict des labels parsés, ou None si timeout / trame vide.

    ⭐ `alphabet` suit le MÊME chemin que les deux autres : l'alphabet légal dépend du
       mode (`HT` n'est un séparateur qu'en standard), et cette fonction reste
       mode-agnostique — c'est `MODES` qui injecte la bonne table.

    ⓘ Une TABLE de 128 entrées, pas un prédicat : sur Pi Zero un appel de fonction
      Python coûte 3 µs à lui seul, soit autant que tout le reste du contrôle. Indexée
      par l'octet déjà masqué, cf. `tic_parite`.

    🚨 LE DÉFAUT VAUT L'HISTORIQUE, c'est-à-dire le plus STRICT, et c'est délibéré.
       Oublier d'injecter le prédicat standard rend le lecteur MUET (tous les groupes
       condamnés sur leurs `HT`) : panne bruyante, que le banc attrape. Le défaut
       inverse — accepter `HT` partout — rouvrirait le trou du modulo 64 EN SILENCE,
       puisque `'I'` et `HT` sont à exactement 64 l'un de l'autre.
    """
    deadline = time.time() + TIC_TIMEOUT_S
    # ⚠️ Remis à neuf ICI, pas aux sorties : la sortie sur délai en
    #    synchronisation n'en touchait aucune, et laissait donc `_cause_rejets`
    #    décrire la trame PRÉCÉDENTE. Inoffensif aujourd'hui — l'appelant
    #    n'interroge le relevé que sur une trame lue — mais c'est le genre de
    #    dépendance invisible qui se paie au premier appelant suivant.
    _derniere_trame.update(gardees=0, rejetees=0, parite=0, parite_groupes=0,
                           alphabet=0, groupes_alphabet=0,
                           etiquettes_rejetees=set())

    # Synchronisation sur STX.
    #
    # ⚠️ La parité est vérifiée ICI AUSSI. Une version antérieure n'appliquait
    #    que `& 0x7F` : un octet de parité fausse dont les 7 bits de poids
    #    faible valent 0x02 était alors pris pour un début de trame. Et les
    #    erreurs vues pendant cette phase n'étaient pas comptées, donc la mesure
    #    de bruit SOUS-ESTIMAIT la réalité — ce qui est le pire défaut possible
    #    pour un compteur dont le seul rôle est de mesurer.
    parite_ko = 0
    while time.time() < deadline:
        raw = ser.read(1)
        if not raw:
            continue
        if not octet_valide(raw[0]):
            parite_ko += 1
            continue
        if (raw[0] & 0x7F) == STX:
            break
    else:
        # ⚠️ Compter AVANT de renoncer. Sur une ligne très bruitée — le cas même
        #    que ce compteur existe pour mesurer — la synchronisation échoue à
        #    chaque tour, et sans cette ligne les rejets ne seraient JAMAIS
        #    comptés : le résumé n'apparaîtrait pas, et le silence se lirait
        #    « tout va bien ».
        _signaler_octets_rejetes(parite_ko)
        log.warning(f"TIC timeout en attente STX ({parite_ko} octet(s) hors parité)")
        return None

    labels: dict = {}
    parite_groupes = 0          # sous-ensemble de parite_ko : voir _derniere_trame
    alpha_ko = 0                # octets hors alphabet TIC — tous dans un groupe
    groupes_alphabet = 0        # groupes que cette seule cause a condamnés
    etiquettes_ko: set = set()
    current = bytearray()
    in_line = False
    # 🚨 UN SEUL OCTET FAUTIF CONDAMNE TOUTE LA LIGNE. Voir au CR pourquoi.
    groupe_douteux = False
    groupe_hors_alphabet = False
    kept = dropped = 0

    while time.time() < deadline:
        raw = ser.read(1)
        if not raw:
            continue
        if not octet_valide(raw[0]):
            parite_ko += 1

            # Un ETX corrompu ferme quand même la trame.
            #
            # Sans ça, la lecture continue DANS LA TRAME SUIVANTE et rend un
            # dict mêlant les deux.
            #
            # ⓘ CE N'EST PAS GRAVE, et il ne faut pas le présenter comme tel :
            #    les trames TIC sont répétitives et viennent du MÊME compteur
            #    sur la MÊME ligne. Fondre N et N+1 donne surtout les valeurs de
            #    N+1 écrasant celles de N — une trame lue déphasée, pas une
            #    corruption. Le coût réel est UNE TRAME PERDUE.
            #
            # ⭐ Mais fermer ici coûte trois lignes et rend le découpage
            #    prévisible, alors que fondre rend les compteurs (gardés,
            #    rejetées, parité) faux : ils porteraient sur deux trames en
            #    disant une. C'est la MESURE qu'on protège, pas la donnée.
            #
            # ⓘ CR et LF corrompus, eux, ne coûtent qu'une ligne : la suivante
            #    resynchronise. On les laisse tomber.
            if (raw[0] & 0x7F) == ETX:
                if in_line:            # un groupe commencé, jamais refermé
                    _note_groupe_rejete(etiquettes_ko, current)
                    dropped += 1
                    # ⚠️ L'IMPUTATION AUSSI, pas seulement le compte. Un groupe qui
                    #    portait un octet hors alphabet puis finit sans son CR était
                    #    compté `rejeté` mais PAS imputé à l'alphabet : la cause
                    #    disparaissait, et c'est exactement le mensonge que ce relevé
                    #    existe pour tuer. Vaut pour les TROIS sorties hors CR.
                    if groupe_hors_alphabet:
                        groupes_alphabet += 1
                log.debug(f"TIC : ETX corrompu (parité) — trame close ici plutôt "
                          f"que fondue avec la suivante ; {kept} groupe(s) gardé(s)")
                _derniere_trame.update(gardees=kept, rejetees=dropped, parite=parite_ko,
                                   parite_groupes=parite_groupes,
                                   alphabet=alpha_ko, groupes_alphabet=groupes_alphabet,
                                   etiquettes_rejetees=etiquettes_ko)
                _signaler_octets_rejetes(parite_ko, alpha_ko)
                return _rendre(labels)

            # 🚨 LE GROUPE EN COURS EST CONDAMNÉ, et ce n'est pas du zèle.
            #
            #    Jeter le caractère et garder le reste supposait que le checksum
            #    rattraperait l'amputation. FAUX : il vaut (somme & 0x3F) + 0x20,
            #    donc il ne voit la somme QUE MODULO 64. Si les caractères
            #    retirés somment à un multiple de 64, le groupe raccourci porte
            #    le MÊME checksum et passe.
            #
            #    Et ce n'est pas exotique :
            #        2 espaces   2 × 0x20 = 64   ← le SÉPARATEUR des champs
            #        4 zéros     4 × 0x30 = 192  ← dans un INDEX
            #        1 arobase   1 × 0x40 = 64
            #
            # 🚨 Conséquence mesurée sur le papier : « 001000000 » amputé de
            #    quatre zéros devient « 00100 » — un index FAUX, accepté, écrit
            #    en base. C'est le seul défaut de tout ce chantier qui
            #    fabriquerait une donnée erronée plutôt que d'en perdre une.
            #
            # ⚠️ La note de la PR affirmait « 1 bit → aucun cas ne passe les deux
            #    contrôles réunis ». C'était vrai de l'OCTET, faux du GROUPE —
            #    parce que le code ne rejetait pas le groupe.
            # 🚨 UN OCTET FAUTIF HORS GROUPE N'EST PAS ANODIN — et le croire
            #    était le dernier endroit où le relevé accusait le compteur.
            #
            #    Entre le CR d'un groupe et le LF du suivant, une trame TIC bien
            #    formée ne contient RIEN. Un octet fautif à cet endroit est donc
            #    presque sûrement le LF lui-même. Or sans LF, `in_line` reste
            #    faux : les octets du groupe qui suit sont jetés un par un, le CR
            #    ne trouve rien à évaluer, et le groupe DISPARAÎT SANS TRACE.
            #
            # ⇒ On ouvre le groupe nous-mêmes, condamné d'avance. Il sera rejeté
            #   au CR et COMPTÉ. Au pire on fabrique un groupe fantôme et le
            #   relevé dit « un groupe rejeté » là où il aurait dit « aucun » :
            #   c'est l'erreur la moins chère des deux, puisque l'autre envoie
            #   chercher un défaut chez le COMPTEUR au lieu de sur le FIL.
            #
            # ⚠️ Ne vaut QUE dans la trame. Les octets refusés pendant l'attente
            #    du STX sont la queue de la trame précédente et n'ouvrent rien —
            #    c'est ce qui laisse le cas du triphasé dire « pas émise ».
            if not in_line:
                current = bytearray()
                in_line = True
            groupe_douteux = True
            parite_groupes += 1      # celui-là, lui, a coûté un groupe
            continue
        b = raw[0] & 0x7F

        if b == ETX:
            if in_line:                # CR perdu sur le dernier groupe
                _note_groupe_rejete(etiquettes_ko, current)
                dropped += 1
                if groupe_hors_alphabet:      # cf. l'ETX corrompu : imputer, pas seulement compter
                    groupes_alphabet += 1
            # ⭐ `parite_ko` est COMPTÉ, pas seulement écarté : c'est ce qui
            #    transforme une protection muette en une mesure. Une liaison
            #    bruyante se verra, au lieu de se déduire de PDL fantômes.
            _derniere_trame.update(gardees=kept, rejetees=dropped, parite=parite_ko,
                                   parite_groupes=parite_groupes,
                                   alphabet=alpha_ko, groupes_alphabet=groupes_alphabet,
                                   etiquettes_rejetees=etiquettes_ko)
            _signaler_octets_rejetes(parite_ko, alpha_ko)
            log.debug(f"Trame TIC complète : {kept} groupe(s) gardé(s), "
                      f"{dropped} rejeté(s), {parite_ko} octet(s) hors parité, "
                      f"{alpha_ko} hors alphabet")
            return _rendre(labels)
        elif b == LF:
            # 🚨 `in_line` encore vrai à l'arrivée d'un LF veut dire une seule
            #    chose : le CR du groupe précédent ne nous est jamais parvenu.
            #    Sans ce comptage, ce groupe s'évaporait — `groupe_douteux` était
            #    remis à faux deux lignes plus bas, AVANT d'avoir servi à quoi que
            #    ce soit, et `rejetees` restait à zéro. Le relevé concluait alors
            #    « aucun groupe rejeté : cette étiquette n'est pas émise ».
            if in_line:
                _note_groupe_rejete(etiquettes_ko, current)
                dropped += 1
                if groupe_hors_alphabet:      # cf. l'ETX corrompu : imputer, pas seulement compter
                    groupes_alphabet += 1
            current = bytearray()
            in_line = True
            groupe_douteux = False
            groupe_hors_alphabet = False
        elif b == CR:
            if in_line and (groupe_douteux or groupe_hors_alphabet):
                # On ne CONSULTE même pas le checksum : il ne peut pas trancher,
                # puisqu'il est aveugle à ce qui manque une fois sur soixante-quatre.
                #
                # ⭐ Le journal NOMME la cause, et nomme les DEUX quand les deux ont
                #    mordu : un groupe peut porter un octet hors parité ET un octet
                #    hors alphabet, et « rejeté » sans cause renverrait chercher le
                #    défaut au hasard.
                causes = []
                if groupe_douteux:
                    causes.append("hors parité")
                if groupe_hors_alphabet:
                    causes.append("hors alphabet TIC")
                    groupes_alphabet += 1
                log.debug("Groupe rejeté : au moins un octet " + " et ".join(causes))
                _note_groupe_rejete(etiquettes_ko, current)
                dropped += 1
                in_line = False
                groupe_douteux = False
                groupe_hors_alphabet = False
                continue
            if in_line and current:
                line = current.decode("ascii", errors="replace")
                if checksum_ok(line):
                    parse_label(line, labels)
                    kept += 1
                else:
                    log.debug(f"Checksum invalide : <{line}>")
                    _note_groupe_rejete(etiquettes_ko, line)
                    dropped += 1
            in_line = False
        elif not alphabet[b]:
            # 🚨 UN OCTET HORS ALPHABET HORS GROUPE N'EST PAS ANODIN — et le croire était
            #    le dernier endroit où ce relevé accusait le COMPTEUR. Le test vivait dans
            #    `elif in_line:` : un tel octet ne tombait alors dans AUCUNE branche et
            #    disparaissait.
            #
            #    Entre le CR d'un groupe et le LF du suivant, une trame TIC bien formée ne
            #    contient RIEN. Un octet fautif à cet endroit est donc presque sûrement le
            #    LF lui-même — et deux bits suffisent : 0x0A devient 0x09, soit `HT`,
            #    illégal en historique et de parité PRÉSERVÉE. Sans LF, `in_line` reste
            #    faux, les octets du groupe suivant sont jetés un par un, le CR ne trouve
            #    rien à évaluer, et le groupe DISPARAÎT SANS TRACE — après quoi
            #    `_cause_rejets` conclut « cette étiquette n'est pas émise ».
            #
            # ⇒ On ouvre donc le groupe nous-mêmes, condamné d'avance, exactement comme le
            #   fait le chemin de la PARITÉ au-dessus et pour la même raison. Au pire on
            #   fabrique un groupe fantôme et le relevé dit « un groupe rejeté » là où il
            #   aurait dit « aucun » : c'est l'erreur la moins chère des deux, puisque
            #   l'autre envoie chercher un défaut chez le COMPTEUR au lieu de sur le FIL.
            #
            # ⚠️ Ne vaut QUE dans la trame : les octets vus pendant l'attente du STX
            #    n'ouvrent rien — c'est ce qui laisse le cas du triphasé dire « pas émise ».
            #
            # ⓘ Placée APRÈS les tests ETX/LF/CR, sans quoi elle les condamnerait : ce sont
            #   des caractères de contrôle, donc hors alphabet par construction.
            if not in_line:
                current = bytearray()
                in_line = True
            alpha_ko += 1
            groupe_hors_alphabet = True
        elif in_line:
            # 🚨 L'ALPHABET LÉGAL EST CONNU D'AVANCE (NOI-CPT_54E §6.2.1.2 : ASCII
            #    imprimables 0x20-0x7E, plus `HT` en standard). Un octet hors de cet
            #    ensemble est une erreur PAR DÉFINITION DE LA NORME — pas une heuristique.
            #
            # 🚨 ET LE DÉFAUT N'ÉTAIT PAS CE QU'ON CROIT. L'octet n'était pas jeté, il
            #    était AJOUTÉ : le groupe gardait sa longueur, un de ses caractères était
            #    simplement REMPLACÉ. Il n'y avait donc pas d'amputation ici — à la
            #    différence de la parité — mais une SUBSTITUTION, et c'est exactement là
            #    que le checksum est aveugle : il vaut (somme & 0x3F) + 0x20, donc modulo
            #    64. Remplacer un caractère par `c - 0x40` retire exactement 64, donc
            #    laisse le checksum IDENTIQUE, et donne un caractère de CONTRÔLE :
            #
            #        'T' = 0x54 -> 0x14      'S' = 0x53 -> 0x13      'I' = 0x49 -> HT
            #
            # 🚨 ET IL FAUT DEUX BITS, PAS UN — ne pas gonfler la menace. Un SEUL bit
            #    retourné sur le fil casse TOUJOURS la parité, puisque le compteur a
            #    calculé le bit de parité sur l'octet d'origine : `octet_valide` l'arrête
            #    déjà, et ce contrôle-ci ne le voit jamais. Ce qui l'atteint, c'est un
            #    nombre PAIR de bits retournés dans le même octet — par exemple le bit 6
            #    de la donnée ET le bit de parité : la parité redevient « juste », et
            #    « PTEC TH.. » arrive en « PTEC \x14H.. » avec un checksum intact.
            #
            # ⭐ C'est donc une COÏNCIDENCE RARE qu'on ferme, pas un événement courant —
            #    le même arbitrage que celui déjà écrit dans `tic_parite.octet_valide`.
            #    Mais en historique PTEC donne l'`index_id`, et le coût de la fermer est
            #    de deux comparaisons par octet : on la ferme.
            #
            #    On condamne donc le groupe sans CONSULTER le checksum, qui ne peut pas
            #    trancher. Et on ne se contente pas de sauter l'octet : la ligne serait
            #    alors AMPUTÉE, ce qui recrée le trou de la parité (des caractères retirés
            #    dont la somme est un multiple de 64 laissent le checksum intact).
            #
            # ⚠️ Compté À PART : fondre les causes dirait « ça décroche » sans dire OÙ.
            current.append(b)

    # ⚠️ Même raison qu'au timeout de synchronisation : c'est précisément quand
    #    tout échoue qu'il faut que le compteur parle.
    #
    # ⓘ UN GROUPE ENCORE OUVERT ICI N'EST PAS COMPTÉ, ni comme rejeté ni comme imputé —
    #   et c'est VOLONTAIREMENT identique au traitement de la parité, qui ne le compte pas
    #   davantage. Inoffensif aujourd'hui : cette sortie rend `None`, donc l'appelant n'a
    #   pas de trame et n'interroge jamais `_cause_rejets`. Si un appelant futur le faisait,
    #   les DEUX causes seraient à traiter ensemble, pas l'alphabet seul — une asymétrie
    #   entre les deux serait pire que l'oubli actuel.
    _derniere_trame.update(gardees=kept, rejetees=dropped, parite=parite_ko,
                                   parite_groupes=parite_groupes,
                                   alphabet=alpha_ko, groupes_alphabet=groupes_alphabet,
                                   etiquettes_rejetees=etiquettes_ko)
    _signaler_octets_rejetes(parite_ko, alpha_ko)
    log.warning(f"TIC timeout en lecture trame ({kept} groupe(s) gardé(s), {dropped} "
                f"rejeté(s), {parite_ko} octet(s) hors parité, {alpha_ko} hors alphabet)")
    return None

# ---------------------------------------------------------------------------
# TIC — décodage (miroir Arduino)
# ---------------------------------------------------------------------------
def select_active_index(ptec: str, labels: dict) -> tuple[int | None, str | None, int | None]:
    """Miroir de selectActiveIndex() tic-reader.ino — sélectionne l'index selon PTEC.

    Retourne (id, name, value) : id = rang PTEC_MAP (0x00..0x0A, pour index_id générique).
    (None, None, None) si PTEC inconnu ; (id, name, None) si l'étiquette index est absente
    du dict (checksum KO ou timeout).
    """
    p = ptec.strip()
    for prefix, n, _id, name in PTEC_MAP:
        if p[:n] == prefix:
            return _id, name, labels.get(name)  # value None si etiquette non vue
    return None, None, None


def build_flags(labels: dict) -> tuple[str | None, bool, bool]:
    """Miroir de buildFlags() tic-reader.ino — retourne (demain, adps, pejp)."""
    demain = DEMAIN_NAMES.get(labels.get("DEMAIN", ""))
    adps   = bool(labels.get("ADPS", False))
    pejp   = bool(labels.get("PEJP", False))
    return demain, adps, pejp


# Champs collectés mais NON câblés au stockage (DEMAIN/ADPS/PEJP histo, NJOURF/NJOURF+1 std) :
# logués À MINIMA, ON-CHANGE en INFO — MÊME format/manière que le récepteur LoRa (main.py
# log_uncabled). Visibilité sans noyer journald (~1,7 s/trame). MSG1/MSG2 = non émis (RAM ATmega328).
_last_uncabled: dict = {}

# Couleurs Tempo portées par STGE (mode standard). Offset VÉRIFIÉ sur trame réelle le
# 2026-08-14 — capture data/tic-ben0001-20260814-1343.bin, STGE=013A4401 → « jour = BLEU »,
# conforme au terrain, et TOUS les autres champs du registre tombent juste avec cette
# convention. Deux sources publiques se contredisaient d'un bit ; c'est la trame qui a tranché.
# Même table que frame_codec.stge_couleurs() côté LoRa — garder les deux alignées.
_STGE_COULEUR = {0: "néant", 1: "bleu", 2: "blanc", 3: "rouge"}


def _stge_lisible(v):
    """'0x013A4401 jour=bleu demain=néant' — ou None si le champ est absent."""
    if v is None:
        return None
    return "0x%08X jour=%s demain=%s" % (
        v, _STGE_COULEUR.get((v >> 24) & 3), _STGE_COULEUR.get((v >> 26) & 3))


def log_uncabled(fields: dict) -> None:
    """Logge en INFO chaque champ non-câblé quand sa valeur change (aligné LoRa)."""
    for name, val in fields.items():
        if val is not None and _last_uncabled.get(name) != val:
            log.info(f"non câblé : {name}={val!r} (collecté, pas stocké) pdl_index={PDL_INDEX}")
            _last_uncabled[name] = val

# ---------------------------------------------------------------------------
# Modes TIC + auto-détection
# ---------------------------------------------------------------------------
# Un descripteur par mode : débit + fonctions de validation/décodage.
MODES = {
    "historique": dict(baud=UART_BAUD_HISTO, checksum=tic_checksum_ok,     parse=_parse_label,
                       alphabet=ALPHABET_HISTO),
    "standard":   dict(baud=UART_BAUD_STD,   checksum=tic_checksum_std_ok, parse=_parse_label_std,
                       alphabet=ALPHABET_STD),   # + HT, séparateur légal
}


def open_serial(baud: int) -> serial.Serial:
    """
    Ouvre l'UART au débit donné, en 8N1.

    ⭐ 8N1 et NON 7E1, délibérément : `pyserial` n'active pas `INPCK` même avec
    `PARITY_EVEN` — mesuré sur ben-0003 —, donc la parité matérielle serait un
    coup d'épée dans l'eau. On lit les 8 bits et on vérifie le 7ᵉ nous-mêmes
    (`tic_parite.octet_valide`), ce qui marche sur tous les ports, mini-UART
    compris.
    """
    return serial.Serial(
        port=UART_DEV,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=1,
    )


def count_valid_groups(ser: serial.Serial, checksum_ok, window_s: float) -> int:
    """Compte les groupes (lignes LF..CR) au checksum valide pendant window_s.

    Au mauvais débit on lit du bruit → quasi aucun checksum ne passe. Sert de
    discriminant de mode sans parser ni décoder (juste valider l'intégrité)."""
    deadline = time.time() + window_s
    current = bytearray()
    in_line = False
    valid = 0
    while time.time() < deadline:
        raw = ser.read(1)
        if not raw:
            continue
        if not octet_valide(raw[0]):
            continue
        b = raw[0] & 0x7F
        if b == LF:
            current = bytearray()
            in_line = True
        elif b == CR:
            if in_line and current:
                if checksum_ok(current.decode("ascii", errors="replace")):
                    valid += 1
            in_line = False
        elif b in (STX, ETX):
            in_line = False
        elif in_line:
            current.append(b)
    return valid


def detect_mode(persisted: str | None) -> str | None:
    """Sonde les débits et renvoie le nom du mode détecté ('historique' /
    'standard'), ou None si aucun. Le mode persisté est sondé en premier (reboot
    rapide). Au mauvais débit → < DETECT_MIN_GROUPS groupes valides → on bascule.
    """
    order = ["historique", "standard"]
    if persisted in MODES:
        order = [persisted] + [m for m in order if m != persisted]

    for name in order:
        m = MODES[name]
        ser = open_serial(m["baud"])
        try:
            n = count_valid_groups(ser, m["checksum"], DETECT_WINDOW_S)
        finally:
            ser.close()
        log.info(f"détection mode : {name} ({m['baud']} baud) → {n} groupes valides "
                 f"(seuil {DETECT_MIN_GROUPS})")
        if n >= DETECT_MIN_GROUPS:
            return name
    return None

# ---------------------------------------------------------------------------
# État persistant
# ---------------------------------------------------------------------------
def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {}
    mode = raw.get("mode")
    return {
        "adco": raw.get("adco", ""),
        "mode": mode if mode in MODES else None,  # dernier mode auto-détecté
    }


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


state = load_state()

# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------
last_success_time = time.time()


def watchdog_loop() -> None:
    while True:
        sleep(30)
        elapsed = time.time() - last_success_time
        if elapsed > WATCHDOG_THRESHOLD:
            log.critical(f"WATCHDOG : {int(elapsed)}s sans succès — relance")
            sleep(1)
            os.execv(sys.executable, [sys.executable] + sys.argv)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # ⭐ TOUT CE QUI SUIT NE S'EXÉCUTE QUE SI ON LANCE CE FICHIER.
    #
    #    Avant, ces lignes étaient au niveau du module : ouvrir le fichier — donc
    #    l'importer — allumait la LED, sondait le port série et démarrait le
    #    watchdog. Un banc qui voulait juste éprouver `read_frame` démarrait en
    #    fait tout le lecteur, et restait bloqué sur une machine sans UART.
    #
    # 🚨 C'est ce qui rendait `read_frame` INTESTABLE, alors que c'est la boucle
    #    la plus délicate du fichier — trois modifications rien que le 26/09
    #    (synchronisation STX, ETX corrompu, relevé des compteurs), sans qu'aucun
    #    banc ne puisse rougir.
    #
    # ⓘ `if __name__` NE CRÉE PAS DE PORTÉE en Python : les variables assignées
    #    ici restent globales, donc `flush_batch()` et `watchdog_loop()`, qui les
    #    lisent, fonctionnent exactement comme avant. Le changement est purement
    #    mécanique — quatre espaces, aucune logique touchée.

    setup_led()
    log.info("LED RGB initialisée (boot indicator vert éteint)")

    # Auto-détection du mode (historique 1200 / standard 9600). Le mode persisté est
    # sondé en premier. Si rien n'est détecté (compteur muet au boot, NTP/TIC pas
    # encore là), on retombe sur le mode persisté ou, à défaut, historique (sortie
    # d'usine) — et le watchdog relancera la détection si aucune trame ne vient.
    detected = detect_mode(state.get("mode"))
    if detected is None:
        mode_name = state.get("mode") or "historique"
        log.warning(f"Aucun mode détecté au boot — repli sur '{mode_name}' "
                    f"(le watchdog re-sondera si rien ne vient)")
    else:
        mode_name = detected
        if mode_name != state.get("mode"):
            log.info(f"Mode TIC : {mode_name} (changement vs persisté={state.get('mode') or 'aucun'})")
            state["mode"] = mode_name
            save_state(state)

    mode = MODES[mode_name]
    checksum_ok = mode["checksum"]
    alphabet = mode["alphabet"]
    parse_label = mode["parse"]
    is_standard = mode_name == "standard"

    ser = open_serial(mode["baud"])
    # ⭐ Dire que le contrôle est ACTIF, pas seulement qu'on a ouvert le port.
    #
    #    Le résumé de parité ne parle que s'il y a des rejets : au bout de trois
    #    jours de silence, « aucune erreur » et « le code ne tourne pas » sont
    #    indiscernables. Cette ligne les sépare — elle est le témoin de la mesure.
    log.info(f"Série ouvert : {UART_DEV} {mode['baud']} 8N1 proto TIC {mode_name} "
             f"— contrôle de parité ACTIF (7E1 lu en 8N1), résumé toutes les "
             f"{_PARITE_PERIODE_S // 60} min s'il y a des rejets")
    # ⓘ `state["adco"]` est INFORMATIF : il n'amorce pas PDL_INDEX, et le dire évite de
    #   lire cette ligne comme « le boîtier sait où écrire ». Il ne le sait pas encore —
    #   la table `pdl` porte bien la correspondance ADCO → pdl_index, mais personne ne
    #   la joint au démarrage. Tant que rien n'est résolu, RIEN N'EST STOCKÉ.
    log.info(f"Dernier compteur vu : {state.get('adco') or '(aucun)'} "
             f"— pdl_index NON RÉSOLU, en attente de la 1re trame portant un ADCO")

    Thread(target=watchdog_loop, daemon=True, name="watchdog").start()
    log.info(f"Watchdog démarré (seuil={WATCHDOG_THRESHOLD}s)")

    # Store SQLite local (conso + outbox cloud). Non bloquant : si la base est
    # indisponible, le reader continue (LED/logs), juste sans stockage.
    try:
        measurements_db = db.connect()
        log.info(f"Store SQLite ouvert : {db.DB_PATH}")
    except Exception as e:
        measurements_db = None
        log.error(f"Store SQLite indisponible ({e}) — on continue sans stockage")
    last_prune = time.time()
    # Signal « stocké » sur le bus local (cf. store/bus.py) — None si paho absent.
    bus_client = bus.local_client("ben-tic-reader")

    # --- Batch d'écriture (volet B) ---------------------------------------------
    # La lecture au fil de l'eau densifie les trames (~7×). Pour ne pas faire un fsync
    # par trame, on accumule et on flush en UN commit (executemany) toutes les
    # BATCH_MAX_AGE_S (ou BATCH_MAX_SIZE). Granularité de perte = le batch (crash → au
    # pire les ~15 dernières s ; courbe append-only, non critique à la seconde).
    batch: list = []          # (pdl_index, labels, ts)
    last_flush = time.time()
    last_heartbeat = 0.0
    last_isousc: int | None = None  # garde RAM : record_isousc seulement sur changement
    # Raccordement triphasé (#50). Relu depuis la base dès que le PDL est connu —
    # voir le préchargement plus bas, qui n'est PAS une optimisation.
    last_phases: int | None = None
    last_src_std: int | None = None  # garde RAM : record_tic_mode seulement sur changement


    def note_tic_mode(mode_std: int) -> None:
        """Signale le mode observé. Garde RAM d'abord : la boucle tourne à ~1 trame/s, on ne
        veut pas d'une lecture SQLite par trame (même raison que le cache pdl_index)."""
        global last_src_std
        if mode_std == last_src_std or not peut_stocker(measurements_db, PDL_INDEX):
            return
        try:
            if db.record_tic_mode(measurements_db, PDL_INDEX, mode_std, db.device_id()):
                log.info(f"MODE TIC → {'standard' if mode_std else 'historique'} — événement émis")
            last_src_std = mode_std
        except Exception as e:
            log.warning(f"store: record_tic_mode échoué: {e}")


    last_pref: int | None = None    # garde RAM : record_pref (abonnement standard, kVA) sur changement
    last_ltarf: tuple | None = None # garde RAM : record_tariff_label (NTARF, LTARF) sur changement
    last_ngtf: str | None = None    # garde RAM : record_ngtf (calendrier fournisseur) sur changement


    def flush_batch() -> None:
        global batch, last_flush
        last_flush = time.time()
        if not batch or measurements_db is None:
            batch = []
            return
        try:
            n = db.record_measurements_batch(measurements_db, batch)
            log.debug(f"store: batch flush {n} mesures")
            bus.publish_stored(bus_client, PDL_INDEX, batch[-1][2])
        except Exception as e:
            log.warning(f"store: flush batch échoué ({len(batch)} pts perdus): {e}")
        finally:
            batch = []


    def _on_sigterm(signum, frame):
        # systemd stop → flush le batch courant avant de mourir (pas de perte évitable).
        log.info("SIGTERM — flush batch puis arrêt")
        flush_batch()
        raise SystemExit(0)


    signal.signal(signal.SIGTERM, _on_sigterm)

    # En mode standard, on logge la PREMIÈRE trame valide en INFO (tous les champs
    # parsés) pour valider le décodage sur un vrai compteur standard ; ensuite on
    # repasse en DEBUG (cadence ~1 s, ne pas noyer journald — cf. preshipping).
    std_first_logged = False

    try:
        while True:
            frame_ok = False
            try:
                # Au fil de l'eau : read_frame se cale sur la cadence du compteur.
                # PAS de reset_input_buffer (on lit le flux en continu) ni de sleep
                # (la trame suivante nous attend déjà dans le port).
                labels = read_frame(ser, checksum_ok, parse_label, alphabet)

                _signaler_condamnees(labels is TRAME_CONDAMNEE)

                if labels is TRAME_CONDAMNEE:
                    # 🚨 `frame_ok` VRAI : la liaison est vivante, seule l'identité
                    #    manque. Le laisser à faux ferait repartir le watchdog toutes
                    #    les 10 min (cf. TRAME_CONDAMNEE). Rien n'est stocké pour
                    #    autant : la trame n'a pas franchi ce point.
                    frame_ok = True
                elif labels is None:
                    log.warning("Trame TIC invalide ou timeout")
                else:
                    # ⓘ La clé n'existe que si l'ADCO EN ÉTAIT un : `garder_adco` l'a
                    #   tranché au décodage. Rien à valider ici — ni pour `state.json`
                    #   juste en dessous, ni pour la résolution du PDL plus bas.
                    adco = labels.get("ADCO", "")
                    if adco:
                        prev_adco = state.get("adco", "")
                        if adco != prev_adco:
                            log.info(f"NOUVEAU PDL détecté : ADCO={adco} (précédent={prev_adco or 'aucun'})")
                            state["adco"] = adco
                            save_state(state)
                        # Résolution ADCO → pdl_index. Plus simple qu'en LoRa : l'ADCO est
                        # dans CHAQUE trame, donc ni table `emitter`, ni attente d'une trame
                        # de boot, ni fenêtre de repli. On résout à la première trame de
                        # chaque exécution (pas seulement au changement) : sinon un boîtier
                        # déplacé puis redémarré retomberait sur l'index 0, celui de son
                        # ancien compteur. `graine=0` = convention de la source câblée.
                        if adco != _pdl_source_adco and measurements_db is not None:
                            try:
                                pdl = db.resolve_pdl(measurements_db, adco, graine=0)
                                if pdl is not None:
                                    if pdl != PDL_INDEX:
                                        log.info(f"pdl_index : {PDL_INDEX} → {pdl} (ADCO={adco})")
                                    PDL_INDEX = pdl
                                    _pdl_source_adco = adco
                                    # 🚨 LE RACCORDEMENT SE RELIT ICI, ET PAS AU
                                    #    DÉMARRAGE : `PDL_INDEX` vaut None tant
                                    #    qu'une trame n'a pas livré son ADCO, donc
                                    #    un préchargement en tête de `main()` ne
                                    #    rendrait jamais rien.
                                    # ⭐ Et ce n'est pas une optimisation : c'est la
                                    #    SEULE chose qui permette de rejeter une
                                    #    trame ayant perdu `IINST2` ET `IINST3` —
                                    #    elle ressemble alors trait pour trait à un
                                    #    monophasé, et le décodeur ne peut pas les
                                    #    distinguer. Il faut la mémoire du
                                    #    raccordement, et elle doit survivre à un
                                    #    redémarrage du lecteur.
                                    if last_phases is None:
                                        su = db.get_phases(measurements_db, pdl)
                                        last_phases = 3 if su == 3 else None
                            except Exception as e:
                                log.warning(f"store: resolve_pdl échoué: {e}")

                    # ISOUSC (abonnement) — écrit SUR CHANGEMENT seulement (garde RAM
                    # + record_isousc fait aussi sa garde DB). Indépendant de la
                    # validité PTEC/PAPP de la trame.
                    isousc = labels.get("ISOUSC")
                    if (isousc is not None and isousc != last_isousc
                            and peut_stocker(measurements_db, PDL_INDEX)):
                        if db.record_isousc(measurements_db, PDL_INDEX, isousc):
                            log.info(f"ISOUSC={isousc} A enregistré (maxVa≈{isousc * 230} VA)")
                        last_isousc = isousc

                    # PHASES (#50) — le raccordement triphasé, record-on-change.
                    # 🚨 DOIT ÊTRE ÉCRIT MÊME QUAND LA TRAME EST REJETÉE PLUS BAS,
                    #    comme ISOUSC : c'est une propriété du RACCORDEMENT, pas une
                    #    mesure. Et c'est ce qui corrige la jauge (ISOUSC est par
                    #    phase, PAPP est un total) — la différer derrière la validité
                    #    PTEC/PAPP la rendrait dépendante d'une trame saine, sur un
                    #    compteur dont le mode historique n'en produit justement pas.
                    phases = labels.get("PHASES")
                    if (phases is not None and phases != last_phases
                            and peut_stocker(measurements_db, PDL_INDEX)):
                        if db.record_phases(measurements_db, PDL_INDEX, phases):
                            log.info(f"Raccordement TRIPHASÉ détecté — maxVa ≈ "
                                     f"{(isousc or 0) * 230 * phases} VA "
                                     f"(ISOUSC est par phase)")
                        last_phases = phases

                    # PREF (abonnement en mode STANDARD, kVA) — record-on-change (chantier ISOUSC std).
                    # Le standard ne donne pas ISOUSC ; PREF×1000 calibre la jauge (arbitré par /live).
                    pref = labels.get("PREF")
                    if (pref is not None and pref != last_pref
                            and peut_stocker(measurements_db, PDL_INDEX)):
                        if db.record_pref(measurements_db, PDL_INDEX, pref):
                            log.info(f"PREF={pref} kVA enregistré (maxVa≈{pref * 1000} VA)")
                        last_pref = pref

                    # LTARF (libellé tarif standard AUTORITATIF) — cache NTARF→label, on-change.
                    # Le wired lit LTARF gratuitement dans la TIC standard (chantier unification labels).
                    ltarf = labels.get("LTARF")
                    ntarf_lbl = labels.get("NTARF")
                    # LTARF n'existe qu'en standard → le contrat associé = NGTF.
                    lt_key = (ntarf_lbl, ltarf, labels.get("NGTF"))
                    if (ltarf and ntarf_lbl and lt_key != last_ltarf
                            and peut_stocker(measurements_db, PDL_INDEX)):
                        if db.record_tariff_label(measurements_db, PDL_INDEX, 1, ntarf_lbl, ltarf,
                                                  labels.get("NGTF") or ""):
                            log.info(f"LTARF NTARF={ntarf_lbl} → {ltarf!r} enregistré")
                        last_ltarf = lt_key

                    # Contrat (calendrier tarifaire) — NGTF en standard, OPTARIF en historique.
                    # Mode-agnostique → level_profile.ngtf. On-change (changement fournisseur/offre).
                    contract = labels.get("NGTF") or labels.get("OPTARIF")
                    if contract and contract != last_ngtf and peut_stocker(measurements_db, PDL_INDEX):
                        if db.record_ngtf(measurements_db, PDL_INDEX, contract):
                            log.info(f"Contrat={contract!r} enregistré")
                        last_ngtf = contract

                    iinst  = labels.get("IINST")
                    papp   = labels.get("PAPP")

                    if not is_standard:
                        # --- Mode HISTORIQUE : index actif déduit de PTEC ----------
                        ptec = labels.get("PTEC")
                        active_id = active_name = active_value = None
                        if ptec:
                            active_id, active_name, active_value = select_active_index(ptec, labels)

                        if not ptec:
                            log.error("PTEC absent de la trame TIC")
                        elif active_name is None:
                            log.warning(f"PTEC inconnu : '{ptec}' — trame ignorée")
                        elif active_value is None:
                            log.warning(f"{active_name} absent de la trame TIC{_cause_rejets(active_name)} — trame ignorée")
                        elif iinst is None:
                            log.warning(f"IINST absent de la trame TIC{_cause_rejets('IINST')} — trame ignorée")
                        elif last_phases == 3 and len(labels.get("_phases") or {}) < 3:
                            # 🚨 Ce boîtier est SU triphasé : une trame qui n'apporte
                            #    qu'une ou deux phases donne une somme fausse, et elle
                            #    est indiscernable d'un monophasé sans cette mémoire.
                            log.warning(
                                f"trame triphasée INCOMPLÈTE "
                                f"({len(labels.get('_phases') or {})}/3 phases) — trame ignorée")
                        elif papp is None:
                            log.warning(f"PAPP absent de la trame TIC{_cause_rejets('PAPP')} — trame ignorée")
                        else:
                            demain, adps, pejp = build_flags(labels)
                            # Champs non-câblés (DEMAIN/ADPS/PEJP) → log INFO on-change (aligné LoRa).
                            log_uncabled({"DEMAIN": demain, "ADPS": adps or None, "PEJP": pejp or None})
                            # Clé générique (chantier index bi-mode) : index_id = rang PTEC.
                            labels["_src_standard"] = 0
                            note_tic_mode(0)
                            labels["_index_id"] = active_id
                            labels["_index_value"] = active_value
                            log.debug(f"OK pdl_index={PDL_INDEX} PTEC={ptec} {active_name}={active_value} "
                                      f"IINST={iinst} PAPP={papp} demain={demain} adps={adps} pejp={pejp}")
                            # On EMPILE dans le batch ; l'écriture BDD se fait par lot
                            # (flush plus bas, volet B). Log en DEBUG : à ~1,7 s/trame, un
                            # INFO par trame noierait journald (cf. preshipping).
                            if peut_stocker(measurements_db, PDL_INDEX):
                                batch.append((PDL_INDEX, labels, int(time.time())))
                            # 🚨 `frame_ok` HORS du garde : il pilote last_success_time,
                            #    lu par le watchdog (10 min sans succès -> os.execv). Le
                            #    mettre dedans ferait d'un ADCO illisible une BOUCLE DE
                            #    REDÉMARRAGE toutes les 10 min — ce qui a brûlé pi-0.9.12.
                            #    Le watchdog surveille la LIAISON TIC, pas la base : même
                            #    raison qui fait continuer le lecteur base indisponible.
                            _signaler_non_stocke(not peut_stocker(measurements_db, PDL_INDEX))
                            frame_ok = True
                    else:
                        # --- Mode STANDARD : papp←SINSTS, iinst←IRMS1 --------------
                        # Stockage GÉNÉRIQUE (chantier index bi-mode) : (src_standard=1,
                        # index_id=NTARF, index_value=EASF[NTARF]) + inject_total=EAIT +
                        # meter_ts (horodate compteur). papp+iinst aussi (courbe + jauge).
                        # PREF (kVA) ≠ ISOUSC (A) → pas mappé dans la jauge (maxVa fausse).
                        ntarf = labels.get("NTARF")
                        east  = labels.get("EAST")
                        easf  = labels.get("EASF", {})
                        active_value = easf.get(f"EASF{ntarf:02d}") if ntarf else None

                        if papp is None:
                            log.warning(f"SINSTS absent de la trame standard{_cause_rejets('SINSTS')} — trame ignorée")
                        elif iinst is None:
                            log.warning(f"IRMS1 absent de la trame standard{_cause_rejets('IRMS1')} — trame ignorée")
                        elif last_phases == 3 and len(labels.get("_phases") or {}) < 3:
                            # 🚨 Même garde qu'en historique, et elle est AUSSI
                            #    nécessaire ici : en standard un monophasé émet
                            #    `IRMS1` seul, donc une trame triphasée qui aurait
                            #    perdu IRMS2 ET IRMS3 est indiscernable d'un
                            #    monophasé sain. Seule la mémoire du raccordement
                            #    les sépare.
                            log.warning(
                                f"trame standard triphasée INCOMPLÈTE "
                                f"({len(labels.get('_phases') or {})}/3 phases) — trame ignorée")
                        else:
                            # _src_standard TOUJOURS posé (c'est le mode → tic_mode/lecture papp).
                            # index_id/index_value seulement si l'index actif a été vu (sinon NULL
                            # sur cette ligne → carry-forward au calcul conso, pas de point perdu).
                            labels["_src_standard"] = 1
                            note_tic_mode(1)
                            if ntarf is not None and active_value is not None:
                                labels["_index_id"] = ntarf
                                labels["_index_value"] = active_value
                            if labels.get("EAIT") is not None:
                                labels["_inject_total"] = labels["EAIT"]
                            mts = _std_horodate_to_epoch(labels.get("DATE_HORODATE"))
                            if mts is not None:
                                labels["_meter_ts"] = mts
                            # Champs non-câblés std (NJOURF/NJOURF+1 Tempo, STGE) → log INFO
                            # on-change (aligné LoRa). STGE est rendu LISIBLE : c'est le seul
                            # porteur de la couleur du LENDEMAIN, et un entier décimal de 32 bits
                            # serait indéchiffrable au journal. NJOURF/NJOURF+1, eux, valent 0 en
                            # permanence (calendrier FOURNISSEUR non programmé par EDF pour Tempo)
                            # — gardés par symétrie avec le LoRa, où ils sont désactivés en 0.1.8.
                            log_uncabled({"NJOURF": labels.get("NJOURF"),
                                          "NJOURF+1": labels.get("NJOURF+1"),
                                          "STGE": _stge_lisible(labels.get("STGE"))})
                            if not std_first_logged:
                                # 1re trame valide : dump complet en INFO pour valider
                                # le décodage sur un vrai compteur standard.
                                log.info("Première trame STANDARD décodée : "
                                         f"ADSC={labels.get('ADCO')} VTIC={labels.get('VTIC')} "
                                         f"SINSTS={papp} IRMS1={iinst} EAST={east} NTARF={ntarf} "
                                         f"EASF[{ntarf}]={active_value} LTARF={labels.get('LTARF')!r} "
                                         f"PREF={labels.get('PREF')} SINSTI={labels.get('SINSTI')} "
                                         f"EAIT={labels.get('EAIT')}")
                                std_first_logged = True
                            log.debug(f"OK[std] pdl_index={PDL_INDEX} SINSTS(PAPP)={papp} "
                                      f"IRMS1(IINST)={iinst} EAST={east} NTARF={ntarf} "
                                      f"EASF[{ntarf}]={active_value} SINSTI={labels.get('SINSTI')}")
                            if peut_stocker(measurements_db, PDL_INDEX):
                                batch.append((PDL_INDEX, labels, int(time.time())))
                            # 🚨 `frame_ok` HORS du garde : il pilote last_success_time,
                            #    lu par le watchdog (10 min sans succès -> os.execv). Le
                            #    mettre dedans ferait d'un ADCO illisible une BOUCLE DE
                            #    REDÉMARRAGE toutes les 10 min — ce qui a brûlé pi-0.9.12.
                            #    Le watchdog surveille la LIAISON TIC, pas la base : même
                            #    raison qui fait continuer le lecteur base indisponible.
                            _signaler_non_stocke(not peut_stocker(measurements_db, PDL_INDEX))
                            frame_ok = True

            except Exception:
                log.error(f"Exception dans la boucle principale:\n{traceback.format_exc()}")

            now = time.time()
            if frame_ok:
                last_success_time = now
                # Heartbeat vert DISCRET, throttlé (≠ un flash par trame ~1,7 s) — la
                # LED en bonne santé reste calme (cf. note d'origine).
                if now - last_heartbeat >= HEARTBEAT_S:
                    blink_rgb(0, 5, 0, 0.1)
                    last_heartbeat = now
            else:
                blink_rgb(5, 0, 0, 0.1, bypass=True)  # rouge immédiat (erreur, visible)

            # Flush du batch si âge ou taille atteinte (volet B).
            if batch and (len(batch) >= BATCH_MAX_SIZE
                          or now - last_flush >= BATCH_MAX_AGE_S):
                flush_batch()

            if measurements_db is not None and now - last_prune > 3600:
                try:
                    deleted = db.prune(measurements_db)
                    log.info(f"store purge (>{db.RETENTION_DAYS}j): {deleted}")
                except Exception as e:
                    log.warning(f"store: purge échouée: {e}")
                last_prune = time.time()

    except (KeyboardInterrupt, SystemExit):
        log.info("Arrêt.")
    finally:
        flush_batch()   # ne pas perdre le batch courant à l'arrêt
        try: ser.close()
        except Exception: pass
        try: GPIO.cleanup()
        except Exception: pass
