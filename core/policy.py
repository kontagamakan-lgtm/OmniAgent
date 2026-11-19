"""Politique d'outils du mode autonome, déclarée par l'utilisateur.

Le mode autonome refusait *tous* les outils dangereux : sûr, mais inutilisable
dès qu'il faut écrire un rapport ou lister un dossier. `autonomy/policy.json`
permet d'autoriser au cas par cas, avec des règles ordonnées :

    {
      "rules": [
        {"tool": "fs_write", "action": "allow",
         "args": {"path": ["rapports/", "reports/"]}},
        {"tool": "shell_run", "action": "allow",
         "args": {"command": ["dir", "Get-ChildItem", "Get-Date"]}},
        {"tool": "*", "action": "deny"}
      ]
    }

Règles :

* la première règle qui correspond décide ; sans correspondance, refus ;
* `tool: "*"` attrape tout ; une valeur de `args` est comparée sans tenir
  compte de la casse, par préfixe ou par motif glob (`rapports/*.md`) ;
* un argument absent ne correspond à aucune règle d'argument ;
* un fichier absent, illisible ou invalide équivaut à « tout refuser » — une
  politique qu'on ne sait pas lire ne doit pas élargir les droits ;
* chaque décision (autorisée ou refusée) est journalisée avec la règle
  appliquée.

La politique ne s'applique qu'aux outils marqués dangereux : les outils de
lecture restent disponibles comme avant.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from config import APP_ID, Settings
from core.autonomy import autonomy_dir

__all__ = ["POLICY_NAME", "ToolPolicy", "load_policy", "policy_path"]

logger = logging.getLogger(f"{APP_ID}.policy")

POLICY_NAME = "policy.json"


def policy_path(settings: Settings) -> Path:
    return autonomy_dir(settings) / POLICY_NAME


@dataclass(slots=True)
class ToolPolicy:
    """Règles ordonnées ; absence de fichier = refus par défaut."""

    rules: list[dict[str, Any]] = field(default_factory=list)
    source: str = ""
    _counts: dict[str, int] = field(default_factory=dict, repr=False)

    def decide(self, tool: str, arguments: Mapping[str, Any]) -> tuple[bool, str]:
        """(autorisé, règle appliquée) ; refus si rien ne correspond."""
        name = str(tool or "")
        for index, rule in enumerate(self.rules):
            if not _tool_matches(rule.get("tool"), name):
                continue
            if not _args_match(rule.get("args"), arguments):
                continue
            action = str(rule.get("action") or "deny").lower()
            if action not in {"allow", "deny"}:
                continue
            label = f"regle {index + 1} ({rule.get('tool') or '*'}/{action})"
            if action == "deny":
                return False, label
            # Plafond par outil : au-dela, le refus est immediat et ne cherche
            # pas une regle suivante plus permissive.
            try:
                maximum = int(rule.get("max_calls") or 0)
            except (TypeError, ValueError):
                maximum = 0
            if maximum > 0:
                used = self._counts.get(name, 0)
                if used >= maximum:
                    return False, f"{label}, plafond {maximum} atteint"
                self._counts[name] = used + 1
            return True, label
        return False, "aucune regle : refus par defaut"

    def describe(self) -> str:
        if not self.rules:
            return "aucune politique : tous les outils dangereux sont refuses"
        return f"{len(self.rules)} regle(s) depuis {self.source}"


def _tool_matches(pattern: Any, name: str) -> bool:
    text = str(pattern or "")
    if not text:
        return False
    return text == "*" or fnmatch.fnmatchcase(name, text)


def _args_match(rules: Any, arguments: Mapping[str, Any]) -> bool:
    """Toutes les contraintes d'argument doivent être satisfaites."""
    if rules is None:
        return True
    if not isinstance(rules, dict):
        return False
    for key, expected in rules.items():
        if key not in arguments:
            return False
        value = arguments.get(key)
        candidates = [value] if not isinstance(value, list) else value
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(_one_matches(candidate, pattern) for candidate in candidates for pattern in allowed):
            return False
    return True


def _one_matches(candidate: Any, pattern: Any) -> bool:
    text = str(candidate or "").replace("\\", "/").lower()
    wanted = str(pattern or "").replace("\\", "/").lower()
    if not wanted:
        return False
    if any(char in wanted for char in "*?["):
        return fnmatch.fnmatchcase(text, wanted)
    return text.startswith(wanted)


def load_policy(settings: Settings) -> ToolPolicy:
    """Charge `autonomy/policy.json` ; toute erreur donne une politique vide."""
    path = policy_path(settings)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ToolPolicy(source=str(path))
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("politique d'outils illisible : tout est refuse (%s)", path)
        return ToolPolicy(source=str(path))
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        logger.warning("politique d'outils sans liste 'rules' : tout est refuse")
        return ToolPolicy(source=str(path))
    clean = [rule for rule in rules if isinstance(rule, dict)]
    return ToolPolicy(rules=clean, source=str(path))
