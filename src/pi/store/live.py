"""
live.py — la dernière mesure d'un PDL, enrichie de tout ce que le boîtier résout.

Source UNIQUE de la forme de `/live` (local_api.py) et de l'état poussé en MQTT
vers Home Assistant (ben-mqtt-ha). Deux consommateurs, une seule fabrique : sinon
un champ ajouté à l'un manquerait à l'autre, et personne ne le verrait — chacun
marcherait seul.

Lecture seule : ne reçoit qu'une connexion ouverte par l'appelant (`read_only=True`).
"""

import time

import db
import levels

DEFAULT_MAXVA = 9000          # échelle jauge par défaut (≈ 45 A × 230 V) tant qu'aucun abonnement lu


def rows(conn, pdl: int) -> list:
    # ⚠️ NE JAMAIS interroger `measurements` sans filtrer sur `pdl_index` :
    # l'index est (pdl_index, ts, papp), donc une requête non filtrée y est
    # AVEUGLE et balaye toute la table. Mesuré sur ben-0001 (3,1 M lignes) :
    # dernier point 114 s sans filtre contre 0,01 s avec.
    out = [dict(r) for r in conn.execute(
        "SELECT ts, pdl_index, base, hchc, hchp, papp, iinst, tariff, "
        "index_id, index_value, src_standard, inject_total "
        "FROM measurements WHERE pdl_index=? ORDER BY ts DESC LIMIT 1",
        (pdl,),
    ).fetchall()]
    # Niveau de conso 1..4 (visuel app), seuils pré-calculés par
    # ben-level-profiler ; ici lecture seule (cf. levels.py).
    now = int(time.time())
    for row in out:
        row["level"] = levels.level_for(
            conn, row["pdl_index"], row.get("papp"), now)
        # Mode TIC du PDL ('standard'/'historique'/null) — affiché par l'app.
        # En standard, `papp` est le NET SIGNÉ (>0 soutiré, <0 surplus injecté).
        mode = db.tic_mode(conn, row["pdl_index"])
        row["tic_mode"] = mode
        # Producteur = injection CONSTATÉE (pas juste EAIT présent) → l'app
        # affiche la jauge bidir soutirage⇄injection (Lot C). inject_total
        # est déjà dans la ligne (index monotone → dernière valeur = MAX).
        row["producer"] = db.producer(
            conn, row["pdl_index"], row.get("inject_total"))
        # Abonnement souscrit (réglages app + étalonnage jauge), exposé brut
        # dans son unité d'origine : ISOUSC (A) en histo, PREF (kVA) en standard.
        # Étalonnage maxVa : on prend la source du MODE COURANT (standard→PREF×1000,
        # histo→ISOUSC×230), avec repli sur l'autre si la valeur du mode manque encore
        # (transition / cold-start). None tant que rien n'est reçu.
        isousc = db.get_isousc(conn, row["pdl_index"])
        pref = db.get_pref(conn, row["pdl_index"])
        row["isousc"] = isousc
        row["pref"] = pref
        # 🚨 ISOUSC est PAR PHASE (#50). Un abonnement 20 A sur un triphasé
        #    autorise 3×20×230 = 13 800 VA, et `maxVa` borne un PAPP qui est
        #    un TOTAL : sans ce facteur la jauge saturerait au tiers de
        #    l'abonnement réel. `get_phases` rend 1 par défaut, donc les
        #    boîtiers monophasés du parc sont strictement inchangés.
        #    ⓘ PREF n'est PAS concerné : il est déjà un total (kVA).
        phases = db.get_phases(conn, row["pdl_index"])
        row["phases"] = phases
        from_isousc = isousc * 230 * phases if isousc else None
        from_pref = pref * 1000 if pref else None
        if mode == "standard":
            row["maxVa"] = from_pref or from_isousc
        else:
            row["maxVa"] = from_isousc or from_pref
        # Jauge : ÉCHELLE RÉSOLUE CÔTÉ BOÎTIER (autoritatif, comme level/tariff_label).
        # Tant que le foyer n'est pas CONNU (même gate que le niveau : ≥ MIN_SAMPLES,
        # ≥ 2 j, dynamique), le high-water mark observé ≈ la conso courante → une conso
        # faible apparaîtrait ROUGE juste après l'unboxing. On renvoie donc l'ABONNEMENT
        # (maxVa PREF/ISOUSC, sinon DEFAULT_MAXVA) comme échelle pendant l'apprentissage,
        # et le plafond OBSERVÉ (auto-calibré) une fois le foyer connu. L'app n'arbitre
        # plus : elle affiche `plafond` tel quel.
        plafond, inject_max = db.gauge_bounds(conn, row["pdl_index"])
        if not levels.is_known(conn, row["pdl_index"]):
            plafond = row["maxVa"] or DEFAULT_MAXVA
            inject_max = inject_max or row["maxVa"] or DEFAULT_MAXVA
        row["plafond"] = plafond
        row["injectMax"] = inject_max
        # Libellé tarifaire EN COURS (jauge HP/HC) — résolu côté serveur :
        # standard→LTARF autoritatif, histo→convention PTEC. None → l'app garde
        # sa propre convention (rétro-compat). Cf. chantier unification labels.
        row["tariff_label"] = db.resolve_label(
            conn, row["pdl_index"], row.get("src_standard"), row.get("index_id"))
        # Couleur Tempo en CHAMP EXPLICITE, résolue côté serveur comme le
        # libellé : sans elle, l'app devrait chercher « rouge »/« blanc »/
        # « bleu » dans un texte français, et connaître les deux conventions
        # (histo « Heures Pleines Jours Rouges » vs standard « HP  ROUGE »).
        # None hors Tempo.
        row["tempo_color"] = db.resolve_tempo_color(
            conn, row["pdl_index"], row.get("src_standard"), row.get("index_id"))
        # Contrat (NGTF, quasi-statique) — distinct du tarif en cours ci-dessus.
        row["contract"] = db.get_ngtf(conn, row["pdl_index"])
    return out
