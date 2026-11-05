# Changelog — BEN Firmware

Toutes les versions notables du firmware BEN (Raspberry Pi + émetteur Arduino).
Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) ;
versionnage [SemVer](https://semver.org/lang/fr/).

Deux pistes indépendantes :

- **Pi** (`pi-x.y.z`) — récepteur / façade radio. Déployé par **OTA** (tags Git signés GPG,
  transitions `updates_caps` dans [`compatibility.yaml`](./compatibility.yaml)).
- **Émetteur Arduino** (`x.y.z`) — lecteur TIC (Pro Mini). **Reflash MANUEL** (pas d'OTA sur AVR).

> Détail machine des chemins d'update : `compatibility.yaml` (`updates_caps`).
> Ancienne matrice pré-ménage (history par-modèle + fallback `updates:`, figée au 2026-07-22) :
> `git show pi-0.9.1:compatibility.yaml`.

---

## Pi (récepteur / façade radio)

### [0.16.0] — à publier

**Home Assistant en push, par MQTT** — nouveau service **opt-in** `ben-mqtt-ha`. Guide :
[`docs/home-assistant.md`](./docs/home-assistant.md).

- Le boîtier publie son état sur le broker MQTT de Home Assistant au format **MQTT
  Discovery** : HA crée seul les entités (puissance, intensité, tarif, couleur Tempo, un
  index d'énergie par registre pour le tableau Énergie, injection, événements compteur,
  signal LoRa, luminosité LED réglable). **Aucun composant à installer côté HA.**
- **Push, pas de sondage** : les lecteurs publient `ben/local/stored` sur le bus local après
  chaque commit (`store/bus.py`, fire-and-forget — ne peut coûter aucune mesure) ; le pont
  relit alors la base en lecture seule.
- **Opt-in** : l'unité est posée partout mais sautée tant que
  `/etc/ben-firmware/mqtt-ha.json` n'existe pas. Un boîtier sans HA ne voit aucune différence.
- La forme de `/live` sort de `local_api.py` vers `store/live.py`, partagée par l'API et le
  pont — **`/live` rend exactement la même chose**.
- 🔒 L'ADCO n'est jamais publié.
- Aucune migration de schéma.

### [0.15.0] — 2026-10-17

**Le récepteur consomme le bit TRIPHASÉ** — la moitié manquante de
[`#51`](https://github.com/xdegenne/ben-firmware/issues/51). Chantier
[`ben-docs#19`](https://github.com/xdegenne/ben-docs/issues/19), sous-tâche
[`#56`](https://github.com/xdegenne/ben-firmware/issues/56).

#### 🚨 Le courant était juste et la jauge fausse

L'émetteur publie depuis `0.1.12` le **total des trois phases** et lève `CF_TRIPHASE` (bit 3 de
l'octet `flags`). Le récepteur décodait ce bit et le **jetait**. Or `ISOUSC` est exprimé **par
phase** alors que `maxVa` borne un `PAPP` qui est un **total** :

```
foyer triphasé 3×20 A tirant 5 000 VA — parfaitement normal
    avant :  5000 / (20 × 230)     = 109 %   🔴 rouge en permanence
    après :  5000 / (20 × 230 × 3) =  36 %
```

⭐ **Aucune migration** : `level_profile.phases` existe depuis `pi-0.14.0` (#50) et
`db.record_phases` aussi. La colonne n'était nourrie que par le lecteur **filaire**. Cette
version branche un consommateur sur une colonne déjà là.

#### 🚨 On n'écrit que 3, jamais 1

Le monophasé est l'**absence de déclaration**, pas une déclaration d'absence : une trame ayant
perdu une phase au checksum arrive sans le bit et ressemble trait pour trait à un monophasé.
Retenir ce « 1 » ferait retomber un triphasé sur **une seule trame abîmée**.

⭐ **Et l'invariant tient sur DEUX barrières indépendantes, mesuré par mutation** : faire écrire
`1` au récepteur ne casse rien — `record_phases` refuse seul. Il faut se tromper **deux fois**.

#### ⚠️ Personne n'est exposé, et rien ne s'active

**Aucun triphasé dans le parc**, et **aucun émetteur n'émet encore le bit** — le reflash est
**physique**, carte par carte : deux faites le 17/10 (ben-0011 et ben-0001), deux hors de
portée. `get_phases` rend 1 par défaut ⇒ les 8 boîtiers monophasés ne voient rien changer.

#### Éprouvé

7 cas, dont le contre-témoin qui protège le parc (une trame sans le bit n'écrit **rien**) et la
non-contamination d'un autre PDL. **Trois mutations rouges.** ⚠️ Une quatrième n'a pas tombé —
c'était la mutation qui était vide, pas le banc.

⚠️ **Et le banc tenait par accident d'ordre** (relevé en revue) : toutes les trames réutilisaient
`batch_seq=1`, donc les suivantes étaient marquées en doublon. La couverture tenait — le bloc
triphasé passe avant ce contrôle — mais *le banc serait devenu vide sans le dire* si cet ordre
changeait. Corrigé.

### [0.14.0] — 2026-10-17

**Deux correctifs sans rapport, groupés dans une même OTA.**

#### ① Le triphasé — le courant publié est le TOTAL des trois phases ([#50](https://github.com/xdegenne/ben-firmware/issues/50))

```
IINST = IINST1 + IINST2 + IINST3     (historique)
IINST = IRMS1  + IRMS2  + IRMS3      (standard)
maxVa = ISOUSC × 230 × phases
```

🚨 **Le pire des deux défauts était le silencieux.** En historique, `IINST` n'est jamais émis par
un triphasé (c'est `IINST1/2/3`) ⇒ la trame était jetée, le compteur ne produisait **rien** ; ça
se voyait. En standard, `IRMS1` existe et était mappé vers `IINST` ⇒ le boîtier publiait **le
courant d'une phase sur trois**. Aucune alerte, une courbe plausible, une donnée fausse — et
d'autant plus discret que `PAPP` était juste (`SINSTS` est un total).

⭐ **La somme, et pas le max.** `IINST` n'alimente pas la jauge : son seul usage calculatoire est
`230 × IINST` quand `PAPP` vaut 0. En triphasé chaque phase est à 230 V vers le neutre, donc
`S = 230 × (I1+I2+I3)` — la somme rend ce secours **exact**, le max le sous-estimerait d'un
facteur 3. ⓘ Et l'app refait le même calcul, donc l'erreur aurait été dupliquée hors de portée
d'une OTA.

⭐ **Le critère est la SIMULTANÉITÉ** : `IINST1/2/3` viennent de la même trame. `IMAX1/2/3` sont
des extrêmes datés séparément (`Σ max ≠ max Σ`) ⇒ hors périmètre, par décision.

🚨 **`ISOUSC` est par phase.** 20 A par phase autorisent `3 × 20 × 230 = 13 800 VA`, mais `maxVa`
borne un `PAPP` **total** : la jauge saturait au **tiers** de l'abonnement. Nouvelle colonne
`level_profile.phases` (NULL = 1). ⓘ `PREF` n'est pas concerné, il est déjà un total.

⚠️ **On n'enregistre que 3, jamais 1** : le monophasé est l'absence de déclaration, pas une
déclaration d'absence — une trame tronquée dirait la même chose. Et « triphasé » ne se conclut
jamais sur `IRMS1`, qu'un monophasé standard émet.

🚨 **Une somme incomplète est une donnée FAUSSE** : si `IINST2` tombe au checksum, `I1 + I3`
serait écrit, là où le monophasé perdant son `IINST` voyait sa trame rejetée. Deux gardes — le
décodeur retire la clé, et la trame est rejetée si le raccordement **su** triphasé n'apporte pas
ses trois phases.

#### ② Le BLE n'est plus coupé sous l'utilisateur ([#49](https://github.com/xdegenne/ben-firmware/issues/49))

`_wait_for_ble_session_end()` vivait dans la branche `else` de `network_recovery.main()` — donc
consultée uniquement quand la fenêtre s'écoule **sans** réseau, c'est-à-dire quand personne ne
configure rien. Sur le chemin nominal du déballage, le provisioner était tué **en pleine
session** et le service GATT disparaissait sous l'app (`primary service not found`).

⚠️ **Course de 0 à 30 s** (`POLL_INTERVAL_SEC`), d'où l'intermittence qui l'a caché : trois
déballages ont réussi le 05/10, un a échoué le 17/10, un autre a réussi le même jour sans le
correctif. Une radio WiFi/BLE **partagée** rendait « le Bluetooth est capricieux » plausible.

ⓘ Actif au **prochain démarrage** : `ben-network-recovery` est un oneshot de boot.

#### Éprouvé

`test_triphase.py` (34 cas, trames forgées) et `test_network_recovery.py` (+2 cas). **Neuf
mutations vérifiées rouges.** Contre-témoins monophasés verts avant comme après : les 7 boîtiers
du parc sont monophasés et ne voient rien changer.

⚠️ Le préflight ③ de l'update contrôle les gardes sur l'**arbre syntaxique**, docstrings
retirées — et son contrôle du volet BLE est **structurel**, pas positionnel : une comparaison
d'indices ne distingue pas « dans le `else` » de « après le `else` », et la mutation passait au
vert.

#### Reste ouvert

`ben-app#4` (l'app affiche ce total comme un courant) · `ben-api#33` (le cloud ignore `phases`)
· `ben-firmware#51` (la passe émetteur radio) · `ben-firmware#53` (trames courtes en dépassement).

### [0.13.0] — 2026-10-07

**L'accès local.** TLS sur l'écoute LAN, jetons porteurs de rôle, et `/claim` relayé au cloud
**par ticket**. Chantier [`ben-docs#3`](https://github.com/xdegenne/ben-docs/issues/3), sous-tâche
[#35](https://github.com/xdegenne/ben-firmware/issues/35).

#### 🚨 La règle cardinale : `:8087` continue de servir, quoi qu'il arrive

C'est lui que l'app du parc et Home Assistant utilisent, **en permanence**. Le code le garantit :
tout chemin d'échec de `_ecoute_tls()` rend `None`, et l'écoute en clair vit dans le thread
**principal**. ⓘ Le `bind` est **dans** le `try` — dehors, un port déjà pris tuait l'API entière
en boucle de redémarrage, là où la docstring promettait l'inverse.

⇒ **Le contrôle d'effet exige `:8087` et RAPPORTE `:8088` sans l'exiger.** Exiger `:8088`
brûlerait la version sur tout boîtier dont le certificat n'a pas de SAN — et le SAN ne se lit
**pas** depuis le cloud (`cert_task.cert` est vidée à la livraison). *Un garde-fou plus exigeant
que le code qu'il garde est un garde-fou faux* (pi-0.9.12).

#### Le partage des deux ports

```
:8087  en clair   GELÉ sur ses 14 routes historiques. Aucune exigence.
:8088  chiffré    jeton EXIGÉ dès le premier jour. Une seule exemption : /claim.
```

La surface d'administration neuve (`/access`, `/invitations`, `/tokens/*`) n'existe **que** sur
`:8088` et répond **404** sur le port clair — 404 et non 426, pour ne pas confirmer à qui balaie
le port clair qu'il y a une administration ailleurs.

⭐ **L'exigence naît AVEC le canal chiffré**, elle ne s'arme pas « plus tard, sur preuve que plus
personne n'appelle sans jeton » : ce jour-là n'arrive jamais tout seul. Une app qui parle `:8088`
a forcément revendiqué, donc il n'y a **aucune population à migrer**.

#### 🚨 Le boîtier relaie un TICKET, jamais un jeton Firebase

Un `ID token` est un **porteur** valable ~1 h auprès de tout le projet Firebase BEN, et
l'attaquant réaliste n'est pas l'opérateur : c'est **l'owner d'un boîtier rooté contre ses propres
invités**. Un en-tête `Authorization` sur `/claim` est donc un **refus franc**.

⭐ **Et le même ticket, remis en BLE, fonde le premier propriétaire** — c'est ce qui débloquait la
livraison d'un boîtier à un client. `grant()` n'était appelé que depuis `mint()` et
`consume_invitation()` : un boîtier neuf était **inrevendiquable**, et les 7 du parc ne
fonctionnaient que par des lignes semées à la main — *un rattrapage, pas une procédure*. La garde
est **côté cloud** (`ben-api#26`) : il refuse la création d'un premier owner sans `fonder`. Le
boîtier ordonne, le cloud exécute — seul partage possible, puisque le boîtier ne sait pas si le
cloud a déjà un owner, mais que **lui seul sait par quel canal le ticket est arrivé**.

#### ⚠️ Le champ d'accès voyage dans `/ping`, pas dans `/hello`

Et c'est le piège **qu'aucun conflit git ne signale**. Le serveur refuse les champs inconnus
(`DisallowUnknownFields`) : un `access` resté dans le hello partirait en **400 `bad_body`** à
chaque déclaration, et la fonctionnalité serait **morte en silence**. Le préflight ④ le vérifie
sur l'**arbre syntaxique**, jamais par `grep` — les commentaires nomment les deux routes. Les deux
moitiés se livrent ensemble : `ben-api` l'accepte depuis `a0e0f42`, **déployée**.

#### 🚨 Le sudoers est validé par `visudo -c` AVANT d'être posé

Un `/etc/sudoers.d/` cassé casse `sudo` **pour tout le monde**, root compris, sur un boîtier qui
est chez un client. C'est le seul préflight de cette update dont l'absence serait une panne
**irrécupérable à distance**. Il est revalidé après la pose, et `sudo -n true` rejoué.

`ben-local-api` rejoint `ben-publisher` dans ce fichier : un processus Python lit son certificat
**une seule fois**, à la construction de son contexte SSL, donc `ben-certd` doit pouvoir le
redémarrer après l'avoir remplacé.

⚠️ **Et `install.sh` ne posait pas ce fichier.** Il avait été posé au parc par
`updates/0.9.15_to_0.9.16/update.sh` : tout boîtier provisionné **depuis** en était dépourvu, et
aurait servi un certificat périmé **en silence**. Corrigé ici — même classe de défaut que le
watchdog de 0.12.0, un fichier du dépôt que la liste explicite d'`install.sh` avait oublié.

#### Trois commentaires FAUX, corrigés

- `local_api.py` portait **deux doctrines contradictoires** : un bloc disait « cette version ne
  refuse rien, l'exigence s'armera plus tard », et `_exige_jeton`, trente lignes plus bas, disait
  l'inverse en toutes lettres. *Deux doctrines dans un fichier : celle qu'on lit en premier est
  celle qu'on croit.*
- « quatre boîtiers du parc portent encore le certificat d'origine, 3650 jours » — **faux depuis la
  bascule PKI** : mesuré côté cloud, les 8 portent un certificat de **180 jours** émis entre le
  24/09 et le 05/10. Le contrôle n'en devient pas décoratif : c'est le **SAN** qui décide
  désormais, et lui ne se lit pas depuis le cloud.
- `access.py` disait dix fois que les lignes remontent « au hello ». Elles remontent dans le
  **battement** (`/ping`) depuis le rebase du 03/10. ⓘ L'argument du « miroir en retard » tient
  quand même : `HELLO_EVERY = 86400`, le battement est **quotidien**.

#### Ce que ça ne fait pas

Aucune migration de `measurements.db`. `access.db` est une base **séparée**
(`/var/lib/ben-firmware/access.db`) créée à la première session, en écriture, par l'API locale
elle-même. ⇒ **Retour arrière vers 0.12.0** : le sudoers et l'unité resteraient en place, sans
danger — l'ancien code ne lit ni l'un ni l'autre, et il n'ouvre pas `:8088`.

### [0.12.0] — 2026-10-07

**Le filet du réseau est enfin posé.** Chantier
[`ben-docs#17`](https://github.com/xdegenne/ben-docs/issues/17), sous-tâche
[#46](https://github.com/xdegenne/ben-firmware/issues/46).

ben-0005 a passé **8 h 40 sans réseau** le 06/10 : il mesurait, il ne publiait plus, et l'OTA
échouait pour la même cause — donc **le seul canal de réparation à distance était fermé en même
temps que la publication**. Il a fallu aller le redémarrer à la main.

```
wifi_watchdog.sh    deux GARDES, et le compteur de rattrapages
install.sh          + install -m 755 du script  + enable du timer   ← la ligne qui manquait
health.py           + les TIMERS (UnitFileState)  + nm_restarts, à plat
```

#### ⚠️ Le remède existait déjà et n'était installé nulle part

`wifi_watchdog.sh` est dans le dépôt depuis le **premier commit réel** (`9cce372`, bootstrap du
30/05) avec son unité et son timer. Mais `install.sh` copie les unités **en bloc**
(`cp config/systemd/*`) alors que la liste des `enable` est **explicite, un par un** — et le
watchdog n'y était pas. D'où une unité `loaded` sur les 8 boîtiers et un timer `disabled` sur les
8 : deux faits qui avaient l'air de se contredire, et qui venaient d'**une ligne manquante**.

ⓘ **L'historique git est net** : aucun commit ne l'a jamais activé, aucun ne l'a jamais désactivé.
Il est arrivé au *bootstrap* — l'import d'une machine configurée à la main — donc il tournait
probablement sur **cette** machine, enrôlé par un `systemctl enable` qui ne vivait que dans l'état
de sa carte SD. Chaque boîtier reflashé depuis est reparti sans, en silence. Même classe de défaut
que `ben-recognizer` et `ben-remote`, qui tournent sur le banc sans être dans le dépôt.

⭐ **Et il aurait suffi** : la Freebox ne voyait **aucun équipement**, donc le boîtier n'était pas
associé, donc `ping 1.1.1.1` aurait échoué et le watchdog aurait relancé NetworkManager toutes les
2 minutes jusqu'à la reprise.

#### 🚨 Deux gardes, et elles sont dans le SCRIPT

Il y a un moment où l'absence de réseau est **voulue** : la fenêtre BLE du déballage. Toucher à
NetworkManager pendant que le téléphone écrit les identifiants toucherait à une radio **partagée**
WiFi/BLE sur Pi Zero W — ce qui a déjà coûté un décrochage en déballage Android.

```bash
# ① déballé ? le FICHIER d'abord — lisible même si NetworkManager est mort
grep -qsx "id=ben-provisioned" "$CONN_DIR"/* || { nmcli … || « NM en panne ⇒ on agit » }
# ② session BLE ? avec sa PÉREMPTION de 900 s
[ -f "$BLE_FLAG" ] && [ "$AGE" -lt 900 ] && exit 0
```

🚨 **La garde ① ne dépend pas de NetworkManager vivant**, et c'est un défaut trouvé en revue qui
**annulait tout le watchdog** : interrogée par `nmcli` seul, elle concluait « pas déballé » dès que
NM était `failed` — donc elle sortait en 0 **à chaque tick**, exactement pendant la coupure qu'elle
doit couvrir. Elle lit donc d'abord le **fichier** de connexion (état persistant), et traite une
erreur `nmcli` comme « NM est en panne, donc on agit ».

🚨 **Et elle lit le CONTENU du keyfile, pas son nom** — mesuré sur ben-0001 : le fichier s'appelle
`ben-provisioned-tmp-1783764212.nmconnection` et porte `id=ben-provisioned`. Un `ls
"$CONN_DIR"/ben-provisioned*` marchait donc **par accident**, sur un nom temporaire que rien ne
garantit, et il aurait dit « déballé » pour n'importe quel keyfile *nommé* ainsi. La garde `grep -qsx
"id=ben-provisioned"` interroge l'identité que NetworkManager, lui, utilise vraiment.

🚨 **Et le drapeau BLE a une péremption** (900 s, comme `SESSION_MAX_SEC`). Le provisioner n'a **pas
de gestionnaire SIGTERM** : le drapeau survit à son arrêt — `_watch_window` qui voit revenir le
réseau, une session qui dépasse 15 min, le `Conflicts=` d'un lecteur — et sans péremption il
bloquait le watchdog **jusqu'au redémarrage**.

Les deux marqueurs existaient déjà (`check_network._has_been_provisioned`,
`provisioning_state.ble_session_active`). ⭐ **Dans le script, et non dans un `enable` placé au bon
moment** : le « quand » devient une **propriété** relue à chaque tour — même doctrine que la
déclaration de version de `0.10.0`, *une condition se re-vérifie, un geste s'oublie* — et ça vaut
pour les boîtiers neufs **comme** pour les 8 existants.

#### 🚨 Une troisième garde : le drapeau ne voyait pas la fenêtre la plus exposée

Le drapeau BLE ne dit qu'une chose — **un téléphone est CONNECTÉ**. Or il existe une fenêtre plus
longue, et plus exposée, où personne n'est connecté et où la radio est prise quand même :

> Coupure de courant, le boîtier revient **avant** la box. Il est **déjà déballé**, donc la garde ①
> le laisse passer. `ben-network-check` démarre `ben-network-recovery`, qui **offre** le BLE pendant
> **300 s** et se contente de **pinguer** pendant ce temps — exprès, pour ne pas toucher à la radio
> partagée. Le watchdog, lui, part 60 s après le boot puis toutes les 2 min : **aucun téléphone
> connecté donc aucun drapeau**, et il relancerait NetworkManager deux ou trois fois dans cette
> fenêtre, avec un scan WiFi complet à chaque fois. Soit **le décrochage de déballage que ce script
> prétend éviter**.

Second chemin, lui aussi réel : un **re-provisioning avec un mauvais mot de passe**. Le provisioner
sort (`os._exit(1)` + `Restart=on-failure`) et son `main()` **efface le drapeau à chaque
démarrage** — entre deux reconnexions BLE, il n'y a donc pas de drapeau.

⭐ **Une unité active est un meilleur témoin qu'un drapeau** : c'est un état que systemd tient, pas
un fichier qu'un processus doit penser à poser **et** à retirer. Troisième application de la même
doctrine. ⇒ Le watchdog s'abstient dès que `ben-ble-provisioner` ou `ben-network-recovery` est
`active`, `activating` ou `deactivating`.

ⓘ **C'est borné**, donc ça n'annule pas le filet : `network_recovery` sort au bout de sa fenêtre, et
sur un boîtier déjà déballé le provisioner n'est lancé que par lui.

#### ⚠️ Et l'unité portait une clé qui n'existe pas

`AccuracyMin=30s` — depuis le premier commit. La clé s'écrit **`AccuracySec`**. systemd l'ignore
**en silence**, sans une ligne de journal, et applique son défaut de **1 min** : mesuré sur
ben-0001, `AccuracyUSec=1min`. Un timer de 2 min avec 1 min de battement, là où l'intention écrite
était 30 s. L'update **pose donc aussi l'unité** et vérifie ce que systemd en a retenu — sinon le
dépôt dirait une chose et les 8 boîtiers une autre, exactement la classe de défaut qui a produit ce
chantier.

#### 🚨 La liaison n'est pas la ligne — et le confondre cassait ce qui marche encore

Un `ping 1.1.1.1` qui échoue ne dit pas que la radio est en panne : il dit qu'**internet est
injoignable**. Les deux causes sont distinctes, et la seconde est banale — une coupure de ligne chez
l'opérateur, box et WiFi parfaitement sains. Redémarrer NetworkManager toutes les 2 min pendant une
panne FAI était faux sur trois plans :

1. 🚨 **ça coupait l'API locale `:8087` à chaque fois** — or c'est exactement ce qui marche **encore**
   pour l'app pendant une coupure internet, donc ce qu'il ne faut surtout pas casser ;
2. `nm-restarts` grimpait, et le cloud aurait relu une panne de **ligne** comme une **radio
   défaillante** : un compteur ne vaut que s'il ne compte que ce qu'il prétend compter ;
3. ça ne réparait rien — la ligne n'est pas de ce côté-ci du mur.

⇒ **Le discriminant est la passerelle.** Si on l'atteint, la liaison est bonne et le défaut est en
amont : on se tait. Si on ne l'atteint pas, ou s'il n'y a **aucune route par défaut**, la liaison
est morte et c'est notre affaire. ⓘ Le cas de ben-0005 reste couvert : la Freebox ne voyait aucun
équipement ⇒ pas d'association, pas de bail, pas de route par défaut ⇒ on agit.

#### ⚠️ Le watchdog tirait avant que NetworkManager existe

`OnBootSec=60`, depuis le premier commit. Mesuré sur ben-0001 en **monotone depuis le boot** :

```
NetworkManager       démarre à 66,2 s   actif à 84,0 s
ben-network-check    de 84,3 s à 104,4 s
wifi-watchdog        tirait à 60,0 s          ← avant les deux
```

Le premier passage tombait donc **avant** NetworkManager : il aurait compté un redémarrage et
bousculé le démarrage en cours, **à chaque boot**, en polluant le seul compteur sur lequel tout ce
chantier repose. ⇒ `OnBootSec=300`, et `ben-network-check.service` rejoint la garde ③ — ce oneshot
pinge jusqu'à 30 s puis joue ~5 s de LED, et c'est **lui** qui décide collecte ou récupération : le
bousculer le ferait conclure « hors ligne » et déclencher 300 s de BLE **sans aucune collecte**,
pour une box simplement lente.

⭐ **Et c'est le bon partage** : le boot a déjà son filet (`ben-network-check` →
`ben-network-recovery`). Celui-ci est le filet du boîtier **en marche**, et il prend la main là où
l'autre a fini.

#### 🚨 Et il compte, ce qui est aussi important que de rattraper

Un filet qui rattrape **en silence** rend un boîtier malade **indiscernable** d'un boîtier sain :
celui qui perd sa radio toutes les deux heures publie normalement. Le watchdog incrémente donc
`/var/lib/ben-firmware/nm-restarts` — écriture **atomique**, dans `/var/lib` et **pas** `/run` pour
survivre aux redémarrages, puisqu'un boîtier malade reboote — et `health` le fait monter **à plat**
(`nm_restarts`).

⚠️ **Aucun champ systemd ne le donnait.** `NRestarts` compte les redémarrages **automatiques** de
systemd (`Restart=`), pas un `systemctl restart` lancé par un script. Et `health.errors()` ne lit
que la priorité 3 (`err`), donc la ligne `logger` du watchdog, en `notice`, n'y remonterait pas.

#### ⭐ `health` remonte désormais les TIMERS — il ne le faisait pas du tout

Il suffixait chaque nom par `.service`, donc **aucun timer** n'arrivait au cloud :

- on ne pouvait pas voir si `wifi-watchdog.timer` était activé — et il est `disabled` sur les 8 ;
- 🚨 on ne pouvait pas voir si **`ben-update.timer`** était activé, c'est-à-dire **si un boîtier
  prend encore ses OTA**, alors que l'OTA est le seul canal de réparation à distance ;
- un `.service` piloté par timer est `inactive/dead` entre deux exécutions **par construction** :
  c'est l'état du **timer** qui porte l'information.

On relève donc `UnitFileState` (`enabled`/`disabled`) et non `ActiveState`. ⇒ C'est ce qui dira si
cette update a pris sur les **six** boîtiers qu'on ne peut pas joindre en SSH.

ⓘ `nm_restarts` est **à plat**, et c'est une leçon payée : `units` est un **tableau**, donc
`health->'units'->>'wifi-watchdog'` rend `NULL` en SQL. J'en avais conclu à tort que `health` ne
rapportait rien — et bâti une priorité entière sur cette fausse mesure.

#### L'ordre de pose n'est pas indifférent

**Le script d'abord, le timer ensuite.** L'unité est déjà installée sur le parc, avec un `ExecStart`
qui pointerait dans le vide : activer le timer avant produirait un échec toutes les 2 minutes,
indéfiniment. `install.sh` reçoit les deux mêmes gestes — sinon le prochain déballage repartirait
sans watchdog, exactement l'oubli qui a produit celui-ci.

#### Le banc, et ce qui a été vu tomber

**18 cas** pour le watchdog, montant de faux `nmcli`/`ping`/`systemctl` dans un `PATH` temporaire —
il tourne donc sans NetworkManager, sur un Mac comme sur un boîtier. Les deux gardes dans tous leurs
états, dont **`nmcli` en erreur** (NM mort ⇒ il doit **agir**) et un **drapeau BLE périmé** (⇒ agir
aussi), l'incrément du compteur, et un compteur **vide** qui vaut zéro sans faire échouer. Deux
cas que le keyfile a imposés : le fichier sous son **nom temporaire réel**, et un **leurre** — un
keyfile nommé `ben-provisioned*` mais portant un autre `id=` — qui ne doit **pas** compter. Et trois
pour la garde ③ : la fenêtre de `network_recovery` **sans téléphone**, le re-provisioning **entre
deux reconnexions**, et une unité en `activating` qui compte déjà comme prise. Et trois
de plus pour les deux derniers défauts : le **contrôle de boot en cours**, une **ligne coupée avec
la passerelle joignable** (⇒ ne rien redémarrer), et **aucune route par défaut** (⇒ agir, c'est
ben-0005).

🚨 **Un faux plus permissif que la réalité valide le défaut.** Le faux `systemctl` sortait en 0 pour
n'importe quel état : une mutation qui testait le **code** de `is-active --quiet` au lieu du **mot**
restait donc **verte**. Le vrai sort en 0 pour `active` **seulement** — `activating` sort en 3. Faux
corrigé, mutation rouge. Même classe pour le faux `ping` : s'il répond **pareil aux deux cibles**,
la ligne coupée et la liaison morte deviennent indiscernables et le banc approuverait un script qui
ne discrimine rien — muté, rouge.
⚖️ Le contre-témoin est la moitié du banc : un watchdog qui ne redémarrerait **jamais** passerait
tous les cas de refus.

**35 cas** pour `health`. **23 mutations rouges** au total. ⚠️ Dont une restée **verte**, qui a fait
ajouter le témoin du **branchement** de la sonde dans `snapshot()` : une sonde livrée mais jamais
appelée est une mesure qu'on croit avoir.

ⓘ **Et l'update a été rejouée en tant que `ben`, pas en `root`** — c'est ainsi que l'agent la
lance (`User=ben`), et c'est la première update qui écrit hors du dépôt : `/usr/local/bin` et
`/etc/systemd/system`. `install.sh` pose `ben ALL=(ALL) NOPASSWD: ALL` depuis le commit de
bootstrap, donc les 8 boîtiers l'ont par construction (fichier daté du 31/05 sur ben-0001), mais un
**préflight ⓞ** le dit maintenant tout de suite, avant d'avoir touché à quoi que ce soit : sans lui,
un sudoers manquant donnerait une update qui retombe à chaque tick sur un message illisible.

ⓘ Et un piège du script d'update lui-même, trouvé en revue : un message d'erreur contenant des
**backticks** entre guillemets doubles est une **substitution de commande** — `bash -n` y serait
lancé sans argument, lirait `stdin` et pourrait **bloquer l'update**. Guillemets simples.

#### Ce que ça ne fait pas

Aucune migration, aucune table, aucun lecteur redémarré — le nouveau `health.py` est lu par
`ben-publisher`, que l'agent redémarre à l'étape ⑩. Retour arrière vers 0.11.1 : **rien à
restaurer**, le script et le timer resteraient en place — ils étaient le but.

⚠️ **Et ce que cette version n'explique pas** : *pourquoi* la radio est tombée 181 fois en 4 heures
alors qu'un boîtier sain n'en fait **aucune** en 43 heures. Le watchdog **masque** le symptôme. ⇒
`ben-docs#17`, point ③ — ne pas fermer ce chantier sur un symptôme disparu.

### [0.11.1] — 2026-10-06

**Une release qui ne change rien, exprès.** Chantier
[`ben-docs#16`](https://github.com/xdegenne/ben-docs/issues/16), sous-tâche
[#44](https://github.com/xdegenne/ben-firmware/issues/44).

Le mécanisme de la ref par boîtier est livré des deux côtés — `pi-0.11.0` sur le parc,
`GET /api/devices/{id}/update` déployé — mais il n'avait **jamais fait passer une vraie release à un
seul boîtier**. C'est ce que celle-ci éprouve, et sans rien risquer : si quelque chose casse, ça
casse sur un `update.sh` qui n'a rien à casser.

Le chemin complet est exercé — ref demandée au cloud, plan lu sur `canary`, signature GPG du tag,
SHA256 du script, exécution, bump, redémarrage du publisher, déclaration au cloud — et le script
lui-même est **inerte**.

#### Ce qu'elle ne fait pas, et c'est exhaustif

Aucun fichier livré. Aucune migration, aucune table, aucune colonne. Aucun service redémarré par le
script. **Aucune écriture, nulle part** — ni dans la base, ni dans `/var/lib`, ni dans `/etc`.

⇒ Le retour arrière vers 0.11.0 ne demande de restaurer **rien** : il n'y a rien à restaurer. Le
seul effet observable est `device.json.softwareVersion = 0.11.1`, écrit par l'**agent** à l'étape ⑨
— pas par le script.

#### ⭐ Le flux éprouvé, et c'est lui le livrable

```
① travail     <type>/<issue#>-<slug>   update.sh · son sha256 · le CHANGELOG
② tag d'essai pi-0.11.1-rc1 signé      sur la tête de CETTE branche
③ canary      UNE ligne                l'entrée updates_caps pointant ce tag
④ on regarde  ben-0001 seulement       ota_ref='canary' côté cloud
⑤ si bon      squash dans main + pi-0.11.1   → tout le parc
⑥ canary      on retire la ligne
```

⭐ **Le tag n'a pas besoin d'être « dans » `canary` ** : le boîtier lit `compatibility.yaml` depuis
`origin/<ref>` et `update.sh` depuis **le tag**. Donc `canary` reste un **plan de contrôle pur** —
une ligne, jamais de code — et le code ne vit qu'à un seul endroit. ⓘ Un tag survit à la suppression
de sa branche.

🚨 **Le `to` est le même des deux côtés, seul le tag diffère.** Cette entrée porte `pi-0.11.1`, pas
le `-rc1` : la branche de travail est exactement ce que `main` recevra, et c'est `canary` qui porte
la variante d'essai. C'est aussi ce qui fait qu'un boîtier d'essai déjà en `0.11.1` ne **rejoue
rien** quand `main` le rattrape — aucune transition ne part de `0.11.1`. Si les `to` divergeaient, il
se retrouverait sur une version que `main` n'a jamais entendue, et `hors_du_plan` crierait pour
toujours.

#### Ce qui se passe quand même, et qu'il faut regarder

L'agent redémarre `ben-publisher` à l'étape ⑩, systématiquement. Le publisher constate alors que la
version installée n'est pas celle qu'il a déclarée, et **déclare** — c'est la condition de
`pi-0.10.0`, et elle fait que `devices.sw_version` suit sans qu'on y pense :

```
déclaration : version installée 0.11.1, déclarée 0.11.0
déclaration OK — N compteur(s) déclaré(s)
```

⚠️ **Et le témoin de tout ce chantier est ailleurs** : les boîtiers dont `ota_ref` est `NULL` ne
doivent voir **aucune** transition. Si le parc entier passe en 0.11.1, le mécanisme n'a pas
fonctionné — il a seulement eu l'air de fonctionner.

### [0.11.0] — 2026-10-06

**Chaque boîtier demande au cloud où chercher ses mises à jour.** Chantier
[`ben-docs#16`](https://github.com/xdegenne/ben-docs/issues/16), sous-tâche
[#42](https://github.com/xdegenne/ben-firmware/issues/42).

Le but : valider une OTA sur une branche, sur **un ou deux boîtiers seulement**, puis fusionner
la branche sur `main` et tout le monde reçoit. Jusqu'ici `main` était écrit **en dur** dans
l'agent, deux fois — donc un tag publié partait sur les 8 boîtiers au tick suivant, dans les dix
minutes. Une release touchant plusieurs surfaces à la fois (`#35` en touche trois : le service
qui publie, l'API locale, le provisioning BLE) ne pouvait pas s'essayer sur un boîtier d'abord.

```
① avant chaque tentative   GET /api/devices/<id>/update, en mTLS
② le cloud répond          { "ref": "canary" }
③ rien d'exploitable       ⇒ main
④ l'agent fetch cette ref  et y lit compatibility.yaml
⑤ le reste est intact      GPG du tag · SHA256 du script · UNE transition par tick
```

#### 🚨 C'est l'agent qui demande, pas le publisher qui relaie

Si une mauvaise version tue le publisher, on doit **encore pouvoir piloter ce boîtier** — c'est
précisément le moment où on en a besoin. Le prix est une trentaine de lignes de client mTLS dans
l'agent, **entièrement sous `try/except`** : il répare tous les autres services, il ne doit pas
gagner une dépendance capable de le tuer. Même doctrine que l'import défensif de
`label_for_model`.

#### ⭐ Débrayable, et sans contrainte d'ordre de déploiement

La route n'existe pas encore (`ben-api#29`) : tout le parc prend donc `main` et se comporte
exactement comme avant. Ce volet part **seul**, et le jour où la route serait coupée, rien ne
s'arrête. C'est l'inverse du champ `access` de `ben-docs#3`, qui exigeait l'API **avant** le tag.

#### 🚨 Et la ref ne peut désigner qu'une BRANCHE

Côté source de la refspec, `refs/heads/<ref>` — et c'est une **barrière**, pas une précision. Avec un
simple `<ref>`, git résout le nom à sa façon et ne cherche pas que dans les branches : vérifié sur
git 2.54.0, `+pi-0.11.0:…` rapatrie le **tag**, et `+pull/43/head:…` la **tête d'une pull request**.

Or `ben-firmware` est **public** : n'importe qui peut ouvrir une PR depuis un fork, donc un
`ota_ref = "pull/N/head"` ferait lire un plan **écrit par un inconnu**. La signature GPG protège
toujours le **code** — `update.sh` vient du tag — mais un plan étranger pourrait faire rejouer un
vieux `update.sh` signé sur une version qui n'est pas la sienne.

ⓘ C'était aussi incohérent avec `ref_existe_sur_origin`, qui ne regarde que `refs/heads/` : le fetch
pouvait réussir là où la vérification d'existence aurait dit non.

#### 🚨 Le nom de branche vient du réseau et finit dans une ligne de commande `git`

`ref_valide` impose un alphabet **fermé** : `[a-z0-9][a-z0-9._/-]{0,99}`. Le premier caractère
alphanumérique règle d'un coup `-x` et `--upload-pack=…`, qui serait une **exécution de
commande**. Sont refusés en plus `..` (intervalle de révisions), `refs/` (un autre espace de
noms), un `/` ou un `.lock` final. La signature GPG du tag reste le verrou qui décide quel
**code** s'exécute, mais elle intervient **après** : elle ne couvre pas ça.

#### 🚨 Refspec explicite et forcée, et les deux moitiés comptent

`+<ref>:refs/remotes/origin/<ref>`.

- **explicite** : sans elle, la mise à jour de la ref distante dépend du `remote.origin.fetch` du
  dépôt — vérifié `+refs/heads/*` sur ben-0001, posé par `install.sh`. Un boîtier provisionné
  autrement lirait un plan **périmé**, sans que rien ne le dise ;
- **forcée** : une branche d'essai **se rebase depuis `main`**, c'est l'usage prévu, donc son
  historique est réécrit. Sans le `+`, le fetch refuserait la mise à jour non fast-forward et le
  boîtier continuerait de lire l'**ancien** plan.

⚠️ **Et une garde qu'on croyait utile ne l'est pas.** Le `--` avant la refspec a été éprouvé par
mutation : le banc reste **vert** sans lui, parce que c'est le `+` qui empêche la lecture comme
option. Le commentaire a été corrigé plutôt que de garder une garde invérifiable — `--` est
conservé parce qu'il ne coûte rien, pas parce qu'il protège.

#### 🚨 Au moindre doute, `main` — parce que l'OTA est le seul canal de réparation

Décision prise avec Xavier, **notée dans la PR #43** après avoir été renversée deux fois en revue.
Ce qui tranche :

- **`ota_ref` sert à recevoir une version en avance, et à rien d'autre.** L'usage « retenir un
  boîtier en arrière » n'existe pas — ni dans `ben-docs#16`, ni dans `ben-api#29`. Il avait été
  introduit en revue, et toute une règle avait été bâtie dessus ;
- donc **`main` est toujours le choix prudent** : c'est ce que le reste du parc reçoit de toute
  façon, et une version d'avance manquée se rattrape au tick suivant ;
- 🚨 **surtout, l'OTA est le seul canal de réparation.** Un certificat expiré, une CA renouvelée,
  et le boîtier ne joint plus le cloud. S'il en concluait « je ne sais pas, donc je ne fais rien »,
  il sortirait de l'OTA **pour toujours**, et la seule issue serait d'y aller en SSH. La prudence
  apparente fermait le seul canal qui pouvait le réparer.

```
panne de l'API · 5xx · 403 · DNS · TLS · certificat illisible
corps illisible · nom de branche refusé · branche disparue       →  main, en WARNING
404 sur la route · {"ref": null}                                 →  main, en INFO
```

⚖️ Et chaque cas vérifie **deux** choses dans le banc : la ref rendue **et** le niveau du journal.
Un `main` silencieux serait aussi faux qu'un gel — ces situations ne sont pas normales.

#### ⓘ Une seule exception : le plan de la branche est inutilisable

Si la branche **existe** mais que son `compatibility.yaml` est **illisible ou absent**, on saute le
tick, **en `error`**. Et c'est l'argument de Xavier : **un plan mal formé est typiquement ce qu'une
branche d'essai existe pour attraper**. Replier sur `main` ferait disparaître de l'écran le défaut
qu'on cherchait à voir, et il ne se manifesterait qu'en atteignant tout le parc.

C'est le **seul** endroit qui lève `TickASauter`, et le préflight ③ de l'`update.sh` le vérifie :
une seule levée, et **jamais** dans `ref_demandee`. La règle est encodée dans le contrôle parce
qu'elle a déjà été renversée deux fois.

#### ⭐ La branche sans suite : on crie, puis on bascule

Une branche fusionnée **en squash** qui survit avec son `ota_ref` encore posé : le boîtier atteint
la dernière version qu'elle prévoyait, puis affiche paisiblement « Already up to date » **pour
toujours**, en ratant toutes les releases suivantes de `main`.

La détection ne demande aucun état : la branche n'offre plus rien pour sa version, `main` offre une
transition. Le boîtier le **crie**, puis **bascule** sur `main`. ⓘ C'est possible précisément parce
qu'il n'existe pas de retenue délibérée à protéger — les deux décisions se tiennent.

#### `{"ref": null}` se dit en `INFO`, pas en `WARNING`

`ota_ref` vaut `NULL` pour la quasi-totalité du parc : ce sera donc la réponse **normale** de 8
boîtiers toutes les 10 minutes, soit **1 150 avertissements par jour** pour dire que tout va bien.
Ça noierait celui de `hors_du_plan`, qui signale un boîtier réellement bloqué. Le `WARNING` reste
pour une valeur **présente mais refusée** — là, une intention ne s'applique pas, et il faut le
voir. Deux cas du banc éprouvent les **niveaux**, pas les valeurs.

#### ⚠️ Attendu, et ce n'est pas une panne : rien ne change au tick qui applique cette update

L'agent est un **processus neuf à chaque tick**, et celui qui exécute `update.sh` a chargé son
code **avant** le `checkout` de l'étape ⑥. Le premier appel au cloud a donc lieu au réveil
suivant du timer, dans ~10 min.

ⓘ C'est l'asymétrie exacte qui a fait fermer `#37` sans la faire : un correctif dans l'agent
n'est **jamais** immédiat ; un correctif dans le publisher, si — l'agent le redémarre à ⑩.

#### 🚨 Un banc de préflight ne doit pas lire un message traduit — il brûlait cette version

Trouvé en revue, et **reproduit sur ben-0001**. Deux cas du banc lisaient le texte d'une erreur de
git (`"remote ref"`). Or `git.mo` **français est présent sur l'image du parc** et `install.sh` ne
fixe aucune locale : sur un boîtier en français, git dit *« impossible de trouver la référence
distante »*. Le banc tombait ⇒ le préflight ② échouait ⇒ l'update avortait ⇒ **rejouée toutes les
10 minutes, sur un boîtier parfaitement sain**, et `pi-0.11.0` était **brûlée**.

`fetch_origin` épingle désormais `LC_ALL=C` — mesuré sur la cible : gettext ignore `LANGUAGE`
quand la locale est `C` — et les deux cas tournent **sous `LANGUAGE=fr`** pour le prouver. Rouges
avant, verts après.

ⓘ Le même piège a mordu une deuxième fois, au même endroit : un cas montait son dépôt en
poussant `main` alors que le git du boîtier nomme sa branche par défaut `master`. Le banc du Mac
ne pouvait pas le voir. Un banc de préflight **doit** être exécuté sur la cible avant le tag.

#### 🚨 Le boîtier n'essaie pas de devenir plus malin que le cloud

Une première version de ce volet détectait une branche **promue** par un test d'ascendance
(`merge-base --is-ancestor`), pour rendre la main à `main` si on avait oublié de détacher le
boîtier. **La garde a été retirée**, et pour deux raisons qui disent la même chose :

- **les dépôts BEN ne fusionnent qu'en squash** — vérifié sur l'API GitHub, `allow_merge_commit`
  et `allow_rebase_merge` sont faux. Un squash crée un commit **neuf** : les commits de la branche
  ne sont donc **jamais** ancêtres de `main`, et le test répondait toujours « non ». La garde ne
  pouvait pas se déclencher. ⚠️ Et son banc la croyait bonne parce qu'il fusionnait en `--ff-only`,
  une forme qui **n'arrive jamais** sur ces dépôts : vert à tort ;
- un second argument avait été avancé — « elle passerait outre un ordre légitime de retenue » — et
  il est **tombé depuis** : la retenue délibérée n'existe pas. Il ne reste que la première raison,
  mais elle suffit : **une garde qui ne peut jamais se déclencher est pire qu'absente**, puisqu'on
  la croit active.

⭐ Ce qui la remplace compare le **contenu** des deux plans — ce que la branche offre pour la
version du boîtier, face à ce que `main` offre — donc résiste au squash. Voir « la branche sans
suite » plus haut.

#### 🚨 « Rien à faire » et « je ne peux plus rien faire » ne se disent pas pareil

`hors_du_plan` remplace la garde : si la version installée n'est **ni** le `from` d'une transition
**ni** le `to` d'une, le plan ne l'a jamais entendue et ce boîtier **n'avancera plus jamais**.
L'agent le dit en `warning` au lieu du paisible « Already up to date ».

ⓘ Ce n'est pas une hypothèse : **ben-0005** annonce `0.9.29`, une version absente de toute
transition, et il est hors du parc OTA depuis des semaines sans qu'une seule ligne le dise.

#### Le trou du repli, fermé

Un `compatibility.yaml` **illisible sur la branche** : `yaml.safe_load` lève une `YAMLError`, pas
une `CalledProcessError`, donc l'erreur traversait et le tick échouait **toutes les 10 minutes**. Le
repli attrape maintenant tout ce qui cloche **sur le chemin de la branche** — et seulement là : un
`main` cassé doit lever, c'est une panne réelle qui doit se voir.

#### Le banc, et ce qui a été vu tomber

**33 cas.** **Quinze** montent un **vrai dépôt git** jetable : seul moyen de prouver « lu depuis
`origin/canary` », « une branche **rebasée** est relue à jour » et « une branche fusionnée **en
squash** reste suivie ». Les autres couvrent la validation du nom, le signalement d'une version hors
plan, et tout ce qui vaut `main` — dont un **5xx au corps valide**, seul cas qui vise la garde sur le
statut HTTP. Les dépôts jetables se **nettoient** (`atexit`) : une update qui échoue est rejouée
toutes les 10 minutes, donc ils s'empilaient sur la carte SD.

⚠️ **Et le banc avait cessé de pouvoir tomber sur la locale.** `_ENV_GIT` était une constante de
module, donc figée à l'**import** : le `LANGUAGE=fr` que les deux cas posent ensuite n'atteignait
plus `git`, et ils restaient verts même en retirant `LC_ALL=C`. L'environnement est maintenant
construit **à chaque appel**, et la mutation a été vérifiée **rouge sur ben-0001**.

⚖️ **42 mutations** vérifiées rouges, **9 sabotages** du préflight ③ aussi. ⚠️ **Une** est restée
verte — le `--` avant la refspec, puisque c'est le `+` qui fait barrière : le commentaire a été
corrigé plutôt que de garder une garde invérifiable.

**Aucun redémarrage** (l'agent est un `oneshot` par timer), **aucune migration**, **aucun état
nouveau sur le disque** — la ref n'est pas mémorisée, elle est redemandée à chaque tick. Le
retour arrière vers 0.10.0 ne demande de restaurer **rien**.

### [0.10.0] — 2026-10-06

**La version installée atteint enfin le cloud — par une condition, plus par un événement.**
Chantier [`ben-docs#15`](https://github.com/xdegenne/ben-docs/issues/15), sous-tâche
[#38](https://github.com/xdegenne/ben-firmware/issues/38).

Mesuré le 04/10, après la publication de `pi-0.9.28` : `devices.sw_version` annonçait
**`0.9.27` pour les 8 boîtiers** alors que ben-0001 et ben-0003 tournaient bel et bien en
`0.9.28` — `device.json` bumpé, dépôt sur le tag, « ✓ update OK » au journal, battement à
l'heure. Or cette colonne est la **seule** source de la vue parc : le panneau « Versions
firmware » du tableau `ben-parc` et la colonne « version » de son détail. Un tableau de bord
qui annonce une version périmée rend le parc **impossible à piloter** — on ne sait plus qui a
pris quoi, ce qui est exactement ce que l'ingestion cloud existe pour dire.

```
ben_publisher   la version déclarée est MÉMORISÉE après le 2xx, et comparée à chaque tour
                /var/lib/ben-firmware/version-declaree.json   { "version", "ts" }
                le drapeau d'OTA est RETIRÉ : pose, lecture, et sa garde d'ordre
```

#### La cause, en une ligne

`devices.sw_version` n'est écrite que par la route `/hello` — la **déclaration** — et par rien
d'autre. Le battement, lui, va sur `/ping`, qui écrit `last_seen` et la santé et ne touche
jamais la version. Et la déclaration post-OTA reposait sur un **drapeau que chaque `update.sh`
devait penser à poser** : **un seul des 37 scripts** du dépôt l'a fait — `0.9.26_to_0.9.27`,
celui qui a introduit le mécanisme — et il a été **oublié dès la transition suivante**.

#### ⭐ Le dépôt portait déjà l'argument, deux fois

C'est ce qui rend le correctif évident après coup. `ben_publisher.py`, sur son déclencheur ② :

> ⭐ ② est une CONDITION, pas un événement à attraper […] Réconcilier un état est plus solide
> que rattraper un événement — un événement raté est définitif, une condition se re-vérifie au
> tour suivant.

Et `check_update.py`, sur le redémarrage du publisher : « *Le laisser à la charge de chaque
`update.sh` reviendrait à l'oublier un jour.* » La déclaration n'avait pas eu droit au même
traitement.

#### ⭐ Mémoire absente = jamais déclarée, et c'est ce qui recale tout le parc

Aucun boîtier ne porte ce fichier aujourd'hui, donc **chacun déclare une fois** au premier
battement après son OTA. Le trou du 04/10 se ferme sans qu'on touche à un boîtier, et sans
rien de manuel.

Ce que la condition couvre, et que le drapeau ne couvrait pas :

- une OTA ;
- un **retour arrière** de version — c'est une **égalité** qu'on teste, pas un ordre ;
- un `device.json` édité à la main ;
- une déclaration **refusée** (4xx/5xx, réseau) : re-tentée au tour suivant, sans mémoire
  d'événement à conserver ;
- un publisher redémarré au mauvais moment.

ⓘ Ce qu'elle ne couvre **pas** : une **restauration de la base cloud** à un état antérieur — le
boîtier croirait avoir déjà déclaré. Seule une variante où le cloud rendrait sa version dans la
réponse du `/ping` fermerait ce cas ; hors périmètre, noté dans `ben-docs#15`.

#### ⓘ Un fichier, pas une table — c'est le mode de défaillance qui tranche

Une table aurait demandé un **DDL dans `update.sh`** : `open_db()` du publisher ouvre en
écriture **sans rejouer le schéma** (délibéré) et l'API locale est en lecture seule, donc aucun
des deux ne peut la créer. Oubliée, elle aurait fait lever `no such table` à chaque tour et le
boîtier aurait cessé de publier **en silence** — la classe de panne que 0.9.27 a failli livrer.
Un fichier absent ou illisible, lui, vaut « jamais déclarée » : une déclaration de trop, bornée
par le plancher.

Hors de `measurements.db` **aussi** parce qu'elle se **reconstruit** (0.9.25) ; et si la mémoire
ne survit pas, on redéclare. Elle vit là où vivait le drapeau — `/var/lib/ben-firmware`,
propriété de `ben` — donc **aucun état nouveau à provisionner**.

⇒ **Aucune migration, aucune table, aucune colonne**, et le retour arrière vers 0.9.28 ne
demande de restaurer **rien**.

#### 🚨 Le plancher s'applique aussi à la version — mais pas le plancher long

Un cloud qui refuse laisse la condition **vraie** : c'est sa force, et c'est aussi pourquoi le
plancher reste. Sans lui, le boîtier redéclarerait toutes les 10 s quand le retard est gros —
la rafale déjà constatée avec le drapeau.

En revanche le plancher **long** (6 h, « plus rien à apprendre » quand tous les pdl sans ref
portent déjà un motif) ne vaut **que si la version est à jour** : six heures de vue parc fausse
à cause d'un ADS non conforme serait un défaut pour un autre.

#### Le drapeau est retiré, pose et consommation

`_version()` disparaît avec son unique appelant — la garde d'ordre pose/bump, qui n'a plus
d'objet puisqu'on compare désormais à `device.json` lui-même. Le publisher **supprime sans
jamais le lire** un drapeau qu'un boîtier porterait encore : l'interpréter reviendrait à garder
les deux mécanismes, donc à garder celui qu'on retire.

#### 🚨 Aucun redémarrage dans le script

Le seul service concerné est `ben-publisher`, et c'est l'**agent** qui le redémarre à l'étape ⑩,
**après** le bump ⑨. Le faire dans `update.sh` le relancerait sur un `device.json` encore en
0.9.28, qui déclarerait 0.9.28.

ⓘ Ce serait sans dommage **durable**, et c'est une propriété de la condition, pas une chance :
au redémarrage de ⑩ la mémoire (0.9.28) différerait de l'installée (0.10.0), donc le boîtier
redéclarerait. Là où le drapeau exigeait une garde d'ordre explicite pour survivre à cette
course, la condition s'en passe.

#### Le banc, et les sabotages qu'on a vérifiés rouges

**15 cas**, aucun matériel, aucune base : les quatre états de la mémoire (absente ⇒ **une**
déclaration · égale ⇒ **aucune** · différente ⇒ **une** · refus du cloud ⇒ re-tentée au tour
suivant **sans rafale**), le retour arrière, la version installée vide, les deux planchers, et
la mémoire sur disque (aller-retour, atomicité, fichier illisible, répertoire non inscriptible).

⚖️ Les témoins **négatifs** sont la moitié du banc : une condition toujours vraie fermerait le
défaut d'origine *et* ferait redéclarer sept boîtiers toutes les 60 s pour toujours. **10
mutations** du correctif vérifiées rouges, chacune par le cas qui la vise.

🚨 Et le préflight ③ prouve que le correctif est **branché**, **sur l'arbre syntaxique, jamais
par un `grep`** : `ben_publisher.py` nomme `memoriser_version_declaree`, `DECLARER_FLAG` et le
chemin du drapeau dans ses **commentaires**, donc un `grep` serait **vert** sur un fichier qui
ne les appelle jamais, et **rouge** sur la suppression qu'on vient de faire. **8 sabotages**
vérifiés rouges, dont « les commentaires seuls » et « affectation retirée mais nom encore
employé » — ce dernier lèverait un `NameError` au premier tour, et le publisher ne démarrerait
plus du tout.

Contrôle d'effet sur `/health` (`db: true`), jamais `/info`. Il ne prouve **que** l'innocuité du
script : la déclaration part après ⑩, quand le script n'existe plus. On ne l'**exige** donc pas
— un garde-fou impossible à tenir brûle une version (pi-0.9.12) — et ce qu'il faut regarder
ensuite est écrit nommément en fin de journal.

#### ⚠️ Pourquoi 0.10.0, et le piège de l'ordre lexicographique

Le mécanisme de la déclaration change de nature et un état local apparaît : ce n'est pas un
correctif de détail. ⚠️ Mais `« 0.10.0 » < « 0.9.28 »` **comme chaînes**. Sans conséquence sur
l'OTA — `find_next_transition` compare `from` par **égalité**, jamais par ordre — mais le
panneau « Versions firmware » de `ben-parc` trie en texte (`ORDER BY 1 DESC`), donc 0.10.0
s'affichera **sous** 0.9.28.

### [0.9.28] — 2026-10-04

**Le boîtier apprend la version de son émetteur, et la dit.** Volet ② du chantier
[`ben-docs#12`](https://github.com/xdegenne/ben-docs/issues/12), sous-tâche
[#28](https://github.com/xdegenne/ben-firmware/issues/28).

L'émetteur Arduino se livre par **reflash PHYSIQUE** — il n'y a pas d'OTA sur AVR. Sa version
n'était donc lisible qu'à son banner série, **un FTDI en main, devant le boîtier**. Depuis
`tic-reader` 0.1.10 il l'annonce dans sa trame de boot (TLV `T_FW`, trois octets) ; cette
version la décode, la range et la fait monter.

```
frame_codec     T_FW = 0x07 → "0.1.11", nommé « FW », déclaré STOCKÉ
emitter         + colonne fw_version (NULLABLE, sans DEFAULT)
ben-telemetry   décode le TLV à la trame de boot et range la valeur
health          la fait monter dans la ligne de SON émetteur → health.emitter[].fw
```

#### 🚨 Une ligne par ÉMETTEUR, pas un champ de boîtier

La version appartient au **satellite**, donc au compteur qu'il lit — et un boîtier peut en
écouter **plusieurs**. Un champ unique à côté de `sw` aurait été faux dès le second émetteur, et
faux **en silence** : le dernier boot reçu aurait écrasé l'autre.

⭐ `health.store()` émettait **déjà** une ligne par émetteur (`addr`/`adco`/`pdl`/`ts`). Le
chantier se réduit donc à un champ dans une structure qui existe.

#### ⭐ Pourquoi le champ voyage DANS `health` et pas au niveau du `/ping`

`ben-api` décode le battement avec `DisallowUnknownFields()`. Une clé **neuve** au niveau du
`/ping` ferait **400 sur chaque battement du parc** jusqu'au déploiement du volet ③. Seul
`health` est un `json.RawMessage`, transmis tel quel jusqu'à une colonne `jsonb` : c'est le point
d'extension prévu, et c'est ce qui permet à ce volet de **partir seul**, sans une ligne de
`ben-api`.

⚠️ Mesuré sur ben-0001, journal du 02/10 : `HTTP 400 {"error":"bad_body","message":"json
invalide: json: unknown field \"points\""}`, **59 échecs consécutifs** pendant la bascule de
0.9.27. Le mécanisme n'est pas théorique.

#### 🚨 Trois états, et le troisième n'est pas « inconnu »

| valeur | sens |
|---|---|
| `0.1.11` | annoncée par l'émetteur — fait **mesuré** |
| `anterieur-campagne` | trame de boot reçue **sans** le TLV ⇒ émetteur < 0.1.10, **à reflasher** |
| `NULL` | **aucune trame de boot vue** |

L'absence du TLV est une **information**, pas un trou — et l'inférence est sûre pour une raison
structurelle : `T_FW` est, avec `T_ADCO`, le seul TLV **inconditionnel** de la trame de boot, et
il est écrit **avant** tout champ issu de la TIC. Son absence ne peut donc pas vouloir dire « la
TIC n'était pas encore lue », contrairement à celle de `CONTRAT`, d'`ISOUSC` ou de `PREF`.

#### ⚠️ Ce qu'il faut attendre, et qui n'est pas une panne

`fw_version` reste **`NULL` sur tout le parc** jusqu'au prochain **boot** de chaque émetteur :
il ne redémarre pas quand le Pi redémarre, il reste en `STREAMING` et ne rejoue pas sa trame de
boot. Pour le remplir, couper l'alim d'un émetteur — ou attendre une coupure du site.

#### ⭐ Deux valeurs qui mentaient, et qui cessent de monter

Relevées sur ben-0001 le 04/10, pour **un** émetteur qui tourne en **0.1.8** :

```
"arduinoFirmwareVersion": "0.0.6"        ← pas même la lignée de numéros de l'émetteur
"lora-tic-receiver": { "fw": "0.1.2" }   ← et pas 0.1.3, que le code sème au provisioning
```

Trois valeurs, trois faussetés différentes. Une constante **globale** livrée par OTA ne pouvait
pas dire un état de reflash qui est **par émetteur**. `caps_for_model` ne les écrit plus, et
`health.versions()` les retire **à l'émission** — ce qui vaut pour les `device.json` déjà posés,
**sans réécrire le fichier** (l'agent d'OTA le réécrit, et le toucher depuis un `update.sh` a
déjà coûté des tours de boucle).

#### La migration : ajout seul, nullable, et éprouvée sur une base réelle

`ALTER TABLE emitter ADD COLUMN fw_version TEXT` — **métadonnée seule** en SQLite : la ligne de
schéma est réécrite, aucune page de données ne l'est.

⚠️ Un `NOT NULL DEFAULT 'anterieur-campagne'` aurait été faux **deux fois** : il aurait fabriqué
un fait d'apparence mesurée pour les émetteurs des 7 boîtiers sans qu'une seule trame de boot
arrive, et détruit la distinction `NULL` / « antérieur » dont tout dépend. La nullabilité
**porte le sens**.

⭐ **Éprouvée sur la base réelle de ben-0001 le 04/10** — 535 Mo, lecteur en marche :

```
db.connect() avec l'ALTER ......  62 ms      db.connect() suivant ....  34 ms  ⇒ ALTER ~28 ms
user_version ..................  1, INCHANGÉ            ⇒ le backfill one-shot n'a PAS rejoué
ligne émetteur ................ (31, '031864467282', 0, 1791066099, None) — intacte
last_tic_ts ................... 1791097096 → 1791097178 ⇒ le lecteur a enregistré PENDANT
ben-publisher (code 0.9.27) ... a publié 86 points APRÈS l'ajout, « reste ~0 »
```

🚨 La dernière ligne est **la garantie de retour arrière, mesurée et non plaidée** : l'ancien code
tourne sur le nouveau schéma. ⓘ Et donc **rien à sauvegarder** — une sauvegarde de 535 Mo
tiendrait un verrou de lecture pendant toute la copie sur un Pi Zero mono-cœur, donc coûterait
des trames, pour assurer contre un risque qu'un `ADD COLUMN` nullable ne porte pas.

#### 🚨 Redémarrages

`ben-telemetry` — **obligatoire**, et c'est le seul que cette update paie : le décodeur vit là, et
**l'agent d'OTA ne redémarre que `ben-publisher`**. L'omettre livrerait un chantier **inerte**
sans qu'aucun contrôle s'en plaigne. Puis `ben-local-api`, après la migration.

**Jamais `ben-radio`**, ni via `capabilities.py restart lora-tic-receiver` qui mappe la capability
sur deux services et l'emporterait : elle est seule maîtresse du RFM95, et le verrou taint de
0.9.12 est né d'un redémarrage de trop. `ben-tic-reader` n'est pas touché — sur un boîtier
filaire ce chantier est un no-op.

#### Le banc, et ses deux témoins symétriques

`src/pi/ben-telemetry/test_fw_emetteur.py` — **13 cas, aucun matériel**, et le préflight de
l'`update.sh` l'**exécute sur le Python du boîtier**.

⚖️ Le témoin qui **protège le parc** vient en premier, parce qu'aucun émetteur du parc n'émet
encore ce TLV : un contrôle trop strict ferait cesser l'enregistrement de **tous** les boîtiers
radio d'un coup. Une trame **sans** `0x07` doit produire exactement ce qu'elle produisait.
⚖️ Le témoin **inverse** va jusqu'au hello : rangée mais non remontée, la version n'aurait rien
résolu.

### [0.9.27] — 2026-10-02

**Le boîtier publie sous la référence que le cloud lui rend.** Ferme
[#32](https://github.com/xdegenne/ben-firmware/issues/32) (chantier `ben-docs#5`).

`pdl_index` est un entier **LOCAL** au boîtier : il numérote les compteurs dans *sa* base, dans
l'ordre où il les a vus. Il ne veut rien dire ailleurs, et il cesse de monter. À sa place, le
boîtier **RANGE et RENVOIE** une référence opaque que le cloud lui rend — sans rien en
comprendre. C'est désormais le seul terme commun entre l'app, le boîtier et le cloud ; l'ADS, lui,
ne monte qu'à la déclaration.

#### Le protocole se scinde en trois routes, là où `/hello` faisait tout

```
POST …/hello          RARE, événementiel
   ↑ sw · fw · model · pdl: [ {index, adco} ]
   ↓ 200  refs: [ {pdl, ref} ]        (ou {pdl, motif} si l'ADS n'est pas conforme)

POST …/ping           FRÉQUENT — c'est l'ancien /hello renommé
   ↑ instantané de santé · contract_epoch · tariff_labels · meter_profile, clavés par ref
   ↓ 204, rien

POST …/measurements
   ↑ { lots: [ {ref, points: [...]} ] }     groupé par compteur, SANS pdl
```

⭐ **Une `ref` par LOT, jamais par point** : un identifiant répété 1000 fois coûte 36 ko, un par
compteur en coûte 36. Et le multi-compteurs cesse d'être un cas particulier — c'est la forme
normale du payload.

**Déclencheurs de la déclaration** : ① à l'init du boîtier, ② tant qu'un pdl est **sans ref**,
③ après une OTA. 🚨 **Jamais au simple démarrage du publisher** — un service qui redémarre n'est
pas un événement, et `/hello` est redevenu rare.

⭐ Le déclencheur ② est une **CONDITION**, pas un événement : elle se re-vérifie à chaque tour,
sans drapeau et sans rien mémoriser de ce que le cloud sait. Elle couvre d'un seul énoncé le
compteur neuf, le compteur remplacé et la ref perdue en local (carte reflashée, désappairage).
**Réconcilier un état est plus solide que rattraper un événement** : un événement raté est
définitif, une condition se re-vérifie au tour suivant.

#### 🚨 La jointure sur `pdl` n'est pas un confort, elle est le filtre

Un compteur sans `ref` n'est pas publiable. Ses points doivent rester `sent = 0` — et surtout
leur `rowid` ne doit **JAMAIS** entrer dans la liste rendue par `fetch_batch` : `mark_sent` les
passerait à 1 après le 2xx du lot des **autres** compteurs, et la mesure serait perdue **POUR
TOUJOURS**, sans trace. C'est le seul défaut de ce chantier qu'on ne pourrait pas rattraper, et
c'est pour ça qu'il est filtré en SQL — où la `LIMIT` porte alors sur les points *publiables*,
là où filtrer après aurait rendu des lots plus petits que demandés sans que rien ne le dise.

#### 🚨 C'est `update.sh` qui applique la migration, et qui la vérifie

Une colonne `pdl.ref`, **ajout seul** — l'ancien code tourne sur la nouvelle base, donc le retour
arrière ne demande de restaurer **rien**.

Mais personne sur le boîtier ne peut la créer au bon moment : le publisher ouvre la base en
écriture **sans rejouer le schéma** (`open_db`, délibéré) et l'API locale en **lecture seule**.
Sans la colonne, `SELECT_BATCH` lève `no such column: p.ref` à chaque tour et le boîtier cesse de
publier **en silence** — service « active », journaux presque calmes. Parier sur un redémarrage
de lecteur aurait laissé muet **pour toujours** le boîtier dont le lecteur ne tourne pas (câble
TIC débranché, émetteur muet) — exactement celui qu'un pari aurait abandonné.

#### 🚨 La déclaration post-OTA passe par un drapeau, parce que le séquencement l'impose

`check_update.py` bumpe `device.json` à l'étape ⑨, **après** `update.sh` (⑧), puis redémarre le
publisher (⑩). Une déclaration émise depuis le script aurait donc annoncé l'**ancienne** version.

⇒ `update.sh` pose `/var/lib/ben-firmware/declaration-requise.json`, portant
`version_attendue: "0.9.27"`. Le publisher ne le consomme **que si** `device.json.softwareVersion`
lui est égal, et ne l'efface **qu'après un 2xx**. Sans cette garde, il resterait la fenêtre de
quelques secondes entre la pose (⑧) et le bump (⑨).

⭐ Pour **cette** transition le drapeau est une ceinture : aucun pdl n'a de `ref` (la colonne vient
de naître), donc le déclencheur ② suffit seul. Il devient le mécanisme unique dès 0.9.28, quand
les refs seront déjà connues — c'est pour ça qu'il naît ici, éprouvé par un cas où il ne risque
rien.

#### Ce que l'update ne redémarre pas, et pourquoi

| unité | geste | motif |
|---|---|---|
| `ben-local-api` | **restart**, si elle tournait, **après** la migration | dernier consommateur de `db.py` ; coûte ni mesure, ni GPIO, ni radio. Elle ouvre en lecture seule : jamais avant la migration |
| `ben-publisher` | **non** | l'agent le fait lui-même à l'étape ⑩, après le bump ; le faire ici le relancerait sur un `device.json` encore à 0.9.26 |
| `ben-tic-reader`, `ben-telemetry` | **non** | changement de `db.py` strictement additif, aucun lecteur n'appelle les nouvelles fonctions, les 4 accès à `pdl` du dépôt nomment tous leurs colonnes (pas de `SELECT *`). Un restart de lecteur **coûte des mesures** (leçons 0.9.17 et 0.9.21) |
| `ben-radio` | **non**, moins que tout | `capabilities` mappe `lora-tic-receiver` sur **deux** services ; elle est seule maîtresse du RFM95 et le verrou taint de 0.9.12 est né d'un redémarrage de trop. Elle n'importe même pas `db.py` |

#### Le préflight, et ses quatre sabotages vérifiés rouges

L'`ALTER` est éprouvé sur une base fabriquée **à l'ancienne forme** — `db.connect(":memory:")` ne
prouverait rien, une base neuve naissant déjà avec la colonne. Puis **deux témoins positifs** :
poser une ref et la relire, et les points retenus qui **repartent** dès que la ref arrive. Sans
eux, un `refs_connues` toujours vide et un `fetch_batch` sans lot passeraient tous les cas de
refus (leçon des préflights 0.9.19 et 0.9.26).

Les quatre sabotages ont été passés et attrapés : `ALTER` retiré, `refs_connues` vide,
`fetch_batch` muet, et filtre `p.ref` retiré.

#### ⚠️ Le parc est muet entre la bascule de l'API et cette OTA

L'API refuse l'ancien format. Donc **avant** cette update, un boîtier qui ne publie pas n'est pas
une anomalie — c'est l'état attendu ; il en devient une **après**. Rien n'est perdu : rétention
locale de 180 jours, et `sent = 1` n'est posé, par `rowid`, qu'après un 2xx.

Le script relève le retard de l'outbox **avant** d'agir et l'écrit au journal, comme point de
comparaison — encadré par les `rowid`, jamais par `count(*) WHERE sent = 0`, qui prend **37 s sur
un Pi Zero**. Il n'exige **rien** de la publication : elle n'est observable qu'après sa sortie, et
un contrôle qu'on ne peut pas tenir brûle une version (pi-0.9.12 et son `/info`). Le contrôle
d'effet porte sur `/health` (`db: true`, et `last_tic_ts` qui avance si le boîtier lisait avant) —
ce qu'il prouve inoffensif est un `ALTER TABLE` exécuté **sous un lecteur en marche**.

---

### [0.9.26] — 2026-10-02

**Un octet hors alphabet TIC condamne son groupe.** Ferme [#10](https://github.com/xdegenne/ben-firmware/issues/10).

`Enedis-NOI-CPT_54E` §6.2.1.2 : le champ « donnée » ne contient que des caractères **ASCII
imprimables, 0x20 à 0x7E** — plus `HT (0x09)`, séparateur de champ du mode standard (§5.3.6).
L'alphabet légal est donc **connu d'avance**, et tout octet hors de cet ensemble est une erreur
**par définition de la norme**, pas une heuristique. `read_frame` ne s'en servait pas.

#### 🚨 Le défaut est une SUBSTITUTION, pas une amputation

L'octet n'était pas jeté, il était **ajouté** : le groupe gardait sa longueur, un de ses
caractères était **remplacé**. Or c'est là que le checksum est aveugle — il vaut
`(somme & 0x3F) + 0x20`, donc il ne voit la somme que **modulo 64**. Remplacer un caractère par
`c - 0x40` retire exactement 64 → **checksum identique** — et donne un caractère de contrôle :

```
'T' = 0x54 → 0x14        'S' = 0x53 → 0x13        'I' = 0x49 → HT
```

En historique, `PTEC TH..` devenait `PTEC \x14H..`, **accepté**. Et en historique c'est `PTEC`
qui donne l'`index_id`.

⚠️ **Il faut DEUX bits, pas un.** Un seul bit retourné casse **toujours** la parité : le compteur
a calculé le bit de parité sur l'octet d'origine, donc `octet_valide` l'arrête et ce contrôle ne
le voit jamais. Ce qui l'atteint est un nombre **pair** de bits dans le même octet — typiquement
le bit 6 de la donnée **et** le bit de parité. C'est donc une **coïncidence rare** qu'on ferme, le
même arbitrage que celui déjà écrit dans `tic_parite.octet_valide`.

⇒ **Trois filtres indépendants**, chacun couvrant l'angle mort des deux autres.

#### Trois défauts trouvés en revue

Chacun avec son banc écrit **avant** le correctif et vérifié rouge.

1. l'explication « un seul bit » ci-dessus — fausse, et elle gonflait la menace ;
2. `groupes_alphabet` **sous-comptait** : il ne montait qu'à la sortie sur CR, donc un groupe
   abîmé dont le CR est perdu était compté *rejeté* mais **plus imputé à sa cause**. Les quatre
   sorties imputent désormais ;
3. 🚨 **le pire, parce qu'il faisait accuser le compteur** : le test vivait dans `elif in_line:`,
   donc un octet hors alphabet arrivant **hors** d'un groupe disparaissait avec tout le groupe
   suivant. Deux bits du LF suffisent — `0x0A` devient `0x09`, soit `HT`, parité préservée et
   illégal en historique — après quoi `_cause_rejets` concluait « cette étiquette n'est pas
   émise ? ». Le chemin de la **parité** traitait déjà ce cas ; l'alphabet ouvre désormais le
   groupe lui-même, condamné d'avance, pour qu'il soit **compté**.

⚖️ Et la frontière tient dans les deux sens : le même octet **avant le STX** ne coûte rien, donc
le cas du triphasé reste intact.

#### 🚨 Le coût, mesuré sur Pi Zero W

armv6l, `nice -19`, par octet et en part de CPU :

| | µs/octet | % CPU à 1200 bd | % CPU à 9600 bd |
|---|---|---|---|
| appel de fonction `not f(b)` | 10,73 | 0,129 % | 1,030 % |
| **table `not T[b]`** | **2,97** | **0,036 %** | **0,285 %** |
| témoin : appel à vide | 3,02 | — | — |
| parité existante `f(b)` | 11,56 | 0,139 % | 1,110 % |

Un appel de fonction Python coûte **à lui seul 3 µs** sur cette machine, soit la quasi-totalité du
coût du prédicat. D'où une **table de 128 entrées**, ×3,6 moins chère et au prix d'une instruction
vide — l'issue exigeait que le contrôle « ne coûte RIEN sur une ligne saine ».

🚨 **128 entrées et pas 256** : `read_frame` indexe avec l'octet **déjà masqué**, après que la
parité a jugé le huitième bit. Les tables sont **dérivées des prédicats**, jamais réécrites, et le
banc le vérifie sur les 128 valeurs — comme pour `PARITE`.

#### Le relevé gagne sa troisième cause

`alphabet` (octets) et `groupes_alphabet` (groupes) ; `_cause_rejets` **nomme** la cause, et les
deux quand les deux ont mordu. Fondre les causes dirait « ça décroche » sans dire **où** : un octet
hors parité accuse le **bruit** du fil, un octet hors alphabet une corruption que la parité **ne
pouvait pas** voir, un checksum faux sur un groupe intégralement lu autre chose encore.

ⓘ Pour [#5] : les colonnes seraient `groupes_ko_alphabet` à côté de `groupes_ko_parite` et
`groupes_ko_checksum`.

#### Deux décisions de structure

Le code vit dans **`tic_parite.py`**, pas dans un fichier neuf : un fichier neuf doit être copié
par l'`update.sh` qui le livre, et s'il est oublié le lecteur meurt sur `ImportError` — en boucle,
puisque systemd le relance. C'est ce qui a brûlé **pi-0.9.0** (paho oublié).

La table s'injecte par `MODES`, comme `checksum_ok` et `parse_label` : `read_frame` reste
mode-agnostique. Le défaut du paramètre vaut l'**historique**, donc le plus strict — oublier
d'injecter la table standard rend le lecteur **muet**, panne bruyante que le banc attrape, là où le
défaut inverse rouvrirait le trou **en silence**.

#### Éprouvé

29/29 sur `read_frame`, 9/9 sur les contrôles d'octet, **14 bancs du dépôt verts**. Témoin tenu sur
**ben-0003** (filaire, `pi-0.9.25`) : code posé à la main, lecteur redémarré, `last_tic_ts` qui
avance — puis dépôt **rétabli**, l'agent OTA faisant un `git checkout` nu.

L'update ne touche que `ben-tic-reader`, et seulement si le boîtier déclare `tic-uart` : la chaîne
LoRa ne passe pas par `read_frame`, donc ni `ben-telemetry` ni `ben-radio`. Aucune migration,
aucune table, aucune colonne.

---

### [0.9.25] — 2026-10-01

**On répare la base corrompue en la recopiant.** Ferme [#23](https://github.com/xdegenne/ben-firmware/issues/23).

`pi-0.9.24` a livré le hello d'escalade, et il a répondu **vingt secondes après l'OTA** :

```
[15:41:07][WARNING] échec n°1 (database disk image is malformed) — nouvelle tentative dans 1 s
[15:41:27][ERROR]   échec n°5 (database disk image is malformed) — nouvelle tentative dans 13 s
```

Ce n'était ni le réseau, ni la taille des lots, ni le serveur : une zone de la table
`measurements` est **illisible**, et `fetch_batch` lève **avant** d'atteindre `cli.post`. Le
serveur n'a jamais rien eu à refuser. 61 lignes `malformed` sur deux instantanés, échec n°57
du processus précédent. Neuf jours.

#### ⭐ Ce qui marche encore dit où est le dommage

| ce qui marche | pourquoi |
|---|---|
| l'écriture — **8 322 lignes en 3 h 12, à 0,2 % du débit nominal** | ajouter écrit dans des pages **neuves**, au bout de l'arbre |
| le hello | il lit `pdl`, `contract_epoch`, `tariff_labels` — intactes |
| `pending`, `unsent`, `pdl.last_ts` | servis par des **index**, qui sont des arbres séparés |
| `radio` (lit `rssi`/`snr`, non couverts par l'index) | accède aux pages de table **récentes**, et réussit |
| **ce qui échoue** | `fetch_batch` : le seul à lire 11 colonnes **et** à partir du rowid non envoyé le plus ancien |

Les écritures réussissant, la page corrompue n'est **pas** sur le chemin menant au bout de
l'arbre : le dommage est dans la partie **ancienne**. Le publisher marche droit dessus à chaque
tentative depuis le 22/09.

ⓘ Cause probable : l'usure de la carte SD — ~64 000 lignes écrites par jour, 24 h sur 24,
depuis le 1er août. **Aucun logiciel ne répare ça** ; le changement de carte reste nécessaire.
Seul un boîtier est touché.

#### ① La garde : une erreur locale n'est plus une panne du serveur *(tout le parc)*

`fetch_batch` levait `sqlite3.DatabaseError`, c'était attrapé par le `except Exception` de la
boucle, compté dans `echecs`, et le backoff **serveur** poussé à 300 s. Pendant neuf jours, ce
boîtier a accusé le serveur d'un défaut de son disque.

⭐ Le dépôt avait déjà le précédent et ne l'appliquait pas là : `cadence_sure()` garde
`pending_approx` avec exactement ce commentaire — *« une base locale qui bronche n'est pas un
serveur en panne »*. `fetch_batch` n'avait aucune garde.

La branche est **avant** le `except Exception` (sinon elle ne serait jamais atteinte), elle a
son **propre compteur**, elle se rendort sur `PERIOD` et jamais sur `PERIOD_RETARD` — ne pas
savoir **lire** ne doit pas faire accélérer — et elle **signale** quand même.

#### ② La sonde canari : deux lignes complètes *(tout le parc)*

Rien ne signalait la panne parce que **tous** les champs de l'instantané qui touchent
`measurements` sont servis par un index. Les index étaient intacts.

```sql
SELECT <les 13 colonnes> FROM measurements WHERE sent = 0 ORDER BY rowid LIMIT 1;  -- 0,56 ms
SELECT <les 13 colonnes> FROM measurements ORDER BY rowid DESC LIMIT 1;            -- 0,31 ms
```

ⓘ Le plan de la seconde annonce `SCAN measurements`, ce qui serait normalement alarmant :
`ORDER BY rowid DESC LIMIT 1` descend directement à la feuille la plus à droite.

- ⚠️ **Ce n'est pas un détecteur complet**, et il faut le dire : un dommage au milieu des lignes
  déjà envoyées n'est lu par personne, donc invisible. C'est une alerte précoce sur le chemin
  de lecture du **publisher**, pas un `integrity_check` déguisé.
- ⚠️ Le champ est émis **même quand tout va bien** (~30 o) : un champ qui n'apparaît qu'en cas
  de panne est un champ qu'on oublie, et dont l'absence devient indiscernable du succès.

#### ③ La reconstruction *(un seul boîtier)*

On ne répare pas le fichier : **on en construit un sain à côté**, en recopiant ce qui est
lisible par tranches de `rowid`, avec **dichotomie** sur les tranches qui lèvent. On ne perd
ainsi que ce qui est réellement détruit.

**Écarté, et pourquoi :**

| | |
|---|---|
| `VACUUM INTO` | relit **toutes** les pages pour reconstruire → avorte sur le dommage |
| `Connection.backup()` | 🚨 copie page par page **sous** les arbres : recopierait la corruption à l'identique. Le piège est qu'il **réussirait** |
| `sqlite3 .recover` | l'outil fait pour ça — mais le binaire est **absent** des boîtiers (vérifié sur les deux modèles) |
| `DELETE` / `UPDATE` | récrit les pages corrompues : on aggrave ce qu'on contourne |

**La bascule** — lien **dur** puis `os.replace`, et l'ordre est le fond. Renommer l'original
puis mettre la neuve ferait **deux** renommages, et entre les deux il n'existe aucun
`measurements.db` : une mort à cet instant ferait recréer une base **vide** au redémarrage. Le
lien dur préexiste, il ne reste qu'un geste, et `os.replace` est atomique. L'original est
conservé en `.corrupt-<horodatage>` et **jamais** supprimé.

**Les services sont dérivés des capabilities**, pas d'une liste en dur : sur un boîtier Radio,
`ben-tic-reader` n'a rien à faire là — il y est `dead` avec **172 redémarrages**, précisément
parce qu'il est du modèle filaire. Et on ne **redémarre** que ce qui **tournait**.

⭐ `ben-certd` et `wifi-watchdog` ne sont **pas** arrêtés : mesuré en lisant `/proc/<pid>/fd`,
seules trois unités ouvrent la base (`ben-telemetry`, `ben-publisher`, `ben-local-api`). Couper
`wifi-watchdog` vingt minutes sur une machine injoignable serait un risque sans contrepartie.
`ben-radio` s'arrête en **dernier** (il possède le GPIO), et c'est **explicite** — pas émergent
de l'ordre des clés de `device.json`.

#### Les quatre garde-fous

1. 🚨 **Ciblage par `device.json` — exception assumée.** Un garde-fou d'identité est le seul qui
   ne puisse pas se tromper, et un faux positif voudrait dire **arrêter les services d'un
   boîtier de terrain sain**. Il échoue du bon côté : `device.json` illisible ⇒ on ne fait rien.
2. 🚨 **Idempotente.** La garde de symptôme la donne presque entièrement — après réparation,
   `--refus` lit une base saine et sort **sans rien arrêter**. Plus un **frein** de 3
   tentatives, incrémenté **avant** le travail pour qu'un SIGKILL compte aussi.
3. 🚨 **Un filet qui survit à SIGKILL** : un timer `systemd-run` transient, armé **avant** tout
   arrêt, annulé au succès. Le `trap` de bash ne peut rien contre SIGKILL.
   ⓘ Aucun risque de timeout par ailleurs, c'est mesuré : `check_update.py` appelle
   `subprocess.run(["bash", script])` **sans** `timeout=`, et `ben-update.service` étant
   `Type=oneshot`, systemd lui donne `TimeoutStartUSec=infinity` — le défaut global est
   1 min 30, donc une unité ordinaire aurait été tuée à 90 s.
4. 🚨 **Aucun état de la donnée ne fait échouer l'update.**

⭐ Et la propriété qui rend tout ceci tenable : **jusqu'au `os.replace` final, rien n'est en
jeu.** Une coupure de courant à n'importe quel moment de la recopie laisse l'original tel quel.

#### Le hello est déclenché explicitement

À la fin, avec le rapport de reconstruction dans `health.rebuild`. On ne se contente pas de
celui que `check_update.py` provoque par effet de bord en redémarrant le publisher.

#### Le banc, et ce que ses échecs ont appris

Nouveau `test_db_rebuild.py`, **14 cas**, dont un qui fabrique une **vraie corruption de page**
et **exige** que SQLite lève. Trois montages ont été nécessaires, et chaque échec était
instructif :

- un `count(*)` se satisfait du plus **petit index** sans toucher une seule page de table — le
  garde-fou du banc était aveugle à ce qu'il devait garantir ;
- un `UPDATE` pour poser la frontière `sent = 0` **retombe** sur la zone abîmée ;
- SQLite remplit d'abord la dernière feuille **existante**, donc la ligne charnière vit
  **avant** `page_count`, pas après.

⭐ Le garde-fou du banc a refusé de passer les trois fois. Bancs : **31/31** (health),
**22/22** (cadence), **14/14** (rebuild).

#### ⚠️ Ce qui n'est pas mesuré, et une découverte au passage

La **durée sur cible** n'est pas mesurée. L'essai lancé sur une copie de la base de 492 Mo d'un
boîtier de banc a coïncidé avec un **redémarrage** de ce boîtier — `throttled=0x0`, aucune
erreur noyau, et ce boîtier redémarre de lui-même tous les 1 à 3 jours, donc la causalité n'est
pas établie. Seule la copie brute est retenue : **86 s pour 492 Mo**. La base visée fait 306 Mo.

⭐ Et les uptimes du parc révèlent que **cinq boîtiers sur sept redémarrent tous les 3 à 5
jours** sans que personne ne le sache — la donnée (`up`, `boot`) est dans l'instantané depuis
0.9.22. Et le seul à **60 jours** d'uptime ininterrompu est précisément celui dont la base est
corrompue.

#### 🔍 Six défauts trouvés en revue de PR, et corrigés

1. 🚨 **`refus()` ne lisait qu'UNE ligne**, alors que `fetch_batch` en lit **mille** : si la
   page détruite n'est pas la première du lot — **le cas le plus probable**, puisque le dernier
   lot parti s'est arrêté juste avant elle — la sonde passait et l'update sortait sans réparer.
   **Elle aurait été inopérante sur le boîtier même pour lequel elle est faite.** La sonde
   `read.old` de `health.py` et le contrôle d'après-bascule avaient le même trou.
2. **La borne haute était majorée de 10 %**, sur l'idée fausse que sonder des `rowid`
   inexistants « ne coûte rien ». La recherche retombe sur la **même feuille détruite** : sur
   4,3 M lignes, ~430 000 lignes fantômes sondées une par une **et comptées comme perdues**.
   La majoration était en plus inutile — `sent` est `NOT NULL DEFAULT 0`, donc la borne par
   index est **exacte**.
3. 🚨 **On n'arrêtait que les unités `is-active`** : une unité en cours de redémarrage ne l'est
   pas, donc elle revenait, gardait la base ouverte, et après le `os.replace` ses écritures
   partaient dans le fichier devenu `.corrupt-*` — **perte silencieuse**. On arrête désormais
   **toute** la liste, et on vérifie l'**effet** : plus personne ne tient le fichier
   (`/proc/<pid>/fd`). ⭐ Et `None` ≠ `[]` : « je n'ai pas pu regarder » ne doit jamais valoir
   « voie libre ».
4. **Le frein de 3 tentatives était annulé** : `_ecris_rapport` réécrivait le fichier sans le
   champ `tentatives`.
5. **`echecs_base` ne repartait pas de zéro** après un lot réussi : la ligne « N fois de suite »
   mentait, et un `database is locked` isolé finissait par déclencher un signalement.
6. **Le watermark du rollup pouvait DESCENDRE**, déclarant couvert un intervalle jamais rempli
   — `/curve` aurait rendu du **vide** en croyant lire un rollup complet.

⭐ Et au passage, la portée réelle du frein a été établie **dans le code de l'agent** : le vrai
frein est le **bump de version**. `update.sh` sort 0 ⇒ `softwareVersion` passe à `0.9.25` ⇒ plus
aucune transition ne correspond. Le compteur ne couvre donc que le cas où `device.json` n'est
**pas** bumpé, c'est-à-dire un script **tué**. ⓘ Et aucune concurrence à craindre pendant les
~20 min : `check_update.py` prend un `flock` exclusif non bloquant et sort en 0 si une autre
instance le tient — un flock sur **descripteur**, donc relâché par le noyau même sur SIGKILL.

#### 🔍 Seconde revue : deux défauts réels, et un troisième trouvé en les corrigeant

🚨 **`ben-level-profiler` manquait, et son `.timer` aussi.** `levels.py` ouvre la base en
**écriture**, son timer tire tous les jours (`Persistent=true`), et **arrêter un `.service`
n'arrête pas son `.timer`** — il pouvait donc relancer le profileur en pleine reconstruction,
et ses écritures seraient parties dans le fichier devenu `.corrupt-*`.

⭐ Et le défaut était **structurel, pas un oubli** : ma liste dérivait de `CAP_SERVICES`, qui ne
décrit que les **lecteurs**. Une tâche périodique n'est pas une capability — aucune dérivation
ne l'aurait trouvée. D'où le garde-fou qui répond à la **classe** du défaut : on **re-vérifie
qui tient le fichier juste avant la bascule**. Ce contrôle ne dépend d'aucune liste, et il
couvre ce que je n'ai pas su énumérer. Les timers sont par ailleurs arrêtés **en premier**.

ⓘ Vérifié : les seules unités qui ouvrent `measurements.db` sont `ben-telemetry`,
`ben-tic-reader`, `ben-publisher`, `ben-local-api` et `ben-level-profiler`. `ben-certd`,
`wifi-watchdog`, `ben-network-*` et `ben-ble-provisioner` ne la touchent pas — ma décision de
les laisser debout tient, cette fois avec la preuve.

**Toutes les tables passent par la copie par tranches.** `copie_table()` chargeait une table
entière en mémoire et l'écrivait en une transaction ; `curve_rollup` peut dépasser 100 000
lignes. La fonction est supprimée (du code mort dans un module critique finit par resservir).
Bénéfice en prime : une table de métadonnée partiellement abîmée est recopiée pour ce qu'elle a
de **lisible** au lieu d'être abandonnée en bloc.

🚨 **Et cette unification a révélé un bug que rien d'autre n'aurait trouvé** : `pdl` est
déclarée `pdl_index INTEGER PRIMARY KEY`, donc `pdl_index` **est** le `rowid`, et le premier PDL
vaut **toujours 0**. La recopie partait de `rowid = 1` : la base reconstruite n'aurait eu
**aucun compteur**, et sans `pdl` aucune mesure n'a de sens. On part de `min(rowid)`, ce qui
évite en plus du travail inutile — sur un boîtier du parc, `min(rowid)` vaut **192 547**, les
plus anciennes étant purgées.

**`echecs_base` est remis à zéro après `fetch_batch`**, pas après le 2xx : la lecture réussie
est ce que ce compteur mesure. Sinon il grimpait pendant toute une panne **serveur**.

#### ⏱️ La durée, enfin mesurée — et les trois leviers du rythme

| variante | débit | pire tranche | WAL final |
|---|---|---|---|
| pause 50 ms · commit/4 · ckpt/40 | 1 281 l/s | 4 765 ms | 5 222 Ko |
| **sans** checkpoint explicite | 1 135 l/s — **×0,89** | 5 224 ms | 5 065 Ko |
| **commit/10** *(retenu)* | **1 360 l/s — ×1,06** | **4 532 ms** | 8 087 Ko |
| commit/20 | 1 339 l/s | 6 404 ms | 7 885 Ko |
| sans pause *(dangereux)* | 1 557 l/s — ×1,22 | 4 112 ms | 8 087 Ko |

⇒ **~50 minutes** pour les ~4 M lignes de 306 Mo, et c'est un **plancher** : le débit est mesuré
au début, quand la destination est petite. Mon « ~20 min » initial n'était pas mesuré.

🚨 Et deux de mes explications étaient fausses : le noyau ne laisse **jamais** 200 Mo de pages
sales s'accumuler (`dirty_background_ratio=10` → 42 Mo ; `dirty_ratio=20` → blocage à 85 Mo), et
le **checkpoint explicite n'est pas redondant** malgré `wal_autocheckpoint=4 Mo` — le retirer
coûte **11 %**. Le seul levier que personne d'autre n'actionne, c'est **rendre la main** : la
pause coûte 22 % de débit, et la **pire tranche** fait 4,5 s contre 60 s de chien de garde.

#### ⚠️ Les index créés après la copie : **non**, et c'est mesuré

Gain net **×1,17** seulement (2 091 l/s pour la copie, mais 1 748 une fois les trois
`CREATE INDEX` comptés). Et le prix est inacceptable : la plus longue instruction **non
cadençable** fait **9,5 s pour 255 000 lignes**, soit 2 à 3 minutes à pleine échelle —
**au-delà des 60 s du chien de garde**, qu'on ne peut ni interrompre ni ralentir.

#### ⭐ Deux phases : l'arrêt passe de ~50 min à quelques secondes

Une fois la durée mesurée (~50 min, et non les ~20 que j'avançais sans mesure), arrêter le
collecteur pendant toute l'opération devenait trop cher. **Le découpage se fait par
mutabilité, pas par taille** :

| | tables | pourquoi |
|---|---|---|
| **phase 1** — *rien d'arrêté*, ~50 min | `measurements`, `lora_link` | **append-only** : le `rowid` croît, les lignes ne changent plus (sauf `sent`) ⇒ lisibles à chaud |
| **phase 2** — *écrivains arrêtés*, ~50 s | le delta des deux précédentes (~2 200 lignes, 2 s), puis `pdl`, `emitter`, `contract_epoch`, `tariff_labels`, `level_profile`, `rollup_state`, `curve_rollup` (~60 000 lignes, 44 s) | **modifiées sur place** : `pdl.last_seen` bouge à chaque trame, `curve_rollup` fait un `UPSERT` par tranche de 2 min |

🚨 Une table modifiée sur place **ne peut pas** être recopiée en phase 1 : un delta par `rowid`
ne verrait pas une mise à jour, et la base neuve porterait des valeurs **périmées** sans que
personne ne le remarque. Un cas de banc l'éprouve en modifiant `pdl.last_seen` **entre** les
deux phases et en exigeant la nouvelle valeur ; la mutation qui recopie `pdl` en phase 1 le
fait tomber.

⭐ Et ça fait s'effondrer un risque : le filet `systemd-run` ne couvre plus que la fenêtre
courte — **15 min au lieu de 2 h**. Avec une seule phase, le danger qu'il tire *pendant*
l'opération (réveillant les écrivains juste avant la bascule) était réel.

**Mesuré sur cible** : phase 1 en 25 s *« RIEN n'a été arrêté, rien n'a été basculé »*
(empreinte de l'original inchangée, vérifiée par un cas de banc), phase 2 en **1,8 s**, et un
relevé externe toutes les 3 s montre les services absents **~6 s** — l'arrêt/redémarrage
lui-même domine sur une petite base.

ⓘ Deux effets de bord assumés : un `sent` qui passe à 1 pendant la phase 1 fait **renvoyer** la
ligne, que le cloud dédoublonne (`ON CONFLICT DO NOTHING`) ; et une ligne purgée après avoir
été recopiée **ressusciterait**, pour être purgée au cycle suivant.

ⓘ Aucune migration, aucune table, aucune colonne.

### [0.9.24] — 2026-10-01

**L'échec se signale lui-même, et la sonde voit enfin.** Ferme [#21](https://github.com/xdegenne/ben-firmware/issues/21).

`pi-0.9.23` a été livrée vers 11 h. **Deux heures plus tard**, le boîtier qui motive tout ce
chantier l'avait prise, avait envoyé son premier instantané de santé — et ne nous avait
toujours rien appris. Les deux raisons sont des défauts de `0.9.23`, pas du boîtier.

#### ① La sonde `pub` était aveugle au seul moment où elle s'exécute

`N_PUB` valait 5. Voici, mot pour mot, les cinq lignes remontées à 11:47 :

```
Stopped ben-publisher.service …
ben-publisher.service: Consumed 1.308s CPU time.
Started ben-publisher.service …
[INFO] démarrage — … · lots de 1000 toutes les 60 s
[INFO] ~569526 point(s) en attente
```

🚨 Cinq lignes, cinq places : **aucune ligne de l'ancien processus** — qui est sa raison
d'être, puisque la sonde ne tourne *qu'*au hello suivant le redémarrage de l'OTA. Or un
redémarrage systemd émet cinq lignes à lui seul.

⚖️ Le défaut ne produisait **aucune erreur** : cinq lignes valides, bien formées, vides de
sens. Rien dans un banc fonctionnel ne pouvait s'en plaindre — c'est la **constante** qu'il
faut lire. `N_PUB = 40` couvre ~40 min d'un publisher en échec pour ~3,5 Ko, contre un
plafond serveur de 16 Ko.

#### ② L'instantané n'arrivait qu'une fois par jour

Mesuré le même jour, sur le même boîtier :

| | |
|---|---|
| il **mesure** | dernier point à l'instant, trame LoRa toutes les 38 s |
| il est **en ligne** | hello 204 en 17 ms, WiFi −40 dBm |
| le publisher **tourne** | `active/running`, 0 redémarrage, charge 0,24 |
| `pending` | **569 526** — exactement les 8,8 jours manquants, à **0,35 %** près |
| POST de mesures | **zéro**, pendant que six autres boîtiers en font 25 en 25 min |
| côté `ben-api` | **aucun 4xx, sur aucun boîtier** |

⭐ Le serveur ne refuse rien : **le boîtier ne demande pas.** Il échoue localement, il l'écrit
dans son journal à chaque tentative, et cette ligne restait illisible jusqu'au lendemain —
l'instantané ne voyage qu'avec le hello, et le hello ne part qu'une fois par jour
(`HELLO_EVERY=86400`). On attendait 24 h pour apprendre ce que le boîtier savait depuis la
première minute.

Désormais, au franchissement de `ECHECS_ERREUR`, le publisher **envoie un hello** :
l'instantané part *pendant* la panne, avec les lignes de son propre journal.

- ⚠️ Plafonné à **un par heure** (`HELLO_SUR_ECHEC_S=3600`). Sans ce plancher, un boîtier en
  panne persistante enverrait un instantané toutes les 300 s au plafond du backoff, avec
  jusqu'à 20 s de collecte chaque fois : un boîtier coupé du monde doit signaler, pas battre.
- ⚠️ Le seuil est celui de l'**erreur**, pas le premier échec — une coupure de quelques
  secondes arrive tous les jours sur les sept boîtiers. **Un seul seuil**, partagé avec
  `niveau_echec()` : deux seraient deux choses à garder d'accord.
- 🚨 **L'ordre est le fond.** Le hello part *après* la ligne de journal, jamais avant, parce
  que `snapshot()` **lit** le journal. Envoyé d'abord, il partirait avec un instantané qui ne
  contient pas l'échec qui l'a déclenché : un signalement qui ne signale rien — exactement le
  défaut de `N_PUB = 5`.
- ⭐ `signaler_echec()` est extraite en **fonction pure**, comme `cadence()` et
  `niveau_echec()` : c'est ce qui permet d'éprouver le plancher horaire sans attendre une
  heure, sans réseau et sans base. Le préflight l'éprouve **sur la cible**, avec son témoin.
- ⚠️ `dernier_hello_echec` part de `-inf` et non de `0.0` : `time.monotonic()` part de
  l'uptime, donc avec `0.0` le premier signalement serait immédiat sur un boîtier debout
  depuis longtemps et retardé d'une heure sur un boîtier qui vient de démarrer — deux
  comportements pour un seul code.

#### ③ `pending` encadre, il ne compte pas — et j'avais confondu les deux

`pending_approx()` vaut `max(rowid) − min(rowid WHERE sent=0) + 1`. Il prouve **où** se trouve
la plus vieille ligne non envoyée, et rien de plus. On a lu « 566 248 en attente » et on en a
déduit « un lot plein part à chaque tour, donc il échoue » — alors qu'**une seule** ligne
restée à `sent = 0` sur un vieux rowid produit exactement le même chiffre.

⚠️ L'accord à 0,35 % avec le trou de 8,78 jours établit la **position** de cette ligne
(22/09 15:44), pas leur **nombre**.

`health.unsent` compte, borné au lot :

```sql
SELECT count(*) FROM (SELECT 1 FROM measurements WHERE sent = 0 LIMIT 1000)
```

| `unsent` | ce qu'on sait |
|---|---|
| **1000** | un lot plein existe ⇒ un POST est tenté ⇒ **il échoue**, et `pub` dit pourquoi |
| **0** | rien à envoyer ⇒ aucun POST, aucun échec ⇒ `pending` est un artefact, et la question devient : pourquoi des lignes sont-elles `sent = 1` alors que le cloud ne les a pas ? |
| entre | des lots **partiels** — un troisième cas, qu'on ne voyait pas |

- ⚠️ **Borné**, et c'est tout ce qui le rend possible : un `count(*)` nu sur `sent = 0` balaie
  les millions d'entrées de l'index et prend **37 s** sur un Pi Zero. Mesuré avec la borne
  *réellement* atteinte : **1,8 ms** (radio) et **2,2 ms** (filaire), par index **couvrant** —
  `idx_meas_sent` suffit, aucun accès à la table.
- ⚠️ La borne est lue sur `BEN_PUB_BATCH`, la **même** variable que `ben_publisher.BATCH`, pas
  une constante à part qui divergerait au premier réglage. `health` étant importé *par*
  `ben_publisher`, l'importer en retour serait circulaire.

#### ④ « rien à envoyer » n'écrivait rien

La branche était en `log.debug`, et le niveau racine est `INFO`.

🚨 Un publisher qui échoue en boucle et un publisher qui n'a rien à envoyer laissaient
**exactement la même trace : aucune.** Impossible de les séparer à distance — et c'est ce qui
a coûté neuf jours.

Elle passe en `info`, **avec le retard joint**, parce que c'est la contradiction qui informe :
`rien à envoyer · reste ~569526` dit en une ligne que `pending` et la réalité ne s'accordent
pas, là où les deux chiffres séparés ne disaient rien.

- ⚠️ Gratuit sur un boîtier sain : à 0,74 point/s et une période de 60 s, chaque tour porte
  ~44 points — cette branche n'y est jamais atteinte.
- ⚠️ L'appel à `pending_approx()` y est gardé par un `try/except sqlite3.Error` : il est
  **dans** le `try` de la boucle, donc une erreur locale compterait comme un échec **serveur**
  et enverrait le publisher en backoff long. C'est le défaut même que `cadence_sure()` existe
  pour éviter.

#### ⚠️ Et un banc qui mentait, attrapé par mutation

Le cas qui vérifie que le signalement réarme `prochain_hello` ancrait sur
`signaler_echec(echecs`, qui matche d'abord la **définition** de la fonction, deux cents
lignes plus haut. Le bloc extrait englobait tout `main()` et contenait le
`prochain_hello = hello()` d'*avant* la boucle : **le cas passait avec le défaut en place.**

⭐ C'est la **mutation** qui l'a trouvé, pas la relecture. **Six** mutations ont été jouées
sur ce tag, et toutes sont attrapées : `N_PUB` remis à 5, signalement placé avant le `log`,
`prochain_hello` non réarmé, `LIMIT` retirée du comptage, `unsent` recopiant `pending`, et
`debug` au lieu d'`info`.

Bancs : **30/30** (`test_health.py`) et **22/22** (`test_cadence.py`).

ⓘ Aucune migration, aucune table, aucune colonne. Ce script ne touche à aucun service — le
seul concerné est `ben-publisher`, que `check_update.py` redémarre à son étape 10.

### [0.9.23] — 2026-10-01

**Le niveau de journalisation atteint enfin le journal.** Ferme [#18](https://github.com/xdegenne/ben-firmware/issues/18).

`pi-0.9.22` a livré l'instantané de santé, et il a **démenti trois diagnostics successifs** sur le boîtier qui motivait tout le chantier :

```
il MESURE              une trame toutes les 38 s, à l'instant
il est EN LIGNE        hello 204 en 17 ms, WiFi à −40 dBm
le publisher TOURNE    active/running, 0 redémarrage
pending                566 248 points — 8,8 jours, l'écart exact observé
ce qui part            RIEN, aucun POST en quatre minutes
```

⇒ Ni « fenêtres de connectivité courtes », ni « arrêt de mesure », ni « émetteur mort ». **Il mesure et ne publie pas.** Et le diagnostic à distance ne pouvait pas voir pourquoi.

**🚨 Le premier correctif écrit pour cette version ne servait à rien**

Monter `log.warning` en `log.error` ne change que le **texte**. `logging.basicConfig()` écrit sur stderr, et systemd range tout ce qui vient de stderr à une priorité **fixe** (`SyslogLevel=6`). Mesuré sur un boîtier du parc, sur un vrai échec :

```
[2026-09-23 03:22:51][WARNING] échec n°1 ([Errno -3] Temporary failure in name…
                      ↑ le texte dit WARNING          →  PRIORITY=6
```

⇒ `health.errors()` interroge `journalctl -p 3`. Il ne voyait **rien** de ce que le publisher dit de ses pannes — et le banc comme le préflight étaient **verts**, parce qu'ils n'éprouvaient que `niveau_echec()`, une fonction juste et sans effet.

> ⭐ **Un contrôle qui vérifie une décision sans vérifier son effet est un contrôle qui ment.**

**⭐ Le vrai correctif : le préfixe `<N>`**

Le mécanisme documenté de systemd (`SyslogLevelPrefix=yes`, actif par défaut) : il lit la priorité en tête de ligne, l'applique, et la **retire** du message. Vérifié sur la cible avec le vrai `installer_journal()` :

```
PRIORITY=6  [INFO]    …
PRIORITY=4  [WARNING] … echec n 1 (timed out)
PRIORITY=3  [ERROR]   … echec n 7 (HTTP 400 lot malforme)
```

⚠️ **Zéro dépendance** : pas de `python-systemd` à embarquer sur sept boîtiers pour ça.

**Et la décision, qui ne vaut que posée sur ce socle**

Au-delà de `ECHECS_ERREUR` (5) échecs consécutifs, l'échec passe en `log.error`, donc en priorité 3, donc **visible à la sonde**. Un réseau cligne : échouer une fois n'est pas une erreur. Au-delà, ce boîtier **ne livre plus ses données** — l'état le plus grave qu'il puisse connaître sans être mort.

⚠️ Seuil **strictement sous le plafond de backoff** (~9 échecs) : au-delà le boîtier n'avance plus, et un seuil plus haut ne se verrait jamais plus tôt tout en retardant la visibilité de dizaines de minutes. Un cas de banc l'exige.

**⚠️ L'explication du serveur voyage avec l'exception**

La ligne escaladée portait `HTTP 400` **sans le corps** — un refus sans sa raison, soit le même angle mort. Or un 400 ou un 413 **ne se résout pas en réessayant** : le lot est malformé ou trop gros, et le corps est la seule chose qui dira lequel.

**⭐ Et la sonde `pub`, abandonnée puis rétablie**

Les 5 dernières lignes du journal de `ben-publisher`, **sans filtre de priorité**.

Je l'avais retirée au motif qu'`errors()` remonte déjà la priorité 3. ⚠️ **C'est faux pour le moment qui compte** : le hello part **juste après** le redémarrage du publisher par l'OTA. À cet instant le nouveau processus a `echecs = 0` — aucune ligne en priorité 3 n'existe encore — et celles de l'**ancien** processus sont en `PRIORITY=6` sur tout boîtier antérieur à ce tag.

⇒ Sans `pub`, la cause d'une panne de publication n'arriverait qu'au hello **suivant**, donc **sous 24 h**. Avec, elle arrive au **premier**, quelques minutes après l'OTA.

⚠️ **Et sans filtre de priorité, c'est ce qui la rend sûre.** `-u` ne dégénère en balayage complet que s'il n'y a **aucune** correspondance — journald parcourt alors tout le journal pour n'en trouver aucune, et c'est de là que venaient les **7,93 s** de `-u ben-radio -p 3`. Les lignes INFO du publisher garantissent toujours une correspondance : la lecture reste une lecture de **queue**.

⚠️ Elle passe **en dernier** : l'ordre des sondes est une liste de priorité, et l'échéance globale sacrifie la dernière en premier. Sur un boîtier en difficulté, mieux vaut perdre son journal de publisher que l'état de ses services.

ⓘ Le nombre de points réellement insérés, lui, est journalisé **côté serveur** (`ben-api#8`) — là où le chiffre était déjà calculé et jeté.

**Budget de collecte : 12 → 20 s**

⚠️ Le coût **varie de 2 à 10 s sur le même boîtier** selon l'état du cache du journal — trois passages consécutifs à 2 029 / 1 998 / 2 088 ms quand le même code venait d'en mettre 9 655. Avec 12 s, la marge était d'une seconde et demie.

**ⓘ Corrige aussi le commentaire sur `tainted`**

Il annonçait « 1024 = TAINT_WARN ». **Faux deux fois** : `TAINT_WARN` est le bit **9** (512), et **1024 est le bit 10** — pilotes *staging* (`snd_bcm2835`, `vc_sm_cma`, `bcm2835_isp`), soit l'état **normal** de Raspberry Pi OS. 1024 est le **plancher**. ⭐ Ce qui compte est tout bit **au-delà** : `128` = noyau mort (OOPS/BUG), `16384` = *soft lockup* (signature d'un SPI figé).

Aucune migration, aucune table, aucune colonne.

### [0.9.22] — 2026-10-01

**Le boîtier joint un instantané de santé à son hello.** Ferme [#16](https://github.com/xdegenne/ben-firmware/issues/16).

> 🚨 Un boîtier qui **cesse de mesurer** continue de dire bonjour. Constaté le 2026-10-01 sur un boîtier du parc : **neuf jours** sans une seule mesure, pendant lesquels il a dit bonjour chaque jour, le serveur a répondu `204`, `last_seen` est resté **frais**, il a pris deux OTA — et **rien n'a alerté**. On l'a découvert en regardant autre chose.

⭐ `last_seen` confond trois états :

| état | `last_seen` | visible ? |
|---|---|---|
| mesure **et** publie | frais | — |
| mesure mais **ne publie pas** | ancien | oui |
| **ne mesure plus** | **frais** | 🚨 non |

Seul le troisième est une panne silencieuse. Cet instantané ne sert à rien d'autre qu'à **séparer le premier du troisième**, et à dire pourquoi.

**⭐⭐ Ce qui sépare deux pannes qui se ressemblaient**

Pour un boîtier **radio** devenu muet, « l'émetteur est mort » et « le récepteur est sourd » donnaient la **même** signature. Deux champs les séparent — et ils existaient déjà sans qu'on les publie :

- **`silence_restarts`** : `ben-radio` le remet à 0 à chaque trame et l'incrémente quand son détecteur de silence le relance. Après des jours sans trame il est donc **grand** si le récepteur écoute vraiment, et **nul** si le détecteur n'a jamais joué.
- **`radio.recent`** : les 20 dernières trames de `lora_link` **avec leurs horodatages**, pour **3,5 ms**. Sur un boîtier muet depuis des jours, elles datent du **jour de sa mort** — chute brutale à pleine puissance ⇒ alimentation ; dégradation progressive ⇒ antenne ou portée. **Aucun autre champ ne permet de trancher**, et l'information s'effacera à 180 jours.

**Autres champs que personne ne regardait**

- **`tainted`** — le masque de bits du noyau, **brut**. ⚠️ Le plancher d'un Raspberry Pi est **1024** (bit 10 `C`, pilotes *staging* : `snd_bcm2835`, `vc_sm_cma`, `bcm2835_isp`…) : c'est l'état normal, pas un incident, et ce sera 1024 sur les sept boîtiers. ⭐ **Ce qui compte est tout bit au-delà** — `128` (le noyau est mort, OOPS/BUG), `16384` (*soft lockup*, la signature d'un SPI figé), `512` (WARNING). Donc `1152` = Pi normal **plus** un oops. ⓘ Le drapeau est permanent : c'est ce qui en faisait un mauvais signal de santé en `0.9.12`, où il pilotait un test périodique (208 redémarrages de `ben-radio`). On le **rapporte**, on ne décide rien avec.
- **`wifi`** via `/proc/net/wireless`, pour 7,9 ms. ben-0001 est à **−76 dBm** contre **−31** pour ben-0003 — la question des « fenêtres de connectivité » commence peut-être là.
- **`repo.dirty`** — un dépôt sale **bloque le `git checkout` nu de l'OTA**, piège documenté qu'on ne pouvait constater qu'en SSH.

**🚨 L'innocuité prime sur le contenu**

Cet instantané voyage dans le hello, qui porte les compteurs, les époques tarifaires et les libellés des sept boîtiers. **Une sonde qui lève ferait échouer le hello entier.** Chaque sonde est donc isolée et se replie sur l'**absence** de son champ — jamais sur une valeur de repli : « 0 trame reçue » et « je n'ai pas pu compter » ne veulent pas dire la même chose. Délai **par sonde** (4 s) *et* budget global (12 s), parce que le vrai risque n'est pas qu'une sonde lève mais qu'elle **pende**.

**Écarté après mesure sur la cible**

- 🚨 **`PRAGMA quick_check` n'est pas « coûteux », il est NUISIBLE** : sur 490 Mo il a poussé la charge d'un Pi Zero mono-cœur de **1,15 à 4,50**, rendu `sshd` muet plus d'une minute, et **ne s'est pas terminé en 180 s**.
- 🚨 **`journalctl --since` coûtait 9 secondes pour rendre 0 octet**, là où `-n 8` seul en rendait 8 392 : les dernières erreurs existaient, elles dataient de plus de 24 h. **Filtrer par date coûtait neuf secondes pour jeter l'information utile.**
- `systemctl status` pagine et rend de la prose localisée ⇒ `systemctl show --property=` avec **`--timestamp=unix`** obligatoire.
- `vcgencmd get_throttled` échoue en utilisateur `ben`.

⭐ **`collect_ms` et `load` sont dans le paquet** : un instantané pris en 1 s sur un boîtier au repos et un pris en 30 s sur un boîtier en détresse se ressemblent, et ne disent pas la même chose.

**Éprouvé**

Banc `test_health.py`, **16 cas**, verts sur Mac — où `/proc` et `systemctl` n'existent pas, donc le cas de panne, gratuit — **sur un boîtier radio et sur un boîtier filaire**. Deux défauts trouvés en lançant la collecte sur de vrais boîtiers, qu'aucun banc sur Mac n'aurait vus : `capabilities` est un **dict** dans `device.json` et pas une liste (ne garder que les listes faisait disparaître le champ en silence, alors qu'il porte la version de firmware de l'émetteur), et le cas « boîtier filaire » était vert **par accident** sur Mac faute de `/var/lib` alors qu'il échouait sur un vrai boîtier radio — `radio()` lit aux **deux** endroits, la table *et* les fichiers d'état.

Coût : **4 786 ms / 576 o gzippés** en radio, **988 ms** en filaire. Le hello fait déjà 276 o gzippés, les mesures 270 Mo/an.

⚠️ **Prérequis déployé AVANT ce tag** : la table `device_health` et l'API qui l'accepte (`ben-api#5`). L'ordre inverse aurait coupé l'ingestion des sept boîtiers d'un coup.

Aucune migration, aucune table, aucune colonne côté boîtier.

### [0.9.21] — 2026-09-30

**Éradiquer les fantômes déjà entrés.** `0.9.19` a fermé la porte ; celle-ci répare ce qui était **déjà dedans**. Ferme [#7](https://github.com/xdegenne/ben-firmware/issues/7).

> 🚨 **C'est une migration de DONNÉES, pas une livraison de code.** Aucun `.py` de service n'est modifié — et ça change la nature du risque. Les cinq updates précédentes risquaient « le service ne repart pas », visible tout de suite. Celle-ci risque « on a supprimé la mauvaise ligne » : **irréversible**, sur des bases qui **ne sont pas sauvegardées**.

**⭐ Une seule cause pour deux symptômes : le bit 6.**

Un caractère de la trame TIC dont le bit 6 s'est mis à 1 — `0`→`p`, `1`→`q`, `.`→`n`. Or `0x40` vaut **64**, et le checksum TIC est `(somme & 0x3F) + 0x20` : **aveugle à tout multiple de 64**.

```
'HC..'  somme=231  checksum=0x47
'HCn.'  somme=295  checksum=0x47     ← le même
```

Relevé le 30/09 sur un boîtier filaire du parc : **trois ADCO fantômes** portant une mesure chacun, et **quatre époques tarifaires** `HC..`→`HCn.`. Le contrôle de parité matérielle de `0.9.18` ferme cette porte — un seul bit retourné rend la parité fausse ; ici on nettoie derrière.

**⭐ La règle des époques tient en une ligne.**

> La **première** époque est la référence. Toute autre qui lui ressemble sur les **deux premiers caractères** part.

- **Pourquoi le préfixe et pas une durée** — une époque **terminale** n'a pas de successeur, donc aucune durée mesurable. Et c'est précisément celle qui compte : c'est elle que lisent `/registers` et tout calcul de coût.
- **Pourquoi « ressemble » et non « diffère »** — les abîmées naissent **en paire** : le lecteur rouvre une époque en revenant au vrai contrat 1 à 4 s plus tard, et cette jumelle est *identique* à la référence. La même condition emporte les deux, et il ne reste qu'une ligne là où cinq disaient la même chose.

⚠️ **Ce qu'elle ne fait pas, et c'est assumé** : un **retour** au contrat d'origine (`BASE`→`TEMPO`→`BASE`) serait effacé lui aussi. Aucun boîtier du parc n'est dans ce cas — `BASE`, `TEMPO`, `HC..` et `BBR(` diffèrent tous dès le deuxième caractère — et **un banc épingle cette limite** pour qu'on ne la redécouvre pas sur le terrain.

**Les mesures sont RÉ-ATTRIBUÉES, jamais supprimées.** Elles viennent du vrai compteur ; seul leur classement était faux. `sent=0` pour qu'elles repartent au cloud, qui les a reçues sous un PDL qui n'existe plus. De même `emitter` est **ré-orientée** et non supprimée : sans sa ligne, une trame de courbe n'aurait plus où se ranger tant qu'aucune trame de boot n'est repassée.

🚨 **Garde** : ré-attribuer suppose de savoir **vers qui**. Avec zéro ou plusieurs PDL sains, la destination est indécidable — on sort sans rien toucher.

**🚨 Le contrôle d'effet est un INVARIANT, pas un compte.**

Le succès n'est pas « j'ai supprimé quelque chose » : sur six boîtiers du parc il n'y a rien à faire, **et c'est un succès**. Exiger un effet ferait échouer l'update partout ailleurs, `device.json` ne serait pas bumpé, et elle **rejouerait toutes les 10 min** — la mécanique exacte qui a brûlé `pi-0.9.12`. On vérifie donc « la base est conforme », vrai **avant comme après** sur un boîtier propre.

⭐ Et **rien à faire ⇒ aucun service touché** : on sort avant même d'arrêter quoi que ce soit. Redémarrer un lecteur coûte des mesures (leçon de `0.9.17`). ⚠️ La porte de sortie interroge **l'invariant**, pas un compte de fantômes — sinon une orpheline sans rapport ferait afficher « déjà conforme ✓ » sans que rien n'ait été vérifié.

**🚨 Et la règle qui en découle, valable pour toute migration de données : AUCUN état de la DONNÉE ne fait échouer cette update.**

Deux états parfaitement légitimes seraient sans cela pris pour des pannes — le **refus** (« je ne sais pas vers qui ré-attribuer »), et un **invariant encore faux** (une anomalie que ce ménage-ci ne sait pas réparer). Or un `update.sh` qui échoue laisse `device.json` non bumpé : l'update rejouerait toutes les 10 min **et ce serait définitif**, puisque aucune version ultérieure ne pourrait plus atteindre le boîtier. On rapporte, on n'échoue pas. Seuls le **préflight** (code cassé — on veut retenter au prochain tag) et « **un service arrêté n'est pas revenu** » (dégât réel) ont le droit de faire échouer.

⚠️ **`ben-publisher` est arrêté lui aussi**, alors qu'il n'écrit aucune mesure : il lit un lot de `sent=0`, le **poste**, puis marque `sent=1` **par rowid**. Un lot parti sous le `pdl_index` fantôme juste avant le ménage verrait ses lignes déplacées sous le vrai PDL, puis marquées envoyées — le cloud ne les aurait jamais reçues sous le bon compteur, et plus rien ne les lui enverrait.

**La marche à blanc est un banc.** Le boîtier concerné est **injoignable** — fenêtres de connectivité courtes, pas de SSH. On ne peut pas lire une marche à blanc sur place : `test_menage_fantomes.py` **reconstitue sa maladie à l'identique** (10 cas), dont le témoin « une vraie bascule de contrat survit » — sans lui, une règle qui supprimerait *tout* passerait tous les cas de suppression et effacerait le passage en Tempo d'un boîtier du parc.

⚠️ **Le hello est une FUSION** (`ON CONFLICT DO UPDATE`, jamais de `DELETE`) : nettoyer le boîtier ne nettoie **pas** le cloud, et nettoyer le cloud **avant** le boîtier se fait défaire au hello suivant. L'ordre est **boîtier d'abord, serveur ensuite**.

Aucune migration de schéma, aucune table, aucune colonne.

### [0.9.20] — 2026-09-30

**Le publisher rattrape son retard.** Un boîtier à **courte fenêtre de connectivité** ne rattrapait jamais : il divergeait, et l'écart grandissait chaque jour. Ferme [#13](https://github.com/xdegenne/ben-firmware/issues/13).

> 🚨 Il envoyait **500 points toutes les 60 s** — soit 500/min — pour une production radio de **64,5/min**. Débit net : 435/min. Il lui fallait donc **plus de 3 h 30 de connectivité par jour rien que pour ne pas reculer.**

**⭐ Le goulot n'était PAS la taille du lot — et c'est la mesure qui l'a tranché.**

Relevé sur un Pi Zero du parc, trois essais par taille :

| lot | brut | gzippé | ratio | aller-retour |
|---|---|---|---|---|
| 500 | 49,8 ko | 3,0 ko | ×16,6 | **206 ms** |
| **1 000** | 99,6 ko | **5,9 ko** | ×16,9 | **400 ms** |
| 2 000 | 200 ko | 11,8 ko | ×17,0 | 690 ms |

Un lot part en ~0,4 s, puis le service dormait **60 s** : il travaillait **0,7 % du temps**.

⚠️ Augmenter `BATCH` seul n'aurait presque rien gagné, et ne rien changer à `BATCH` non plus : **le sommeil se paie par lot, pas par point**, donc les deux paramètres se décident **ensemble**.

| `BATCH` | sommeil | lots | rattrapage d'une journée |
|---|---|---|---|
| 500 | 60 s | 186 | **3 h 06** ← avant |
| 500 | 10 s | 186 | 33 min |
| **1 000** | **10 s** | **93** | **16 min** ← retenu |
| 1 000 | 0 s | 93 | 37 s |

**Le seuil de survie tombe de ~3 h 30 par jour à ~16 min.**

**Pourquoi 10 s et pas 0** — 🚨 un sommeil nul, c'est **tout le parc à plein débit sur l'API en même temps** après une panne d'opérateur, sur une VM **DEV1-S** au budget mémoire déjà tendu. À 10 s la charge parc plafonne à **~670 points/s** pour sept boîtiers, et 16 min de rattrapage reste largement au-delà du besoin. On ne paie pas un risque serveur pour un gain qui ne sert à rien.

⭐ **Et le contrôle du retard ne coûte rien** : `pending_approx()` est **O(1)** — il encadre par les `rowid`, justement parce qu'un `count(*) WHERE sent=0` prenait **37 s** sur Pi Zero — et il était **déjà** appelé à chaque lot pour la ligne de journal.

**🚨 Un défaut trouvé en revue, introduit par ce correctif**

`pending_approx()` interroge la base, et son appelant est **hors du `try`** de la boucle. L'ancien `_sleep(PERIOD)` ne pouvait rien lever ; le nouveau si — verrou tenu au-delà du timeout pendant que le lecteur écrit, ou erreur d'E/S sur la carte SD. Sans garde, l'exception remontait **hors de `main()`** : le process mourait sans passer par « arrêté proprement », systemd le relançait, et on perdait le backoff.

`cadence_sure()` l'attrape. ⭐ Son repli est **`PERIOD`, jamais `PERIOD_RETARD`** : ne pas savoir mesurer le retard ne doit pas faire **accélérer**. ⚠️ Et `echecs` n'est pas incrémenté — ce compteur parle du **serveur** ; une base locale qui bronche n'est pas un serveur en panne.

⚠️ **Le chemin d'échec est inchangé** : il sort par `continue` en gardant son backoff exponentiel à gigue totale. Un serveur en panne ne déclenche donc **jamais** la cadence de rattrapage.

**Le script d'update**

| | |
|---|---|
| ne touche **qu'un service** | `ben-publisher`. Ni les lecteurs, ni `ben-radio`, ni `ben-local-api` ne partagent de code avec ce changement |
| ne démarre pas ce qui ne tournait pas | leçon de 0.9.17 |
| préflight | `ast.parse` — jamais `py_compile` — **et la cadence éprouvée avec ses témoins** |
| 🚨 contrôle d'effet **différent des versions précédentes** | `last_tic_ts` prouve qu'un **lecteur** lit ; il ne prouve **rien** sur le publisher, qui n'écrit pas dans `measurements`. Le reprendre donnerait un vert qui ne veut rien dire — le défaut de `pi-0.9.12` sous une autre forme |

⭐ Ce qu'on contrôle à la place : le service est debout **et il le reste**. `is-active` juste après un `restart` ne voit pas une boucle de plantage — on regarde donc **deux fois**, à 15 s d'intervalle, et on exige que `NRestarts` n'ait pas bougé. Plus une trace d'activité au journal : un service figé sur une exception avalée resterait « active » sans plus rien faire.

**Aucune migration, aucune table, aucune colonne.** Banc `test_cadence.py` : 9 cas, dont trois témoins et deux cas de terrain. Quatre sabotages joués.

### [0.9.19] — 2026-09-29

**Ne jamais écrire sous un `pdl_index` deviné.** Trois portes laissaient une mesure partir en base sous un compteur qui n'était pas le sien. Ferme [#6](https://github.com/xdegenne/ben-firmware/issues/6).

> 🚨 **Le défaut n'était pas théorique** : un ADCO fait de **deux octets NUL** a créé un PDL fantôme portant **13 056 mesures** sur un boîtier du parc. Un second est né **en direct pendant la préparation de cette version**, le 28/09 à 22:00:46, et a détourné la courbe du vrai compteur pendant treize heures.

**① `resolve_pdl` acceptait n'importe quel ADCO.** Son garde testait le **vide**, pas la **forme** — et `.strip()` ne retire pas les octets NUL, si bien que `'\x00\x00'` le passait.

Nouveau prédicat **public** `db.adco_valide()` : douze chiffres ASCII, **miroir exact du `CHECK (ads ~ '^[0-9]{12}$')` du cloud**, mais posé **là où la donnée naît**. Public parce que le ménage des fantômes doit cibler par prédicat, jamais par une liste de `pdl_index` recopiée — le bon PDL est presque toujours `0`, et une faute de recopie détruirait la vraie courbe.

⚠️ **`isdigit()` seul ne suffit pas** : il est vrai des chiffres Unicode. `'²' * 12` fait bien douze caractères et passerait — il créerait donc un PDL que le cloud refuserait. C'est `isascii()` qui rend les deux prédicats équivalents, pas une précaution de style.

**② `PDL_INDEX` valait `0` à l'amorce du lecteur filaire.** Or `0` est l'index du **premier compteur de tout boîtier** : l'amorce et une vraie réponse étaient **indiscernables**. Si la première trame ne portait pas d'ADCO exploitable, la mesure partait sous `0` — le compteur d'un boîtier **déplacé**, en silence.

Sentinelle `None`, et prédicat pur `peut_stocker(conn, pdl_index)` comme seul juge du droit d'écrire. ⭐ Même idiome que la voie LoRa, où `get_pdl_index()` rend déjà `int | None`.

**③ Un ADCO non conforme condamne la TRAME ENTIÈRE**, pas seulement son groupe — et c'est le point le moins intuitif.

On n'atteint ce contrôle **que** par un groupe ayant passé **la parité ET le checksum** sans avoir la forme d'un ADCO : un groupe abîmé par l'un ou l'autre est déjà jeté par `read_frame`. Le seul cas qui arrive jusque-là est donc **l'amputation dans l'angle mort du checksum** — caractères retirés sommant à un multiple de 64.

⭐ Or cet angle mort est le **même pour tous les groupes** de la trame. Un index ou un PAPP raccourci a pu passer exactement pareil, sans qu'aucune forme ne le révèle, puisqu'un nombre raccourci reste un nombre. **L'ADCO est le seul champ de la TIC dont la forme soit connue d'avance** : c'est le seul témoin qu'on ait de cet angle mort, et le garder pour ne jeter que son groupe reviendrait à s'en priver.

**Côté LoRa**, une trame de boot sans identité valable est écartée **en entier**. Le MAC ChaCha20 prouve que les octets sont ceux qui ont été émis — donc un ADCO difforme ne dit pas « la radio a abîmé la trame », il dit **« l'émetteur a mal lu sa TIC »**. Et `ISOUSC`, `PREF`, `CONTRAT` sortent de **la même lecture**.

🚨 **`frame_ok` reste HORS du garde de stockage, et `TRAME_CONDAMNEE` est distinct de `None`.** Une trame condamnée **prouve que la liaison est vivante** : ses groupes arrivent, leur parité et leur checksum passent. La confondre avec une trame absente laisserait `last_success_time` figé et le watchdog relancerait le process **toutes les 10 minutes** — le mode de défaillance qui a brûlé `pi-0.9.12`. Défaut trouvé **en revue**, couvert par un banc dédié.

**Le script d'update**

| | |
|---|---|
| 🚨 **ne redémarre PAS `ben-radio`** | `capabilities.py` mappe `lora-tic-receiver` sur ben-radio **et** ben-telemetry, mais la façade radio n'est pas touchée — elle n'importe même pas `db.py`. On se sert de `capabilities has` pour **décider**, on nomme les unités **à la main** |
| ne démarre pas ce qui ne tournait pas | leçon de 0.9.17, qui avait lancé un lecteur filaire sur des boîtiers radio |
| préflight | `ast.parse` — **jamais `py_compile`**, qui écrit un `__pycache__` appartenant à root — **plus `adco_valide` éprouvé sur la cible AVEC SON TÉMOIN** |
| contrôle d'effet | `/health`, **jamais `/info`** : il prouve en plus que la base est lisible (`db: true`), et que `last_tic_ts` **avance** |

⭐ Le témoin du préflight n'est pas décoratif : **sans lui, un `adco_valide` qui refuserait tout passerait tous les cas de refus**, et le boîtier cesserait de créer le moindre PDL sans que rien ne le signale. Un garde faux brûle une version aussi sûrement qu'un vrai défaut.

**Aucune migration, aucune table, aucune colonne.** Éprouvé 20 h sur un boîtier filaire et 4 h sur un boîtier LoRa ; bancs joués **sur les Pi Zero eux-mêmes**, pas seulement en CI.

### [0.9.18] — 2026-09-27

**Répare ce que 0.9.17 a cassé sur les boîtiers radio.** Le code de 0.9.17 était bon ; son script d'update avait deux défauts enchaînés.

> 🚨 **`pi-0.9.17` est brûlée.** Sa transition a été retirée de `compatibility.yaml` pour arrêter l'hémorragie, et remplacée par deux transitions vers 0.9.18 — une qui répare, une qui aligne.

**Le premier défaut : une garde qui ne joue jamais.**

```bash
if ! systemctl cat ben-tic-reader.service >/dev/null 2>&1; then … exit 0
```

`systemctl cat` réussit dès que le **fichier d'unité** existe — et l'image dorée le pose sur **tous** les boîtiers, radio compris. Tester l'existence d'un fichier ne dit rien sur ce qui doit tourner.

**Le second en découle.** Le script atteignait donc `systemctl restart ben-tic-reader`. Or cette unité est `static` : pas de `[Install]`, pas activée, c'est `check_network` qui la démarre d'après les **capabilities**. ⚠️ Et `restart` sur un service **arrêté le démarre**.

**Ce que ça a coûté, mesuré :**

| | |
|---|---|
| plantages du lecteur, 3 jours **avant** le tag | **0** |
| plantages dans les 9 h **après** | **2 538** |
| cycles d'update en échec | 53 |

Le lecteur filaire, démarré là où il n'a rien à faire, perd la course au GPIO de la LED contre `ben-radio` qui en est le propriétaire (`lgpio.error: 'GPIO not allocated'`), et systemd le relance sans fin. Le contrôle d'effet échouant, `device.json` n'était pas bumpé : l'update **rejouait toutes les 10 minutes**. C'est la mécanique exacte qui avait brûlé `pi-0.9.12`.

> ⭐ **La règle qui en sort, et elle vaut pour toute update future :**
>
> **Une update redémarre ce qui TOURNE. Elle ne démarre JAMAIS ce qui ne tourne pas.**
>
> Ce qui doit tourner est une décision de `check_network` à partir des capabilities. Une update n'a ni à la reprendre, ni à la contredire.

**Et la décision vient des capabilities, jamais du modèle.** `device.json.model` porte depuis la 0.8.0 un **label commercial** (« Radio », « Filaire »), pas un modèle technique — s'y fier avait déjà été supprimé en 0.9.12. Le script interroge `capabilities.py`, la même source que `check_network`. Capabilities illisibles ⇒ **aucun service n'est touché**.

Les trois modèles, dont le troisième que 0.9.17 aurait cassé aussi :

| capabilities déclarées | ce que 0.9.18 fait |
|---|---|
| `tic-uart` | redémarrage encadré, **seulement s'il tournait déjà** |
| `lora` + `lora-tic-receiver` | **arrête** le lecteur filaire si 0.9.17 l'a lancé |
| `lora` + `lora-tic-receiver` + `tic-uart` | les deux lecteurs coexistent, et `ben-radio` possède le GPIO |

**Pourquoi le contrôle d'effet de 0.9.17 ne l'a pas vu.** Il avait été éprouvé sur un boîtier **filaire**, où le lecteur tourne déjà : `restart` y redémarre au lieu de démarrer, et la garde inopérante ne se voyait pas. Le cas radio n'avait été exercé sur **aucune** cible.

> ⭐ **Un script « universel » doit être éprouvé sur les deux modèles, pas sur le plus favorable.** Et une garde qu'on n'a pas vue jouer est une garde qu'on suppose.

**La transition 0.9.17 → 0.9.18 ne fait rien, volontairement.** Les boîtiers filaires ont déjà le bon code ; les deux tags portent le même arbre pour le lecteur TIC. 🚨 Elle **ne redémarre pas** le lecteur : ça coûterait des secondes de mesures sur chaque boîtier pour rien, et rouvrirait la fenêtre de course au GPIO. Elle se contente de vérifier le checkout — un tag est une chose qu'on peut rater, et un fichier manquant doit être vu tant que rien n'a bougé.

---

### [0.9.17] — 2026-09-27

**Le lecteur TIC vérifie la parité au lieu de la jeter.** La protection arrivait jusqu'à nous, et on la mettait à la poubelle à l'entrée.

**Le symptôme.** Un boîtier du parc déclarait **quatre compteurs** alors qu'il n'en lit qu'un. Ce n'était pas quatre compteurs : c'était le même ADCO, corrompu d'un seul caractère, et à chaque fois sur le **bit 6** (`'0'` = 0x30 → `'p'` = 0x70).

**Pourquoi le checksum ne peut pas le voir.** Il vaut `(somme & 0x3F) + 0x20` : il ne voit la somme que **modulo 64**. Basculer le bit 6 ajoute exactement 64, et le masque jette la retenue.

```
ADCO 061947000000  -> checksum '2'
ADCO p61947000000  -> checksum '2'   ← IDENTIQUE
```

> 🚨 Invisible **par construction**, et ce n'est pas un défaut de la spec : le checksum doit tenir dans un caractère imprimable, donc six bits.

**La protection existait, on l'effaçait.** La TIC est en **7E1** : chaque caractère porte un bit de parité. Le port étant ouvert en 8N1, l'UART nous le remettait fidèlement en position 7 — et `& 0x7F` l'effaçait sans jamais le regarder. L'Arduino, lui, n'a jamais rien jeté (`SERIAL_7E1` depuis toujours) : deux lecteurs du même protocole, deux niveaux de protection, et c'est le moins protégé qui a produit les ADCO déformés.

**Logiciel, pas matériel — et c'est une mesure qui l'a décidé.**

| configuration | `INPCK` | résultat |
|---|---|---|
| 8N1 + masque *(l'ancien)* | non | 124 valides · 0 rejeté |
| 7E1 sans `INPCK` | non | 123 valides · 0 rejeté |
| 7E1 + `INPCK\|IGNPAR` | **oui** | 123 valides · 0 rejeté |

> ⭐ `pyserial` n'active **pas** `INPCK`, même avec `PARITY_EVEN`. Passer le port en 7E1 n'aurait **rien** changé tout en donnant l'air d'un correctif — le pire des deux mondes. Le contrôle logiciel, lui, ne dépend d'aucun drapeau, marche aussi sur mini-UART, **se teste sans matériel**, et laisse **compter** les rejets : ce que le noyau fait en silence.

**1. `tic_parite.py`, fichier nouveau.** `octet_valide()` vit à part parce que `main_uart.py` importe `RPi.GPIO` au chargement : tant que la fonction y était, « testable sans boîtier » était **faux**.

**2. Un octet hors parité condamne le GROUPE entier.** Jeter l'octet et garder le reste supposait que le checksum rattraperait l'amputation. Il ne peut pas : toute amputation dont les caractères retirés somment à un multiple de 64 le laisse **inchangé**.

```
2 espaces   2 × 0x20 =  64    ← et l'espace est le SÉPARATEUR des champs
4 zéros     4 × 0x30 = 192    ← et le zéro est dans tout INDEX
```

> 🚨 `HCHC 001000000` amputé de quatre zéros devient `HCHC 00100`, **même checksum**, et un **index faux** est écrit en base. Seul défaut de ce chantier qui fabriquait une donnée *erronée* plutôt que d'en perdre une.

**3. Le relevé n'accuse plus le compteur pour un défaut de la liaison.** Deux compteurs séparés : `parite` mesure le **bruit** et voit tout ; `parite_groupes` mesure l'**imputation** et ne compte que ce qui a coûté un groupe. Et `_cause_rejets` prend l'étiquette cherchée en argument pour répondre sur **elle**.

**Ce que ça coûte.**

| | µs/octet | % CPU à 9600 bd |
|---|---|---|
| masque seul *(l'ancien)* | 0,55 | 0,053 % |
| `bin().count()` | 5,82 | 0,559 % |
| table de 256 entrées | 2,24 | 0,215 % |

Le lecteur consomme 9 % de CPU : **+0,16 point**, dix fois moins en 1200 bauds.

**Ce qui l'éprouve.** Banc de **11 cas**, sans matériel. Dont le témoin qui manquait au chantier : une **vraie trame de production** encodée en 7E1 et rejouée cent fois → **1100 groupes gardés, 0 rejeté**. Dix cas prouvaient qu'on rejette ce qu'il faut, aucun ne prouvait qu'on ne rejette **que** ça.

> ⚖️ Chaque garde-fou a été saboté séparément — ce qui a révélé **trois défauts du banc lui-même**, dont un lanceur qui ne rattrapait que `AssertionError` : une autre exception **tuait la suite** en n'affichant que trois échecs sur onze.

Sur un boîtier en production, trois déploiements successifs : débit inchangé dans la plage **325-337 mesures/10 min**.

**Vocabulaire.** Les termes de la spec sont désormais tenus dans le code et les journaux : **trame** (`STX`…`ETX`) · **groupe** (`LF`…`CR`, l'unité qui porte *son* checksum) · **étiquette** · **séparateur** · **caractère** (ce qui circule, 7 bits + parité) · **octet** (ce que l'UART remet, 8 bits). 🚨 Les deux derniers ne sont pas synonymes, et c'est le cœur de cette version.

**Et le dépôt est public.** Les numéros de série réels de compteurs en ont été retirés : un ADCO identifie un foyer. Matricules **inventés**, pas tronqués — un ADS fait douze caractères numériques (§2.2) et une valeur amputée n'aurait plus la forme que les bancs vérifient.

---

### [0.9.16] — 2026-09-24

**Le boîtier entretient son propre certificat.** Jusqu'ici, remplacer un certificat voulait dire se déplacer. Cette version installe l'agent qui le fait seul, à répétition, sans personne sur place.

**Pourquoi maintenant.** Mesuré le 23/09 sur un iPhone 11 / iOS 26.5 — trois certificats portant **la même clé privée**, seul le certificat changeant :

| certificat | Android | iOS |
|---|---|---|
| CN seul, zéro extension, 10 ans *(le parc)* | ✓ | ✗ |
| CN + `subjectAltName`, 10 ans | ✓ | ✗ |
| CN + `subjectAltName`, **397 jours** | | ✓ |

> ⭐ **Une seule variable : la DURÉE.** Apple plafonne les certificats serveur à ~398 jours, **même avec une CA privée fournie par l'application**. Les certificats du parc font 3650 jours : inutilisables pour le futur HTTPS local, et impossibles à remplacer à la main tous les six mois.

**1. `ben_certd.py`, l'agent.** Réveil quotidien, `GET :8444/`, une directive, on obéit.

> ⭐ **Le boîtier ne se souvient de rien.** Aucun état local : il vit côté serveur. S'il tenait le sien, les deux pourraient diverger — et ce serait toujours le boîtier qui aurait tort, sans moyen de le savoir.

> **Gigue pleine** (`random.uniform(période/2, période)`), pas un intervalle fixe : sinon toute la flotte se réveille ensemble après une coupure de courant générale.

**Cinq gardes avant toute bascule, du moins cher au plus cher — et l'ordre compte :**

1. **pas déjà expiré** — en premier, parce que `openssl verify` contrôle les dates *dans* la validation de chaîne. Placé après, ce contrôle ne serait **jamais atteint** et un certificat expiré serait journalisé « chaîne invalide » : on chercherait un problème de CA à 3 h du matin alors que c'est une date. *Trouvé par le banc, pas par relecture.*
2. chaîne valide contre le `root-ca.crt` **du boîtier** ;
3. 🚨 **la clé publique correspond à `device.key`** — le seul garde qui évite la brique définitive. Un certificat qui ne correspond pas à la clé locale rend le boîtier **muet**, et sans mTLS il ne peut même plus signaler qu'il est cassé ;
4. le CN est bien le sien ;
5. ⭐ **une poignée de main mTLS complète avec le candidat**, pendant que l'ancien est encore en place. On ne *déduit* plus qu'il marchera : on l'a **utilisé**.

Puis écriture atomique (`os.replace`), ancien conservé à côté.

**2. `ben-certd.service`** — sans `[Install]`/`WantedBy` : c'est `ben-network-check` qui le lance, dans la **même branche que les lecteurs** (provisionné *et* en ligne). Ni `WatchdogSec`, ni `StartLimitAction=reboot` — un agent de certificat qui tombe ne doit **jamais** redémarrer le Pi.

**3. `/etc/sudoers.d/ben-certd`** — un verbe, une unité : `systemctl restart ben-publisher`.

> 🚨 **Pourquoi ce redémarrage est obligatoire.** Le publisher charge son certificat **une seule fois**, à la construction de son contexte SSL, puis tient une connexion persistante. Sans redémarrage il continuerait indéfiniment avec l'ancien — **sans la moindre erreur au journal** — et ben-api enregistrerait l'ancien certificat. Une divergence silencieuse, dans la table même qui pilote la bascule du parc.

> 🚨 **Et c'est le risque propre à cette version.** Un fichier malformé dans `/etc/sudoers.d` casse `sudo` **pour tout le monde**, sur un boîtier qu'on ne peut pas dépanner à distance. D'où `visudo -cf` sur la **source**, **avant** toute installation — vérifier après serait vérifier depuis l'intérieur du trou — mode `0440 root:root` (sudo refuse **en silence** un fichier accessible en écriture à autrui), puis `visudo -c` sur la configuration **globale**, celle que sudo relira réellement.

**4. `check_network.py` démarre `certd`** à côté du publisher — par le helper `_start()`, jamais un `subprocess` en dur.

> ⚠️ Les deux lignes qui l'avaient fait (le publisher, en 0.9.15) **échappaient au banc** `test_network_recovery.py`, qui stubbe `_start` et non `subprocess`. Le banc était **rouge depuis trois versions** sans que personne ne le voie : l'agent qui porte toutes les mesures vers le cloud était entré dans le chemin de démarrage **sans couverture**.

> 🚨 **Ce qui ne fait PAS échouer l'update** : un boîtier sans certificat, ou un cloud injoignable. `certd` a `Restart=always`, l'ancien certificat reste valide jusqu'à son échéance, et il n'y a **jamais** urgence à renouveler. Faire échouer l'update la ferait rejouer toutes les 10 minutes, pour toujours — la mécanique qui a brûlé pi-0.9.12.

Aucune migration, aucune table, aucune colonne : `certd` ne touche pas à la base. Universel, LoRa et filaire — entretenir son certificat n'est pas une propriété du matériel.

Côté boîtier documenté dans [`docs/pki-renouvellement.md`](./docs/pki-renouvellement.md). ⚠️ Ce dépôt est **public** : la politique serveur (durées, seuil, révocation) n'y figure pas.

✅ **Script exécuté tel quel sur ben-0003 le 24/09 à 11:50** — filaire, en 0.9.15, dépôt propre, sans l'agent ni l'unité ni le sudoers : l'installation fraîche a donc bien été exercée, geste sudoers compris. Code 0, tous contrôles verts.

Effet constaté dans la foulée, boucle complète en six secondes :

```
CSR déposé (202) — motif : durée 3650 j > 180 j (plafond Apple ~398 j)
   … signature côté opérateur …
certificat remplacé (ancien conservé en .bak-20260924-115145)
ben-publisher redémarré
acquitté auprès de ben-api (204)
```

Tâche fermée côté serveur, inventaire passé de 3650 à 180 jours.

### [0.9.15] — 2026-09-21

**Le boîtier pousse enfin ses mesures vers le cloud.** L'outbox `measurements.sent` existait dans le schéma **depuis le premier jour** — colonne et index posés en prévision, jamais écrits une seule fois. Cette version branche le tuyau.

Trois pièces indissociables :

**1. `ben_publisher.py`, le sender.** Lots de 500 points en mTLS toutes les 60 s, `sent=1` marqué **seulement après un 2xx**. Un seul curseur, qui avance **du plus ancien vers le plus récent** — le curseur à deux bouts avait été conçu, puis jeté : la rétention à 180 jours de la 0.9.14 a supprimé la contrainte qui le justifiait. Stdlib `http.client`, pas `requests`, absent de `requirements.txt`.

> ⭐ **Gigue TOTALE sur échec** : `random.uniform(0, min(2**n, 300))`, et non `min(2**n, 300)`. La seconde forme *synchronise* — deux boîtiers tombés ensemble reviendraient frapper ensemble. Vérifié en coupant le serveur le 21/09 : délais tirés 0, 2, 1, 12, 9, 40, 40, 93, 202 s.

> 🚨 **`pending_approx()` encadre par les rowid au lieu de compter.** `SELECT count(*) WHERE sent=0` prend **37 secondes** sur 5,4 M lignes de Pi Zero — et il était appelé après chaque lot.

**2. `ben-publisher.service`** — l'unité **n'était installée nulle part**. Ni `install.sh`, ni aucune update ne la posaient : l'étape 10 de `check_update.py`, « redémarrer le publisher après chaque mise à jour », ne faisait donc **rien** depuis son ajout.

> Pas de `WatchdogSec` ni de `StartLimitAction=reboot` : **un publisher qui tombe ne doit jamais redémarrer le Pi.** Les mesures continuent de s'accumuler dans l'outbox — c'est exactement à ça qu'elle sert. Leçon du verrou taint (0.9.12) : un garde-fou plus destructeur que la panne qu'il traite est un défaut, pas une protection.

**3. 🚨 La CA conforme — et elle est posée en premier.** La CA du parc (1712 o) n'a **aucune extension X509v3**. `openssl s_client` la valide sans broncher ; Python 3.13, dont `create_default_context()` active `VERIFY_X509_STRICT` par défaut, la **refuse** — avec un message qui égare :

```
CERTIFICATE_VERIFY_FAILED: Missing Authority Key Identifier
```

Le vrai motif n'apparaît qu'avec `openssl verify -x509_strict` : *« error 79: invalid CA certificate »*. Constaté sur ben-0001 le 19/09, au premier hello réel. La CA a été **réémise avec la même clé et le même sujet** : les 11 certificats de boîtiers déjà émis restent valides, vérifié un par un avant tout déploiement.

> ⭐ **Le garde-fou central** : avant de remplacer quoi que ce soit, le script exige que la **nouvelle CA valide le certificat de ce boîtier**. En poser une qui ne correspond pas le couperait définitivement du cloud, **en silence**. L'ancienne est sauvegardée horodatée à côté.

> 🚨 **Ce qui ne fait PAS échouer l'update** : un boîtier sans certificat, ou un cloud injoignable. Le publisher a `Restart=always`, il réessaiera seul. Faire échouer l'update la ferait **rejouer tous les 10 minutes, pour toujours** — la mécanique exacte qui a brûlé pi-0.9.12. Un contrôle d'effet ne vérifie **que ce dont l'update est responsable**.

Aucune migration, aucune table, aucune colonne : le publisher ouvre la base en **lecture seule**. Universel, LoRa et filaire.

✅ **Script exécuté tel quel sur ben-0003 le 21/09 à 20:11** — 0.9.14, filaire, portant encore l'ancienne CA, donc exerçant réellement le remplacement. Code 0, tous contrôles verts, effet constaté côté serveur dans la seconde : `hello OK`, puis `envoyé 500 · inséré 500`.

### [0.9.14] — 2026-09-18

**Le passé s'effaçait sous le chantier.** Le parc atteignait trois mois alors que `RETENTION_DAYS` valait 90 : le plus ancien jour d'historique disparaissait chaque jour, et il n'existait **nulle part ailleurs** — il n'y a aucune sauvegarde des bases embarquées. Mesuré sur ben-0001 le 18 septembre : 5 441 400 lignes couvrant **exactement** 90 jours (20/06 → 18/09). La purge mordait bel et bien.

L'ingestion cloud est en construction. La bâtir avant que le passé n'ait disparu était une course perdue d'avance : chaque semaine de chantier coûtait une semaine d'historique. **180 jours suppriment l'échéance au lieu de courir après**, pour le prix d'une constante.

Le coût est dérisoire — la base passe de 420 à ~840 Mo, pour 9,5 Go libres sur la carte de ben-0001. Ce n'est d'ailleurs pas l'espace qui limite, mais la pression mémoire sur les 512 Mo du Pi Zero et la taille du WAL : d'où 180 et non 365.

> 🚨 **Le piège de cette version : un argument par défaut.**
> ```python
> def prune(conn, retention_days: int = RETENTION_DAYS)
> ```
> En Python, un argument par défaut est évalué **une seule fois, à l'import du module**. Un lecteur déjà lancé garde la valeur qu'il avait au démarrage. **Changer le fichier sans redémarrer les lecteurs n'aurait strictement rien fait** — et l'update serait passée pour réussie. On ne s'en serait aperçu que trois mois plus tard, en constatant que rien n'avait changé.

Le préflight vérifie donc en **deux temps** : un `grep` sur la constante, puis un **import réel du module** pour lire la valeur effective — une redéfinition plus bas dans le fichier passerait le premier et pas le second. Un garde-fou d'espace disque refuse d'agir si la carte ne peut pas accueillir une base doublée plus 512 Mo de marge : échanger une perte d'historique contre une carte pleine serait nettement pire.

⚠️ **Ce que le contrôle d'effet ne peut pas prouver** : que la purge garde effectivement 180 jours, puisqu'elle ne tourne qu'environ une fois par heure. Il vérifie ce qui est vérifiable dans sa fenêtre — services actifs après redémarrage, `/health` répondant avec `db: true` — et **imprime la ligne exacte à surveiller ensuite** plutôt que de faire semblant.

🎁 **Le mouvement s'inversera.** Une fois le cloud alimenté, cette valeur pourra **descendre** à 7 ou 30 jours, et le boîtier deviendra plus rapide. Les 90 jours ne servaient qu'à être la seule copie existante.

Aucune migration, aucune table, aucune colonne. Universel (LoRa et filaire).

### [0.9.13] — 2026-09-11

> ⚠️ **Republie `pi-0.9.12`, brûlée le 10 septembre.** Le contenu livré est identique ; seul le **contrôle d'effet** final était faux. Il interrogeait `/info` — une route qui **n'existe pas** dans l'API locale (404). L'API était parfaitement saine (`active (running)`, `/health` en 0,15 s), tout le reste de l'update était appliqué, et le script échouait quand même : `device.json` non bumpé, update rejouée à chaque tick, **façade radio redémarrée toutes les dix minutes**. Le contrôle porte désormais sur `/health`, qui exerce `_device_info()` **et** une lecture de base — un `db: false` est traité comme un échec, là où un simple code 200 l'aurait masqué. **Un garde-fou faux brûle une version aussi sûrement qu'un vrai défaut** : un contrôle d'effet se vérifie sur la cible avant de faire signer le tag, au même titre que le code qu'il contrôle. Aucune transition `0.9.12 → …` n'existe : cette version n'a jamais été inscrite sur un boîtier.


**Un drapeau qui ne s'efface jamais pilotait un test qui recommence toutes les 90 secondes.** `radio_alive()` commençait par `kernel_died()`. Or un taint noyau est **permanent** — il ne se nettoie que par un reboot — tandis que ce test est **périodique** et que sa seule action corrective est un **restart de service**. Aucun restart ne nettoyant un taint, la boucle est sans issue **par construction** : un oops, n'importe où dans le système, condamne la façade radio à mourir indéfiniment.

Vécu sur ben-0001 le 5 septembre : oops à 14:35 dans le contexte d'un script tiers, sans aucun rapport avec la radio → **208 redémarrages en 6 h, 46 % des mesures perdues**, pendant que la radio acquittait à 130 ms et recevait l'émetteur TIC. Le garde-fou a détruit un service parfaitement sain. Le filet `StartLimitAction=reboot` existait bel et bien, et **il est passé à 3 s près** : un cycle de 103 s contre une fenêtre de 300 s pour trois démarrages.

La radio est désormais jugée **sur la radio** — son self-test SPI — et sur rien d'autre. Le taint garde son intérêt d'**indice** : signalé une fois au journal, sans jamais rien décider.

**Et le monolithe `ben-lora-receiver` disparaît.** Découpé en `ben-radio` + `ben-telemetry` en 0.9.1, figé à l'état 0.9.4, exécuté par aucun boîtier depuis le cutover 0.9.0. Il laissait derrière lui 35 Ko de logique dupliquée qui ne pouvait que diverger, et une unit que `install.sh` copiait sur **chaque device neuf** (`cp config/systemd/*.service`).

⚠️ **Ces références mortes ne dormaient pas : elles produisaient des défauts.**

- **Le désappairage ne fermait pas la base.** `local_api._unprovision` arrêtait « `ben-tic-reader ben-lora-receiver` ». Sur un boîtier LoRa moderne, **ce stop n'arrêtait rien** : la LED restait tenue pendant le flash d'au revoir, et surtout le `?wipe=1` supprimait une base que `ben-telemetry` gardait **ouverte en WAL** — elle se recréait dans la seconde. Wipe illusoire, exactement le défaut corrigé le 15/08 dans le script de preshipping et resté intact ici. La liste vient maintenant des **capabilities**, avec repli sur l'**union** de tous les services connus : au désappairage, en arrêter trop ne coûte rien (le boîtier s'éteint juste après), en arrêter trop peu casse le wipe.
- **Le repli par modèle démarrait le monolithe.** `check_network.READERS_BY_MODEL` était censé servir aux devices « pas encore migrés ». Il était mort **et** nuisible : mort, parce que tout boîtier arrivé en 0.9.x a franchi la migration 0.6.1 → 0.7.0 qui écrit les capabilities ; nuisible, parce que `device.json.model` porte le **label commercial** (« Filaire », « Radio ») depuis la 0.8.0 — la table ne matchait donc plus rien, on tombait dans la branche « modèle inconnu » et on lançait `ben-lora-receiver` au lieu de la façade radio. **Les capabilities sont la norme** ; sans elles il n'y a rien à démarrer, et on le dit en `ERROR` plutôt que d'inventer une liste par défaut.
- **L'ordonnancement de la LED avait un trou** : le `Before=` de `ben-led-release` nommait le monolithe, donc ne couvrait plus `ben-radio`/`ben-telemetry` — les pins pouvaient n'être libérées qu'après le démarrage des lecteurs.

> **Le répertoire `src/pi/lora-receiver/` reste, et c'est délibéré.** Il n'héberge plus de récepteur mais les **codecs partagés** — `frame_codec`, `curve_codec`, `secure_link` — importés en production par les deux services via `sys.path`. Le supprimer casserait la façade radio sur tout le parc. Un README l'explique désormais sur place ; le renommer est un chantier à part, pas un coup de balai.

Le banc `test_network_recovery.py` couvre maintenant **quels** agents démarrent : un `device.json` sans capabilities ne doit rien lancer du tout. Aucune migration, aucune table, aucune colonne. **Universel.** Restart des services des capabilities déclarées (le correctif taint vit dans `ben-radio`), **puis** `ben-local-api` — dans cet ordre, l'API ouvrant la base en lecture seule.

### [0.9.11] — 2026-08-19

**Un boîtier qui redémarre avant sa box restait bloqué en provisioning BLE, indéfiniment.** Le scénario n'a rien d'exotique : une coupure de courant, le boîtier reboote plus vite que la box, il ne trouve pas de réseau — et il n'en ressortait plus jamais.

`check_network.py` est un **oneshot** : il tranche une fois au boot, puis rend la main. Sur un device déjà provisionné mais sans réseau, il partait en provisioning BLE et **personne ne revenait tester**. Deux faits de structure l'enfermaient là :

- `ben-network-check.service` est `Type=oneshot`, `WantedBy=multi-user.target`, sans aucun timer ni aucun service qui le relance ;
- les agents de mesure n'ont **pas** de `[Install]`/`WantedBy` — ils ne démarrent **que** par un appel explicite de `_start_readers()`.

Résultat : LED violet-jaune, aucune mesure enregistrée, jusqu'à ce que quelqu'un débranche le boîtier. Une coupure de trente secondes pouvait coûter des jours de données.

⚠️ **Et aucun filet ne rattrapait ça.** On pourrait croire que `wifi-watchdog` — relance de NetworkManager toutes les deux minutes — sauvait au moins le réseau. Il n'est **installé nulle part** : `install.sh` ne copie jamais son script vers `/usr/local/bin` et n'active jamais son timer, et aucun `update.sh` ne le fait. Vérifié sur ben-0001 : script absent, timer `disabled`/`inactive`. La seule reprise réelle est l'autoconnect de NetworkManager, qui renonce après ses `autoconnect-retries` (défaut 4). Le déploiement de ce watchdog reste à faire, et c'est un chantier à part.

**Nouvel agent `ben-network-recovery`**, démarré par `check_network` dans la **seule** branche « déjà provisionné mais réseau KO ». Son cycle :

1. provisioning BLE pendant 5 min — le boîtier reste joignable par l'app, car une coupure peut aussi être un vrai changement de box et l'utilisateur doit pouvoir reconfigurer ;
2. **veille passive** pendant cette fenêtre, un ping toutes les 30 s. C'est le cas nominal : NetworkManager se reconnecte seul quand la box revient, et on le voit **sans avoir rien coupé** ;
3. fenêtre écoulée → on arrête le BLE et **la collecte démarre**, réseau ou pas.

Le déroulé est **linéaire, sans boucle ni compteur**. Une première version alternait N fois BLE ↔ retentative WiFi avant de renoncer ; c'était inutile, puisqu'on collecte **de toute façon** au bout de la fenêtre — la retentative ne décidait plus de rien. Il n'en reste qu'une relance `nmcli` best-effort juste avant la bascule, qui n'améliore que les chances d'avoir l'heure NTP juste dès le premier point.

**Un boîtier hors ligne n'est pas un boîtier inutile.** Il n'a pas besoin du réseau pour faire son travail : la base SQLite est **locale** et l'app lit l'API locale du device. Et surtout, un boîtier qui perd le réseau **en marche** continue de collecter — rien ne l'arrête, `check_network` est un oneshot déjà terminé. Refuser de collecter après un **redémarrage** était donc une **incohérence** : la même panne donnait deux comportements opposés selon qu'elle survenait avant ou après le boot. Passée la fenêtre, la mesure passe devant l'attente.

⚠️ **L'horodatage hors ligne est décalé, pas corrompu.** Le Pi Zero n'a pas de RTC, mais systemd restaure la dernière heure connue au boot et seulement **vers l'avant** — `System time advanced to timestamp on /var/lib/systemd/timesync/clock`, relevé dans le journal de ben-0001. Aucun point ne peut donc s'écrire dans le passé de la base. L'erreur vaut la durée de la coupure, et le retour du NTP la rattrape d'un saut en avant, laissant un trou dans les `ts`. En mode **standard** elle est même évitable : `meter_ts` porte déjà l'horodate du **compteur** sur 100 % des points — mesuré sur ben-0001, 3802/3802 sur la dernière heure, écart maximal de 8 s avec NTP. Caler l'horloge système dessus est un chantier à part.

> **Le BLE n'est pas sacrifié.** Démarrer les agents éteint le provisioning (`Conflicts` LED/GPIO), mais **chaque** boot sans réseau rejoue cette fenêtre : un simple débranchement rouvre 5 min de re-provisionnement. Le BLE étant le seul chemin de configuration hors ligne, il fallait garantir qu'il reste atteignable avant de rendre la mesure prioritaire.

Une session BLE en cours **retient la bascule** : on patiente tant qu'elle dure, sans quoi la radio se couperait sous un utilisateur en pleine configuration. L'expiration du drapeau (15 min) empêche en retour une session oubliée de retenir la collecte indéfiniment.

⚠️ **Pourquoi arrêter le BLE pour retenter le WiFi.** Sur Pi Zero W, la radio WiFi et BLE est **partagée**. C'est exactement pour cela que le rescan périodique avait été retiré du provisioner en 0.8.2 : il affamait le lien et faisait décrocher les téléphones. Une tentative WiFi pendant une session BLE reproduirait ce défaut à l'identique.

**Ce qu'on ne coupe jamais : une session BLE en cours.** Nouveau drapeau `provisioning_state`, un fichier dans `/run` — tmpfs, donc effacé à chaque boot, ce qu'on attend d'un état de session. Posé par le provisioner à `on_connect`, retiré à `on_disconnect`. Arrêter la radio pendant qu'un utilisateur saisit son mot de passe transformerait la récupération **en panne**. Le drapeau **expire** au bout de 15 min et `main()` l'efface à chaque démarrage du provisioner — que systemd relance à chaque déconnexion BLE — de sorte qu'un provisioner tué en pleine session ne puisse pas condamner la récupération au silence.

> **Le premier unboxing n'est pas concerné.** Un boîtier jamais provisionné n'a pas de connexion `ben-provisioned` : `check_network` part en BLE direct, sans récupération. Y rester indéfiniment est son mode **nominal**, pas une panne — l'alternance n'aurait aucun sens et rendrait le boîtier fuyant pendant l'unboxing.

**Au passage : une exclusion mutuelle qui était fausse.** `ben-ble-provisioner.service` déclarait `Conflicts=ben-tic-reader ben-lora-receiver`. Or le monolithe `ben-lora-receiver` a été **découpé en `ben-radio` + `ben-telemetry` en 0.9.1** : sur tout boîtier LoRa en capabilities, aucune des deux units listées n'est jamais active, et l'exclusion ne protégeait donc **plus rien** depuis. Le provisioner pouvait coexister avec la façade radio et se disputer le SX127x et la LED. Les deux units sont ajoutées ; les noms legacy restent pour les boîtiers non migrés. La même leçon vaut pour le code : la garde « un agent normal tourne déjà » de `network_recovery` est dérivée des **capabilities**, pas d'une liste de noms en dur.

Code (`provisioner/network_recovery.py`, `provisioner/provisioning_state.py`, `provisioner/check_network.py`, `provisioner/main.py`) + deux units systemd. **Aucune migration**, aucune table, aucune colonne. **Universel**, pas de gate. Banc de non-régression `provisioner/test_network_recovery.py`, **joué par l'update lui-même** : le défaut ne se manifeste qu'au boot, sans réseau, sans personne pour lire un journal — et compiler ne prouve rien sur une machine à états. Il couvre l'aiguillage de `check_network` (dont la garantie que le **premier unboxing** n'entre jamais en récupération), les quatre issues du déroulé, et l'invariant « jamais de reader démarré sur un provisioner actif ».

> **Effet différé, et c'est voulu.** Cet OTA n'atteint qu'un boîtier **en ligne**, donc en mode normal, donc dont la récupération n'a rien à faire. On installe les units et on recharge systemd : **aucun service n'est redémarré**, aucune mesure n'est perdue, et le correctif prend effet au prochain boot sans réseau — celui qui suivra la prochaine coupure.

### [0.9.10] — 2026-08-19

**`STGE` est nommé, décodé, et il porte la couleur Tempo de DEMAIN.** Le registre de statuts du mode standard (TLV `0x23`, 32 bits bruts) était jusqu'ici un champ inconnu de plus. Il est désormais interprété par `frame_codec.stge_couleurs()` et journalisé par `log_uncabled()` à chaque changement, sous forme lisible : `STGE='0x013A4401 jour=bleu demain=néant'`.

Ce champ résout l'énigme ouverte le 12/08 : **la couleur du lendemain n'existe nulle part ailleurs dans la TIC standard.** `NJOURF+1`, le candidat évident, renvoie au calendrier **fournisseur** — qu'EDF ne programme pas pour Tempo. Il vaut `0` en permanence, vérifié sur ben-0001 les 12, 13 et 14/08 alors même que le contrat Tempo était actif depuis le 13/08 à 06:00.

⚠️ **L'offset des bits vient d'une trame réelle, pas d'une documentation.** Deux sources publiques se contredisaient d'un bit — 24-25/26-27 contre 25-26/27-28 — et un décalage d'un seul bit fait dérailler la table entière. La capture du 14/08 (`data/tic-ben0001-20260814-1343.bin`, sha256 `18173ebf…`) donne `STGE=013A4401` : la convention **24-25 = jour** rend « BLEU », conforme au terrain et à l'API publique, et surtout **tous** les autres champs du registre tombent juste avec elle — index fournisseur = 2 concorde avec `NTARF=02`, sortie télé-info = standard, mode consommateur. C'est cette cohérence croisée qui tranche, pas l'autorité d'une source. Table complète dans `docs/tic-stge-capture-2026-08-14.md`.

**Ce que ça ne fait pas** : aucun stockage, aucune colonne, aucune exposition par l'API. On **observe d'abord** — on ignore encore si les bits 26-27 se peuplent, et quand : « néant » à 13:43 le 14/08 alors que l'API publique connaissait déjà la couleur du 15, là où la littérature annonce ~20 h.

> **Asymétrie à connaître.** Sur LoRa, `STGE` exige l'émetteur **≥ 0.1.8**, donc un reflash physique — il n'y a pas d'OTA sur AVR. En dessous, aucun TLV `0x23` n'arrive : sans effet, sans risque. Sur **filaire**, `main_uart.py` lit la TIC en direct et cet OTA suffit, sans intervention sur site. Un boîtier filaire en Tempo standard pourrait donc trancher la question de la couleur du lendemain **avant** le parc LoRa. Aucun n'est en service à ce jour — pi10jd75 est en historique, donc sans `STGE` — le code part **non éprouvé**, en attente d'un tel boîtier.

**L'anti-rollback d'index comparait tous les registres dans un seul casier.** Deux défauts, découverts sur ben-0001 après sa bascule en Tempo.

`active_name` vaut `None` pour **tous** les registres du mode standard — ils sont opaques, libellés par le `LTARF` fournisseur, sans table en dur — et c'est précisément lui qui servait de clé. Les dix registres Tempo partageaient donc un unique casier. Le registre 1 y avait déposé 15 415 362 Wh (le Linky ne remet pas ses `EASF` à zéro en changeant d'offre : tout l'historique pré-Tempo est tombé dedans), et le registre 2, né à ~4 000 Wh le 13/08 à 06:00, passait pour un rollback de quinze millions. Aggravant : la mise à jour de l'état vivait dans la branche `else`, donc l'alerte se **verrouillait** — une ligne toutes les 40 s, des heures durant.

Second défaut, plus sournois : **la clé ne survivait pas au JSON.** `None` se sérialise en la *chaîne* `"null"`, que `.get(None)` ne retrouve plus au rechargement — d'où deux clés `"null"` en double dans `lora-state.json`, et un garde-fou **amnésique** repartant de zéro à chaque redémarrage. Il avait donc l'air de fonctionner (plus d'alerte) alors qu'il ne protégeait plus rien.

Clé texte explicite désormais : `s<index_id>` en standard, le nom canonique en historique (`BASE`/`HCHC`/`BBRHCJB`… — il **existe** là-bas, et les boîtiers historiques l'ont déjà en base, migration `last_base` comprise). Le préfixe évite en prime la collision entre l'ère historique et l'ère standard d'un **même** boîtier : ben-0001 a fait cette bascule, son `BASE` et son registre standard n°1 ne désignent pas le même compteur physique. La mise à jour devient **inconditionnelle** — on signale une fois, on ne verrouille plus — et `load_state()` purge les clés `"null"` héritées, qui ne désignent aucun registre. La vraie défense contre un compteur étranger reste le garde ADCO.

**L'issue d'une commande descendante devient observable.** `ben-radio` publie désormais sur `ben/lora/tx/ack` le résultat de chaque commande émise : `{ts, to, cmd, cnt, hid, ack, rtt_ms, id}`, où `ack: false` signifie trois essais sans réponse.

`send_acked()` connaissait déjà le sort d'un ordre dès l'ACK RadioHead — à ~130 ms près — mais ne le disait **qu'au journal**. Un client du bus ne pouvait donc pas distinguer « ordre parti » de « ordre acquitté par la cible » : il affichait le même résultat dans les deux cas. La conséquence est structurelle et indépendante de tout actionneur : **une cible devenue sourde est indiscernable d'une cible qui marche**, et une panne datable à la seconde se transforme en plage d'incertitude de plusieurs heures. Le canal descendant n'avait aucun retour d'exécution ; c'est ce trou-là qui se ferme.

`id` est recopié **tel quel** depuis la demande reçue sur `ben/lora/tx` : c'est le client qui corrèle, la façade ne mémorise rien et reste *stateless*. Publication en `qos=0` sous `try/except` — un broker qui râle ne doit jamais faire échouer une émission radio qui a déjà eu lieu. Aucun abonné n'est requis : le topic est purement additif.

Pur code (`lora-receiver/frame_codec.py`, `ben-telemetry/ben_telemetry.py`, `tic-reader/main_uart.py`, `ben-radio/ben_radio.py`), **aucune migration**, aucune table, aucune colonne. **Universel** — pas de gate capability : un boîtier filaire ne reçoit simplement jamais le TLV `0x23`, et les deux autres correctifs le servent aussi. Redémarre `ben-radio`, `ben-telemetry` et `ben-tic-reader` ; `ben-local-api` n'est pas concerné.

### [0.9.9] — 2026-08-13

**`/registers` montrait des registres d'offres révolues.** `db.registers()` agrégeait **tout** le rollup sans borne temporelle : un registre vu une fois restait affiché à vie. Constaté sur ben-0001 — le registre BASE de l'ère **historique**, muet depuis sept semaines, trônait dans la carte Réglages à côté des registres Tempo, donnant l'impression de deux index « Base » concurrents.

Ce n'est pas un simple problème d'affichage. En mode standard, `index_id` vaut `NTARF` : une **position dans le calendrier du contrat**. Le même numéro désigne un registre **physiquement différent** d'une offre à l'autre — sur ben-0001, la position 1 valait le registre BASE cumulé à 15 409 856 Wh avant la bascule, et les heures creuses bleues après. Agréger par-dessus un changement d'offre, c'est donc fusionner deux compteurs distincts sous une seule ligne.

`registers()` se borne désormais au début de l'époque de contrat en cours (`contract_epoch`, posée en 0.9.8). Pour un boîtier n'ayant jamais vu de changement d'offre, `ts_start = 0` rend le filtre **neutre** — vérifié sans régression sur ben-0003 (historique HP/HC) et ben-0010 (historique Tempo), dont les bases n'ont même pas la table.

⚠️ La valeur affichée reste **brute, celle qui figure sur la facture**. Le Linky ne remet **pas** ses registres `EASF` à zéro en changeant d'offre : un registre fraîchement nommé « HC BLEU » peut donc traîner l'énergie accumulée sous l'offre précédente. C'est la vérité du compteur, pas une anomalie — documenté dans la docstring pour que personne ne « corrige » ça plus tard.

> Conséquence assumée : un registre jamais revu depuis le début du contrat disparaît de la
> liste. En Tempo, BLANC et ROUGE n'apparaîtront donc qu'à leur premier jour de la couleur.
> Sans donnée sous ce contrat, il n'y a rien d'honnête à afficher.

**Une trame TIC tronquée n'ouvre plus d'époque tarifaire.** À chaque ré-enregistrement de l'émetteur, ben-0001 recevait `CONTRAT='00'`, puis la vraie valeur sept secondes plus tard. Inoffensif jusqu'en 0.9.4 ; depuis l'arrivée de `contract_epoch` en 0.9.8, **chaque redémarrage d'émetteur** aurait ouvert une époque bidon, émis **deux** `changement_offre` (aller puis retour), et — combiné au correctif ci-dessus — vidé `/registers` : un registre de nuit comme HC BLEU aurait disparu une journée entière.

Cause côté émetteur : `readAndParseTIC()` sort de sa boucle `STX→ETX` sur **timeout** comme sur `ETX`, et rend `kept > 0` dans les deux cas. Une trame coupée après ses premières lignes livre donc un ADCO juste — `ADSC`/`ADCO` est en tête de trame — et un contrat qui ne vaut rien ; or la porte d'émission de la trame de boot ne testait que l'ADCO.

Le récepteur refuse désormais le contrat d'un boot dépourvu à la fois d'`ISOUSC` et de `PREF` : ni l'un ni l'autre signifie que l'émetteur n'a pas lu la TIC. Garde **volontairement redondante** avec celle de l'émetteur — le récepteur ne doit jamais faire confiance à ce qui arrive par radio, et lui part par OTA quand les émetteurs déjà posés garderont leur firmware des mois. Test de non-régression : `src/pi/ben-telemetry/test_boot_contrat.py`.

Pur code (`store/db.py`, `ben-telemetry/ben_telemetry.py`, `lora-receiver/main.py`), **aucune migration** — `contract_epoch` existe depuis 0.9.8. **Universel**. Redémarre le lecteur puis l'API.

### [0.9.8] — 2026-08-13

**Bandes de courbe non coloriées en mode STANDARD.** `_band_kind()` ne reconnaissait que les libellés **historiques** (« creus » / « plein »). En standard, `LTARF` est abrégé — « HP  BLEU », « HC  BLANC » — donc aucun des deux mots : tout retombait sur `base`, et **la courbe restait grise**, badge HC/HP compris. Le commentaire au-dessus de `HISTO_LABELS` annonçait pourtant la règle : *« le mot Creuses/Pleines dans le libellé pilote `_band_kind` »* — vrai en historique, faux en standard. On accepte désormais aussi le préfixe `HC`/`HP`. ⚠️ Ça ne touchait pas que Tempo : **tout contrat HC/HP en mode standard** était concerné. Correction rétroactive — les bandes sont calculées à la lecture depuis le rollup, donc l'historique se colore aussi, sans backfill.

**Bornes de contrat (`contract_epoch`) — un registre ne veut rien dire sans son époque.** En mode standard, `index_id` vaut `NTARF` : une **position dans le calendrier du contrat**, pas une signification absolue. Constaté sur ben-0001 la nuit du 12 au 13/08, à la seconde près : à 23:59:46 `index_id=1` valait « BASE » ; à 00:00:07 une trame d'identité annonce `CONTRAT='TEMPO'` ; à 00:01:11 le **même `index_id=1`** désigne désormais les heures creuses bleues. Et l'index ne fait même pas de saut — Enedis reporte le cumul (15 409 379 → 15 409 381), donc rien ne permet de détecter la rupture dans les valeurs.

Sans borne, la corruption était **imminente et invisible** : dès la capture du `LTARF` des heures creuses bleues, `resolve_label` aurait étiqueté « HC BLEU » **toutes** les bandes portant `index_id=1`, y compris les 2,7 millions de points de l'ère BASE — toute la courbe des mois précédents serait passée en indigo du jour au lendemain.

Nouvelle table `contract_epoch (pdl_index, ts_start, ngtf)` : une ligne par changement d'offre, écrite par `record_ngtf()` au moment où il le constate — la seule occasion de connaître la borne à la seconde. Les bornes existantes sont reconstituées **depuis les événements `changement_offre`** (datés, avec `avant`/`apres`), à défaut depuis le contrat courant. ⚠️ Table séparée et non lecture des événements : **un événement est un message, une borne est un fait** — l'utilisateur peut masquer ou supprimer une notification, ça ne doit pas changer le sens des données.

`resolve_label()` accepte désormais le contrat de l'époque ; `tariff_bands()` charge les bornes **une seule fois** puis résout en mémoire par dichotomie — appeler la base par tranche ferait 21 600 requêtes sur une fenêtre de 30 j — et le **contrat entre dans la clé de fusion** des bandes, sans quoi une bande enjamberait le changement d'offre et effacerait la période suivante. Aucune colonne ajoutée à `measurements` : le contrat est une **période**, pas une propriété de chaque point.

**Repli de libellé restreint.** `resolve_label` retombait sur « le libellé le plus récent tous contrats confondus » quand le registre n'avait pas de libellé sous le contrat courant — d'où un `index_id=1` devenu heures creuses mais affiché « BASE ». Ce repli ne vaut plus que si le contrat est **inconnu**, cas qu'il visait réellement (NGTF pas encore capté au démarrage). Mieux vaut aucun libellé qu'un libellé d'une autre offre.

Pur code (`store/db.py`), migration additive (une table). **Universel**. ⚠️ Redémarre **le lecteur PUIS l'API**, dans cet ordre : `contract_epoch` est créée par `db.connect()` en ÉCRITURE, et c'est cette même ouverture qui reconstitue les bornes — l'API locale, en lecture seule, ne peut ni créer ni amorcer.

> Aurait dû partir dans 0.9.7 — le défaut a été trouvé après la publication du tag, et un tag
> publié ne se réécrit jamais.

### [0.9.7] — 2026-08-13

**Couleur Tempo en champ explicite + événements sans action.** Pur code, aucune migration.

**(1) `tempo_color` sur `/live` et sur chaque ligne de `/registers`** — `bleu` / `blanc` / `rouge`, absent hors Tempo. Jusqu'ici la couleur n'existait **nulle part comme donnée** : elle était enfouie dans le texte de `tariff_label`, et l'app devait la retrouver par recherche de mot FRANÇAIS, en connaissant les **deux conventions** — historique « Heures Pleines Jours Rouges », standard « HP  ROUGE ». C'est exactement ce que la résolution côté serveur existe pour éviter (cf. `resolve_label`, chantier labels). Le boîtier le fait mieux et une seule fois : en **historique c'est purement déterministe** (`index_id` 5-6 bleu, 7-8 blanc, 9-10 rouge — aucun texte analysé), en **standard** c'est lu une fois par registre dans le `LTARF` déjà conservé par `tariff_labels`.

⚠️ **Aucune colonne ajoutée, aucune migration** : la couleur est une **interprétation d'`index_id`**, pas une donnée. La stocker sur `measurements` dupliquerait des millions de fois ce que la colonne voisine porte déjà — et le rollup, dont la clé primaire contient `index_id`, **ventile donc la consommation par couleur depuis le premier jour** ; il ne manquait que le nom à mettre dessus. Sur `/registers`, la couleur rend les index exploitables : « heures pleines — 340 kWh » ne veut rien dire en Tempo, où trois registres HP coexistent du simple au quintuple.

**(2) Les événements n'émettent plus d'`action`.** `record_ngtf()` envoyait `{"libelle": "Voir les tarifs", "route": "/dashboard"}`. Deux destinations essayées puis écartées : `/dashboard` ne montre ni tarif ni contrat, et `/settings` affiche bien la formule mais **l'événement dit déjà « passé de X à Y »**. Au-delà de l'utilité, c'est une question de couche : **une route est une notion de l'APP**, le firmware n'a pas à connaître sa navigation — et l'action étant **figée dans l'événement à sa naissance**, un remaniement des écrans casserait toutes les lignes anciennes. Répartition retenue : le boîtier fournit le FAIT et son texte (ce qui garde la compatibilité ascendante — une vieille app rend correctement un type qu'elle ne connaît pas), l'app déduit l'action du `type`. Le champ reste au contrat pour un futur backend, qui aura de bonnes raisons de pointer ailleurs qu'une route d'app.

**Universel** (LoRa et filaire), pas de gate capability. Seule l'API sert ces champs → restart `ben-local-api` uniquement, les lecteurs ne sont pas redémarrés (pas de trou de mesure).

### [0.9.6] — 2026-08-12

**`pdl_index` identifie un COMPTEUR (ADCO), plus un émetteur — + événements — + performances API.** Trois chantiers, tout en pur code.

**(1) Identité du compteur.** `pdl_index` était dérivé de l'**adresse LoRa**, flashée en EEPROM : elle identifie l'ÉMETTEUR et le suit s'il est reposé sur un autre Linky, ce qui empilait deux compteurs sous le même PDL. Pas théorique — reconstruire l'option tarifaire sur `ben0001.db` montrait **4 « changements d'offre » en juin-juillet** qui n'étaient que des déplacements de test, avec des index cumulés sans continuité entre eux. Nouvelles tables **`pdl`** (ADCO → `pdl_index`, à vie) et **`emitter`** (quel compteur au bout de quel émetteur), entretenues à la **trame de boot**, seule à porter l'ADCO. **Migration additive** : les boîtiers du terrain n'ayant vécu que sur UN Linky, lier l'ADCO courant au `pdl_index` existant est historiquement juste — aucune renumérotation, aucun backfill. `sources.json` reste la **graine** (le 1er PDL vaut TOUJOURS 0 — allocation explicite, jamais `AUTOINCREMENT` qui commencerait à 1) **et le repli** : après l'OTA le Pi redémarre mais **pas l'Arduino**, qui reste en `STREAMING` sans réémettre sa trame de boot ; sans repli le boîtier cesserait de stocker. En filaire, l'ADCO est dans chaque trame → résolution directe, `PDL_INDEX` n'est plus une constante. `/pdls` expose `adco`. Vérifié sur ben-0001 : redémarrage d'émetteur → tables peuplées, `pdl_index` conservé à 0, 34 s de trou (fenêtre `REGISTERING`), aucun faux événement.

**(2) Événements** (`event`, `GET /events`, en-tête `X-Ben-Last-Event` sur `/live`). Deux bascules annoncées : l'**offre** (`OPTARIF` en histo / `NGTF` en standard — lus **directement dans la trame**, l'émetteur réémet son identité dès qu'ils changent) via `record_ngtf()`, et le **mode TIC** historique↔standard (le mode n'est écrit nulle part : c'est la trame elle-même qui l'est, le lecteur le découvre par auto-détection) via `record_tic_mode()`. Write-on-change, 1re observation silencieuse, symétriques dans les deux sens, annonce immédiate. L'écriture de l'état n'est jamais conditionnée à l'émission de l'événement : la notification est un bonus, l'attribut du compteur est le travail principal. Le garde-fou ADCO du détecteur devient **structurel** grâce à (1) — un autre compteur = un autre PDL = pas de ligne antérieure = pas de faux événement. Côté app : rien encore (les événements existent sur le boîtier sans être affichés).

**(3) Performances — le plus gros gain, découvert en testant (1) et (2).** L'index de `measurements` est `(pdl_index, ts, papp)` : **toute requête ne filtrant pas sur `pdl_index` y est aveugle** et balaye la table. Second piège, contre-intuitif : SQLite n'applique son optimisation MIN/MAX que pour **un seul agrégat** — `SELECT MIN(ts), MAX(ts) … WHERE pdl_index=?` = 16,5 s, les deux requêtes séparées = 0,00 s. Mesuré sur ben-0001 (3,1 M lignes) :

| | avant | après |
|---|---|---|
| `/live` (polé toutes les 10 s par l'app) | 28,6 s | **0,10 s** |
| `/pdls` | 36,7 s | **0,06 s** |
| `/health` | 16,6 s | **0,99 s** |
| `levels.refresh_all` (profileur de niveaux) | 199,9 s | **0,55 s** |
| charge moyenne du boîtier | 1,96 | **0,16** |

Correctifs : `/live` prend `pdl_index=0` par défaut (**changement de contrat** — il ne renvoie plus un objet par PDL ; un client multi-PDL doit passer le paramètre) ; `/pdls` lit la liste des PDL dans `pdl`/`level_profile` puis fait ses `MIN`/`MAX` **séparés et filtrés**, et `points` (COUNT, cher même filtré, lu par personne) passe sur `?count=1` ; `prune()` supprime par PDL sur les 3 tables ; le talon (P15 de la PAPP sur 30 j) est calculé sur **`curve_rollup`** — ~21 600 tranches de 2 min au lieu de 1,76 M lignes — avec repli sur le brut si le rollup ne couvre pas la fenêtre. **Le talon ne bouge pas** : vérifié sur trois foyers (ben-0001 71→71, ben-0003 0→0, ben-0010 550→556, soit +1,1 % au pire) — un percentile bas ne se déplace pas quand on lisse sur 2 min, les creux durant bien plus longtemps. `n_samples` reste la somme des `papp_count` (échantillons bruts) pour ne pas décaler `MIN_SAMPLES` et le démarrage à froid d'un facteur ~120.

**⚠️ 0.9.5 brûlée.** Le tag `pi-0.9.5` porte exactement ce contenu mais n'a jamais été déployable : son `update.sh.sha256` manquait, et l'agent refuse d'exécuter un script dont il ne peut pas vérifier l'empreinte (`verify_sha256`) — après avoir pourtant validé la signature GPG du tag et fait le checkout. Échec propre et sans dégât (`device.json` non modifié, retry au tick suivant), constaté sur ben-0003. Un tag publié ne se réécrit jamais (les `git fetch --tags` des devices casseraient) → republication à l'identique en 0.9.6, avec la somme de contrôle.

**Divers.** `src/pi/lora-receiver/main.py` est marqué **DÉPRÉCIÉ** et figé à l'état 0.9.4 : remplacé par la façade `ben-radio` + `ben-telemetry` depuis 0.9.0 (cutover), plus aucun boîtier ne l'exécute. Aucune migration destructive : que des créations de tables et une colonne ; l'ancien code ignore les nouvelles. **Universel** (LoRa et filaire) : pas de gate capability, restart du lecteur **puis** de `ben-local-api` — dans cet ordre, l'API étant en lecture seule et incapable de créer le schéma.

### [0.9.4] — 2026-07-26

**Coloration HC/HP en historique Tempo/BBR (registres index 5-10).** `db.HISTO_LABELS` — la convention de libellé tarifaire en **historique** (l'histo ne porte pas de `LTARF`) — s'arrêtait à l'index 4 (Base / HC / HP / EJP). Les 6 registres **BBR/Tempo** (index 5-10 : `BBRHCJB`…`BBRHPJR`) tombaient dans le trou → `resolve_label(histo, 5..10)` renvoyait `None` → `_band_kind(None)="base"` → **l'app ne coloriait AUCUNE bande de courbe** (`_kindColor("base")=null → bande ignorée`), n'affichait **pas le badge HC/HP** live (`tariffKind` null), et `/live.tariff_label` restait `null`. Trou révélé par **ben-0010**, 1er device en Tempo réel (les historiques précédents = Base ou HC/HP, index 0-2, déjà couverts). **Fix** : on complète `HISTO_LABELS` avec les 6 registres BBR (« Heures Creuses Jours Bleus »…). Le mot « Creuses »/« Pleines » du libellé pilote `_band_kind` → couleur **HC (indigo) / HP (orange)** de la courbe **et** le badge live, et `/live.tariff_label` porte le libellé complet. Le **jour Tempo** (Bleu/Blanc/Rouge) reste dans le **texte seul** : le rendu tricolore n'existe pas dans l'app (chantier couleur Tempo séparé). Pur code (`store/db.py`), aucune migration. **Universel** (LoRa **et** filaire — un filaire Tempo a le même trou) : pas de gate capability, restart `ben-local-api`.

### [0.9.3] — 2026-07-25

**Unboxing rapide (PAPP/IINST au boot) + fix blink blanc bloquant.** (1) L'émetteur (arduino ≥ 0.1.6) embarque **PAPP + IINST dans la trame de boot** ; `frame_codec` les décode (`T_PAPP` int24 signé, `T_IINST` uint16) et `ben-telemetry` insère une **mesure immédiate dès le boot** (`record_measurement`, index NULL) → `/live` affiche la conso **au 1ᵉʳ boot**, sans attendre le streaming courbe (~40 s). En **standard** la conso/injection sort direct (papp net signé) ; en **historique** PAPP=0 en injection → `IINST` porte la production estimée (230×IINST), comme la courbe. Ligne papp-seul exclue des agrégations d'index (pas de fausse énergie). (2) **Fix blink blanc BLOQUANT** : le blink « discovery » de `ben-telemetry` faisait `sleep(2.0 s)` sous `_led_lock` ; le flash RF de `on_recv` (ben-radio) bloquait dessus **AVANT** `send_app_ack` → l'ACC applicatif partait trop tard → l'émetteur ratait sa fenêtre → **boucle de boots + flashs blancs à répétition**. Fix : blanc **2.0 → 0.2 s** + flash RF déplacé **après** l'ACK. Pur code (frame_codec + ben_telemetry + ben_radio), rétro-compatible (émetteur < 0.1.6 inchangé). Restart ben-radio + ben-telemetry. Gated `lora-tic-receiver`.

### [0.9.2] — 2026-07-23

**ACK applicatif crypto-vérifié des trames boot** (`ben-radio.send_app_ack`). Anti cross-talk multi-logement : plusieurs centrales partagent l'adresse LoRa serveur `0x20` et RadioHead ACK au niveau LIAISON toute trame adressée à `0x20` *avant* toute vérif de clé → une centrale voisine « volait » le boot et l'émetteur se croyait enregistré chez elle (il aurait émis ADCO/OPTARIF/abonnement à côté). Désormais la façade ne renvoie un ACK (`HMAC(K_mac, nonce)`) QUE si le MAC montant est valide (elle détient donc la clé du device), et cet ACK est lui-même un HMAC que seul le détenteur de la clé peut produire → l'émetteur (≥ arduino 0.1.3) ne s'enregistre QUE chez SA centrale. Purement code (`ben_radio.py`) : aucune dépendance/migration/unit. Rétro-compatible (émetteur < 0.1.3 non impacté). Validé multi-centrales (ben-0011 ACK / ben-0001 refus MAC), 2026-07-22. Gated `lora-tic-receiver`. ⚠️ Ordre : central 0.9.2 AVANT reflash émetteur 0.1.3.

### [0.9.1] — 2026-07-22

FACADE RADIO + FIX paho. La 0.9.0 (facade) oubliait python3-paho-mqtt (lib cliente MQTT) -> ben-radio crashe a l import -> crash-loop -> boucle reboot (incident ben-0011/ben-0003). Tag 0.9.0 immuable -> on corrige en 0.9.1 = MEME facade AVEC paho + garde-fou (abort AVANT cutover si paho absent -> monolithe preserve, pas de boucle). On REDIRIGE 0.8.7 direct vers 0.9.1 (plus par la 0.9.0 cassee). Decoupe monolithe -> ben-radio + ben-telemetry via bus MQTT local, canal commande volet chiffre, derivation cle par-device, adresse LoRa EEPROM, topic LED ben/led. GATED lora-tic-receiver.

### [0.8.7] — 2026-07-18

« Au revoir » au désappairage : /unprovision fait 3 flashs VIOLETS sur la LED RGB juste avant le poweroff (signal clair de départ). local_api._unprovision stoppe désormais les readers TOUJOURS (libère les pins LED + ferme la base) puis flash. Redémarre ben-local-api (tous modèles). Effet au prochain /unprovision.

### [0.8.6] — 2026-07-18

FIX récepteur : l'IINST (2e courbe histo) était ENVOYÉE par l'émetteur ET décodée par frame_codec, mais on_recv_curve ne la rangeait pas en base (boucle sur PAPP seulement) → colonne iinst NULL en histo/LoRa. On ajoute labels["IINST"]=iinst[i] dans la boucle de stockage. Redémarre ben-lora-receiver sur les devices lora. Sans effet en standard (pas de 2e courbe IINST).

### [0.8.5] — 2026-07-17

échelle de jauge RÉSOLUE CÔTÉ BOÎTIER : /live ne renvoyait le plafond OBSERVÉ (high-water mark) qu'inconditionnellement → juste après l'unboxing ce max ≈ conso courante → conso faible affichée ROUGE dans l'app. Fix local_api.py : plafond observé UNIQUEMENT si le foyer est CONNU (même gate que le niveau, levels.is_known : ≥200 pts, ≥2j, dynamique), sinon l'ABONNEMENT (PREF/ISOUSC, sinon 9000). L'app affiche le plafond tel quel (n'arbitre plus). Redémarre ben-local-api sur tous les modèles.

### [0.8.4] — 2026-07-17

2 fixes terrain : (1) lora-receiver — watchdog de SILENCE (RX vivant mais sourd : IRQ RX morte, SPI lisible → self-test aveugle), restart à backoff exponentiel 5min→×2→plafond 1h, persisté, reset à la 1re trame (incident ben-0010 17/07, 55 min de silence sans relance) ; (2) provisioner — garde anti-doublon on_connect (bluezero l'appelle 2× Connected+ServicesResolved sur tél lent → écrasait la reco couleurs → échec unboxing MIUI). Restart ben-lora-receiver sur les devices lora ; provisioner : effet au prochain unboxing.

### [0.8.3] — 2026-07-14

RETRAIT de l'agent de pairing (leurre) : il faisait bonder iOS → re-découverte post-bond → gel couleurs/verify au 1er unboxing + « Peer removed » au re-provisioning. Le vrai fix iOS = la MTU (0.8.2). No-op ; effet au prochain provisioning.

### [0.8.2] — 2026-07-14

fixes unboxing BLE (provisioner) : DEVICE_INFO compact (device.json>MTU iOS tronqué → app iOS KO), scan WiFi unique (rescan 30s affamait le lien BLE → décrochage Android), agent pairing Just Works (BlueZ 5.x/Service Changed). No-op pour un device en service (provisioner off) ; effet au prochain provisioning.

### [0.8.1] — 2026-07-11

fix agent OTA : clobber device.json (self-heal relabel + re-read avant bump)

### [0.8.0] — 2026-07-11

relabel model → commercial (Filaire/Radio) — TOUS les devices

### [0.7.0] — 2026-07-11

durcissement watchdog LoRa (capability-aware : has_cap lora ; wired skip)

### [0.6.1] — 2026-07-11

MIGRATION model → caps (0.6.1 = fix ownership du 0.6.0 buggé)

### [0.6.0] — 2026-07-11

Migration vers le flux d'update unique (`updates_caps`, keyé sur softwareVersion) — fin des chemins d'update par-modèle. Nouvel agent OTA capability-aware.

### [0.5.0] — 2026-07-10

Watchdog SELF-TEST RADIO (récepteur LoRa). Lecture périodique de REG_VERSION du SX127x (0x42 doit valoir 0x12) : si le SPI/radio est figé (incident ben-0001 09/07), le process cesse de pinguer sd_notify(WATCHDOG=1) → systemd restart le service (WatchdogSec=90 s). Distingue « radio HS » de « rien à recevoir » : le silence de trames (émetteur off) ne déclenche PAS de restart — VALIDÉ sur ben-0001 (émetteur-off 3,5 min, NRestarts=0 ; + test d'isolation du mécanisme sd_notify). État indexes/seq persisté → restart sûr. Unit : NotifyAccess=main, WatchdogSec=90, StartLimitIntervalSec=0 (restart infini, escalade reboot DIFFÉRÉE à un prochain palier). AUCUNE migration BDD. Transition = install unit + daemon-reload + restart ben-lora-receiver.

### [0.4.0] — 2026-07-07

Jauge bidirectionnelle CALÉE SUR L'OBSERVÉ + garde-fou raw. High-water mark d'INJECTION (level_profile.papp_inject_max_alltime) par PDL, symétrique du plafond conso : standard = -papp net (MESURÉ), histo = 230×IINST (ESTIMÉ, papp plancher à 0), maintenu au fil de l'eau (record_measurement/_batch) + BACKFILL one-shot À LA MIGRATION (reconstruit depuis ~3 mois de measurements — une requête gardée par l'existence de la colonne → jauge calibrée immédiatement, sans attendre l'accumulation). /live expose plafond + injectMax → l'app cale CHAQUE CÔTÉ de la jauge sur son propre max observé (échelle linéaire -injectMax→+plafond, 0 à sa vraie place ; courbe d'injection négative côté standard). /chart?raw=1 BORNÉ à 24 h (garde-fou perf : pas de scan brut multi-jours). ADD COLUMN idempotent, aucune migration destructive. Transition = restart ben-lora-receiver + ben-local-api.

### [0.3.0] — 2026-07-06

ROLLUP PAR INDEX — Phase 3 (côté LECTURE, exploite le rollup de 0.2.0). NOUVEL endpoint GET /chart : courbe RICHE {points, tariff_bands, source} — le serveur arbitre la source des points (rollup rapide sur vue large / brut au zoom, raw=1 force le brut) ; tariff_bands = zones tarifaires AUTO-DESCRIPTIVES {from,to,kind,index_id,src_standard,label} distinguant TOUS les tarifs (HC/HP, Tempo bleu/blanc/rouge, EJP), histo ET standard, jamais un parcours de points (§5). /curve reste INTACT (brut) → app courante inchangée. /consumption et /registers ACCÉLÉRÉS via le rollup (index_last, fallback brut) : mesuré ben-0003 ×85 (/consumption 3574→42 ms) et ×550 (/registers 28 s→52 ms), résultat EXACT au Wh (écart bord ≤ 1 tranche). STRICTEMENT ADDITIF : /curve/consumption/registers gardent leur forme (bascule interne transparente) ; /chart est nouveau. Aucune migration BDD. Cf. docs/rollup-par-index.md §5/§6. Transition = restart ben-lora-receiver + ben-local-api.

### [0.2.0] — 2026-07-06

ROLLUP PAR INDEX — phases 1+2 (côté ÉCRITURE, PUREMENT ADDITIF : aucune lecture ni champ API changé → zéro régression, invisible). Nouvelle table curve_rollup = résumé pré-agrégé par (tranche 2 min, tarif index_id) : min/max/sum/count/index_last. Alimentée AU FIL DE L'EAU (record_measurement / record_measurements_batch) + BACKFILL progressif de l'historique (newest-first, 1 jour/pas, borné ~2 s, greffé sur prune(), REPRENABLE via curseur watermark persistant rollup_state, idempotent). Prépare la perf /curve + les bandes HP/HC (Phase 3). Schéma créé au 1er db.connect (CREATE TABLE IF NOT EXISTS), aucune migration destructive. Cf. docs/rollup-par-index.md. Transition = restart ben-tic-reader + ben-local-api.

### [0.1.0] — 2026-07-06

PALIER 0.1.0 (fin des 0.0.x — unification des versions Pi + Arduino). Récepteur LoRa : décodeur de trame EXTRAIT dans frame_codec.py (module pur testable, réutilisé par le banc de test) — main.py délègue à frame_codec.decode. Porté par le codec : DÉCHIFFREMENT ChaCha20 (encrypt-then-MAC, bit7) + décode IINST 2e courbe (histo). ALIGNEMENT du logging avec le lecteur wired : les champs collectés-mais-non-stockés (DEMAIN/ADPS/PEJP, NJOURF/NJOURF+1) sont logués INFO ON-CHANGE via log_uncabled (même format des deux côtés). STRICTEMENT ADDITIF : aucune migration BDD, aucun champ API retiré/renommé. Rétro-compatible : frame_codec décode aussi les trames NON chiffrées (émetteur < arduino 0.1.0). Transition = restart ben-lora-receiver + ben-local-api.

### [0.0.54] — 2026-07-03

Chantier « unification labels + contrat ». Récepteur LoRa : décode l'EXT COURBE v2 (flag bit7) = EAIT + LTARF (label tarif standard) + DIAG index=0 ; contrat NGTF (standard) / OPTARIF (histo) dans la trame boot v0x01 (octet 15 = longueur, 16.. = ascii ; borne relâchée 15..32 o). Résolution de label UNIFIÉE côté serveur (resolve_label : standard=LTARF autoritatif via tariff_labels / histo=convention statique HISTO_LABELS) + API : /live.tariff_label + /live.contract + nouvel endpoint /registers (registres + index par tarif + contrat). Stockage : table tariff_labels keyée (pdl,src,index_id,ngtf) — segmentée par CONTRAT — + colonne level_profile.ngtf. STRICTEMENT ADDITIF : aucun champ /live retiré ni renommé → app legacy inchangées, wired histo intact. MIGRATION idempotente au 1er db.connect (CREATE tariff_labels avec la nouvelle PK + ALTER level_profile.ngtf ; drop/recreate tariff_labels si ancienne PK). Transition = restart ben-lora-receiver + ben-local-api. NB : les nouveaux champs restent dormants tant que l'émetteur n'émet pas LTARF/contrat (arduino ≥ 0.0.9) — rétro-compatible (trames actuelles décodées à l'identique).

### [0.0.53] — 2026-07-02

MIGRATION backfill index générique legacy. db.connect() dérive index_id/index_value de tariff + base/hchc/hchp pour les vieilles lignes histo (index_value NULL, d'avant que le reader ne peuple la générique en pi-0.0.43). Sans ça /consumption fait COALESCE(index_value,base,hchc,hchp) qui écrase HC et HP dans une seule colonne → sur-comptage (~+30 % mesuré sur legacy HC/HP, ben-0003 : 43486 au lieu de 33252 Wh sur 7j). ONE-SHOT via user_version, idempotent. UPDATE en masse au 1er db.connect du reader après MAJ (~qq s ; ben-0001 ~315k lignes) → démarrage un peu plus lent cette fois-là. Données non perdues, index_value peuplé. Transition = restart ben-lora-receiver + ben-local-api.

### [0.0.52] — 2026-07-02

Deux ajouts Pi-side, low-risk. (1) CHECKPOINT WAL périodique : db.prune() fait PRAGMA wal_checkpoint(TRUNCATE) (~1×/h) → le fichier -wal, qui ne se tronque jamais seul (observé 392 Mo sur ben-0001 → ralentit toutes les lectures), reste borné. Maintenance pure, aucune donnée modifiée. (2) INSTRUMENTATION index=0 : le récepteur logge un WARNING greppable INDEX0 (batch_seq/NTARF/rssi/snr/gap) quand l'émetteur envoie un keyframe index_value=0 → capture le contexte de la cause racine (carry-forward EASF empoisonné côté Arduino, à corréler). AUCUNE migration. Transition = restart ben-local-api + ben-lora-receiver.

### [0.0.51] — 2026-07-01

Robustesse aux index_value=0 parasites. Un index compteur cumulatif n'est JAMAIS 0 ; des index_value=0 (carry-forward EASF empoisonné côté émetteur — cause racine à confirmer, cf. chantier Arduino) faisaient renvoyer par /consumption l'index ABSOLU (MAX-MIN, ~15 MWh → coût délirant, ex. 2957 €/jour). db.py : /consumption filtre COALESCE(...) > 0 (au lieu de IS NOT NULL) ; écriture (_generic_cols) normalise index_value=0 → NULL (donnée BRUTE propre : courbe, /measurements, futur cloud). AUCUNE migration (garde en lecture + normalisation des nouvelles écritures ; les 0 déjà stockés restent mais /consumption les ignore). Transition = restart ben-local-api + ben-lora-receiver.

### [0.0.50] — 2026-07-01

Chantier index énergie bi-mode, Lot B (suite) : endpoint /consumption. Le carry-forward (conso PAR REGISTRE sur une plage) est calculé SERVER-SIDE — Pi maintenant, cloud plus tard, MÊME contrat → l'app est agnostique du backend, logique non dupliquée. db.py : consumption(pdl,since,until) → {by_register:[{src_standard,index_id,wh}],total_wh} ; par registre MAX(index)-MIN(index) (index monotone → exact, immunisé au saut de registre ; COALESCE index_value/base/hchc/hchp → bi-mode + legacy histo). local_api : GET /consumption?pdl_index&since&until. L'app applique le prix (Σ wh×prix moyen aujourd'hui ; par registre à terme = coût à l'euro près, rétroactif, sans changer le contrat). AUCUNE migration (lecture seule sur colonnes existantes). Endpoint ADDITIF (app pas-à-jour intacte). Transition = restart ben-local-api + ben-lora-receiver.

### [0.0.49] — 2026-07-01

Chantier index énergie bi-mode, Lot B (exposition API). /live, /measurements et curve_buckets exposent désormais l'index GÉNÉRIQUE (index_id, index_value, src_standard, inject_total) : le coût/conso manquait en mode STANDARD où base/hchc/hchp sont NULL et l'index vit dans index_value. /live ajoute le flag producer (injection constatée → jauge bidir soutirage/injection de l'app). PERF : index COUVRANT idx_meas_pdl_ts_papp (pdl_index,ts,papp) remplace idx_meas_pdl_ts → l'agrégation /curve se résout depuis l'index (point chaud du 7j). MIGRATION = création index couvrant + DROP ancien au 1er db.connect (schéma, idempotent ; ~qq s one-shot sur grosse base, NON destructif, aucune perte). Exposition API purement ADDITIVE (app pas-à-jour intacte). Transition = restart ben-local-api + ben-lora-receiver.

### [0.0.48] — 2026-06-26

Chantier ISOUSC STANDARD : calibrage de la jauge en mode standard via PREF (puiss. de réf., kVA — le standard ne fournit pas ISOUSC en A). db.py : colonne level_profile.pref (ALTER conditionnel idempotent) + record_pref/get_pref. lora-receiver : lit l'octet 14 de la trame boot v0x01 → record_pref (émetteur ≥ arduino 0.0.7 ; < 0.0.7 → octet 14 = 0, ignoré, rétro-compat). local_api : /live arbitre maxVa = standard ? pref×1000 : isousc×230 (+ expose pref). MIGRATION = ALTER pref (idempotente, non bloquante). Transition = restart ben-local-api + ben-lora-receiver. ⚠️ jauge LoRa standard nécessite AUSSI arduino 0.0.7.

### [0.0.47] — 2026-06-25

Désappairage : POST /unprovision ÉTEINT TOUJOURS le boîtier (poweroff), avec ou sans wipe (avant : reboot quand pas de wipe). Au prochain allumage, sans WiFi → mode configuration (BLE). Signal d'extinction uniforme. Modif local_api.py. AUCUNE migration. Transition = restart ben-local-api + ben-lora-receiver.

### [0.0.46] — 2026-06-24

Trame courbe LoRa v0x05 : HORODATAGE PAR POINT. Chaque point porte son intervalle réel (dt en secondes, varint) depuis le précédent au lieu d'un period_ds uniforme supposé → le récepteur reconstruit t[i] = t0 + Σdt (courbe fidèle quelle que soit la cadence, trames ratées incluses). En standard le dt vient de l'horodate compteur (instant de mesure, sans dérive) → meter_ts exact ; en historique de millis(). Décodeur curve_codec.py expose sample_dt_s ; anchor_timestamps/meter_timestamps cumulent les dt. period_ds conservé comme moyenne/hint. Compat v0x04 RETIRÉE (un seul émetteur en service). AUCUNE migration BDD (ts/meter_ts déjà présents). Transition = restart ben-local-api + ben-lora-receiver. ⚠️ déployer le Pi AVANT de flasher l'Arduino 0.0.6 (un récepteur < 0.0.46 rejette le v0x05 → « 20 attendus »).

### [0.0.45] — 2026-06-23

Unpair « supprimer les données » (POST /unprovision?wipe=1) : on STOPPE le reader avant le rm de la base (wipe propre, plus de base ouverte en WAL) puis on ÉTEINT le boîtier (poweroff) au lieu de reboot → prépa livraison béta (part hors tension, rallumé par le testeur pour provisionner via l'app). Unpair simple (sans suppression) → reboot (re-pairing BLE) inchangé. Modif local_api.py. AUCUNE migration. Transition = restart ben-local-api + ben-lora-receiver.

### [0.0.44] — 2026-06-23

LED de boot : remplace la séquence arc-en-ciel « disco » (~5 s, 9 couleurs) par 3 flashs bleus brefs au démarrage (aligné avec le wired). Cosmétique pur, AUCUNE migration. Transition = restart ben-local-api + ben-lora-receiver.

### [0.0.43] — 2026-06-23

Mode TIC STANDARD (Enedis-NOI-CPT_54E) bout-en-bout + dorsale stockage index bi-mode. Récepteur LoRa décode le v0x04 standard (papp NET SIGNÉ via src_standard, − = injection ; bloc ext EAIT ; horodate compteur → meter_ts, immunisée au délai radio) ; NTARF standard non mappé sur INDEX_NAMES (opaque) → clé générique (src_standard, index_id, index_value). Suppression du watchdog restart-sur-silence (inutile côté récepteur ; fraîcheur via heartbeat). MIGRATION BDD : ALTER ×5 (src_standard/index_id/index_value/inject_total/meter_ts), idempotente, NON bloquante, double-écriture base/hchc/hchp. /live expose tic_mode (standard/historique). Transition = restart ben-local-api + ben-lora-receiver. ⚠️ déployer le Pi AVANT de flasher l'Arduino standard (arduino 0.0.4).

### [0.0.42] — 2026-06-18

Chantier ISOUSC : le récepteur (wired ET lora) lit l'intensité souscrite et la stocke par PDL (level_profile.isousc, write-on-change). LoRa : ISOUSC dans la trame d'identité v0x01 (octet 13). /live expose isousc + maxVa (=ISOUSC×230) pour les réglages app + l'étalonnage de la jauge. Migration BDD = ALTER conditionnel (colonne isousc), idempotent. Transition = restart ben-local-api + ben-lora-receiver.

### [0.0.41] — 2026-06-18

Courbe LoRa v0x04 : le récepteur décode la trame v0x04 (courbe PAPP batchée ~2 s, keyframe + deltas varint, HMAC-8) en plus de v0x02/v0x01. Réutilise db.py/local_api.py de 0.0.40 (curve_buckets + /curve agrégé) → /curve et /measurements identiques au wired. Saut 0.0.39→0.0.41 (0.0.40 = tag wired). Transition = restart ben-local-api + ben-lora-receiver, AUCUNE migration BDD. ⚠️ déployer le Pi AVANT de reflasher l'Arduino en 0.0.2 (un Pi 0.0.39 rejette le v0x04).

### [0.0.40] — 2026-06-13

Courbe temps réel (pi-wired). Lecture TIC AU FIL DE L'EAU (suppression du sleep 15s, event-driven sur la trame → courbe fine ~1,5s au lieu de 1/15s) + écritures BDD BATCHÉES (1 commit/15s via executemany → ménage la SD malgré ~7× plus de points). API : nouvel endpoint /curve (agrégé min/max/avg, bucketing ABSOLU + quantifié pour stabilité au pan, centroïde temporel, index porté) ; /measurements degrade-safe (agrège au lieu de tronquer) ; /pdls expose first_ts. db.py/local_api.py partagés mais ADDITIFS (pas de changement de schéma, reader LoRa intact) → seuls les wired bumpent. AUCUNE migration. update.sh = restart ben-tic-reader + ben-local-api.

### [0.0.39] — 2026-06-08

Provisioning BLE — 2 corrections UX de la LED. (1) Échec WiFi (ex. mauvais mot de passe) : le BLE reste connecté et l'utilisateur peut re-saisir → set_status() ne rejoue PLUS le blink violet/jaune (« aucun téléphone, à configurer ») qui laissait croire à un reset ; on garde les 3 flashs rouges (échec) puis on ÉTEINT (état « connecté, en attente de saisie »). (2) Fin de l'apprentissage des couleurs (PREVIEW_CMD='0') : on arrête la boucle + éteint, puis on attend VERIFY_AFTER_PREVIEW_SEC (3 s) avant de démarrer le code de test, le temps d'arriver sur l'écran de test sans rater le début. Provisioner on-demand → rien à redémarrer, pas de reboot.

### [0.0.38] — 2026-06-08

Provisioning BLE — phase d'APPRENTISSAGE des couleurs avant le test d'association. led.py : la boucle de séquence accepte un callback (on_show + tokens) qui notifie la couleur affichée. main.py : 2 caracs GATT — PREVIEW_CMD (…0007, write '1'/'0') et PREVIEW_COLOR (…0008, read|notify B|Y|W|R|-). '1' joue les 4 couleurs en boucle (ordre fixe BYWR) en notifiant la couleur courante (l'app surligne la pastille en synchro) ; '0' (bouton Suivant) arrête et enchaîne sur le code de test. Repli : si l'app ne pilote pas PREVIEW, le code de test s'affiche après VERIFY_DISPLAY_DELAY_SEC comme avant. Durée d'affichage de chaque couleur du CODE de test allongée (VERIFY_ON_SEC=1.3 s) pour laisser le temps de lire. Provisioner on-demand → pris au prochain mode BLE, rien à redémarrer, pas de reboot. Côté app : nouvel écran « Repérez les couleurs » entre le scan et le test.

### [0.0.37] — 2026-06-08

API locale : POST /unprovision corrigé. Bug : on supprimait la connexion WiFi AVANT de répondre → couper `ben-provisioned` tue le lien TCP, l'app ne recevait jamais l'ack (et le diagnostic était aveugle car l'API silence log_message par défaut). Fix : on répond d'abord, PUIS désappairage + reboot en ASYNCHRONE (threading.Timer 2 s) ; on oublie TOUTES les connexions WiFi (ben-provisioned + éventuel profil opérateur `wifi` du golden, qui sinon reconnectait en autoconnect) ; ajout de logs « [unprovision] … ». update.sh : restart ben-local-api ; pas de reboot.

### [0.0.36] — 2026-06-08

(1) Palette LED de vérif BLE recalibrée (led.py) : BLANC CHAUD (canal bleu écrasé, B≈0,25×R) car la LED bleue est perceptuellement plus vive → le blanc virait au bleu (confusion blanc/bleu constatée au provisioning). Palette daltonien conservée (axe bleu↔jaune + luminosité, sans vert). Provisioner on-demand → pris au prochain provisioning. (2) API locale : POST /unprovision (désappairage) — supprime la connexion WiFi `ben-provisioned` (→ provisioning BLE au reboot), efface optionnellement les données (?wipe=1), puis reboot ; garde l'identité (certs, deviceId). SANS auth (raccourci assumé : LAN + confirmation app). update.sh : restart ben-local-api ; pas de reboot. L'app ajoute une carte « Désappairer » (Avancé) avec option wipe.

### [0.0.35] — 2026-06-08

/health expose lastUpdateTs (epoch s) = date de la dernière MAJ firmware, lue via la mtime de device.json (réécrit seulement à un changement de version OTA ou au provisioning). local_api.py : _device_info() ajoute le champ. Pas de champ stocké, pas de migration, aucun reader touché. update.sh : restart ben-local-api ; pas de reboot. Permet à l'app d'afficher « Mis à jour le … » et de repérer un device en retard.

### [0.0.34] — 2026-06-08

Modèle de niveau « course [talon, plafond] » (remplace les percentiles P30/P70/P90, qui dégénéraient sur les foyers à faible base : un frigo seul ~150 W était sur-noté niveau 3, vu en prod sur ben-0003/Neuville). niveau = position de la PAPP entre le talon (P15, ancrage bas) et le plafond = papp_max_alltime (high-water mark MONOTONE de la PAPP, jamais décrémenté, survit au prune 3 mois ; « si c'est arrivé ça arrivera encore »). ratio=(PAPP−talon)/(plafond−talon), bandes 0,10/0,40/0,70. db.py : colonnes level_profile.talon + .papp_max_alltime (maintenu par record_measurement) + index idx_meas_pdl_papp (ALTER/index auto). levels.py : talon en SQL, cold-start (ou plafond<=talon) → niveau 2. Plus aucun seuil absolu en watts : bornes tirées des données du foyer → adaptatif ET non-dégénéré. update.sh : backfill one-shot du plafond depuis l'historique + restart ben-local-api + reader + recalcul talon ; pas de reboot. Firmware only (l'app lit déjà reading.level).

### [0.0.33] — 2026-06-07

Provisioner BLE — feedback succès connexion réseau : sur succès WiFi, 2 flashs verts rapides au lieu de 3 flashs + vert tenu permanent (cohérence UX, plus de vert fixe). N'impacte que le provisioning BLE ; rien à redémarrer (provisioner on-demand).

### [0.0.32] — 2026-06-07

Provisioner BLE — feedback connexion : à la connexion Bluetooth, arrêt du blink d'attente violet/jaune + 2 flashs verts rapides (= connecté), puis délai 10s, puis boucle du code couleur. N'impacte que le provisioning BLE ; rien à redémarrer (provisioner on-demand).

### [0.0.31] — 2026-06-07

Provisioner BLE — UX vérif couleur : code correct → 3 blinks verts rapides (au lieu du vert fixe permanent) ; délai de 10s avant d'afficher le code couleur à la connexion (blink violet/jaune d'attente pendant le délai, puis le code se répète au rythme normal ~1,5s). + ben-led-release.service : flash blanc 'welcome' rendu atomique (3 canaux R/G/B en une seule commande pinctrl) — avant, 3 commandes séparées faisaient voir un balayage rouge→jaune→blanc→cyan→bleu au boot. Migration : réinstalle ben-led-release.service + daemon-reload (effet au prochain boot) ; provisioner on-demand donc rien d'autre à redémarrer.

### [0.0.30] — 2026-06-07

cf. note pi0-wired 0.0.30 — tic-reader wired (PERIOD 15s + wake-up bleu) ; sur lora pur = no-op (try-restart d'un service inactif).

### [0.0.29] — 2026-06-07

Fix race de services au provisioning. ben-tic-reader / ben-lora-receiver perdent leur autostart (WantedBy retiré) : ils sont désormais lancés UNIQUEMENT par check_network.py quand le réseau est up. Avant, ils démarraient au boot en doublon et tuaient ben-ble-provisioner via Conflicts → bug 'code couleur affiché puis ré-écoute BLE en boucle', provisioning impossible (vu en démo sans Linky). De plus check_network détecte 'jamais provisionné' (absence de connexion ben-provisioned) → bascule BLE directe sans pinguer 30s au premier boot. Migration : réinstalle les units + systemctl disable des readers. N'impacte pas un device en service (le reader tourne jusqu'au prochain reboot où check_network le relance).

### [0.0.28] — 2026-06-05

Vérification couleur au provisioning BLE (confirme le bon boîtier). led.py : palette daltonien (B/J/Blanc/Rouge, sans vert) + start_sequence. main.py : GATT VERIFY/VERIFY_STATUS, hook on_connect qui génère un code couleur affiché sur la LED, WIFI_CONFIG refusé tant que non vérifié. ⚠ CASSANT : nécessite l'app avec l'étape VERIFY pour provisionner ; n'impacte QUE le provisioning BLE (un device en service en mode normal n'est pas affecté). Code en place via checkout ; aucun service à redémarrer. Cf. docs/ble-color-verification.md. (NB : pi-0.0.27 avait été taggé sans son update.sh.sha256 → OTA en échec, jamais appliqué ; 0.0.28 = même contenu + checksum, transition directe 0.0.26→0.0.28.)

### [0.0.26] — 2026-06-05

Tarif HC/HP exposé par l'API. Colonne `tariff` sur measurements (index tarifaire actif : 0=BASE, 1=HC, 2=HP, 3+=EJP/Tempo ; migration ALTER auto au prochain connect écriture) remplie par record_measurement (PTEC côté wired, index actif de la trame côté LoRa). /live et /measurements ajoutent `tariff`. L'app affiche HC/HP : chip sous la jauge (🌙 indigo / ☀️ orange) + zones de fond pastel dans la courbe avec pointillé aux transitions. update.sh redémarre ben-local-api + le reader ; pas de reboot.

### [0.0.25] — 2026-06-05

Niveau de consommation 1..4 exposé par /live (visuel app). Nouveau module store/levels.py : percentiles PAPP du foyer (P30/P70/P90 sur 7 j glissants) → niveau 1..4, lissé sur 2 min + hystérésis ; défaut 2 en cold-start (< 2 j d'historique). Table level_profile (auto-créée par db.connect). Service planifié ben-level-profiler.{service,timer} : recalcul des seuils 1×/jour (SoC : seul ce job écrit, l'API reste read-only). update.sh installe les 2 units + redémarre ben-local-api + arme le timer & 1er calcul ; pas de reboot.

### [0.0.24] — 2026-06-04

FIX MAJEUR sink SQLite multi-thread (db.py check_same_thread=False) : le récepteur LoRa ouvrait la connexion dans le thread principal mais écrivait depuis le thread RX → CHAQUE écriture échouait silencieusement (avalée en WARNING), le store LoRa n'avait JAMAIS écrit une ligne (constaté sur ben-0001 : frames reçues OK, /pdls /live /lora-link vides). Le wired n'était pas touché (écrit depuis son thread principal). + API locale : GET /ping → {ben:true} (zéro I/O, polling régulier / voyant app) et /health renvoie `last_tic_ts` (ts dernière trame TIC). update.sh redémarre ben-local-api + le reader, pas de reboot.

### [0.0.23] — 2026-06-04

Réglages utilisateur via l'API locale : luminosité LED par crans (led_level 0-5, 0=éteinte), mapping perceptuel gamma. settings.py (store partagé) + GET/POST /settings ; la couche LED (led.py + blink_rgb readers) applique le cran. bypass=True (décidé par l'appelant) pour erreurs + provisioning → toujours visibles même LED éteinte. Résilient à l'absence de settings.json (defaults en code). Inclut le RATTRAPAGE de l'annonce mDNS avahi _ben._tcp (avait raté la 0.0.22 publiée — commit amendé après push) : update.sh l'installe.

### [0.0.22] — 2026-06-03

Phase 1 — store local SQLite (rétention 3 mois glissants) : les readers écrivent chaque trame (conso + qualité LoRa rssi/snr dans une table à part lora_link). Nouvelle API locale read-only :8087 (ben-local-api.service) lue par l'app via l'IP du device. Colonne `sent` = outbox prête pour le futur push cloud.

### [0.0.21] — 2026-06-03

BLE provisioner remonte l'IP locale du device en suffixe du statut (connected:<ip>) → l'app peut se connecter directement sur le LAN après le provisioning (mode proto). Code provisioner uniquement, transition no-op.

### [0.0.20] — 2026-06-01

restaure gpio=13=op,dh dans config.txt (pi-0.0.17 l'avait stripé) → boot indicator vert revient. ben-led-release.service modifié : éteint le boot indicator puis fait un flash blanc 150ms de welcome avant de release les pins.

### [0.0.19] — 2026-06-01

fix 0.0.18 : retire Before=basic.target de ben-led-release.service (cassait le cycle multi-user.target ↔ basic.target). Service tourne enfin au boot, LED check_network fonctionne.

### [0.0.18] — 2026-06-01

fix 0.0.17 : installe ben-led-release.service (oneshot Before= tous les services BEN qui libère les pins via pinctrl). Transition 0.0.17 → 0.0.18. Bug latent : Before=basic.target dans l'unit crée un ordering cycle → systemd skip silencieusement le job au boot.

### [0.0.17] — 2026-06-01

tag pi-0.0.17 PUBLIÉ avec un update.sh foireux (strip gpio=13=op,dh du config.txt + reboot, perte du boot indicator). ben-0001 a effectivement OTA'd à 0.0.17 et a perdu le boot indicator. Voir 0.0.18 pour la fix.

### [0.0.16] — 2026-06-01

fix update.sh : suppression du redirect bash `> /var/log/...` qui faisait fail le script tournant comme `ben`. Transitions ajoutées 0.0.15 → 0.0.16 (no-op) pour aligner ben-0003.

### [0.0.15] — 2026-06-01

tag pi-0.0.15 publié avec .sha256 fix. La transition no-op 0.0.14 → 0.0.15 a réussi sur ben-0003. La transition 0.0.13 → 0.0.15 contenait un bug d'apt redirect (sudo n'élève pas le `>` shell, script tournait comme `ben`) → ben-0001 stuck à 0.0.13. Voir 0.0.16.

### [0.0.14] — 2026-06-01

BLE WiFi provisioning + boot-time network check — NEVER DEPLOYED (tag pi-0.0.14 publié mais update.sh.sha256 manquant).

### [0.0.13] — 2026-05-31

flashs RX jaune + HMAC OK vert discrets 50ms × 5/255 ; heartbeat rapproché (sleep 0.5s)

### [0.0.12] — 2026-05-31

heartbeat UX-driven : OK très court (50ms × 5/255), erreur visible (300ms × 8/255)

### [0.0.11] — 2026-05-31

SNR_MAX_PLAUSIBLE 12 → 20 pour tolérer les SNR close-range valides

### [0.0.10] — 2026-05-31

heartbeat LED encore plus discret (0.1s × intensité 5/255, séparés de 1s)

### [0.0.9] — 2026-05-31

heartbeat LED présence calme (0.4s × intensité 10/255)

### [0.0.8] — 2026-05-31

fix SPI + rpi-lgpio + WorkingDirectory + RPi.GPIO transitive uninstall + raspi_lora SNR sign

### [0.0.6] — 2026-05-31

LED 5% partout : jaune wake / vert trame OK / rouge trame KO

### [0.0.5] — 2026-05-31

LED silence radio — plus aucun blink (for sleeping operators)

### [0.0.4] — 2026-05-31

LED bonk-bonk : 2 flashs violet par cycle raté (remplace single-violet stale)

### [0.0.3] — 2026-05-31

agent LED: dim yellow wake (30%), violet stale alert (>5min); WATCHDOG 5min→10min

### [0.0.2] — 2026-05-30

log-only baseline (Influx stripped, hostname rename)

### [0.0.1] — 2026-05-30

first dev release (published, no devices in field)

## Émetteur Arduino (tic-reader)

### [0.1.8] — 2026-08-19

**`STGE` émis à chaque trame de courbe — et ça coûte 74 octets de flash EN MOINS.** Nouveau TLV `T_STGE` (`0x23`), le registre de statuts du mode standard transmis **brut**, en `uint32` little-endian. Il porte la couleur Tempo du jour (bits 24-25) et du **lendemain** (bits 26-27), plus la surtension, le dépassement de `PREF`, l'état de l'organe de coupure et le mode producteur. Interpréter côté AVR aurait coûté de la flash sans rien rapporter : le Pi a la place, il décide.

Le financement vient de `NJOURF`/`NJOURF+1`, passés derrière `#define SEND_NJOURF 0`. Ils désignent le calendrier **fournisseur**, qu'EDF ne programme pas pour Tempo, et valent `0` en permanence (vérifié les 12, 13 et 14/08). On échangeait donc 6 octets d'air contre du vide. **Désactivés et non supprimés** : ils redeviennent valides chez un fournisseur qui déclare un calendrier. Bilan flash : **30 154 → 30 080 o (98 % → 97 %)**.

⚠️ **`STGE` est en HEXADÉCIMAL dans la TIC**, huit caractères — `strtoul(val, 0, 16)`. En base 10 la valeur est silencieusement fausse, pas rejetée.

Émission **systématique**, pas sur-changement : il n'y a alors aucun état à synchroniser entre émetteur et récepteur — un Pi qui redémarre ne peut pas redemander la valeur courante — et une trame perdue est réparée au flush suivant, 40 s plus tard. Ce n'est pas théorique : ce boîtier a perdu 20-25 % de ses trames la semaine du 11/08. Coût réel : 6 octets sur 117, soit +5 %, exactement ce que libéraient les `NJOURF` désactivés.

**Nouveau sketch de diagnostic `src/arduino/tic-relay/`** — c'est lui qui a rendu la capture possible. Le Pro Mini n'a qu'un seul UART, et c'est celui qui lit la TIC : impossible d'ouvrir un second port pour observer. Mais l'UART est **full duplex** — on réémet sur `TX` ce qu'on reçoit sur `RX`, sans toucher au câblage ni sortir le fer. Le FTDI voit alors la trame telle que l'Arduino la reçoit, y compris les champs que le firmware de production jette en silence. ⚠️ Il **remplace** `tic-reader` le temps de la session (plus d'émission LoRa) ; reflasher la production après. L'upload n'efface pas l'EEPROM, donc la clé ChaCha20 et le mode persisté survivent.

- Reflash **MANUEL** (Pro Mini, pas d'OTA). Flash **97 %** — 698 o de marge.
- Prérequis du décodage côté récepteur : **pi-0.9.10**. Les deux sont indépendants — un émetteur 0.1.8 face à un récepteur antérieur voit son TLV ignoré, sans erreur.

### [0.1.7] — 2026-08-13

**Une trame TIC tronquée n'annonce plus de contrat.** `readAndParseTIC()` sort de sa boucle `STX→ETX` sur **timeout** aussi bien que sur `ETX`, et renvoie `kept > 0` dans les deux cas : une trame coupée après ses premières lignes était donc rendue comme valide. Comme `ADSC`/`ADCO` est en **tête** de trame, elle livrait un ADCO parfaitement juste accompagné d'un contrat qui ne valait rien — et la porte d'émission de la trame de boot ne testait que l'ADCO. Résultat observé sur ben-0001 : `CONTRAT='00'` à chaque ré-enregistrement, suivi sept secondes plus tard de la vraie valeur.

Côté récepteur, depuis pi-0.9.8, ce faux contrat aurait ouvert une époque tarifaire bidon et émis deux `changement_offre` à chaque redémarrage d'émetteur (cf. pi-0.9.9).

Nouveau champ `v.complete` : vrai si la boucle est sortie sur `ETX`, faux si elle a expiré. **La trame de boot entière** y est conditionnée, aux trois points d'émission — discovery, ré-émission on-change, retry `REGISTERING`. Ne garder que le contrat aurait traité un symptôme : dans une trame coupée, `ISOUSC`/`PREF` manquent tout autant, et une jauge calibrée sur un abonnement absent est un défaut aussi durable qu'une fausse époque tarifaire. L'émetteur retente au tour suivant, soit environ deux secondes — à comparer à une identité fausse qui, elle, resterait des mois.

⚠️ Au point `REGISTERING`, la garde porte sur **l'émission seule**, pas sur le bloc : le rejet du batch-horloge et la remise à zéro de `curveFlushPending` doivent avoir lieu même sur trame tronquée, sans quoi le flush différé enverrait la courbe alors qu'on est encore en `REGISTERING` — l'invariant « aucune mesure tant que le boot n'est pas acquitté » serait rompu.

Le drapeau porte sur le **cadrage**, pas sur la qualité des données : une ligne au checksum faux est écartée mais l'`ETX` arrive quand même, donc un front-end mal accordé n'est pas privé d'enregistrement. Et il ne peut se déclencher en fonctionnement normal — la spec impose au plus **33,4 ms** entre deux groupes d'une même trame (`§5.3.6`) contre 6 s de timeout. Ce `§5.3.6` est la couche **liaison**, commune : `STX`/`ETX` cadre l'historique comme le standard, la garde vaut donc dans les deux modes.

Coût mesuré (`arduino-cli`, `arduino:avr:pro:cpu=8MHzatmega328`) : flash **30 100 → 30 154 o (98 %)**, soit **566 o de marge**, globals inchangés à 1 239 o, **+1 o** dans une structure de pile (809 o disponibles). Aucun tampon supplémentaire : la lecture reste ligne à ligne dans `char line[40]`.

- Reflash MANUEL (Pro Mini, pas d'OTA). Flash **97 %** — 624 o de marge.

### [0.1.6] — 2026-07-25

**PAPP + IINST dans le boot (unboxing) + chantier RAM (débordement de pile résolu) + APP_ACK_MS.**
- **T_PAPP + T_IINST dans la trame de boot** : conso instantanée émise dès le boot → l'app affiche la conso au 1ᵉʳ contact (cf. pi-0.9.3 côté récepteur). Histo : IINST pour la production quand PAPP=0 en injection.
- **Chantier RAM — débordement de pile CORRIGÉ.** Diagnostic mesuré (high-water via `__brkval`, écrit en EEPROM 0x40) : la pile débordait (0 o de marge, 111 reboots) pendant `frameSeal` — cause racine **RadioHead surdimensionné** (buffers 255 o pour des trames ≤130). Corrigé côté **lib RadioHead** (⚠️ patchs hors repo, à documenter) : `RH_RF95_MAX_PAYLOAD_LEN 255→160` (−95 o) + `_seenIds[256]→[64]` + masque `& 0x3F` (−192 o, dédup émetteur inutile). Plus **buffer-reuse** dans la vérif ACC (`dev`/`km`/`exp` : exp réutilise dev, −32 o pile). Marge finale : **0 → 64 o** en histo. Reste une **instrumentation pile temporaire** (paintStack/reportStackHW) à retirer.
- **APP_ACK_MS 800 → 2000 ms** (0.1.4) : le Pi Zero chargé mettait > 800 ms à répondre (crypto Python) → l'émetteur ratait l'ACC applicatif. (Le vrai fix du blink bloquant est côté central pi-0.9.3.)
- Reflash MANUEL (Pro Mini, pas d'OTA). Flash 97 % / marge pile ~64 o.

### [0.1.3] — 2026-07-23

**ACK applicatif crypto-vérifié côté émetteur** : après la trame boot, l'émetteur attend `HMAC(K_mac, nonce)` de la centrale (`recvfromAckTimeout`) et ne se déclare `bootAcked` QUE si le MAC est valide → immunité cross-talk multi-logement (le link-ACK RadioHead d'un voisin ne suffit plus). **Signal LED de registration** : triple pulse vert lent = enregistré chez SA centrale / double pulse orange = REGISTERING (cherche encore). **Fix** : la trame boot est sérialisée dans le buffer GLOBAL `curveBuf` (le buffer pile `buf[64]` débordait pendant le ChaCha/HMAC et corrompait l'ACK → gate bloquée). Reflash MANUEL. À flasher APRÈS avoir déployé la centrale en pi-0.9.2.

### [0.1.2] — 2026-07-10

Découplage émetteur↔récepteur (incident ben-0001 09/07) + refonte LED. sendtoWait : setTimeout(600) + setRetries(1). MACHINE À ÉTATS REGISTERING/STREAMING : la trame de boot (petit paquet) sert de probe de vivacité — tant qu'elle n'est pas ACK, AUCUNE mesure émise ; retry boot à la cadence batch (v frais → identité jamais périmée) ; mesure non-ACK → retour REGISTERING → outage = 1 TX/~40 s, plus de brownout couplé. LED : couleur = quelle trame (magenta=boot, blanc=courbe), vert = ACK commun ; conso réduite (intensités ~10-40 vs 255, séquences boot ~7 s→<1 s, brownout=tick) ; codes erreur morts supprimés. Reflash MANUEL (pas d'OTA Pro Mini). minimum reste 0.0.1. (0.1.1 non publié : setTimeout/setRetries + machine à états regroupés en 0.1.2.)

### [0.1.0] — 2026-07-06

PALIER 0.1.0 (unification des versions — fin du compteur interne 0.5.x du sketch, aligné sur le Pi). tic-reader.ino : garde histo TOLÉRANTE (trame glitchée → skip silencieux, robuste au front-end marginal — corrige le rouge-rouge sur vrai Linky historique) + IINST 2e courbe (histo) + flush courbe 55 s (/live < 60 s) + checksum S1/S2 + CHIFFREMENT ChaCha20 (FRAME_ENCRYPT actif, encrypt-then-MAC). Reflash MANUEL (pas d'OTA sur Pro Mini). À flasher APRÈS avoir déployé le Pi en pi-0.1.0 (qui déchiffre) ; rétro-compat récepteur (trames non chiffrées encore décodées). minimum reste 0.0.1.

### [0.0.9] — 2026-07-03

Contrat mode-agnostique dans la trame boot : contractOf(v) = NGTF en standard / OPTARIF en historique (le champ 'contrat' de la trame boot porte l'un ou l'autre selon le mode) → le récepteur le stocke comme contrat (level_profile.ngtf) quel que soit le mode. Complète 0.0.8 (qui n'envoyait NGTF qu'en standard). Aucun impact sur un émetteur standard (contractOf renvoie NGTF, identique à 0.0.8) — l'ajout OPTARIF ne sert qu'en histo. minimum reste 0.0.1. À flasher avec le Pi ≥ pi-0.0.54.

### [0.0.8] — 2026-07-02

Chantier labels + fix index=0. (1) GARDE index=0 : en standard, ne pas démarrer la courbe tant que lastStdIndex==0 (cold-start post-reboot : NTARF vu mais EASF pas encore capté → keyframe figée à 0 sur tout le batch) → on saute la trame, le batch démarre à la suivante avec le vrai index (au pire 1 point perdu). Cause confirmée par l'instrumentation INDEX0 (pi-0.0.52). (2) LTARF : parsé + transporté dans l'ext courbe v2 (flag bit7, ext_fields bit1), on-change de NTARF, dédup + re-tx jusqu'à ACK → label tarif standard autoritatif. (3) NGTF : parsé + ajouté à la trame boot v0x01 (octet 15 = len, 16.. = ascii). minimum reste 0.0.1. ⚠️ flasher APRÈS avoir déployé le Pi récepteur en pi-0.0.54 (qui décode l'ext v2 + la trame boot étendue). Rétro-compatible : un récepteur < pi-0.0.54 ignore l'ext v2 (frames décodées à l'identique).

### [0.0.7] — 2026-06-26

Chantier ISOUSC standard : l'émetteur parse PREF (puiss. de réf., kVA, mode standard) et le place dans la trame d'identité v0x01 (octet 14 ; octet 13 reste ISOUSC histo). Ré-émet le boot frame au démarrage ET sur changement d'abonnement (ISOUSC ou PREF). Rétro-compatible (octet de padding réservé ; récepteur < pi-0.0.48 ignore l'octet 14). minimum reste 0.0.1. À flasher avec le Pi en pi-0.0.48 (qui lit l'octet 14).

### [0.0.6] — 2026-06-24

Émetteur v0x05 : horodatage PAR POINT. curveAdd envoie un dt (secondes, varint) par échantillon — en standard l'écart d'horodate compteur (todSeconds), en historique l'écart millis() sans dérive cumulative. period_ds devient la moyenne réelle du batch (hint). FIX DÉBIT : UART TIC ouvert en continu (ticSerialBegin begin-once) au lieu de Serial.begin/end par trame → fini la fenêtre aveugle, ~1 trame/s (n≈58/batch au lieu de ~30). Carry-forward NTARF+EASF (index quasi-statique → un drop de ligne ne jette plus le point). Garde standard = PAPP + horodate valide ; drop de ligne = skip SILENCIEUX (plus de rouge). LED : bleu=lecture trame, blanc=émission LoRa, vert=ACK, rouge réservé Vcc(1×)/pas-de-TIC(2×). Debug Serial par trame retiré. minimum reste 0.0.1. ⚠️ flasher APRÈS avoir déployé le Pi en pi-0.0.46 (un récepteur < 0.0.46 rejette le v0x05).

### [0.0.5] — 2026-06-23

Echo de la version firmware au boot (Serial : « tic-reader boot v0.0.5 ») pour confirmer au debug quelle version est flashée. Aucun changement fonctionnel (FW_VERSION + 1 println). minimum reste 0.0.1.

### [0.0.4] — 2026-06-23

Mode TIC STANDARD : décodage 9600 + auto-détection historique↔standard (mode persisté EEPROM 0x20, re-discovery sur échec prolongé) + parseur standard (séparateur HT, checksum HT-de-queue inclus). Trame v0x04 standard : papp NET SIGNÉ (SINSTS + / SINSTI − ; flag src_standard), horodate compteur DATE → ts (flag ts_valid), EAIT → bloc extension (flag has_ext). Identité v0x01 inchangée (ADCO ← ADSC ; ISOUSC=0 en standard, calibrage PREF différé). minimum reste 0.0.1. ⚠️ flasher APRÈS avoir déployé le Pi récepteur en pi-0.0.43 (un récepteur < 0.0.43 misread le papp signé).

### [0.0.3] — 2026-06-18

Chantier ISOUSC : l'émetteur parse ISOUSC et le place dans la trame d'identité v0x01 (octet 13). Ré-émet le boot frame au démarrage ET sur changement d'abonnement. Rétro-compatible (octet de padding réservé). minimum reste 0.0.1.

### [0.0.2] — 2026-06-18

Émetteur v0x04 : courbe PAPP fine batchée (capture continue, flush 60 s, LowPower retiré). Parseur TIC réécrit char-based (corrige une corruption de heap String qui rendait PTEC vide → trames courbe jamais émises). Remplace v0x02 (l'émetteur n'émet plus de v0x02). minimum reste 0.0.1 : les anciens émetteurs v0x02 restent décodés par le Pi 0.0.41 (rétro-compat récepteur).

### [0.0.1] — 2026-05-30

_(pas de note)_
