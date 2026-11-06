"""Configuration guidée d'une machine (« csync setup »)."""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any, Callable, Dict, List, Optional

from .crypto import Cipher, generate_key_file, key_permissions_warning, load_key
from .hooks import install_hooks
from .paths import MachineConfig, Paths, default_machine_name, new_machine_id, system_name
from .scan import apply_user_choices, save_profile, scan
from .adapter import load_adapter
from .schedule import install_schedule
from .store import Store
from .util import CSyncError, ensure_dir, run, which


class Asker:
	"""Questions posées dans le terminal ; en mode --yes, la réponse par défaut est prise."""

	def __init__(self, assume_yes: bool = False, out: Callable[[str], None] = print):
		self.assume_yes = assume_yes
		self.out = out

	def text(self, question: str, default: Optional[str] = None) -> str:
		if self.assume_yes:
			if default is None:
				raise CSyncError("Réponse requise en mode non interactif : %s" % question)
			return default
		suffix = " [%s]" % default if default else ""
		while True:
			try:
				answer = input("%s%s : " % (question, suffix)).strip()
			except EOFError:
				answer = ""
			if answer:
				return answer
			if default is not None:
				return default

	def yes(self, question: str, default: bool = True) -> bool:
		if self.assume_yes:
			return default
		hint = "O/n" if default else "o/N"
		try:
			answer = input("%s [%s] : " % (question, hint)).strip().lower()
		except EOFError:
			answer = ""
		if not answer:
			return default
		return answer in ("o", "oui", "y", "yes")


def run_setup(paths: Paths, options: Dict[str, Any], out: Callable[[str], None] = print) -> int:
	ask = Asker(options.get("yes", False), out)
	out("Configuration de CSync sur cette machine.\n")

	if which("git") is None:
		raise CSyncError("git est introuvable. Installez-le puis relancez « csync setup ».")
	if sys.version_info < (3, 9):
		raise CSyncError("Python 3.9 ou plus récent est requis.")
	from .util import find_claude
	if find_claude() is None:
		out("Remarque : la commande « claude » est introuvable. CSync fonctionnera, mais Claude Code devra "
			"être installé pour que les hooks servent.\n")

	existing = MachineConfig.exists(paths)
	data: Dict[str, Any] = {}
	if existing:
		current = MachineConfig.load(paths)
		if not ask.yes("CSync est déjà configuré ici (machine « %s »). Reprendre la configuration ?"
					   % current.machine_name, default=True):
			return 0
		data = dict(current.data)
		from .paths import machine_binding
		binding = machine_binding(paths)
		if binding and data.get("binding") and data["binding"] != binding:
			# Configuration copiée depuis une autre machine : celle-ci reçoit sa propre identité,
			# et rien de ce que l'autre machine savait n'est réutilisé.
			out("Cette configuration provient d'une autre machine : une identité propre est créée pour celle-ci.")
			for key in ("machine_id", "machine_name", "binding", "claude_dir", "claude_json"):
				data.pop(key, None)
			# Le profil est gardé : vos choix de projets (exclusions, noms communs) restent valables.
			for f in (paths.state_file, paths.state_file + ".bak", paths.state_file + ".journal",
					  os.path.join(paths.csync_dir, "status.json")):
				if os.path.exists(f):
					os.remove(f)
			shutil.rmtree(paths.repo_dir, ignore_errors=True)

	data.setdefault("machine_id", new_machine_id())
	from .util import find_claude
	if find_claude():
		data["claude_path"] = find_claude()   # pour la fenêtre et les tâches de fond
	from .paths import machine_binding
	if machine_binding(paths):
		data["binding"] = machine_binding(paths)
	data["machine_name"] = options.get("name") or ask.text("Nom de cette machine", data.get("machine_name") or default_machine_name())
	data["repo_url"] = options.get("repo") or ask.text("Adresse du dépôt GitHub privé des données (ex. git@github.com-csync:vous/csync-data.git)",
													   data.get("repo_url"))

	# Clé de chiffrement
	key_path = options.get("key") or data.get("key_path")
	create = options.get("create_key")
	if not key_path:
		has_key = ask.yes("Avez-vous déjà une clé CSync (créée sur une autre machine) ?", default=False)
		if has_key:
			key_path = ask.text("Chemin du fichier de clé")
		else:
			key_path = ask.text("Où créer la nouvelle clé ? (vous la rangerez vous-même)", "~/csync.key")
			create = True
	key_path = os.path.expanduser(key_path)
	if create and not os.path.exists(key_path):
		generate_key_file(key_path)
		out("\nClé créée : %s" % key_path)
		out("Gardez-en une copie en lieu sûr (gestionnaire de mots de passe). Pour l'autre machine, copiez ce\n"
			"fichier par un moyen sûr (par exemple scp), jamais par le dépôt GitHub.\n")
	cipher = Cipher(load_key(key_path))
	warning = key_permissions_warning(key_path)
	if warning:
		out("Attention : " + warning)
	data["key_path"] = key_path

	# Emplacement de la configuration de Claude Code, enregistré une fois pour toutes : les
	# lancements automatiques (launchd, systemd, fenêtre) ne voient pas forcément les variables
	# d'environnement de votre terminal.
	env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
	if not data.get("claude_dir"):
		data["claude_dir"], data["claude_json"] = paths.claude_dir, paths.claude_json
	elif env_dir and os.path.abspath(env_dir) != os.path.abspath(data["claude_dir"]):
		base = Paths(csync_dir=paths.csync_dir, home=paths.home)
		if ask.yes("CLAUDE_CONFIG_DIR désigne %s, alors que CSync synchronise %s. Synchroniser désormais %s ?"
				   % (base.claude_dir, data["claude_dir"], base.claude_dir), default=True):
			data["claude_dir"], data["claude_json"] = base.claude_dir, base.claude_json
	config = MachineConfig(data)
	ensure_dir(paths.csync_dir, 0o700)
	config.save(paths)
	paths = paths.with_config(config)
	out("Configuration de Claude Code synchronisée : %s (et %s)." % (paths.claude_dir, paths.claude_json))
	if not os.path.isdir(paths.claude_dir):
		ensure_dir(paths.claude_dir, 0o700)

	# Dépôt de données
	store = Store(paths.repo_dir, cipher, config.machine_id, config.branch, config.get("git_timeout", 120))
	if store.is_cloned():
		remote = store.git("remote", "get-url", "origin", check=False)
		if remote.ok and remote.out.strip() != config.repo_url:
			out("Le dépôt local pointe vers une autre adresse : il est recloné.")
			shutil.rmtree(paths.repo_dir)
	if not store.is_cloned():
		out("Clonage du dépôt de données…")
		store.clone(config.repo_url)
	if not store.remote_has_branch():
		out("Dépôt vide : initialisation (première machine).")
		store.initialize_remote()
	else:
		store.fetch_and_rebase()
		if not store.looks_initialized():
			if not store.can_initialize_here():
				raise CSyncError("Ce dépôt contient déjà d'autres fichiers que des données CSync. Créez un dépôt "
								 "privé vide, réservé à CSync, et relancez « csync setup ».")
			out("Dépôt sans données CSync : initialisation (première machine).")
			store.initialize_remote()
		else:
			store.check_format()
			store.verify_key()
			out("Dépôt de données et clé vérifiés.")

	# Scan
	out("\nScan de la machine…")
	adapter = load_adapter(paths.adapter_file)
	remote_profiles = store.other_profiles()
	from .scan import load_profile
	result = scan(paths, adapter, config.machine_id, config.machine_name, previous=load_profile(paths),
				  remote_profiles=remote_profiles, full=True)
	profile = result.profile
	print_scan(result, out)
	choices = ask_unconfirmed(result, ask)
	profile = apply_user_choices(profile, choices)
	save_profile(paths, profile)
	if not profile["format_check"]["sessions_ok"] or not profile["format_check"]["mcp_ok"]:
		out("\nAttention : le format de certains fichiers de Claude Code n'est pas reconnu. La partie concernée "
			"ne sera pas synchronisée tant qu'une réparation n'est pas approuvée (« csync repair »).")

	# Sauvegarde complète de la configuration avant toute réception
	backup = initial_backup(paths)
	if backup:
		out("Sauvegarde de la configuration actuelle : %s" % backup)

	# Une machine du même nom existe déjà : sans doute celle-ci avant une réinstallation.
	from . import ops
	for machine in store.list_machines():
		if machine == config.machine_id:
			continue
		try:
			doc = store.read_manifest(machine)
		except (CSyncError, ValueError):
			doc = None
		if doc and doc[0].get("machine_name") == config.machine_name:
			if ask.yes("Une machine nommée « %s » figure déjà dans le dépôt (identifiant %s). S'il s'agit de cette "
					   "machine avant une réinstallation, son ancienne entrée doit être retirée. La retirer ?"
					   % (config.machine_name, machine), default=False):
				ops.do_forget_machine(paths, machine, force=True)
				out("Ancienne entrée retirée.")

	# Première synchronisation
	others = store.list_machines()
	others = [m for m in others if m != config.machine_id]
	prefer_remote = bool(options.get("prefer_remote"))
	if others:
		out("\nUne autre machine a déjà envoyé ses données. Aperçu de ce qui serait reçu :")
		preview = ops.do_pull(paths, dry_run=True)
		for line in preview.summary_lines():
			out("  " + line)
		if not options.get("prefer_remote_set"):
			prefer_remote = ask.yes("Quand un fichier de configuration diffère entre les deux machines, adopter la "
									"version de l'autre machine ? (sinon, la version locale est gardée et une copie "
									"de conflit est créée)", default=False)
		if ask.yes("Lancer la première synchronisation maintenant ?", True):
			reports = ops.do_sync(paths, prefer_remote=prefer_remote)
			for report in reports:
				for line in report.summary_lines():
					out("  " + line)
	else:
		if ask.yes("\nEnvoyer maintenant les données de cette machine ?", True):
			report = ops.do_push(paths)
			for line in report.summary_lines():
				out("  " + line)
	# Intégrations, après la première synchronisation : les réglages reçus de l'autre machine
	# sont déjà en place quand les hooks y sont ajoutés.
	if not options.get("no_hooks") and ask.yes("\nInstaller les hooks dans Claude Code (synchronisation automatique) ?", True):
		try:
			install_hooks(paths)
			out("Hooks installés dans %s." % paths.settings_file)
		except CSyncError as exc:
			out("Hooks non installés : %s" % exc)
	if not options.get("no_schedule") and ask.yes("Installer la vérification périodique (toutes les %d minutes) ?"
												  % config.get("periodic_minutes", 15), True):
		ok, message = install_schedule(paths, int(config.get("periodic_minutes", 15)))
		out(message)
	if system_name() == "darwin" and not options.get("no_app"):
		try:
			from .macapp import install_app
			out(install_app(paths))
		except CSyncError as exc:
			out("Application non installée : %s" % exc)
	out("\nCSync est prêt sur « %s »." % config.machine_name)
	return 0


def ask_unconfirmed(result, ask: "Asker") -> Dict[str, Dict[str, Any]]:
	"""Dossiers sans git hors du dossier personnel : partagés seulement sur demande, sous un nom choisi."""
	choices: Dict[str, Dict[str, Any]] = {}
	for p in result.diff.get("to_confirm", []):
		if not ask.yes("Le dossier %s n'a pas de dépôt git. Le partager avec l'autre machine (il faudra lui donner "
					   "le même nom des deux côtés) ?" % p["path"], default=False):
			choices[p["path"]] = {"include": False}
			continue
		name = ask.text("Nom commun pour ce dossier (le même sur l'autre machine)")
		choices[p["path"]] = {"name": name, "include": True}
	return choices


def initial_backup(paths: Paths) -> Optional[str]:
	"""Copie la configuration de Claude Code (hors sessions) et ~/.claude.json avant la première réception."""
	import time
	from .sync import CONFIG_DIRS, CONFIG_FILES
	import uuid
	dest = os.path.join(paths.backups_dir, "initial-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6]))
	copied = False
	for name in CONFIG_FILES:
		src = os.path.join(paths.claude_dir, name)
		if os.path.isfile(src):
			ensure_dir(dest, 0o700)
			shutil.copy2(src, os.path.join(dest, name))
			copied = True
	for name in CONFIG_DIRS:
		src = os.path.join(paths.claude_dir, name)
		if os.path.isdir(src):
			shutil.copytree(src, os.path.join(dest, name), symlinks=True,
							ignore=shutil.ignore_patterns(".git", "node_modules", "synced", ".trash"))
			copied = True
	if os.path.isfile(paths.claude_json):
		ensure_dir(dest, 0o700)
		shutil.copy2(paths.claude_json, os.path.join(dest, ".claude.json"))
		copied = True
	return dest if copied else None


def print_scan(result, out: Callable[[str], None] = print) -> None:
	profile = result.profile
	projects = profile.get("projects", [])
	out("Claude Code : %s" % (profile.get("claude_version") or "version inconnue"))
	out("Dossiers de session trouvés : %d ; projets partagés : %d" % (len(profile.get("dir_map", {})), len(projects)))
	for p in projects:
		flag = "" if p.get("include", True) else " (exclu)"
		out("  - %s  →  %s%s" % (p["key"], p["path"], flag))
	diff = result.diff
	for p in diff.get("added", []):
		out("  Nouveau projet : %s (%s)" % (p["key"], p["path"]))
	for p in diff.get("moved", []):
		out("  Projet déplacé : %s, de %s vers %s" % (p["key"], p["old"], p["new"]))
	for p in diff.get("removed", []):
		out("  Projet disparu : %s (%s)" % (p["key"], p["path"]))
	for p in diff.get("remote_only", []):
		out("  Présent seulement sur %s : %s (%s)" % (p["machine"], p["key"], p.get("remote_path") or "?"))
	if profile.get("unresolved_dirs"):
		out("  Dossiers de session non identifiés (ignorés) : %d" % len(profile["unresolved_dirs"]))
	for w in profile.get("warnings", []):
		out("  Attention : " + w)
	fc = profile.get("format_check", {})
	for d in fc.get("details", []):
		out("  Format : " + d)
	for w in fc.get("warnings", []):
		out("  Format (avertissement) : " + w)
