# Home Assistant — le boîtier pousse, en MQTT

Le service `ben-mqtt-ha` publie l'état du boîtier sur le broker MQTT de Home Assistant, au
format **MQTT Discovery** : l'intégration MQTT native de HA crée les appareils et les entités
toute seule. **Rien à installer côté HA**, pas de composant, pas de HACS.

Le service est **opt-in** : installé et activé sur tous les boîtiers, il ne démarre que si
`/etc/ben-firmware/mqtt-ha.json` existe.

## Pourquoi du push, et pas Home Assistant qui sonde `:8087`

`:8087` reste là et reste ouvert (cf. `local_api.py`). Mais un HA qui sonde `/live` toutes les
N secondes réveille le Pi Zero pour rien la plupart du temps, et rate quand même l'instant où
la mesure arrive. Ici, c'est la mesure qui déclenche :

```
lecteur (ben-telemetry / ben-tic-reader)
   │  commit dans measurements.db
   ▼
ben/local/stored {pdl_index, ts}        bus MQTT LOCAL (127.0.0.1) — store/bus.py
   │
ben-mqtt-ha ── relit la base (lecture seule, store/live.py) ──► broker de Home Assistant
```

**Latence** = cadence d'écriture du lecteur : ~15 s en filaire (flush du batch), ~60 s en radio
(une trame courbe LoRa par minute). C'est la radio qui fixe ce rythme, pas le pont.

⭐ Le bus local ne change pas de nature : il n'écoute toujours que sur `127.0.0.1`, sans
persistance. Le pont est un **client** du broker distant ; aucun port n'est ouvert sur le
boîtier.

## Mise en route

### 1. Côté broker

Un utilisateur dédié au boîtier, avec mot de passe. ACL minimale :

```
user ben
topic readwrite ben/#
topic write homeassistant/device/+/config
topic read homeassistant/status
```

Mosquitto (module officiel HA ou autre) : `password_file` + `acl_file` sur le listener que le
boîtier joindra.

### 2. Côté boîtier

```bash
sudo tee /etc/ben-firmware/mqtt-ha.json >/dev/null <<'EOF'
{
  "host": "192.168.1.10",
  "port": 1883,
  "username": "ben",
  "password": "…"
}
EOF
sudo chown ben:ben /etc/ben-firmware/mqtt-ha.json
sudo chmod 600 /etc/ben-firmware/mqtt-ha.json
sudo systemctl restart ben-mqtt-ha
journalctl -u ben-mqtt-ha -f        # « broker HA connecté (…) »
```

| clé | défaut | rôle |
|---|---|---|
| `host` | — (requis) | broker MQTT de HA |
| `port` | `1883` | |
| `username` / `password` | `null` | auth du broker |
| `tls` | `false` | `true` → TLS ; CA système, ou `ca_file` |
| `ca_file` | `null` | CA du broker (chemin sur le boîtier) |
| `discovery_prefix` | `homeassistant` | doit valoir celui de l'intégration MQTT de HA |
| `base_topic` | `ben` | racine des topics d'état |

### 3. Côté Home Assistant

Rien, si l'intégration MQTT est configurée sur ce broker (Discovery est actif par défaut).
Deux appareils apparaissent : **BEN ben-XXXX** (le boîtier) et **BEN ben-XXXX Linky N** (un
par PDL), relié au premier.

Tableau **Énergie** → *Consommation du réseau* : ajouter **un capteur `Index …` par registre**
(Base, Heures Creuses, Heures Pleines, couleurs Tempo…) avec son prix — c'est ce qui permet le
coût juste en HC/HP ou en Tempo. `Index total` convient si un seul prix s'applique.
Producteurs : `Index injection` → *Retour au réseau*.

## Entités

Par PDL :

| entité | source | notes |
|---|---|---|
| Puissance apparente | `papp` | **VA**, pas W. En mode standard, net signé (< 0 = injection) |
| Intensité | `iinst` | A |
| Niveau de consommation | `level` | 1..4, comme la jauge de l'app |
| Tarif en cours | `tariff_label` | |
| Couleur Tempo | `tempo_color` | contrats Tempo seulement |
| Intensité / Puissance souscrite | `isousc` / `maxVa` | |
| Index *registre* | `/registers` | Wh, `total_increasing` — tableau Énergie |
| Index total | Σ registres du mode courant | Wh |
| Index injection | `inject_total` | producteurs seulement |
| Événement compteur | `event` | `changement_offre`, `changement_mode` — déclencheur d'automatisation |
| Contrat, Mode TIC, Dernière mesure, Production constatée | | diagnostic |
| Signal LoRa, SNR LoRa | `lora_link` | diagnostic, modèles radio |

Boîtier : **Luminosité LED** (0..5), réglable depuis HA — même effet que `POST /settings`.

Les valeurs *live* passent « indisponibles » après 15 min sans mesure ; les index, jamais (un
cumul reste vrai). Le boîtier publie un testament `offline` : s'il s'éteint, tout passe
indisponible.

🚨 **Changer d'offre crée de NOUVEAUX capteurs d'index**, et c'est voulu. En standard, le n°
d'un registre est une position dans le calendrier du fournisseur : après un changement d'offre,
le même numéro désigne un autre registre physique. Garder le même capteur ferait chuter
l'index, ce que HA lit comme une remise à zéro — et il compterait tout le nouvel index comme
consommé d'un coup. Le contrat fait donc partie de l'identifiant ; il faut rebrancher les
nouveaux `Index …` dans le tableau Énergie.

## Topics

```
ben/<id>/status                 online|offline         retenu, testament
ben/<id>/pdl/<n>/live           forme de GET /live + `time` (ISO 8601)      retenu
ben/<id>/pdl/<n>/registers      forme de GET /registers + `wh` + `total_wh` retenu
ben/<id>/pdl/<n>/link           {ts, rssi, snr}         retenu, modèles radio
ben/<id>/pdl/<n>/event          {event_type, id, ts, time, severite, titre, corps}
ben/<id>/led_level              0..5                    retenu
ben/<id>/led_level/set          ← commande HA
homeassistant/device/<id>/config          discovery du boîtier
homeassistant/device/<id>_pdl<n>/config   discovery d'un PDL
```

🔒 **L'ADCO n'est jamais publié** (cf. ARCHITECTURE §13) : un broker domestique le recopierait
dans l'historique et les sauvegardes de HA. Les entités sont identifiées par
`<deviceId>_pdl<n>`.

## Ce que le pont ne fait pas

- **Aucun tampon hors ligne.** Broker injoignable → rien n'est stocké ; à la reconnexion, tout
  l'état est republié. Les index étant cumulés, la statistique d'énergie de HA ne perd aucun Wh.
- **Aucune écriture sur la SD**, sauf `settings.json` sur commande LED.
- **Aucune exception remontée aux lecteurs** : le signal du bus est fire-and-forget.

Filet de sécurité : tout l'état est republié toutes les 5 min, même sans signal (lecteur
redémarré, signal perdu). Ce n'est pas la cadence normale.

## Désactiver

```bash
sudo rm /etc/ben-firmware/mqtt-ha.json
sudo systemctl stop ben-mqtt-ha
```

Les messages retenus restent sur le broker ; pour retirer les appareils de HA, publier un
message vide retenu sur chaque `homeassistant/device/…/config`, ou les supprimer dans HA.
