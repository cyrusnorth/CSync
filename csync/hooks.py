"""Intégration à Claude Code : les hooks qui déclenchent CSync automatiquement.

- SessionStart (démarrage ou reprise) : récupère les nouveautés de l'autre machine.
- Stop (fin de chaque réponse de Claude) : envoie, au plus une fois toutes les 5 minutes.
- SessionEnd (fermeture) : dernier envoi.

Les deux envois sont lancés en arrière-plan : le hook rend la main immédiatement.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

from .paths import Paths
from .util import CSyncError, atomic_write_text, read_bytes, spawn_detached

MARKER = "csync"
EVENTS = {
	"SessionStart": ("session-start", 90),
	"Stop": ("stop", 10),
	"SessionEnd": ("session-end", 10),
}


def hook_command(paths: Paths, arg: str) -> str:
	bin_path = paths.bin_path
	if bin_path.startswith(paths.home + "/"):
		bin_path = "$HOME" + bin_path[len(paths.home):]
	return '"%s" hook %s' % (bin_path, arg)


def self_command(paths: Paths) -> List[str]:
	"""Commande pour relancer CSync lui-même en arrière-plan."""
	if os.path.exists(paths.bin_path):
		return [paths.bin_path]
	return [sys.executable, "-m", "csync"]


def _load_settings(paths: Paths) -> Dict[str, Any]:
	raw = read_bytes(paths.settings_file)
	if raw is None or not raw.strip():
		return {}
	try:
		data = json.loads(raw.decode("utf-8"))
	except (ValueError, UnicodeDecodeError) as exc:
		raise CSyncError("%s n'est pas un JSON valide (%s) : corrigez-le avant d'installer les hooks."
						 % (paths.settings_file, exc))
	if not isinstance(data, dict):
		raise CSyncError("%s n'est pas un objet JSON." % paths.settings_file)
	return data


def _is_ours(handler: Dict[str, Any]) -> bool:
	cmd = handler.get("command", "")
	return isinstance(cmd, str) and "/bin/csync" in cmd and " hook " in cmd


def strip_our_hooks(settings: Dict[str, Any]) -> Dict[str, Any]:
	"""Copie des réglages sans les hooks de CSync (ils restent propres à chaque machine)."""
	import copy
	out = copy.deepcopy(settings)
	hooks = out.get("hooks")
	if not isinstance(hooks, dict):
		return out
	for event in list(hooks.keys()):
		groups = hooks[event]
		if not isinstance(groups, list):
			continue
		new_groups = []
		for g in groups:
			if isinstance(g, dict) and isinstance(g.get("hooks"), list):
				kept = [h for h in g["hooks"] if not (isinstance(h, dict) and _is_ours(h))]
				if kept:
					g = dict(g)
					g["hooks"] = kept
					new_groups.append(g)
				elif len(kept) == len(g["hooks"]):
					new_groups.append(g)
			else:
				new_groups.append(g)
		if new_groups:
			hooks[event] = new_groups
		else:
			del hooks[event]
	if not hooks:
		out.pop("hooks", None)
	return out


def settings_has_our_hooks(path: str) -> bool:
	raw = read_bytes(path)
	if not raw:
		return False
	try:
		data = json.loads(raw.decode("utf-8"))
	except (ValueError, UnicodeDecodeError):
		return False
	hooks = data.get("hooks") if isinstance(data, dict) else None
	if not isinstance(hooks, dict):
		return False
	return any(_is_ours(h) for groups in hooks.values() if isinstance(groups, list)
			   for g in groups if isinstance(g, dict) for h in g.get("hooks", []) if isinstance(h, dict))


def hooks_installed(paths: Paths) -> bool:
	try:
		settings = _load_settings(paths)
	except CSyncError:
		return False
	hooks = settings.get("hooks", {})
	if not isinstance(hooks, dict):
		return False
	for event in EVENTS:
		groups = hooks.get(event, [])
		if not any(_is_ours(h) for g in groups if isinstance(g, dict) for h in g.get("hooks", []) if isinstance(h, dict)):
			return False
	return True


def install_hooks(paths: Paths) -> bool:
	"""Ajoute les hooks de CSync sans toucher aux autres. Renvoie True si le fichier a changé."""
	settings = _load_settings(paths)
	hooks = settings.get("hooks")
	if not isinstance(hooks, dict):
		hooks = {}
	changed = False
	for event, (arg, timeout) in EVENTS.items():
		groups = hooks.get(event)
		if not isinstance(groups, list):
			groups = []
		wanted = {"type": "command", "command": hook_command(paths, arg), "timeout": timeout}
		present = False
		for g in groups:
			if not isinstance(g, dict):
				continue
			for h in g.get("hooks", []):
				if isinstance(h, dict) and _is_ours(h):
					present = True
					if h != wanted:
						h.clear()
						h.update(wanted)
						changed = True
		if not present:
			groups.append({"hooks": [wanted]})
			changed = True
		hooks[event] = groups
	if changed:
		settings["hooks"] = hooks
		atomic_write_text(paths.settings_file, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
	return changed


def uninstall_hooks(paths: Paths) -> bool:
	settings = _load_settings(paths)
	hooks = settings.get("hooks")
	if not isinstance(hooks, dict):
		return False
	changed = False
	for event in list(hooks.keys()):
		groups = hooks[event]
		if not isinstance(groups, list):
			continue
		new_groups = []
		for g in groups:
			if isinstance(g, dict) and isinstance(g.get("hooks"), list):
				kept = [h for h in g["hooks"] if not (isinstance(h, dict) and _is_ours(h))]
				if len(kept) != len(g["hooks"]):
					changed = True
				if kept:
					g["hooks"] = kept
					new_groups.append(g)
			else:
				new_groups.append(g)
		if new_groups:
			hooks[event] = new_groups
		else:
			del hooks[event]
	if changed:
		if hooks:
			settings["hooks"] = hooks
		else:
			settings.pop("hooks", None)
		atomic_write_text(paths.settings_file, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
	return changed


def read_hook_input() -> Dict[str, Any]:
	try:
		if sys.stdin is None or sys.stdin.isatty():
			return {}
		data = sys.stdin.read()
	except (OSError, ValueError):
		return {}
	try:
		obj = json.loads(data) if data.strip() else {}
	except ValueError:
		return {}
	return obj if isinstance(obj, dict) else {}


def launch_background_push(paths: Paths, wait_lock: int = 0) -> None:
	args = self_command(paths) + ["push", "--quiet", "--background"]
	if wait_lock:
		args += ["--wait-lock", str(wait_lock)]
	spawn_detached(args, os.path.join(paths.logs_dir, "background.log"), env={"CSYNC_INTERNAL": ""})
