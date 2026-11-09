"""Synchronisation des fichiers de travail avec Syncthing.

CSync ne transporte pas lui-même les fichiers : il installe Syncthing (logiciel libre et gratuit, qui
copie des dossiers directement d'une machine à l'autre), le configure et affiche son état.

- L'identifiant Syncthing de chaque machine est publié dans son profil, dans le dépôt privé chiffré :
  à chaque synchronisation, CSync ajoute à Syncthing les machines qu'il y trouve et leur partage les
  dossiers. Aucune manipulation n'est nécessaire.
- Les dossiers partagés sont désignés par rapport au dossier personnel (« ~/Claude ») : ils ont donc le
  même chemin sur toutes les machines, et chaque conversation y retrouve son dossier.
- Un fichier supprimé ou remplacé par une autre machine est gardé 30 jours dans « .stversions ».
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from .paths import MachineConfig, Paths
from .util import CSyncError, LOG, run, tool_path

DEFAULT_FOLDERS = [
	{"id": "csync-claude", "path": "~/Claude", "label": "Claude"},
	{"id": "csync-claude-desktop", "path": "~/Library/Application Support/Claude/scratch-workspaces",
	 "label": "Claude Desktop (sessions sans dossier)"},
]
IGNORES = ["(?d).DS_Store", "(?d)node_modules", "(?d).venv", "(?d)__pycache__", "(?d).Spotlight-V100",
		   "(?d).Trashes"]
VERSION_DAYS = 30


# ---------------------------------------------------------------------------
# Accès à Syncthing
# ---------------------------------------------------------------------------

def config_candidates(paths: Paths) -> List[str]:
	home = paths.home
	return [os.path.join(home, "Library", "Application Support", "Syncthing", "config.xml"),
			os.path.join(home, ".local", "state", "syncthing", "config.xml"),
			os.path.join(home, ".config", "syncthing", "config.xml")]


def find_config_file(paths: Paths, config: Optional[MachineConfig] = None) -> Optional[str]:
	forced = config.get("syncthing_config") if config is not None else None
	for candidate in ([forced] if forced else []) + config_candidates(paths):
		if candidate and os.path.isfile(candidate):
			return candidate
	return None


class Syncthing:
	"""Petit client de l'interface de pilotage (REST) de Syncthing, sur cette machine."""

	def __init__(self, base_url: str, api_key: str, timeout: float = 10.0):
		self.base_url = base_url.rstrip("/")
		self.api_key = api_key
		self.timeout = timeout

	@classmethod
	def from_config_file(cls, path: str) -> "Syncthing":
		try:
			root = ET.parse(path).getroot()
		except (OSError, ET.ParseError) as exc:
			raise CSyncError("Configuration de Syncthing illisible (%s) : %s" % (path, exc))
		gui = root.find("gui")
		if gui is None or not (gui.findtext("apikey") or "").strip():
			raise CSyncError("Configuration de Syncthing incomplète (%s) : clé d'accès absente." % path)
		address = (gui.findtext("address") or "127.0.0.1:8384").strip()
		if address.startswith("0.0.0.0"):
			address = "127.0.0.1" + address[len("0.0.0.0"):]
		scheme = "https" if gui.get("tls", "false").lower() == "true" else "http"
		return cls("%s://%s" % (scheme, address), gui.findtext("apikey").strip())

	def request(self, method: str, path: str, body: Any = None, query: Optional[Dict[str, str]] = None) -> Any:
		url = self.base_url + path + ("?" + urllib.parse.urlencode(query) if query else "")
		data = json.dumps(body).encode("utf-8") if body is not None else None
		req = urllib.request.Request(url, data=data, method=method,
									 headers={"X-API-Key": self.api_key, "Content-Type": "application/json"})
		try:
			with urllib.request.urlopen(req, timeout=self.timeout) as resp:
				raw = resp.read()
		except urllib.error.HTTPError as exc:
			raise CSyncError("Syncthing a refusé « %s %s » : %s %s" % (method, path, exc.code,
																	   exc.read()[:200].decode("utf-8", "replace")))
		except (urllib.error.URLError, OSError) as exc:
			raise CSyncError("Syncthing ne répond pas (%s) : %s" % (self.base_url, getattr(exc, "reason", exc)))
		if not raw:
			return None
		try:
			return json.loads(raw.decode("utf-8"))
		except ValueError:
			return None

	def my_id(self) -> str:
		return self.request("GET", "/rest/system/status")["myID"]

	def config(self) -> Dict[str, Any]:
		return self.request("GET", "/rest/config")


def api(paths: Paths, config: Optional[MachineConfig] = None) -> Syncthing:
	path = find_config_file(paths, config)
	if path is None:
		raise CSyncError("Syncthing n'est pas installé (ou n'a jamais été lancé) sur cette machine. "
						 "Lancez « csync files setup ».")
	return Syncthing.from_config_file(path)


# ---------------------------------------------------------------------------
# Dossiers et appareils
# ---------------------------------------------------------------------------

def folders(paths: Paths, config: MachineConfig) -> List[Dict[str, str]]:
	"""Dossiers partagés, avec leur chemin sur cette machine."""
	result = []
	for f in config.get("file_folders") or DEFAULT_FOLDERS:
		if not isinstance(f, dict) or not f.get("id") or not f.get("path"):
			continue
		raw = str(f["path"])
		local = os.path.join(paths.home, raw[2:]) if raw.startswith("~/") else raw
		result.append({"id": str(f["id"]), "path": os.path.normpath(local), "label": str(f.get("label") or f["id"])})
	return result


def peers_from_profiles(profiles: Dict[str, Dict[str, Any]], my_id: str) -> List[Dict[str, str]]:
	peers = []
	for machine, prof in sorted(profiles.items()):
		sid = prof.get("syncthing_id")
		if isinstance(sid, str) and sid and sid != my_id:
			peers.append({"id": sid, "name": str(prof.get("machine_name") or machine)})
	return peers


def ensure(st: Syncthing, paths: Paths, config: MachineConfig, peers: List[Dict[str, str]]) -> List[str]:
	"""Met la configuration de Syncthing en accord avec CSync. Renvoie la liste des changements."""
	changes: List[str] = []
	current = st.config()
	my_id = st.my_id()
	devices = {d.get("deviceID"): d for d in current.get("devices", [])}
	for peer in peers:
		if peer["id"] in devices:
			continue
		dev = st.request("GET", "/rest/config/defaults/device") or {}
		dev.update({"deviceID": peer["id"], "name": peer["name"], "addresses": ["dynamic"],
					"autoAcceptFolders": False})
		st.request("PUT", "/rest/config/devices/" + urllib.parse.quote(peer["id"], safe=""), dev)
		changes.append("appareil ajouté : %s" % peer["name"])
	existing = {f.get("id"): f for f in current.get("folders", [])}
	wanted_devices = [my_id] + [p["id"] for p in peers]
	for f in folders(paths, config):
		os.makedirs(f["path"], exist_ok=True)
		folder = existing.get(f["id"])
		if folder is None:
			folder = st.request("GET", "/rest/config/defaults/folder") or {}
			folder.update({"id": f["id"], "label": f["label"], "path": f["path"], "type": "sendreceive",
						   "fsWatcherEnabled": True, "rescanIntervalS": 3600,
						   "versioning": {"type": "trashcan", "params": {"cleanoutDays": str(VERSION_DAYS)},
										  "cleanupIntervalS": 3600},
						   "devices": [{"deviceID": d} for d in wanted_devices]})
			st.request("PUT", "/rest/config/folders/" + urllib.parse.quote(f["id"], safe=""), folder)
			st.request("POST", "/rest/db/ignores", {"ignore": IGNORES}, query={"folder": f["id"]})
			changes.append("dossier partagé : %s" % f["path"])
			continue
		have = {d.get("deviceID") for d in folder.get("devices", [])}
		missing = [d for d in wanted_devices if d not in have]
		if missing:
			folder["devices"] = list(folder.get("devices", [])) + [{"deviceID": d} for d in missing]
			st.request("PUT", "/rest/config/folders/" + urllib.parse.quote(f["id"], safe=""), folder)
			changes.append("dossier %s partagé avec %d appareil(s) de plus" % (f["label"], len(missing)))
	return changes


def _remove_default_folder(st: Syncthing, paths: Paths) -> None:
	"""Le dossier « Sync » que Syncthing crée à son premier lancement ne sert pas : il est retiré s'il est vide."""
	for folder in st.config().get("folders", []):
		if folder.get("id") != "default":
			continue
		path = folder.get("path") or ""
		try:
			content = [n for n in os.listdir(path) if n not in (".stfolder", ".DS_Store")]
		except OSError:
			content = []
		if content:
			return
		st.request("DELETE", "/rest/config/folders/default")
		shutil.rmtree(os.path.join(path, ".stfolder"), ignore_errors=True)
		try:
			os.rmdir(path)
		except OSError:
			pass


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------

def _brew() -> Optional[str]:
	return shutil.which("brew", path=tool_path())


def install_and_start(paths: Paths, config: MachineConfig, wait_seconds: float = 60.0) -> Syncthing:
	"""Installe Syncthing avec Homebrew s'il manque, le lance au démarrage de la machine, puis attend
	qu'il réponde."""
	if find_config_file(paths, config) is None or not shutil.which("syncthing", path=tool_path()):
		brew = _brew()
		if brew is None:
			raise CSyncError("Homebrew est introuvable : installez Syncthing vous-même (https://syncthing.net), "
							 "puis relancez « csync files setup ».")
		if not shutil.which("syncthing", path=tool_path()):
			print("Installation de Syncthing avec Homebrew…")
			res = run([brew, "install", "syncthing"], timeout=900)
			if not res.ok:
				raise CSyncError("Installation de Syncthing impossible : %s" % (res.err or res.out)[-400:])
		print("Lancement de Syncthing (et à chaque démarrage du Mac)…")
		res = run([brew, "services", "start", "syncthing"], timeout=120)
		if not res.ok and "already started" not in (res.err + res.out):
			raise CSyncError("Lancement de Syncthing impossible : %s" % (res.err or res.out)[-400:])
	deadline = time.time() + wait_seconds
	last_error: Optional[Exception] = None
	while time.time() < deadline:
		try:
			st = api(paths, config)
			st.my_id()
			return st
		except CSyncError as exc:
			last_error = exc
			time.sleep(1)
	raise CSyncError("Syncthing ne répond pas après son lancement : %s" % last_error)


def setup(paths: Paths, peers_profiles: Dict[str, Dict[str, Any]], st: Optional[Syncthing] = None) -> List[str]:
	"""« csync files setup » : installation, identifiant publié, dossiers et appareils configurés."""
	config = MachineConfig.load(paths)
	st = st or install_and_start(paths, config)
	my_id = st.my_id()
	_remove_default_folder(st, paths)
	config.data["files_enabled"] = True
	config.data["syncthing_id"] = my_id
	if not config.get("file_folders"):
		config.data["file_folders"] = DEFAULT_FOLDERS
	config.save(paths)
	return ensure(st, paths, config, peers_from_profiles(peers_profiles, my_id))


def ensure_after_sync(paths: Paths, config: MachineConfig, profiles: Dict[str, Dict[str, Any]]) -> None:
	"""Appelé après chaque synchronisation : ajoute les machines apparues depuis. Ne lève jamais d'erreur."""
	if not config.get("files_enabled"):
		return
	try:
		st = api(paths, config)
		changes = ensure(st, paths, config, peers_from_profiles(profiles, st.my_id()))
		for c in changes:
			LOG.info("Syncthing : %s", c)
	except CSyncError as exc:
		LOG.warning("Synchronisation des fichiers : %s", exc)
	except Exception:  # la synchronisation des conversations ne doit jamais en pâtir
		LOG.exception("Synchronisation des fichiers : erreur inattendue")


# ---------------------------------------------------------------------------
# État
# ---------------------------------------------------------------------------

def status(paths: Paths, config: MachineConfig, profiles: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
	"""État des fichiers, pour « csync status » et la fenêtre."""
	result: Dict[str, Any] = {"enabled": bool(config.get("files_enabled")), "alerts": [], "lines": []}
	if not result["enabled"]:
		return result
	try:
		st = api(paths, config)
		my_id = st.my_id()
		conns = (st.request("GET", "/rest/system/connections") or {}).get("connections", {})
	except CSyncError:
		result["alerts"].append("Fichiers : Syncthing ne répond pas. Relancez-le avec « brew services restart "
								"syncthing » (ou « csync files setup »).")
		return result
	peers = peers_from_profiles(profiles, my_id)
	if not peers:
		result["lines"].append("Fichiers : en attente de l'autre machine (lancez « csync files setup » sur elle).")
		return result
	for peer in peers:
		connected = bool(conns.get(peer["id"], {}).get("connected"))
		pending = 0
		for f in folders(paths, config):
			try:
				comp = st.request("GET", "/rest/db/completion", query={"folder": f["id"], "device": peer["id"]}) or {}
				pending += int(comp.get("needItems") or 0) + int(comp.get("needDeletes") or 0)
			except CSyncError:
				continue
		for f in folders(paths, config):
			try:
				local = st.request("GET", "/rest/db/status", query={"folder": f["id"]}) or {}
				pending += int(local.get("needFiles") or 0) + int(local.get("needDeletes") or 0)
			except CSyncError:
				continue
		if not connected:
			result["lines"].append("Fichiers : %s hors ligne (les changements partiront à sa prochaine connexion)."
								   % peer["name"])
		elif pending:
			result["lines"].append("Fichiers : %d élément(s) en cours d'échange avec %s." % (pending, peer["name"]))
		else:
			result["lines"].append("Fichiers : à jour avec %s." % peer["name"])
	return result
