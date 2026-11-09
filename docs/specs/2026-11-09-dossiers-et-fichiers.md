# Dossiers manquants, déplacements et synchronisation des fichiers

Date : 9 novembre 2026. Statut : validé par Cyrus (conversation du 9 novembre).

## Le problème

Une conversation menée sur le MacBook dans un dossier créé le soir même est arrivée sur le Mac Studio,
mais elle y restait invisible :

- son dossier de travail n'existait pas sur le Mac Studio ;
- CSync l'a rangée sous le nom d'un dossier absent sans rien signaler (`csync status` : « Tout va bien ») ;
- les fichiers du dossier, eux, ne sont synchronisés par rien.

## Ce qui change

### 1. Réception : le dossier absent est créé

Quand une conversation arrive et que son dossier de travail n'existe pas ici :

- s'il se trouve dans le dossier personnel, CSync le crée (vide), puis range la conversation comme avant ;
- s'il se trouve ailleurs (disque externe, `/var/www`…), CSync ne crée rien : la conversation est mise
  en attente, et `csync status` le signale ;
- les dossiers créés sont listés dans le résumé de la synchronisation et dans `csync status`
  (les 10 derniers, pendant 7 jours).

La conversation peut se poursuivre dans un autre dossier que celui où elle a commencé (l'app Desktop
le fait quand on choisit un dossier en cours de route). Le dernier dossier de travail écrit dans la
conversation est donc créé lui aussi s'il manque, aux mêmes conditions.

Jamais créés : un dossier hors du dossier personnel, ou situé dans `~/.Trash`, `~/.claude`, `~/.csync`.

### 2. `csync resume <identifiant>` : reprise depuis le bon dossier

`csync resume` retrouve la conversation dans `~/.claude/projects`, se place dans le dossier de travail
auquel Claude Code l'associe (en le créant s'il manque, aux conditions ci-dessus), puis lance
`claude --resume <identifiant>`. Peu importe d'où la commande est lancée.

### 3. `csync move <ancien> <nouveau>` : déplacer un dossier sans perdre ses conversations

1. synchronise d'abord (réception puis envoi) ;
2. refuse si une conversation de ce dossier est ouverte, si la destination existe, ou si l'un des deux
   chemins est hors du dossier personnel ;
3. déplace le dossier ;
4. pour chaque dossier de conversations qui correspond à l'ancien chemin (ou à un sous-dossier) :
   remplace l'ancien chemin par le nouveau dans le champ `cwd` de chaque ligne (copie de sauvegarde
   avant), et range le tout sous le nom correspondant au nouveau chemin ;
5. envoie : l'autre machine reçoit les conversations sous le nouveau chemin et supprime les anciennes.

Les fichiers eux-mêmes arrivent sur l'autre machine par la synchronisation des fichiers (point 4).

### 4. Synchronisation des fichiers avec Syncthing

Syncthing (logiciel libre et gratuit) copie des dossiers directement d'une machine à l'autre. CSync
l'installe, le configure et affiche son état ; l'utilisateur n'ouvre jamais Syncthing.

- `csync files setup` : installe Syncthing avec Homebrew s'il manque, le lance au démarrage du Mac
  (`brew services`), puis configure les dossiers partagés.
- Dossiers partagés par défaut : `~/Claude` et le dossier des sessions sans dossier de l'app Desktop
  (`~/Library/Application Support/Claude/scratch-workspaces`). Liste modifiable dans `config.json`
  (`file_folders`). Chemins identiques sur les deux machines (relatifs au dossier personnel).
- Appairage : l'identifiant Syncthing de chaque machine est publié dans son profil, dans le dépôt
  privé chiffré. À chaque synchronisation, CSync ajoute à Syncthing les machines qu'il y trouve et
  leur partage les dossiers. Aucune manipulation.
- Sécurité : les fichiers supprimés ou remplacés par l'autre machine sont gardés 30 jours dans
  `.stversions` (corbeille de Syncthing). Fichiers ignorés : `.DS_Store`, `node_modules`, `.venv`,
  `__pycache__`.
- `csync status` affiche l'état des fichiers : à jour, en cours (nombre d'éléments), autre Mac hors
  ligne, Syncthing arrêté.

## Hors sujet

- La liste des sessions de l'app Desktop : elle a son propre index, que CSync ne modifie pas.
- `~/.claude.json` (confiance accordée aux dossiers) : non modifié par `csync move` ; Claude Code
  redemandera la confiance pour le nouveau dossier.

## Tests

- réception d'une conversation dont le dossier manque (dans le dossier personnel : créé ;
  ailleurs : en attente, alerte) ;
- conversation qui change de dossier en cours de route : le dernier dossier est créé ;
- `csync resume` : choix du dossier ;
- `csync move` : dossier déplacé, conversations reprenables à la nouvelle place sur les deux machines,
  refus quand une conversation est ouverte ;
- Syncthing : configuration produite (dossiers, appareils, corbeille) avec un faux Syncthing ;
  appairage à partir des profils.
