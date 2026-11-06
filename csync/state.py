"""État local de la synchronisation (state.json) : ce que CSync sait de chaque élément."""

from __future__ import annotations

import uuid
from typing import Any, Dict, Iterable, List, Optional

from .paths import Paths
from .util import CSyncError, read_json, write_json

STATE_FORMAT = 2
HIST_MAX = 40


class State:
	def __init__(self, data: Dict[str, Any]):
		self.data = data
		self.data.setdefault("format", STATE_FORMAT)
		self.data.setdefault("entries", {})
		self.data.setdefault("conflicts", [])
		self.data.setdefault("parked", [])
		self.data.setdefault("pending_mcp", [])
		self.data.setdefault("counter", 0)
		self.data.setdefault("conflict_copies", {})

	@classmethod
	def load(cls, paths: Paths) -> "State":
		import json
		import os
		try:
			data = read_json(paths.state_file, None)
		except CSyncError:
			raise CSyncError("L'état local (%s) est illisible et rien n'a été modifié. Remplacez-le par sa copie "
							 "%s.bak si elle existe ; sinon supprimez-le : CSync reconstruira son état au prochain "
							 "passage (les éléments modifiés des deux côtés donneront alors des copies de conflit, "
							 "jamais de perte)." % (paths.state_file, paths.state_file))
		if not isinstance(data, dict):
			data = {}
		state = cls(data)
		# Écritures faites lors d'une opération interrompue avant l'enregistrement de l'état
		# (arrêt brutal, coupure) : on les reporte, pour que l'état décrive bien les fichiers.
		journal = paths.state_file + ".journal"
		try:
			with open(journal, "r", encoding="utf-8") as fh:
				for line in fh:
					try:
						item = json.loads(line)
					except ValueError:
						continue   # dernière ligne incomplète
					if isinstance(item, dict) and isinstance(item.get("st"), dict) and item.get("key"):
						state.entry(item["key"]).update(item["st"])
		except FileNotFoundError:
			pass
		except OSError:
			pass
		state._journal = journal
		return state

	def journal(self, key: str, fields: Dict[str, Any]) -> None:
		"""Note immédiatement sur disque une écriture locale qui vient d'avoir lieu."""
		import json
		import os
		path = getattr(self, "_journal", None)
		if not path:
			return
		try:
			fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
			try:
				os.write(fd, (json.dumps({"key": key, "st": fields}, ensure_ascii=False) + "\n").encode("utf-8"))
				os.fsync(fd)
			finally:
				os.close(fd)
		except OSError:
			pass

	def save(self, paths: Paths) -> None:
		import os
		import shutil
		if os.path.exists(paths.state_file):
			try:
				shutil.copy2(paths.state_file, paths.state_file + ".bak")
			except OSError:
				pass
		write_json(paths.state_file, self.data, 0o600)
		try:
			os.remove(paths.state_file + ".journal")
		except OSError:
			pass

	@property
	def entries(self) -> Dict[str, Dict[str, Any]]:
		return self.data["entries"]

	def entry(self, key: str) -> Dict[str, Any]:
		return self.entries.setdefault(key, {})

	def get(self, key: str, default: Any = None) -> Any:
		return self.data.get(key, default)

	def set(self, key: str, value: Any) -> None:
		self.data[key] = value

	def new_event(self, content: Optional[str], machine_id: str) -> str:
		"""Identifiant unique d'une modification locale (même si le contenu a déjà existé)."""
		self.data["counter"] = int(self.data.get("counter", 0)) + 1
		return "%s@%s#%d" % (content or "deleted", machine_id, self.data["counter"])

	# -- conflits -------------------------------------------------------------

	@property
	def conflicts(self) -> List[Dict[str, Any]]:
		return self.data["conflicts"]

	def add_conflict(self, item: Dict[str, Any]) -> None:
		for existing in self.conflicts:
			if existing.get("key") == item.get("key") and existing.get("machine_id") == item.get("machine_id"):
				existing.update(item)
				existing.setdefault("id", uuid.uuid4().hex[:10])
				return
		item = dict(item)
		item.setdefault("id", uuid.uuid4().hex[:10])
		self.conflicts.append(item)

	def resolve_conflicts(self, ids: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
		"""Marque des conflits comme résolus.

		Pour un fichier : votre version actuelle l'emporte (la version distante devient un de
		ses ancêtres, elle sera donc envoyée à l'autre machine).
		Pour une conversation : la copie reste une conversation à part ; CSync n'en crée plus
		d'autres pour cette divergence-là.
		"""
		wanted = None if ids is None else set(ids)
		resolved, keep = [], []
		for item in self.conflicts:
			if wanted is None or item.get("id") in wanted:
				resolved.append(item)
				st = self.entries.get(item.get("key"))
				if st is not None and item.get("kind") in ("file", "mcp"):
					rv = item.get("remote_version")
					hist = st.setdefault("hist", [])
					if rv and rv not in hist:
						hist.insert(max(0, len(hist) - 1), rv)
						del hist[:-HIST_MAX]
				elif st is not None and item.get("kind") == "session":
					ack = st.setdefault("conflict_ack", {})
					if item.get("remote_sig"):
						ack[item.get("machine_id", "")] = {"len": item.get("remote_len"), "sig": item["remote_sig"]}
					else:
						ack[item.get("machine_id", "")] = item.get("remote_chunks") or []
			else:
				keep.append(item)
		self.data["conflicts"] = keep
		return resolved


def push_hist(st: Dict[str, Any], version: str) -> None:
	hist = st.setdefault("hist", [])
	if not hist or hist[-1] != version:
		if version in hist:
			hist.remove(version)
		hist.append(version)
	del hist[:-HIST_MAX]
