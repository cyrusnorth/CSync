"""Interface de pilotage utilisée par la fenêtre et par certaines commandes.

Ce module ne dépend d'aucune bibliothèque graphique : toute la logique reste ici,
la fenêtre ne fait qu'afficher et appeler ces fonctions.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from . import ops
from .adapter import load_adapter
from .crypto import Cipher, key_permissions_warning, load_key
from .hooks import hooks_installed
from .paths import MachineConfig, Paths, system_name
from .scan import ScanResult, add_named_project, apply_user_choices, load_profile, save_profile, scan
from .state import State
from .store import Store
from .util import CSyncError, human_time, read_json


class Controller:
	def __init__(self, paths: Optional[Paths] = None):
		paths = paths or Paths()
		if MachineConfig.exists(paths):
			try:
				paths = paths.with_config(MachineConfig.load(paths))
			except CSyncError:
				pass
		self.paths = paths

	# ------------------------------------------------------------------
	# État
	# ------------------------------------------------------------------

	def configured(self) -> bool:
		return MachineConfig.exists(self.paths)

	def _store(self, config: MachineConfig) -> Optional[Store]:
		try:
			cipher = Cipher(load_key(config.key_path))
		except CSyncError:
			return None
		return Store(self.paths.repo_dir, cipher, config.machine_id, config.branch)

	def status(self) -> Dict[str, Any]:
		result: Dict[str, Any] = {"configured": self.configured(), "alerts": [], "machines": [], "conflicts": [],
								  "parked": [], "last_push": None, "last_pull": None}
		if not result["configured"]:
			return result
		config = MachineConfig.load(self.paths)
		state = State.load(self.paths)
		status = ops.read_status(self.paths)
		profile = load_profile(self.paths) or {}
		result.update({
			"machine_name": config.machine_name,
			"machine_id": config.machine_id,
			"last_push": status.get("last_push") or state.get("last_push"),
			"last_pull": status.get("last_pull") or state.get("last_pull"),
			"last_error": status.get("last_error"),
			"last_error_ts": status.get("last_error_ts"),
			"conflicts": list(state.conflicts),
			"parked": list(state.get("parked", [])),
			"pending_mcp": list(state.get("pending_mcp", [])),
			"format_check": profile.get("format_check", {}),
			"claude_version": profile.get("claude_version"),
			"pending_repair": os.path.exists(self.paths.pending_adapter_file),
		})
		alerts: List[str] = result["alerts"]
		if result["last_error"]:
			alerts.append("Dernière erreur (%s) : %s" % (human_time(result["last_error_ts"]), result["last_error"]))
		if result["conflicts"]:
			alerts.append("%d conflit(s) à examiner." % len(result["conflicts"]))
		if result["parked"]:
			total = sum(p.get("count", 1) for p in result["parked"])
			alerts.append("%d session(s) en attente : projet absent de cette machine." % total)
		if result["pending_mcp"]:
			alerts.append("Serveurs MCP à appliquer dès que Claude Code sera fermé : %s." % ", ".join(result["pending_mcp"]))
		links = state.get("unmanaged_links") or []
		if links:
			shown = ", ".join(links[:3]) + (" (et %d autres)" % (len(links) - 3) if len(links) > 3 else "")
			alerts.append("Non synchronisés parce que ce sont des liens symboliques : %s." % shown)
		for p in profile.get("projects", []):
			if p.get("missing"):
				alerts.append("Dossier partagé introuvable : %s (« %s »). S'il a été déplacé, lancez un scan."
							  % (p.get("path"), str(p.get("key", ""))[4:]))
		unconfirmed = [p["path"] for p in profile.get("projects", []) if str(p.get("key", "")).startswith("unconfirmed:")]
		if unconfirmed:
			alerts.append("Dossiers sans git non partagés : %s. Pour en partager un, donnez-lui le même nom sur les "
						  "deux machines (« csync name-project <dossier> <nom> »)." % ", ".join(unconfirmed[:3]))
		for w in profile.get("warnings", []) or []:
			alerts.append(w)
		fc = result["format_check"]
		if fc and not (fc.get("sessions_ok", True) and fc.get("mcp_ok", True)):
			alerts.append("Format de fichiers non reconnu : une partie de la synchronisation est suspendue "
						  "(réparation possible).")
		store = self._store(config)
		if store is None:
			alerts.append("Fichier de clé illisible : %s" % config.key_path)
		elif store.is_cloned():
			profiles: Dict[str, Any] = {}
			metas: Dict[str, Any] = {}
			try:
				profiles = store.other_profiles()
				for machine in store.list_machines():
					if machine == config.machine_id:
						continue
					try:
						doc = store.read_manifest(machine)
					except (CSyncError, ValueError):
						doc = None
					if doc is not None:
						metas[machine] = doc[0]
			except CSyncError as exc:
				alerts.append(str(exc))
			for machine in sorted(set(profiles) | set(metas)):
				prof = profiles.get(machine, {})
				meta = metas.get(machine, {})
				m = {"id": machine, "name": prof.get("machine_name") or meta.get("machine_name") or machine,
					 "claude_version": prof.get("claude_version"), "updated": meta.get("updated"),
					 "system": prof.get("system")}
				result["machines"].append(m)
				if m["claude_version"] and result["claude_version"] and m["claude_version"] != result["claude_version"]:
					alerts.append("Versions de Claude Code différentes : %s ici, %s sur %s. Les formats de fichiers "
								  "peuvent différer ; mettez à jour la plus ancienne." % (result["claude_version"],
																						  m["claude_version"], m["name"]))
			size = store.size_mb()
			result["repo_size_mb"] = round(size, 1)
			limit = float(config.get("auto_compact_mb", 300) or 0)
			if limit > 0 and size > limit * 1.5:
				result["compact_advised"] = True
				alerts.append("Le dépôt de données occupe %.0f Mo : un compactage est conseillé (« csync compact »)." % size)
		guard = state.get("deletion_guard")
		if isinstance(guard, dict) and guard.get("count"):
			alerts.append("%d éléments de configuration (fichiers ou serveurs MCP) ont disparu d'un coup : leur "
						  "suppression n'a pas été envoyée. Si elle est voulue : « csync push --allow-deletions »."
						  % guard["count"])
		return result

	# ------------------------------------------------------------------
	# Actions
	# ------------------------------------------------------------------

	def sync(self, dry_run: bool = False):
		return ops.do_sync(self.paths, dry_run=dry_run, lock_timeout=30)

	def rescan(self) -> ScanResult:
		config = MachineConfig.load(self.paths)
		store = self._store(config)
		remote_profiles = {}
		if store is not None and store.is_cloned():
			try:
				remote_profiles = store.other_profiles()
			except CSyncError:
				remote_profiles = {}
		return scan(self.paths, load_adapter(self.paths.adapter_file), config.machine_id, config.machine_name,
					previous=load_profile(self.paths), remote_profiles=remote_profiles, full=True)

	def _locked(self, func):
		from .util import FileLock
		lock = FileLock(self.paths.lock_file)
		if not lock.acquire(timeout=60):
			raise CSyncError("Une autre opération CSync est en cours ; réessayez dans un instant.")
		try:
			return func()
		finally:
			lock.release()

	def apply_scan(self, result: ScanResult, choices: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
		profile = apply_user_choices(result.profile, choices)
		self._locked(lambda: save_profile(self.paths, profile))
		return profile

	def name_project(self, path: str, name: str) -> None:
		def go():
			profile = load_profile(self.paths)
			if profile is None:
				raise CSyncError("Aucun profil : lancez d'abord un scan.")
			save_profile(self.paths, add_named_project(profile, path, name))
		self._locked(go)

	def profile_text(self) -> str:
		profile = load_profile(self.paths)
		if not profile:
			return "Aucun profil : lancez un scan."
		lines = [
			"Machine : %s (%s)" % (profile.get("machine_name"), profile.get("system")),
			"Claude Code : %s" % (profile.get("claude_version") or "version inconnue"),
			"Dossier personnel : %s" % profile.get("home"),
			"Configuration de Claude : %s" % profile.get("claude_dir"),
			"Dernier scan : %s" % profile.get("updated"),
			"",
			"Projets partagés :",
		]
		for p in profile.get("projects", []):
			lines.append("  %s %s\n      %s" % ("✓" if p.get("include", True) else "✗", p["key"], p["path"]))
		if not profile.get("projects"):
			lines.append("  (aucun : les sessions sont repérées par rapport au dossier personnel)")
		lines += ["", "Dossiers de session : %d" % len(profile.get("dir_map", {}))]
		if profile.get("unresolved_dirs"):
			lines.append("Dossiers non identifiés (ignorés) : %s" % ", ".join(profile["unresolved_dirs"]))
		lines += ["", "Plugins activés : %s" % (", ".join(profile.get("plugins", [])) or "aucun"),
				  "Serveurs MCP : %s" % (", ".join(profile.get("mcp_servers", [])) or "aucun")]
		fc = profile.get("format_check", {})
		lines += ["", "Vérification du format : %s" % ("OK" if fc.get("sessions_ok", True) and fc.get("mcp_ok", True) else "à réparer")]
		lines += ["  " + d for d in fc.get("details", [])]
		lines += ["  (avertissement) " + w for w in fc.get("warnings", [])]
		return "\n".join(lines)

	def compact(self):
		return ops.do_compact(self.paths)

	def resolve_conflicts(self, ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
		from .util import FileLock
		lock = FileLock(self.paths.lock_file)
		if not lock.acquire(timeout=30):
			raise CSyncError("Une autre opération CSync est en cours ; réessayez dans un instant.")
		try:
			state = State.load(self.paths)
			done = state.resolve_conflicts(ids)
			state.save(self.paths)
			return done
		finally:
			lock.release()

	def repair_propose(self):
		from . import ai
		return ai.propose(self.paths)

	def repair_approve(self) -> None:
		from . import ai
		ai.approve(self.paths)
		# Nouveau scan rapide pour que la vérification reflète l'adaptateur approuvé.
		config = MachineConfig.load(self.paths)
		profile = load_profile(self.paths) or {}
		result = scan(self.paths, load_adapter(self.paths.adapter_file), config.machine_id, config.machine_name,
					  previous=profile, full=False)
		save_profile(self.paths, result.profile)

	def repair_reject(self) -> None:
		from . import ai
		ai.reject(self.paths)

	def pending_repair(self) -> Optional[Dict[str, Any]]:
		from . import ai
		return ai.pending(self.paths)

	# ------------------------------------------------------------------
	# Vérification de l'installation (premier lancement de la fenêtre)
	# ------------------------------------------------------------------

	def environment_check(self) -> List[str]:
		problems: List[str] = []
		if not self.configured():
			return ["CSync n'est pas configuré : lancez « csync setup » dans un terminal."]
		config = MachineConfig.load(self.paths)
		if not os.path.exists(self.paths.bin_path):
			problems.append("Le lanceur %s est absent : relancez install.sh." % self.paths.bin_path)
		try:
			load_key(config.key_path)
			warning = key_permissions_warning(config.key_path)
			if warning:
				problems.append(warning)
		except CSyncError as exc:
			problems.append(str(exc))
		if not os.path.isdir(os.path.join(self.paths.repo_dir, ".git")):
			problems.append("Le dépôt de données n'est pas cloné : relancez « csync setup ».")
		if not hooks_installed(self.paths):
			problems.append("Les hooks de Claude Code ne sont pas installés (« csync hooks install »).")
		from .schedule import schedule_installed
		installed = schedule_installed(self.paths)
		if installed is False:
			problems.append("La vérification périodique n'est pas active (« csync schedule install »).")
		return problems
