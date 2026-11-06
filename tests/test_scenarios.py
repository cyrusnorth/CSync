"""Scénarios issus de la relecture indépendante : chaque test reproduit un cas qui posait problème.

Lancement : python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid

import csync.sync
from csync import ops
from csync.controller import Controller
from csync.hooks import hooks_installed
from csync.paths import MachineConfig
from csync.state import State
from csync.sync import Engine
from csync.util import CSyncError, read_json

import test_csync
from test_csync import Harness, git, jsonl, read_bytes_of, write


def git_out(*args, cwd=None):
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True, env=env)


class Scenarios(Harness):

	def _engine(self, machine):
		return Engine(machine.paths, MachineConfig.load(machine.paths))

	def _manifest(self, machine):
		eng = self._engine(machine)
		doc = eng.store.read_manifest(MachineConfig.load(machine.paths).machine_id)
		return doc[1] if doc else {}

	def _append(self, path, obj):
		with open(path, "a", encoding="utf-8") as fh:
			fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

	# -- C1 : variables git de l'utilisateur ----------------------------------------------

	def test_git_environment_is_ignored(self):
		"""Un hook lancé depuis un « git commit » hérite de GIT_DIR : CSync ne doit pas toucher au dépôt de l'utilisateur."""
		self._setup_both()
		m, lx = self.mac, self.linux
		write(os.path.join(m.paths.claude_dir, "agents", "x.md"), "x\n")
		user_git = os.path.join(m.project, ".git")
		saved = {k: os.environ.get(k) for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
		os.environ.update({"GIT_DIR": user_git, "GIT_WORK_TREE": m.project,
						   "GIT_INDEX_FILE": os.path.join(user_git, "index")})
		try:
			m.push()
			lx.pull()
		finally:
			for k, v in saved.items():
				if v is None:
					os.environ.pop(k, None)
				else:
					os.environ[k] = v
		self.assertNotEqual(git_out("rev-parse", "--verify", "-q", "HEAD", cwd=m.project).returncode, 0,
							"un commit a été créé dans le dépôt de l'utilisateur")
		self.assertFalse(os.path.exists(os.path.join(user_git, "index")))
		self.assertFalse(os.path.exists(os.path.join(m.project, "objects")))
		self.assertTrue(os.path.exists(os.path.join(lx.paths.claude_dir, "agents", "x.md")))

	# -- C2 : retour à un contenu antérieur, suppression puis recréation ------------------

	def test_revert_to_earlier_content_propagates(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		am = os.path.join(m.paths.claude_dir, "agents", "relecteur.md")
		al = os.path.join(lx.paths.claude_dir, "agents", "relecteur.md")
		write(am, "v2\n")
		m.push()
		lx.sync()
		write(am, "agent\n")          # retour au contenu d'origine
		m.sync()
		with open(am) as fh:
			self.assertEqual(fh.read(), "agent\n")
		lx.sync()
		with open(al) as fh:
			self.assertEqual(fh.read(), "agent\n")
		self.assertEqual(lx.state().conflicts, [])
		self.assertEqual(m.state().conflicts, [])

	def test_deleted_file_recreated_with_same_content(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		am = os.path.join(m.paths.claude_dir, "agents", "relecteur.md")
		al = os.path.join(lx.paths.claude_dir, "agents", "relecteur.md")
		os.remove(am)
		m.push_twice()
		lx.sync()
		self.assertFalse(os.path.exists(al))
		write(al, "agent\n")          # recréé à l'identique
		lx.sync()
		m.sync()
		self.assertTrue(os.path.exists(am))
		lx.sync()
		self.assertTrue(os.path.exists(al))

	def test_deletion_waits_for_confirmation(self):
		self._setup_both()
		csync.sync.DELETE_GRACE_SECONDS = 60
		m, lx = self.mac, self.linux
		key = "c|agents/relecteur.md"
		os.remove(os.path.join(m.paths.claude_dir, "agents", "relecteur.md"))
		m.push()
		m.push()
		lx.pull()
		self.assertTrue(os.path.exists(os.path.join(lx.paths.claude_dir, "agents", "relecteur.md")),
						"une absence de moins d'une minute ne doit pas être publiée")
		state = State.load(m.paths)
		state.entries[key]["missing_since"] -= 120
		state.save(m.paths)
		m.push()
		lx.pull()
		self.assertFalse(os.path.exists(os.path.join(lx.paths.claude_dir, "agents", "relecteur.md")))

	# -- C3 : absence n'est pas suppression ------------------------------------------------

	def test_missing_claude_json_never_deletes_mcp_servers(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		self.assertIn("fs", read_json(lx.paths.claude_json)["mcpServers"])
		os.remove(lx.paths.claude_json)
		lx.push_twice()
		m.pull()
		self.assertIn("fs", read_json(m.paths.claude_json)["mcpServers"])
		report = lx.pull()
		self.assertTrue(any("MCP" in w for w in report.warnings))
		self.assertFalse(os.path.exists(lx.paths.claude_json), "~/.claude.json ne doit pas être créé par CSync")

	def test_missing_claude_dir_suspends_sync(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		moved = lx.paths.claude_dir + ".ailleurs"
		shutil.move(lx.paths.claude_dir, moved)
		with self.assertRaises(CSyncError):
			lx.push()
		shutil.move(moved, lx.paths.claude_dir)
		m.pull()
		self.assertTrue(os.path.exists(os.path.join(m.paths.claude_dir, "CLAUDE.md")))
		self.assertTrue(os.path.exists(os.path.join(m.paths.claude_dir, "agents", "relecteur.md")))

	def test_other_claude_config_dir_in_environment_is_ignored(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		empty = os.path.join(self.root, "autre-config")
		os.makedirs(empty)
		os.environ["CLAUDE_CONFIG_DIR"] = empty
		try:
			lx.push_twice()
		finally:
			os.environ.pop("CLAUDE_CONFIG_DIR", None)
		m.pull()
		self.assertTrue(os.path.exists(os.path.join(m.paths.claude_dir, "agents", "relecteur.md")))
		self.assertEqual(os.listdir(empty), [])

	def test_mass_deletion_requires_confirmation(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		names = ["a%d.md" % i for i in range(10)]
		for n in names:
			write(os.path.join(m.paths.claude_dir, "agents", n), n + "\n")
		m.push()
		lx.pull()
		for n in names:
			os.remove(os.path.join(m.paths.claude_dir, "agents", n))
		report = m.push_twice()
		self.assertTrue(any("allow-deletions" in w for w in report.warnings))
		lx.pull()
		for n in names:
			self.assertTrue(os.path.exists(os.path.join(lx.paths.claude_dir, "agents", n)))
		alerts = Controller(m.paths).status()["alerts"]
		self.assertTrue(any("allow-deletions" in a for a in alerts))
		m.push(allow_deletions=True)
		lx.pull()
		for n in names:
			self.assertFalse(os.path.exists(os.path.join(lx.paths.claude_dir, "agents", n)))
		self.assertFalse(any("allow-deletions" in a for a in Controller(m.paths).status()["alerts"]))

	# -- C4 : hooks installés sur les deux machines -----------------------------------------

	def test_setup_with_hooks_on_both_machines(self):
		self.mac.setup(self.remote, self.key, create_key=True, hooks=True)
		self.linux.setup(self.remote, self.key, hooks=True)
		m, lx = self.mac, self.linux
		self.assertTrue(hooks_installed(m.paths))
		self.assertTrue(hooks_installed(lx.paths))
		settings = read_json(lx.paths.settings_file)
		self.assertEqual(settings["permissions"]["allow"], ["Read(%s/**)" % lx.project])
		self.assertEqual(m.state().conflicts, [])
		self.assertEqual(lx.state().conflicts, [])
		# Les hooks ne partent jamais dans le dépôt.
		eng = self._engine(m)
		entry = self._manifest(m)["c|settings.json"]
		self.assertNotIn(b"/bin/csync", eng.store.get_object(entry["oid"]))
		# Une modification des réglages passe, et chaque machine garde ses hooks.
		s = read_json(m.paths.settings_file)
		s["model"] = "opus"
		write(m.paths.settings_file, json.dumps(s, indent=2))
		m.push()
		lx.pull()
		self.assertEqual(read_json(lx.paths.settings_file)["model"], "opus")
		self.assertTrue(hooks_installed(lx.paths))
		self.assertTrue(hooks_installed(m.paths))
		self.assertEqual(lx.state().conflicts, [])
		# Stabilité : rien ne fait des allers-retours.
		m.sync()
		lx.sync()
		before = self._remote_head()
		m.sync()
		lx.sync()
		self.assertEqual(before, self._remote_head())

	# -- C5 : copie de conflit reprise --------------------------------------------------------

	def test_resumed_conflict_copy_is_never_overwritten(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		mp, lp = m.session_path(m.project, self.sid), lx.session_path(lx.project, self.sid)
		self._append(mp, {"type": "user", "sessionId": self.sid, "uuid": "mac-only"})
		self._append(lp, {"type": "user", "sessionId": self.sid, "uuid": "linux-only"})
		m.push()
		lx.sync()
		copy = lx.state().conflicts[0]["copy"]
		copy_id = os.path.basename(copy)[:-len(".jsonl")]
		# L'utilisateur reprend la copie dans Claude Code.
		self._append(copy, {"type": "user", "sessionId": copy_id, "uuid": "work-in-copy"})
		lx.sync()
		# Le Mac continue la session d'origine.
		self._append(mp, {"type": "user", "sessionId": self.sid, "uuid": "mac-2"})
		m.sync()
		lx.sync()
		self.assertEqual(self._read_lines(copy)[-1]["uuid"], "work-in-copy", "travail fait dans la copie perdu")
		latest = lx.state().conflicts[-1]["copy"]
		self.assertNotEqual(latest, copy)
		self.assertEqual(self._read_lines(latest)[-1]["uuid"], "mac-2")
		# La copie reprise est devenue une conversation ordinaire, reçue par le Mac.
		m.pull()
		mac_copy = m.session_path(m.project, copy_id)
		self.assertEqual(self._read_lines(mac_copy)[-1]["uuid"], "work-in-copy")

	# -- C6 : octets non UTF-8 et caractères isolés -------------------------------------------

	def test_invalid_bytes_and_lone_surrogates_roundtrip(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		mp, lp = m.session_path(m.project, self.sid), lx.session_path(lx.project, self.sid)
		raw = [
			b'{"type":"user","sessionId":"' + self.sid.encode() + b'","t":"\xff\xfe caf\xc3\xa9"}\n',
			('{"type":"user","sessionId":"%s","t":"x \\ud83d"}\n' % self.sid).encode(),
			b'{"type":"user","cwd":"' + m.project.encode() + b'","t":"\x80 \xed\xa0\xbd"}\n',
		]
		with open(mp, "ab") as fh:
			fh.write(b"".join(raw))
		m.push()
		lx.pull()
		expected = b"".join(r.replace(m.project.encode(), lx.project.encode()) for r in raw)
		self.assertTrue(read_bytes_of(lp).endswith(expected))
		with open(lp, "ab") as fh:
			fh.write(b'{"type":"assistant","t":"\xc0\xaf ' + lx.project.encode() + b'"}\n')
		lx.push()
		m.pull()
		self.assertTrue(read_bytes_of(mp).endswith(b'{"type":"assistant","t":"\xc0\xaf ' + m.project.encode() + b'"}\n'))
		self.assertEqual(lx.state().conflicts, [])
		self.assertEqual(m.state().conflicts, [])

	# -- Interruption brutale pendant une réception --------------------------------------------

	def test_interrupted_pull_does_not_create_conflicts(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		# Ligne qui mentionne un chemin du serveur : sa forme neutre diffère selon la machine qui la calcule.
		self._append(m.session_path(m.project, self.sid),
					 {"type": "user", "sessionId": self.sid, "message": {"content": "ssh serveur ls %s/src" % lx.project}})
		m.push()
		original_save = State.save
		State.save = lambda self_, paths: None     # arrêt brutal : l'état n'est jamais enregistré
		try:
			lx.pull()
		finally:
			State.save = original_save
		self.assertTrue(os.path.exists(lx.paths.state_file + ".journal"))
		report = lx.sync()[0]
		self.assertFalse(report.counts.get("sessions_conflict"))
		report = m.sync()[0]
		self.assertFalse(report.counts.get("sessions_conflict"))
		self.assertEqual(lx.state().conflicts, [])
		self.assertEqual(m.state().conflicts, [])
		self.assertFalse(os.path.exists(lx.paths.state_file + ".journal"))

	# -- Projet vu avant d'avoir un dépôt distant ----------------------------------------------

	def test_project_seen_before_its_remote_is_matched(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		newp = os.path.join(m.home, "dev", "video-ia")
		os.makedirs(newp)
		git("init", "-q", cwd=newp)
		sid = str(uuid.uuid4())
		write(m.session_path(newp, sid), jsonl([{"type": "user", "sessionId": sid, "cwd": newp,
												 "message": {"content": "voir %s/src/main.py" % newp}}]))
		m.push()
		git("remote", "add", "origin", "git@github.com:Alex/video-ia.git", cwd=newp)
		ctrl = Controller(m.paths)
		ctrl.apply_scan(ctrl.rescan(), {})
		m.push()
		clone = os.path.join(lx.home, "code", "video-ia")
		os.makedirs(clone)
		git("init", "-q", cwd=clone)
		git("remote", "add", "origin", "https://github.com/alex/video-ia", cwd=clone)
		lx.sync()     # le clone est trouvé sans rescan manuel
		target = lx.session_path(clone, sid)
		self.assertTrue(os.path.exists(target), "session non installée dans le clone")
		first = self._read_lines(target)[0]
		self.assertEqual(first["cwd"], clone)
		self.assertEqual(first["message"]["content"], "voir %s/src/main.py" % clone)
		self.assertFalse(os.path.exists(os.path.join(lx.home, "dev", "video-ia")))
		# Et dans l'autre sens.
		self._append(target, {"type": "user", "sessionId": sid, "cwd": clone, "message": {"content": "%s/lib" % clone}})
		lx.push()
		m.pull()
		self.assertEqual(self._read_lines(m.session_path(newp, sid))[-1]["message"]["content"], "%s/lib" % newp)
		self.assertEqual(lx.state().conflicts, [])
		self.assertEqual(m.state().conflicts, [])

	# -- Projet exclu ---------------------------------------------------------------------------

	def test_excluded_project_is_no_longer_published(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		ctrl = Controller(m.paths)
		ctrl.apply_scan(ctrl.rescan(), {m.project: {"include": False}})
		self._append(m.session_path(m.project, self.sid), {"type": "user", "sessionId": self.sid, "uuid": "secret"})
		m.push()
		self.assertFalse([k for k in self._manifest(m) if "monprojet" in k])
		lx.pull()
		lines = self._read_lines(lx.session_path(lx.project, self.sid))
		self.assertNotIn("secret", [l.get("uuid") for l in lines])

	# -- Dépôt créé sur GitHub avec un README -----------------------------------------------------

	def test_repository_created_with_readme(self):
		seed = os.path.join(self.root, "seed")
		git("clone", "-q", self.remote, seed)
		write(os.path.join(seed, "README.md"), "# csync-data\n")
		git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A", cwd=seed)
		git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "Initial commit", cwd=seed)
		git("push", "-q", "origin", "HEAD:main", cwd=seed)
		self._setup_both()
		with open(os.path.join(self.linux.paths.claude_dir, "CLAUDE.md")) as fh:
			self.assertEqual(fh.read(), "Réponds en français.\n")

	def test_repository_with_other_content_is_refused(self):
		seed = os.path.join(self.root, "seed")
		git("clone", "-q", self.remote, seed)
		write(os.path.join(seed, "src", "app.py"), "print('x')\n")
		git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A", cwd=seed)
		git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "code", cwd=seed)
		git("push", "-q", "origin", "HEAD:main", cwd=seed)
		with self.assertRaises(CSyncError) as ctx:
			self.mac.setup(self.remote, self.key, create_key=True)
		self.assertIn("dépôt", str(ctx.exception))

	# -- Réglages qui mentionnent un projet absent de l'autre machine ------------------------------

	def test_settings_with_project_unknown_elsewhere_roundtrip(self):
		m, lx = self.mac, self.linux
		other = os.path.join(m.home, "dev", "autre")
		os.makedirs(other)
		git("init", "-q", cwd=other)
		git("remote", "add", "origin", "https://github.com/alex/autre.git", cwd=other)
		sid = str(uuid.uuid4())
		write(m.session_path(other, sid), jsonl([{"type": "user", "sessionId": sid, "cwd": other}]))
		s = read_json(m.paths.settings_file)
		s["permissions"]["allow"].append("Read(%s/**)" % other)
		write(m.paths.settings_file, json.dumps(s, indent=2))
		self._setup_both()
		s = read_json(lx.paths.settings_file)
		s["model"] = "opus"
		write(lx.paths.settings_file, json.dumps(s, indent=2))
		lx.push()
		m.pull()
		s = read_json(m.paths.settings_file)
		self.assertEqual(s["model"], "opus")
		self.assertEqual(s["permissions"]["allow"], ["Read(%s/**)" % m.project, "Read(%s/**)" % other])

	# -- Compactage ----------------------------------------------------------------------------------

	def _remote_objects(self):
		out = git_out("--git-dir", self.remote, "ls-tree", "-r", "--name-only", "main").stdout.split()
		return {os.path.basename(p)[:-4] for p in out if p.startswith("objects/")}

	def _referenced(self, *machines):
		refs = set()
		for mach in machines:
			for entry in self._manifest(mach).values():
				refs.update(entry.get("chunks") or [])
				if entry.get("oid"):
					refs.add(entry["oid"])
		return refs

	def test_compaction_and_realignment(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		mp, lp = m.session_path(m.project, self.sid), lx.session_path(lx.project, self.sid)
		for i in range(4):
			self._append(mp, {"type": "user", "sessionId": self.sid, "uuid": "m%d" % i})
			m.sync()
			lx.sync()
			self._append(lp, {"type": "user", "sessionId": self.sid, "uuid": "l%d" % i})
			lx.sync()
			m.sync()
		before = self._remote_objects()
		self.assertTrue(before - self._referenced(m, lx), "le scénario devrait produire des blocs inutilisés")
		# Linux a une modification calculée mais pas encore envoyée au moment du compactage.
		self._append(lp, {"type": "user", "sessionId": self.sid, "uuid": "linux-pending"})
		lx.pull()
		removed, _, _ = ops.do_compact(m.paths)
		self.assertGreater(removed, 0)
		self.assertEqual(git_out("--git-dir", self.remote, "rev-list", "--count", "main").stdout.strip(), "1")
		lx.sync()
		m.pull()
		self.assertEqual(self._read_lines(mp)[-1]["uuid"], "linux-pending")
		garbage = self._remote_objects() - self._referenced(m, lx)
		self.assertEqual(garbage, set(), "des blocs inutilisés sont revenus après le réalignement")
		# Tout continue de fonctionner dans les deux sens.
		self._append(mp, {"type": "user", "sessionId": self.sid, "uuid": "after-m"})
		m.sync()
		lx.sync()
		self.assertEqual(self._read_lines(lp)[-1]["uuid"], "after-m")
		self.assertEqual(m.state().conflicts, [])
		self.assertEqual(lx.state().conflicts, [])
		test_csync.TwoMachines._assert_no_plaintext_leak(self)

	def test_periodic_run_compacts_large_repository(self):
		self._setup_both()
		m = self.mac
		config = MachineConfig.load(m.paths)
		config.data["auto_compact_mb"] = 0.0001
		config.save(m.paths)
		r = self._cli(m, ["periodic"])
		self.assertEqual(r.returncode, 0, r.stderr)
		self.assertEqual(git_out("--git-dir", self.remote, "rev-list", "--count", "main").stdout.strip(), "1")
		self.assertTrue(ops.read_status(m.paths).get("last_compact_attempt"))
		self.linux.sync()   # l'autre machine se réaligne sans erreur

	# -- Conflits désignés par identifiant ----------------------------------------------------------

	def test_cli_conflicts_by_id(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		write(os.path.join(m.paths.claude_dir, "agents", "relecteur.md"), "mac\n")
		write(os.path.join(lx.paths.claude_dir, "agents", "relecteur.md"), "linux\n")
		m.push()
		lx.pull()
		cid = lx.state().conflicts[0]["id"]
		r = self._cli(lx, ["conflicts"])
		self.assertIn("[%s]" % cid, r.stdout)
		r = self._cli(lx, ["conflicts", "--resolve", "inexistant"])
		self.assertEqual(r.returncode, 1)
		self.assertIn("inconnu", r.stderr)
		r = self._cli(lx, ["conflicts", "--resolve", cid])
		self.assertEqual(r.returncode, 0, r.stderr)
		self.assertEqual(lx.state().conflicts, [])
		lx.push()
		m.pull()
		with open(os.path.join(m.paths.claude_dir, "agents", "relecteur.md")) as fh:
			self.assertEqual(fh.read(), "linux\n")

	# -- Seconde relecture ---------------------------------------------------------------------------

	def test_unconfirmed_folder_outside_home_is_not_shared(self):
		m, lx = self.mac, self.linux
		mac_api = os.path.join(self.root, "mac", "Volumes", "SSD", "api")
		srv_api = os.path.join(self.root, "linux", "var", "www", "api")
		for d in (mac_api, srv_api):
			os.makedirs(d)
		sid = str(uuid.uuid4())
		write(m.session_path(mac_api, sid), jsonl([{"type": "user", "sessionId": sid, "cwd": mac_api}]))
		write(os.path.join(m.session_dir(mac_api), "memory", "MEMORY.md"), "mémoire du Mac\n")
		sid2 = str(uuid.uuid4())
		write(lx.session_path(srv_api, sid2), jsonl([{"type": "user", "sessionId": sid2, "cwd": srv_api}]))
		self._setup_both()
		m.sync()
		lx.sync()
		self.assertFalse(os.path.exists(lx.session_path(srv_api, sid)))
		self.assertFalse(os.path.exists(os.path.join(lx.session_dir(srv_api), "memory", "MEMORY.md")))
		self.assertFalse([k for k in self._manifest(m) if "api" in k])
		self.assertTrue(any("non partagés" in a for a in Controller(m.paths).status()["alerts"]))
		# Partagé explicitement des deux côtés sous le même nom : les conversations circulent.
		Controller(m.paths).name_project(mac_api, "api")
		Controller(lx.paths).name_project(srv_api, "api")
		m.sync()
		lx.sync()
		self.assertTrue(os.path.exists(lx.session_path(srv_api, sid)))
		self.assertEqual(self._read_lines(lx.session_path(srv_api, sid))[0]["cwd"], srv_api)

	def test_all_mcp_servers_vanishing_requires_confirmation(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		cj = read_json(m.paths.claude_json)
		cj["mcpServers"]["github"] = {"command": "gh-mcp"}
		write(m.paths.claude_json, json.dumps(cj))
		m.push()
		lx.pull()
		cj["mcpServers"] = {}
		write(m.paths.claude_json, json.dumps(cj))
		m.push_twice()
		lx.pull()
		self.assertEqual(sorted(read_json(lx.paths.claude_json)["mcpServers"]), ["fs", "github"])
		m.push(allow_deletions=True)
		lx.pull()
		self.assertEqual(read_json(lx.paths.claude_json)["mcpServers"], {})

	def test_undecodable_file_name_does_not_block_sync(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		bad = os.path.join(os.fsencode(lx.paths.claude_dir), b"skills", b"reel", b"caf\xe9.txt")
		with open(bad, "wb") as fh:
			fh.write(b"latin-1\n")
		write(os.path.join(lx.paths.claude_dir, "CLAUDE.md"), "modifié sur Linux\n")
		report = lx.push()
		self.assertTrue(any("encodage" in w for w in report.warnings))
		m.pull()
		with open(os.path.join(m.paths.claude_dir, "CLAUDE.md")) as fh:
			self.assertEqual(fh.read(), "modifié sur Linux\n")

	def test_symlinked_projects_folder(self):
		lx = self.linux
		elsewhere = os.path.join(self.root, "linux", "data", "claude-projects")
		os.makedirs(elsewhere)
		os.symlink(elsewhere, lx.paths.projects_dir)
		self._setup_both()
		m = self.mac
		self.assertTrue(os.path.exists(os.path.join(elsewhere, os.path.basename(lx.session_dir(lx.project)),
													self.sid + ".jsonl")))
		sid = str(uuid.uuid4())
		write(lx.session_path(lx.project, sid), jsonl([{"type": "user", "sessionId": sid, "cwd": lx.project}]))
		lx.push()
		m.pull()
		self.assertTrue(os.path.exists(m.session_path(m.project, sid)))

	def test_copied_configuration_gets_its_own_identity(self):
		self._setup_both()
		m = self.mac
		config = MachineConfig.load(m.paths)
		config.data["binding"] = "copie-d-une-autre-machine"
		config.save(m.paths)
		with self.assertRaises(CSyncError) as ctx:
			m.push()
		self.assertIn("csync setup", str(ctx.exception))
		old_id = config.machine_id
		m.setup(self.remote, self.key)
		self.assertNotEqual(MachineConfig.load(m.paths).machine_id, old_id)
		m.sync()
		self.linux.sync()

	def test_extensions_do_not_fill_backups(self):
		self._setup_both()
		m, lx = self.mac, self.linux

		def size(path):
			return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs) \
				if os.path.isdir(path) else 0
		before = size(lx.paths.backups_dir)
		for i in range(5):
			self._append(m.session_path(m.project, self.sid), {"type": "user", "sessionId": self.sid, "i": i,
															   "pad": "x" * 20000})
			m.push()
			lx.pull()
		self.assertEqual(size(lx.paths.backups_dir), before)
		self.assertEqual(self._read_lines(lx.session_path(lx.project, self.sid))[-1]["i"], 4)

	def test_hook_scripts_and_machine_specific_settings(self):
		m, lx = self.mac, self.linux
		s = read_json(m.paths.settings_file)
		s["hooks"] = {"PostToolUse": [{"hooks": [{"type": "command", "command": "~/.claude/hooks/format.sh"}]}]}
		s["statusLine"] = {"type": "command", "command": "bash ~/.claude/statusline-command.sh"}
		write(m.paths.settings_file, json.dumps(s, indent=2))
		write(os.path.join(m.paths.claude_dir, "hooks", "format.sh"), "#!/bin/sh\necho format\n", 0o755)
		write(os.path.join(m.paths.claude_dir, "statusline-command.sh"), "echo status\n")
		self._setup_both()
		self.assertTrue(os.access(os.path.join(lx.paths.claude_dir, "hooks", "format.sh"), os.X_OK))
		self.assertTrue(os.path.exists(os.path.join(lx.paths.claude_dir, "statusline-command.sh")))
		# Le serveur garde ses propres hooks et son propre serveur MCP « fs ».
		for args in (["local", "settings", "hooks"], ["local", "mcp", "fs"]):
			self.assertEqual(self._cli(lx, args).returncode, 0)
		s = read_json(lx.paths.settings_file)
		s["hooks"] = {"Stop": [{"hooks": [{"type": "command", "command": "echo serveur"}]}]}
		write(lx.paths.settings_file, json.dumps(s, indent=2))
		cj = read_json(lx.paths.claude_json)
		cj["mcpServers"]["fs"] = {"command": "/usr/bin/fs-linux"}
		write(lx.paths.claude_json, json.dumps(cj))
		lx.push_twice()
		m.pull()
		self.assertIn("PostToolUse", read_json(m.paths.settings_file)["hooks"], "les hooks du Mac ont été remplacés")
		self.assertEqual(read_json(m.paths.claude_json)["mcpServers"]["fs"]["command"], "npx")
		s = read_json(m.paths.settings_file)
		s["model"] = "opus"
		s["hooks"]["Notification"] = [{"hooks": [{"type": "command", "command": "afplay x.aiff"}]}]
		write(m.paths.settings_file, json.dumps(s, indent=2))
		m.push()
		lx.pull()
		s = read_json(lx.paths.settings_file)
		self.assertEqual(s["model"], "opus")
		self.assertEqual(list(s["hooks"]), ["Stop"])
		self.assertEqual(read_json(lx.paths.claude_json)["mcpServers"]["fs"]["command"], "/usr/bin/fs-linux")
		self.assertEqual(lx.state().conflicts, [])
		self.assertEqual(m.state().conflicts, [])

	def test_moved_project_is_found_again(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		new_place = os.path.join(m.home, "work", "monprojet")
		os.makedirs(os.path.dirname(new_place))
		shutil.move(m.project, new_place)
		sid = str(uuid.uuid4())
		write(lx.session_path(lx.project, sid), jsonl([{"type": "user", "sessionId": sid, "cwd": lx.project}]))
		lx.push()
		m.sync()
		target = m.session_path(new_place, sid)
		self.assertTrue(os.path.exists(target))
		self.assertEqual(self._read_lines(target)[0]["cwd"], new_place)

	def test_lost_state_and_reinstall(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		self._append(m.session_path(m.project, self.sid),
					 {"type": "user", "sessionId": self.sid, "message": {"content": "ls %s" % lx.project}})
		m.push()
		lx.pull()
		for f in (lx.paths.state_file, lx.paths.state_file + ".bak"):
			os.remove(f)
		self.assertFalse(lx.sync()[0].counts.get("sessions_conflict"))
		self.assertFalse(m.sync()[0].counts.get("sessions_conflict"))
		# Réinstallation complète du Mac, puis retrait de son ancienne identité.
		old_id = MachineConfig.load(m.paths).machine_id
		shutil.rmtree(m.paths.csync_dir)
		m.setup(self.remote, self.key)
		self.assertEqual(m.state().conflicts, [])
		r = self._cli(m, ["forget-machine", old_id])
		self.assertEqual(r.returncode, 1, "une machine active récemment ne doit pas être retirée sans --force")
		r = self._cli(m, ["forget-machine", old_id, "--force"])
		self.assertEqual(r.returncode, 0, r.stderr)
		names = [x["name"] for x in Controller(lx.paths).status()["machines"]]
		self.assertEqual(names, ["mac"])
		lx.sync()
		self.assertEqual(lx.state().conflicts, [])

	def test_compaction_keeps_resolved_conflicts_resolved(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		self._append(m.session_path(m.project, self.sid), {"type": "user", "sessionId": self.sid, "uuid": "mac"})
		self._append(lx.session_path(lx.project, self.sid), {"type": "user", "sessionId": self.sid, "uuid": "lx"})
		lx.push()
		m.sync()
		cid = m.state().conflicts[0]["id"]
		Controller(m.paths).resolve_conflicts([cid])
		for i in range(3):
			self._append(lx.session_path(lx.project, self.sid), {"type": "user", "sessionId": self.sid, "i": i})
			lx.sync()
		ops.do_compact(lx.paths)
		report = m.sync()[0]
		self.assertFalse(report.counts.get("sessions_conflict"))

	def test_mode_change_propagates(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		rel = os.path.join("skills", "reel", "scripts", "build.sh")
		write(os.path.join(m.paths.claude_dir, rel), "#!/bin/sh\n")
		m.sync()
		lx.sync()
		os.chmod(os.path.join(m.paths.claude_dir, rel), 0o755)
		m.sync()
		lx.sync()
		self.assertTrue(os.access(os.path.join(lx.paths.claude_dir, rel), os.X_OK))

	def test_settings_deleted_elsewhere_keeps_local_hooks(self):
		self.mac.setup(self.remote, self.key, create_key=True, hooks=True)
		self.linux.setup(self.remote, self.key, hooks=True)
		m, lx = self.mac, self.linux
		os.remove(m.paths.settings_file)
		m.push_twice()
		lx.pull()
		self.assertTrue(hooks_installed(lx.paths))
		self.assertNotIn("permissions", read_json(lx.paths.settings_file))
		lx.sync()
		m.sync()
		self.assertFalse(os.path.exists(m.paths.settings_file), "settings.json réapparu sur le Mac")

	# -- Troisième relecture ------------------------------------------------------------------------

	def test_session_in_root_or_disk_folder_does_not_block_projects(self):
		m, lx = self.mac, self.linux
		disk = os.path.join(self.root, "mac", "Volumes", "SSD")
		app = os.path.join(disk, "app")
		os.makedirs(app)
		git("init", "-q", cwd=app)
		git("remote", "add", "origin", "https://github.com/alex/app.git", cwd=app)
		for cwd in ("/", disk, os.path.dirname(m.home)):
			sid = str(uuid.uuid4())
			write(m.session_path(cwd, sid), jsonl([{"type": "user", "sessionId": sid, "cwd": cwd}]))
		sid_app = str(uuid.uuid4())
		write(m.session_path(app, sid_app), jsonl([{"type": "user", "sessionId": sid_app, "cwd": app}]))
		clone = os.path.join(lx.home, "code", "app")
		os.makedirs(clone)
		git("init", "-q", cwd=clone)
		git("remote", "add", "origin", "git@github.com:alex/app.git", cwd=clone)
		self._setup_both()
		keys = [p["key"] for p in read_json(m.paths.profile_file)["projects"]]
		self.assertNotIn("unconfirmed:/", keys)
		self.assertTrue(os.path.exists(lx.session_path(clone, sid_app)), "projet git sur disque externe bloqué")
		self.assertTrue(os.path.exists(lx.session_path(lx.project, self.sid)))
		new_sid = str(uuid.uuid4())
		write(m.session_path(m.project, new_sid), jsonl([{"type": "user", "sessionId": new_sid, "cwd": m.project}]))
		m.push()
		lx.pull()
		self.assertTrue(os.path.exists(lx.session_path(lx.project, new_sid)))

	def test_forgetting_a_live_machine_heals_itself(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		write(os.path.join(lx.paths.claude_dir, "agents", "x.md"), "x\n")
		lx.pull()          # calcule et enregistre localement sans envoyer
		ops.do_forget_machine(m.paths, "linux", force=True)
		lx.sync()
		lx.sync()
		m.pull()
		self.assertTrue(os.path.exists(os.path.join(m.paths.claude_dir, "agents", "x.md")))
		self.assertEqual([x["name"] for x in Controller(m.paths).status()["machines"]], ["linux"])

	def test_exec_bit_is_never_stripped(self):
		m, lx = self.mac, self.linux
		script = os.path.join("skills", "reel", "scripts", "run.sh")
		# Même contenu des deux côtés, exécutable seulement sur le Mac.
		write(os.path.join(lx.paths.claude_dir, script), "#!/bin/sh\necho ok\n", 0o644)
		self._setup_both()
		for _ in range(3):
			m.sync()
			lx.sync()
		self.assertTrue(os.stat(os.path.join(m.paths.claude_dir, script)).st_mode & 0o100)
		# Le serveur retire le droit d'exécution (ou son disque est monté sans exécution) : le Mac garde le sien.
		os.chmod(os.path.join(lx.paths.claude_dir, script), 0o644)
		write(os.path.join(lx.paths.claude_dir, script), "#!/bin/sh\necho modifié\n")
		lx.sync()
		m.sync()
		path = os.path.join(m.paths.claude_dir, script)
		with open(path) as fh:
			self.assertEqual(fh.read(), "#!/bin/sh\necho modifié\n")
		self.assertTrue(os.stat(path).st_mode & 0o100)

	def test_settings_deletion_keeps_local_keys(self):
		self.mac.setup(self.remote, self.key, create_key=True, hooks=True)
		self.linux.setup(self.remote, self.key, hooks=True)
		m, lx = self.mac, self.linux
		self.assertEqual(self._cli(lx, ["local", "settings", "env"]).returncode, 0)
		s = read_json(lx.paths.settings_file)
		s["env"] = {"TOKEN_SERVEUR": "1"}
		write(lx.paths.settings_file, json.dumps(s, indent=2))
		lx.push()
		m.pull()
		os.remove(m.paths.settings_file)
		m.push_twice()
		lx.pull()
		s = read_json(lx.paths.settings_file)
		self.assertEqual(s.get("env"), {"TOKEN_SERVEUR": "1"})
		self.assertNotIn("permissions", s)
		self.assertTrue(hooks_installed(lx.paths))
		lx.sync()
		m.sync()
		self.assertFalse(os.path.exists(m.paths.settings_file))

	def test_unmounted_project_comes_back(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		hidden = m.project + ".démonté"
		os.rename(m.project, hidden)       # disque débranché
		m.sync()
		lx.sync()
		os.rename(hidden, m.project)       # rebranché
		sid = str(uuid.uuid4())
		write(lx.session_path(lx.project, sid), jsonl([{"type": "user", "sessionId": sid, "cwd": lx.project}]))
		lx.push()
		m.sync()
		target = m.session_path(m.project, sid)
		self.assertTrue(os.path.exists(target))
		self.assertEqual(self._read_lines(target)[0]["cwd"], m.project)

	def test_new_identity_keeps_project_choices(self):
		self._setup_both()
		m = self.mac
		ctrl = Controller(m.paths)
		ctrl.apply_scan(ctrl.rescan(), {m.project: {"include": False}})
		config = MachineConfig.load(m.paths)
		config.data["binding"] = "copie"
		config.save(m.paths)
		m.setup(self.remote, self.key)
		self.assertEqual([p["include"] for p in read_json(m.paths.profile_file)["projects"]
						  if p["path"] == m.project], [False])
		m.push()
		self.assertFalse([k for k in self._manifest(m) if "monprojet" in k])

	def test_resolved_divergence_keeps_later_remote_work_visible(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		mp, lp = m.session_path(m.project, self.sid), lx.session_path(lx.project, self.sid)
		self._append(mp, {"type": "user", "sessionId": self.sid, "uuid": "mac-1"})
		self._append(lp, {"type": "user", "sessionId": self.sid, "uuid": "lx-1"})
		m.push()
		lx.sync()
		copy = lx.state().conflicts[0]["copy"]
		os.remove(copy)                    # « je garde ma version »
		lx.sync()
		self.assertEqual(lx.state().conflicts, [])
		self._append(mp, {"type": "user", "sessionId": self.sid, "uuid": "mac-2"})
		m.push()
		lx.sync()
		self.assertEqual(lx.state().conflicts, [], "pas de nouveau conflit à traiter")
		copies = [f for f in os.listdir(os.path.dirname(lp)) if f.endswith(".jsonl") and self.sid not in f]
		found = [c for c in copies if self._read_lines(os.path.join(os.path.dirname(lp), c))[-1].get("uuid") == "mac-2"]
		self.assertEqual(len(found), 1, "la suite écrite sur le Mac doit rester consultable ici")
		m.push()
		lx.sync()
		self.assertEqual(len([f for f in os.listdir(os.path.dirname(lp)) if f.endswith(".jsonl")]), len(copies) + 1)

	# -- Suppression des conversations ---------------------------------------------------------

	def test_deletion_waits_while_conversation_is_open_elsewhere(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		os.remove(m.session_path(m.project, self.sid))
		m.push_twice()
		marker = os.path.join(lx.paths.claude_sessions_dir, "%d.json" % os.getpid())
		write(marker, json.dumps({"sessionId": self.sid}))
		lx.pull()
		self.assertTrue(os.path.exists(lx.session_path(lx.project, self.sid)))
		os.remove(marker)
		lx.pull()
		self.assertFalse(os.path.exists(lx.session_path(lx.project, self.sid)))

	def test_conversation_continued_elsewhere_survives_deletion(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		os.remove(m.session_path(m.project, self.sid))
		m.push_twice()
		self._append(lx.session_path(lx.project, self.sid), {"type": "user", "sessionId": self.sid, "uuid": "suite"})
		lx.sync()
		self.assertTrue(os.path.exists(lx.session_path(lx.project, self.sid)))
		m.pull()
		self.assertEqual(self._read_lines(m.session_path(m.project, self.sid))[-1]["uuid"], "suite")

	def test_all_conversations_vanishing_requires_confirmation(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		shutil.rmtree(m.paths.projects_dir)
		os.makedirs(m.paths.projects_dir)
		report = m.push_twice()
		self.assertTrue(any("allow-deletions" in w for w in report.warnings))
		lx.pull()
		self.assertTrue(os.path.exists(lx.session_path(lx.project, self.sid)))


class StateJournal(unittest.TestCase):
	def test_journal_is_replayed_then_cleared(self):
		d = tempfile.mkdtemp()
		try:
			from csync.paths import Paths
			paths = Paths(csync_dir=d, home=d, claude_dir=os.path.join(d, ".claude"), claude_json=os.path.join(d, "cj"))
			st = State.load(paths)
			st.entry("k")["chunks"] = ["a"]
			st.save(paths)
			st = State.load(paths)
			st.journal("k", {"chunks": ["a", "b"], "local_len": 10})
			with open(paths.state_file + ".journal", "a") as fh:
				fh.write('{"key": "k", "st": {"chunks": ["tronqu')     # dernière ligne incomplète
			again = State.load(paths)
			self.assertEqual(again.entries["k"]["chunks"], ["a", "b"])
			again.save(paths)
			self.assertFalse(os.path.exists(paths.state_file + ".journal"))
			self.assertEqual(State.load(paths).entries["k"]["local_len"], 10)
		finally:
			shutil.rmtree(d)


if __name__ == "__main__":
	unittest.main()
