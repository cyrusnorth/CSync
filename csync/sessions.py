"""Retrouver une conversation de Claude Code et le dossier de travail auquel elle est rattachée."""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

from .adapter import load_adapter, session_cwd
from .paths import Paths, can_create_work_dir
from .scan import load_profile
from .util import CSyncError


def find_session(paths: Paths, ident: str) -> Tuple[str, str]:
	"""(chemin du fichier, dossier de ~/.claude/projects) d'une conversation, d'après son identifiant
	ou le début de celui-ci."""
	ident = ident.strip()
	if ident.endswith(".jsonl"):
		ident = ident[:-len(".jsonl")]
	if len(ident) < 4 or "/" in ident:
		raise CSyncError("Identifiant de conversation trop court ou invalide : « %s »." % ident)
	matches: List[Tuple[str, str]] = []
	try:
		names = sorted(os.listdir(paths.projects_dir))
	except OSError:
		names = []
	for d in names:
		full = os.path.join(paths.projects_dir, d)
		if not os.path.isdir(full):
			continue
		try:
			files = os.listdir(full)
		except OSError:
			continue
		for f in files:
			if f.endswith(".jsonl") and f.startswith(ident) and ".csync-conflict-" not in f:
				matches.append((os.path.join(full, f), d))
	exact = [m for m in matches if os.path.basename(m[0]) == ident + ".jsonl"]
	if exact:
		return exact[0]
	if not matches:
		raise CSyncError("Aucune conversation « %s » sur cette machine. Lancez « csync sync », puis réessayez." % ident)
	if len(matches) > 1:
		raise CSyncError("Plusieurs conversations commencent par « %s » : donnez l'identifiant complet." % ident)
	return matches[0]


def session_work_dir(paths: Paths, session_file: str, project_dir: str) -> Optional[str]:
	"""Dossier de travail depuis lequel Claude Code propose cette conversation à la reprise : celui
	que représente son dossier de ~/.claude/projects."""
	profile = load_profile(paths) or {}
	cwd = (profile.get("dir_map") or {}).get(project_dir)
	if not cwd:
		cwd = session_cwd(session_file, load_adapter(paths.adapter_file))
	return os.path.normpath(cwd) if cwd else None


def prepare_work_dir(paths: Paths, cwd: str) -> bool:
	"""S'assure que le dossier existe, en le créant si CSync en a le droit. Renvoie True s'il existe."""
	if os.path.isdir(cwd):
		return True
	if not can_create_work_dir(cwd, paths):
		return False
	os.makedirs(cwd, exist_ok=True)
	return True
