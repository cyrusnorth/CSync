"""Synchronisation des fichiers : configuration de Syncthing par CSync, avec un faux Syncthing.

Lancement : python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from csync import files, ops
from csync.controller import Controller
from csync.paths import MachineConfig

import test_csync  # noqa: F401
from test_csync import Harness, write


class FakeSyncthing:
	"""Imite l'interface REST de Syncthing utilisée par CSync."""

	def __init__(self, device_id: str, api_key: str = "cle-de-test"):
		self.device_id = device_id
		self.api_key = api_key
		self.config = {"devices": [{"deviceID": device_id, "name": "moi"}],
					   "folders": [{"id": "default", "path": "", "devices": [{"deviceID": device_id}]}]}
		self.ignores = {}
		self.connected = set()
		fake = self

		class Handler(BaseHTTPRequestHandler):
			def log_message(self, *args):
				pass

			def _send(self, code, obj=None):
				body = json.dumps(obj).encode() if obj is not None else b""
				self.send_response(code)
				self.send_header("Content-Type", "application/json")
				self.send_header("Content-Length", str(len(body)))
				self.end_headers()
				self.wfile.write(body)

			def _body(self):
				n = int(self.headers.get("Content-Length") or 0)
				return json.loads(self.rfile.read(n).decode()) if n else None

			def _route(self, method):
				if self.headers.get("X-API-Key") != fake.api_key:
					return self._send(403, {"error": "clé"})
				url = urllib.parse.urlparse(self.path)
				q = dict(urllib.parse.parse_qsl(url.query))
				p = url.path
				if method == "GET" and p == "/rest/system/status":
					return self._send(200, {"myID": fake.device_id})
				if method == "GET" and p == "/rest/config":
					return self._send(200, fake.config)
				if method == "GET" and p == "/rest/config/defaults/device":
					return self._send(200, {"compression": "metadata", "addresses": ["dynamic"]})
				if method == "GET" and p == "/rest/config/defaults/folder":
					return self._send(200, {"type": "sendreceive", "rescanIntervalS": 3600})
				if method == "PUT" and p.startswith("/rest/config/devices/"):
					dev = self._body()
					fake.config["devices"] = [d for d in fake.config["devices"] if d["deviceID"] != dev["deviceID"]] + [dev]
					return self._send(200)
				if method == "PUT" and p.startswith("/rest/config/folders/"):
					folder = self._body()
					fake.config["folders"] = [f for f in fake.config["folders"] if f["id"] != folder["id"]] + [folder]
					return self._send(200)
				if method == "DELETE" and p.startswith("/rest/config/folders/"):
					fid = urllib.parse.unquote(p.rsplit("/", 1)[1])
					fake.config["folders"] = [f for f in fake.config["folders"] if f["id"] != fid]
					return self._send(200)
				if method == "POST" and p == "/rest/db/ignores":
					fake.ignores[q["folder"]] = self._body()["ignore"]
					return self._send(200)
				if method == "GET" and p == "/rest/system/connections":
					return self._send(200, {"connections": {d: {"connected": True} for d in fake.connected}})
				if method == "GET" and p == "/rest/db/completion":
					return self._send(200, {"completion": 100, "needItems": 0, "needDeletes": 0})
				if method == "GET" and p == "/rest/db/status":
					return self._send(200, {"needFiles": 0, "needDeletes": 0, "state": "idle"})
				return self._send(404, {"error": p})

			def do_GET(self):
				self._route("GET")

			def do_PUT(self):
				self._route("PUT")

			def do_POST(self):
				self._route("POST")

			def do_DELETE(self):
				self._route("DELETE")

		self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
		self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
		self.thread.start()

	@property
	def address(self):
		return "127.0.0.1:%d" % self.server.server_address[1]

	def write_config_xml(self, home):
		write(os.path.join(home, "Library", "Application Support", "Syncthing", "config.xml"),
			  '<configuration version="37"><gui enabled="true" tls="false"><address>%s</address>'
			  '<apikey>%s</apikey></gui></configuration>\n' % (self.address, self.api_key))

	def stop(self):
		self.server.shutdown()
		self.server.server_close()

	def folder(self, fid):
		return next(f for f in self.config["folders"] if f["id"] == fid)

	def device_ids(self):
		return {d["deviceID"] for d in self.config["devices"]}


class FilesSync(Harness):

	def setUp(self):
		super().setUp()
		self.st_mac = FakeSyncthing("MAC-ID")
		self.st_lx = FakeSyncthing("LINUX-ID")
		self.st_mac.write_config_xml(self.mac.home)
		self.st_lx.write_config_xml(self.linux.home)

	def tearDown(self):
		self.st_mac.stop()
		self.st_lx.stop()
		super().tearDown()

	def _files_setup(self, machine):
		"""Ce que fait « csync files setup », sans installation (Syncthing est déjà là)."""
		eng = ops.engine(machine.paths)
		eng.pull()
		changes = files.setup(machine.paths, eng.store.other_profiles(), st=files.api(machine.paths))
		eng.config = MachineConfig.load(machine.paths)
		eng.push()
		return changes

	def test_both_machines_pair_without_any_manual_step(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		self._files_setup(m)
		self.assertEqual(self.st_mac.device_ids(), {"MAC-ID"}, "aucune autre machine connue à ce stade")
		self._files_setup(lx)
		self.assertIn("MAC-ID", self.st_lx.device_ids())
		m.sync()   # la synchronisation ordinaire relie le Mac à la machine apparue
		self.assertIn("LINUX-ID", self.st_mac.device_ids())
		for fake, home, own, other in ((self.st_mac, m.home, "MAC-ID", "LINUX-ID"),
									   (self.st_lx, lx.home, "LINUX-ID", "MAC-ID")):
			folder = fake.folder("csync-claude")
			self.assertEqual(folder["path"], os.path.join(home, "Claude"))
			self.assertEqual({d["deviceID"] for d in folder["devices"]}, {own, other})
			self.assertEqual(folder["versioning"]["type"], "trashcan")
			self.assertEqual(folder["versioning"]["params"]["cleanoutDays"], "30")
			scratch = fake.folder("csync-claude-desktop")
			self.assertEqual(scratch["path"],
							 os.path.join(home, "Library", "Application Support", "Claude", "scratch-workspaces"))
			self.assertIn("(?d).DS_Store", fake.ignores["csync-claude"])
			self.assertTrue(os.path.isdir(os.path.join(home, "Claude")))
			self.assertFalse([f for f in fake.config["folders"] if f["id"] == "default"],
							 "dossier « Sync » par défaut laissé en place")

	def test_status_reports_files(self):
		self._setup_both()
		m, lx = self.mac, self.linux
		self._files_setup(m)
		self._files_setup(lx)
		m.sync()
		status = Controller(m.paths).status()
		self.assertIn("Fichiers : linux hors ligne (les changements partiront à sa prochaine connexion).",
					  status["files"]["lines"])
		self.st_mac.connected.add("LINUX-ID")
		status = Controller(m.paths).status()
		self.assertEqual(status["files"]["lines"], ["Fichiers : à jour avec linux."])

	def test_stopped_syncthing_does_not_break_conversation_sync(self):
		self._setup_both()
		m = self.mac
		self._files_setup(m)
		self.st_mac.stop()
		m.sync()   # ne lève pas d'erreur
		status = Controller(m.paths).status()
		self.assertTrue(any("Syncthing ne répond pas" in a for a in status["alerts"]))
		self.st_mac = FakeSyncthing("MAC-ID")   # pour tearDown
