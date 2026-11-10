"""Barre latérale de l'app Claude : fiches de session (conception du 10 novembre 2026).

Lancement : python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import uuid

from csync import ops
from csync.appcards import card_dirs, card_id_for, read_cards
from csync.controller import Controller

import test_csync  # noqa: F401  (prépare l'environnement des tests)
from test_csync import Harness, jsonl, write


def cards_dir(machine):
	return os.path.join(machine.home, "Library", "Application Support", "Claude", "claude-code-sessions",
						"org", "compte")


def add_card(machine, sid, cwd, title, **extra):
	card = {"sessionId": "local_" + str(uuid.uuid4()), "cliSessionId": sid, "cwd": cwd, "originCwd": cwd,
			"createdAt": 1000, "lastActivityAt": 2000, "model": "claude-opus-5-5", "effort": "medium",
			"isArchived": False, "title": title, "titleSource": "auto",
			"remoteMcpServersConfig": [{"headers": {"Authorization": "Bearer secret"}}]}
	card.update(extra)
	write(os.path.join(cards_dir(machine), card["sessionId"] + ".json"), json.dumps(card))
	return card


def cards_by_cli(machine):
	out = {}
	for d in card_dirs(machine.paths):
		for _cid, (path, card) in read_cards(d).items():
			out[card["cliSessionId"]] = (path, card)
	return out


class AppCards(Harness):

	def _session(self, machine, cwd, sid=None):
		sid = sid or str(uuid.uuid4())
		write(machine.session_path(cwd, sid), jsonl([{"type": "user", "sessionId": sid, "cwd": cwd, "uuid": "a"}]))
		return sid

	def _received_session(self):
		"""Conversation et fiche créées sur le Mac, reçues sur l'autre machine."""
		self._setup_both()
		m, lx = self.mac, self.linux
		os.makedirs(cards_dir(lx))   # l'app est installée sur les deux machines
		mac_dir = os.path.join(m.home, "Claude", "Projects", "Psychologie")
		os.makedirs(mac_dir)
		sid = self._session(m, mac_dir)
		add_card(m, sid, mac_dir, "Psychologie des gens bordéliques")
		m.sync()
		return sid, os.path.join(lx.home, "Claude", "Projects", "Psychologie")

	def test_received_conversation_gets_a_card(self):
		sid, lx_dir = self._received_session()
		lx = self.linux
		lx.sync()
		lx.sync()
		cards = cards_by_cli(lx)
		self.assertIn(sid, cards, "aucune fiche créée pour la conversation reçue")
		path, card = cards[sid]
		self.assertEqual(card["cwd"], lx_dir)
		self.assertEqual(card["title"], "Psychologie des gens bordéliques")
		self.assertEqual(card["sessionId"], card_id_for(sid))
		self.assertEqual(os.path.basename(path), card_id_for(sid) + ".json")
		self.assertNotIn("remoteMcpServersConfig", card)
		with open(path) as fh:
			self.assertNotIn("secret", fh.read())

	def test_settings_never_leave_the_machine(self):
		sid, _lx_dir = self._received_session()
		self.linux.sync()
		cards = ops.engine(self.linux.paths).store.other_cards()
		published = json.dumps(cards)
		self.assertIn("Psychologie des gens", published)
		self.assertNotIn("secret", published)
		self.assertNotIn("cwd", published)

	def test_nothing_written_while_the_app_is_open(self):
		sid, _lx_dir = self._received_session()
		lx = self.linux
		os.environ["CSYNC_ASSUME_APP_RUNNING"] = "1"
		try:
			lx.sync()
			lx.sync()
		finally:
			os.environ["CSYNC_ASSUME_APP_RUNNING"] = "0"
		self.assertNotIn(sid, cards_by_cli(lx))
		alerts = " ".join(Controller(lx.paths).status()["alerts"])
		self.assertIn("barre latérale", alerts)
		lx.sync()
		self.assertIn(sid, cards_by_cli(lx))
		self.assertNotIn("barre latérale", " ".join(Controller(lx.paths).status()["alerts"]))

	def test_removed_card_is_not_recreated(self):
		sid, _lx_dir = self._received_session()
		lx = self.linux
		lx.sync()
		lx.sync()
		path, _card = cards_by_cli(lx)[sid]
		os.remove(path)
		lx.sync()
		self.assertNotIn(sid, cards_by_cli(lx))

	def test_existing_card_is_left_alone(self):
		"""Une conversation qui a déjà sa fiche ici n'en reçoit pas une seconde."""
		sid, lx_dir = self._received_session()
		lx = self.linux
		add_card(lx, sid, lx_dir, "Ma fiche")
		lx.sync()
		lx.sync()
		titles = [c["title"] for d in card_dirs(lx.paths) for _p, c in read_cards(d).values() if c["cliSessionId"] == sid]
		self.assertEqual(titles, ["Ma fiche"])

	def test_card_follows_a_moved_folder(self):
		self._setup_both()
		m = self.mac
		old = os.path.join(m.home, "Claude", "Ancien")
		new = os.path.join(m.home, "Claude", "Projects", "Nouveau")
		os.makedirs(old)
		os.makedirs(os.path.dirname(new))
		sid = self._session(m, old)
		add_card(m, sid, old, "Sujet", gitAnchorsFolderRealpath=old)
		m.sync()
		ops.do_move(m.paths, old, new)
		_path, card = cards_by_cli(m)[sid]
		self.assertEqual(card["cwd"], new)
		self.assertEqual(card["originCwd"], new)
		self.assertEqual(card["gitAnchorsFolderRealpath"], os.path.realpath(new))
		self.assertIn("remoteMcpServersConfig", card, "le reste de la fiche doit rester intact")

	def test_no_app_no_cards(self):
		"""Sans l'app Desktop, rien n'est créé."""
		sid, _lx_dir = self._received_session()
		lx = self.linux
		os.rmdir(cards_dir(lx))
		lx.sync()
		lx.sync()
		self.assertEqual(cards_by_cli(lx), {})
