"""Description du format des fichiers de Claude Code, et vérification sur les vrais fichiers.

L'adaptateur dit où trouver les rares informations dont CSync a besoin. Si une mise à jour
de Claude Code change ces formats, la vérification échoue, la catégorie concernée est
suspendue (rien n'est écrit), et une réparation peut être proposée par l'IA (voir ai.py).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from .tokens import slug
from .util import read_json, read_json_safe

DEFAULT_ADAPTER: Dict[str, Any] = {
	"version": 1,
	# Clé, dans chaque ligne d'une session, donnant le dossier de travail.
	"session_cwd_key": "cwd",
	# Clé, dans chaque ligne d'une session, donnant l'identifiant de la session.
	"session_id_key": "sessionId",
	# Clé de ~/.claude.json contenant les serveurs MCP personnels.
	"claude_json_mcp_key": "mcpServers",
	# Clé de ~/.claude.json contenant l'état par projet (sert à retrouver les dossiers).
	"claude_json_projects_key": "projects",
	# Règle de nommage des dossiers de projet : « nonalnum-dash » = tout caractère autre
	# qu'une lettre ou un chiffre remplacé par « - ».
	"slug_rule": "nonalnum-dash",
}

SLUG_RULES = ("nonalnum-dash",)


def load_adapter(path: str) -> Dict[str, Any]:
	data = read_json_safe(path, None)
	adapter = dict(DEFAULT_ADAPTER)
	if isinstance(data, dict):
		adapter.update({k: v for k, v in data.items() if k in DEFAULT_ADAPTER})
	return adapter


def get_path(obj: Any, dotted: str) -> Any:
	"""Lit une clé éventuellement imbriquée, ex. « message.cwd »."""
	current = obj
	for part in dotted.split("."):
		if not isinstance(current, dict) or part not in current:
			return None
		current = current[part]
	return current


def set_path(obj: Dict[str, Any], dotted: str, value: Any) -> bool:
	parts = dotted.split(".")
	current = obj
	for part in parts[:-1]:
		nxt = current.get(part)
		if not isinstance(nxt, dict):
			return False
		current = nxt
	if parts[-1] not in current:
		return False
	current[parts[-1]] = value
	return True


def read_head_lines(path: str, max_lines: int = 60, max_bytes: int = 1_000_000) -> List[str]:
	"""Premières lignes complètes d'un fichier, lues par petits morceaux (les lignes peuvent être énormes)."""
	lines: List[str] = []
	buffer = b""
	total = 0
	try:
		with open(path, "rb") as fh:
			while len(lines) < max_lines and total < max_bytes:
				block = fh.read(65536)
				if not block:
					break
				total += len(block)
				buffer += block
				while b"\n" in buffer and len(lines) < max_lines:
					raw, buffer = buffer.split(b"\n", 1)
					raw = raw.strip()
					if raw:
						lines.append(raw.decode("utf-8", "replace"))
				if lines and len(buffer) > 4 * 65536:
					break  # ligne gigantesque : on se contente de ce qu'on a
	except OSError:
		return lines
	return lines


def session_cwd(path: str, adapter: Dict[str, Any]) -> Optional[str]:
	key = adapter["session_cwd_key"]
	for line in read_head_lines(path):
		try:
			obj = json.loads(line)
		except ValueError:
			continue
		value = get_path(obj, key)
		if isinstance(value, str) and value.startswith("/"):
			return value
	return None


def session_id_of(path: str, adapter: Dict[str, Any]) -> Optional[str]:
	key = adapter["session_id_key"]
	for line in read_head_lines(path, 20):
		try:
			obj = json.loads(line)
		except ValueError:
			continue
		value = get_path(obj, key)
		if isinstance(value, str) and value:
			return value
	return None


def list_session_files(projects_dir: str, limit: int = 40) -> List[Tuple[str, str]]:
	"""Les sessions les plus récentes : (dossier de projet, fichier)."""
	found: List[Tuple[float, str, str]] = []
	try:
		dirs = os.listdir(projects_dir)
	except FileNotFoundError:
		return []
	for d in dirs:
		full = os.path.join(projects_dir, d)
		if not os.path.isdir(full):
			continue
		try:
			names = os.listdir(full)
		except OSError:
			continue
		for name in names:
			if name.endswith(".jsonl") and ".orphaned-" not in name:
				p = os.path.join(full, name)
				try:
					found.append((os.path.getmtime(p), d, p))
				except OSError:
					pass
	found.sort(reverse=True)
	return [(d, p) for _, d, p in found[:limit]]


class FormatReport:
	def __init__(self):
		self.sessions_ok = True
		self.mcp_ok = True
		self.details: List[str] = []
		self.warnings: List[str] = []

	@property
	def ok(self) -> bool:
		return self.sessions_ok and self.mcp_ok

	def to_dict(self) -> Dict[str, Any]:
		return {"sessions_ok": self.sessions_ok, "mcp_ok": self.mcp_ok,
				"details": self.details, "warnings": self.warnings}


def check_format(adapter: Dict[str, Any], projects_dir: str, claude_json_path: str, limit: int = 40) -> FormatReport:
	"""Confronte l'adaptateur aux vrais fichiers de la machine."""
	report = FormatReport()
	if adapter.get("slug_rule") not in SLUG_RULES:
		report.sessions_ok = False
		report.details.append("Règle de nommage des dossiers inconnue : %r." % adapter.get("slug_rule"))

	sessions = list_session_files(projects_dir, limit=limit)
	if sessions:
		with_cwd = 0
		slug_match = 0
		id_present = 0
		id_match = 0
		for d, p in sessions:
			cwd = session_cwd(p, adapter)
			if cwd is not None:
				with_cwd += 1
				s = slug(cwd)
				# Claude Code peut raccourcir les noms très longs : on accepte un préfixe commun.
				if d == s or (len(s) > 100 and d[:100] == s[:100]):
					slug_match += 1
			sid = session_id_of(p, adapter)
			if sid is not None:
				id_present += 1
				if os.path.basename(p)[:-len(".jsonl")] == sid:
					id_match += 1
		total = len(sessions)
		report.details.append("Sessions examinées : %d ; dossier de travail trouvé : %d ; nommage vérifié : %d."
							  % (total, with_cwd, slug_match))
		if with_cwd < max(1, total // 2):
			report.sessions_ok = False
			report.details.append("La clé « %s » ne donne pas le dossier de travail dans la plupart des sessions."
								  % adapter["session_cwd_key"])
		elif slug_match < max(1, int(with_cwd * 0.7)):
			report.sessions_ok = False
			report.details.append("Le nom des dossiers de projet ne correspond plus à la règle connue.")
		if id_present and id_match < int(id_present * 0.7):
			report.warnings.append("L'identifiant de session (« %s ») ne correspond pas au nom des fichiers : "
								   "les copies de conflit seront nommées sans le modifier." % adapter["session_id_key"])
		elif not id_present:
			report.warnings.append("Identifiant de session introuvable dans les fichiers (clé « %s »)."
								   % adapter["session_id_key"])
	else:
		report.details.append("Aucune session Claude Code sur cette machine pour l'instant.")

	if os.path.exists(claude_json_path):
		try:
			with open(claude_json_path, "rb") as fh:
				data = json.loads(fh.read().decode("utf-8"))
		except (OSError, ValueError, UnicodeDecodeError):
			report.mcp_ok = False
			report.details.append("%s est illisible : la synchronisation MCP est suspendue." % claude_json_path)
			return report
		if not isinstance(data, dict):
			report.mcp_ok = False
			report.details.append("%s n'a pas la structure attendue." % claude_json_path)
			return report
		servers = get_path(data, adapter["claude_json_mcp_key"])
		if servers is None:
			suspects = [k for k, v in data.items() if "mcp" in k.lower() and isinstance(v, dict) and v]
			if suspects:
				report.mcp_ok = False
				report.details.append("Serveurs MCP introuvables sous « %s », mais d'autres clés existent : %s."
									  % (adapter["claude_json_mcp_key"], ", ".join(suspects)))
		elif not isinstance(servers, dict) or not all(isinstance(v, dict) for v in servers.values()):
			report.mcp_ok = False
			report.details.append("La clé « %s » n'a pas la structure attendue." % adapter["claude_json_mcp_key"])
	return report


def structure_summary(projects_dir: str, claude_json_path: str, adapter: Dict[str, Any]) -> Dict[str, Any]:
	"""Description anonymisée des fichiers, pour la réparation par l'IA.

	Ne contient que des noms de clés et des types : jamais de contenu de conversation,
	jamais de valeur de ~/.claude.json, jamais de chemin réel (remplacés par des exemples neutres).
	"""
	keys: Dict[str, str] = {}

	def walk(obj: Any, prefix: str, depth: int) -> None:
		if depth > 3 or len(keys) > 300:
			return
		if isinstance(obj, dict):
			for k, v in obj.items():
				if "/" in k or "\\" in k or len(k) > 60 or " " in k:
					# Clé dynamique (souvent un chemin de fichier) : jamais transmise.
					keys.setdefault((prefix + ".*") if prefix else "*", "clés dynamiques")
					continue
				path = prefix + "." + k if prefix else k
				kind = type(v).__name__
				if isinstance(v, str) and v.startswith("/"):
					kind = "str(chemin absolu)"
				keys.setdefault(path, kind)
				walk(v, path, depth + 1)

	examples = []
	for d, p in list_session_files(projects_dir, limit=8):
		for line in read_head_lines(p, 30):
			try:
				walk(json.loads(line), "", 0)
			except ValueError:
				continue
		# Exemple de nommage, sans révéler le chemin : on vérifie juste la règle.
		examples.append({"nom_dossier_conforme_a_la_regle_actuelle": d == slug(session_cwd(p, adapter) or "")})

	claude_json_keys: Dict[str, str] = {}
	data = read_json_safe(claude_json_path, None)
	if isinstance(data, dict):
		for k, v in data.items():
			desc = type(v).__name__
			if isinstance(v, dict):
				sub = list(v.values())[:3]
				desc = "dict(%d entrées%s)" % (len(v), ", valeurs de type dict" if sub and all(isinstance(s, dict) for s in sub) else "")
			claude_json_keys[k] = desc
	return {
		"cles_des_lignes_de_session": keys,
		"verifications_de_nommage": examples,
		"cles_de_claude_json": claude_json_keys,
		"adaptateur_actuel": adapter,
	}


def session_last_cwd(path: str, adapter: Dict[str, Any]) -> Optional[str]:
	"""Dernier dossier de travail écrit dans une session (lu à la fin du fichier)."""
	import re
	key = str(adapter["session_cwd_key"]).split(".")[-1]
	pattern = re.compile(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % re.escape(key))
	try:
		size = os.path.getsize(path)
		with open(path, "rb") as fh:
			fh.seek(max(0, size - 65536))
			found = pattern.findall(fh.read().decode("utf-8", "surrogateescape"))
		value = json.loads('"%s"' % found[-1]) if found else None
	except (OSError, ValueError):
		return None
	return value if isinstance(value, str) and value.startswith("/") else None
