"""Emplacements des fichiers de CSync et de Claude Code, et configuration de la machine."""

from __future__ import annotations

import os
import platform
import socket
import uuid
from typing import Any, Dict, Optional

from .util import CSyncError, ensure_dir, read_json, write_json

DEFAULT_SETTINGS: Dict[str, Any] = {
	"branch": "main",
	# Délai minimal entre deux envois déclenchés par le hook Stop (5 minutes).
	"push_debounce_seconds": 300,
	# Intervalle de la vérification périodique (launchd / systemd).
	"periodic_minutes": 15,
	# Sur batterie ou en mode Économie d'énergie, la vérification périodique ne fait rien.
	"skip_on_battery": True,
	# Une session modifiée il y a moins de ce délai est considérée comme active.
	"active_session_grace_seconds": 120,
	# Les fichiers plus gros sont ignorés (GitHub refuse les fichiers de plus de 100 Mo).
	"max_file_mb": 50,
	# Délai maximal pour chaque opération réseau git.
	"git_timeout": 120,
	# Récupérer les nouveautés à l'ouverture d'une session Claude Code.
	"pull_on_session_start": True,
	# Durée de conservation des sauvegardes, en jours, et taille maximale de l'ensemble.
	"backups_days": 30,
	"backups_max_mb": 2048,
	# Clés de settings.json et serveurs MCP propres à cette machine (jamais envoyés ni remplacés).
	"local_settings_keys": [],
	"local_mcp_servers": [],
	# Au-delà de cette taille (en Mo), le dépôt de données est compacté automatiquement
	# (au plus une fois par semaine, lors de la vérification périodique). 0 : jamais.
	"auto_compact_mb": 300,
}


class Paths:
	"""Tous les chemins utilisés par CSync sur cette machine."""

	def __init__(self, csync_dir: Optional[str] = None, home: Optional[str] = None,
				 claude_dir: Optional[str] = None, claude_json: Optional[str] = None):
		self.home = os.path.abspath(home or os.path.expanduser("~"))
		self.csync_dir = os.path.abspath(csync_dir or os.environ.get("CSYNC_DIR") or os.path.join(self.home, ".csync"))
		env_config = os.environ.get("CLAUDE_CONFIG_DIR")
		self.claude_dir = os.path.abspath(claude_dir or env_config or os.path.join(self.home, ".claude"))
		if claude_json:
			self.claude_json = os.path.abspath(claude_json)
		elif env_config and claude_dir is None and os.path.exists(os.path.join(self.claude_dir, ".claude.json")):
			self.claude_json = os.path.join(self.claude_dir, ".claude.json")
		else:
			self.claude_json = os.path.join(self.home, ".claude.json")

	def with_config(self, config: "MachineConfig") -> "Paths":
		"""Chemins de Claude Code tels qu'enregistrés à la configuration.

		Ils ne dépendent donc pas de l'environnement du moment : launchd, systemd ou la fenêtre
		ne voient pas forcément la variable CLAUDE_CONFIG_DIR de votre terminal."""
		claude_dir = config.get("claude_dir")
		claude_json = config.get("claude_json")
		if not claude_dir:
			return self
		env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
		if env_dir and os.path.abspath(env_dir) != os.path.abspath(claude_dir):
			import logging
			logging.getLogger("csync").warning(
				"CLAUDE_CONFIG_DIR (%s) diffère du dossier enregistré (%s) : c'est ce dernier qui est synchronisé.",
				env_dir, claude_dir)
		return Paths(csync_dir=self.csync_dir, home=self.home, claude_dir=claude_dir,
					 claude_json=claude_json or self.claude_json)

	# Fichiers de CSync
	@property
	def config_file(self) -> str:
		return os.path.join(self.csync_dir, "config.json")

	@property
	def profile_file(self) -> str:
		return os.path.join(self.csync_dir, "profile.json")

	@property
	def state_file(self) -> str:
		return os.path.join(self.csync_dir, "state.json")

	@property
	def repo_dir(self) -> str:
		return os.path.join(self.csync_dir, "repo")

	@property
	def backups_dir(self) -> str:
		return os.path.join(self.csync_dir, "backups")

	@property
	def logs_dir(self) -> str:
		return os.path.join(self.csync_dir, "logs")

	@property
	def log_file(self) -> str:
		return os.path.join(self.logs_dir, "csync.log")

	@property
	def lock_file(self) -> str:
		return os.path.join(self.csync_dir, "lock")

	@property
	def ai_dir(self) -> str:
		return os.path.join(self.csync_dir, "ai")

	@property
	def adapter_file(self) -> str:
		return os.path.join(self.csync_dir, "adapter.json")

	@property
	def pending_adapter_file(self) -> str:
		return os.path.join(self.csync_dir, "adapter.pending.json")

	@property
	def bin_path(self) -> str:
		return os.path.join(self.csync_dir, "bin", "csync")

	# Dossiers de Claude Code
	@property
	def projects_dir(self) -> str:
		return os.path.join(self.claude_dir, "projects")

	@property
	def settings_file(self) -> str:
		return os.path.join(self.claude_dir, "settings.json")

	@property
	def claude_sessions_dir(self) -> str:
		return os.path.join(self.claude_dir, "sessions")


class MachineConfig:
	"""Configuration propre à cette machine (config.json dans le dossier de CSync)."""

	def __init__(self, data: Dict[str, Any]):
		self.data = dict(DEFAULT_SETTINGS)
		self.data.update(data)

	@classmethod
	def load(cls, paths: Paths) -> "MachineConfig":
		data = read_json(paths.config_file)
		if data is None:
			raise CSyncError("CSync n'est pas encore configuré sur cette machine. Lancez « csync setup ».")
		return cls(data)

	@classmethod
	def exists(cls, paths: Paths) -> bool:
		return os.path.exists(paths.config_file)

	def save(self, paths: Paths) -> None:
		ensure_dir(paths.csync_dir, 0o700)
		write_json(paths.config_file, self.data, 0o600)

	def get(self, key: str, default: Any = None) -> Any:
		return self.data.get(key, default)

	@property
	def machine_id(self) -> str:
		return self.data["machine_id"]

	@property
	def machine_name(self) -> str:
		return self.data.get("machine_name") or self.machine_id

	@property
	def repo_url(self) -> str:
		return self.data["repo_url"]

	@property
	def key_path(self) -> str:
		return os.path.expanduser(self.data["key_path"])

	@property
	def branch(self) -> str:
		return self.data.get("branch") or "main"


def new_machine_id() -> str:
	return uuid.uuid4().hex[:12]


def default_machine_name() -> str:
	name = socket.gethostname().split(".")[0] or "machine"
	return name


def system_name() -> str:
	return platform.system().lower()


_HW_ID: Optional[str] = None


def hardware_id() -> str:
	"""Identifiant stable de l'ordinateur (vide s'il est introuvable)."""
	global _HW_ID
	if _HW_ID is not None:
		return _HW_ID
	value = ""
	if system_name() == "darwin":
		from .util import run
		result = run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], timeout=10)
		if result.ok:
			import re
			m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', result.out)
			value = m.group(1) if m else ""
	else:
		for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
			try:
				with open(candidate, "r") as fh:
					value = fh.read().strip()
			except OSError:
				continue
			if value:
				break
	_HW_ID = value
	return value


def machine_binding(paths: "Paths") -> Optional[str]:
	"""Empreinte qui relie la configuration de CSync à cet ordinateur et à ce compte.

	Elle permet de reconnaître un dossier ~/.csync copié d'une autre machine (sauvegarde
	restaurée, assistant de migration) : deux machines ne doivent jamais partager une identité."""
	hw = hardware_id()
	if not hw:
		return None
	import hashlib
	return hashlib.sha256(("%s|%s" % (hw, paths.home)).encode("utf-8")).hexdigest()[:20]


def can_create_work_dir(path: str, paths: "Paths") -> bool:
	"""Vrai si CSync peut créer ce dossier de travail absent : il se trouve dans le dossier personnel,
	hors de la corbeille et des dossiers de Claude Code et de CSync, et rien n'existe à sa place."""
	from .util import path_inside
	local = os.path.normpath(path)
	home = os.path.normpath(paths.home)
	if local == home or not path_inside(local, home) or os.path.lexists(local):
		return False
	# ~/Library appartient aux applications : l'app Desktop, par exemple, y supprime les dossiers de
	# sessions qu'elle ne reconnaît pas.
	for forbidden in (os.path.join(home, ".Trash"), os.path.join(home, "Library"), paths.claude_dir,
					  paths.csync_dir):
		if path_inside(local, forbidden):
			return False
	return True
