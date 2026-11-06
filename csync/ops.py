"""Opérations de haut niveau, partagées par la ligne de commande et la fenêtre."""

from __future__ import annotations

import os
import traceback
from typing import Any, Dict, List, Optional

from .paths import MachineConfig, Paths
from .sync import Engine, Report
from .util import CSyncError, FileLock, LOG, now, read_json, write_json


def status_path(paths: Paths) -> str:
	return os.path.join(paths.csync_dir, "status.json")


def read_status(paths: Paths) -> Dict[str, Any]:
	try:
		data = read_json(status_path(paths), {})
	except CSyncError:
		data = {}
	return data if isinstance(data, dict) else {}


def update_status(paths: Paths, **fields: Any) -> None:
	data = read_status(paths)
	data.update(fields)
	try:
		write_json(status_path(paths), data, 0o600)
	except OSError as exc:
		LOG.error("Écriture de status.json impossible : %s", exc)


class Busy(CSyncError):
	pass


def run_locked(paths: Paths, action: str, func, lock_timeout: float = 0.0):
	"""Exécute une opération sous verrou et consigne le résultat (succès ou erreur) dans status.json."""
	lock = FileLock(paths.lock_file)
	if not lock.acquire(timeout=lock_timeout):
		raise Busy("Une autre opération CSync est déjà en cours.")
	try:
		result = func()
		update_status(paths, last_error=None, last_error_ts=None, last_ok_action=action, last_ok_ts=now())
		return result
	except CSyncError as exc:
		update_status(paths, last_error=str(exc), last_error_ts=now(), last_error_action=action)
		raise
	except Exception as exc:  # erreur inattendue : consignée avec sa trace dans le journal
		LOG.error("Erreur inattendue pendant « %s » :\n%s", action, traceback.format_exc())
		update_status(paths, last_error="Erreur inattendue : %s" % exc, last_error_ts=now(), last_error_action=action)
		raise CSyncError("Erreur inattendue : %s (détails dans %s)" % (exc, paths.log_file))
	finally:
		lock.release()


def check_binding(paths: Paths, config: MachineConfig) -> None:
	"""Refuse de travailler avec une configuration copiée depuis un autre ordinateur."""
	from .paths import machine_binding
	current = machine_binding(paths)
	if current is None:
		return   # identifiant de l'ordinateur illisible : rien à comparer
	stored = config.get("binding")
	if not stored:
		config.data["binding"] = current
		config.save(paths)
		return
	if stored != current:
		raise CSyncError("La configuration de CSync de cet ordinateur (%s) semble copiée depuis une autre machine "
						 "(sauvegarde restaurée, migration). Rien n'a été synchronisé. Lancez « csync setup » pour "
						 "donner à cette machine sa propre identité." % paths.csync_dir)


def engine(paths: Paths, hook_mode: bool = False, background: bool = False) -> Engine:
	config = MachineConfig.load(paths)
	check_binding(paths, config)
	return Engine(paths.with_config(config), config, hook_mode=hook_mode, background=background)


def do_push(paths: Paths, dry_run: bool = False, lock_timeout: float = 0.0, allow_deletions: bool = False,
			hook_mode: bool = False) -> Report:
	def go():
		update_status(paths, last_push_attempt=now())
		report = engine(paths, hook_mode).push(dry_run=dry_run, allow_deletions=allow_deletions)
		if not dry_run:
			update_status(paths, last_push=now())
		return report
	return run_locked(paths, "push", go, lock_timeout)


def do_pull(paths: Paths, dry_run: bool = False, current_session: Optional[str] = None,
			prefer_remote: bool = False, lock_timeout: float = 0.0, hook_mode: bool = False) -> Report:
	def go():
		report = engine(paths, hook_mode).pull(dry_run=dry_run, current_session=current_session,
											   prefer_remote=prefer_remote)
		if not dry_run:
			update_status(paths, last_pull=now())
		return report
	return run_locked(paths, "pull", go, lock_timeout)


def do_sync(paths: Paths, dry_run: bool = False, current_session: Optional[str] = None,
			prefer_remote: bool = False, lock_timeout: float = 0.0, allow_deletions: bool = False,
			background: bool = False) -> List[Report]:
	def go():
		update_status(paths, last_push_attempt=now())
		reports = engine(paths, background=background).sync(dry_run=dry_run, current_session=current_session, prefer_remote=prefer_remote,
									 allow_deletions=allow_deletions)
		if not dry_run:
			update_status(paths, last_pull=now(), last_push=now())
		return reports
	return run_locked(paths, "sync", go, lock_timeout)


def do_compact(paths: Paths, lock_timeout: float = 30.0, background: bool = False):
	def go():
		started = now()
		result = engine(paths, background=background).compact()
		update_status(paths, last_compact=started)
		return result
	return run_locked(paths, "compact", go, lock_timeout)


def do_forget_machine(paths: Paths, ident: str, lock_timeout: float = 30.0, force: bool = False) -> str:
	"""Retire du dépôt une machine qui n'existe plus (réinstallée, remplacée). Renvoie son nom."""
	def go():
		eng = engine(paths)
		eng._check_repo()
		eng.store.fetch_and_rebase()
		machines = {}
		for machine in eng.store.list_machines():
			try:
				doc = eng.store.read_manifest(machine)
			except (CSyncError, ValueError):
				doc = None
			machines[machine] = (doc[0].get("machine_name") if doc else None) or machine
		for machine, prof in eng.store.other_profiles().items():
			machines.setdefault(machine, prof.get("machine_name") or machine)
		matches = [m for m, name in machines.items() if ident in (m, name)]
		if not matches:
			raise CSyncError("Aucune machine « %s » dans le dépôt. Machines connues : %s."
							 % (ident, ", ".join("%s (%s)" % (n, m) for m, n in sorted(machines.items()))))
		if len(matches) > 1:
			raise CSyncError("Plusieurs machines s'appellent « %s » : désignez-la par son identifiant (%s)."
							 % (ident, ", ".join(matches)))
		machine = matches[0]
		if machine == eng.config.machine_id:
			raise CSyncError("C'est la machine sur laquelle vous êtes : elle ne peut pas être retirée d'ici.")
		try:
			doc = eng.store.read_manifest(machine)
		except (CSyncError, ValueError):
			doc = None
		updated = float((doc[0] if doc else {}).get("updated") or 0)
		if not force and now() - updated < 7 * 86400:
			raise CSyncError("La machine « %s » a envoyé des données il y a moins d'une semaine : elle semble encore "
							 "en service (elle se réinscrirait d'ailleurs à sa prochaine synchronisation). Si elle "
							 "n'existe vraiment plus, relancez avec --force." % machines[machine])
		eng.store.forget_machine(machine)
		eng.store.commit_and_push()
		return machines[machine]
	return run_locked(paths, "forget-machine", go, lock_timeout)


def repo_size_mb(paths: Paths) -> float:
	from .store import Store
	return Store(paths.repo_dir, None, "").size_mb()
