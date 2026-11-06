"""Fonctions utilitaires partagées : écriture atomique, JSON, journal, processus."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, Optional, Sequence

LOG = logging.getLogger("csync")


class CSyncError(Exception):
	"""Erreur attendue, dont le message est destiné à l'utilisateur (en français)."""


# ---------------------------------------------------------------------------
# Temps
# ---------------------------------------------------------------------------

def now() -> float:
	return time.time()


def iso(ts: Optional[float] = None) -> str:
	if ts is None:
		ts = now()
	return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def human_time(ts: Optional[float]) -> str:
	if not ts:
		return "jamais"
	return time.strftime("%d/%m/%Y %H:%M", time.localtime(ts))


# ---------------------------------------------------------------------------
# Fichiers
# ---------------------------------------------------------------------------

def ensure_dir(path: str, mode: int = 0o755) -> str:
	os.makedirs(path, mode=mode, exist_ok=True)
	return path


def atomic_write_bytes(path: str, data: bytes, mode: Optional[int] = None) -> None:
	"""Écrit un fichier de façon atomique : un lecteur voit l'ancien ou le nouveau contenu, jamais un mélange."""
	directory = os.path.dirname(path) or "."
	ensure_dir(directory)
	if mode is None:
		try:
			mode = os.stat(path).st_mode & 0o7777
		except FileNotFoundError:
			mode = 0o644
	fd, tmp = tempfile.mkstemp(prefix=".csync-tmp-", dir=directory)
	try:
		with os.fdopen(fd, "wb") as fh:
			fh.write(data)
			fh.flush()
			os.fsync(fh.fileno())
		os.chmod(tmp, mode)
		os.replace(tmp, path)
	except BaseException:
		try:
			os.unlink(tmp)
		except OSError:
			pass
		raise


def atomic_write_text(path: str, text: str, mode: Optional[int] = None) -> None:
	atomic_write_bytes(path, text.encode("utf-8"), mode)


def read_bytes(path: str) -> Optional[bytes]:
	try:
		with open(path, "rb") as fh:
			return fh.read()
	except FileNotFoundError:
		return None


def read_json(path: str, default: Any = None) -> Any:
	data = read_bytes(path)
	if data is None:
		return default
	try:
		return json.loads(data.decode("utf-8"))
	except (ValueError, UnicodeDecodeError) as exc:
		raise CSyncError("Le fichier %s n'est pas un JSON valide (%s)." % (path, exc))


def read_json_safe(path: str, default: Any = None) -> Any:
	"""Comme read_json, mais renvoie default si le fichier est absent ou illisible."""
	try:
		return read_json(path, default)
	except CSyncError as exc:
		LOG.warning("%s", exc)
		return default


def write_json(path: str, obj: Any, mode: Optional[int] = None) -> None:
	atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False) + "\n", mode)


def canonical_json(obj: Any) -> bytes:
	"""Sérialisation stable : deux contenus égaux donnent toujours les mêmes octets."""
	return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest()


def copy_preserving(src: str, dst: str) -> None:
	ensure_dir(os.path.dirname(dst))
	shutil.copy2(src, dst)


def is_executable(path: str) -> bool:
	try:
		return bool(os.stat(path).st_mode & 0o111)
	except OSError:
		return False


def path_inside(path: str, root: str) -> bool:
	"""Vrai si path est root ou se trouve dessous (après normalisation)."""
	path = os.path.normpath(path)
	root = os.path.normpath(root)
	return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def symlink_on_path(path: str, root: str) -> bool:
	"""Vrai si path, ou l'un de ses dossiers entre root (exclu) et lui, est un lien symbolique."""
	path = os.path.normpath(path)
	root = os.path.normpath(root)
	current = path
	while path_inside(current, root) and current != root:
		if os.path.islink(current):
			return True
		current = os.path.dirname(current)
	return False


def remove_file(path: str) -> bool:
	try:
		os.unlink(path)
		return True
	except FileNotFoundError:
		return False


def prune_empty_dirs(start: str, stop: str) -> None:
	"""Supprime les dossiers vides en remontant de start jusqu'à stop (exclu)."""
	start = os.path.abspath(start)
	stop = os.path.abspath(stop)
	current = start
	while current.startswith(stop + os.sep) and current != stop:
		try:
			os.rmdir(current)
		except OSError:
			return
		current = os.path.dirname(current)


# ---------------------------------------------------------------------------
# Verrou
# ---------------------------------------------------------------------------

class FileLock:
	"""Verrou exclusif entre processus CSync (un seul push ou pull à la fois)."""

	def __init__(self, path: str):
		self.path = path
		self._fh = None

	def acquire(self, timeout: float = 0.0) -> bool:
		import fcntl

		ensure_dir(os.path.dirname(self.path))
		self._fh = open(self.path, "a+")
		deadline = now() + timeout
		while True:
			try:
				fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
				self._fh.seek(0)
				self._fh.truncate()
				self._fh.write("%d\n" % os.getpid())
				self._fh.flush()
				return True
			except OSError as exc:
				if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
					raise
				if now() >= deadline:
					self._fh.close()
					self._fh = None
					return False
				time.sleep(0.5)

	def release(self) -> None:
		import fcntl

		if self._fh is not None:
			try:
				fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
			finally:
				self._fh.close()
				self._fh = None

	def __enter__(self):
		if not self.acquire(timeout=0):
			raise CSyncError("Une autre opération CSync est déjà en cours.")
		return self

	def __exit__(self, *exc):
		self.release()
		return False


# ---------------------------------------------------------------------------
# Commandes externes
# ---------------------------------------------------------------------------

class CommandResult:
	def __init__(self, code: int, out: str, err: str):
		self.code = code
		self.out = out
		self.err = err

	@property
	def ok(self) -> bool:
		return self.code == 0


# Variables git autorisées à passer. Toutes les autres (GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE…)
# sont retirées : git les exporte vers les hooks, et elles feraient agir CSync sur le dépôt de
# l'utilisateur au lieu du sien.
_GIT_ENV_ALLOWED = {
	"GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSL_CAINFO", "GIT_SSL_CAPATH", "GIT_SSL_NO_VERIFY",
	"GIT_TERMINAL_PROMPT", "GIT_ASKPASS", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
	"GIT_COMMITTER_EMAIL", "GIT_HTTP_PROXY", "GIT_PROXY_COMMAND",
}


def clean_env(extra: Optional[dict] = None) -> dict:
	"""Copie de l'environnement sans les variables git dangereuses."""
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in _GIT_ENV_ALLOWED}
	if extra:
		env.update(extra)
	return env


def _kill_group(proc: "subprocess.Popen") -> None:
	"""Arrête la commande et tous les processus qu'elle a lancés (ssh pour git, par exemple)."""
	import signal
	try:
		os.killpg(proc.pid, signal.SIGKILL)
	except OSError:
		pass
	try:
		proc.kill()
	except OSError:
		pass


def run(cmd: Sequence[str], cwd: Optional[str] = None, timeout: float = 60, env: Optional[dict] = None,
		input_text: Optional[str] = None) -> CommandResult:
	"""Lance une commande avec un délai maximal. Ne lève pas d'exception si elle échoue.

	La commande tourne dans son propre groupe de processus : en cas de dépassement du délai,
	elle est arrêtée avec tout ce qu'elle a lancé, et rien ne continue en arrière-plan."""
	full_env = clean_env(env)
	try:
		proc = subprocess.Popen(
			list(cmd), cwd=cwd, env=full_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
			stdin=subprocess.DEVNULL if input_text is None else subprocess.PIPE, start_new_session=True,
		)
	except FileNotFoundError:
		return CommandResult(127, "", "commande introuvable : %s" % cmd[0])
	except OSError as exc:
		return CommandResult(126, "", "lancement impossible de %s : %s" % (cmd[0], exc))
	try:
		out, err = proc.communicate(input=input_text.encode("utf-8") if input_text is not None else None,
									timeout=timeout)
	except subprocess.TimeoutExpired:
		_kill_group(proc)
		try:
			proc.communicate(timeout=5)
		except Exception:
			pass
		return CommandResult(124, "", "délai dépassé (%ds) : %s" % (int(timeout), " ".join(cmd[:3])))
	except BaseException:
		_kill_group(proc)
		raise
	return CommandResult(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


def which(name: str) -> Optional[str]:
	return shutil.which(name)


# Emplacements habituels des outils quand le PATH est minimal (application ouverte depuis le
# Finder, tâche launchd ou systemd).
_EXTRA_BIN = ("~/.local/bin", "~/.claude/local", "~/.npm-global/bin", "~/.bun/bin", "~/.volta/bin",
			  "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")


def tool_path() -> str:
	parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
	for extra in _EXTRA_BIN:
		extra = os.path.expanduser(extra)
		if extra not in parts:
			parts.append(extra)
	return os.pathsep.join(parts)


def find_claude() -> Optional[str]:
	"""Chemin de la commande « claude », y compris quand le PATH est minimal.

	CSYNC_CLAUDE contient l'emplacement relevé lors de la configuration, dans le terminal."""
	found = shutil.which("claude", path=tool_path())
	if found:
		return found
	recorded = os.environ.get("CSYNC_CLAUDE")
	if recorded and os.path.isfile(recorded) and os.access(recorded, os.X_OK):
		return recorded
	return None


def claude_env(extra: Optional[dict] = None) -> dict:
	"""Variables pour lancer Claude Code : PATH complété (node, par exemple), hooks de CSync neutralisés."""
	claude = find_claude()
	path = tool_path()
	if claude:
		path = os.path.dirname(claude) + os.pathsep + path
	env = {"PATH": path, "CSYNC_INTERNAL": "1"}
	env.update(extra or {})
	return env


def claude_running() -> bool:
	"""Indique si un processus Claude Code tourne sur la machine.

	Utilisé avant de modifier ~/.claude.json, que Claude Code réécrit lui-même.
	"""
	forced = os.environ.get("CSYNC_ASSUME_CLAUDE_RUNNING")
	if forced is not None:
		return forced == "1"
	result = run(["ps", "-axo", "pid=,args="], timeout=10)
	if not result.ok:
		result = run(["ps", "-eo", "pid=,args="], timeout=10)
		if not result.ok:
			# Dans le doute, on considère que Claude tourne : on reporte l'écriture.
			return True
	me = os.getpid()
	for line in result.out.splitlines():
		parts = line.strip().split(None, 1)
		if len(parts) != 2:
			continue
		pid, args = parts
		if pid == str(me) or "/bin/csync" in args or "csync.cli" in args or "-m csync" in args:
			continue
		if "@anthropic-ai/claude-code" in args:
			return True
		# Selon l'installation, Claude Code apparaît comme « claude » (binaire natif)
		# ou comme « node …/claude ». On regarde les deux premiers mots.
		for token in args.split()[:2]:
			if os.path.basename(token) == "claude":
				return True
	return False


def spawn_detached(args: Sequence[str], log_path: str, env: Optional[dict] = None) -> None:
	"""Lance un processus en arrière-plan, détaché du processus appelant."""
	ensure_dir(os.path.dirname(log_path))
	full_env = clean_env(env)
	with open(log_path, "ab") as log:
		subprocess.Popen(
			list(args), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
			start_new_session=True, close_fds=True, env=full_env,
		)


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

def setup_logging(log_path: str, verbose: bool = False, to_stderr: bool = True) -> None:
	ensure_dir(os.path.dirname(log_path))
	LOG.setLevel(logging.DEBUG)
	for handler in list(LOG.handlers):
		LOG.removeHandler(handler)
	file_handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
	file_handler.setFormatter(logging.Formatter("%(asctime)s [%(process)d] %(levelname)s %(message)s"))
	file_handler.setLevel(logging.DEBUG)
	LOG.addHandler(file_handler)
	if to_stderr:
		stream = logging.StreamHandler(sys.stderr)
		stream.setFormatter(logging.Formatter("%(message)s"))
		stream.setLevel(logging.DEBUG if verbose else logging.ERROR)
		LOG.addHandler(stream)


def chunked(items: Iterable[Any], size: int):
	batch = []
	for item in items:
		batch.append(item)
		if len(batch) >= size:
			yield batch
			batch = []
	if batch:
		yield batch
