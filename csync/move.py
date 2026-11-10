"""« csync move » : déplacer un dossier de travail sans perdre ses conversations.

Claude Code rattache une conversation à son dossier de deux façons : le nom de son dossier dans
~/.claude/projects (le chemin, dont chaque caractère autre qu'une lettre ou un chiffre devient « - »),
et le champ « cwd » de chacune de ses lignes. Après un simple déplacement, Claude Code ne propose
plus ces conversations. Ici, on déplace le dossier, puis on range ses conversations sous le nouveau
nom en y remplaçant l'ancien chemin. L'autre machine reçoit ensuite les conversations sous le
nouveau chemin et supprime les anciennes.
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Tuple

from .paths import Paths
from .sync import DELETE_GRACE_SECONDS, ENCODING, UUID_RE, Engine
from .tokens import slug
from .util import CSyncError, LOG, atomic_write_bytes, now, path_inside, read_bytes


def _check_paths(paths: Paths, old: str, new: str) -> Tuple[str, str]:
	home = os.path.normpath(paths.home)
	old = os.path.normpath(os.path.abspath(os.path.expanduser(old)))
	new = os.path.normpath(os.path.abspath(os.path.expanduser(new)))
	if not os.path.isdir(old) or os.path.islink(old):
		raise CSyncError("Le dossier à déplacer n'existe pas (ou c'est un lien) : %s" % old)
	if os.path.lexists(new):
		raise CSyncError("La destination existe déjà : %s. Choisissez un nouveau nom." % new)
	for p in (old, new):
		if p == home or not path_inside(p, home):
			raise CSyncError("« csync move » ne déplace que des dossiers situés dans votre dossier personnel (%s)." % p)
	for forbidden in (paths.claude_dir, paths.csync_dir):
		if path_inside(old, forbidden) or path_inside(new, forbidden) or path_inside(forbidden, old):
			raise CSyncError("Ce dossier contient des fichiers de Claude Code ou de CSync : il ne peut pas être déplacé.")
	if path_inside(new, old):
		raise CSyncError("La destination se trouve dans le dossier déplacé.")
	if not os.path.isdir(os.path.dirname(new)):
		raise CSyncError("Le dossier qui doit accueillir la destination n'existe pas : %s" % os.path.dirname(new))
	return old, new


def _rewrite_cwd(data: bytes, cwd_key: str, old: str, new: str) -> bytes:
	"""Remplace l'ancien chemin par le nouveau dans le champ cwd, sans toucher au reste du texte."""
	text = data.decode("utf-8", ENCODING)
	esc_old = json.dumps(old, ensure_ascii=False)[1:-1]
	esc_new = json.dumps(new, ensure_ascii=False)[1:-1]
	pattern = re.compile(r'("%s"\s*:\s*")%s(?=["/])' % (re.escape(cwd_key), re.escape(esc_old)))
	return pattern.sub(lambda m: m.group(1) + esc_new, text).encode("utf-8", ENCODING)


def _last_cwd(path: str, cwd_key: str) -> Optional[str]:
	"""Dernier dossier de travail écrit dans une conversation (lu depuis la fin du fichier)."""
	pattern = re.compile(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % re.escape(cwd_key))
	try:
		size = os.path.getsize(path)
		with open(path, "rb") as fh:
			for span in (65536, 1 << 20, size):
				fh.seek(max(0, size - span))
				found = pattern.findall(fh.read().decode("utf-8", ENCODING))
				if found:
					return json.loads('"%s"' % found[-1])
				if span >= size:
					break
	except (OSError, ValueError):
		return None
	return None


def _plan_units(eng: Engine, old: str, new: str, cwd_key: str) -> List[Tuple[str, str, List[str]]]:
	"""Ce qu'il faut ranger ailleurs : (dossier de conversations source, dossier cible, éléments à déplacer
	— None pour tout le dossier)."""
	projects = eng.paths.projects_dir
	dir_map = eng.profile.get("dir_map", {}) or {}
	units: List[Tuple[str, str, Optional[List[str]]]] = []
	whole = set()
	# Le dossier lui-même et ses sous-dossiers.
	for d, cwd in sorted(dir_map.items()):
		if path_inside(cwd, old) and os.path.isdir(os.path.join(projects, d)):
			units.append((d, slug(new + cwd[len(old):]), None))
			whole.add(d)
	# Conversations rangées ailleurs mais poursuivies dans ce dossier (l'app Desktop range une session
	# sous le dossier où elle a commencé, même quand on lui en choisit un autre en cours de route).
	try:
		names = sorted(os.listdir(projects))
	except OSError:
		names = []
	for d in names:
		full = os.path.join(projects, d)
		if d in whole or not os.path.isdir(full):
			continue
		for f in sorted(os.listdir(full)):
			if not f.endswith(".jsonl") or not UUID_RE.fullmatch(f[:-len(".jsonl")]):
				continue
			last = _last_cwd(os.path.join(full, f), cwd_key)
			if last and path_inside(last, old):
				items = [f] + ([f[:-len(".jsonl")]] if os.path.isdir(os.path.join(full, f[:-len(".jsonl")])) else [])
				units.append((d, slug(new + last[len(old):]), items))
	return units


def _unit_files(projects: str, unit) -> List[Tuple[str, str]]:
	d, new_d, items = unit
	src = os.path.join(projects, d)
	dst = os.path.join(projects, new_d)
	pairs = []
	tops = [src] if items is None else [os.path.join(src, i) for i in items]
	for top in tops:
		if os.path.isfile(top):
			pairs.append((top, os.path.join(dst, os.path.relpath(top, src))))
			continue
		for root, _dirs, files in os.walk(top):
			for f in files:
				path = os.path.join(root, f)
				pairs.append((path, os.path.join(dst, os.path.relpath(path, src))))
	return pairs


def move_folder(eng: Engine, old: str, new: str, relink_only: bool = False) -> Dict[str, object]:
	"""Déplace old vers new et y rattache ses conversations. Avec relink_only, le dossier a déjà été
	déplacé (dans le Finder, par exemple) : seules les conversations sont rattachées.
	L'appelant synchronise avant et après."""
	paths = eng.paths
	if relink_only:
		old, new = _check_relink_paths(paths, old, new)
	else:
		old, new = _check_paths(paths, old, new)
	projects = paths.projects_dir
	cwd_key = str(eng.adapter.get("session_cwd_key", "cwd")).split(".")[-1]
	units = _plan_units(eng, old, new, cwd_key)

	# Refus si une conversation concernée est ouverte, ou si un fichier serait écrasé.
	active = eng._active_sessions(None)
	pairs: List[Tuple[str, str]] = []
	for unit in units:
		for path, target in _unit_files(projects, unit):
			# Identifiant de la conversation : début du chemin dans son dossier de conversations (le nom de
			# ce dossier peut lui-même contenir des identifiants, ceux de l'app Desktop par exemple).
			ids = UUID_RE.findall(os.path.relpath(path, os.path.join(projects, unit[0])).split(os.sep)[0])
			if ids and ids[0] in active:
				raise CSyncError("Une conversation de ce dossier est ouverte dans Claude Code (%s). Fermez-la, "
								 "puis relancez la commande." % ids[0])
			if path != target and os.path.lexists(target):
				raise CSyncError("Opération annulée : %s existe déjà." % target)
			pairs.append((path, target))

	# 1. Le dossier lui-même.
	if not relink_only:
		try:
			os.rename(old, new)
		except OSError as exc:
			raise CSyncError("Déplacement impossible : %s" % exc)
		LOG.info("Dossier déplacé : %s → %s", old, new)

	# 2. Les conversations : chemin réécrit (après sauvegarde), puis rangement sous le nouveau nom.
	moved_files: Dict[str, str] = {}
	sessions = 0
	for path, target in pairs:
		if path.endswith(".jsonl"):
			data = read_bytes(path) or b""
			updated = _rewrite_cwd(data, cwd_key, old, new)
			if updated != data:
				eng._backup(path)
				atomic_write_bytes(path, updated, 0o600)
			if os.path.dirname(os.path.dirname(path)) == projects:
				sessions += 1
		if path != target:
			os.makedirs(os.path.dirname(target), exist_ok=True)
			os.rename(path, target)
			moved_files[path] = target
	for d, _new_d, _items in units:
		src = os.path.join(projects, d)
		for root, dirs, _files in os.walk(src, topdown=False):
			for name in dirs:
				try:
					os.rmdir(os.path.join(root, name))
				except OSError:
					pass
		try:
			os.rmdir(src)
		except OSError:
			pass

	# 3. Les anciennes entrées seront publiées comme supprimées dès le prochain envoi (pas de délai
	#    d'attente : la disparition est voulue), les nouvelles comme des conversations nouvelles.
	ts = now()
	dir_tok = eng.state.data.get("dir_tok", {})
	for d, _new_d, items in units:
		if items is None:
			dir_tok.pop(d, None)
	for key, st in eng.state.entries.items():
		if st.get("path") in moved_files:
			st["missing_since"] = ts - DELETE_GRACE_SECONDS - 1
	return {"old": old, "new": new, "session_dirs": len({u[1] for u in units}), "sessions": sessions,
			"files": len(moved_files)}


def _check_relink_paths(paths: Paths, old: str, new: str) -> Tuple[str, str]:
	home = os.path.normpath(paths.home)
	old = os.path.normpath(os.path.abspath(os.path.expanduser(old)))
	new = os.path.normpath(os.path.abspath(os.path.expanduser(new)))
	if os.path.lexists(old):
		raise CSyncError("L'ancien emplacement existe encore (%s) : utilisez « csync move » pour déplacer le dossier." % old)
	if not os.path.isdir(new):
		raise CSyncError("Le nouvel emplacement n'existe pas : %s" % new)
	for p in (old, new):
		if p == home or not path_inside(p, home):
			raise CSyncError("Seuls les dossiers situés dans votre dossier personnel sont pris en charge (%s)." % p)
	return old, new
