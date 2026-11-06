"""Réparation assistée par IA de l'adaptateur de format.

Déclenchée uniquement à la demande, quand la vérification du format échoue (par exemple
après une mise à jour de Claude Code). Le principe :

1. CSync décrit la structure des fichiers (noms de clés et types, jamais de contenu).
2. Claude Code, lancé en mode non interactif, propose un nouvel adaptateur.
3. CSync teste cet adaptateur sur les vrais fichiers. S'il échoue, il est rejeté.
4. Vous l'approuvez (ou non) : rien n'est appliqué sans votre accord.

L'appel n'enregistre aucune session (--no-session-persistence) et désactive tous les
hooks (--settings), ce qui empêche CSync de se déclencher lui-même en boucle.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional, Tuple

from .adapter import DEFAULT_ADAPTER, SLUG_RULES, FormatReport, check_format, load_adapter, structure_summary
from .paths import Paths
from .util import CSyncError, ensure_dir, read_json, remove_file, run, which, write_json

PROMPT = """Tu aides l'outil CSync à lire les fichiers de Claude Code après un changement de format.

CSync a besoin de savoir :
- session_cwd_key : la clé (éventuellement imbriquée, avec des points) qui donne, dans chaque ligne
  JSON d'une session, le dossier de travail absolu ;
- session_id_key : la clé qui donne l'identifiant de la session ;
- claude_json_mcp_key : la clé de ~/.claude.json qui contient les serveurs MCP personnels
  (un dictionnaire nom -> configuration) ;
- claude_json_projects_key : la clé de ~/.claude.json qui contient l'état par projet,
  indexé par chemin absolu ;
- slug_rule : la règle de nommage des dossiers de projet ; seule valeur possible : "nonalnum-dash".

Voici l'adaptateur actuel, le résultat de la vérification, et la structure anonymisée des fichiers :

{payload}

Réponds UNIQUEMENT avec un objet JSON contenant exactement ces cinq clés, sans aucun texte autour.
Si une information est introuvable dans la structure, reprends la valeur de l'adaptateur actuel.
"""


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
	text = text.strip()
	candidates = [text]
	m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
	if m:
		candidates.insert(0, m.group(1))
	start, end = text.find("{"), text.rfind("}")
	if start != -1 and end > start:
		candidates.append(text[start:end + 1])
	for c in candidates:
		try:
			obj = json.loads(c)
		except ValueError:
			continue
		if isinstance(obj, dict):
			return obj
	return None


def _clean(candidate: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
	result = dict(current)
	for key in DEFAULT_ADAPTER:
		if key == "version":
			continue
		value = candidate.get(key)
		if isinstance(value, str) and 0 < len(value) <= 80 and re.fullmatch(r"[A-Za-z0-9_.\-]+", value):
			result[key] = value
	if result.get("slug_rule") not in SLUG_RULES:
		result["slug_rule"] = current.get("slug_rule", DEFAULT_ADAPTER["slug_rule"])
	return result


def propose(paths: Paths) -> Tuple[Dict[str, Any], FormatReport, FormatReport]:
	"""Demande une proposition à Claude Code et la teste. Renvoie (proposition, avant, après)."""
	from .util import claude_env, find_claude
	claude = find_claude()
	if claude is None:
		raise CSyncError("Claude Code (commande « claude ») est introuvable : réparation automatique impossible.")
	current = load_adapter(paths.adapter_file)
	before = check_format(current, paths.projects_dir, paths.claude_json)
	payload = {
		"verification": before.to_dict(),
		"structure": structure_summary(paths.projects_dir, paths.claude_json, current),
	}
	prompt = PROMPT.replace("{payload}", json.dumps(payload, ensure_ascii=False, indent=2))
	workdir = ensure_dir(paths.ai_dir, 0o700)
	# La consigne passe par l'entrée standard (« claude -p » la lit quand aucun argument n'est donné).
	result = run(
		[claude, "-p", "--settings", json.dumps({"disableAllHooks": True}), "--no-session-persistence"],
		cwd=workdir, timeout=240, env=claude_env(), input_text=prompt,
	)
	if not result.ok:
		raise CSyncError("L'appel à Claude Code a échoué : %s" % (result.err.strip() or result.out.strip())[:400])
	candidate = _extract_json(result.out)
	if candidate is None:
		raise CSyncError("La réponse de Claude Code ne contient pas d'adaptateur exploitable.")
	proposal = _clean(candidate, current)
	after = check_format(proposal, paths.projects_dir, paths.claude_json)
	write_json(paths.pending_adapter_file, {"adapter": proposal, "before": before.to_dict(),
											"after": after.to_dict()}, 0o600)
	return proposal, before, after


def pending(paths: Paths) -> Optional[Dict[str, Any]]:
	return read_json(paths.pending_adapter_file, None)


def approve(paths: Paths) -> Dict[str, Any]:
	data = pending(paths)
	if not data:
		raise CSyncError("Aucune proposition de réparation en attente.")
	proposal = data["adapter"]
	after = check_format(proposal, paths.projects_dir, paths.claude_json)
	if not after.ok:
		raise CSyncError("La proposition ne passe plus la vérification ; elle n'est pas appliquée.")
	write_json(paths.adapter_file, proposal, 0o600)
	remove_file(paths.pending_adapter_file)
	return proposal


def reject(paths: Paths) -> None:
	remove_file(paths.pending_adapter_file)


def reset(paths: Paths) -> None:
	"""Revient à l'adaptateur par défaut."""
	remove_file(paths.adapter_file)
