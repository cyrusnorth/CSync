"""Moteur de synchronisation : inventaire local, envoi (push), réception et fusion (pull).

Trois sortes d'éléments :

- « jsonl » : fichiers de session de Claude Code. Ils ne font en général que s'allonger ; on
  les découpe en tranches. Une session distante qui prolonge la nôtre est installée ; une
  session réécrite ailleurs (et que nous n'avions pas modifiée) remplace la nôtre ; deux
  sessions qui ont réellement divergé donnent une copie de conflit, jamais un écrasement.
- « file » : configuration (CLAUDE.md, settings.json, skills, agents…). Chaque modification
  locale reçoit un identifiant d'événement unique ; chaque machine publie l'historique des
  événements qu'elle a connus. Une version distante qui descend de la nôtre est appliquée ;
  une divergence produit une copie de conflit.
- « mcp » : chaque serveur MCP personnel de ~/.claude.json, traité comme un fichier.

Règles de prudence :
- toute écriture locale est précédée d'une copie de sauvegarde ;
- un fichier absent n'est considéré comme supprimé qu'après confirmation (deux passages
  espacés d'au moins une minute), et jamais en masse sans accord explicite ;
- rien n'est supprimé sous ~/.claude/projects (sessions, mémoire) ;
- les liens symboliques ne sont ni lus ni écrits.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import __version__
from .adapter import get_path, load_adapter, set_path
from .crypto import Cipher, load_key
from .hooks import install_hooks, settings_has_our_hooks, strip_our_hooks
from .paths import MachineConfig, Paths
from .scan import build_mapper, load_profile, save_profile, scan
from .state import State, push_hist
from .store import Store
from .tokens import PathMapper, make_token, slug
from .util import (CSyncError, LOG, atomic_write_bytes, canonical_json, claude_running, ensure_dir, now,
				   path_inside, prune_empty_dirs, read_bytes, read_json_safe, remove_file, sha256_hex,
				   symlink_on_path)

CHUNK_MAX_BYTES = 64 * 1024
VH_PUBLISHED = 12
DELETE_GRACE_SECONDS = 60

CONFIG_FILES = ("CLAUDE.md", "settings.json", "keybindings.json")
CONFIG_DIRS = ("skills", "agents", "commands", "output-styles", "rules", "workflows", "themes", "agent-memory", "hooks")
# Dossiers internes de Claude Code, jamais synchronisés même si settings.json y fait référence.
DENY_TOP = {"projects", "sessions", "todos", "shell-snapshots", "statsig", "ide", "plugins", "local", "debug",
			"file-history", "session-env", "paste-cache", "logs", "cache", "backups", "downloads", "telemetry",
			"history.jsonl", "plans", "usage-data"}
LOCAL_KEYS_MARKER = "__csync_local_keys"
# Chemins vers des fichiers de ~/.claude cités dans settings.json (scripts de hooks, barre d'état…).
_REF_PREFIXES = ("~/.claude/", "$HOME/.claude/", "${HOME}/.claude/", "$CLAUDE_CONFIG_DIR/", "${CLAUDE_CONFIG_DIR}/")
JSON_TOKENIZED = ("settings.json",)
SKIP_DIR_NAMES = {".git", "__pycache__", "node_modules", ".trash", ".venv", "venv"}
SKIP_TOP_SKILL_DIRS = {"synced", ".trash"}
# Fichiers de fonctionnement (journaux, bases, verrous) que des scripts écrivent parfois dans
# ~/.claude/hooks ou dans un skill : propres à chaque machine, jamais synchronisés.
RUNTIME_SUFFIXES = (".log", ".lock", ".pid", ".tmp", ".swp", ".sqlite", ".sqlite3", ".db", ".db-journal",
					".db-wal", ".db-shm", ".jsonl")
RUNTIME_DIRS = {"logs", "log", "cache", ".cache", "tmp", "state"}
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ENCODING = "surrogateescape"  # conserve à l'identique les octets qui ne seraient pas de l'UTF-8 valide


# ---------------------------------------------------------------------------
# Rapport d'exécution
# ---------------------------------------------------------------------------

class Report:
	def __init__(self, action: str, dry_run: bool = False):
		self.action = action
		self.dry_run = dry_run
		self.counts: Dict[str, int] = {}
		self.messages: List[str] = []
		self.warnings: List[str] = []
		self.parked: List[Dict[str, Any]] = []
		self.stale_current: List[str] = []   # machines où la conversation reprise a avancé
		self.pushed = False

	def count(self, name: str, n: int = 1) -> None:
		self.counts[name] = self.counts.get(name, 0) + n

	def warn(self, text: str) -> None:
		LOG.warning(text)
		if text not in self.warnings:
			self.warnings.append(text)

	def note(self, text: str) -> None:
		LOG.info(text)
		self.messages.append(text)

	LABELS = {
		"sessions_new": "sessions reçues",
		"sessions_extended": "sessions complétées",
		"sessions_rewritten": "sessions remplacées par leur version réécrite sur l'autre machine",
		"sessions_conflict": "sessions en conflit (copie créée)",
		"sessions_active_skipped": "sessions en cours d'utilisation laissées intactes",
		"sessions_deleted": "conversations supprimées (supprimées sur l'autre machine)",
		"files_applied": "fichiers de configuration mis à jour",
		"files_deleted": "fichiers supprimés (supprimés sur l'autre machine)",
		"files_conflict": "fichiers en conflit (copie créée)",
		"mcp_applied": "serveurs MCP mis à jour",
		"mcp_pending": "changements MCP en attente (Claude Code ouvert)",
		"entries_changed": "éléments modifiés envoyés",
		"deletions_sent": "suppressions envoyées",
		"objects_written": "blocs envoyés",
		"skipped_large": "fichiers trop gros ignorés",
		"skipped_unsafe": "éléments ignorés par sécurité",
		"parked": "sessions en attente (projet inconnu ici)",
	}

	def summary_lines(self) -> List[str]:
		lines = []
		prefix = "[à blanc] " if self.dry_run else ""
		for key, label in self.LABELS.items():
			n = self.counts.get(key)
			if n:
				lines.append("%s%s : %d" % (prefix, label, n))
		if not lines:
			lines.append(prefix + "Rien à faire : tout est à jour.")
		return lines + self.warnings


# ---------------------------------------------------------------------------
# Tranches de sessions
# ---------------------------------------------------------------------------

def split_complete_lines(data: bytes) -> Tuple[bytes, List[str]]:
	"""Ne garde que les lignes terminées (une dernière ligne en cours d'écriture est ignorée)."""
	cut = data.rfind(b"\n") + 1
	complete = data[:cut]
	if not complete:
		return complete, []
	return complete, complete.decode("utf-8", ENCODING).split("\n")[:-1]


def chunk_lines(lines: List[str], first: Optional[List[str]] = None) -> List[List[str]]:
	"""Découpage glouton : une tranche se ferme quand la ligne suivante la ferait dépasser la limite."""
	chunks: List[List[str]] = []
	current: List[str] = list(first or [])
	size = sum(len(l.encode("utf-8", ENCODING)) + 1 for l in current)
	for line in lines:
		n = len(line.encode("utf-8", ENCODING)) + 1
		if current and size + n > CHUNK_MAX_BYTES:
			chunks.append(current)
			current, size = [], 0
		current.append(line)
		size += n
	if current:
		chunks.append(current)
	return chunks


def chunk_bytes(lines: List[str]) -> bytes:
	return ("\n".join(lines) + "\n").encode("utf-8", ENCODING)


def parse_chunk(data: bytes) -> List[str]:
	return data.decode("utf-8", ENCODING).split("\n")[:-1]


def version_hash(chunks: List[str]) -> str:
	"""Empreinte courte d'une version complète de session (liste de tranches)."""
	return sha256_hex("\n".join(chunks).encode("ascii"))[:20]


def push_vh(st: Dict[str, Any], chunks: List[str], extra: Iterable[str] = ()) -> None:
	"""Mémorise les versions successives d'une session (sert à reconnaître une réécriture)."""
	vh = st.setdefault("vh", [])
	for h in list(extra) + [version_hash(chunks)]:
		if h in vh:
			vh.remove(h)
		vh.append(h)
	del vh[:-60]


def merge_hist(local: List[str], remote: List[str], head: str) -> List[str]:
	out: List[str] = []
	for v in list(remote) + list(local):
		if v not in out:
			out.append(v)
	if head in out:
		out.remove(head)
	out.append(head)
	return out[-40:]


def event_content(event: Optional[str]) -> Optional[str]:
	"""Identifiant de contenu d'un événement (None pour une suppression)."""
	if not event:
		return None
	content = event.split("@", 1)[0]
	return None if content == "deleted" else content


# ---------------------------------------------------------------------------
# Moteur
# ---------------------------------------------------------------------------

def _is_executable(path: str) -> bool:
	"""Droit d'exécution du propriétaire, lu dans les permissions (os.access répond « non » sur
	un disque monté sans droit d'exécution, même pour un script exécutable)."""
	try:
		return bool(os.stat(path).st_mode & 0o100)
	except OSError:
		return False


def _pid_alive(pid: int) -> bool:
	try:
		os.kill(pid, 0)
	except ProcessLookupError:
		return False
	except (PermissionError, OSError):
		return True
	return True


def append_bytes(path: str, data: bytes) -> None:
	"""Ajoute à la fin d'un fichier sans le remplacer (le fichier reste le même pour qui l'a ouvert)."""
	fd = os.open(path, os.O_WRONLY | os.O_APPEND)
	try:
		view = memoryview(data)
		while view:
			n = os.write(fd, view)
			view = view[n:]
		os.fsync(fd)
	finally:
		os.close(fd)


class LocalItem:
	__slots__ = ("key", "kind", "path", "tok", "extra")

	def __init__(self, key: str, kind: str, path: str, tok: str = "none", extra: Any = None):
		self.key = key
		self.kind = kind
		self.path = path
		self.tok = tok
		self.extra = extra


class Engine:
	def __init__(self, paths: Paths, config: MachineConfig, cipher: Optional[Cipher] = None, hook_mode: bool = False,
				 background: bool = False):
		self.paths = paths
		self.config = config
		self.hook_mode = hook_mode
		self.background = background
		# Le dossier des conversations peut être un lien vers un autre disque : on travaille
		# directement dans le dossier réel.
		self.projects_root = os.path.realpath(paths.projects_dir)
		self._current_session: Optional[str] = None
		self._links: Set[str] = set()
		self._warned: Set[str] = set()
		if cipher is None:
			cipher = Cipher(load_key(config.key_path))
		self.cipher = cipher
		timeout = 10 if hook_mode else float(config.get("git_timeout", 120))
		self.store = Store(paths.repo_dir, cipher, config.machine_id, config.branch, timeout)
		self.store.light = hook_mode
		self.state = State.load(paths)
		self.adapter = load_adapter(paths.adapter_file)
		self.profile: Dict[str, Any] = load_profile(paths) or {}
		self.mapper: PathMapper = build_mapper(paths, self.profile)
		self.report: Optional[Report] = None
		self._backup_dir: Optional[str] = None
		self._new_plain: Dict[str, bytes] = {}
		self._write_objects = False
		self._created: Set[str] = set()
		self._cj: Any = None
		self._cj_loaded = False
		self._unmanaged: Set[str] = set()
		self._mcp_unknown = False
		self._mcp_writable = False

	# ------------------------------------------------------------------
	# Contenus
	# ------------------------------------------------------------------

	def _store_plain(self, plain: bytes) -> str:
		"""Enregistre un contenu calculé localement et renvoie son identifiant.

		En fonctionnement normal, le bloc chiffré est écrit tout de suite dans le dépôt local :
		tout identifiant mémorisé dans l'état correspond donc toujours à un bloc existant.
		"""
		oid = self.cipher.object_id(plain)
		if self.store.find_object(oid) is not None or oid in self._new_plain:
			return oid
		if self._write_objects:
			self.store.put_object(plain)
		else:
			self._new_plain[oid] = plain
		self._created.add(oid)
		return oid

	def _get(self, oid: str) -> bytes:
		plain = self._new_plain.get(oid)
		if plain is not None:
			return plain
		return self.store.get_object(oid)

	def _object_available(self, oid: str) -> bool:
		return oid in self._new_plain or self.store.find_object(oid) is not None

	def _begin(self, action: str, dry_run: bool) -> Report:
		self.report = Report(action, dry_run)
		self._write_objects = not dry_run
		self._new_plain = {}
		self._created = set()
		self._backup_dir = None
		self._cj_loaded = False
		self._unmanaged = set()
		self._mcp_unknown = False
		self._mcp_writable = False
		self._links = set()
		self._warned = set()
		return self.report

	# ------------------------------------------------------------------
	# Préparation
	# ------------------------------------------------------------------

	def refresh_profile(self, full: bool = False) -> None:
		# Le profil est relu : vos choix enregistrés entre-temps (écran du scan) sont respectés.
		self.profile = load_profile(self.paths) or self.profile
		remote_profiles = self.store.other_profiles() if self.store.is_cloned() else {}
		result = scan(self.paths, self.adapter, self.config.machine_id, self.config.machine_name,
					  previous=self.profile, remote_profiles=remote_profiles, full=full, search=not self.hook_mode,
					  protected_ok=not self.background)
		self.profile = result.profile
		save_profile(self.paths, self.profile)
		self.mapper = build_mapper(self.paths, self.profile)

	def _check_repo(self) -> None:
		if not self.store.is_cloned():
			raise CSyncError("Le dépôt de données n'est pas cloné. Relancez « csync setup ».")
		self.store.check_format()
		self.store.verify_key()

	def _check_claude_dir(self) -> None:
		if not os.path.isdir(self.paths.claude_dir):
			raise CSyncError("Le dossier de configuration de Claude Code (%s) est introuvable : synchronisation "
							 "suspendue pour ne rien supprimer par erreur." % self.paths.claude_dir)

	def _sessions_enabled(self) -> bool:
		return bool(self.profile.get("format_check", {}).get("sessions_ok", True))

	def _mcp_enabled(self) -> bool:
		return bool(self.profile.get("format_check", {}).get("mcp_ok", True))

	# ------------------------------------------------------------------
	# Inventaire local
	# ------------------------------------------------------------------

	def _warn_once(self, text: str) -> None:
		if text not in self._warned:
			self._warned.add(text)
			if self.report:
				self.report.warn(text)

	def _note_link(self, path: str) -> None:
		self._links.add(path)

	def _walk_files(self, root: str, rel_prefix: str, top_skip: Set[str] = frozenset(),
					config: bool = False) -> Iterable[Tuple[str, str]]:
		if os.path.islink(root):
			self._note_link(root)
			return
		if not os.path.isdir(root):
			return
		for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
			rel_dir = os.path.relpath(dirpath, root)
			keep = []
			for d in dirnames:
				full = os.path.join(dirpath, d)
				if d in SKIP_DIR_NAMES or (config and d in RUNTIME_DIRS):
					continue
				if os.path.islink(full):
					self._note_link(full)
					continue
				if rel_dir == "." and d in top_skip:
					continue
				keep.append(d)
			dirnames[:] = sorted(keep)
			for f in sorted(filenames):
				full = os.path.join(dirpath, f)
				if f == ".DS_Store" or f.startswith(".csync-tmp-") or ".csync-conflict-" in f:
					continue
				if config and f.lower().endswith(RUNTIME_SUFFIXES):
					continue
				if os.path.islink(full):
					self._note_link(full)
					continue
				rel = os.path.normpath(os.path.join(rel_prefix, rel_dir, f)).replace(os.sep, "/")
				try:
					rel.encode("utf-8")
				except UnicodeEncodeError:
					self._warn_once("Fichier ignoré (nom dans un encodage non pris en charge) : %s"
									% full.encode("utf-8", "replace").decode("utf-8"))
					continue
				yield rel, full

	# -- réglages propres à cette machine, fichiers cités par settings.json --------------------

	def _local_settings_keys(self) -> List[str]:
		keys = self.config.get("local_settings_keys") or []
		return [k for k in keys if isinstance(k, str)]

	def _local_mcp_names(self) -> Set[str]:
		return {k for k in (self.config.get("local_mcp_servers") or []) if isinstance(k, str)}

	def _config_rel_allowed(self, rel: str) -> bool:
		"""Fichier de ~/.claude que CSync peut lire et écrire."""
		if not self._safe_rel(rel):
			return False
		parts = rel.split("/")
		first = parts[0]
		if first in CONFIG_FILES:
			return len(parts) == 1
		if first in CONFIG_DIRS:
			return not (first == "skills" and len(parts) > 1 and parts[1] in SKIP_TOP_SKILL_DIRS)
		return (not first.startswith(".") and first not in DENY_TOP and len(parts) <= 4
				and not rel.endswith(".jsonl") and first != "settings.local.json")

	def _referenced_files(self) -> List[Tuple[str, str]]:
		"""Fichiers de ~/.claude cités dans settings.json (scripts de hooks, barre d'état…)."""
		raw = read_bytes(self.paths.settings_file)
		if not raw:
			return []
		text = raw.decode("utf-8", "replace")
		prefixes = list(_REF_PREFIXES) + [self.paths.claude_dir.rstrip("/") + "/"]
		if os.path.normpath(self.paths.claude_dir) != os.path.join(self.paths.home, ".claude"):
			prefixes = [p for p in prefixes if ".claude/" not in p or p.startswith(self.paths.claude_dir)]
		pattern = "(?:" + "|".join(re.escape(p) for p in prefixes) + r")([A-Za-z0-9_.\-/]+)"
		found = []
		for m in re.finditer(pattern, text):
			rel = os.path.normpath(m.group(1).rstrip(".")).replace(os.sep, "/")
			if rel.split("/")[0] in CONFIG_FILES + CONFIG_DIRS or not self._config_rel_allowed(rel):
				continue
			full = os.path.join(self.paths.claude_dir, rel)
			if os.path.isfile(full) and not os.path.islink(full) and not symlink_on_path(full, self.paths.claude_dir):
				found.append((rel, full))
		return sorted(set(found))

	def _claude_json(self) -> Any:
		"""~/.claude.json, lu une seule fois par opération (None s'il est absent ou illisible)."""
		if not self._cj_loaded:
			path = os.path.realpath(self.paths.claude_json)
			self._cj = read_json_safe(path, None) if os.path.isfile(path) else None
			self._cj_loaded = True
		return self._cj

	def _local_mcp(self) -> Optional[Dict[str, Any]]:
		"""Serveurs MCP locaux, ou None si on ne peut pas les connaître avec certitude.

		Un ~/.claude.json absent ou sans la clé des serveurs n'est pas « zéro serveur » :
		dans le doute, rien n'est publié comme supprimé. Un fichier lisible sans cette clé
		peut en revanche recevoir les serveurs des autres machines."""
		data = self._claude_json()
		if not isinstance(data, dict):
			return None
		self._mcp_writable = True
		servers = get_path(data, self.adapter["claude_json_mcp_key"])
		if not isinstance(servers, dict):
			return None
		return dict(servers)

	def _excluded_roots(self) -> List[str]:
		return [p["path"] for p in self.profile.get("projects", []) if not p.get("include", True)]

	def _is_excluded(self, cwd: str) -> bool:
		"""Un dossier de travail est exclu si le projet le plus précis qui le contient est exclu
		(un dossier non partagé n'exclut pas les projets rangés à l'intérieur)."""
		best = None
		for p in self.profile.get("projects", []):
			path = p.get("path")
			if path and path_inside(cwd, path) and (best is None or len(path) > len(best["path"])):
				best = p
		return best is not None and not best.get("include", True)

	def local_items(self) -> Dict[str, LocalItem]:
		items: Dict[str, LocalItem] = {}
		cdir = self.paths.claude_dir
		for name in CONFIG_FILES:
			p = os.path.join(cdir, name)
			if os.path.islink(p):
				self._unmanaged.add("c|" + name)
				self._note_link(p)
				continue
			if os.path.isfile(p):
				items["c|" + name] = LocalItem("c|" + name, "file", p, "json" if name in JSON_TOKENIZED else "none")
		for d in CONFIG_DIRS:
			top_skip = SKIP_TOP_SKILL_DIRS if d == "skills" else frozenset()
			for rel, full in self._walk_files(os.path.join(cdir, d), d, top_skip, config=True):
				items["c|" + rel] = LocalItem("c|" + rel, "file", full)
		for rel, full in self._referenced_files():
			items.setdefault("c|" + rel, LocalItem("c|" + rel, "file", full))
		if self._mcp_enabled():
			servers = self._local_mcp()
			if servers is None:
				self._mcp_unknown = True
			else:
				local_only = self._local_mcp_names()
				for name, conf in servers.items():
					if name in local_only:
						continue
					try:
						name.encode("utf-8")
					except UnicodeEncodeError:
						continue
					items["m|" + name] = LocalItem("m|" + name, "mcp", self.paths.claude_json, extra=conf)
		else:
			self._mcp_unknown = True
		if self._sessions_enabled():
			dir_map = self.profile.get("dir_map", {})
			path_keys = {st["path"]: key for key, st in self.state.entries.items()
						 if key.startswith("p|") and st.get("path")}
			dir_tok = self.state.data.setdefault("dir_tok", {})
			copies = self.state.data.setdefault("conflict_copies", {})
			for d, cwd in sorted(dir_map.items()):
				if self._is_excluded(cwd):
					continue  # projet exclu, ou dossier non partagé : rien ne part
				root = os.path.join(self.projects_root, d)
				if os.path.islink(root):
					self._note_link(root)
					continue
				if not os.path.isdir(root):
					continue
				tokcwd = dir_tok.get(d)
				if not tokcwd:
					tokcwd = self.mapper.tokenize_path(cwd)
					dir_tok[d] = tokcwd
				for rel, full in self._walk_files(root, ""):
					base = os.path.basename(rel)
					if ".orphaned-" in base or ".superseded-" in base:
						continue
					if full in copies:
						# Copie de conflit : reste locale tant que vous ne l'avez pas reprise.
						data = read_bytes(full)
						if data is not None and sha256_hex(data) == copies[full]:
							continue
						del copies[full]
					kind = "jsonl" if rel.endswith(".jsonl") else "file"
					key = path_keys.get(full) or "p|%s|%s" % (tokcwd, rel)
					items[key] = LocalItem(key, kind, full)
		return items

	# ------------------------------------------------------------------
	# Version locale d'un élément (avec réutilisation de l'état)
	# ------------------------------------------------------------------

	def _stat(self, path: str) -> Optional[Tuple[int, int]]:
		try:
			s = os.stat(path)
			return s.st_size, s.st_mtime_ns
		except OSError:
			return None

	def local_jsonl_chunks(self, item: LocalItem) -> Optional[List[str]]:
		"""Tranches (identifiants) représentant la version locale d'une session."""
		st = self.state.entry(item.key)
		stat = self._stat(item.path)
		if stat is None:
			return None
		if (st.get("kind") == "jsonl" and st.get("stat") == list(stat) and st.get("chunks")
				and all(self._object_available(o) for o in st["chunks"][-1:])):
			return list(st["chunks"])
		data = read_bytes(item.path)
		if data is None:
			return None
		complete, _ = split_complete_lines(data)
		prev_len = st.get("local_len")
		prev_chunks = st.get("chunks")
		reuse = (st.get("kind") == "jsonl" and prev_chunks and isinstance(prev_len, int)
				 and prev_len <= len(complete) and sha256_hex(complete[:prev_len]) == st.get("local_hash")
				 and all(self._object_available(o) for o in prev_chunks))
		if reuse:
			# Le début du fichier n'a pas changé : sa forme neutre déjà établie est conservée
			# telle quelle, et seules les lignes ajoutées sont traduites.
			added = complete[prev_len:]
			if not added:
				new_ids = list(prev_chunks)
			else:
				_, new_lines = split_complete_lines(added)
				tok = [self.mapper.tokenize_line(l) for l in new_lines]
				last = parse_chunk(self._get(prev_chunks[-1]))
				groups = chunk_lines(tok, first=last)
				new_ids = list(prev_chunks[:-1]) + [self._store_plain(chunk_bytes(g)) for g in groups]
		else:
			_, lines = split_complete_lines(complete)
			tok = [self.mapper.tokenize_line(l) for l in lines]
			new_ids = [self._store_plain(chunk_bytes(g)) for g in chunk_lines(tok)]
		if new_ids != prev_chunks:
			push_vh(st, new_ids)
		st.update({"kind": "jsonl", "path": item.path, "chunks": new_ids, "local_len": len(complete),
				   "local_hash": sha256_hex(complete), "stat": list(stat)})
		st.pop("gone", None)
		st.pop("deleted_vh", None)
		st.pop("deleted_chunks", None)
		return new_ids

	def _file_plaintext(self, item: LocalItem, data: bytes) -> Optional[bytes]:
		"""Forme neutre d'un fichier (None si c'est un JSON illisible)."""
		if item.tok != "json":
			return data
		try:
			obj = json.loads(data.decode("utf-8"))
		except (ValueError, UnicodeDecodeError):
			return None
		if item.key == "c|settings.json" and isinstance(obj, dict):
			obj = strip_our_hooks(obj)   # les hooks de CSync restent propres à chaque machine
			local_keys = self._local_settings_keys()
			for k in local_keys:
				obj.pop(k, None)
			obj.pop(LOCAL_KEYS_MARKER, None)
			if local_keys:
				# Les autres machines laisseront ces clés telles qu'elles sont chez elles.
				obj[LOCAL_KEYS_MARKER] = sorted(local_keys)
		return (json.dumps(self.mapper.tokenize_json(obj), indent=2, ensure_ascii=False) + "\n").encode("utf-8")

	def _settings_effectively_empty(self, data: bytes) -> bool:
		"""settings.json qui ne contient que les hooks de CSync et des clés propres à cette machine."""
		try:
			obj = json.loads(data.decode("utf-8"))
		except (ValueError, UnicodeDecodeError):
			return False
		if not isinstance(obj, dict):
			return False
		obj = strip_our_hooks(obj)
		for k in self._local_settings_keys():
			obj.pop(k, None)
		return not obj

	def local_file_version(self, item: LocalItem) -> Optional[str]:
		"""Événement correspondant à la version locale d'un fichier ou d'un serveur MCP.

		Renvoie None si l'élément est absent, « invalid:… » pour un JSON illisible, « large: »
		pour un fichier trop gros (ces deux derniers ne sont jamais envoyés ni écrasés)."""
		st = self.state.entry(item.key)
		x_changed = False
		if item.kind == "mcp":
			h = sha256_hex(canonical_json(item.extra))
			if st.get("local_hash") == h and st.get("oid") and st.get("v") and self._object_available(st["oid"]):
				return st["v"]
			oid = self._store_plain(canonical_json(self.mapper.tokenize_json(item.extra)))
		else:
			stat = self._stat(item.path)
			if stat is None:
				return None
			max_bytes = int(self.config.get("max_file_mb", 50)) * 1024 * 1024
			if stat[0] > max_bytes:
				if self.report:
					self.report.count("skipped_large")
					self.report.warn("Fichier ignoré car trop gros (jamais envoyé ni remplacé) : %s" % item.path)
				return "large:"
			x_now = _is_executable(item.path)
			same_x = st.get("x", x_now) == x_now
			cached = st.get("oid") and st.get("v") and self._object_available(st["oid"]) and same_x
			if st.get("stat") == list(stat) and cached:
				return st["v"]
			data = read_bytes(item.path)
			if data is None:
				return None
			h = sha256_hex(data)
			if st.get("local_hash") == h and cached:
				st["stat"] = list(stat)
				return st["v"]
			if item.key == "c|settings.json" and self._settings_effectively_empty(data):
				return None   # ne contient que des réglages propres à cette machine : rien à partager
			plain = self._file_plaintext(item, data)
			if plain is None:
				if self.report:
					self.report.warn("%s n'est pas un JSON valide : il n'est ni envoyé ni remplacé tant qu'il "
									 "n'est pas corrigé." % item.path)
				return "invalid:" + h
			oid = self._store_plain(plain)
			st["stat"] = list(stat)
			x_changed = "x" in st and bool(st["x"]) != x_now
			st["x"] = x_now
		st.update({"kind": item.kind, "path": item.path, "local_hash": h, "tok": item.tok})
		st.pop("missing_since", None)
		if st.get("oid") != oid or not st.get("v") or x_changed:
			event = self.state.new_event(oid, self.config.machine_id)
			st.update({"oid": oid, "v": event})
			push_hist(st, event)
		return st["v"]

	# ------------------------------------------------------------------
	# Envoi
	# ------------------------------------------------------------------

	def push(self, dry_run: bool = False, allow_deletions: bool = False) -> Report:
		report = self._begin("push", dry_run)
		self._check_repo()
		self._check_claude_dir()
		if not dry_run:
			self.store.fetch_and_rebase()
		self.refresh_profile(full=False)
		try:
			meta, entries = self._build_manifest(report, allow_deletions)
		except BaseException:
			if not dry_run:
				self.state.save(self.paths)
			raise
		if dry_run:
			return report
		self.store.write_manifest(meta, entries)
		self.store.write_profile(self._public_profile())
		# L'état est enregistré avant l'envoi : si l'envoi échoue, les blocs sont déjà dans le
		# dépôt local et le prochain envoi les emportera.
		self.state.save(self.paths)
		report.pushed = self.store.commit_and_push()
		self.state.set("last_push", now())
		self.state.save(self.paths)
		if not self.hook_mode:
			self.store.maintenance()
		return report

	def _public_profile(self) -> Dict[str, Any]:
		keep = ("machine_id", "machine_name", "system", "home", "claude_version", "projects", "plugins",
				"mcp_servers", "format_check")
		prof = {k: self.profile.get(k) for k in keep}
		prof["csync_version"] = __version__
		return prof

	def _entry_for(self, key: str, item: LocalItem) -> Optional[Dict[str, Any]]:
		if item.kind == "jsonl":
			chunks = self.local_jsonl_chunks(item)
			if chunks is None:
				return None
			st = self.state.entry(key)
			if not st.get("vh"):
				push_vh(st, chunks)
			return {"k": "jsonl", "chunks": chunks, "vh": list(st.get("vh", []))[-VH_PUBLISHED:]}
		v = self.local_file_version(item)
		if v is None or v.startswith("invalid:") or v.startswith("large:"):
			return None
		st = self.state.entry(key)
		entry = {"k": item.kind, "v": v, "oid": st.get("oid"), "hist": list(st.get("hist", [v])), "tok": item.tok}
		if item.kind == "file":
			entry["x"] = bool(st.get("x"))
		return entry

	def _session_deletion_entry(self, st: Dict[str, Any], prev: Dict[str, Any]) -> Dict[str, Any]:
		return {"k": "jsonl", "deleted": True, "vh": list(st.get("deleted_vh") or [])[-VH_PUBLISHED * 5:],
				"chunks_hash": version_hash(list(st.get("deleted_chunks") or prev.get("chunks") or []))}

	def _forget_session_traces(self, key: str, st: Dict[str, Any]) -> None:
		"""Après la suppression d'une conversation : plus aucune référence à son contenu, ni dans
		l'état (le compactage pourra l'effacer du dépôt), ni dans les sauvegardes locales."""
		path = st.get("path")
		for field in ("chunks", "local_len", "local_hash", "stat", "missing_since", "conflict_seen", "conflict_ack"):
			st.pop(field, None)
		st["gone"] = True
		self.state.data["conflicts"] = [c for c in self.state.conflicts if c.get("key") != key]
		if path:
			self._purge_backups_of(path)

	def _purge_backups_of(self, path: str) -> None:
		home = self.paths.home
		rel = os.path.relpath(path, home) if path_inside(path, home) else path.lstrip("/")
		try:
			names = os.listdir(self.paths.backups_dir)
		except OSError:
			return
		for name in names:
			copy = os.path.join(self.paths.backups_dir, name, rel)
			if os.path.isfile(copy):
				remove_file(copy)

	def _entry_objects(self, entry: Dict[str, Any]) -> List[str]:
		if entry.get("k") == "jsonl":
			return list(entry.get("chunks", []))
		oid = entry.get("oid")
		return [oid] if oid else []

	def _forget_cache(self, key: str) -> None:
		st = self.state.entries.get(key)
		if st:
			for field in ("stat", "local_hash", "local_len", "chunks"):
				st.pop(field, None)

	def _build_manifest(self, report: Report, allow_deletions: bool) -> Tuple[Dict[str, Any], Dict[str, Any]]:
		old = self.store.read_manifest(self.config.machine_id)
		old_entries: Dict[str, Any] = old[1] if old else {}
		entries: Dict[str, Any] = {}
		items = self.local_items()
		for key, item in items.items():
			try:
				key.encode("utf-8")
			except UnicodeEncodeError:
				self._warn_once("Élément ignoré (nom dans un encodage non pris en charge) : %s"
								% item.path.encode("utf-8", "replace").decode("utf-8"))
				continue
			try:
				entry = self._entry_for(key, item)
				if entry is not None and not all(self._object_available(o) for o in self._entry_objects(entry)):
					# État local incohérent (bloc manquant) : on recalcule entièrement.
					self._forget_cache(key)
					entry = self._entry_for(key, item)
			except Exception as exc:  # un élément illisible ne doit jamais bloquer les autres
				LOG.exception("Élément %s", key)
				report.warn("Élément ignoré (%s) : %s" % (key, exc))
				entry = None
			if entry is None:
				st = self.state.entries.get(key, {})
				prev = old_entries.get(key)
				if (prev is not None and not key.startswith("p|") and "oid" in st and st.get("oid") is None
						and st.get("v") and event_content(st["v"]) is None and st["v"] != prev.get("v")):
					# settings.json réduit à nos hooks après une suppression reçue : on relaie la suppression.
					entries[key] = {"k": prev.get("k"), "v": st["v"], "oid": None,
									"hist": list(st.get("hist", [st["v"]])), "tok": prev.get("tok", "none")}
					report.count("entries_changed")
				elif prev is not None:
					entries[key] = prev
				continue
			if old_entries.get(key) != entry:
				report.count("entries_changed")
			entries[key] = entry

		# Éléments disparus localement.
		deletions: List[str] = []
		published_live = sum(1 for k, e in old_entries.items()
							 if not k.startswith("p|") and e.get("k") != "jsonl" and e.get("oid"))
		excluded = self._excluded_roots()
		local_mcp = self._local_mcp_names()
		# Les conversations ne sont déclarées supprimées que si leur dossier est lisible et
		# reconnu : un dossier absent ou un format non reconnu n'est jamais pris pour une suppression.
		sessions_known = self._sessions_enabled() and os.path.isdir(self.projects_root)
		excluded_tokens = [make_token("P:" + str(p.get("key"))) for p in self.profile.get("projects", [])
						   if not p.get("include", True)]
		ts = now()
		for key, prev in old_entries.items():
			if key in entries:
				continue
			if key.startswith("m|") and key[2:] in local_mcp:
				report.count("entries_changed")
				continue   # serveur MCP propre à cette machine : plus publié, jamais supprimé ailleurs
			if key.startswith("p|") and excluded:
				tokcwd = key.split("|", 2)[1]
				cwd = self.mapper.detokenize_path(tokcwd)
				if (any(tokcwd == t or tokcwd.startswith(t + "/") for t in excluded_tokens)
						or (cwd and self._is_excluded(cwd))):
					# Projet exclu : il n'est plus publié (ses contenus partiront au prochain compactage).
					report.count("entries_changed")
					continue
			st = self.state.entries.get(key, {})
			if prev.get("k") == "jsonl":
				if st.get("deleted_vh") is not None and not prev.get("deleted"):
					# Conversation supprimée sur une autre machine et supprimée ici : on relaie.
					entries[key] = self._session_deletion_entry(st, prev)
					report.count("entries_changed")
					continue
				if prev.get("deleted") or not sessions_known or not st.get("path") or os.path.lexists(st["path"]):
					entries[key] = prev
					continue
			if (prev.get("k") != "jsonl" and "oid" in st and st.get("oid") is None
					and st.get("v") and event_content(st["v"]) is None and st["v"] != prev.get("v")):
				# Suppression reçue d'une autre machine et appliquée ici : on la relaie telle quelle.
				entries[key] = {"k": prev.get("k"), "v": st["v"], "oid": None, "hist": list(st.get("hist", [st["v"]])),
								"tok": prev.get("tok", "none")}
				report.count("entries_changed")
				continue
			keep_prev = (
				(key.startswith("p|") and not sessions_known)
				or (key.startswith("m|") and self._mcp_unknown)
				or key in self._unmanaged
				or (prev.get("k") != "jsonl" and not prev.get("oid"))     # déjà une suppression
				or (st.get("path") and st.get("kind") == "file" and os.path.lexists(st["path"]))
			)
			if keep_prev:
				entries[key] = prev
				continue
			# Absence à confirmer : un fichier momentanément absent (éditeur, checkout) n'est
			# publié comme supprimé qu'au-delà d'une minute et d'au moins deux passages.
			first_seen = st.get("missing_since")
			if not first_seen:
				st["missing_since"] = ts
				self.state.entries[key] = st
				entries[key] = prev
				continue
			if ts - first_seen < DELETE_GRACE_SECONDS:
				entries[key] = prev
				continue
			deletions.append(key)
			entries[key] = prev

		if deletions:
			config_deletions = [k for k in deletions if not k.startswith("p|")]
			massive = len(config_deletions) > 5 and len(config_deletions) > 0.3 * max(published_live, 1)
			live_sessions = sum(1 for k, e in old_entries.items() if k.startswith("p|") and e.get("k") == "jsonl"
								and not e.get("deleted"))
			gone_sessions = sum(1 for k in deletions if k.startswith("p|") and old_entries[k].get("k") == "jsonl")
			if gone_sessions >= 2 and gone_sessions == live_sessions:
				massive = True   # toutes les conversations ont disparu d'un coup : sans doute pas voulu
			for prefix in ("c|", "m|"):
				# Disparition de tous les éléments d'une catégorie (tous les serveurs MCP, par exemple
				# après une réinitialisation de ~/.claude.json) : confirmation exigée, même pour un seul.
				live = sum(1 for k, e in old_entries.items() if k.startswith(prefix) and e.get("oid"))
				gone = sum(1 for k in deletions if k.startswith(prefix))
				if gone >= 1 and gone == live:
					massive = True
			if massive and not allow_deletions:
				report.warn("%d éléments (fichiers, serveurs MCP ou conversations) ont disparu d'un coup : ces "
							"suppressions ne sont PAS envoyées. Si elles sont voulues : « csync push --allow-deletions »."
							% len(deletions))
				self.state.set("deletion_guard", {"count": len(deletions), "ts": ts})
			else:
				self.state.set("deletion_guard", None)
				for key in deletions:
					prev = old_entries[key]
					st = self.state.entries.setdefault(key, {})
					self.state.set("compact_due", ts)
					if prev.get("k") == "jsonl":
						# Conversation supprimée ici : elle le sera partout.
						st["deleted_vh"] = list(dict.fromkeys(list(st.get("vh", [])) + list(prev.get("vh", []))))[-60:]
						st["deleted_chunks"] = list(prev.get("chunks") or [])
						self._forget_session_traces(key, st)
						entries[key] = self._session_deletion_entry(st, prev)
						report.count("deletions_sent")
						continue
					event = self.state.new_event(None, self.config.machine_id)
					push_hist(st, event)
					st.update({"v": event, "oid": None, "kind": prev.get("k")})
					for field in ("stat", "local_hash", "missing_since"):
						st.pop(field, None)
					entries[key] = {"k": prev.get("k"), "v": event, "oid": None, "hist": list(st["hist"]),
									"tok": prev.get("tok", "none")}
					report.count("deletions_sent")
		else:
			self.state.set("deletion_guard", None)

		# Une entrée ne doit jamais désigner un bloc absent du dépôt.
		for key in list(entries.keys()):
			missing = [o for o in self._entry_objects(entries[key]) if not self._object_available(o)]
			if missing:
				report.warn("Élément %s retiré de l'envoi : bloc %s introuvable." % (key, missing[0][:12]))
				del entries[key]
		report.count("objects_written", len(self._created))
		self.state.set("unmanaged_links", sorted(self._links)[:50])
		meta = {"format": 1, "machine_id": self.config.machine_id, "machine_name": self.config.machine_name,
				"csync_version": __version__, "updated": ts}
		return meta, entries

	# ------------------------------------------------------------------
	# Réception
	# ------------------------------------------------------------------

	def pull(self, dry_run: bool = False, current_session: Optional[str] = None,
			 prefer_remote: bool = False) -> Report:
		report = self._begin("pull", dry_run)
		self._check_repo()
		self._check_claude_dir()
		if not dry_run:
			self.store.fetch_and_rebase()
		self.refresh_profile(full=False)
		try:
			self._pull_inner(report, dry_run, current_session, prefer_remote)
		finally:
			# Même en cas d'interruption, l'état reflète exactement ce qui a été écrit.
			if not dry_run:
				self.state.save(self.paths)
		return report

	def _pull_inner(self, report, dry_run, current_session, prefer_remote) -> None:
		self._current_session = current_session
		if not dry_run:
			self._auto_resolve_conflicts()
		manifests = self.store.other_manifests()
		profiles = self.store.other_profiles()
		self._set_fallbacks(profiles)
		if not manifests:
			report.note("Aucune autre machine n'a encore envoyé de données.")
		items = self.local_items()
		active = self._active_sessions(current_session)
		mcp_changes: Dict[str, Tuple[Optional[Dict[str, Any]], Dict[str, Any]]] = {}
		mcp_blocked: Set[str] = set()
		parked: Dict[str, Dict[str, Any]] = {}

		for machine, (meta, entries) in sorted(manifests.items(), key=lambda kv: kv[1][0].get("updated", 0)):
			mname = meta.get("machine_name", machine)
			for key, rentry in sorted(entries.items()):
				try:
					if key.startswith("p|"):
						if self._sessions_enabled():
							self._pull_project_entry(key, rentry, items, active, mname, machine, profiles,
													 parked, dry_run)
					elif key.startswith("m|"):
						if key[2:] in self._local_mcp_names():
							continue   # serveur propre à cette machine
						if self._mcp_enabled() and self._mcp_writable:
							self._pull_mcp_entry(key, rentry, items, mname, machine, mcp_changes, prefer_remote, dry_run)
						elif self._mcp_enabled() and rentry.get("oid"):
							mcp_blocked.add(key[2:])
					elif key.startswith("c|") and rentry.get("k") == "file":
						self._pull_config_entry(key, rentry, items, mname, machine, prefer_remote, dry_run)
				except Exception as exc:  # un élément défectueux ne doit jamais bloquer les autres
					LOG.exception("Réception de %s", key)
					report.warn("Élément non reçu (%s) : %s" % (key, exc))

		if mcp_blocked:
			report.warn("Serveurs MCP non reçus (%s) : %s est absent ou illisible. Lancez Claude Code une fois sur "
						"cette machine, puis synchronisez." % (", ".join(sorted(mcp_blocked)), self.paths.claude_json))
		if mcp_changes:
			if dry_run:
				report.count("mcp_applied", len(mcp_changes))
			elif claude_running():
				self.state.set("pending_mcp", sorted(set(self.state.get("pending_mcp", [])) | set(mcp_changes)))
				report.count("mcp_pending", len(mcp_changes))
				report.warn("Claude Code est ouvert : les changements MCP seront appliqués quand il sera fermé.")
			else:
				try:
					self._apply_mcp(mcp_changes)
					self.state.set("pending_mcp", [])
					report.count("mcp_applied", len(mcp_changes))
				except CSyncError as exc:
					self.state.set("pending_mcp", sorted(set(self.state.get("pending_mcp", [])) | set(mcp_changes)))
					report.count("mcp_pending", len(mcp_changes))
					report.warn(str(exc))
		elif not dry_run:
			self.state.set("pending_mcp", [])

		if not dry_run:
			self.state.set("parked", list(parked.values()))
			self.state.set("last_pull", now())
			self.state.set("unmanaged_links", sorted(self._links)[:50])
		report.parked = list(parked.values())
		if parked:
			report.count("parked", sum(p.get("count", 1) for p in parked.values()))

	def _set_fallbacks(self, profiles: Dict[str, Dict[str, Any]]) -> None:
		"""Pour un projet inconnu ici, on suppose qu'il est rangé au même endroit, relativement au
		dossier personnel, que sur la machine d'origine (utilisé pour le texte des sessions)."""
		fallback: Dict[str, str] = {}
		for prof in profiles.values():
			rhome = prof.get("home")
			for p in prof.get("projects", []) or []:
				name = "P:" + p.get("key", "")
				if name in self.mapper.name_to_path or name in fallback or not p.get("path"):
					continue
				rpath = p["path"]
				if rhome and path_inside(rpath, rhome):
					fallback[name] = self.paths.home + rpath[len(rhome.rstrip("/")):]
				else:
					fallback[name] = rpath
		self.mapper.fallback = fallback
		# Un projet que l'autre machine désigne encore par sa place dans son dossier personnel
		# (vu avant d'avoir un dépôt distant) est reconnu grâce à son profil.
		self.mapper.set_aliases([])
		aliases: List[Tuple[str, str]] = []
		home_token = make_token("HOME")
		for prof in profiles.values():
			rhome = (prof.get("home") or "").rstrip("/")
			for p in prof.get("projects", []) or []:
				local = self.mapper.name_to_path.get("P:" + str(p.get("key", "")))
				rpath = (p.get("path") or "").rstrip("/")
				if not local or not rhome or not rpath.startswith(rhome + "/") or not p.get("include", True):
					continue
				alias = home_token + rpath[len(rhome):]
				if self.mapper.detokenize_str(alias, use_fallback=False) != local:
					aliases.append((alias, local))
		self.mapper.set_aliases(aliases)

	def _auto_resolve_conflicts(self) -> None:
		"""Un conflit dont vous avez supprimé la copie est considéré comme résolu."""
		gone = [c["id"] for c in self.state.conflicts if c.get("copy") and not os.path.exists(c["copy"]) and c.get("id")]
		if gone:
			self.state.resolve_conflicts(gone)

	# -- sessions et fichiers de projet --------------------------------------

	def _active_sessions(self, current: Optional[str]) -> Set[str]:
		active: Set[str] = set()
		if current:
			active.add(current)
		sdir = self.paths.claude_sessions_dir
		try:
			names = os.listdir(sdir)[:200]
		except OSError:
			names = []
		for name in names:
			stem = name.split(".", 1)[0]
			if stem.isdigit() and not _pid_alive(int(stem)):
				continue   # marqueur laissé par un Claude Code arrêté brutalement
			active.update(UUID_RE.findall(name))
			full = os.path.join(sdir, name)
			data = read_bytes(full) if os.path.isfile(full) else None
			if data:
				active.update(UUID_RE.findall(data[:65536].decode("utf-8", "replace")))
		return active

	def _local_dir_for_cwd(self, cwd: str) -> str:
		for d, c in self.profile.get("dir_map", {}).items():
			if c == cwd:
				return d
		return slug(cwd)

	def _cwd_from_content(self, chunks: List[str]) -> Optional[str]:
		"""Dossier de travail indiqué dans les lignes de la session, traduit pour cette machine."""
		key = self.adapter.get("session_cwd_key", "cwd")
		try:
			lines = parse_chunk(self._get(chunks[0]))
		except CSyncError:
			return None
		for line in lines[:80]:
			try:
				obj = json.loads(line)
			except ValueError:
				continue
			value = get_path(obj, key) if isinstance(obj, dict) else None
			if isinstance(value, str):
				local = self.mapper.detokenize_path(value)
				if local and os.path.isdir(local):
					return local
		return None

	def _safe_rel(self, rel: str) -> bool:
		parts = rel.split("/")
		return bool(rel) and not rel.startswith("/") and ".." not in parts and "" not in parts and "." not in parts

	def _pull_project_entry(self, key, rentry, items, active, mname, machine, profiles, parked, dry_run):
		_, tokcwd, rel = key.split("|", 2)
		if not self._safe_rel(rel):
			self.report.count("skipped_unsafe")
			self.report.warn("Chemin refusé (hors de ~/.claude/projects) : %s" % rel)
			return
		cwd = self.mapper.detokenize_path(tokcwd)
		if cwd is not None and not os.path.isdir(cwd) and rentry.get("k") == "jsonl" and rentry.get("chunks"):
			# Le nom de dossier choisi par l'autre machine ne correspond à rien ici : on se fie au
			# dossier de travail écrit dans la session elle-même.
			cwd = self._cwd_from_content(rentry["chunks"]) or cwd
		project_key = None
		if cwd is None:
			missing = self.mapper.unknown_tokens(tokcwd)
			pkey = missing[0] if missing else tokcwd
			project_key = pkey[2:] if pkey.startswith("P:") else pkey
			if any(p.get("key") == project_key and not p.get("include", True)
				   for p in self.profile.get("projects", [])):
				return   # projet exclu sur cette machine : ni envoyé, ni reçu
			if project_key.startswith("dir:"):
				# Dossier sans git : s'il existe au même endroit (relativement au dossier personnel)
				# que sur l'autre machine, on l'utilise.
				guess = self.mapper.guess_path(tokcwd)
				if guess and os.path.isdir(guess):
					cwd = guess
			if cwd is None and rentry.get("k") == "jsonl" and rentry.get("chunks"):
				cwd = self._cwd_from_content(rentry["chunks"])
			if cwd is None:
				hint = None
				for p in profiles.get(machine, {}).get("projects", []) or []:
					if p.get("key") == project_key:
						hint = p.get("path")
				slot = parked.setdefault(pkey, {"project": project_key, "machine": mname, "remote_path": hint,
												"count": 0, "kind": "dir" if project_key.startswith("dir:") else "git"})
				if rentry.get("k") == "jsonl" and "/" not in rel:
					slot["count"] += 1
				return
		if self._is_excluded(cwd):
			return   # projet exclu sur cette machine
		root = self.projects_root
		target = os.path.join(root, self._local_dir_for_cwd(cwd), rel)
		if not path_inside(target, root) or symlink_on_path(target, root):
			self.report.count("skipped_unsafe")
			self._warn_once("Conversations non reçues : un lien symbolique se trouve dans %s." % os.path.dirname(target))
			return
		session_id = rel.split("/", 1)[0]
		if session_id.endswith(".jsonl"):
			session_id = session_id[:-len(".jsonl")]
		if rentry.get("k") == "jsonl":
			self._pull_session(key, rentry, items, target, session_id, active, mname, machine, dry_run)
		else:
			self._pull_plain_file(key, rentry, items, target, mname, machine, False, dry_run,
								  allow_delete=True, conflict_dir=os.path.join(self.paths.csync_dir, "conflicts"),
								  root=root)

	def _is_active(self, path: str, session_id: str, active: Set[str]) -> bool:
		if session_id in active:
			return True
		grace = float(self.config.get("active_session_grace_seconds", 120))
		try:
			return now() - os.path.getmtime(path) < grace
		except OSError:
			return False

	def _pull_session(self, key, rentry, items, target, session_id, active, mname, machine, dry_run):
		report = self.report
		if rentry.get("deleted"):
			self._apply_session_deletion(key, rentry, items, target, session_id, active, dry_run)
			return
		rchunks: List[str] = list(rentry.get("chunks") or [])
		if not rchunks:
			return
		st = self.state.entry(key)
		item = items.get(key)
		if item is None:
			if os.path.exists(target):
				item = LocalItem(key, "jsonl", target)
			elif st.get("deleted_vh") is not None:
				if version_hash(rchunks) in st["deleted_vh"] or not set(rentry.get("vh", [])) - set(st["deleted_vh"]):
					return   # version supprimée ici : elle ne revient pas
				st.pop("deleted_vh", None)   # l'autre machine l'a poursuivie depuis : elle est reçue
			elif st.get("chunks") and st.get("path"):
				# Disparue ici depuis peu : sa suppression sera envoyée au passage suivant.
				return
		if item is None:
			if not dry_run:
				self._install_session(key, rchunks, target, rentry.get("vh", []), expected_stat=None)
			report.count("sessions_new")
			return
		lchunks = self.local_jsonl_chunks(item) or []
		if lchunks == rchunks:
			return
		expected = st.get("stat")
		relation = self._relation(lchunks, rchunks)
		if relation in ("equal", "local_ahead"):
			return
		if relation == "remote_ahead" and version_hash(rchunks) in st.get("vh", []):
			return  # la version distante est une de nos anciennes versions (session raccourcie ici)
		if relation == "diverged":
			if version_hash(lchunks) in rentry.get("vh", []):
				relation = "remote_rewrite"   # l'autre machine a réécrit une version que nous avions
			elif version_hash(rchunks) in st.get("vh", []):
				return                        # la version distante est une de nos anciennes versions
			else:
				# Les tranches diffèrent, mais peut-être pas le contenu (état local perdu,
				# réinstallation, chemins écrits autrement) : on compare le texte lui-même.
				relation = self._byte_relation(key, st, item, rchunks, rentry, dry_run)
				if relation in ("equal", "local_ahead"):
					return
		if relation in ("remote_ahead", "remote_rewrite"):
			if self._is_active(item.path, session_id, active):
				# Conversation ouverte dans Claude Code (y compris celle que vous reprenez : Claude Code
				# l'a déjà chargée quand le hook s'exécute). On n'y touche pas ; si c'est celle que vous
				# reprenez, un message vous propose de la rouvrir à jour (« csync resume »).
				if self._current_session and session_id == self._current_session:
					report.stale_current.append(mname)
				report.count("sessions_active_skipped")
				return
			if not dry_run:
				if not self._install_session(key, rchunks, item.path, rentry.get("vh", []), expected_stat=expected):
					report.count("sessions_active_skipped")
					return
			report.count("sessions_extended" if relation == "remote_ahead" else "sessions_rewritten")
			return
		self._session_conflict(key, st, rchunks, item, mname, machine, dry_run)

	def _apply_session_deletion(self, key, rentry, items, target, session_id, active, dry_run) -> None:
		"""Conversation supprimée sur l'autre machine : supprimée ici aussi, sauf si elle est ouverte
		(elle le sera au passage suivant) ou si elle a été poursuivie ici entre-temps (elle est gardée,
		et renvoyée à l'autre machine)."""
		st = self.state.entry(key)
		item = items.get(key)
		path = item.path if item is not None else (target if os.path.exists(target) else None)
		deleted_vh = list(rentry.get("vh") or [])
		if path is None:
			if not dry_run and st.get("deleted_vh") is None:
				st["deleted_vh"] = deleted_vh
				self._forget_session_traces(key, st)
			return
		if self._is_active(path, session_id, active):
			self.report.count("sessions_active_skipped")
			return
		lchunks = self.local_jsonl_chunks(item or LocalItem(key, "jsonl", path)) or []
		current = version_hash(lchunks)
		if current not in deleted_vh and current != rentry.get("chunks_hash"):
			return   # poursuivie ici depuis : la modification l'emporte
		if not dry_run:
			remove_file(path)
			prune_empty_dirs(os.path.dirname(path), self.projects_root)
			st["deleted_vh"] = list(dict.fromkeys(deleted_vh + list(st.get("vh", []))))[-60:]
			st["path"] = path
			self._forget_session_traces(key, st)
			self.state.set("compact_due", now())
		self.report.count("sessions_deleted")

	def _byte_relation(self, key, st, item, rchunks, rentry, dry_run) -> str:
		data = read_bytes(item.path)
		if data is None:
			return "diverged"
		local, _ = split_complete_lines(data)
		try:
			remote = self._session_bytes(rchunks)
		except CSyncError:
			return "diverged"
		if local == remote or local.startswith(remote):
			if not dry_run:
				# Même contenu (ou contenu prolongé ici) : on adopte la forme neutre de l'autre
				# machine pour la partie commune, sans rien écrire.
				st.update({"kind": "jsonl", "path": item.path, "chunks": list(rchunks), "local_len": len(remote),
						   "local_hash": sha256_hex(remote)})
				st.pop("stat", None)
				push_vh(st, list(rchunks), rentry.get("vh", []))
				self.local_jsonl_chunks(item)
			return "equal" if local == remote else "local_ahead"
		if remote.startswith(local):
			return "remote_ahead"
		return "diverged"

	def _session_conflict(self, key, st, rchunks, item, mname, machine, dry_run) -> None:
		"""Divergence réelle : la session locale est gardée ; la version distante devient une
		conversation à part (créée une fois, puis complétée tant que vous n'y avez pas touché)."""
		report = self.report
		quiet = False
		ack = st.get("conflict_ack", {}).get(machine)
		if ack and self._covers(ack, rchunks):
			if self._covers(ack, rchunks, exact=True):
				return  # divergence déjà examinée et classée par vous
			# L'autre machine a continué sa version depuis : sa suite reste consultable dans une
			# conversation à part, sans nouveau conflit à traiter.
			quiet = True
		seen = st.setdefault("conflict_seen", {}).get(machine)
		if isinstance(seen, dict) and seen.get("chunks") == rchunks:
			return
		copies = self.state.data.setdefault("conflict_copies", {})
		previous_copy = None
		if isinstance(seen, dict) and seen.get("copy") and os.path.exists(seen["copy"]):
			data = read_bytes(seen["copy"])
			untouched = data is not None and sha256_hex(data) == seen.get("hash")
			if untouched and self._covers(seen, rchunks):
				previous_copy = seen["copy"]
		if dry_run:
			if not previous_copy and not quiet:
				report.count("sessions_conflict")
			return
		copy_path, data_hash = self._write_conflict_session(rchunks, item.path, mname, previous_copy)
		copies[copy_path] = data_hash
		sig_len, sig = self._content_sig(rchunks)
		st["conflict_seen"][machine] = {"chunks": rchunks, "copy": copy_path, "hash": data_hash,
										"len": sig_len, "sig": sig}
		if quiet:
			return
		self.state.add_conflict({"key": key, "kind": "session", "path": item.path, "copy": copy_path,
								 "machine": mname, "machine_id": machine, "remote_version": version_hash(rchunks),
								 "remote_len": sig_len, "remote_sig": sig, "ts": now()})
		if not previous_copy:
			report.count("sessions_conflict")

	def _content_sig(self, chunks: List[str]) -> Tuple[int, str]:
		"""Longueur et empreinte du contenu (forme neutre) d'une version de session.

		Elles survivent au compactage du dépôt, contrairement aux identifiants de tranches."""
		data = b"".join(self._get(o) for o in chunks)
		return len(data), sha256_hex(data)

	def _covers(self, mark: Any, rchunks: List[str], exact: bool = False) -> bool:
		"""La version marquée (classée ou déjà copiée) est-elle un début de la version distante ?
		(exact=True : est-elle la version distante elle-même ?)"""
		try:
			if isinstance(mark, dict) and mark.get("sig"):
				need = int(mark.get("len") or 0)
				acc = b""
				for o in rchunks:
					if len(acc) >= need and not exact:
						break
					acc += self._get(o)
				if exact and len(acc) != need:
					return False
				return len(acc) >= need and sha256_hex(acc[:need]) == mark["sig"]
			chunks = mark.get("chunks") if isinstance(mark, dict) else mark
			if exact:
				return bool(chunks) and list(chunks) == list(rchunks)
			return bool(chunks) and self._is_prefix(list(chunks), rchunks)
		except (CSyncError, ValueError, TypeError):
			return False

	def _relation(self, local: List[str], remote: List[str]) -> str:
		if local == remote:
			return "equal"
		if self._is_prefix(local, remote):
			return "remote_ahead"
		if self._is_prefix(remote, local):
			return "local_ahead"
		return "diverged"

	def _is_prefix(self, short: List[str], long: List[str]) -> bool:
		"""Le contenu représenté par short est-il un début du contenu représenté par long ?"""
		if not short:
			return True
		if len(short) > len(long):
			return False
		k = len(short) - 1
		if short[:k] != long[:k]:
			return False
		if short[k] == long[k]:
			return True
		a = parse_chunk(self._get(short[k]))
		b = parse_chunk(self._get(long[k]))
		return len(a) <= len(b) and b[:len(a)] == a

	def _session_bytes(self, chunks: List[str]) -> bytes:
		lines: List[str] = []
		for oid in chunks:
			lines.extend(parse_chunk(self._get(oid)))
		lines = [self.mapper.detokenize_line(l) for l in lines]
		return ("\n".join(lines) + "\n").encode("utf-8", ENCODING) if lines else b""

	def _install_session(self, key: str, chunks: List[str], path: str, remote_vh: Iterable[str],
						 expected_stat: Optional[List[int]], append_only: bool = False) -> bool:
		data = self._session_bytes(chunks)
		existing = read_bytes(path) if os.path.exists(path) else None
		if expected_stat is not None:
			# Dernier contrôle : si la session a bougé depuis notre lecture, on n'y touche pas.
			current = self._stat(path)
			if current is None or list(current) != list(expected_stat):
				return False
		if existing is not None and len(existing) < len(data) and data.startswith(existing):
			# Simple prolongement : on ajoute la suite à la fin du fichier, sans le remplacer.
			# Rien n'est perdu, aucune sauvegarde n'est donc nécessaire.
			append_bytes(path, data[len(existing):])
		elif existing is not None and existing == data:
			pass
		elif append_only:
			return False
		else:
			self._backup(path)
			atomic_write_bytes(path, data, 0o600)
		st = self.state.entry(key)
		stat = self._stat(path)
		push_vh(st, list(chunks), remote_vh)
		fields = {"kind": "jsonl", "path": path, "chunks": list(chunks), "local_len": len(data),
				  "local_hash": sha256_hex(data), "stat": list(stat) if stat else None, "vh": list(st["vh"])}
		self.state.journal(key, fields)
		st.update(fields)
		st.pop("gone", None)
		return True

	def _write_conflict_session(self, chunks: List[str], local_path: str, mname: str,
								existing_copy: Optional[str] = None) -> Tuple[str, str]:
		"""Écrit la version distante d'une session divergente comme une conversation à part."""
		raw = self._session_bytes(chunks)
		lines = raw.decode("utf-8", ENCODING).split("\n")[:-1] if raw else []
		directory = os.path.dirname(local_path)
		base = os.path.basename(local_path)
		old_id = base[:-len(".jsonl")]
		top_level = os.path.basename(directory) != "subagents" and UUID_RE.fullmatch(old_id or "")
		key_name = self.adapter.get("session_id_key")
		if top_level and key_name:
			new_id = os.path.basename(existing_copy)[:-len(".jsonl")] if existing_copy else str(uuid.uuid4())
			out = []
			for line in lines:
				try:
					obj = json.loads(line)
				except ValueError:
					out.append(line)
					continue
				if isinstance(obj, dict) and get_path(obj, key_name) == old_id:
					set_path(obj, key_name, new_id)
					out.append(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
				else:
					out.append(line)
			path = os.path.join(directory, new_id + ".jsonl")
			lines = out
		else:
			safe = re.sub(r"[^A-Za-z0-9_-]", "_", mname)
			path = existing_copy or os.path.join(directory, old_id + ".csync-conflict-" + safe + ".jsonl")
		data = ("\n".join(lines) + "\n").encode("utf-8", ENCODING) if lines else b""
		self._backup(path)
		atomic_write_bytes(path, data, 0o600)
		return path, sha256_hex(data)

	# -- fichiers de configuration ---------------------------------------------

	def _pull_config_entry(self, key, rentry, items, mname, machine, prefer_remote, dry_run):
		rel = key[2:]
		if not self._config_rel_allowed(rel):
			self.report.count("skipped_unsafe")
			self.report.warn("Chemin de configuration refusé : %s" % rel)
			return
		target = os.path.join(self.paths.claude_dir, rel)
		self._pull_plain_file(key, rentry, items, target, mname, machine, prefer_remote, dry_run,
							  allow_delete=True, conflict_dir=None)

	def _decide(self, key: str, rentry: Dict[str, Any], lv: Optional[str], machine: str,
				prefer_remote: bool) -> str:
		"""Décision de fusion : « skip », « apply », « adopt » (même contenu) ou « conflict »."""
		rv = rentry.get("v")
		if not rv or rv == lv:
			return "skip"
		if lv and lv.startswith("large:"):
			return "skip"
		st = self.state.entries.get(key, {})
		local_hist = st.get("hist", [])
		remote_hist = rentry.get("hist", [])
		remote_oid = rentry.get("oid")
		invalid = bool(lv) and lv.startswith("invalid:")
		local_oid = None if lv is None or invalid else event_content(lv)
		if lv is not None and not invalid and remote_oid == local_oid:
			return "adopt"        # même contenu des deux côtés : on aligne seulement les historiques
		if lv is None:
			if remote_oid is None:
				return "skip"     # absent ici, supprimé là-bas
			if rv in local_hist:
				return "skip"     # déjà eu ici puis supprimé localement après
			return "apply"
		if lv in remote_hist:
			return "apply"        # la version distante descend de la nôtre
		if rv in local_hist:
			return "skip"         # la nôtre descend de la version distante
		if local_oid is None and not invalid:
			return "apply"        # supprimé ici, modifié là-bas : la modification l'emporte
		if remote_oid is None:
			return "skip"         # supprimé là-bas, modifié ici : on garde (et on renverra)
		if prefer_remote and not invalid:
			return "apply"
		if st.get("conflict_seen", {}).get(machine) == rv:
			return "skip"
		return "conflict"

	def _pull_plain_file(self, key, rentry, items, target, mname, machine, prefer_remote, dry_run,
						 allow_delete: bool, conflict_dir: Optional[str], root: Optional[str] = None):
		report = self.report
		root = root or self.paths.claude_dir
		if os.path.islink(target) or symlink_on_path(target, root) or not path_inside(target, root):
			report.count("skipped_unsafe")
			return
		item = items.get(key)
		if item is None and os.path.isfile(target):
			item = LocalItem(key, "file", target, rentry.get("tok", "none"))
		if item is not None:
			lv = self.local_file_version(item)
		else:
			lv = self.state.entries.get(key, {}).get("v")
			if lv and event_content(lv):
				lv = None  # l'état dit « présent » mais le fichier n'existe plus : traité comme absent
		decision = self._decide(key, rentry, lv, machine, prefer_remote)
		if decision == "skip":
			return
		rv = rentry["v"]
		st = self.state.entry(key)
		if decision == "adopt":
			if not dry_run:
				# Même contenu ; seul le droit d'exécution a pu changer sur l'autre machine.
				x_remote = bool(rentry.get("x"))
				# Le droit d'exécution n'est jamais retiré par la synchronisation : seulement ajouté.
				if (os.path.isfile(target) and x_remote and not _is_executable(target)
						and lv in rentry.get("hist", [])):
					os.chmod(target, self._target_mode(target, x_remote))
					st["x"] = x_remote
					st["stat"] = list(self._stat(target) or [])
					report.count("files_applied")
				head = max(rv, lv or rv)
				st["hist"] = merge_hist(st.get("hist", []), rentry.get("hist", []), head)
				st["v"] = head
			return
		if decision == "apply":
			if rentry.get("oid") is None:
				if not allow_delete:
					return
				existed = os.path.exists(target)
				if not dry_run:
					if existed:
						had_hooks = key == "c|settings.json" and settings_has_our_hooks(target)
						kept: Dict[str, Any] = {}
						if key == "c|settings.json":
							local = read_json_safe(target, None)
							if isinstance(local, dict):
								kept = {k: local[k] for k in self._local_settings_keys() if k in local}
						if key.startswith("p|"):
							self._purge_backups_of(target)   # contenu de conversation : aucune copie gardée
						else:
							self._backup(target)
						if kept:
							# Les réglages propres à cette machine restent en place.
							atomic_write_bytes(target, (json.dumps(kept, indent=2, ensure_ascii=False) + "\n")
											   .encode("utf-8"), self._target_mode(target, False))
						else:
							remove_file(target)
							prune_empty_dirs(os.path.dirname(target), root)
						if had_hooks:
							install_hooks(self.paths)   # les hooks de CSync restent en place ici
					st.update({"v": rv, "oid": None, "kind": "file", "path": target,
							   "hist": merge_hist(st.get("hist", []), rentry.get("hist", []), rv)})
					self.state.set("compact_due", now())
					for field in ("stat", "local_hash", "missing_since"):
						st.pop(field, None)
				if existed:
					report.count("files_deleted")
				return
			if not dry_run:
				data = self._materialize(rentry["oid"], rentry.get("tok", "none"), key=key,
										 merge_path=target if key == "c|settings.json" else None)
				had_hooks = key == "c|settings.json" and settings_has_our_hooks(target)
				self._backup(target)
				mode = self._target_mode(target, bool(rentry.get("x")))
				atomic_write_bytes(target, data, mode)
				if had_hooks:
					install_hooks(self.paths)   # nos hooks restent en place sur cette machine
				written = read_bytes(target) or b""
				stat = self._stat(target)
				st.update({"kind": "file", "path": target, "v": rv, "oid": rentry["oid"],
						   "local_hash": sha256_hex(written), "stat": list(stat) if stat else None,
						   "tok": rentry.get("tok", "none"), "x": bool(rentry.get("x")),
						   "hist": merge_hist(st.get("hist", []), rentry.get("hist", []), rv)})
				st.pop("missing_since", None)
			report.count("files_applied")
			return
		# Conflit : la version locale est gardée, la version distante est enregistrée à côté.
		if rentry.get("oid") is None:
			return
		if not dry_run:
			safe = re.sub(r"[^A-Za-z0-9_-]", "_", mname)
			if conflict_dir:
				ensure_dir(conflict_dir, 0o700)
				copy = os.path.join(conflict_dir, re.sub(r"[^A-Za-z0-9_.-]", "_", key[2:]) + ".csync-conflict-" + safe)
			else:
				copy = target + ".csync-conflict-" + safe
			self._backup(copy)
			atomic_write_bytes(copy, self._materialize(rentry["oid"], rentry.get("tok", "none"), key=key), 0o600)
			st.setdefault("conflict_seen", {})[machine] = rv
			self.state.add_conflict({"key": key, "kind": "file", "path": target, "copy": copy, "machine": mname,
									 "machine_id": machine, "remote_version": rv, "ts": now()})
		report.count("files_conflict")

	def _target_mode(self, target: str, executable: bool) -> int:
		"""Permissions à donner au fichier écrit : celles du fichier actuel, plus le droit
		d'exécution si l'autre machine l'a. Un droit d'exécution présent ici n'est jamais retiré."""
		try:
			mode = os.stat(target).st_mode & 0o777
		except OSError:
			return 0o755 if executable else 0o644
		return (mode | 0o111) if executable else mode

	def _materialize(self, oid: str, tok: str, key: Optional[str] = None, merge_path: Optional[str] = None) -> bytes:
		plain = self.store.get_object(oid)
		if tok == "json":
			obj = self.mapper.detokenize_json(json.loads(plain.decode("utf-8")))
			if key == "c|settings.json" and isinstance(obj, dict):
				sender_keys = obj.pop(LOCAL_KEYS_MARKER, None) or []
				keep = set(k for k in sender_keys if isinstance(k, str)) | set(self._local_settings_keys())
				if merge_path and keep:
					# Les clés propres à une machine (la nôtre ou celle d'origine) gardent ici leur valeur locale.
					local = read_json_safe(merge_path, None)
					local = local if isinstance(local, dict) else {}
					for k in keep:
						if k in local:
							obj[k] = local[k]
						else:
							obj.pop(k, None)
			return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
		return plain

	# -- serveurs MCP -----------------------------------------------------------

	def _pull_mcp_entry(self, key, rentry, items, mname, machine, changes, prefer_remote, dry_run):
		item = items.get(key)
		if item is not None:
			lv = self.local_file_version(item)
		else:
			lv = self.state.entries.get(key, {}).get("v")
			if lv and event_content(lv):
				lv = None
		decision = self._decide(key, rentry, lv, machine, prefer_remote)
		name = key[2:]
		rv = rentry["v"]
		st = self.state.entry(key)
		if decision == "adopt":
			if not dry_run:
				head = max(rv, lv or rv)
				st["hist"] = merge_hist(st.get("hist", []), rentry.get("hist", []), head)
				st["v"] = head
			return
		if decision == "apply":
			if rentry.get("oid") is None:
				conf = None
			else:
				conf = self.mapper.detokenize_json(json.loads(self.store.get_object(rentry["oid"]).decode("utf-8")))
			# L'état n'est mis à jour qu'au moment où le changement est réellement écrit.
			changes[name] = (conf, rentry)
		elif decision == "conflict" and not dry_run and rentry.get("oid"):
			conflict_dir = ensure_dir(os.path.join(self.paths.csync_dir, "conflicts"), 0o700)
			safe = re.sub(r"[^A-Za-z0-9_-]", "_", mname)
			copy = os.path.join(conflict_dir, "mcp-%s-%s.json" % (re.sub(r"[^A-Za-z0-9_-]", "_", name), safe))
			conf = self.mapper.detokenize_json(json.loads(self.store.get_object(rentry["oid"]).decode("utf-8")))
			atomic_write_bytes(copy, (json.dumps({name: conf}, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
							   0o600)
			st.setdefault("conflict_seen", {})[machine] = rv
			self.state.add_conflict({"key": key, "kind": "mcp", "path": self.paths.claude_json, "copy": copy,
									 "machine": mname, "machine_id": machine, "remote_version": rv, "ts": now()})
			self.report.count("files_conflict")

	def _apply_mcp(self, changes: Dict[str, Tuple[Optional[Dict[str, Any]], Dict[str, Any]]]) -> None:
		path = os.path.realpath(self.paths.claude_json)   # un lien symbolique reste un lien
		for _ in range(2):
			before = self._stat(path)
			raw = read_bytes(path)
			if raw is None:
				raise CSyncError("%s est introuvable : serveurs MCP non appliqués." % path)
			try:
				data = json.loads(raw.decode("utf-8"))
			except (ValueError, UnicodeDecodeError):
				raise CSyncError("%s est illisible : serveurs MCP non appliqués." % path)
			if not isinstance(data, dict):
				raise CSyncError("%s n'a pas la structure attendue : serveurs MCP non appliqués." % path)
			parts = self.adapter["claude_json_mcp_key"].split(".")
			container = data
			for part in parts[:-1]:
				nxt = container.get(part)
				if not isinstance(nxt, dict):
					nxt = {}
					container[part] = nxt
				container = nxt
			servers = container.get(parts[-1])
			if not isinstance(servers, dict):
				servers = {}
				container[parts[-1]] = servers
			for name, (conf, _) in changes.items():
				if conf is None:
					servers.pop(name, None)
				else:
					servers[name] = conf
			payload = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
			# Dernières vérifications juste avant d'écrire : Claude Code fermé, fichier inchangé.
			if claude_running():
				raise CSyncError("Claude Code vient de s'ouvrir : serveurs MCP appliqués plus tard.")
			if self._stat(path) != before:
				continue
			self._backup(path)
			try:
				mode = os.stat(path).st_mode & 0o777
			except OSError:
				mode = 0o600
			atomic_write_bytes(path, payload, mode)
			break
		else:
			raise CSyncError("%s est modifié en continu : serveurs MCP appliqués plus tard." % path)
		self._cj_loaded = False
		for name, (conf, rentry) in changes.items():
			st = self.state.entry("m|" + name)
			st.update({"kind": "mcp", "v": rentry["v"], "oid": rentry.get("oid"), "path": self.paths.claude_json,
					   "hist": merge_hist(st.get("hist", []), rentry.get("hist", []), rentry["v"])})
			if conf is None:
				st.pop("local_hash", None)
			else:
				st["local_hash"] = sha256_hex(canonical_json(conf))

	# -- sauvegardes ------------------------------------------------------------

	def _backup(self, path: str) -> None:
		if not os.path.exists(path):
			return
		if self._backup_dir is None:
			stamp = time.strftime("%Y%m%d-%H%M%S")
			self._backup_dir = ensure_dir(os.path.join(self.paths.backups_dir,
													   "%s-%d-%s" % (stamp, os.getpid(), uuid.uuid4().hex[:6])), 0o700)
			self._prune_backups()
		home = self.paths.home
		rel = os.path.relpath(path, home) if path_inside(path, home) else path.lstrip("/")
		dest = os.path.join(self._backup_dir, rel)
		ensure_dir(os.path.dirname(dest), 0o700)
		if os.path.exists(dest):
			return  # la version d'origine de ce fichier est déjà sauvegardée pour cette opération
		shutil.copy2(path, dest)

	def _prune_backups(self) -> None:
		"""Garde les sauvegardes des 30 derniers jours, dans la limite de 2 Go (les plus anciennes
		partent d'abord ; la sauvegarde « initial- » de la configuration est toujours gardée)."""
		days = float(self.config.get("backups_days", 30))
		max_bytes = float(self.config.get("backups_max_mb", 2048)) * 1024 * 1024
		limit = now() - days * 86400
		try:
			names = os.listdir(self.paths.backups_dir)
		except FileNotFoundError:
			return
		dated = []
		for name in names:
			if not name[:1].isdigit():
				continue
			full = os.path.join(self.paths.backups_dir, name)
			if full == self._backup_dir:
				continue
			try:
				mtime = os.path.getmtime(full)
			except OSError:
				continue
			if mtime < limit:
				shutil.rmtree(full, ignore_errors=True)
			else:
				dated.append((mtime, full))
		sizes = []
		total = 0
		for mtime, full in sorted(dated):
			size = 0
			for dirpath, _, files in os.walk(full):
				for f in files:
					try:
						size += os.path.getsize(os.path.join(dirpath, f))
					except OSError:
						pass
			sizes.append((full, size))
			total += size
		for full, size in sizes:
			if total <= max_bytes:
				break
			shutil.rmtree(full, ignore_errors=True)
			total -= size

	# ------------------------------------------------------------------
	# Synchronisation complète et compactage
	# ------------------------------------------------------------------

	def sync(self, dry_run: bool = False, current_session: Optional[str] = None,
			 prefer_remote: bool = False, allow_deletions: bool = False) -> List[Report]:
		pulled = self.pull(dry_run=dry_run, current_session=current_session, prefer_remote=prefer_remote)
		pushed = self.push(dry_run=dry_run, allow_deletions=allow_deletions)
		return [pulled, pushed]

	def compact(self) -> Tuple[int, float, float]:
		"""Envoie d'abord nos changements, puis réduit le dépôt aux seuls blocs encore utilisés."""
		self.push()
		referenced: Set[str] = set()
		docs = dict(self.store.other_manifests())
		own = self.store.read_manifest(self.config.machine_id)
		if own:
			docs[self.config.machine_id] = own
		for _, (_, entries) in docs.items():
			for entry in entries.values():
				referenced.update(self._entry_objects(entry))
		for st in self.state.entries.values():
			referenced.update(st.get("chunks") or [])
			if st.get("oid"):
				referenced.add(st["oid"])
			for mark in list((st.get("conflict_ack") or {}).values()) + list((st.get("conflict_seen") or {}).values()):
				chunks = mark.get("chunks") if isinstance(mark, dict) else mark
				if isinstance(chunks, list):
					referenced.update(c for c in chunks if isinstance(c, str))
		return self.store.compact(referenced)
