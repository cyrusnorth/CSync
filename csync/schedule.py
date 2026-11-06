"""Vérification périodique : launchd sur macOS, timer systemd (ou cron) sur Linux.

Le planificateur lance CSync quelques secondes toutes les 15 minutes, puis CSync se
termine. Aucun processus ne reste en mémoire, et rien n'empêche la mise en veille.
"""

from __future__ import annotations

import os
import re
from typing import Optional, Tuple

from .paths import Paths, system_name
from .util import LOG, atomic_write_text, ensure_dir, remove_file, run, which

LABEL = "com.csync.periodic"


def _plist_path(paths: Paths) -> str:
	return os.path.join(paths.home, "Library", "LaunchAgents", LABEL + ".plist")


def _xml_escape(text: str) -> str:
	return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plist(paths: Paths, minutes: int) -> str:
	log = _xml_escape(os.path.join(paths.logs_dir, "periodic.log"))
	return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>%s</string>
	<key>ProgramArguments</key>
	<array>
		<string>%s</string>
		<string>periodic</string>
	</array>
	<key>StartInterval</key>
	<integer>%d</integer>
	<key>RunAtLoad</key>
	<false/>
	<key>ProcessType</key>
	<string>Background</string>
	<key>LowPriorityIO</key>
	<true/>
	<key>Nice</key>
	<integer>10</integer>
	<key>StandardOutPath</key>
	<string>%s</string>
	<key>StandardErrorPath</key>
	<string>%s</string>
</dict>
</plist>
""" % (LABEL, _xml_escape(paths.bin_path), minutes * 60, log, log)


def _systemd_dir(paths: Paths) -> str:
	base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(paths.home, ".config")
	return os.path.join(base, "systemd", "user")


def install_schedule(paths: Paths, minutes: int = 15) -> Tuple[bool, str]:
	"""Installe la vérification périodique. Renvoie (succès, message)."""
	ensure_dir(paths.logs_dir)
	system = system_name()
	if system == "darwin":
		plist = _plist_path(paths)
		ensure_dir(os.path.dirname(plist))
		atomic_write_text(plist, _plist(paths, minutes), 0o644)
		domain = "gui/%d" % os.getuid()
		run(["launchctl", "bootout", domain + "/" + LABEL], timeout=20)
		result = run(["launchctl", "bootstrap", domain, plist], timeout=20)
		if not result.ok:
			legacy = run(["launchctl", "load", "-w", plist], timeout=20)
			if not legacy.ok:
				return False, "launchd a refusé la tâche : %s" % (result.err.strip() or legacy.err.strip())
		return True, "Vérification périodique installée (launchd, toutes les %d minutes)." % minutes
	if system == "linux":
		if which("systemctl") and run(["systemctl", "--user", "show-environment"], timeout=10).ok:
			unit_dir = _systemd_dir(paths)
			ensure_dir(unit_dir)
			atomic_write_text(os.path.join(unit_dir, "csync.service"),
							  "[Unit]\nDescription=CSync : vérification périodique\n\n[Service]\nType=oneshot\n"
							  "ExecStart=%s periodic\nNice=10\nIOSchedulingClass=idle\n" % paths.bin_path, 0o644)
			atomic_write_text(os.path.join(unit_dir, "csync.timer"),
							  "[Unit]\nDescription=CSync : toutes les %d minutes\n\n[Timer]\nOnBootSec=5min\n"
							  "OnUnitActiveSec=%dmin\nPersistent=false\n\n[Install]\nWantedBy=timers.target\n"
							  % (minutes, minutes), 0o644)
			run(["systemctl", "--user", "daemon-reload"], timeout=30)
			result = run(["systemctl", "--user", "enable", "--now", "csync.timer"], timeout=30)
			if result.ok:
				message = "Vérification périodique installée (timer systemd, toutes les %d minutes)." % minutes
				linger = run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"], timeout=10)
				if linger.ok and "Linger=no" in linger.out:
					message += (" Pour qu'elle tourne aussi sans session ouverte sur le serveur : "
								"« sudo loginctl enable-linger %s »." % os.environ.get("USER", "$USER"))
				return True, message
			LOG.warning("systemd utilisateur indisponible (%s), repli sur cron.", result.err.strip())
		return _install_cron(paths, minutes)
	return False, "Système non pris en charge pour la planification : %s" % system


def _cron_line(paths: Paths, minutes: int) -> str:
	return "*/%d * * * * %s periodic >/dev/null 2>&1 # csync" % (minutes, paths.bin_path)


def _install_cron(paths: Paths, minutes: int) -> Tuple[bool, str]:
	if not which("crontab"):
		return False, "Ni systemd utilisateur ni cron ne sont disponibles : planification non installée."
	current = run(["crontab", "-l"], timeout=10)
	lines = [l for l in (current.out.splitlines() if current.ok else []) if not l.rstrip().endswith("# csync")]
	lines.append(_cron_line(paths, minutes))
	result = run(["crontab", "-"], timeout=10, input_text="\n".join(lines) + "\n")
	if not result.ok:
		return False, "Impossible d'installer la tâche cron : %s" % result.err.strip()
	return True, "Vérification périodique installée (cron, toutes les %d minutes)." % minutes


def uninstall_schedule(paths: Paths) -> str:
	system = system_name()
	if system == "darwin":
		run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)], timeout=20)
		remove_file(_plist_path(paths))
		return "Tâche launchd supprimée."
	messages = []
	if which("systemctl"):
		run(["systemctl", "--user", "disable", "--now", "csync.timer"], timeout=30)
		unit_dir = _systemd_dir(paths)
		if remove_file(os.path.join(unit_dir, "csync.timer")) | remove_file(os.path.join(unit_dir, "csync.service")):
			run(["systemctl", "--user", "daemon-reload"], timeout=30)
			messages.append("Timer systemd supprimé.")
	if which("crontab"):
		current = run(["crontab", "-l"], timeout=10)
		if current.ok and "# csync" in current.out:
			lines = [l for l in current.out.splitlines() if not l.rstrip().endswith("# csync")]
			run(["crontab", "-"], timeout=10, input_text="\n".join(lines) + "\n")
			messages.append("Tâche cron supprimée.")
	return " ".join(messages) or "Aucune planification trouvée."


def schedule_installed(paths: Paths) -> Optional[bool]:
	system = system_name()
	if system == "darwin":
		if not os.path.exists(_plist_path(paths)):
			return False
		return run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), LABEL)], timeout=10).ok
	if system == "linux":
		if which("systemctl"):
			result = run(["systemctl", "--user", "is-enabled", "csync.timer"], timeout=10)
			if result.ok and "enabled" in result.out:
				return True
		if which("crontab"):
			current = run(["crontab", "-l"], timeout=10)
			if current.ok and "# csync" in current.out:
				return True
		return False
	return None


def on_battery() -> bool:
	"""Vrai si la machine est sur batterie ou en mode Économie d'énergie (au mieux de ce qu'on sait lire)."""
	system = system_name()
	if system == "darwin":
		batt = run(["pmset", "-g", "batt"], timeout=10)
		if batt.ok and "Battery Power" in batt.out:
			return True
		settings = run(["pmset", "-g"], timeout=10)
		if settings.ok and re.search(r"lowpowermode\s+1", settings.out):
			return True
		return False
	if system == "linux":
		base = "/sys/class/power_supply"
		try:
			names = os.listdir(base)
		except OSError:
			return False
		saw_ac = False
		for name in names:
			try:
				with open(os.path.join(base, name, "type")) as fh:
					kind = fh.read().strip()
				if kind == "Mains":
					saw_ac = True
					with open(os.path.join(base, name, "online")) as fh:
						if fh.read().strip() == "1":
							return False
			except OSError:
				continue
		return saw_ac
	return False
