"""Chargeur dynamique de compétences du cortex évolutif.

Les compétences sont des modules Python autonomes, écrits dans
`evolution_core/skills/`. Chaque module expose :

* `NAME` (facultatif) : nom court, sinon le nom du fichier ;
* `DESCRIPTION` : phrase affichée dans le catalogue d'outils ;
* `PARAMETERS` : schéma JSON des arguments (objet) ;
* `run(**kwargs)` : fonction appelée par l'agent.

Le chargeur ne charge **que** ce dossier, valide la source (taille, syntaxe,
imports et appels interdits, présence de `run`) puis recharge le module via
`importlib.reload` : une compétence réécrite est disponible sans redémarrer
l'application.

La validation statique n'est pas un bac à sable : un module validé s'exécute
dans le processus de l'agent. C'est pourquoi les compétences générées passent
d'abord par un test isolé (`core.self_evolver`), que les outils exposés sont
`dangerous` et que seules les écritures sous `evolution_core/` sont permises.
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from config import APP_ID
from tools.base import FunctionTool, Tool

__all__ = [
    "FORBIDDEN_CALLS",
    "FORBIDDEN_IMPORTS",
    "SKILL_PREFIX",
    "SkillError",
    "SkillLoader",
    "SkillSpec",
    "validate_skill_source",
]

logger = logging.getLogger(f"{APP_ID}.skills")

SKILL_PREFIX = "skill_"
#: Modules qui donneraient à une compétence un accès direct au système.
FORBIDDEN_IMPORTS = frozenset(
    {
        "subprocess",
        "socket",
        "ctypes",
        "winreg",
        "multiprocessing",
        "pty",
        "resource",
        "_winapi",
    }
)
#: Appels qui contourneraient la validation et l'exécution contrôlée.
FORBIDDEN_CALLS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "system",
        "popen",
        "spawn",
        "spawnl",
        "spawnv",
        "execv",
        "execve",
        "execl",
        "startfile",
    }
)
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
_DEFAULT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
#: Suffixe réservé aux fichiers de test, jamais chargés comme compétences.
_TEST_SUFFIX = ".test.py"


class SkillError(ValueError):
    """Compétence refusée : source invalide, contrat incomplet ou rechargement raté."""


@dataclass(slots=True)
class SkillSpec:
    """Description d'une compétence chargée, pour l'affichage et les tests."""

    name: str
    description: str
    path: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "description": self.description, "path": self.path}


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def validate_skill_source(source: str, *, max_chars: int = 20000) -> ast.Module:
    """Valide une compétence et retourne son AST, ou lève `SkillError`."""
    text = str(source or "")
    if not text.strip():
        raise SkillError("compétence vide")
    if len(text) > max(1000, int(max_chars)):
        raise SkillError(f"compétence trop longue ({len(text)} caractères)")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise SkillError(f"syntaxe invalide ligne {exc.lineno}: {exc.msg}") from exc

    has_run = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".")[0] for alias in node.names}
            forbidden = roots & FORBIDDEN_IMPORTS
            if forbidden:
                raise SkillError(f"import interdit : {', '.join(sorted(forbidden))}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in FORBIDDEN_IMPORTS:
                raise SkillError(f"import interdit : {root}")
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in FORBIDDEN_CALLS:
                raise SkillError(f"appel interdit : {name}()")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "run":
                has_run = True
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise SkillError(f"attribut réservé interdit : {node.attr}")
        elif isinstance(node, ast.Name) and node.id.startswith("__"):
            if node.id not in {"__name__", "__doc__"}:
                raise SkillError(f"nom réservé interdit : {node.id}")
    if not has_run:
        raise SkillError("la compétence doit définir run(**kwargs)")
    return tree


class SkillLoader:
    """Découvre, valide, charge et recharge les modules de `skills/`."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_chars: int = 20000,
    ) -> None:
        self.root = Path(root).resolve()
        self.max_chars = max(1000, int(max_chars))
        self._tools: dict[str, Tool] = {}
        self._specs: dict[str, SkillSpec] = {}
        self.errors: dict[str, str] = {}

    # -- découverte ---------------------------------------------------------

    def discover(self) -> list[Path]:
        """Fichiers `.py` candidats, hors tests et fichiers masqués."""
        try:
            candidates = sorted(self.root.glob("*.py"))
        except OSError:
            return []
        return [
            path
            for path in candidates
            if path.is_file()
            and not path.name.startswith(("_", "."))
            and not path.name.endswith(_TEST_SUFFIX)
        ]

    def _path_for(self, name: str) -> Path:
        """Chemin d'une compétence, confiné au dossier `skills/`."""
        safe = str(name or "").strip().lower()
        if not _SAFE_NAME.fullmatch(safe):
            raise SkillError(f"nom de compétence invalide : {name!r}")
        path = (self.root / f"{safe}.py").resolve()
        if path.parent != self.root:
            raise SkillError("compétence hors du dossier autorisé")
        return path

    # -- chargement ---------------------------------------------------------

    def load(self, path: str | Path) -> Tool:
        """Charge ou recharge un module ; lève `SkillError` s'il est refusé."""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = (self.root / candidate).resolve()
        if candidate.parent != self.root:
            raise SkillError("compétence hors du dossier autorisé")
        try:
            source = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillError(f"lecture impossible : {exc}") from exc
        validate_skill_source(source, max_chars=self.max_chars)

        module_name = f"omniagent_skill_{candidate.stem}"
        importlib.invalidate_caches()
        try:
            # `importlib.reload` exige qu'un finder connaisse le nom ; un module
            # chargé par chemin n'en a pas. On ré-exécute donc le fichier dans un
            # module neuf : même effet de rechargement à chaud, sans redémarrage.
            spec = importlib.util.spec_from_file_location(module_name, candidate)
            if spec is None or spec.loader is None:
                raise SkillError("module non chargeable")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except SkillError:
            sys.modules.pop(module_name, None)
            raise
        except Exception as exc:  # noqa: BLE001 - une compétence fautive est isolée
            sys.modules.pop(module_name, None)
            raise SkillError(f"exécution du module impossible : {type(exc).__name__}: {exc}") from exc

        tool = self._tool_for(module, candidate)
        self._tools[tool.name] = tool
        self._specs[tool.name] = SkillSpec(
            name=tool.name,
            description=tool.description,
            path=str(candidate),
        )
        self.errors.pop(candidate.stem, None)
        return tool

    def _tool_for(self, module: Any, path: Path) -> Tool:
        raw_name = str(getattr(module, "NAME", "") or path.stem).strip().lower()
        if raw_name.startswith(SKILL_PREFIX):
            raw_name = raw_name[len(SKILL_PREFIX) :]
        if not _SAFE_NAME.fullmatch(raw_name):
            raise SkillError(f"NAME invalide : {raw_name!r}")
        parameters = getattr(module, "PARAMETERS", _DEFAULT_SCHEMA)
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise SkillError("PARAMETERS doit être un schéma objet")
        run = getattr(module, "run", None)
        if not callable(run):
            raise SkillError("run doit être appelable")
        description = str(getattr(module, "DESCRIPTION", "") or "").strip()[:240]
        return FunctionTool(
            f"{SKILL_PREFIX}{raw_name}",
            description or f"Compétence auto-générée ({raw_name}).",
            dict(parameters),
            run,
            read_only=False,
            dangerous=True,
        )

    def load_all(self) -> list[Tool]:
        """Charge chaque compétence ; une compétence fautive n'empêche pas les autres."""
        loaded: list[Tool] = []
        for path in self.discover():
            try:
                loaded.append(self.load(path))
            except SkillError as exc:
                self.errors[path.stem] = str(exc)
                logger.warning("compétence %s ignorée : %s", path.name, exc)
        return loaded

    def unload(self, name: str) -> None:
        """Retire une compétence du registre et de `sys.modules`."""
        tool_name = name if name.startswith(SKILL_PREFIX) else f"{SKILL_PREFIX}{name}"
        self._tools.pop(tool_name, None)
        self._specs.pop(tool_name, None)
        sys.modules.pop(f"omniagent_skill_{tool_name[len(SKILL_PREFIX) :]}", None)

    # -- exécution ----------------------------------------------------------

    def run(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Exécute une compétence chargée (ou la charge à la demande)."""
        tool_name = name if name.startswith(SKILL_PREFIX) else f"{SKILL_PREFIX}{name}"
        tool = self._tools.get(tool_name)
        if tool is None:
            tool = self.load(self._path_for(tool_name[len(SKILL_PREFIX) :]))
        result = tool.execute(**dict(arguments or {}))
        if not result.ok:
            raise SkillError(
                str(getattr(result, "error", "") or "compétence en échec")
            )
        return result.data

    # -- introspection ------------------------------------------------------

    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict[str, str]]:
        return [
            self._specs[name].as_dict() for name in sorted(self._specs) if name in self._specs
        ]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        tool_name = str(name)
        if not tool_name.startswith(SKILL_PREFIX):
            tool_name = f"{SKILL_PREFIX}{tool_name}"
        return tool_name in self._tools


def load_tools(loader: SkillLoader) -> Iterable[Tool]:
    """Compatibilité : outils actuellement chargés."""
    return loader.tools()
