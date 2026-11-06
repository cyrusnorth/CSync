"""Réécriture des chemins propres à une machine en repères neutres, et l'inverse.

Exemple : « /Users/alex/dev/monprojet/src » devient « ⟪csync:P:git:github.com/alex/monprojet⟫/src »
sur le Mac, puis « /home/alex/code/monprojet/src » sur le serveur.

Les remplacements se font directement sur le texte brut (y compris pour les lignes JSON) :
seuls les chemins reconnus changent, tout le reste est conservé octet pour octet. Un texte
qui contiendrait déjà « ⟪csync: » est protégé (échappé), pour que l'opération soit réversible.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

TOKEN_OPEN = "⟪" + "csync:"   # ⟪csync:
TOKEN_CLOSE = "⟫"              # ⟫
TOKEN_RE = re.compile(re.escape(TOKEN_OPEN) + r"([^⟫\s]{1,300})" + re.escape(TOKEN_CLOSE))
# Un repère complet, ou un début de repère isolé (texte quelconque à protéger).
_OPEN_OR_TOKEN_RE = re.compile(re.escape(TOKEN_OPEN) + r"(?:([^⟫\s]{1,300})" + re.escape(TOKEN_CLOSE) + r")?")

# Caractères qui peuvent faire partie d'un nom de fichier : un chemin n'est remplacé
# que s'il n'est ni précédé ni suivi de l'un d'eux (évite /Users/alex2 ou /Volumes/Users/alex).
_PATH_CHAR = r"A-Za-z0-9_.\-"
# Après un chemin : ni un caractère de nom de fichier, ni un point suivi d'un tel caractère
# (un point final de phrase est accepté : « … /Users/alex/dev/monprojet. »).
_PATH_END = r"(?![A-Za-z0-9_\-]|\.[A-Za-z0-9_\-])"
_SLUG_MIN_LEN = 8
ESCAPED_OPEN = TOKEN_OPEN + "=" + TOKEN_CLOSE


def slug(path: str) -> str:
	"""Nom du dossier de projet de Claude Code : tout caractère autre qu'une lettre ou un chiffre devient « - »."""
	return re.sub(r"[^A-Za-z0-9]", "-", path)


def make_token(name: str) -> str:
	return TOKEN_OPEN + name + TOKEN_CLOSE


def _normalize(path: str) -> str:
	path = os.path.normpath(path)
	return path.rstrip("/") or "/"


class PathMapper:
	"""Table de correspondance entre chemins locaux et repères neutres, propre à une machine."""

	def __init__(self, mappings: Iterable[Tuple[str, str]]):
		# mappings : (chemin local absolu, nom du repère), ex. ("/Users/alex", "HOME")
		cleaned: Dict[str, str] = {}
		names: Dict[str, str] = {}
		for path, name in mappings:
			if not path or not name:
				continue
			path = _normalize(path)
			if path == "/":
				continue
			if path in cleaned:
				continue
			if name in names and name not in ("HOME", "CLAUDE"):
				continue  # premier arrivé conservé : un repère de projet désigne un seul chemin
			cleaned[path] = name
			names.setdefault(name, path)   # à la réception, c'est la première écriture qui sert
		self.path_to_name = cleaned
		self.name_to_path = names
		ordered = sorted(cleaned.keys(), key=len, reverse=True)
		if ordered:
			self._path_re = re.compile(
				r"(?<![" + _PATH_CHAR + r"])(" + "|".join(re.escape(p) for p in ordered) + r")" + _PATH_END
			)
		else:
			self._path_re = None
		slugs: Dict[str, str] = {}
		for path, name in cleaned.items():
			s = slug(path)
			if len(s) >= _SLUG_MIN_LEN and s not in slugs:
				slugs[s] = "S:" + name
		self.slug_to_name = slugs
		self.slugname_to_slug: Dict[str, str] = {}
		for k, v in slugs.items():
			self.slugname_to_slug.setdefault(v, k)   # premier chemin enregistré pour ce repère
		# Chemins de repli pour les repères inconnus ici (projet absent de cette machine) :
		# meilleure estimation, utilisée seulement pour l'affichage dans le contenu reçu.
		self.fallback: Dict[str, str] = {}
		self._alias_res: List[Tuple[Any, Dict[str, str]]] = []
		ordered_slugs = sorted(slugs.keys(), key=len, reverse=True)
		if ordered_slugs:
			self._slug_re = re.compile(
				r"(?<![A-Za-z0-9\-])(" + "|".join(re.escape(s) for s in ordered_slugs) + r")(?![A-Za-z0-9])"
			)
		else:
			self._slug_re = None

	def set_aliases(self, aliases: Iterable[Tuple[str, str]]) -> None:
		"""Formes neutres équivalentes à un projet connu ici.

		Exemple : l'autre machine a vu un projet avant qu'il n'ait de dépôt distant, et l'a donc
		désigné par sa place dans le dossier personnel (« ⟪csync:HOME⟫/dev/video ») ; son profil
		indique maintenant que ce dossier est le projet git:…/video, rangé ici ailleurs."""
		paths_table: Dict[str, str] = {}
		slugs_table: Dict[str, str] = {}
		for tok, local in aliases:
			local = _normalize(local)
			if tok and tok not in paths_table:
				paths_table[tok] = local
				# Forme « nom de dossier de Claude Code » correspondante.
				if tok.startswith(make_token("HOME")):
					rest = tok[len(make_token("HOME")):]
					slugs_table.setdefault(make_token("S:HOME") + slug(rest), slug(local))
		self._alias_res = []
		for table, end in ((paths_table, _PATH_END), (slugs_table, r"(?![A-Za-z0-9])")):
			if table:
				ordered = sorted(table.keys(), key=len, reverse=True)
				regex = re.compile("(" + "|".join(re.escape(t) for t in ordered) + ")" + end)
				self._alias_res.append((regex, table))

	# -- chaînes ------------------------------------------------------------

	def _is_foreign(self, name: str) -> bool:
		"""Repère de projet inconnu ici : il vient forcément d'un contenu reçu et laissé tel quel."""
		base = name[2:] if name.startswith("S:") else name
		return base.startswith("P:") and base not in self.name_to_path

	def _protect(self, m) -> str:
		name = m.group(1)
		if name is not None and name != "=" and self._is_foreign(name):
			return m.group(0)   # repère étranger conservé : l'aller-retour reste exact
		return ESCAPED_OPEN + m.group(0)[len(TOKEN_OPEN):]

	def tokenize_str(self, text: str) -> str:
		if TOKEN_OPEN in text:
			text = _OPEN_OR_TOKEN_RE.sub(self._protect, text)
		if self._path_re is not None and "/" in text:
			text = self._path_re.sub(lambda m: make_token(self.path_to_name[m.group(1)]), text)
		if self._slug_re is not None and "-" in text:
			text = self._slug_re.sub(lambda m: make_token(self.slug_to_name[m.group(1)]), text)
		return text

	def detokenize_str(self, text: str, use_fallback: bool = True) -> str:
		if TOKEN_OPEN not in text:
			return text
		for regex, table in self._alias_res:
			text = regex.sub(lambda m, t=table: t[m.group(1)], text)

		def resolve(name: str) -> Optional[str]:
			value = self.name_to_path.get(name)
			if value is None and use_fallback:
				value = self.fallback.get(name)
			return value

		def repl(m):
			name = m.group(1)
			if name == "=":
				return TOKEN_OPEN
			if name.startswith("S:"):
				value = self.slugname_to_slug.get(name)
				if value is None:
					inner = resolve(name[2:])
					value = slug(inner) if inner else None
			else:
				value = resolve(name)
			return value if value is not None else m.group(0)

		return TOKEN_RE.sub(repl, text)

	def unknown_tokens(self, text: str) -> List[str]:
		missing = []
		for m in TOKEN_RE.finditer(text):
			name = m.group(1)
			if name == "=":
				continue
			base = name[2:] if name.startswith("S:") else name
			if base not in self.name_to_path:
				missing.append(base)
		return missing

	# -- chemins --------------------------------------------------------------

	def tokenize_path(self, path: str) -> str:
		return self.tokenize_str(_normalize(path))

	def detokenize_path(self, tokpath: str) -> Optional[str]:
		"""Chemin local correspondant, ou None si un repère est inconnu sur cette machine."""
		if self.unknown_tokens(tokpath):
			return None
		return self.detokenize_str(tokpath, use_fallback=False)

	def guess_path(self, tokpath: str) -> Optional[str]:
		"""Estimation du chemin local à l'aide des chemins de repli, ou None."""
		value = self.detokenize_str(tokpath)
		return None if TOKEN_OPEN in value else value

	# -- JSON -------------------------------------------------------------------

	def _walk(self, obj: Any, fn) -> Any:
		if isinstance(obj, str):
			return fn(obj)
		if isinstance(obj, list):
			return [self._walk(v, fn) for v in obj]
		if isinstance(obj, dict):
			return {fn(k): self._walk(v, fn) for k, v in obj.items()}
		return obj

	def tokenize_json(self, obj: Any) -> Any:
		return self._walk(obj, self.tokenize_str)

	def detokenize_json(self, obj: Any) -> Any:
		return self._walk(obj, self.detokenize_str)

	# -- lignes de session ------------------------------------------------------

	def tokenize_line(self, line: str) -> str:
		"""Une ligne de session, traitée comme du texte : seuls les chemins changent."""
		return self.tokenize_str(line)

	def detokenize_line(self, line: str) -> str:
		return self.detokenize_str(line)


def dump_line(obj: Any) -> str:
	return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def token_names_in(text: str) -> List[str]:
	return [m.group(1) for m in TOKEN_RE.finditer(text)]
