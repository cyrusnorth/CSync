"""Barre latérale de l'app Claude (onglet Code) : les fiches de session.

L'app Desktop affiche ses sessions d'après une fiche par session, rangée dans
~/Library/Application Support/Claude/claude-code-sessions/<organisation>/<compte>/local_<id>.json.
Chaque fiche donne le titre de la session, l'identifiant de sa conversation (« cliSessionId ») et son
dossier (« cwd »), qui sert aussi à grouper la barre latérale par projet. Ces fiches sont propres à
chaque Mac : une conversation reçue de l'autre Mac n'y apparaît donc pas.

CSync s'en occupe ainsi :
- chaque machine publie, chiffrée, une version réduite de ses fiches (titre, dates, archivée ou non) ;
  le reste de la fiche (réglages, serveurs MCP, qui peuvent contenir des accès) ne quitte pas la machine ;
- l'autre machine crée une fiche pour chaque conversation reçue qui n'en a pas, avec le dossier où la
  conversation est rangée ici ;
- une fiche dont la conversation a changé de dossier (« csync move ») est rattachée au nouveau dossier.

L'app lit ses fiches à son démarrage et réécrit celles qu'elle a en mémoire : CSync ne les modifie
donc que lorsque l'app est fermée. Sinon, les changements attendent la prochaine synchronisation.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from .paths import Paths
from .tokens import slug
from .util import LOG, atomic_write_bytes, read_bytes, run

# Champs publiés pour chaque fiche. Rien d'autre ne quitte la machine.
EXPORTED = ("title", "titleSource", "createdAt", "lastActivityAt", "isArchived", "model", "effort")
CARD_NAMESPACE = uuid.UUID("5b0f2c4e-7f0a-4d61-9a43-3c2f0c5d9e11")
UUID_LEN = 36


def app_root(paths: Paths) -> str:
	return os.environ.get("CSYNC_APP_SESSIONS_DIR") or os.path.join(
		paths.home, "Library", "Application Support", "Claude", "claude-code-sessions")


def card_dirs(paths: Paths) -> List[str]:
	"""Dossiers de fiches (un par compte). Vide si l'app Desktop n'est pas installée."""
	root = app_root(paths)
	out = []
	try:
		orgs = sorted(os.listdir(root))
	except OSError:
		return []
	for org in orgs:
		org_dir = os.path.join(root, org)
		if not os.path.isdir(org_dir):
			continue
		for acc in sorted(os.listdir(org_dir)):
			d = os.path.join(org_dir, acc)
			if os.path.isdir(d):
				out.append(d)
	return out


def card_id_for(cli_id: str) -> str:
	"""Identifiant des fiches créées par CSync : le même sur toutes les machines pour une conversation."""
	return "local_" + str(uuid.uuid5(CARD_NAMESPACE, cli_id))


def app_running() -> bool:
	forced = os.environ.get("CSYNC_ASSUME_APP_RUNNING")
	if forced is not None:
		return forced == "1"
	result = run(["ps", "-axo", "args="], timeout=10)
	if not result.ok:
		return True   # dans le doute, on n'écrit pas
	return any(line.strip().endswith(".app/Contents/MacOS/Claude") for line in result.out.splitlines())


def read_cards(d: str) -> Dict[str, Tuple[str, Dict[str, Any]]]:
	"""{identifiant de fiche: (chemin, contenu)} pour les fiches lisibles d'un dossier."""
	out = {}
	try:
		names = sorted(os.listdir(d))
	except OSError:
		return out
	for f in names:
		if not (f.startswith("local_") and f.endswith(".json")):
			continue
		path = os.path.join(d, f)
		try:
			card = json.loads((read_bytes(path) or b"").decode("utf-8"))
		except (ValueError, UnicodeDecodeError):
			continue
		if isinstance(card, dict) and isinstance(card.get("cliSessionId"), str):
			out[f[:-len(".json")]] = (path, card)
	return out


def export_cards(paths: Paths) -> Dict[str, Dict[str, Any]]:
	"""Version réduite des fiches de cette machine, par identifiant de conversation."""
	out: Dict[str, Dict[str, Any]] = {}
	for d in card_dirs(paths):
		for _cid, (_path, card) in read_cards(d).items():
			cli = card["cliSessionId"]
			rec = {k: card[k] for k in EXPORTED if k in card}
			prev = out.get(cli)
			if prev is None or (rec.get("lastActivityAt") or 0) > (prev.get("lastActivityAt") or 0):
				out[cli] = rec
	return out


def conversation_index(paths: Paths) -> Dict[str, List[str]]:
	"""{identifiant de conversation: dossiers de ~/.claude/projects qui la contiennent}."""
	idx: Dict[str, List[str]] = {}
	try:
		names = sorted(os.listdir(paths.projects_dir))
	except OSError:
		return idx
	for d in names:
		full = os.path.join(paths.projects_dir, d)
		if not os.path.isdir(full):
			continue
		try:
			files = os.listdir(full)
		except OSError:
			continue
		for f in files:
			if f.endswith(".jsonl") and len(f) == UUID_LEN + len(".jsonl"):
				idx.setdefault(f[:UUID_LEN], []).append(d)
	return idx


def plan_changes(paths: Paths, dir_map: Dict[str, str], remote: Dict[str, Dict[str, Dict[str, Any]]],
				 created: Set[str]) -> List[Tuple[str, str, Dict[str, Any]]]:
	"""Changements à faire : (action, chemin de la fiche, nouveau contenu), action valant « create »,
	« relink » ou « update ». remote : {machine: {conversation: fiche réduite}}. created : fiches déjà
	créées par CSync ici ; si l'une a disparu depuis, c'est que vous l'avez retirée : elle n'est pas recréée."""
	dirs = card_dirs(paths)
	if not dirs:
		return []
	idx = conversation_index(paths)

	def work_dir(cli: str, current: Optional[str] = None) -> Optional[str]:
		places = idx.get(cli) or []
		if not places or (current and slug(current) in places):
			return None
		cwd = dir_map.get(places[0])
		return cwd if cwd and os.path.isdir(cwd) else None

	best: Dict[str, Dict[str, Any]] = {}
	for cards in remote.values():
		for cli, rec in (cards or {}).items():
			if not isinstance(rec, dict):
				continue
			if cli not in best or (rec.get("lastActivityAt") or 0) > (best[cli].get("lastActivityAt") or 0):
				best[cli] = rec

	changes: List[Tuple[str, str, Dict[str, Any]]] = []
	target, target_count = dirs[0], -1
	local: Dict[str, Tuple[str, Dict[str, Any]]] = {}
	known_cli: Set[str] = set()
	deleted: Set[str] = set()
	for d in dirs:
		cards = read_cards(d)
		local.update(cards)
		known_cli.update(c["cliSessionId"] for _p, c in cards.values())
		try:
			deleted.update(f[len("deleted_"):] for f in os.listdir(d) if f.startswith("deleted_"))
		except OSError:
			pass
		if len(cards) > target_count:
			target, target_count = d, len(cards)

	# 1. Fiches existantes dont la conversation est rangée sous un autre dossier.
	for cid, (path, card) in local.items():
		new_cwd = work_dir(card["cliSessionId"], card.get("cwd"))
		updated = dict(card)
		if new_cwd and new_cwd != card.get("cwd"):
			updated["cwd"] = new_cwd
			updated["originCwd"] = new_cwd
			if "gitAnchorsFolderRealpath" in updated:
				updated["gitAnchorsFolderRealpath"] = os.path.realpath(new_cwd)
		# Fiches créées par CSync : titre, date et archivage suivent l'autre machine.
		rec = best.get(card["cliSessionId"])
		if cid == card_id_for(card["cliSessionId"]) and rec and \
				(rec.get("lastActivityAt") or 0) > (card.get("lastActivityAt") or 0):
			for k in ("title", "titleSource", "lastActivityAt", "isArchived"):
				if k in rec:
					updated[k] = rec[k]
		if updated != card:
			changes.append(("relink" if updated.get("cwd") != card.get("cwd") else "update", path, updated))

	# 2. Conversations reçues sans fiche ici.
	for cli, rec in sorted(best.items()):
		cid = card_id_for(cli)
		if cli in known_cli or cid in created or cid[len("local_"):] in deleted:
			continue
		places = idx.get(cli) or []
		cwd = dir_map.get(places[0]) if places else None
		if not cwd or not os.path.isdir(cwd):
			continue
		when = rec.get("lastActivityAt") or rec.get("createdAt") or 0
		card = {"sessionId": cid, "cliSessionId": cli, "cwd": cwd, "originCwd": cwd,
				"createdAt": rec.get("createdAt") or when, "lastActivityAt": when, "lastFocusedAt": when,
				"model": rec.get("model") or "claude-opus-5-5", "effort": rec.get("effort") or "medium",
				"isArchived": bool(rec.get("isArchived")), "title": rec.get("title") or "Session reçue",
				"titleSource": rec.get("titleSource") or "auto", "permissionMode": "default"}
		changes.append(("create", os.path.join(target, cid + ".json"), card))
	return changes


def apply_changes(changes: List[Tuple[str, str, Dict[str, Any]]]) -> int:
	done = 0
	for action, path, card in changes:
		try:
			data = json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
			atomic_write_bytes(path, data, 0o600)
			done += 1
			LOG.info("Barre latérale de l'app : fiche %s (%s) %s", os.path.basename(path), card.get("title"),
					 {"create": "créée", "relink": "rattachée à %s" % card.get("cwd"), "update": "mise à jour"}[action])
		except OSError as exc:
			LOG.error("Fiche %s non écrite : %s", path, exc)
	return done
