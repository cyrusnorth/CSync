"""Scan de la machine : où sont les projets, les sessions, la configuration de Claude Code.

Le résultat est le « profil » de la machine. Il sert à traduire les chemins locaux en
repères neutres (voir tokens.py). Un projet git est reconnu partout grâce à l'adresse
de son dépôt distant, même s'il n'est pas rangé au même endroit sur chaque machine.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .adapter import check_format, get_path, session_cwd, session_last_cwd
from .paths import Paths, system_name
from .tokens import PathMapper
from .util import LOG, iso, now, read_json, read_json_safe, run, which, write_json

PROFILE_FORMAT = 1


def normalize_remote(url: str) -> Optional[str]:
	"""Adresse de dépôt git ramenée à « hôte/propriétaire/nom », sans identifiants."""
	url = url.strip()
	if not url:
		return None
	m = re.match(r"^[\w.\-]+@([\w.\-]+):(.+)$", url)          # git@github.com:owner/repo.git
	if m:
		host, path = m.group(1), m.group(2)
	else:
		m = re.match(r"^[a-z+]+://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", url)  # https://, ssh://
		if not m:
			# Dépôt distant local (chemin sur le disque) : rien ne garantit qu'il désigne le même
			# projet sur l'autre machine, il ne sert donc pas d'identifiant.
			return None
		host, path = m.group(1), m.group(2)
	path = path.strip("/")
	if path.endswith(".git"):
		path = path[:-4]
	result = (host + "/" + path).lower()
	return re.sub(r"\s+", "_", result)


def claude_version() -> Optional[str]:
	from .util import claude_env, find_claude
	claude = find_claude()
	if claude is None:
		return None
	result = run([claude, "--version"], timeout=20, env=claude_env())
	if not result.ok:
		return None
	m = re.search(r"(\d+\.\d+\.\d+)", result.out)
	return m.group(1) if m else None


def git_identity(path: str) -> Optional[Tuple[str, str]]:
	"""(racine du dépôt, clé partagée) si path est dans un dépôt git doté d'un dépôt distant."""
	if which("git") is None or not os.path.isdir(path):
		return None
	top = run(["git", "-C", path, "rev-parse", "--show-toplevel"], timeout=10)
	if not top.ok:
		return None
	root = os.path.normpath(top.out.strip())
	remote = run(["git", "-C", root, "remote", "get-url", "origin"], timeout=10)
	url = remote.out.strip() if remote.ok else ""
	if not url:
		remotes = run(["git", "-C", root, "remote"], timeout=10)
		names = remotes.out.split() if remotes.ok else []
		if names:
			other = run(["git", "-C", root, "remote", "get-url", names[0]], timeout=10)
			url = other.out.strip() if other.ok else ""
	key = normalize_remote(url) if url else None
	if not key:
		return None
	return root, "git:" + key


def _is_within(path: str, parent: str) -> bool:
	return path == parent or path.startswith(parent.rstrip("/") + "/")


def collect_cwds(paths: Paths, adapter: Dict[str, Any], known_dirs: Dict[str, str],
				 fallback_dirs: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, str], List[str]]:
	"""Associe chaque dossier de ~/.claude/projects au dossier de travail qu'il représente."""
	dir_map: Dict[str, str] = {}
	unresolved: List[str] = []
	try:
		names = sorted(os.listdir(paths.projects_dir))
	except FileNotFoundError:
		names = []
	claude_json = read_json_safe(paths.claude_json, None)
	known_projects = get_path(claude_json, adapter["claude_json_projects_key"]) if isinstance(claude_json, dict) else None
	slug_lookup: Dict[str, str] = {}
	if isinstance(known_projects, dict):
		from .tokens import slug
		for p in known_projects.keys():
			if isinstance(p, str) and p.startswith("/"):
				slug_lookup.setdefault(slug(os.path.normpath(p)), os.path.normpath(p))
	for d in names:
		full = os.path.join(paths.projects_dir, d)
		if not os.path.isdir(full):
			continue
		if d in known_dirs:
			dir_map[d] = known_dirs[d]
			continue
		cwd = None
		try:
			files = sorted((f for f in os.listdir(full) if f.endswith(".jsonl")),
						   key=lambda f: os.path.getmtime(os.path.join(full, f)), reverse=True)
		except OSError:
			files = []
		# Le dossier de travail qu'il représente : de préférence celui dont le nom correspond (une session
		# peut avoir commencé ailleurs, puis avoir été déplacée ici par « csync move »).
		from .tokens import slug as _slug
		candidates: List[str] = []
		for f in files[:5]:
			fp = os.path.join(full, f)
			for c in (session_cwd(fp, adapter), session_last_cwd(fp, adapter)):
				if c and c not in candidates:
					candidates.append(c)
			if any(_slug(os.path.normpath(c)) == d for c in candidates):
				break
		matching = [c for c in candidates if _slug(os.path.normpath(c)) == d]
		cwd = (matching or candidates or [None])[0]
		if not cwd:
			cwd = slug_lookup.get(d)
		if not cwd and fallback_dirs:
			# Dossier qui ne contient plus de session (ex. seulement « memory/ » après la purge
			# automatique) : on garde ce qu'on savait de lui.
			cwd = fallback_dirs.get(d)
		if cwd:
			dir_map[d] = os.path.normpath(cwd)
		else:
			unresolved.append(d)
	return dir_map, unresolved


def classify(cwd: str, paths: Paths) -> Optional[Dict[str, Any]]:
	"""Projet auquel appartient un dossier de travail, ou None (le dossier personnel suffit)."""
	home = paths.home
	ident = git_identity(cwd)
	if ident:
		root, key = ident
		# Un dépôt qui englobe le dossier personnel ou la configuration de Claude n'est pas un projet.
		if _is_within(home, root) or _is_within(root, paths.claude_dir) or _is_within(root, paths.csync_dir):
			return None
		return {"key": key, "path": root, "kind": "git", "include": True}
	if not os.path.isdir(cwd):
		return None
	real_home = os.path.realpath(home)
	if _is_within(cwd, home) or _is_within(cwd, real_home):
		return None
	# La racine du disque ou un dossier qui contient le dossier personnel (/Users, /home) ou la
	# configuration de Claude n'est pas un projet.
	if cwd in ("/", "") or any(_is_within(x, cwd) for x in (home, real_home, paths.claude_dir, paths.csync_dir)):
		return None
	# Dossier sans git hors du dossier personnel : rien ne permet de savoir à quel dossier il
	# correspond sur l'autre machine (deux dossiers « api » peuvent n'avoir aucun rapport). Il
	# n'est donc pas partagé tant que vous ne lui avez pas donné un nom commun sur les deux machines.
	return {"key": "unconfirmed:" + cwd, "path": cwd, "kind": "dir", "include": False, "to_confirm": True}


# Dossiers protégés par macOS : y accéder depuis une tâche de fond déclenche une demande
# d'autorisation. Ils ne sont examinés que lors d'une action de votre part.
PROTECTED_DIRS = ("Desktop", "Documents", "Downloads", "Library/Mobile Documents")


def is_protected(path: str, home: str) -> bool:
	if system_name() != "darwin":
		return False
	return any(_is_within(path, os.path.join(home, d)) for d in PROTECTED_DIRS)


SEARCH_SKIP = {
	"Library", "Applications", "Pictures", "Music", "Movies", "Public", "node_modules", "__pycache__", "venv",
	".venv", "env", "target", "build", "dist", "vendor", "Pods", "DerivedData", "snap", "go", "miniconda3",
	"anaconda3", "opt", "OneDrive", "Dropbox", "Google Drive",
}


def find_git_repos(home: str, wanted: set, max_depth: int = 5, budget_seconds: float = 20.0,
				   exclude: Tuple[str, ...] = (), protected_ok: bool = True) -> List[Dict[str, Any]]:
	"""Cherche dans le dossier personnel les dépôts git correspondant aux projets d'une autre machine.

	La recherche s'arrête dès que tous les projets voulus sont trouvés, ou après le délai imparti.
	Les dossiers cachés et les dossiers volumineux habituels (Library, node_modules…) sont ignorés.
	"""
	import time
	found: List[Dict[str, Any]] = []
	remaining = set(wanted)
	if not remaining:
		return found
	deadline = time.time() + budget_seconds
	stack: List[Tuple[str, int]] = [(home, 0)]
	while stack and remaining and time.time() < deadline:
		directory, depth = stack.pop()
		try:
			entries = sorted(os.scandir(directory), key=lambda e: e.name)
		except OSError:
			continue
		if directory != home and any(e.name == ".git" for e in entries):
			ident = git_identity(directory)
			if ident and ident[1] in remaining:
				found.append({"key": ident[1], "path": ident[0], "kind": "git", "include": True})
				remaining.discard(ident[1])
			continue
		if depth >= max_depth:
			continue
		for e in reversed(entries):
			if not e.is_dir(follow_symlinks=False) or e.name.startswith(".") or e.name in SEARCH_SKIP:
				continue
			if any(_is_within(e.path, x) for x in exclude):
				continue
			if not protected_ok and is_protected(e.path, home):
				continue
			stack.append((e.path, depth + 1))
	return found


class ScanResult:
	def __init__(self, profile: Dict[str, Any], diff: Dict[str, List[Dict[str, Any]]]):
		self.profile = profile
		self.diff = diff

	@property
	def has_changes(self) -> bool:
		return any(self.diff.get(k) for k in ("added", "removed", "moved", "to_confirm"))


def scan(paths: Paths, adapter: Dict[str, Any], machine_id: str, machine_name: str,
		 previous: Optional[Dict[str, Any]] = None, remote_profiles: Optional[Dict[str, Dict[str, Any]]] = None,
		 full: bool = True, search: bool = False, protected_ok: bool = True) -> ScanResult:
	"""Scan de la machine.

	full=False : scan rapide (utilisé avant chaque synchronisation) qui reprend le profil
	existant et n'examine que les nouveaux dossiers de travail.
	search=True (scan rapide hors hooks) : cherche aussi les projets des autres machines,
	mais seulement quand il en apparaît un nouveau, et au plus une fois par jour sinon.
	"""
	previous = previous or {}
	prev_projects = {p["path"]: p for p in previous.get("projects", [])}
	known_dirs = {} if full else dict(previous.get("dir_map", {}))
	dir_map, unresolved = collect_cwds(paths, adapter, known_dirs, fallback_dirs=previous.get("dir_map", {}))

	projects: Dict[str, Dict[str, Any]] = {}
	by_key: Dict[str, str] = {}
	warnings: List[str] = []

	def add_project(entry: Dict[str, Any]) -> None:
		path = entry["path"]
		if path in projects:
			return
		other = by_key.get(entry["key"])
		if other and other != path and projects.get(other, {}).get("missing"):
			del projects[other]      # projet retrouvé à un nouvel emplacement
			other = None
		if other and other != path:
			warnings.append("Deux dossiers correspondent au même projet %s : %s et %s. Seul le premier est utilisé."
							% (entry["key"], other, path))
			return
		projects[path] = entry
		by_key[entry["key"]] = path

	# 1. Projets déjà connus (noms choisis par l'utilisateur, inclusions).
	#    Un projet introuvable est gardé, marqué absent, lors des scans rapides (disque externe
	#    débranché, dossier déplacé) : il est remplacé dès qu'on le retrouve ailleurs. Un scan
	#    complet, fait à votre demande, oublie les projets git disparus.
	used_cwds = set(dir_map.values())
	for path, entry in prev_projects.items():
		entry = dict(entry)
		if not protected_ok and is_protected(path, paths.home):
			add_project(entry)       # tâche de fond : dossier protégé non examiné, rien ne change
			continue
		if str(entry.get("key", "")).startswith("unconfirmed:"):
			if path in used_cwds and os.path.isdir(path):
				add_project(entry)
			continue
		if os.path.isdir(path):
			entry.pop("missing", None)
			add_project(entry)
		elif not full or entry.get("kind") == "named":
			entry["missing"] = True
			add_project(entry)

	# 2. Nouveaux dossiers de travail. S'il manque un projet, les dossiers déjà examinés le sont
	#    de nouveau : le projet est peut-être revenu à l'un d'eux.
	recheck = any(e.get("missing") for e in projects.values())
	seen_cwds = set()
	for cwd in sorted(set(dir_map.values())):
		if cwd in seen_cwds:
			continue
		seen_cwds.add(cwd)
		if any(_is_within(cwd, p) for p, e in projects.items()
			   if not e.get("missing") and not str(e.get("key", "")).startswith("unconfirmed:")):
			continue
		if not full and not recheck and cwd in previous.get("checked_cwds", []):
			continue
		if not protected_ok and is_protected(cwd, paths.home):
			seen_cwds.discard(cwd)   # examiné plus tard, lors d'une action de votre part
			continue
		entry = classify(cwd, paths)
		if entry and entry["path"] not in prev_projects:
			add_project(entry)

	# 3. Projets connus sur les autres machines mais pas encore ici : recherche dans le dossier
	#    personnel, uniquement lors d'un scan complet (configuration ou « Rescanner »).
	wanted = set()
	for rprof in (remote_profiles or {}).values():
		for p in rprof.get("projects", []) or []:
			key = p.get("key", "")
			mine = projects.get(by_key.get(key, ""), {})
			if p.get("include", True) and str(key).startswith("git:") and (key not in by_key or mine.get("missing")):
				wanted.add(key)
	last_search = previous.get("last_repo_search") or 0
	searched_keys = list(previous.get("searched_keys", []))
	searched = None
	new_keys = wanted - set(searched_keys)
	if wanted and (full or (search and protected_ok and (new_keys or now() - last_search > 86400))):
		searched = now()
		searched_keys = sorted(wanted | set(searched_keys))
		for entry in find_git_repos(paths.home, wanted, budget_seconds=20.0 if full else 10.0,
									exclude=(paths.claude_dir, paths.csync_dir), protected_ok=protected_ok):
			add_project(entry)

	format_report = check_format(adapter, paths.projects_dir, paths.claude_json, limit=40 if full else 12)
	claude_json = read_json_safe(paths.claude_json, None)
	mcp = get_path(claude_json, adapter["claude_json_mcp_key"]) if isinstance(claude_json, dict) else None
	settings = read_json_safe(paths.settings_file, None)
	plugins = []
	if isinstance(settings, dict) and isinstance(settings.get("enabledPlugins"), dict):
		plugins = sorted(k for k, v in settings["enabledPlugins"].items() if v)

	profile = {
		"format": PROFILE_FORMAT,
		"machine_id": machine_id,
		"machine_name": machine_name,
		"system": system_name(),
		"updated": iso(),
		"home": paths.home,
		"claude_dir": paths.claude_dir,
		"claude_json": paths.claude_json,
		"claude_version": ((claude_version() if full or not previous.get("claude_version") else None)
						   or previous.get("claude_version")),
		"projects": sorted(projects.values(), key=lambda p: p["path"]),
		"dir_map": dir_map,
		"checked_cwds": sorted(seen_cwds | set(previous.get("checked_cwds", []))),
		"unresolved_dirs": unresolved,
		"format_check": format_report.to_dict(),
		"plugins": plugins,
		"mcp_servers": sorted(mcp.keys()) if isinstance(mcp, dict) else [],
		"warnings": warnings,
		"last_repo_search": searched if searched is not None else last_search,
		"searched_keys": searched_keys[-500:],
	}

	diff: Dict[str, List[Dict[str, Any]]] = {"added": [], "removed": [], "moved": [], "to_confirm": [], "remote_only": []}
	prev_by_key = {p["key"]: p for p in previous.get("projects", [])}
	for p in profile["projects"]:
		old = prev_by_key.get(p["key"])
		if old is None:
			diff["added"].append(p)
		elif old["path"] != p["path"]:
			diff["moved"].append({"key": p["key"], "old": old["path"], "new": p["path"]})
		if p.get("to_confirm"):
			diff["to_confirm"].append(p)
	if full:
		current_keys = {p["key"] for p in profile["projects"]}
		for p in previous.get("projects", []):
			if p["key"] not in current_keys:
				diff["removed"].append(p)
	local_keys = {p["key"] for p in profile["projects"]}
	for machine, rprof in (remote_profiles or {}).items():
		for p in rprof.get("projects", []):
			if p.get("include", True) and p["key"] not in local_keys:
				diff["remote_only"].append({"key": p["key"], "machine": rprof.get("machine_name", machine),
											"remote_path": p["path"]})
	return ScanResult(profile, diff)


def save_profile(paths: Paths, profile: Dict[str, Any]) -> None:
	write_json(paths.profile_file, profile, 0o600)


def load_profile(paths: Paths) -> Optional[Dict[str, Any]]:
	return read_json_safe(paths.profile_file, None)


def build_mapper(paths: Paths, profile: Dict[str, Any]) -> PathMapper:
	mappings: List[Tuple[str, str]] = [(paths.claude_dir, "CLAUDE")]
	for p in profile.get("projects", []):
		if p.get("include", True) and not p.get("missing") and not str(p.get("key", "")).startswith("unconfirmed:"):
			mappings.append((p["path"], "P:" + p["key"]))
	mappings.append((paths.home, "HOME"))
	# Dossier personnel ou configuration atteints par un lien symbolique : les deux écritures
	# d'un même chemin sont reconnues (la première sert à l'écriture des chemins reçus).
	for path, name in ((paths.claude_dir, "CLAUDE"), (paths.home, "HOME")):
		real = os.path.realpath(path)
		if real != os.path.normpath(path):
			mappings.append((real, name))
	return PathMapper(mappings)


def apply_user_choices(profile: Dict[str, Any], choices: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
	"""Applique les choix de l'écran de validation : {chemin: {"include": bool, "name": str}}."""
	updated = []
	used = set()
	for p in profile.get("projects", []):
		p = dict(p)
		choice = choices.get(p["path"], {})
		if "include" in choice:
			p["include"] = bool(choice["include"])
		name = (choice.get("name") or "").strip()
		if name and p["kind"] != "git":
			p["key"] = "dir:" + re.sub(r"\s+", "_", name)
			p["kind"] = "named"
			p.pop("to_confirm", None)
		if str(p["key"]).startswith("unconfirmed:"):
			p["include"] = False      # pas de nom commun : jamais partagé
		elif "include" not in choice and p.get("to_confirm"):
			p["include"] = False
		if not str(p["key"]).startswith("unconfirmed:"):
			p.pop("to_confirm", None)
		if p["key"] in used:
			LOG.warning("Nom partagé en double ignoré : %s", p["key"])
			p["include"] = False
		used.add(p["key"])
		updated.append(p)
	profile = dict(profile)
	profile["projects"] = updated
	return profile


def add_named_project(profile: Dict[str, Any], path: str, name: str) -> Dict[str, Any]:
	"""Déclare manuellement un dossier comme projet partagé sous un nom commun."""
	path = os.path.normpath(os.path.abspath(path))
	profile = dict(profile)
	projects = [p for p in profile.get("projects", []) if p["path"] != path]
	projects.append({"key": "dir:" + re.sub(r"\s+", "_", name.strip()), "path": path, "kind": "named", "include": True})
	profile["projects"] = sorted(projects, key=lambda p: p["path"])
	return profile
