"""Le dépôt de données : un clone git local du dépôt GitHub privé, et son contenu chiffré.

Organisation du dépôt (tout est chiffré sauf csync-format.json, README.md et .gitignore) :

  csync-format.json                     version du format, en clair
  keycheck.bin                          permet de vérifier qu'une machine a la bonne clé
  manifests/<machine>/meta.bin          informations sur la machine
  manifests/<machine>/<xx>.bin          état de la machine, réparti en 256 morceaux
  profiles/<machine>.bin                profil d'une machine (projets, versions)
  objects/<machine>/<ab>/<id>.bin       contenus, adressés par leur identifiant

Chaque machine n'écrit que dans ses propres fichiers : deux machines ne peuvent jamais
entrer en conflit au niveau de git. Les messages de commit ne contiennent aucune information.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple

from . import DATA_FORMAT
from .crypto import Cipher
from .util import (CSyncError, LOG, atomic_write_bytes, atomic_write_text, ensure_dir, read_bytes,
				   remove_file, run, which)

KEYCHECK_TEXT = b"csync-keycheck-v1"
COMMIT_MESSAGE = "csync"
TRIVIAL_FILES = {"README.md", "README", "README.txt", "LICENSE", "LICENSE.md", "LICENSE.txt", ".gitignore",
				 ".gitattributes"}

README = """# Données CSync

Ce dépôt est géré automatiquement par CSync. Son contenu est chiffré avec une clé
qui n'est jamais envoyée ici : sans elle, il est illisible.

Ne modifiez pas ces fichiers à la main.
"""

GITIGNORE = ".csync-tmp-*\n.DS_Store\n"


class GitError(CSyncError):
	pass


def shard_of(key: str) -> str:
	return hashlib.sha1(key.encode("utf-8")).hexdigest()[:2]


class Store:
	def __init__(self, repo_dir: str, cipher: Optional[Cipher], machine_id: str, branch: str = "main",
				 git_timeout: float = 120):
		self.repo_dir = repo_dir
		self.cipher = cipher
		self.machine_id = machine_id
		self.branch = branch
		self.git_timeout = git_timeout
		self.light = False   # vrai dans un hook : pas de tâche longue (nettoyage différé)
		self._chunk_cache: Dict[str, bytes] = {}

	# ------------------------------------------------------------------
	# git
	# ------------------------------------------------------------------

	def _git_env(self) -> Dict[str, str]:
		import time
		stamp = "@%d +0000" % int(time.time())
		env = {
			"GIT_TERMINAL_PROMPT": "0",
			"GIT_ASKPASS": "",
			"SSH_ASKPASS": "",
			"LC_ALL": "C",
			# Aucune information personnelle dans les commits du dépôt de données : ni nom, ni
			# adresse, ni fuseau horaire.
			"GIT_AUTHOR_NAME": "csync", "GIT_AUTHOR_EMAIL": "csync@localhost",
			"GIT_COMMITTER_NAME": "csync", "GIT_COMMITTER_EMAIL": "csync@localhost",
			"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp,
		}
		if "GIT_SSH_COMMAND" not in os.environ:
			env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=20"
		return env

	def git(self, *args: str, network: bool = False, check: bool = True):
		if which("git") is None:
			raise GitError("git est introuvable sur cette machine.")
		timeout = self.git_timeout if network else 60
		# --git-dir et --work-tree explicites : CSync ne peut agir que sur son propre dépôt.
		cmd = ["git", "--git-dir=" + os.path.join(self.repo_dir, ".git"), "--work-tree=" + self.repo_dir] + list(args)
		result = run(cmd, cwd=self.repo_dir, timeout=timeout, env=self._git_env())
		if check and not result.ok:
			detail = (result.err or result.out).strip().splitlines()
			message = detail[-1] if detail else "code %d" % result.code
			raise GitError("Échec de « git %s » : %s" % (args[0], message))
		return result

	def is_cloned(self) -> bool:
		return os.path.isdir(os.path.join(self.repo_dir, ".git"))

	def clone(self, url: str) -> None:
		parent = os.path.dirname(self.repo_dir)
		ensure_dir(parent)
		if os.path.exists(self.repo_dir) and os.listdir(self.repo_dir):
			raise CSyncError("Le dossier %s existe déjà et n'est pas vide." % self.repo_dir)
		result = run(["git", "clone", url, self.repo_dir], cwd=parent, timeout=self.git_timeout, env=self._git_env())
		if not result.ok:
			hint = ""
			err = result.err.lower()
			if "permission denied" in err or "publickey" in err:
				hint = " Vérifiez que cette machine a accès au dépôt (clé SSH ajoutée à GitHub)."
			elif "not found" in err or "does not exist" in err:
				hint = " Vérifiez l'adresse du dépôt et qu'il existe bien."
			raise GitError("Impossible de cloner le dépôt de données.%s\n%s" % (hint, result.err.strip()))
		self._configure()

	def _configure(self) -> None:
		for key, value in (("user.name", "csync"), ("user.email", "csync@localhost"), ("commit.gpgsign", "false"),
						   ("core.autocrlf", "false"), ("pull.rebase", "true"), ("core.hooksPath", "/dev/null"),
						   ("gc.auto", "0")):
			self.git("config", key, value)

	def has_commits(self) -> bool:
		return self.git("rev-parse", "--verify", "HEAD", check=False).ok

	def remote_has_branch(self) -> bool:
		result = self.git("ls-remote", "--heads", "origin", self.branch, network=True, check=False)
		if not result.ok:
			raise GitError("Le dépôt de données est injoignable : %s" % (result.err.strip().splitlines() or ["?"])[-1])
		return bool(result.out.strip())

	def tracked_files(self) -> List[str]:
		result = self.git("ls-files", check=False)
		return [l for l in result.out.splitlines() if l.strip()] if result.ok else []

	def looks_initialized(self) -> bool:
		return os.path.exists(os.path.join(self.repo_dir, "csync-format.json"))

	def can_initialize_here(self) -> bool:
		"""Vrai si le dépôt est vide ou ne contient que des fichiers anodins (README créé par GitHub…)."""
		return all(os.path.basename(f) in TRIVIAL_FILES and "/" not in f for f in self.tracked_files())

	def initialize_remote(self) -> None:
		"""Premier usage : crée la structure du dépôt (format, vérification de clé) et l'envoie."""
		if self.cipher is None:
			raise CSyncError("Clé requise pour initialiser le dépôt.")
		if not self.has_commits():
			self.git("checkout", "-B", self.branch, check=False)
		atomic_write_text(os.path.join(self.repo_dir, "csync-format.json"),
						  json.dumps({"format": DATA_FORMAT, "tool": "CSync"}, indent=2) + "\n")
		atomic_write_text(os.path.join(self.repo_dir, "README.md"), README)
		atomic_write_text(os.path.join(self.repo_dir, ".gitignore"), GITIGNORE)
		atomic_write_text(os.path.join(self.repo_dir, "generation.json"), json.dumps({"generation": 1}) + "\n")
		atomic_write_bytes(os.path.join(self.repo_dir, "keycheck.bin"), self.cipher.encrypt(KEYCHECK_TEXT, "keycheck"))
		self.git("add", "-A")
		self.git("commit", "-m", COMMIT_MESSAGE)
		result = self.git("push", "-u", "origin", "HEAD:" + self.branch, network=True, check=False)
		if not result.ok:
			raise GitError("Envoi initial impossible (une autre machine a-t-elle été configurée en même temps ?) : %s"
						   % (result.err.strip().splitlines() or ["?"])[-1])

	def check_format(self) -> None:
		data = read_bytes(os.path.join(self.repo_dir, "csync-format.json"))
		if data is None:
			raise CSyncError("Ce dépôt n'est pas un dépôt de données CSync (csync-format.json absent).")
		try:
			fmt = json.loads(data.decode("utf-8")).get("format")
		except ValueError:
			raise CSyncError("csync-format.json est illisible.")
		if fmt != DATA_FORMAT:
			raise CSyncError("Format de données %s non pris en charge par cette version de CSync (attendu : %s). "
							 "Mettez CSync à jour sur toutes les machines." % (fmt, DATA_FORMAT))

	def verify_key(self) -> None:
		blob = read_bytes(os.path.join(self.repo_dir, "keycheck.bin"))
		if blob is None:
			raise CSyncError("keycheck.bin absent du dépôt de données.")
		try:
			ok = self.cipher.decrypt(blob, "keycheck") == KEYCHECK_TEXT
		except CSyncError:
			ok = False
		if not ok:
			raise CSyncError("La clé utilisée sur cette machine ne correspond pas à celle du dépôt de données. "
							 "Copiez sur cette machine le même fichier de clé que sur l'autre.")

	def _remove_stale_locks(self) -> None:
		"""Supprime les verrous laissés par un git interrompu (veille, coupure, délai dépassé).

		Appelé uniquement sous le verrou de CSync : aucun autre git ne travaille sur ce dépôt."""
		git_dir = os.path.join(self.repo_dir, ".git")
		candidates = [os.path.join(git_dir, n) for n in ("index.lock", "HEAD.lock", "packed-refs.lock", "shallow.lock",
														 "config.lock", "ORIG_HEAD.lock", "FETCH_HEAD.lock")]
		for dirpath, _, files in os.walk(os.path.join(git_dir, "refs")):
			candidates.extend(os.path.join(dirpath, f) for f in files if f.endswith(".lock"))
		for path in candidates:
			if os.path.exists(path):
				LOG.warning("Verrou git abandonné supprimé : %s", path)
				remove_file(path)

	def recover_dirty(self) -> None:
		"""Remet le dépôt local dans un état propre avant d'intégrer les données distantes.

		Cas normaux : des blocs calculés lors d'une réception attendent le prochain envoi.
		Cas rares : une opération précédente a été interrompue (verrou, rebase en cours)."""
		self._remove_stale_locks()
		git_dir = os.path.join(self.repo_dir, ".git")
		if os.path.isdir(os.path.join(git_dir, "rebase-merge")) or os.path.isdir(os.path.join(git_dir, "rebase-apply")):
			LOG.warning("Rebase interrompu détecté : annulation.")
			self.git("rebase", "--abort", check=False)
		if not os.path.exists(os.path.join(self.repo_dir, ".gitignore")) and self.looks_initialized():
			atomic_write_text(os.path.join(self.repo_dir, ".gitignore"), GITIGNORE)
		status = self.git("status", "--porcelain", check=False)
		if status.ok and status.out.strip():
			self.git("add", "-A")
			self.git("commit", "-m", COMMIT_MESSAGE, check=False)

	def _net_fail_marker(self) -> str:
		return os.path.join(self.repo_dir, ".git", "csync-net-fail")

	def fetch_and_rebase(self) -> None:
		"""Récupère les données des autres machines. Nos fichiers ne recoupent jamais les leurs.

		Dans un hook (self.light), un réseau absent ou lent ne bloque pas Claude Code : on
		travaille avec les données déjà récupérées, et après un échec on ne réessaie pas
		avant dix minutes."""
		import time
		self.recover_dirty()
		upstream = "origin/" + self.branch
		previous = self.git("rev-parse", "--verify", "-q", upstream, check=False)
		previous_head = previous.out.strip() if previous.ok else None
		if self.light:
			try:
				if time.time() - os.path.getmtime(self._net_fail_marker()) < 600:
					LOG.info("Réseau indisponible récemment : données déjà récupérées utilisées.")
					return
			except OSError:
				pass
			result = self.git("fetch", "origin", self.branch, network=True, check=False)
			if not result.ok:
				atomic_write_text(self._net_fail_marker(), "1\n")
				LOG.warning("Récupération impossible pendant le hook : %s",
							(result.err.strip().splitlines() or ["?"])[-1])
				return
			remove_file(self._net_fail_marker())
		else:
			if not self.remote_has_branch():
				return
			self.git("fetch", "origin", self.branch, network=True)
			remove_file(self._net_fail_marker())
		if not self.git("rev-parse", "--verify", "-q", upstream, check=False).ok:
			return
		if not self.has_commits():
			self.git("checkout", "-B", self.branch, upstream)
			return
		if not self.git("merge-base", "HEAD", upstream, check=False).ok:
			# Historique distant recréé (compactage par une autre machine) : on repart de lui
			# en conservant nos propres fichiers.
			self._reroot(upstream, previous_head)
			return
		result = self.git("rebase", upstream, check=False)
		if not result.ok:
			# Cas anormal (par exemple : cette machine a été retirée du dépôt depuis une autre).
			# Chaque machine n'écrit que ses propres fichiers : on repart de l'historique distant
			# en y replaçant les nôtres, ce qui est toujours correct.
			self.git("rebase", "--abort", check=False)
			LOG.warning("Intégration par rebase impossible (%s) : réalignement.",
						(result.err.strip().splitlines() or ["?"])[-1])
			self._reroot(upstream, previous_head)

	def _own_paths(self) -> List[str]:
		return [os.path.join("objects", self.machine_id), os.path.join("manifests", self.machine_id),
				os.path.join("profiles", self.machine_id + ".bin")]

	def _pushed_objects(self, ref: Optional[str]) -> Optional[Set[str]]:
		"""Nos blocs déjà présents sur GitHub à la référence donnée (None si inconnu)."""
		if not ref:
			return None
		result = self.git("ls-tree", "-r", "--name-only", ref, "--", "objects/" + self.machine_id, check=False)
		if not result.ok:
			return None
		return {os.path.basename(l)[:-4] for l in result.out.splitlines() if l.endswith(".bin")}

	def _own_referenced(self) -> Optional[Set[str]]:
		"""Blocs utilisés par notre propre état publié (None s'il est illisible)."""
		try:
			doc = self.read_manifest(self.machine_id)
		except (CSyncError, ValueError, OSError):
			return None
		if doc is None:
			return set()
		refs: Set[str] = set()
		for entry in doc[1].values():
			if isinstance(entry, dict):
				refs.update(entry.get("chunks") or [])
				if entry.get("oid"):
					refs.add(entry["oid"])
		return refs

	def _reroot(self, upstream: str, previous_head: Optional[str] = None) -> None:
		LOG.warning("Historique du dépôt de données recréé ailleurs : réalignement.")
		# Parmi nos blocs, on ne rapporte que ceux qui servent encore ou qui n'avaient jamais été
		# envoyés : ceux que le compactage a retirés parce que plus personne ne les utilisait
		# ne reviennent pas.
		pushed = self._pushed_objects(previous_head)
		referenced = self._own_referenced()
		saved = tempfile.mkdtemp(prefix="csync-reroot-")
		try:
			for rel in self._own_paths():
				src = os.path.join(self.repo_dir, rel)
				if os.path.isdir(src):
					shutil.copytree(src, os.path.join(saved, rel))
				elif os.path.isfile(src):
					ensure_dir(os.path.dirname(os.path.join(saved, rel)))
					shutil.copy2(src, os.path.join(saved, rel))
			self.git("checkout", "-B", self.branch, upstream)
			self.git("reset", "--hard", upstream)
			# Nos propres fichiers font foi : notre état et notre profil remplacent ceux du dépôt
			# recréé (qui peuvent être plus anciens), nos blocs complètent les siens.
			own_manifest = os.path.join(self.repo_dir, "manifests", self.machine_id)
			if os.path.isdir(os.path.join(saved, "manifests", self.machine_id)):
				shutil.rmtree(own_manifest, ignore_errors=True)
			for rel in self._own_paths():
				src = os.path.join(saved, rel)
				dst = os.path.join(self.repo_dir, rel)
				if os.path.isdir(src):
					for dirpath, _, files in os.walk(src):
						for f in files:
							s = os.path.join(dirpath, f)
							d = os.path.join(dst, os.path.relpath(s, src))
							if rel.startswith("objects"):
								if os.path.exists(d):
									continue
								oid = f[:-4] if f.endswith(".bin") else f
								if (pushed is not None and referenced is not None and oid in pushed
										and oid not in referenced):
									continue
							ensure_dir(os.path.dirname(d))
							shutil.copy2(s, d)
				elif os.path.isfile(src):
					ensure_dir(os.path.dirname(dst))
					shutil.copy2(src, dst)
			self.git("add", "-A")
			self.git("commit", "-m", COMMIT_MESSAGE, check=False)
		finally:
			shutil.rmtree(saved, ignore_errors=True)
		# L'ancien historique n'est plus utile : on libère la place qu'il occupait ici
		# (dans un hook, ce nettoyage est laissé au prochain passage ordinaire).
		if self.light:
			self._prune_pending = True
		else:
			self._release_old_history()

	def _release_old_history(self) -> None:
		self.git("reflog", "expire", "--expire=now", "--all", check=False)
		self.git("gc", "--prune=now", "--quiet", check=False)
		marker = os.path.join(self.repo_dir, ".git", "csync-prune-pending")
		remove_file(marker)

	@property
	def _prune_pending(self) -> bool:
		return os.path.exists(os.path.join(self.repo_dir, ".git", "csync-prune-pending"))

	@_prune_pending.setter
	def _prune_pending(self, value: bool) -> None:
		marker = os.path.join(self.repo_dir, ".git", "csync-prune-pending")
		if value:
			atomic_write_text(marker, "1\n")
		else:
			remove_file(marker)

	def maintenance(self) -> None:
		"""Nettoyage de git, hors hooks : libère l'ancien historique après un réalignement et
		regroupe les fichiers internes quand ils deviennent nombreux."""
		if self._prune_pending:
			self._release_old_history()
		self.git("-c", "gc.auto=6700", "gc", "--auto", "--quiet", check=False)

	def commit_and_push(self, attempts: int = 3) -> bool:
		"""Enregistre et envoie nos fichiers. Renvoie False s'il n'y avait rien à envoyer."""
		self.git("add", "-A")
		staged = self.git("diff", "--cached", "--quiet", check=False)
		committed = False
		if staged.code == 1:
			self.git("commit", "-m", COMMIT_MESSAGE)
			committed = True
		ahead = self._ahead_count()
		if not committed and ahead == 0:
			return False
		last_error = None
		for _ in range(attempts):
			result = self.git("push", "origin", "HEAD:" + self.branch, network=True, check=False)
			if result.ok:
				return True
			last_error = (result.err.strip().splitlines() or ["?"])[-1]
			# Une autre machine a envoyé entre-temps : on intègre puis on réessaie.
			self.fetch_and_rebase()
		raise GitError("Envoi impossible après %d tentatives : %s" % (attempts, last_error))

	def _ahead_count(self) -> int:
		result = self.git("rev-list", "--count", "origin/%s..HEAD" % self.branch, check=False)
		if not result.ok:
			return 1 if self.has_commits() else 0
		try:
			return int(result.out.strip() or "0")
		except ValueError:
			return 0

	def size_mb(self) -> float:
		total = 0
		for dirpath, _, files in os.walk(os.path.join(self.repo_dir, ".git", "objects")):
			for f in files:
				try:
					total += os.path.getsize(os.path.join(dirpath, f))
				except OSError:
					pass
		return total / (1024 * 1024)

	# ------------------------------------------------------------------
	# Compactage
	# ------------------------------------------------------------------

	def compact(self, referenced: Set[str]) -> Tuple[int, float, float]:
		"""Supprime les blocs que plus aucune machine n'utilise et repart d'un historique d'un seul commit.

		Les autres machines se réalignent d'elles-mêmes à leur prochaine synchronisation.
		Renvoie (blocs supprimés, taille avant, taille après) en Mo."""
		before = self.size_mb()
		removed = 0
		base = os.path.join(self.repo_dir, "objects")
		for dirpath, _, files in os.walk(base):
			for f in files:
				if f.endswith(".bin") and f[:-4] not in referenced:
					remove_file(os.path.join(dirpath, f))
					removed += 1
		old_head = self.git("rev-parse", "origin/" + self.branch).out.strip()
		gen_path = os.path.join(self.repo_dir, "generation.json")
		try:
			generation = int(json.loads((read_bytes(gen_path) or b"{}").decode("utf-8")).get("generation", 1))
		except (ValueError, AttributeError):
			generation = 1
		atomic_write_text(gen_path, json.dumps({"generation": generation + 1}) + "\n")
		self.git("branch", "-D", "csync-compact", check=False)   # reste d'une tentative interrompue
		self.git("checkout", "--orphan", "csync-compact")
		self.git("add", "-A")
		self.git("commit", "-m", COMMIT_MESSAGE)
		self.git("branch", "-M", self.branch)
		result = self.git("push", "--force-with-lease=%s:%s" % (self.branch, old_head), "origin",
						  "HEAD:" + self.branch, network=True, check=False)
		if not result.ok:
			raise GitError("Compactage non envoyé (une autre machine vient d'envoyer ; réessayez) : %s"
						   % (result.err.strip().splitlines() or ["?"])[-1])
		self.git("fetch", "origin", self.branch, network=True, check=False)
		self.git("reflog", "expire", "--expire=now", "--all", check=False)
		self.git("gc", "--prune=now", "--quiet", check=False)
		return removed, before, self.size_mb()

	# ------------------------------------------------------------------
	# Objets
	# ------------------------------------------------------------------

	def _object_path(self, machine: str, oid: str) -> str:
		return os.path.join(self.repo_dir, "objects", machine, oid[:2], oid + ".bin")

	def machine_dirs(self) -> List[str]:
		base = os.path.join(self.repo_dir, "objects")
		try:
			names = sorted(os.listdir(base))
		except FileNotFoundError:
			return [self.machine_id]
		if self.machine_id in names:
			names.remove(self.machine_id)
		return [self.machine_id] + names

	def find_object(self, oid: str) -> Optional[str]:
		for machine in self.machine_dirs():
			path = self._object_path(machine, oid)
			if os.path.exists(path):
				return path
		return None

	def put_object(self, plaintext: bytes) -> str:
		oid = self.cipher.object_id(plaintext)
		if self.find_object(oid) is None:
			atomic_write_bytes(self._object_path(self.machine_id, oid), self.cipher.encrypt(plaintext, "object:" + oid))
		return oid

	def get_object(self, oid: str) -> bytes:
		cached = self._chunk_cache.get(oid)
		if cached is not None:
			return cached
		path = self.find_object(oid)
		if path is None:
			raise CSyncError("Contenu %s absent du dépôt de données." % oid[:12])
		plaintext = self.cipher.decrypt(read_bytes(path), "object:" + oid)
		if self.cipher.object_id(plaintext) != oid:
			raise CSyncError("Contenu %s altéré (identifiant incohérent)." % oid[:12])
		if len(self._chunk_cache) > 256:
			self._chunk_cache.clear()
		self._chunk_cache[oid] = plaintext
		return plaintext

	# ------------------------------------------------------------------
	# Manifestes (répartis en morceaux) et profils
	# ------------------------------------------------------------------

	def _encrypt_json(self, obj: Any, context: str) -> bytes:
		data = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
		return self.cipher.encrypt(data, context)

	def _decrypt_json(self, blob: bytes, context: str) -> Any:
		return json.loads(self.cipher.decrypt(blob, context).decode("utf-8"))

	def _write_if_changed(self, path: str, obj: Any, context: str) -> bool:
		old = read_bytes(path)
		if old is not None:
			try:
				if self._decrypt_json(old, context) == obj:
					return False
			except (CSyncError, ValueError):
				pass
		atomic_write_bytes(path, self._encrypt_json(obj, context))
		return True

	def write_manifest(self, meta: Dict[str, Any], entries: Dict[str, Any]) -> bool:
		"""Écrit notre état ; seuls les morceaux modifiés changent. Renvoie True si quelque chose a changé."""
		base = os.path.join(self.repo_dir, "manifests", self.machine_id)
		ensure_dir(base)
		shards: Dict[str, Dict[str, Any]] = {}
		for key, entry in entries.items():
			shards.setdefault(shard_of(key), {})[key] = entry
		changed = False
		for name, content in shards.items():
			if self._write_if_changed(os.path.join(base, name + ".bin"), content,
									  "manifest:%s:%s" % (self.machine_id, name)):
				changed = True
		for f in os.listdir(base):
			if f.endswith(".bin") and f != "meta.bin" and f[:-4] not in shards:
				remove_file(os.path.join(base, f))
				changed = True
		meta_path = os.path.join(base, "meta.bin")
		if changed or not os.path.exists(meta_path):
			self._write_if_changed(meta_path, meta, "manifest-meta:%s" % self.machine_id)
			changed = True
		return changed

	def read_manifest(self, machine: str) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
		base = os.path.join(self.repo_dir, "manifests", machine)
		meta_blob = read_bytes(os.path.join(base, "meta.bin"))
		if meta_blob is None:
			return None
		meta = self._decrypt_json(meta_blob, "manifest-meta:%s" % machine)
		entries: Dict[str, Any] = {}
		try:
			names = sorted(os.listdir(base))
		except OSError:
			names = []
		for f in names:
			if f.endswith(".bin") and f != "meta.bin":
				blob = read_bytes(os.path.join(base, f))
				if blob is None:
					continue   # morceau retiré pendant la lecture
				entries.update(self._decrypt_json(blob, "manifest:%s:%s" % (machine, f[:-4])))
		return meta, entries

	def write_profile(self, profile: Dict[str, Any]) -> None:
		path = os.path.join(self.repo_dir, "profiles", self.machine_id + ".bin")
		ensure_dir(os.path.dirname(path))
		self._write_if_changed(path, profile, "profile:" + self.machine_id)

	def read_profile(self, machine: str) -> Optional[Dict[str, Any]]:
		blob = read_bytes(os.path.join(self.repo_dir, "profiles", machine + ".bin"))
		if blob is None:
			return None
		return self._decrypt_json(blob, "profile:" + machine)

	def write_cards(self, cards: Dict[str, Any]) -> None:
		"""Fiches réduites de la barre latérale de l'app Claude (voir appcards.py)."""
		path = os.path.join(self.repo_dir, "cards", self.machine_id + ".bin")
		ensure_dir(os.path.dirname(path))
		self._write_if_changed(path, cards, "cards:" + self.machine_id)

	def other_cards(self) -> Dict[str, Dict[str, Any]]:
		out = {}
		base = os.path.join(self.repo_dir, "cards")
		try:
			names = sorted(n[:-4] for n in os.listdir(base) if n.endswith(".bin"))
		except FileNotFoundError:
			names = []
		for machine in names:
			if machine == self.machine_id:
				continue
			try:
				data = self._decrypt_json(read_bytes(os.path.join(base, machine + ".bin")) or b"", "cards:" + machine)
			except (CSyncError, ValueError) as exc:
				LOG.error("Fiches de la machine %s illisibles : %s", machine, exc)
				continue
			if isinstance(data, dict):
				out[machine] = data
		return out

	def forget_machine(self, machine: str) -> None:
		"""Retire l'état et le profil d'une machine disparue. Ses contenus encore utilisés par
		d'autres restent en place ; les autres partiront au prochain compactage."""
		if not machine or machine == self.machine_id or "/" in machine or machine.startswith("."):
			raise CSyncError("Machine invalide : %s" % machine)
		shutil.rmtree(os.path.join(self.repo_dir, "manifests", machine), ignore_errors=True)
		remove_file(os.path.join(self.repo_dir, "profiles", machine + ".bin"))
		remove_file(os.path.join(self.repo_dir, "cards", machine + ".bin"))

	def list_machines(self) -> List[str]:
		base = os.path.join(self.repo_dir, "manifests")
		try:
			return sorted(n for n in os.listdir(base) if os.path.isdir(os.path.join(base, n)))
		except FileNotFoundError:
			return []

	def other_manifests(self) -> Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]:
		out = {}
		for machine in self.list_machines():
			if machine == self.machine_id:
				continue
			try:
				doc = self.read_manifest(machine)
			except (CSyncError, ValueError) as exc:
				LOG.error("Lecture impossible de l'état de la machine %s : %s", machine, exc)
				continue
			if doc is not None:
				out[machine] = doc
		return out

	def other_profiles(self) -> Dict[str, Dict[str, Any]]:
		out = {}
		base = os.path.join(self.repo_dir, "profiles")
		try:
			names = sorted(n[:-4] for n in os.listdir(base) if n.endswith(".bin"))
		except FileNotFoundError:
			names = []
		for machine in names:
			if machine == self.machine_id:
				continue
			try:
				prof = self.read_profile(machine)
			except (CSyncError, ValueError) as exc:
				LOG.error("Lecture impossible du profil %s : %s", machine, exc)
				continue
			if prof is not None:
				out[machine] = prof
		return out
