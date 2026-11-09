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
from typing import Dict, List, Tuple

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


def move_folder(eng: Engine, old: str, new: str) -> Dict[str, object]:
	"""Déplace old vers new et y rattache ses conversations. L'appelant synchronise avant et après."""
	paths = eng.paths
	old, new = _check_paths(paths, old, new)
	projects = paths.projects_dir
	dir_map = eng.profile.get("dir_map", {}) or {}
	cwd_key = str(eng.adapter.get("session_cwd_key", "cwd")).split(".")[-1]

	# Dossiers de conversations concernés : le dossier lui-même et ses sous-dossiers.
	plan: List[Tuple[str, str, str]] = []   # (dossier actuel, nouveau dossier, nouveau cwd)
	for d, cwd in sorted(dir_map.items()):
		if path_inside(cwd, old) and os.path.isdir(os.path.join(projects, d)):
			new_cwd = new + cwd[len(old):]
			plan.append((d, slug(new_cwd), new_cwd))

	# Refus si une conversation concernée est ouverte, ou si un fichier serait écrasé.
	active = eng._active_sessions(None)
	for d, new_d, _ in plan:
		src = os.path.join(projects, d)
		for root, _dirs, files in os.walk(src):
			for f in files:
				ids = UUID_RE.findall(os.path.relpath(os.path.join(root, f), src))
				if ids and ids[0] in active:
					raise CSyncError("Une conversation de ce dossier est ouverte dans Claude Code (%s). Fermez-la, "
									 "puis relancez « csync move »." % ids[0])
				dest = os.path.join(projects, new_d, os.path.relpath(os.path.join(root, f), src))
				if new_d != d and os.path.lexists(dest):
					raise CSyncError("Déplacement annulé : %s existe déjà." % dest)

	# 1. Le dossier lui-même.
	try:
		os.rename(old, new)
	except OSError as exc:
		raise CSyncError("Déplacement impossible : %s" % exc)
	LOG.info("Dossier déplacé : %s → %s", old, new)

	# 2. Les conversations : chemin réécrit (après sauvegarde), puis rangement sous le nouveau nom.
	moved_files: Dict[str, str] = {}
	sessions = 0
	for d, new_d, _new_cwd in plan:
		src = os.path.join(projects, d)
		dst = os.path.join(projects, new_d)
		for root, _dirs, files in os.walk(src):
			for f in files:
				path = os.path.join(root, f)
				target = os.path.join(dst, os.path.relpath(path, src))
				if f.endswith(".jsonl"):
					data = read_bytes(path) or b""
					updated = _rewrite_cwd(data, cwd_key, old, new)
					if updated != data:
						eng._backup(path)
						atomic_write_bytes(path, updated, 0o600)
					if os.path.dirname(path) == src:
						sessions += 1
				os.makedirs(os.path.dirname(target), exist_ok=True)
				os.rename(path, target)
				moved_files[path] = target
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
	for d, _new_d, _ in plan:
		dir_tok.pop(d, None)
	for key, st in eng.state.entries.items():
		if st.get("path") in moved_files:
			st["missing_since"] = ts - DELETE_GRACE_SECONDS - 1
	return {"old": old, "new": new, "session_dirs": len(plan), "sessions": sessions, "files": len(moved_files)}
