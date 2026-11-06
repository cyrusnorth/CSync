"""Ligne de commande de CSync."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import __version__
from .paths import MachineConfig, Paths
from .util import CSyncError, LOG, human_time, now, setup_logging


def _print_reports(reports, quiet: bool = False) -> None:
	if quiet:
		return
	for report in reports:
		title = {"pull": "Réception", "push": "Envoi"}.get(report.action, report.action)
		print("%s :" % title)
		for line in report.summary_lines():
			print("  " + line)
		for p in report.parked:
			print("  En attente : projet %s (sur %s : %s). Clonez-le ici puis lancez « csync scan »."
				  % (p["project"], p["machine"], p.get("remote_path") or "?"))


def cmd_setup(paths: Paths, args) -> int:
	from .setupwizard import run_setup
	options = {
		"yes": args.yes, "name": args.name, "repo": args.repo, "key": args.key, "create_key": args.create_key,
		"no_hooks": args.no_hooks, "no_schedule": args.no_schedule, "no_app": args.no_app,
		"prefer_remote": args.prefer_remote, "prefer_remote_set": args.prefer_remote or args.yes,
	}
	return run_setup(paths, options)


def cmd_sync(paths: Paths, args) -> int:
	from . import ops
	reports = ops.do_sync(paths, dry_run=args.dry_run, lock_timeout=args.wait_lock,
						  allow_deletions=args.allow_deletions)
	_print_reports(reports, args.quiet)
	return 0


def cmd_push(paths: Paths, args) -> int:
	from . import ops
	try:
		report = ops.do_push(paths, dry_run=args.dry_run, lock_timeout=args.wait_lock,
							 allow_deletions=args.allow_deletions)
	except ops.Busy:
		if args.background:
			LOG.info("Envoi ignoré : une autre opération est en cours.")
			return 0
		raise
	_print_reports([report], args.quiet)
	return 0


def cmd_pull(paths: Paths, args) -> int:
	from . import ops
	report = ops.do_pull(paths, dry_run=args.dry_run, prefer_remote=args.prefer_remote, lock_timeout=args.wait_lock)
	_print_reports([report], args.quiet)
	return 0


def cmd_status(paths: Paths, args) -> int:
	from .controller import Controller
	status = Controller(paths).status()
	if args.json:
		print(json.dumps(status, indent=2, ensure_ascii=False))
		return 0
	if not status["configured"]:
		print("CSync n'est pas configuré sur cette machine. Lancez « csync setup ».")
		return 1
	print("Machine : %s" % status["machine_name"])
	print("Dernier envoi : %s ; dernière réception : %s" % (human_time(status["last_push"]), human_time(status["last_pull"])))
	for m in status["machines"]:
		print("Autre machine : %s [%s] (Claude Code %s), dernière mise à jour %s"
			  % (m["name"], m["id"], m.get("claude_version") or "?", human_time(m.get("updated"))))
	for line in status["alerts"]:
		print("• " + line)
	if not status["alerts"]:
		print("Tout va bien.")
	return 0


def cmd_scan(paths: Paths, args) -> int:
	from .controller import Controller
	from .setupwizard import Asker, print_scan
	ctrl = Controller(paths)
	result = ctrl.rescan()
	print_scan(result)
	ask = Asker(args.yes)
	from .setupwizard import ask_unconfirmed
	choices = ask_unconfirmed(result, ask)
	if args.yes or ask.yes("Enregistrer ce profil ?", True):
		ctrl.apply_scan(result, choices)
		print("Profil enregistré.")
	return 0


def cmd_name_project(paths: Paths, args) -> int:
	from .controller import Controller
	Controller(paths).name_project(args.path, args.name)
	print("Le dossier %s est désormais partagé sous le nom « %s »." % (args.path, args.name))
	return 0


def cmd_profile(paths: Paths, args) -> int:
	from .controller import Controller
	print(Controller(paths).profile_text())
	return 0


def cmd_conflicts(paths: Paths, args) -> int:
	from .controller import Controller
	ctrl = Controller(paths)
	if args.resolve is not None:
		ids = None if args.resolve == "all" else [i.strip() for i in args.resolve.split(",") if i.strip()]
		done = ctrl.resolve_conflicts(ids)
		if ids is not None and len(done) != len(ids):
			raise CSyncError("Identifiant de conflit inconnu. Lancez « csync conflicts » pour voir la liste.")
		print("%d conflit(s) marqué(s) comme résolu(s)." % len(done))
		return 0
	conflicts = ctrl.status()["conflicts"]
	if not conflicts:
		print("Aucun conflit.")
		return 0
	for c in conflicts:
		print("[%s] %s\n   version de %s : %s" % (c.get("id"), c["path"], c["machine"], c.get("copy")))
	print("\nComparez chaque élément avec la version de l'autre machine, gardez la bonne, puis supprimez la "
		  "copie (ou lancez « csync conflicts --resolve <identifiant> », ou « --resolve all »).")
	return 0


def cmd_repair(paths: Paths, args) -> int:
	from . import ai
	if args.approve:
		ai.approve(paths)
		print("Réparation appliquée. La synchronisation complète reprend au prochain passage.")
		return 0
	if args.reject:
		ai.reject(paths)
		print("Proposition rejetée.")
		return 0
	if args.reset:
		ai.reset(paths)
		print("Adaptateur par défaut rétabli.")
		return 0
	print("Demande d'une proposition à Claude Code…")
	proposal, before, after = ai.propose(paths)
	print(json.dumps(proposal, indent=2, ensure_ascii=False))
	print("Vérification avec cette proposition : %s" % ("réussie" if after.ok else "échouée"))
	for d in after.details:
		print("  " + d)
	if after.ok:
		print("Pour l'appliquer : « csync repair --approve ».")
	return 0


def cmd_keygen(paths: Paths, args) -> int:
	from .crypto import generate_key_file
	generate_key_file(args.path)
	print("Clé créée : %s. Conservez-en une copie en lieu sûr." % os.path.expanduser(args.path))
	return 0


def cmd_hooks(paths: Paths, args) -> int:
	from .hooks import install_hooks, uninstall_hooks
	if args.action == "install":
		print("Hooks installés." if install_hooks(paths) else "Hooks déjà en place.")
	else:
		print("Hooks retirés." if uninstall_hooks(paths) else "Aucun hook CSync trouvé.")
	return 0


def cmd_schedule(paths: Paths, args) -> int:
	from .schedule import install_schedule, uninstall_schedule
	if args.action == "install":
		config = MachineConfig.load(paths)
		ok, message = install_schedule(paths, int(config.get("periodic_minutes", 15)))
		print(message)
		return 0 if ok else 1
	print(uninstall_schedule(paths))
	return 0


def cmd_app(paths: Paths, args) -> int:
	from .macapp import install_app
	print(install_app(paths))
	return 0


def cmd_gui(paths: Paths, args) -> int:
	from .gui import main as gui_main
	return gui_main(paths)


def cmd_check(paths: Paths, args) -> int:
	from .controller import Controller
	problems = Controller(paths).environment_check()
	if not problems:
		print("Installation vérifiée : tout est en place.")
		return 0
	for p in problems:
		print("• " + p)
	return 1


def cmd_hook(paths: Paths, args) -> int:
	"""Appelé par Claude Code. Ne doit jamais bloquer ni faire échouer la session."""
	from . import ops
	from .hooks import launch_background_push, read_hook_input
	if os.environ.get("CSYNC_INTERNAL") == "1" or not MachineConfig.exists(paths):
		return 0
	data = read_hook_input()
	try:
		config = MachineConfig.load(paths)
		if args.event == "session-start":
			source = data.get("source", "startup")
			if source not in ("startup", "resume") or not config.get("pull_on_session_start", True):
				return 0
			report = ops.do_pull(paths, current_session=data.get("session_id"), lock_timeout=5, hook_mode=True)
			notes = []
			if report.stale_current:
				notes.append("cette conversation a avancé sur %s ; pour la reprendre à jour, quittez Claude Code "
							 "puis lancez « csync resume %s »" % (report.stale_current[0], data.get("session_id")))
			if report.counts.get("sessions_conflict") or report.counts.get("files_conflict"):
				notes.append("conflits détectés (ouvrez CSync)")
			if report.counts.get("parked"):
				notes.append("%d session(s) en attente d'un projet absent ici" % report.counts["parked"])
			if notes:
				print(json.dumps({"systemMessage": "CSync : " + " ; ".join(notes) + "."}, ensure_ascii=False))
		elif args.event == "stop":
			status = ops.read_status(paths)
			last = max(status.get("last_push") or 0, status.get("last_push_attempt") or 0)
			if now() - last < float(config.get("push_debounce_seconds", 300)):
				return 0
			ops.update_status(paths, last_push_attempt=now())
			launch_background_push(paths)
		elif args.event == "session-end":
			launch_background_push(paths, wait_lock=120)
	except ops.Busy:
		LOG.info("Hook %s ignoré : une autre opération CSync est en cours.", args.event)
	except CSyncError as exc:
		LOG.error("Hook %s : %s", args.event, exc)
		if args.event == "session-start":
			print(json.dumps({"systemMessage": "CSync : %s" % exc}, ensure_ascii=False))
	except Exception as exc:  # un hook ne doit jamais casser la session
		LOG.exception("Hook %s : erreur inattendue", args.event)
	return 0


def cmd_compact(paths: Paths, args) -> int:
	from . import ops
	removed, before, after = ops.do_compact(paths)
	print("Dépôt compacté : %d blocs inutilisés supprimés ; historique local %.0f Mo → %.0f Mo. Les autres "
		  "machines se réaligneront à leur prochaine synchronisation." % (removed, before, after))
	return 0


def cmd_resume(paths: Paths, args) -> int:
	"""Récupère les nouveautés, puis ouvre « claude --resume » : la conversation reprise est à jour."""
	from . import ops
	from .util import find_claude
	claude = find_claude()
	if claude is None:
		raise CSyncError("Claude Code (commande « claude ») est introuvable.")
	try:
		report = ops.do_pull(paths, lock_timeout=60)
		for line in report.warnings:
			print("CSync : " + line, file=sys.stderr)
	except CSyncError as exc:
		print("CSync : synchronisation impossible (%s). La conversation est reprise telle qu'elle est ici." % exc,
			  file=sys.stderr)
	argv = [claude, "--resume"] + ([args.session] if args.session else [])
	sys.stdout.flush()
	sys.stderr.flush()
	os.execv(claude, argv)
	return 0


def cmd_forget_machine(paths: Paths, args) -> int:
	from . import ops
	name = ops.do_forget_machine(paths, args.machine, force=args.force)
	print("Machine « %s » retirée du dépôt. Ses contenus inutilisés partiront au prochain compactage." % name)
	return 0


def cmd_local(paths: Paths, args) -> int:
	"""Réglages et serveurs MCP propres à cette machine."""
	config = MachineConfig.load(paths)
	field = {"settings": "local_settings_keys", "mcp": "local_mcp_servers"}
	if args.kind:
		if not args.name:
			raise CSyncError("Indiquez la clé de settings.json ou le nom du serveur MCP.")
		values = [v for v in (config.get(field[args.kind]) or []) if isinstance(v, str)]
		if args.remove:
			values = [v for v in values if v != args.name]
		elif args.name not in values:
			values.append(args.name)
		config.data[field[args.kind]] = sorted(values)
		config.save(paths)
	print("Clés de settings.json propres à cette machine : %s"
		  % (", ".join(config.get("local_settings_keys") or []) or "aucune"))
	print("Serveurs MCP propres à cette machine : %s" % (", ".join(config.get("local_mcp_servers") or []) or "aucun"))
	return 0


def cmd_periodic(paths: Paths, args) -> int:
	from . import ops
	from .schedule import on_battery
	if not MachineConfig.exists(paths):
		return 0
	config = MachineConfig.load(paths)
	if config.get("skip_on_battery", True) and on_battery():
		LOG.info("Vérification périodique ignorée : machine sur batterie.")
		return 0
	try:
		ops.do_sync(paths, background=True)
	except ops.Busy:
		LOG.info("Vérification périodique ignorée : une autre opération est en cours.")
		return 0
	except CSyncError as exc:
		LOG.error("Vérification périodique : %s", exc)
		return 1
	# Après une suppression (envoyée ou reçue), le dépôt est compacté pour que le contenu supprimé
	# disparaisse aussi de l'historique de GitHub.
	from .state import State
	status = ops.read_status(paths)
	try:
		due = float(State.load(paths).get("compact_due") or 0)
	except CSyncError:
		due = 0
	if due and due > float(status.get("last_compact") or 0):
		try:
			ops.update_status(paths, last_compact_attempt=now())
			ops.do_compact(paths, lock_timeout=0, background=True)
			LOG.info("Compactage après suppression effectué.")
		except ops.Busy:
			pass
		except CSyncError as exc:
			LOG.error("Compactage après suppression : %s", exc)
		return 0
	# Compactage automatique, au plus une fois par semaine, quand le dépôt devient volumineux.
	limit = float(config.get("auto_compact_mb", 300))
	if limit > 0 and now() - float(status.get("last_compact_attempt") or 0) > 7 * 86400:
		try:
			if ops.repo_size_mb(paths) > limit:
				ops.update_status(paths, last_compact_attempt=now())
				removed, before, after = ops.do_compact(paths, lock_timeout=0, background=True)
				LOG.warning("Compactage automatique : %.0f Mo → %.0f Mo.", before, after)
		except ops.Busy:
			pass
		except CSyncError as exc:
			LOG.error("Compactage automatique : %s", exc)
	return 0


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(prog="csync", description="Synchronise Claude Code entre vos machines via GitHub.")
	parser.add_argument("--version", action="version", version="CSync " + __version__)
	parser.add_argument("-v", "--verbose", action="store_true", help="affiche le détail des opérations")
	sub = parser.add_subparsers(dest="command", metavar="commande")

	p = sub.add_parser("setup", help="configure cette machine")
	p.add_argument("--repo", help="adresse du dépôt de données")
	p.add_argument("--key", help="chemin du fichier de clé")
	p.add_argument("--create-key", action="store_true", help="crée la clé à ce chemin")
	p.add_argument("--name", help="nom de la machine")
	p.add_argument("--yes", action="store_true", help="accepte les réponses par défaut")
	p.add_argument("--no-hooks", action="store_true")
	p.add_argument("--no-schedule", action="store_true")
	p.add_argument("--no-app", action="store_true")
	p.add_argument("--prefer-remote", action="store_true", help="en cas de différence, adopte la version de l'autre machine")
	p.set_defaults(func=cmd_setup)

	for name, func, helptext in (("sync", cmd_sync, "réception puis envoi"),
								 ("push", cmd_push, "envoie les changements de cette machine"),
								 ("pull", cmd_pull, "reçoit les changements des autres machines")):
		p = sub.add_parser(name, help=helptext)
		p.add_argument("--dry-run", action="store_true", help="montre ce qui serait fait, sans rien modifier")
		p.add_argument("--quiet", action="store_true")
		p.add_argument("--wait-lock", type=float, default=0, help="attend jusqu'à N secondes si une opération est en cours")
		if name in ("push", "sync"):
			p.add_argument("--allow-deletions", action="store_true",
						   help="envoie aussi une disparition massive de fichiers (si elle est voulue)")
		if name == "push":
			p.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
		if name == "pull":
			p.add_argument("--prefer-remote", action="store_true")
		p.set_defaults(func=func)

	p = sub.add_parser("status", help="état de la synchronisation")
	p.add_argument("--json", action="store_true")
	p.set_defaults(func=cmd_status)

	p = sub.add_parser("scan", help="rescanne la machine et met à jour son profil")
	p.add_argument("--yes", action="store_true")
	p.set_defaults(func=cmd_scan)

	p = sub.add_parser("name-project", help="partage un dossier sans dépôt git sous un nom commun")
	p.add_argument("path")
	p.add_argument("name")
	p.set_defaults(func=cmd_name_project)

	sub.add_parser("profile", help="affiche le profil de la machine").set_defaults(func=cmd_profile)

	p = sub.add_parser("conflicts", help="liste ou résout les conflits")
	p.add_argument("--resolve", metavar="ID|all",
				   help="marque des conflits comme résolus (identifiants séparés par des virgules, ou all)")
	p.set_defaults(func=cmd_conflicts)

	p = sub.add_parser("repair", help="réparation du format assistée par IA")
	group = p.add_mutually_exclusive_group()
	group.add_argument("--approve", action="store_true")
	group.add_argument("--reject", action="store_true")
	group.add_argument("--reset", action="store_true")
	p.set_defaults(func=cmd_repair)

	p = sub.add_parser("keygen", help="crée un fichier de clé")
	p.add_argument("path")
	p.set_defaults(func=cmd_keygen)

	p = sub.add_parser("hooks", help="installe ou retire les hooks Claude Code")
	p.add_argument("action", choices=["install", "uninstall"])
	p.set_defaults(func=cmd_hooks)

	p = sub.add_parser("schedule", help="installe ou retire la vérification périodique")
	p.add_argument("action", choices=["install", "uninstall"])
	p.set_defaults(func=cmd_schedule)

	sub.add_parser("compact", help="réduit la taille du dépôt de données").set_defaults(func=cmd_compact)

	p = sub.add_parser("forget-machine", help="retire du dépôt une machine qui n'existe plus")
	p.add_argument("machine", help="nom ou identifiant de la machine (voir « csync status »)")
	p.add_argument("--force", action="store_true", help="même si elle a envoyé des données récemment")
	p.set_defaults(func=cmd_forget_machine)

	p = sub.add_parser("resume", help="met à jour puis reprend une conversation (claude --resume)")
	p.add_argument("session", nargs="?", help="identifiant de la conversation (sinon, liste à choisir)")
	p.set_defaults(func=cmd_resume)

	p = sub.add_parser("local", help="réglages et serveurs MCP propres à cette machine")
	p.add_argument("kind", nargs="?", choices=["settings", "mcp"])
	p.add_argument("name", nargs="?", help="clé de settings.json (ex. hooks) ou nom du serveur MCP")
	p.add_argument("--remove", action="store_true", help="la synchronise de nouveau")
	p.set_defaults(func=cmd_local)
	sub.add_parser("app", help="installe l'application CSync (macOS)").set_defaults(func=cmd_app)
	sub.add_parser("gui", help="ouvre la fenêtre CSync").set_defaults(func=cmd_gui)
	sub.add_parser("check", help="vérifie l'installation").set_defaults(func=cmd_check)

	p = sub.add_parser("hook")
	p.add_argument("event", choices=["session-start", "stop", "session-end"])
	p.set_defaults(func=cmd_hook)

	sub.add_parser("periodic").set_defaults(func=cmd_periodic)
	return parser


def main(argv: Optional[List[str]] = None) -> int:
	parser = build_parser()
	args = parser.parse_args(argv)
	paths = Paths()
	if MachineConfig.exists(paths):
		try:
			config = MachineConfig.load(paths)
			paths = paths.with_config(config)
			if config.get("claude_path") and not os.environ.get("CSYNC_CLAUDE"):
				os.environ["CSYNC_CLAUDE"] = config.get("claude_path")
		except CSyncError:
			pass
	quiet_command = args.command in ("hook", "periodic") or getattr(args, "background", False)
	setup_logging(paths.log_file, verbose=args.verbose, to_stderr=not quiet_command)
	if not getattr(args, "command", None):
		parser.print_help()
		return 0
	try:
		return int(args.func(paths, args) or 0)
	except CSyncError as exc:
		if quiet_command:
			LOG.error("%s", exc)
			return 0 if args.command == "hook" else 1
		print("Erreur : %s" % exc, file=sys.stderr)
		return 1
	except KeyboardInterrupt:
		print("\nInterrompu.", file=sys.stderr)
		return 130
