"""Tests de bout en bout : deux machines simulées (un « Mac » et un « serveur Linux ») et un dépôt git local
qui joue le rôle de GitHub.

Lancement : python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ["CSYNC_ASSUME_CLAUDE_RUNNING"] = "0"
os.environ.pop("CLAUDE_CONFIG_DIR", None)
for _var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
	os.environ.pop(_var, None)
os.environ.setdefault("GIT_AUTHOR_NAME", "test")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
os.environ.setdefault("GIT_COMMITTER_NAME", "test")
os.environ.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")

import csync.sync  # noqa: E402
from csync import ops  # noqa: E402
from csync.crypto import Cipher, generate_key_file, load_key  # noqa: E402
from csync.paths import MachineConfig, Paths  # noqa: E402
from csync.setupwizard import run_setup  # noqa: E402
from csync.state import State  # noqa: E402
from csync.sync import CHUNK_MAX_BYTES, Engine, chunk_lines  # noqa: E402
from csync.tokens import PathMapper, slug  # noqa: E402
from csync.util import CSyncError, read_json  # noqa: E402

REMOTE_URL = "git@github.com:Alex/Monprojet.git"


def git(*args, cwd=None):
	subprocess.run(["git"] + list(args), cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write(path, text, mode=None):
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w", encoding="utf-8") as fh:
		fh.write(text)
	if mode:
		os.chmod(path, mode)


def read_bytes_of(path):
	with open(path, "rb") as fh:
		return fh.read()


def jsonl(lines):
	return "".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines)


class Machine:
	def __init__(self, root, name, home_rel, project_rel):
		self.name = name
		self.home = os.path.join(root, name, home_rel)
		self.paths = Paths(csync_dir=os.path.join(self.home, ".csync"), home=self.home,
						   claude_dir=os.path.join(self.home, ".claude"),
						   claude_json=os.path.join(self.home, ".claude.json"))
		self.project = os.path.join(self.home, project_rel)
		os.makedirs(self.project, exist_ok=True)
		git("init", "-q", cwd=self.project)
		git("remote", "add", "origin", REMOTE_URL, cwd=self.project)
		os.makedirs(self.paths.claude_dir, exist_ok=True)

	def session_dir(self, cwd):
		return os.path.join(self.paths.projects_dir, slug(cwd))

	def session_path(self, cwd, sid):
		return os.path.join(self.session_dir(cwd), sid + ".jsonl")

	def setup(self, repo_url, key_path, create_key=False, prefer_remote=False, hooks=False):
		out = io.StringIO()
		with redirect_stdout(out):
			run_setup(self.paths, {"yes": True, "name": self.name, "repo": repo_url, "key": key_path,
								   "create_key": create_key, "no_hooks": not hooks, "no_schedule": True,
								   "no_app": True, "prefer_remote": prefer_remote, "prefer_remote_set": True})
		config = MachineConfig.load(self.paths)
		config.data["active_session_grace_seconds"] = 0
		config.save(self.paths)
		return out.getvalue()

	def push(self, **kw):
		return ops.do_push(self.paths, **kw)

	def push_twice(self, **kw):
		"""Une disparition n'est publiée comme suppression qu'au second passage."""
		ops.do_push(self.paths, **kw)
		return ops.do_push(self.paths, **kw)

	def pull(self, **kw):
		return ops.do_pull(self.paths, **kw)

	def sync(self, **kw):
		return ops.do_sync(self.paths, **kw)

	def state(self):
		return State.load(self.paths)


class Harness(unittest.TestCase):
	"""Deux machines simulées et un faux GitHub ; aucun test ici."""

	def setUp(self):
		self.root = tempfile.mkdtemp(prefix="csync-test-")
		self.remote = os.path.join(self.root, "remote.git")
		git("init", "-q", "--bare", "-b", "main", self.remote)
		self.key = os.path.join(self.root, "csync.key")
		self.mac = Machine(self.root, "mac", "Users/alex", "dev/monprojet")
		self.linux = Machine(self.root, "linux", "home/alex", "code/monprojet")
		self.sid = str(uuid.uuid4())
		self._populate_mac()
		# Claude Code déjà lancé une fois sur le serveur (le fichier existe, sans serveur MCP).
		write(self.linux.paths.claude_json, json.dumps({"numStartups": 1}))
		self._grace = csync.sync.DELETE_GRACE_SECONDS
		csync.sync.DELETE_GRACE_SECONDS = 0

	def tearDown(self):
		csync.sync.DELETE_GRACE_SECONDS = self._grace
		shutil.rmtree(self.root, ignore_errors=True)

	# -- données de départ ----------------------------------------------------------

	def _populate_mac(self):
		m = self.mac
		lines = [
			{"type": "user", "sessionId": self.sid, "cwd": m.project, "uuid": "u1",
			 "message": {"role": "user", "content": "Lis %s/src/app.py" % m.project}},
			{"type": "assistant", "sessionId": self.sid, "cwd": m.project, "uuid": "u2", "parentUuid": "u1",
			 "message": {"role": "assistant", "content": [{"type": "text", "text": "Fichier %s/src/app.py lu ; voir aussi %s/notes.txt"
																				% (m.project, m.home)}]}},
			{"type": "user", "sessionId": self.sid, "cwd": m.project, "uuid": "u3", "parentUuid": "u2",
			 "toolUseResult": {"filePath": m.project + "/src/app.py", "transcript": m.paths.claude_dir + "/projects/" + slug(m.project) + "/x.jsonl"}},
		]
		write(m.session_path(m.project, self.sid), jsonl(lines))
		self.notes = os.path.join(m.home, "Documents", "notes")
		os.makedirs(self.notes)
		self.sid2 = str(uuid.uuid4())
		write(m.session_path(self.notes, self.sid2), jsonl([
			{"type": "user", "sessionId": self.sid2, "cwd": self.notes, "message": {"content": "bonjour"}}]))
		write(os.path.join(m.session_dir(m.project), "memory", "MEMORY.md"), "# Mémoire\n- tests : npm test\n")
		write(os.path.join(m.paths.claude_dir, "CLAUDE.md"), "Réponds en français.\n")
		write(m.paths.settings_file, json.dumps({
			"permissions": {"allow": ["Read(%s/**)" % m.project], "additionalDirectories": [m.home + "/shared"]},
			"enabledPlugins": {"formatter@market": True},
		}, indent=2))
		write(os.path.join(m.paths.claude_dir, "skills", "reel", "SKILL.md"), "---\ndescription: test\n---\nCorps\n")
		write(os.path.join(m.paths.claude_dir, "skills", "reel", "scripts", "run.sh"), "#!/bin/sh\necho ok\n", 0o755)
		write(os.path.join(m.paths.claude_dir, "skills", "synced", "pdf", "SKILL.md"), "synced from claude.ai\n")
		write(os.path.join(m.paths.claude_dir, "agents", "relecteur.md"), "agent\n")
		write(os.path.join(m.paths.claude_dir, ".credentials.json"), '{"secret": "ne pas envoyer"}')
		write(m.paths.claude_json, json.dumps({
			"oauthAccount": {"email": "x@example.com"},
			"projects": {m.project: {"hasTrustDialogAccepted": True}},
			"mcpServers": {"fs": {"command": "npx", "args": ["-y", "fs-server", m.home + "/data"]}},
		}, indent=2))

	def _setup_both(self, prefer_remote=False, hooks=False):
		self.mac.setup(self.remote, self.key, create_key=True, hooks=hooks)
		shutil.copy(self.key, self.key + ".copy")
		self.linux.setup(self.remote, self.key, prefer_remote=prefer_remote, hooks=hooks)

	def _read_lines(self, path):
		with open(path, encoding="utf-8") as fh:
			return [json.loads(l) for l in fh if l.strip()]

	def _remote_head(self):
		return subprocess.run(["git", "--git-dir", self.remote, "rev-parse", "main"], capture_output=True, text=True).stdout

	def _cli(self, machine, args, stdin="", extra_env=None):
		env = dict(os.environ)
		env["HOME"] = machine.home
		env.pop("CLAUDE_CONFIG_DIR", None)
		env["PYTHONPATH"] = os.pathsep.join([ROOT] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else []))
		env.update(extra_env or {})
		return subprocess.run([sys.executable, "-m", "csync"] + args, input=stdin, capture_output=True,
							  text=True, env=env, timeout=120)

	def _lock_free(self, machine):
		from csync.util import FileLock
		lock = FileLock(machine.paths.lock_file)
		if lock.acquire(timeout=0):
			lock.release()
			return True
		return False


class TwoMachines(Harness):
	# -- tests -------------------------------------------------------------------------

	def test_full_roundtrip(self):
		self._setup_both()
		lx, m = self.linux, self.mac
		# Session du projet git : installée dans le dossier correspondant au chemin Linux.
		target = lx.session_path(lx.project, self.sid)
		self.assertTrue(os.path.exists(target), "session non installée sur Linux")
		lines = self._read_lines(target)
		self.assertEqual(lines[0]["cwd"], lx.project)
		self.assertIn(lx.project + "/src/app.py", lines[0]["message"]["content"])
		self.assertIn(lx.home + "/notes.txt", lines[1]["message"]["content"][0]["text"])
		self.assertEqual(lines[2]["toolUseResult"]["transcript"],
						 lx.paths.claude_dir + "/projects/" + slug(lx.project) + "/x.jsonl")
		# Session dans un dossier non git du dossier personnel.
		notes_linux = os.path.join(lx.home, "Documents", "notes")
		self.assertTrue(os.path.exists(lx.session_path(notes_linux, self.sid2)))
		# Mémoire du projet.
		self.assertTrue(os.path.exists(os.path.join(lx.session_dir(lx.project), "memory", "MEMORY.md")))
		# Configuration.
		with open(os.path.join(lx.paths.claude_dir, "CLAUDE.md")) as fh:
			self.assertEqual(fh.read(), "Réponds en français.\n")
		settings = read_json(lx.paths.settings_file)
		self.assertEqual(settings["permissions"]["allow"], ["Read(%s/**)" % lx.project])
		self.assertEqual(settings["permissions"]["additionalDirectories"], [lx.home + "/shared"])
		self.assertEqual(settings["enabledPlugins"], {"formatter@market": True})
		run_sh = os.path.join(lx.paths.claude_dir, "skills", "reel", "scripts", "run.sh")
		self.assertTrue(os.access(run_sh, os.X_OK), "bit exécutable perdu")
		self.assertFalse(os.path.exists(os.path.join(lx.paths.claude_dir, "skills", "synced")),
						 "les skills synchronisés par claude.ai ne doivent pas être copiés")
		self.assertFalse(os.path.exists(os.path.join(lx.paths.claude_dir, ".credentials.json")))
		# MCP fusionné sans toucher au reste de ~/.claude.json.
		cj = read_json(lx.paths.claude_json)
		self.assertEqual(cj["mcpServers"]["fs"]["args"][-1], lx.home + "/data")
		self.assertNotIn("oauthAccount", cj)
		# Rien en clair dans le dépôt.
		self._assert_no_plaintext_leak()
		# Une seconde synchronisation sans changement n'envoie rien.
		before = self._remote_head()
		lx.sync()
		m.sync()
		self.assertEqual(before, self._remote_head(), "commit inutile alors que rien n'a changé")

	def _assert_no_plaintext_leak(self):
		clone = os.path.join(self.root, "inspect")
		shutil.rmtree(clone, ignore_errors=True)
		git("clone", "-q", self.remote, clone)
		needles = [b"alex", b"Monprojet", b"monprojet", b"fran\xc3\xa7ais", b"bonjour", b"npx", b"formatter"]
		for dirpath, dirnames, filenames in os.walk(clone):
			if ".git" in dirpath.split(os.sep):
				continue
			for f in filenames:
				with open(os.path.join(dirpath, f), "rb") as fh:
					data = fh.read()
				for n in needles:
					self.assertNotIn(n, data, "texte en clair trouvé dans %s" % f)
				if f not in ("README.md", "csync-format.json", ".gitignore", "generation.json"):
					self.assertTrue(data.startswith(b"CSY1"), "fichier non chiffré : %s" % f)
		# Ni les messages ni les auteurs des commits ne contiennent d'information.
		log = subprocess.run(["git", "--git-dir", self.remote, "log", "--format=%an|%ae|%cn|%ce|%s"],
							 capture_output=True, text=True).stdout.splitlines()
		self.assertTrue(log)
		self.assertEqual(set(log), {"csync|csync@localhost|csync|csync@localhost|csync"})
		zones = subprocess.run(["git", "--git-dir", self.remote, "log", "--format=%ad %cd", "--date=format:%z"],
							   capture_output=True, text=True).stdout.split()
		self.assertEqual(set(zones), {"+0000"}, "fuseau horaire visible dans les commits")

	def test_append_and_extend_both_ways(self):
		self._setup_both()
		lx, m = self.linux, self.mac
		target = lx.session_path(lx.project, self.sid)
		with open(target, "a", encoding="utf-8") as fh:
			fh.write(json.dumps({"type": "user", "sessionId": self.sid, "cwd": lx.project, "uuid": "l1",
								 "message": {"content": "suite sur Linux dans %s/lib" % lx.project}}) + "\n")
		lx.push()
		report = m.pull()
		self.assertEqual(report.counts.get("sessions_extended"), 1)
		mac_lines = self._read_lines(m.session_path(m.project, self.sid))
		self.assertEqual(len(mac_lines), 4)
		self.assertEqual(mac_lines[-1]["message"]["content"], "suite sur Linux dans %s/lib" % m.project)
		self.assertEqual(mac_lines[0]["cwd"], m.project)
		# Et dans l'autre sens.
		with open(m.session_path(m.project, self.sid), "a", encoding="utf-8") as fh:
			fh.write(json.dumps({"type": "assistant", "sessionId": self.sid, "uuid": "m5"}) + "\n")
		m.push()
		lx.pull()
		self.assertEqual(len(self._read_lines(target)), 5)
		# Pas de conflit, pas d'envoi superflu.
		self.assertEqual(m.state().conflicts, [])
		self.assertEqual(lx.state().conflicts, [])

	def test_partial_last_line_is_ignored(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		with open(m.session_path(m.project, self.sid), "a", encoding="utf-8") as fh:
			fh.write('{"type": "user", "uuid": "complete"}\n{"type": "user", "uuid": "en cours d\'écri')
		m.push()
		lx.pull()
		lines = self._read_lines(lx.session_path(lx.project, self.sid))
		self.assertEqual(lines[-1]["uuid"], "complete")

	def test_divergence_creates_resumable_copy(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		with open(m.session_path(m.project, self.sid), "a", encoding="utf-8") as fh:
			fh.write(json.dumps({"type": "user", "sessionId": self.sid, "uuid": "mac-only"}) + "\n")
		with open(lx.session_path(lx.project, self.sid), "a", encoding="utf-8") as fh:
			fh.write(json.dumps({"type": "user", "sessionId": self.sid, "uuid": "linux-only"}) + "\n")
		m.push()
		report = lx.sync()[0]
		self.assertEqual(report.counts.get("sessions_conflict"), 1)
		local = self._read_lines(lx.session_path(lx.project, self.sid))
		self.assertEqual(local[-1]["uuid"], "linux-only", "la version locale doit être gardée")
		conflicts = lx.state().conflicts
		self.assertEqual(len(conflicts), 1)
		copy = conflicts[0]["copy"]
		copy_lines = self._read_lines(copy)
		self.assertEqual(copy_lines[-1]["uuid"], "mac-only")
		new_id = os.path.basename(copy)[:-len(".jsonl")]
		self.assertNotEqual(new_id, self.sid)
		self.assertTrue(all(l.get("sessionId") in (None, new_id) for l in copy_lines))
		# Pas de nouvelle copie à chaque passage.
		lx.pull()
		self.assertEqual(len(lx.state().conflicts), 1)

	def test_config_change_conflict_and_deletion(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		agent_m = os.path.join(m.paths.claude_dir, "agents", "relecteur.md")
		agent_l = os.path.join(lx.paths.claude_dir, "agents", "relecteur.md")
		# Modification simple : propagée.
		write(agent_m, "agent v2\n")
		m.push()
		lx.pull()
		with open(agent_l) as fh:
			self.assertEqual(fh.read(), "agent v2\n")
		# Modification qui descend de la version reçue : propagée dans l'autre sens, sans conflit.
		write(agent_l, "agent v3 (linux)\n")
		lx.push()
		m.pull()
		with open(agent_m) as fh:
			self.assertEqual(fh.read(), "agent v3 (linux)\n")
		self.assertEqual(m.state().conflicts, [])
		# Modifications concurrentes : conflit, version locale gardée, copie créée.
		write(agent_m, "agent mac\n")
		write(agent_l, "agent linux\n")
		m.push()
		lx.pull()
		with open(agent_l) as fh:
			self.assertEqual(fh.read(), "agent linux\n")
		conflicts = lx.state().conflicts
		self.assertEqual(len(conflicts), 1)
		with open(conflicts[0]["copy"]) as fh:
			self.assertEqual(fh.read(), "agent mac\n")
		# Résolution sur Linux en supprimant la copie : la version Linux gagne partout.
		os.remove(conflicts[0]["copy"])
		lx.sync()
		self.assertEqual(lx.state().conflicts, [])
		m.pull()
		with open(agent_m) as fh:
			self.assertEqual(fh.read(), "agent linux\n")
		# Suppression propagée.
		os.remove(agent_l)
		lx.push_twice()
		report = m.pull()
		self.assertEqual(report.counts.get("files_deleted"), 1)
		self.assertFalse(os.path.exists(agent_m))
		# Elle ne revient pas.
		m.sync()
		lx.sync()
		self.assertFalse(os.path.exists(agent_l))
		self.assertFalse(os.path.exists(agent_m))

	def test_deleted_session_is_deleted_everywhere(self):
		self._setup_both()
		lx, m = self.linux, self.mac
		target = lx.session_path(lx.project, self.sid)
		mac_path = m.session_path(m.project, self.sid)
		chunks = m.state().entries["p|⟪csync:P:git:github.com/alex/monprojet⟫|%s.jsonl" % self.sid]["chunks"]
		os.remove(target)
		lx.sync()
		self.assertTrue(os.path.exists(mac_path), "une absence d'un seul passage ne suffit pas")
		lx.sync()
		report = m.pull()
		self.assertEqual(report.counts.get("sessions_deleted"), 1)
		self.assertFalse(os.path.exists(mac_path))
		# Elle ne revient d'aucun côté, et aucune copie n'en reste dans les sauvegardes.
		m.sync()
		lx.sync()
		self.assertFalse(os.path.exists(target))
		self.assertFalse(os.path.exists(mac_path))
		for mach in (m, lx):
			for d, _, fs in os.walk(mach.paths.backups_dir):
				self.assertFalse(any(self.sid in f for f in fs))
		# Après compactage par la tâche périodique, son contenu a quitté l'historique du dépôt.
		for mach in (lx, m):
			r = self._cli(mach, ["periodic"])
			self.assertEqual(r.returncode, 0, r.stderr)
		out = subprocess.run(["git", "--git-dir", self.remote, "rev-list", "--all", "--objects"],
							 capture_output=True, text=True).stdout
		self.assertFalse([c for c in chunks if c in out], "contenu supprimé encore présent dans le dépôt")

	def test_unknown_project_is_parked_then_installed(self):
		m, lx = self.mac, self.linux
		other = os.path.join(m.home, "dev", "autre")
		os.makedirs(other)
		git("init", "-q", cwd=other)
		git("remote", "add", "origin", "https://github.com/alex/autre.git", cwd=other)
		sid = str(uuid.uuid4())
		write(m.session_path(other, sid), jsonl([{"type": "user", "sessionId": sid, "cwd": other}]))
		self._setup_both()
		parked = lx.state().get("parked")
		self.assertEqual(len(parked), 1)
		self.assertEqual(parked[0]["project"], "git:github.com/alex/autre")
		self.assertEqual(parked[0]["remote_path"], other)
		# L'utilisateur clone le projet ailleurs sur Linux puis relance.
		clone = os.path.join(lx.home, "src", "autre")
		os.makedirs(clone)
		git("init", "-q", cwd=clone)
		git("remote", "add", "origin", "git@github.com:Alex/autre", cwd=clone)
		write(os.path.join(lx.paths.projects_dir, slug(clone), "placeholder.jsonl"),
			  jsonl([{"type": "user", "cwd": clone}]))
		lx.sync()
		self.assertTrue(os.path.exists(lx.session_path(clone, sid)))
		self.assertEqual(lx.state().get("parked"), [])

	def test_active_session_untouched(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		with open(m.session_path(m.project, self.sid), "a", encoding="utf-8") as fh:
			fh.write(json.dumps({"type": "user", "sessionId": self.sid, "uuid": "x"}) + "\n")
		m.push()
		target = lx.session_path(lx.project, self.sid)
		before = read_bytes_of(target)
		# Conversation ouverte dans une autre fenêtre de Claude Code : on n'y touche pas.
		marker = os.path.join(lx.paths.claude_sessions_dir, "%d.json" % os.getpid())
		write(marker, json.dumps({"pid": os.getpid(), "sessionId": self.sid}))
		report = lx.pull()
		self.assertEqual(report.counts.get("sessions_active_skipped"), 1)
		self.assertEqual(before, read_bytes_of(target))
		# Un marqueur laissé par un processus disparu n'empêche rien.
		os.remove(marker)
		write(os.path.join(lx.paths.claude_sessions_dir, "999999.json"), json.dumps({"sessionId": self.sid}))
		lx.pull()
		after = read_bytes_of(target)
		self.assertTrue(after.startswith(before) and len(after) > len(before))

	def test_resumed_session_is_never_modified_but_reported(self):
		"""Claude Code a déjà chargé la conversation reprise quand le hook s'exécute : on n'y touche
		pas, on prévient, et « csync resume » la rouvre à jour."""
		self._setup_both()
		m, lx = self.mac, self.linux
		with open(m.session_path(m.project, self.sid), "a", encoding="utf-8") as fh:
			fh.write(json.dumps({"type": "user", "sessionId": self.sid, "uuid": "x"}) + "\n")
		m.push()
		target = lx.session_path(lx.project, self.sid)
		before = read_bytes_of(target)
		r = self._cli(lx, ["hook", "session-start"], json.dumps({"session_id": self.sid, "source": "resume"}))
		self.assertEqual(r.returncode, 0, r.stderr)
		message = json.loads(r.stdout)["systemMessage"]
		self.assertIn("a avancé sur mac", message)
		self.assertIn("csync resume %s" % self.sid, message)
		self.assertEqual(before, read_bytes_of(target))
		# « csync resume » récupère d'abord, puis lance claude --resume.
		fake = os.path.join(self.root, "fake-claude")
		calls = os.path.join(self.root, "calls.txt")
		write(fake, "#!/bin/sh\necho \"$@\" > '%s'\n" % calls, 0o755)
		r = self._cli(lx, ["resume", self.sid], extra_env={"CSYNC_CLAUDE": fake, "PATH": "/usr/bin:/bin"})
		self.assertEqual(r.returncode, 0, r.stderr)
		with open(calls) as fh:
			self.assertEqual(fh.read().split(), ["--resume", self.sid])
		self.assertEqual(self._read_lines(target)[-1]["uuid"], "x")

	def test_dry_run_changes_nothing(self):
		self.mac.setup(self.remote, self.key, create_key=True)
		lx = self.linux
		os.makedirs(lx.paths.csync_dir, exist_ok=True)
		# Configuration de Linux sans synchronisation, puis aperçu.
		config = MachineConfig({"machine_id": "linuxtest001", "machine_name": "linux", "repo_url": self.remote,
								"key_path": self.key, "active_session_grace_seconds": 0})
		config.save(lx.paths)
		from csync.store import Store
		Store(lx.paths.repo_dir, Cipher(load_key(self.key)), "linuxtest001").clone(self.remote)
		report = lx.pull(dry_run=True)
		self.assertGreaterEqual(report.counts.get("sessions_new", 0), 2)
		self.assertFalse(os.path.exists(lx.paths.projects_dir))
		self.assertFalse(os.path.exists(os.path.join(lx.paths.claude_dir, "CLAUDE.md")))

	def test_wrong_key_is_refused(self):
		self.mac.setup(self.remote, self.key, create_key=True)
		wrong = os.path.join(self.root, "wrong.key")
		generate_key_file(wrong)
		with self.assertRaises(CSyncError) as ctx:
			self.linux.setup(self.remote, wrong)
		self.assertIn("clé", str(ctx.exception))

	def test_unreachable_remote_leaves_local_intact(self):
		self._setup_both()
		lx = self.linux
		subprocess.run(["git", "-C", lx.paths.repo_dir, "remote", "set-url", "origin", os.path.join(self.root, "absent.git")])
		snapshot = {}
		for dirpath, _, files in os.walk(lx.paths.claude_dir):
			for f in files:
				p = os.path.join(dirpath, f)
				snapshot[p] = read_bytes_of(p)
		with self.assertRaises(CSyncError):
			lx.sync()
		for p, data in snapshot.items():
			self.assertEqual(read_bytes_of(p), data)
		status = ops.read_status(lx.paths)
		self.assertTrue(status.get("last_error"))

	def test_corrupted_object_is_reported_not_applied(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		write(os.path.join(m.paths.claude_dir, "CLAUDE.md"), "nouvelle consigne\n")
		m.push()
		# Altération du bloc côté dépôt distant (simulation d'un fichier abîmé).
		clone = os.path.join(self.root, "tamper")
		git("clone", "-q", self.remote, clone)
		state = m.state()
		oid = state.entries["c|CLAUDE.md"]["oid"]
		target = None
		for dirpath, _, files in os.walk(os.path.join(clone, "objects")):
			if oid + ".bin" in files:
				target = os.path.join(dirpath, oid + ".bin")
		data = bytearray(read_bytes_of(target))
		data[-1] ^= 0xFF
		with open(target, "wb") as fh:
			fh.write(bytes(data))
		git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "tamper", cwd=clone)
		git("push", "-q", "origin", "HEAD:main", cwd=clone)
		report = lx.pull()
		self.assertTrue(any("CLAUDE.md" in w for w in report.warnings))
		with open(os.path.join(lx.paths.claude_dir, "CLAUDE.md")) as fh:
			self.assertEqual(fh.read(), "Réponds en français.\n")

	def test_format_check_failure_suspends_sessions(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		with open(m.paths.adapter_file, "w") as fh:
			json.dump({"session_cwd_key": "workingDirectory"}, fh)
		with open(m.session_path(m.project, self.sid), "a", encoding="utf-8") as fh:
			fh.write(json.dumps({"type": "user", "sessionId": self.sid, "uuid": "late"}) + "\n")
		m.push()
		self.assertFalse(read_json(m.paths.profile_file)["format_check"]["sessions_ok"])
		report = lx.pull()
		self.assertFalse(report.counts.get("sessions_extended"))
		# Retour au format reconnu : tout reprend.
		os.remove(m.paths.adapter_file)
		m.push()
		lx.pull()
		self.assertEqual(self._read_lines(lx.session_path(lx.project, self.sid))[-1]["uuid"], "late")

	def test_concurrent_pushes(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		write(os.path.join(m.paths.claude_dir, "agents", "a.md"), "a\n")
		write(os.path.join(lx.paths.claude_dir, "agents", "b.md"), "b\n")
		m.push()
		lx.push()   # doit intégrer l'envoi du Mac puis réussir
		m.pull()
		lx.pull()
		self.assertTrue(os.path.exists(os.path.join(m.paths.claude_dir, "agents", "b.md")))
		self.assertTrue(os.path.exists(os.path.join(lx.paths.claude_dir, "agents", "a.md")))

	def test_rescan_does_not_duplicate_sessions(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		# Un dossier non git devient partagé sous un nom commun après coup.
		from csync.controller import Controller
		Controller(m.paths).name_project(self.notes, "notes")
		with open(m.session_path(self.notes, self.sid2), "a", encoding="utf-8") as fh:
			fh.write(json.dumps({"type": "user", "sessionId": self.sid2, "uuid": "after", "cwd": self.notes}) + "\n")
		m.push()
		lx.pull()
		notes_linux = os.path.join(lx.home, "Documents", "notes")
		last = self._read_lines(lx.session_path(notes_linux, self.sid2))[-1]
		self.assertEqual(last["uuid"], "after")
		self.assertEqual(last["cwd"], notes_linux, "chemin d'un projet inconnu mal estimé")
		self.assertEqual(lx.state().conflicts, [])
		files = os.listdir(lx.paths.projects_dir)
		self.assertEqual(len([f for f in files if "notes" in f]), 1)

	def test_mcp_pending_while_claude_runs(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		cj = read_json(m.paths.claude_json)
		cj["mcpServers"]["web"] = {"type": "http", "url": "https://example.com/mcp"}
		with open(m.paths.claude_json, "w") as fh:
			json.dump(cj, fh)
		m.push()
		os.environ["CSYNC_ASSUME_CLAUDE_RUNNING"] = "1"
		try:
			report = lx.pull()
		finally:
			os.environ["CSYNC_ASSUME_CLAUDE_RUNNING"] = "0"
		self.assertEqual(report.counts.get("mcp_pending"), 1)
		self.assertNotIn("web", read_json(lx.paths.claude_json)["mcpServers"])
		lx.pull()
		self.assertIn("web", read_json(lx.paths.claude_json)["mcpServers"])
		self.assertEqual(lx.state().get("pending_mcp"), [])

	def test_prefer_remote_on_first_setup(self):
		write(os.path.join(self.linux.paths.claude_dir, "CLAUDE.md"), "ancienne consigne linux\n")
		self.mac.setup(self.remote, self.key, create_key=True)
		self.linux.setup(self.remote, self.key, prefer_remote=True)
		with open(os.path.join(self.linux.paths.claude_dir, "CLAUDE.md")) as fh:
			self.assertEqual(fh.read(), "Réponds en français.\n")
		backups = os.listdir(self.linux.paths.backups_dir)
		self.assertTrue(any(b.startswith("initial-") for b in backups))

	def test_invalid_json_settings_not_overwritten(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		settings_m = read_json(m.paths.settings_file)
		settings_m["model"] = "opus"
		with open(m.paths.settings_file, "w") as fh:
			json.dump(settings_m, fh)
		m.push()
		write(lx.paths.settings_file, "{ en cours de modification")
		lx.pull()
		with open(lx.paths.settings_file) as fh:
			self.assertEqual(fh.read(), "{ en cours de modification")

	def test_large_session_and_subagents(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		path = m.session_path(m.project, self.sid)
		big = "y" * 90000
		with open(path, "a", encoding="utf-8") as fh:
			for i in range(12):
				fh.write(json.dumps({"type": "assistant", "sessionId": self.sid, "i": i,
									 "content": "%s/f%d %s" % (m.project, i, big)}) + "\n")
		sub = os.path.join(m.session_dir(m.project), self.sid, "subagents", "agent-1.jsonl")
		write(sub, jsonl([{"type": "user", "isSidechain": True, "cwd": m.project, "sessionId": self.sid}]))
		m.push()
		lx.pull()
		lines = self._read_lines(lx.session_path(lx.project, self.sid))
		self.assertEqual(len(lines), 15)
		self.assertTrue(lines[-1]["content"].startswith(lx.project + "/f11 "))
		lsub = os.path.join(lx.session_dir(lx.project), self.sid, "subagents", "agent-1.jsonl")
		self.assertEqual(self._read_lines(lsub)[0]["cwd"], lx.project)
		# Un ajout ne renvoie que les dernières tranches.
		with open(path, "a", encoding="utf-8") as fh:
			fh.write(json.dumps({"type": "user", "sessionId": self.sid, "uuid": "tail"}) + "\n")
		report = m.push()
		self.assertLessEqual(report.counts.get("objects_written", 0), 1)
		lx.pull()
		self.assertEqual(self._read_lines(lx.session_path(lx.project, self.sid))[-1]["uuid"], "tail")
		self.assertEqual(lx.state().conflicts, [])

	def test_rewritten_session_replaces_untouched_copy(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		path = m.session_path(m.project, self.sid)
		lines = self._read_lines(path)
		lines[0]["message"]["content"] = "réécrit (compaction)"
		write(path, jsonl(lines + [{"type": "user", "uuid": "after-rewrite"}]))
		m.push()
		report = lx.pull()
		# Linux n'avait pas touché à la session : la version réécrite la remplace, sans conflit.
		self.assertEqual(report.counts.get("sessions_rewritten"), 1)
		self.assertEqual(self._read_lines(lx.session_path(lx.project, self.sid))[0]["message"]["content"],
						 "réécrit (compaction)")
		# Et le Mac ne voit pas l'ancienne version de Linux comme un conflit.
		lx.push()
		report = m.pull()
		self.assertFalse(report.counts.get("sessions_conflict"))
		self.assertEqual(m.state().conflicts, [])

	def test_conflict_copy_is_updated_not_multiplied(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		mp, lp = m.session_path(m.project, self.sid), lx.session_path(lx.project, self.sid)
		lines = self._read_lines(mp)
		lines[0]["message"]["content"] = "réécrit sur le Mac"
		write(mp, jsonl(lines))
		with open(lp, "a", encoding="utf-8") as fh:
			fh.write(json.dumps({"type": "user", "sessionId": self.sid, "uuid": "linux-only"}) + "\n")
		m.push()
		lx.sync()
		self.assertEqual(len(lx.state().conflicts), 1)
		copy = lx.state().conflicts[0]["copy"]
		for i in range(3):
			with open(mp, "a", encoding="utf-8") as fh:
				fh.write(json.dumps({"type": "user", "sessionId": self.sid, "uuid": "mac-%d" % i}) + "\n")
			m.push()
			lx.pull()
		self.assertEqual(len(lx.state().conflicts), 1, "une seule copie, mise à jour")
		self.assertEqual(self._read_lines(copy)[-1]["uuid"], "mac-2")
		copies = [f for f in os.listdir(os.path.dirname(lp)) if f.endswith(".jsonl")]
		self.assertEqual(len(copies), 2)

	def test_cli_hooks(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		write(os.path.join(m.paths.claude_dir, "agents", "nouveau.md"), "via hook\n")
		# Fin de réponse sur le Mac : envoi en arrière-plan.
		ops.update_status(m.paths, last_push=0, last_push_attempt=0)
		r = self._cli(m, ["hook", "stop"], json.dumps({"session_id": self.sid}))
		self.assertEqual(r.returncode, 0, r.stderr)
		self.assertEqual(r.stdout, "")
		deadline = time.time() + 60
		while time.time() < deadline and m.state().entries.get("c|agents/nouveau.md", {}).get("v") is None:
			time.sleep(0.5)
		while time.time() < deadline and os.path.exists(m.paths.lock_file) and not self._lock_free(m):
			time.sleep(0.5)
		# Deuxième appel immédiat : ignoré (délai de 5 minutes).
		before = ops.read_status(m.paths).get("last_push_attempt")
		self._cli(m, ["hook", "stop"], json.dumps({"session_id": self.sid}))
		self.assertEqual(before, ops.read_status(m.paths).get("last_push_attempt"))
		# Démarrage d'une session sur Linux : réception, sortie vide (aucun conflit).
		r = self._cli(lx, ["hook", "session-start"], json.dumps({"session_id": str(uuid.uuid4()), "source": "startup"}))
		self.assertEqual(r.returncode, 0, r.stderr)
		self.assertEqual(r.stdout.strip(), "")
		self.assertTrue(os.path.exists(os.path.join(lx.paths.claude_dir, "agents", "nouveau.md")))
		# Pendant une opération en cours ou sans configuration, un hook ne fait jamais échouer Claude Code.
		env_home = os.path.join(self.root, "vide")
		os.makedirs(env_home)
		r = subprocess.run([sys.executable, "-m", "csync", "hook", "session-start"], input="{}", capture_output=True,
						   text=True, env=dict(os.environ, HOME=env_home, PYTHONPATH=ROOT), timeout=60)
		self.assertEqual((r.returncode, r.stdout), (0, ""))

	def test_status_and_profile_text(self):
		self._setup_both()
		from csync.controller import Controller
		status = Controller(self.linux.paths).status()
		self.assertEqual(status["machine_name"], "linux")
		self.assertEqual([m["name"] for m in status["machines"]], ["mac"])
		text = Controller(self.linux.paths).profile_text()
		self.assertIn("git:github.com/alex/monprojet", text)


class Units(unittest.TestCase):
	def test_token_roundtrip(self):
		a = PathMapper([("/Users/alex/.claude", "CLAUDE"), ("/Users/alex/dev/p", "P:git:x/p"), ("/Users/alex", "HOME")])
		b = PathMapper([("/home/cy/.claude", "CLAUDE"), ("/home/cy/code/p", "P:git:x/p"), ("/home/cy", "HOME")])
		text = "cd /Users/alex/dev/p/src && cat /Users/alex/notes /Users/alex2/x /Volumes/Users/alex/y " \
			   "/Users/alex/.claude/projects/-Users-alex-dev-p-src/a.jsonl"
		tok = a.tokenize_str(text)
		self.assertNotIn("/Users/alex/", tok.replace("/Users/alex2", "").replace("/Volumes/Users/alex", ""))
		out = b.detokenize_str(tok)
		self.assertEqual(out, "cd /home/cy/code/p/src && cat /home/cy/notes /Users/alex2/x /Volumes/Users/alex/y "
							  "/home/cy/.claude/projects/-home-cy-code-p-src/a.jsonl")
		self.assertEqual(a.detokenize_str(b.tokenize_str(out)), text)

	def test_unknown_token_kept(self):
		a = PathMapper([("/Users/c/dev/p", "P:git:x/p"), ("/Users/c", "HOME")])
		b = PathMapper([("/home/c", "HOME")])
		tok = a.tokenize_line(json.dumps({"cwd": "/Users/c/dev/p"}))
		self.assertIsNone(b.detokenize_path(a.tokenize_path("/Users/c/dev/p")))
		back = b.detokenize_line(tok)
		self.assertEqual(b.tokenize_line(back), tok)

	def test_chunking_is_prefix_stable(self):
		lines = ["x" * 1000 + str(i) for i in range(600)]
		full = chunk_lines(lines)
		part = chunk_lines(lines[:300])
		self.assertEqual(full[:len(part) - 1], part[:-1])
		self.assertEqual(full[len(part) - 1][:len(part[-1])], part[-1])
		resumed = part[:-1] + chunk_lines(lines[300:], first=part[-1])
		self.assertEqual(resumed, full)
		self.assertTrue(all(sum(len(l) + 1 for l in c) <= CHUNK_MAX_BYTES for c in full))

	def test_crypto(self):
		d = tempfile.mkdtemp()
		try:
			k = os.path.join(d, "k")
			generate_key_file(k)
			c = Cipher(load_key(k))
			blob = c.encrypt(b"bonjour", "ctx")
			self.assertEqual(c.decrypt(blob, "ctx"), b"bonjour")
			with self.assertRaises(CSyncError):
				c.decrypt(blob, "autre-contexte")
			with self.assertRaises(CSyncError):
				generate_key_file(k)
			self.assertEqual(os.stat(k).st_mode & 0o777, 0o600)
		finally:
			shutil.rmtree(d)

	def test_hooks_install_idempotent(self):
		d = tempfile.mkdtemp()
		try:
			paths = Paths(csync_dir=os.path.join(d, ".csync"), home=d, claude_dir=os.path.join(d, ".claude"),
						  claude_json=os.path.join(d, ".claude.json"))
			write(paths.settings_file, json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo moi"}]}]},
												   "model": "opus"}))
			from csync.hooks import hooks_installed, install_hooks, uninstall_hooks
			self.assertTrue(install_hooks(paths))
			self.assertFalse(install_hooks(paths))
			self.assertTrue(hooks_installed(paths))
			s = read_json(paths.settings_file)
			self.assertEqual(len(s["hooks"]["Stop"]), 2)
			commands = [h["command"] for g in s["hooks"]["Stop"] for h in g["hooks"]]
			self.assertIn('"$HOME/.csync/bin/csync" hook stop', commands)
			self.assertTrue(uninstall_hooks(paths))
			s = read_json(paths.settings_file)
			self.assertEqual(s["hooks"], {"Stop": [{"hooks": [{"type": "command", "command": "echo moi"}]}]})
			self.assertEqual(s["model"], "opus")
		finally:
			shutil.rmtree(d)

	def test_normalize_remote(self):
		from csync.scan import normalize_remote
		for url in ("git@github.com:Alex/Monprojet.git", "https://github.com/alex/monprojet",
					"ssh://git@github.com/Alex/Monprojet.git", "https://user:tok@github.com/alex/monprojet.git/"):
			self.assertEqual(normalize_remote(url), "github.com/alex/monprojet")


if __name__ == "__main__":
	unittest.main()
