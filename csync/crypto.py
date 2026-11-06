"""Chiffrement de bout en bout : tout ce qui part sur GitHub est chiffré avec votre clé.

- Une clé maîtresse de 32 octets, stockée dans un fichier que vous rangez vous-même.
- Deux clés dérivées (HKDF-SHA256) : une pour chiffrer (AES-256-GCM), une pour
  calculer l'identifiant des objets (HMAC-SHA256).
- Chaque bloc est compressé (zlib) avant d'être chiffré.
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import os
import zlib

from .util import CSyncError, atomic_write_text, read_bytes

KEY_PREFIX = "csync-key-v1:"
BLOB_MAGIC = b"CSY1"

try:
	from cryptography.exceptions import InvalidTag
	from cryptography.hazmat.primitives import hashes
	from cryptography.hazmat.primitives.ciphers.aead import AESGCM
	from cryptography.hazmat.primitives.kdf.hkdf import HKDF
	_IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover - dépend de l'installation
	_IMPORT_ERROR = exc


def _require_library() -> None:
	if _IMPORT_ERROR is not None:
		raise CSyncError("La bibliothèque « cryptography » est absente. Relancez install.sh.")


def generate_key_file(path: str) -> None:
	"""Crée un nouveau fichier de clé (refuse d'écraser un fichier existant)."""
	path = os.path.expanduser(path)
	if os.path.exists(path):
		raise CSyncError("Le fichier %s existe déjà. CSync ne remplace jamais une clé existante." % path)
	raw = os.urandom(32)
	text = (
		"# Clé de chiffrement CSync. Sans elle, vos données synchronisées sont illisibles,\n"
		"# y compris pour vous. Conservez-en une copie en lieu sûr (gestionnaire de mots de passe).\n"
		+ KEY_PREFIX + base64.b64encode(raw).decode("ascii") + "\n"
	)
	atomic_write_text(path, text, 0o600)


def load_key(path: str) -> bytes:
	path = os.path.expanduser(path)
	data = read_bytes(path)
	if data is None:
		raise CSyncError("Fichier de clé introuvable : %s" % path)
	for line in data.decode("utf-8", "replace").splitlines():
		line = line.strip()
		if line.startswith(KEY_PREFIX):
			try:
				raw = base64.b64decode(line[len(KEY_PREFIX):], validate=True)
			except ValueError:  # binascii.Error hérite de ValueError
				break
			if len(raw) == 32:
				return raw
			break
	raise CSyncError("Le fichier %s ne contient pas une clé CSync valide." % path)


def key_permissions_warning(path: str):
	"""Renvoie un avertissement si le fichier de clé est lisible par d'autres utilisateurs."""
	try:
		mode = os.stat(os.path.expanduser(path)).st_mode & 0o777
	except OSError:
		return None
	if mode & 0o077:
		return "Le fichier de clé %s est lisible par d'autres comptes (droits %o). Conseillé : chmod 600." % (path, mode)
	return None


class Cipher:
	def __init__(self, master_key: bytes):
		_require_library()
		self._enc_key = self._derive(master_key, b"csync-encryption")
		self._mac_key = self._derive(master_key, b"csync-object-id")
		self._aead = AESGCM(self._enc_key)

	@staticmethod
	def _derive(master: bytes, info: bytes) -> bytes:
		return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"csync-v1", info=info).derive(master)

	def object_id(self, plaintext: bytes) -> str:
		"""Identifiant d'un contenu : même contenu, même identifiant ; ne révèle rien sans la clé."""
		return hmac.new(self._mac_key, plaintext, hashlib.sha256).hexdigest()[:40]

	def encrypt(self, plaintext: bytes, context: str) -> bytes:
		nonce = os.urandom(12)
		body = zlib.compress(plaintext, 6)
		return BLOB_MAGIC + nonce + self._aead.encrypt(nonce, body, context.encode("utf-8"))

	def decrypt(self, blob: bytes, context: str) -> bytes:
		if not blob.startswith(BLOB_MAGIC) or len(blob) < len(BLOB_MAGIC) + 12 + 16:
			raise CSyncError("Bloc chiffré invalide (%s)." % context)
		nonce = blob[len(BLOB_MAGIC):len(BLOB_MAGIC) + 12]
		try:
			body = self._aead.decrypt(nonce, blob[len(BLOB_MAGIC) + 12:], context.encode("utf-8"))
		except InvalidTag:
			raise CSyncError("Impossible de déchiffrer %s : mauvaise clé ou fichier altéré." % context)
		try:
			return zlib.decompress(body)
		except zlib.error:
			raise CSyncError("Contenu corrompu après déchiffrement (%s)." % context)
