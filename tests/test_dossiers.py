"""Dossiers de travail absents, reprise et déplacement (conception du 9 novembre 2026).

Lancement : python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import uuid

from csync import ops
from csync.controller import Controller
from csync.paths import MachineConfig
from csync.sync import Engine
from csync.util import CSyncError

import test_csync  # noqa: F401  (prépare l'environnement des tests)
from test_csync import Harness, jsonl, write


class MissingWorkDir(Harness):

	def _session(self, machine, store_cwd, lines_cwds, sid=None):
		sid = sid or str(uuid.uuid4())
		write(machine.session_path(store_cwd, sid),
			  jsonl([{"type": "user", "sessionId": sid, "cwd": c, "uuid": "l%d" % i} for i, c in enumerate(lines_cwds)]))
		return sid

	def test_missing_home_folder_is_created(self):
		"""Le cas du 8 novembre : dossier créé sur une machine, absent de l'autre."""
		self._setup_both()
		m, lx = self.mac, self.linux
		mac_dir = os.path.join(m.home, "Claude", "Psychologie des gens")
		os.makedirs(mac_dir)
		sid = self._session(m, mac_dir, [mac_dir])
		m.push()
		reports = lx.sync()
		lx_dir = os.path.join(lx.home, "Claude", "Psychologie des gens")
		self.assertTrue(os.path.isdir(lx_dir), "dossier de travail non créé")
		self.assertTrue(os.path.exists(lx.session_path(lx_dir, sid)))
		self.assertIn(lx_dir, reports[0].created_dirs)
		self.assertIn(lx_dir, Controller(lx.paths).status()["created_dirs"])

	def test_last_folder_of_a_moved_session_is_created(self):
		"""Session commencée dans un dossier temporaire puis poursuivie dans un autre (app Desktop)."""
		self._setup_both()
		m, lx = self.mac, self.linux
		scratch = os.path.join(m.home, "Library", "Application Support", "Claude", "scratch-1")
		final = os.path.join(m.home, "Claude", "Sujet")
		os.makedirs(scratch)
		os.makedirs(final)
		self._session(m, scratch, [scratch])
		sid = self._session(m, scratch, [scratch, final, final])
		m.push()
		lx.sync()
		lx_scratch = os.path.join(lx.home, "Library", "Application Support", "Claude", "scratch-1")
		lx_final = os.path.join(lx.home, "Claude", "Sujet")
		self.assertTrue(os.path.isdir(lx_scratch))
		self.assertTrue(os.path.isdir(lx_final), "dernier dossier de travail non créé")
		self.assertTrue(os.path.exists(lx.session_path(lx_scratch, sid)))

	def test_dry_run_creates_nothing(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		mac_dir = os.path.join(m.home, "Claude", "Essai")
		os.makedirs(mac_dir)
		self._session(m, mac_dir, [mac_dir])
		m.push()
		Engine(lx.paths, MachineConfig.load(lx.paths)).store.fetch_and_rebase()   # un essai à blanc ne télécharge rien
		report = lx.pull(dry_run=True)
		lx_dir = os.path.join(lx.home, "Claude", "Essai")
		self.assertFalse(os.path.exists(lx_dir))
		self.assertIn(lx_dir, report.created_dirs)

	def test_forbidden_places_are_not_created(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		trash = os.path.join(m.home, ".Trash", "vieux")
		os.makedirs(trash)
		self._session(m, trash, [trash])
		m.push()
		lx.sync()
		self.assertFalse(os.path.exists(os.path.join(lx.home, ".Trash", "vieux")))

	def test_missing_git_project_is_not_replaced_by_an_empty_folder(self):
		"""Un projet git absent (disque débranché, autre emplacement) : pas de dossier vide à sa place."""
		self._setup_both()
		m, lx = self.mac, self.linux
		hidden = lx.project + ".démonté"
		os.rename(lx.project, hidden)
		sid = self._session(m, m.project, [m.project])
		m.push()
		lx.sync()
		self.assertFalse(os.path.exists(lx.project), "dossier vide créé à la place du projet git")
		os.rename(hidden, lx.project)
		del sid


class ResumeAndMove(Harness):

	def _session(self, machine, cwd, sid=None, n=2):
		sid = sid or str(uuid.uuid4())
		write(machine.session_path(cwd, sid),
			  jsonl([{"type": "user", "sessionId": sid, "cwd": cwd, "uuid": "l%d" % i} for i in range(n)]))
		return sid

	def test_resume_starts_claude_in_the_session_folder(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		folder = os.path.join(m.home, "Claude", "Reprise")
		os.makedirs(folder)
		sid = self._session(m, folder)
		m.push()
		out = os.path.join(self.root, "claude-args.txt")
		fake_bin = os.path.join(self.root, "fake-bin")
		write(os.path.join(fake_bin, "claude"), "#!/bin/sh\npwd > %s\necho \"$@\" >> %s\n" % (out, out), 0o755)
		res = self._cli(lx, ["resume", sid[:8]],
						extra_env={"PATH": fake_bin + os.pathsep + os.environ.get("PATH", "")})
		self.assertEqual(res.returncode, 0, res.stderr)
		with open(out) as fh:
			cwd, argv = fh.read().splitlines()
		self.assertEqual(os.path.realpath(cwd), os.path.realpath(os.path.join(lx.home, "Claude", "Reprise")))
		self.assertEqual(argv, "--resume " + sid)

	def test_move_keeps_conversations_resumable_on_both_machines(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		old = os.path.join(m.home, "Claude", "Ancien")
		sub = os.path.join(old, "sous")
		os.makedirs(sub)
		write(os.path.join(old, "script.md"), "texte\n")
		sid = self._session(m, old)
		sid_sub = self._session(m, sub)
		write(os.path.join(m.session_dir(old), "memory", "MEMORY.md"), "# mémoire\n")
		m.push()
		lx.sync()
		lx_old = os.path.join(lx.home, "Claude", "Ancien")
		self.assertTrue(os.path.exists(lx.session_path(lx_old, sid)))

		os.makedirs(os.path.join(m.home, "Claude", "Projects"))
		new = os.path.join(m.home, "Claude", "Projects", "Nouveau")
		result = ops.do_move(m.paths, old, new)
		self.assertEqual(result["sessions"], 2)
		self.assertFalse(os.path.exists(old))
		self.assertTrue(os.path.exists(os.path.join(new, "script.md")))
		moved = m.session_path(new, sid)
		self.assertTrue(os.path.exists(moved))
		self.assertEqual({l["cwd"] for l in self._read_lines(moved)}, {new})
		self.assertEqual({l["cwd"] for l in self._read_lines(m.session_path(os.path.join(new, "sous"), sid_sub))},
						 {os.path.join(new, "sous")})
		self.assertTrue(os.path.exists(os.path.join(m.session_dir(new), "memory", "MEMORY.md")))
		self.assertFalse(os.path.exists(m.session_dir(old)))

		lx.sync()
		lx_new = os.path.join(lx.home, "Claude", "Projects", "Nouveau")
		self.assertTrue(os.path.exists(lx.session_path(lx_new, sid)), "conversation absente au nouvel emplacement")
		self.assertEqual({l["cwd"] for l in self._read_lines(lx.session_path(lx_new, sid))}, {lx_new})
		self.assertTrue(os.path.isdir(lx_new))
		self.assertFalse(os.path.exists(lx.session_path(lx_old, sid)), "ancienne copie restée sur l'autre machine")
		# Les machines restent d'accord ensuite (pas de conflit, pas de retour de l'ancienne copie).
		m.sync()
		lx.sync()
		self.assertFalse(os.path.exists(m.session_path(old, sid)))
		self.assertFalse(lx.state().conflicts)
		self.assertFalse(m.state().conflicts)

	def test_move_refuses_when_a_conversation_is_open(self):
		self._setup_both()
		m = self.mac
		old = os.path.join(m.home, "Claude", "Ouvert")
		os.makedirs(old)
		sid = self._session(m, old)
		write(os.path.join(m.paths.claude_sessions_dir, "%d.json" % os.getpid()), '{"sessionId": "%s"}' % sid)
		with self.assertRaises(CSyncError):
			ops.do_move(m.paths, old, os.path.join(m.home, "Claude", "Ailleurs"))
		self.assertTrue(os.path.isdir(old))

	def test_move_refuses_an_existing_destination(self):
		self._setup_both()
		m = self.mac
		old = os.path.join(m.home, "Claude", "A")
		new = os.path.join(m.home, "Claude", "B")
		os.makedirs(old)
		os.makedirs(new)
		with self.assertRaises(CSyncError):
			ops.do_move(m.paths, old, new)
