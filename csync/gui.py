"""Fenêtre de CSync (macOS). Elle n'affiche que l'état et appelle controller.py.

Aucune minuterie ne tourne : l'état est relu à l'ouverture et après chaque action.
Fermer la fenêtre quitte réellement l'application.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional

from .controller import Controller
from .paths import Paths, system_name
from .util import CSyncError, human_time, setup_logging

try:
	from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
	from PySide6.QtGui import QFont
	from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
								   QLabel, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
								   QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)
	_QT_ERROR = None
except ImportError as exc:  # pragma: no cover - dépend de l'installation
	_QT_ERROR = exc


def reveal(path: str) -> None:
	if not path:
		return
	if system_name() == "darwin":
		subprocess.Popen(["open", "-R", path])
	else:
		subprocess.Popen(["xdg-open", os.path.dirname(path) or "."])


if _QT_ERROR is None:

	class _Signals(QObject):
		done = Signal(int, object)
		failed = Signal(int, str)

	class Task(QRunnable):
		"""Exécute une fonction hors de l'interface pour qu'elle reste réactive.

		Les signaux sont reliés à des méthodes de la fenêtre : Qt les exécute alors dans le fil
		principal, le seul autorisé à toucher à l'interface.
		"""

		def __init__(self, task_id: int, func: Callable[[], Any], signals: "_Signals"):
			super().__init__()
			self.task_id = task_id
			self.func = func
			self.signals = signals

		def run(self) -> None:
			try:
				result = self.func()
			except CSyncError as exc:
				self.signals.failed.emit(self.task_id, str(exc))
				return
			except Exception as exc:  # erreur inattendue : affichée plutôt que perdue
				self.signals.failed.emit(self.task_id, "Erreur inattendue : %s" % exc)
				return
			self.signals.done.emit(self.task_id, result)

	class ScanDialog(QDialog):
		COLS = ("Inclure", "Dossier sur cette machine", "Identifiant partagé", "Statut")

		def __init__(self, parent, result):
			super().__init__(parent)
			self.setWindowTitle("Résultat du scan")
			self.result = result
			layout = QVBoxLayout(self)
			intro = QLabel("Projets reconnus sur cette machine. Un projet git est reconnu automatiquement sur "
						   "l'autre machine. Un dossier sans git hors du dossier personnel n'est partagé que si vous "
						   "le cochez et lui donnez un nom, le même sur l'autre machine.")
			intro.setWordWrap(True)
			layout.addWidget(intro)
			projects = result.profile.get("projects", [])
			added = {p["path"] for p in result.diff.get("added", [])}
			moved = {p["new"] for p in result.diff.get("moved", [])}
			self.table = QTableWidget(len(projects), len(self.COLS))
			self.table.setHorizontalHeaderLabels(list(self.COLS))
			self.table.verticalHeader().setVisible(False)
			for row, p in enumerate(projects):
				check = QTableWidgetItem()
				check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
				check.setCheckState(Qt.CheckState.Checked if p.get("include", True) else Qt.CheckState.Unchecked)
				self.table.setItem(row, 0, check)
				path_item = QTableWidgetItem(p["path"])
				path_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
				self.table.setItem(row, 1, path_item)
				if p["key"].startswith("unconfirmed:"):
					shown = ""          # à vous de choisir un nom commun, identique sur l'autre machine
				elif p["key"].startswith("dir:"):
					shown = p["key"][4:]
				else:
					shown = p["key"]
				key_item = QTableWidgetItem(shown)
				if p.get("kind") == "git":
					key_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
				self.table.setItem(row, 2, key_item)
				if p["path"] in added:
					label = "nouveau"
				elif p["path"] in moved:
					label = "déplacé"
				elif p.get("to_confirm"):
					label = "à confirmer"
				else:
					label = "connu"
				status = QTableWidgetItem(label)
				status.setFlags(Qt.ItemFlag.ItemIsEnabled)
				self.table.setItem(row, 3, status)
			header = self.table.horizontalHeader()
			header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
			header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
			layout.addWidget(self.table)
			extra: List[str] = []
			for p in result.diff.get("removed", []):
				extra.append("Disparu : %s (%s)" % (p["key"], p["path"]))
			for p in result.diff.get("remote_only", []):
				extra.append("Seulement sur %s : %s (%s) — clonez-le ici pour recevoir ses sessions."
							 % (p["machine"], p["key"], p.get("remote_path") or "?"))
			for w in result.profile.get("warnings", []):
				extra.append("Attention : " + w)
			if extra:
				note = QLabel("\n".join(extra))
				note.setWordWrap(True)
				layout.addWidget(note)
			buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
			buttons.accepted.connect(self.accept)
			buttons.rejected.connect(self.reject)
			layout.addWidget(buttons)
			self.resize(760, 420)

		def choices(self) -> Dict[str, Dict[str, Any]]:
			out: Dict[str, Dict[str, Any]] = {}
			for row, p in enumerate(self.result.profile.get("projects", [])):
				include = self.table.item(row, 0).checkState() == Qt.CheckState.Checked
				name = self.table.item(row, 2).text().strip()
				entry: Dict[str, Any] = {"include": include}
				if p.get("kind") != "git" and name:
					entry["name"] = name
				elif p["key"].startswith("unconfirmed:"):
					entry["include"] = False   # sans nom commun, un dossier n'est jamais partagé
				out[p["path"]] = entry
			return out

	class TextDialog(QDialog):
		def __init__(self, parent, title: str, text: str):
			super().__init__(parent)
			self.setWindowTitle(title)
			layout = QVBoxLayout(self)
			view = QPlainTextEdit(text)
			view.setReadOnly(True)
			font = QFont("Menlo")
			font.setStyleHint(QFont.StyleHint.Monospace)
			view.setFont(font)
			layout.addWidget(view)
			buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
			buttons.rejected.connect(self.reject)
			buttons.accepted.connect(self.accept)
			layout.addWidget(buttons)
			self.resize(640, 460)

	class ConflictsDialog(QDialog):
		def __init__(self, parent, controller: Controller, conflicts: List[Dict[str, Any]]):
			super().__init__(parent)
			self.controller = controller
			self.setWindowTitle("Conflits")
			layout = QVBoxLayout(self)
			info = QLabel("Pour chaque conflit, votre version a été gardée et celle de l'autre machine a été "
						  "enregistrée à côté (pour une conversation : une conversation à part, que vous pouvez "
						  "reprendre dans Claude Code). Comparez, gardez la bonne version, puis supprimez la copie "
						  "ou marquez le conflit comme résolu : pour un fichier, votre version sera alors envoyée "
						  "à l'autre machine.")
			info.setWordWrap(True)
			layout.addWidget(info)
			self.list = QListWidget()
			for c in conflicts:
				item = QListWidgetItem("%s\n    version de %s : %s" % (c.get("path"), c.get("machine"), c.get("copy")))
				item.setData(Qt.ItemDataRole.UserRole, c.get("copy"))
				item.setData(Qt.ItemDataRole.UserRole + 1, c.get("id"))
				self.list.addItem(item)
			layout.addWidget(self.list)
			row = QHBoxLayout()
			show = QPushButton("Afficher la copie")
			show.clicked.connect(self._show)
			resolve = QPushButton("Marquer comme résolu")
			resolve.clicked.connect(self._resolve_selected)
			resolve_all = QPushButton("Tout marquer comme résolu")
			resolve_all.clicked.connect(self._resolve_all)
			close = QPushButton("Fermer")
			close.clicked.connect(self.accept)
			for b in (show, resolve, resolve_all):
				row.addWidget(b)
			row.addStretch(1)
			row.addWidget(close)
			layout.addLayout(row)
			self.resize(720, 360)

		def _show(self) -> None:
			item = self.list.currentItem()
			if item is not None:
				reveal(item.data(Qt.ItemDataRole.UserRole))

		def _resolve_selected(self) -> None:
			row = self.list.currentRow()
			item = self.list.currentItem()
			if row < 0 or item is None:
				return
			try:
				self.controller.resolve_conflicts([item.data(Qt.ItemDataRole.UserRole + 1)])
			except CSyncError as exc:
				QMessageBox.warning(self, "CSync", str(exc))
				return
			self.list.takeItem(row)

		def _resolve_all(self) -> None:
			try:
				self.controller.resolve_conflicts(None)
			except CSyncError as exc:
				QMessageBox.warning(self, "CSync", str(exc))
				return
			self.list.clear()

	class MainWindow(QWidget):
		def __init__(self, controller: Controller):
			super().__init__()
			self.controller = controller
			self.pool = QThreadPool.globalInstance()
			self._callbacks: Dict[int, Callable[[Any], None]] = {}
			self._running: Dict[int, Any] = {}
			self._next_task = 0
			self._signals = _Signals(self)
			self._signals.done.connect(self._task_done)
			self._signals.failed.connect(self._task_failed)
			self._status: Dict[str, Any] = {}
			self.setWindowTitle("CSync")
			layout = QVBoxLayout(self)
			self.title = QLabel("CSync")
			font = self.title.font()
			font.setPointSize(font.pointSize() + 5)
			font.setBold(True)
			self.title.setFont(font)
			layout.addWidget(self.title)
			self.info = QLabel()
			self.info.setWordWrap(True)
			self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
			layout.addWidget(self.info)
			self.alerts = QLabel()
			self.alerts.setWordWrap(True)
			self.alerts.setStyleSheet("color: #b4530a;")
			layout.addWidget(self.alerts)

			row = QHBoxLayout()
			self.btn_sync = QPushButton("Synchroniser")
			self.btn_sync.setDefault(True)
			self.btn_sync.clicked.connect(self.on_sync)
			self.btn_scan = QPushButton("Rescanner")
			self.btn_scan.clicked.connect(self.on_scan)
			self.btn_profile = QPushButton("Voir le profil")
			self.btn_profile.clicked.connect(self.on_profile)
			for b in (self.btn_sync, self.btn_scan, self.btn_profile):
				row.addWidget(b)
			layout.addLayout(row)

			row2 = QHBoxLayout()
			self.btn_conflicts = QPushButton("Conflits")
			self.btn_conflicts.clicked.connect(self.on_conflicts)
			self.btn_parked = QPushButton("Sessions en attente")
			self.btn_parked.clicked.connect(self.on_parked)
			self.btn_repair = QPushButton("Réparer le format")
			self.btn_repair.clicked.connect(self.on_repair)
			self.btn_compact = QPushButton("Compacter")
			self.btn_compact.clicked.connect(self.on_compact)
			for b in (self.btn_conflicts, self.btn_parked, self.btn_repair, self.btn_compact):
				row2.addWidget(b)
			row2.addStretch(1)
			layout.addLayout(row2)

			self.footer = QLabel()
			self.footer.setWordWrap(True)
			self.footer.setStyleSheet("color: gray;")
			layout.addWidget(self.footer)
			layout.addStretch(1)
			self.resize(460, 300)
			self.refresh()

		# -- utilitaires ----------------------------------------------------------

		def busy(self, on: bool, message: str = "") -> None:
			for b in (self.btn_sync, self.btn_scan, self.btn_profile, self.btn_conflicts, self.btn_parked, self.btn_repair,
					  self.btn_compact):
				b.setEnabled(not on)
			if message:
				self.footer.setText(message)

		def run_task(self, func, on_done, busy_message: str) -> None:
			self._next_task += 1
			task_id = self._next_task
			self._callbacks[task_id] = on_done
			self.busy(True, busy_message)
			task = Task(task_id, func, self._signals)
			task.setAutoDelete(False)
			self._running[task_id] = task  # garde une référence tant que la tâche tourne
			self.pool.start(task)

		def _task_done(self, task_id: int, result: Any) -> None:
			self._running.pop(task_id, None)
			callback = self._callbacks.pop(task_id, None)
			self.busy(False)
			if callback is not None:
				try:
					callback(result)
				except CSyncError as exc:
					QMessageBox.warning(self, "CSync", str(exc))
			self.refresh()

		def _task_failed(self, task_id: int, message: str) -> None:
			self._running.pop(task_id, None)
			self._callbacks.pop(task_id, None)
			self.busy(False)
			self.footer.setText("Échec : " + message)
			QMessageBox.warning(self, "CSync", message)
			self.refresh()

		def refresh(self) -> None:
			try:
				status = self.controller.status()
			except CSyncError as exc:
				self.info.setText(str(exc))
				return
			if not status.get("configured"):
				self.title.setText("CSync")
				self.info.setText("CSync n'est pas encore configuré sur cette machine. Ouvrez un terminal et lancez "
								  "« csync setup ».")
				self.alerts.setText("")
				self.btn_sync.setEnabled(False)
				self.btn_scan.setEnabled(False)
				for b in (self.btn_conflicts, self.btn_parked, self.btn_repair, self.btn_compact):
					b.setVisible(False)
				return
			self.title.setText("CSync — %s" % status["machine_name"])
			lines = ["Dernier envoi : %s" % human_time(status.get("last_push")),
					 "Dernière réception : %s" % human_time(status.get("last_pull"))]
			for m in status.get("machines", []):
				lines.append("Autre machine : %s (Claude Code %s), mise à jour %s"
							 % (m["name"], m.get("claude_version") or "?", human_time(m.get("updated"))))
			if not status.get("machines"):
				lines.append("Aucune autre machine pour l'instant.")
			self.info.setText("\n".join(lines))
			self.alerts.setText("\n".join("• " + a for a in status.get("alerts", [])))
			n_conf = len(status.get("conflicts", []))
			self.btn_conflicts.setText("Conflits (%d)" % n_conf)
			self.btn_conflicts.setVisible(n_conf > 0)
			self.btn_parked.setVisible(bool(status.get("parked")))
			fc = status.get("format_check") or {}
			needs_repair = not (fc.get("sessions_ok", True) and fc.get("mcp_ok", True))
			self.btn_repair.setVisible(needs_repair or bool(status.get("pending_repair")))
			self.btn_compact.setVisible(bool(status.get("compact_advised")))
			self._status = status

		# -- actions --------------------------------------------------------------------

		def on_sync(self) -> None:
			def done(reports):
				lines = []
				for r in reports:
					lines.extend(r.summary_lines())
				self.footer.setText(" · ".join(dict.fromkeys(lines)))
			self.run_task(lambda: self.controller.sync(), done, "Synchronisation en cours…")

		def on_scan(self) -> None:
			def done(result):
				dialog = ScanDialog(self, result)
				if dialog.exec() == QDialog.DialogCode.Accepted:
					self.controller.apply_scan(result, dialog.choices())
					self.footer.setText("Profil enregistré.")
				else:
					self.footer.setText("Scan annulé : profil inchangé.")
			self.run_task(self.controller.rescan, done, "Scan en cours…")

		def on_profile(self) -> None:
			TextDialog(self, "Profil de la machine", self.controller.profile_text()).exec()

		def on_conflicts(self) -> None:
			ConflictsDialog(self, self.controller, self._status.get("conflicts", [])).exec()
			self.refresh()

		def on_compact(self) -> None:
			answer = QMessageBox.question(
				self, "Compacter le dépôt",
				"CSync va retirer du dépôt de données les contenus que plus aucune machine n'utilise et en "
				"réduire l'historique. Les autres machines se réaligneront d'elles-mêmes. Continuer ?")
			if answer != QMessageBox.StandardButton.Yes:
				return

			def done(result):
				removed, before, after = result
				self.footer.setText("Dépôt compacté : %.0f Mo → %.0f Mo." % (before, after))
			self.run_task(self.controller.compact, done, "Compactage en cours…")

		def on_parked(self) -> None:
			lines = ["Ces sessions viennent d'un projet absent de cette machine.",
					 "Projet git : clonez-le ici, où vous voulez ; il sera trouvé à la synchronisation suivante "
					 "(ou tout de suite avec « Rescanner »).",
					 "Dossier sans git : donnez au dossier correspondant le même nom que sur l'autre machine "
					 "(« Rescanner », colonne « Identifiant partagé »).", ""]
			for p in self._status.get("parked", []):
				lines.append("• %s (%d session(s), sur %s : %s)" % (p.get("project"), p.get("count", 0), p.get("machine"),
																	 p.get("remote_path") or "?"))
			TextDialog(self, "Sessions en attente", "\n".join(lines)).exec()

		def on_repair(self) -> None:
			pending = self.controller.pending_repair()
			if pending:
				self._ask_approval(pending)
				return
			answer = QMessageBox.question(
				self, "Réparer le format",
				"Claude Code va examiner la structure de vos fichiers (noms de clés seulement, jamais le contenu des "
				"conversations) et proposer une correction. Rien ne sera appliqué sans votre accord. Continuer ?")
			if answer != QMessageBox.StandardButton.Yes:
				return

			def done(result):
				self._ask_approval(self.controller.pending_repair() or {})
			self.run_task(self.controller.repair_propose, done, "Analyse par Claude Code en cours…")

		def _ask_approval(self, pending: Dict[str, Any]) -> None:
			after = pending.get("after", {})
			ok = after.get("sessions_ok") and after.get("mcp_ok")
			import json
			text = ("Proposition :\n%s\n\nVérification sur vos fichiers : %s\n%s"
					% (json.dumps(pending.get("adapter", {}), indent=2, ensure_ascii=False),
					   "réussie" if ok else "échouée", "\n".join(after.get("details", []))))
			if not ok:
				QMessageBox.warning(self, "Réparer le format", text + "\n\nLa proposition est rejetée.")
				self.controller.repair_reject()
				self.refresh()
				return
			answer = QMessageBox.question(self, "Réparer le format", text + "\n\nAppliquer cette réparation ?")
			try:
				if answer == QMessageBox.StandardButton.Yes:
					self.controller.repair_approve()
					self.footer.setText("Réparation appliquée.")
				else:
					self.controller.repair_reject()
					self.footer.setText("Réparation rejetée.")
			except CSyncError as exc:
				QMessageBox.warning(self, "CSync", str(exc))
			self.refresh()


def main(paths: Optional[Paths] = None) -> int:
	if _QT_ERROR is not None:
		print("La fenêtre nécessite PySide6, absent de cette installation (%s). Relancez install.sh." % _QT_ERROR,
			  file=sys.stderr)
		return 1
	paths = paths or Paths()
	setup_logging(paths.log_file, to_stderr=False)
	app = QApplication.instance() or QApplication(sys.argv[:1])
	app.setApplicationName("CSync")
	app.setQuitOnLastWindowClosed(True)
	controller = Controller(paths)
	window = MainWindow(controller)
	window.show()
	problems = []
	try:
		problems = controller.environment_check()
	except CSyncError as exc:
		problems = [str(exc)]
	if problems:
		QMessageBox.warning(window, "Vérification de l'installation", "\n\n".join(problems))
	return app.exec()
