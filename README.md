# CSync

Synchronise Claude Code entre plusieurs machines (un Mac et un serveur Linux) via un dépôt GitHub privé, chiffré de bout en bout.

## Ce qui est synchronisé

- **Les conversations de Claude Code** (`~/.claude/projects/`), y compris les sous-agents et la mémoire automatique des projets. Une conversation commencée sur une machine se reprend sur l'autre avec `claude --resume`.
- **La configuration** : `CLAUDE.md`, `settings.json`, `keybindings.json`, les dossiers `skills/`, `agents/`, `commands/`, `output-styles/`, `rules/`, `workflows/`, `themes/`, `agent-memory/`, `hooks/`, ainsi que les fichiers de `~/.claude` que `settings.json` mentionne (script de la barre d'état, par exemple). Les fichiers de fonctionnement que des scripts écrivent parfois dans ces dossiers (journaux `.log`, bases `.db`, verrous, dossiers `logs/`, `cache/`, `tmp/`, `state/`) restent propres à chaque machine. Le droit d'exécution d'un script est transmis, mais jamais retiré par la synchronisation.
- **Les serveurs MCP personnels** (la clé `mcpServers` de `~/.claude.json`), sans toucher au reste de ce fichier. Si ce fichier n'existe pas encore sur une machine, CSync ne le crée pas : lancez Claude Code une fois, les serveurs arriveront à la synchronisation suivante.
- **Les fichiers de vos dossiers de travail**, avec `csync files setup` (voir « Les fichiers de travail ») : `~/Claude` et les dossiers des sessions sans dossier de l'app Desktop, par Syncthing.
- **Les plugins**, indirectement : la liste des plugins activés et des marketplaces voyage dans `settings.json`, et Claude Code télécharge lui-même ce qui manque.

## Ce qui n'est jamais synchronisé

- Votre connexion (`~/.claude/.credentials.json`, le reste de `~/.claude.json`, le trousseau macOS).
- Les skills et plugins synchronisés par votre compte claude.ai (`skills/synced/`), que Claude Code gère déjà.
- Le code de vos projets : chacun garde son propre dépôt git (sauf s'il se trouve dans un dossier partagé par `csync files`).
- Les fichiers de plus de 50 Mo.
- Les hooks de CSync eux-mêmes : chaque machine garde les siens dans `settings.json`, le reste du fichier est synchronisé.
- Ce que vous déclarez propre à une machine (voir « Réglages propres à une machine »).
- Les fichiers et dossiers qui sont des liens symboliques (CSync vous les signale) ; seul le dossier des conversations peut être un lien vers un autre disque.

## Comment ça marche

- **Chemins** : un scan repère vos projets. Un projet git est reconnu partout grâce à l'adresse de son dépôt distant, même s'il n'est pas rangé au même endroit sur les deux machines. Les chemins sont réécrits à l'envoi, puis à la réception. Quand l'autre machine annonce un projet inconnu ici, CSync le cherche dans votre dossier personnel (jamais pendant un hook, au plus une fois par jour pour un même projet). Sur Mac, les dossiers protégés (Bureau, Documents, Téléchargements) ne sont examinés que lors d'une action de votre part, jamais par la tâche de fond, pour ne pas déclencher de demande d'autorisation.
- **Dossiers sans git** : un dossier situé dans votre dossier personnel est reconnu sur l'autre machine s'il est rangé au même endroit. S'il n'y existe pas encore, CSync le crée (vide) en recevant la conversation, ainsi que le dernier dossier où elle s'est poursuivie : sans lui, Claude Code ne la proposerait pas à la reprise. `csync status` liste les dossiers créés dans la semaine. Un projet git absent n'est jamais remplacé par un dossier vide. Un dossier situé ailleurs (disque externe, `/var/www`…) n'est jamais partagé d'office : deux dossiers qui portent le même nom peuvent n'avoir aucun rapport. Pour le partager, donnez-lui le même nom sur les deux machines (`csync name-project`).
- **Envois légers** : les conversations sont découpées en tranches ; un ajout n'envoie que les nouvelles tranches. La configuration ne part que si elle a changé.
- **Chiffrement** : tout est chiffré (AES-256-GCM) avec une clé qui ne quitte jamais vos machines. GitHub ne voit que des blocs illisibles ; les commits portent tous le même auteur (`csync`), le même message et une heure en temps universel.
- **Sécurité des données** :
  - avant toute modification d'un fichier local, une copie est faite dans `~/.csync/backups/` (une conversation qui ne fait que s'allonger est complétée à la fin, sans copie : rien n'y est effacé) ;
  - une conversation ouverte dans Claude Code n'est jamais modifiée, y compris celle que vous êtes en train de reprendre (Claude Code l'a déjà chargée quand CSync intervient) ;
  - en cas de modifications simultanées sur les deux machines, votre version est gardée et celle de l'autre machine est enregistrée à côté (« conflit ») ;
  - une conversation supprimée sur une machine l'est aussi sur l'autre, y compris par la purge automatique de Claude Code (par défaut au-delà de 30 jours, réglage `cleanupPeriodDays`). Exceptions : une conversation ouverte sur l'autre machine n'est supprimée qu'une fois fermée, et une conversation poursuivie là-bas entre-temps est gardée (la modification l'emporte). Aucune copie n'en est conservée dans les sauvegardes, et le dépôt est compacté au plus tôt ensuite (à la vérification périodique suivante, sauf sur batterie) pour que son contenu chiffré quitte aussi l'historique de GitHub. Si toutes les conversations disparaissent d'un coup, rien n'est envoyé sans votre accord (`csync push --allow-deletions`) ;
  - un fichier de configuration qui disparaît n'est déclaré supprimé qu'après confirmation : deux passages espacés d'au moins une minute. Si beaucoup de fichiers disparaissent d'un coup, rien n'est envoyé sans votre accord explicite (`csync push --allow-deletions`) ;
  - si le dossier de Claude Code est introuvable, CSync s'arrête au lieu de croire que tout a été supprimé.
- **Déclenchement** :
  - à l'ouverture d'une session Claude Code (réception) ;
  - après les réponses de Claude, au plus une fois toutes les 5 minutes (envoi) ;
  - à la fermeture d'une session (envoi) ;
  - toutes les 15 minutes en arrière-plan, sauf sur batterie ;
  - à la demande, avec le bouton « Synchroniser » ou `csync sync`.

## Installation

Dans ce qui suit, remplacez `VOUS` par votre nom d'utilisateur GitHub, `UTILISATEUR` par votre nom d'utilisateur sur le serveur et `SERVEUR` par son adresse. Les commandes se tapent dans le Terminal (Mac) ou dans une session SSH (serveur).

### Ce qu'il faut avoir

- **Sur le Mac** : `git` et `python3` (3.9 ou plus récent). S'ils manquent, `xcode-select --install` les installe tous les deux.
- **Sur le serveur** : `git`, `python3` (3.9 ou plus) et le module `venv`. Sur Debian ou Ubuntu : `sudo apt install git python3 python3-venv`.
- Claude Code installé sur les deux machines (CSync fonctionne sans, mais ses hooks ne servent qu'avec lui).

### Étape 1 : créer les deux dépôts sur GitHub

Sur github.com, menu en haut à droite, « New repository » :

1. **`CSync`**, le code de l'outil. Il peut être **public** : il ne contient aucun secret ni aucune donnée personnelle.
2. **`csync-data`**, vos données. Il doit être **privé**. Ne cochez rien (ni README, ni .gitignore, ni licence) ; un README seul est toléré. Ne vous en servez pour rien d'autre.

### Étape 2 : mettre le code dans le dépôt `CSync` (depuis le Mac)

Décompressez `CSync.zip`, puis, depuis le dossier obtenu :

```sh
cd ~/Downloads/CSync          # là où se trouve le dossier décompressé
git init
git add -A
git commit -m "CSync 1.0.0"
git branch -M main
git remote add origin https://github.com/VOUS/CSync.git
git push -u origin main
```

Git vous demandera de vous identifier auprès de GitHub comme pour vos autres dépôts. S'il répond « Please tell me who you are », indiquez-lui une fois pour toutes votre nom et votre adresse avec `git config --global user.name "…"` et `git config --global user.email "…"`, puis relancez `git commit`. À défaut, la page du dépôt propose « uploading an existing file » : glissez-y le contenu du dossier.

### Étape 3 : donner au Mac l'accès à `csync-data`

CSync travaille aussi en arrière-plan, sans personne pour taper un mot de passe. Il lui faut donc une **clé de déploiement** : une clé SSH (une paire de fichiers qui sert de badge d'accès) qui n'ouvre que `csync-data`, et rien d'autre de votre compte.

1. Créez la clé (laissez la phrase de passe vide : appuyez deux fois sur Entrée) :
   ```sh
   ssh-keygen -t ed25519 -f ~/.ssh/csync_deploy -C "csync mac"
   ```
2. Copiez sa partie publique :
   ```sh
   pbcopy < ~/.ssh/csync_deploy.pub
   ```
3. Sur GitHub, dépôt `csync-data` → « Settings » → « Deploy keys » → « Add deploy key ». Titre : `mac`. Collez la clé dans « Key », **cochez « Allow write access »**, puis « Add key ».
4. Indiquez à SSH d'utiliser cette clé pour `csync-data`, sous un nom dédié (`github.com-csync`) qui ne touche pas à vos autres accès GitHub. Ouvrez le fichier de réglages de SSH avec `nano ~/.ssh/config` (il est créé s'il n'existe pas), ajoutez ces lignes à la fin, puis enregistrez (Ctrl+O, Entrée) et quittez (Ctrl+X) :
   ```
   Host github.com-csync
       HostName github.com
       User git
       IdentityFile ~/.ssh/csync_deploy
       IdentitiesOnly yes
   ```
5. Vérifiez, et répondez `yes` si SSH demande de confirmer l'identité de github.com :
   ```sh
   ssh -T git@github.com-csync
   ```
   La réponse doit commencer par « Hi » et indiquer « You've successfully authenticated ».

### Étape 4 : installer CSync sur le Mac

```sh
git clone https://github.com/VOUS/CSync.git ~/CSync
cd ~/CSync
sh install.sh
```

L'installation prépare un environnement Python isolé dans `~/.csync`, puis lance la configuration guidée. Réponses à donner :

| Question | Réponse |
|---|---|
| Nom de cette machine | `mac` (ou ce que vous voulez) |
| Adresse du dépôt GitHub privé des données | `git@github.com-csync:VOUS/csync-data.git` |
| Avez-vous déjà une clé CSync ? | non (Entrée) |
| Où créer la nouvelle clé ? | Entrée (`~/csync.key`) |
| Le dossier … n'a pas de dépôt git. Le partager ? | non, sauf si vous voulez le partager (il faudra lui donner le même nom sur le serveur) |
| Envoyer maintenant les données de cette machine ? | oui (Entrée) |
| Installer les hooks dans Claude Code ? | oui (Entrée) |
| Installer la vérification périodique ? | oui (Entrée) |

L'application `CSync` est ensuite créée dans `~/Applications`. Si le Terminal indique que `~/.local/bin` n'est pas dans votre `PATH`, lancez la ligne qu'il affiche, puis ouvrez une nouvelle fenêtre de Terminal.

### Étape 5 : ranger la clé de chiffrement

Le fichier `~/csync.key` contient une seule ligne, qui commence par `csync-key-v1:`. Sans elle, vos données sur GitHub sont illisibles, y compris pour vous.

- Affichez-la avec `cat ~/csync.key` et enregistrez-la dans votre gestionnaire de mots de passe, comme une note sécurisée.
- Ne la mettez jamais dans un dépôt git, ni dans un e-mail, ni dans un service de stockage en ligne non chiffré.

### Étape 6 : donner au serveur l'accès à `csync-data`

Connectez-vous au serveur (`ssh UTILISATEUR@SERVEUR`) et refaites l'étape 3 **avec une nouvelle clé, propre au serveur** (une clé privée ne doit pas circuler d'une machine à l'autre) :

1. `ssh-keygen -t ed25519 -f ~/.ssh/csync_deploy -C "csync serveur"` (phrase de passe vide) ;
2. `cat ~/.ssh/csync_deploy.pub`, puis copiez la ligne affichée ;
3. sur GitHub, une seconde clé de déploiement sur `csync-data`, titre `serveur`, avec « Allow write access » ;
4. le même bloc `Host github.com-csync` dans `~/.ssh/config` du serveur ;
5. `ssh -T git@github.com-csync`, puis `yes`.

Le serveur a besoin de sa propre clé : la tâche de fond n'a pas accès à une clé transmise depuis votre Mac pendant une session SSH.

### Étape 7 : copier la clé de chiffrement sur le serveur

Depuis le Mac (pas depuis le serveur) :

```sh
scp ~/csync.key UTILISATEUR@SERVEUR:~/csync.key
ssh UTILISATEUR@SERVEUR chmod 600 ~/csync.key
```

Autre possibilité : sur le serveur, `nano ~/csync.key`, collez la ligne conservée dans votre gestionnaire de mots de passe, enregistrez, puis `chmod 600 ~/csync.key`.

### Étape 8 : installer CSync sur le serveur

```sh
git clone https://github.com/VOUS/CSync.git ~/CSync
cd ~/CSync
sh install.sh
```

| Question | Réponse |
|---|---|
| Nom de cette machine | `serveur` |
| Adresse du dépôt GitHub privé des données | `git@github.com-csync:VOUS/csync-data.git` |
| Avez-vous déjà une clé CSync ? | **oui** (`o`) |
| Chemin du fichier de clé | `~/csync.key` |
| Quand un fichier de configuration diffère…, adopter la version de l'autre machine ? | **oui** si la configuration de Claude Code du serveur n'a rien de particulier (celle du Mac la remplace ; l'ancienne est sauvegardée) ; **non** sinon (une copie de conflit est créée, à examiner) |
| Lancer la première synchronisation maintenant ? | oui (Entrée) |
| Installer les hooks / la vérification périodique ? | oui (Entrée) |

Avant la première synchronisation, CSync affiche ce qu'il va recevoir, et sauvegarde la configuration actuelle du serveur dans `~/.csync/backups/initial-…`.

Enfin, pour que la vérification périodique tourne même quand vous n'êtes pas connecté au serveur :

```sh
sudo loginctl enable-linger $USER
```

### Étape 9 : vérifier

Sur chaque machine :

```sh
csync check     # l'installation est-elle complète ?
csync status    # l'autre machine apparaît-elle, y a-t-il des alertes ?
```

Essai rapide : sur le Mac, ajoutez une ligne à `~/.claude/CLAUDE.md`, lancez `csync push` ; sur le serveur, lancez `csync pull`, puis vérifiez que la ligne est arrivée.

## Utilisation au quotidien

Il n'y a rien à faire : la synchronisation est automatique. Deux points à connaître :

- si vous changez de machine moins de 5 minutes après votre dernier échange avec Claude, cliquez sur « Synchroniser » (ou lancez `csync push`) avant de partir ;
- pour reprendre une conversation poursuivie sur l'autre machine, lancez **`csync resume`** (ou `csync resume <identifiant>`) plutôt que `claude --resume` : CSync récupère d'abord les nouveautés, puis ouvre `claude --resume`. Si vous passez directement par `claude --resume` alors que la conversation a avancé ailleurs, CSync ne peut plus la mettre à jour (Claude Code l'a déjà chargée) ; il vous le signale à l'ouverture, avec la commande à lancer après avoir quitté. Les conversations que vous n'avez pas ouvertes, elles, sont mises à jour dès le démarrage de Claude Code.

### La fenêtre (Mac)

Ouvrez **CSync** depuis le Finder ou Spotlight. Elle affiche l'état, et propose trois boutons :

- **Synchroniser** ;
- **Rescanner** ;
- **Voir le profil**.

Des boutons supplémentaires apparaissent seulement en cas de besoin :

- **Conflits** ;
- **Sessions en attente** ;
- **Réparer le format**.

### Les commandes

| Commande | Rôle |
|---|---|
| `csync sync` | réception puis envoi (`--dry-run` pour un aperçu sans rien modifier) |
| `csync push` / `csync pull` | envoi seul / réception seule ; `--allow-deletions` confirme une disparition massive de fichiers |
| `csync resume [identifiant]` | met à jour, puis reprend une conversation avec `claude --resume`, depuis son dossier de travail (le début de l'identifiant suffit) |
| `csync move <dossier> <nouveau chemin>` | déplace un dossier de travail ; ses conversations le suivent et restent reprenables, sur toutes les machines |
| `csync files setup` | installe et configure la synchronisation des fichiers (Syncthing) |
| `csync files status` | état de la synchronisation des fichiers |
| `csync status` | état, autres machines, fichiers, alertes |
| `csync scan` | rescanne la machine (nouveaux projets, projets déplacés) |
| `csync name-project <dossier> <nom>` | partage un dossier sans git (ou un dépôt dont l'adresse diffère) sous un nom commun, à faire sur les deux machines |
| `csync profile` | affiche le profil de la machine |
| `csync conflicts` | liste les conflits avec leur identifiant ; `--resolve <identifiant>` (ou `all`) les marque comme résolus |
| `csync compact` | réduit la taille du dépôt de données (voir plus bas) |
| `csync local` | réglages et serveurs MCP propres à cette machine (voir plus bas) |
| `csync forget-machine <nom>` | retire du dépôt une machine qui n'existe plus (après une réinstallation, par exemple) ; `--force` si elle a envoyé des données dans la semaine |
| `csync repair` | réparation du format assistée par IA (voir plus bas) |
| `csync check` | vérifie l'installation |
| `csync hooks install` / `uninstall` | hooks de Claude Code |
| `csync schedule install` / `uninstall` | vérification périodique |

## Situations particulières

### Conflits

Si un même fichier a été modifié des deux côtés entre deux synchronisations :

1. votre version locale est gardée ;
2. la version de l'autre machine est enregistrée à côté, avec un nom en `.csync-conflict-<machine>` ;
3. comparez les deux et gardez la bonne version ;
4. **supprimez la copie** (ou cliquez « Marquer comme résolu ») : votre version sera alors envoyée à l'autre machine.

Pour une conversation, la version de l'autre machine devient une nouvelle conversation, que vous pouvez reprendre avec `claude --resume`. Cette copie reste sur la machine où elle a été créée tant que vous n'y touchez pas ; si vous la reprenez, elle devient une conversation ordinaire, synchronisée comme les autres, et CSync ne la modifie plus jamais. Si l'autre machine continue ensuite sa propre version, la suite reste consultable ici dans une conversation à part, sans nouveau conflit à traiter.

### Les fichiers de travail

CSync synchronise les conversations, pas les fichiers. Pour les fichiers, `csync files setup` installe **Syncthing** (logiciel libre et gratuit, avec Homebrew), le lance à chaque démarrage du Mac et lui confie deux dossiers :

- `~/Claude` ;
- `~/Library/Application Support/Claude/scratch-workspaces`, où l'app Desktop range les sessions ouvertes sans dossier.

Lancez la commande une fois sur chaque machine. Chacune publie son identifiant Syncthing dans le dépôt chiffré ; à la synchronisation suivante, les deux se relient toutes seules. Les fichiers passent directement d'une machine à l'autre, chiffrés, sans service en ligne ; les deux machines doivent être allumées en même temps pour échanger.

- Un fichier supprimé ou remplacé par l'autre machine est gardé 30 jours dans le dossier caché `.stversions` du dossier partagé.
- Si un même fichier a changé des deux côtés, Syncthing garde les deux versions (l'une porte `sync-conflict` dans son nom).
- Ignorés : `.DS_Store`, `node_modules`, `.venv`, `__pycache__`.
- Pour partager d'autres dossiers, modifiez la liste `file_folders` de `~/.csync/config.json` (chemins commençant par `~/`, identifiant identique sur les deux machines), puis relancez `csync files setup`.

### Déplacer un dossier

Déplacer un dossier dans le Finder coupe ses conversations : Claude Code ne les propose plus. Utilisez plutôt :

```sh
csync move ~/Claude/Ancien ~/Claude/Projects/Nouveau
```

CSync synchronise d'abord, déplace le dossier, réécrit le chemin dans ses conversations (copie de sauvegarde avant), puis l'annonce à l'autre machine, qui range les conversations au nouvel endroit et supprime les anciennes. Les fichiers suivent par Syncthing. La commande refuse si une conversation du dossier est ouverte. L'app Desktop garde sa propre liste de sessions : celles du dossier déplacé peuvent y pointer encore vers l'ancien chemin.

### Sessions en attente

Une conversation qui appartient à un projet absent de cette machine attend. Pour la recevoir, clonez le projet ici, où vous voulez : il sera trouvé lors d'une prochaine synchronisation ordinaire. Pour ne pas attendre, cliquez « Rescanner » (ou lancez `csync scan`), puis « Synchroniser ».

### Exclure un projet

Dans l'écran du scan, décochez le projet. Cette machine cesse alors d'envoyer et de recevoir ses conversations, et les contenus qu'elle avait envoyés disparaissent du dépôt au compactage suivant. L'exclusion vaut pour la machine où elle est faite : excluez le projet sur chaque machine.

### Réglages propres à une machine

Certains réglages n'ont de sens que sur une machine : un hook qui joue un son sur Mac, un serveur MCP dont la commande pointe vers `/opt/homebrew/…`. Déclarez-les propres à la machine concernée :

```sh
csync local settings hooks      # la clé « hooks » de settings.json reste propre à cette machine
csync local mcp github          # le serveur MCP « github » aussi
csync local                     # affiche la liste ; --remove pour revenir en arrière
```

Ces éléments ne sont plus envoyés depuis cette machine, et ce que l'autre machine envoie ne les remplace jamais ici.

### Machine copiée ou réinstallée

Si le dossier `~/.csync` est recopié sur un autre ordinateur (sauvegarde restaurée, assistant de migration), CSync refuse de travailler avec lui et vous demande de lancer `csync setup`, qui donne à la nouvelle machine sa propre identité (vos choix de projets sont conservés). Après une réinstallation, l'ancienne identité reste visible dans `csync status` : retirez-la avec `csync forget-machine <nom ou identifiant>` (la configuration vous le propose quand elle la repère). Retirer par erreur une machine encore en service est sans conséquence : elle se réinscrit d'elle-même à sa synchronisation suivante.

### Compactage

Chaque envoi ajoute un peu d'historique au dépôt de données. Quand il dépasse 300 Mo, la vérification périodique le compacte d'elle-même, au plus une fois par semaine : les contenus que plus aucune machine n'utilise sont retirés et l'historique repart d'un seul commit. Les autres machines se réalignent seules à leur synchronisation suivante. Vous pouvez aussi le faire à la main (`csync compact`, ou le bouton « Compacter » quand il apparaît).

### Réparer le format

Si une mise à jour de Claude Code change la structure de ses fichiers, CSync le détecte. Il suspend alors la partie concernée, sans rien écrire. La réparation se passe ainsi :

1. cliquez « Réparer le format » (ou lancez `csync repair`) ;
2. Claude Code examine la **structure** de vos fichiers : noms de clés et types, jamais le contenu des conversations ;
3. il propose une correction ;
4. CSync la teste sur vos vrais fichiers ;
5. elle n'est appliquée qu'avec votre accord (`csync repair --approve`).

Gardez de préférence la même version de Claude Code sur les deux machines. CSync vous prévient si elles diffèrent.

## Mettre à jour CSync

Sur chaque machine :

```sh
cd ~/CSync
git pull
sh install.sh --no-setup
```

## Désinstaller

```sh
csync hooks uninstall
csync schedule uninstall
rm -rf ~/.csync ~/.local/bin/csync ~/Applications/CSync.app
```

Vos fichiers Claude Code ne sont pas touchés. Si vous réinstallez CSync plus tard sur cette machine, retirez ensuite son ancienne identité depuis l'autre machine (`csync forget-machine`).

## Dépannage

- **Journal** : `~/.csync/logs/csync.log`. La vérification périodique et les envois en arrière-plan écrivent aussi dans `periodic.log` et `background.log`.
- **Sauvegardes** : `~/.csync/backups/`. Chaque dossier daté contient les fichiers tels qu'ils étaient avant d'être remplacés. `initial-…` est la copie complète faite lors de la première configuration.
- **Le dépôt n'est pas joignable** : vérifiez `ssh -T git@github.com`. Une clé SSH protégée par une phrase de passe doit être chargée dans l'agent SSH, car CSync ne peut pas la demander en arrière-plan.
- **État local illisible** (`~/.csync/state.json`) : remplacez-le par sa copie `state.json.bak`. À défaut, supprimez-le : CSync reconstruit son état au passage suivant ; un élément modifié des deux côtés donne alors une copie de conflit, jamais une perte.

## Limites connues

- **L'app Desktop** : un utilisateur a signalé que l'onglet Code de l'app Desktop n'affiche pas toujours les sessions copiées depuis une autre machine, alors que `claude --resume` les trouve ([issue GitHub](https://github.com/anthropics/claude-code/issues/69585)). Ce n'est pas propre à CSync.
- **Le Dock** : la fenêtre peut apparaître dans le Dock sous le nom « Python ».
- **La fenêtre n'a pas pu être essayée** lors du développement (la bibliothèque graphique n'était pas installable dans l'environnement de test). Toutes ses actions passent par les mêmes fonctions que la ligne de commande, qui, elles, sont testées ; en cas de souci, `csync status` et `csync sync` font la même chose.
- **Les noms de projets non git** : un dossier sans dépôt git est reconnu sur l'autre machine s'il est rangé au même endroit par rapport au dossier personnel. Sinon, utilisez `csync name-project` sur les deux machines.
- **Les fichiers de projet** (`.claude/` à l'intérieur de vos dépôts) ne sont pas concernés : ils voyagent avec le dépôt git du projet.
