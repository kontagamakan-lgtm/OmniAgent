"""Approbation humaine au niveau de l'outil, avant tout dispatch.

Une proposition s'approuvait au niveau de la tâche ; un outil dangereux avait
une politique, mais l'utilisateur ne voyait ni le diff, ni la commande, ni le
domaine au moment de l'action. Cette porte s'intercale entre le modèle et
l'exécution :

* `required(tool)` décide si l'appel doit être approuvé (`dangerous`, `write`
  ou tout, selon `approvals.mode`) ;
* `authorize(tool, arguments)` construit un aperçu — diff pour une écriture,
  arborescence pour une suppression, commande pour le shell, domaine pour le
  web — et demande une décision : **une fois**, **pour la session**, **refuser** ;
* chaque décision est journalisée dans `data/approvals.jsonl`, sans recopier le
  contenu sensible.

Un refus ou une panne de l'interface vaut refus : l'outil n'est jamais dispatché
avant la décision.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from config import APP_ID, Settings
from core.profile import extract_host
from tools.base import Tool
from tools.capabilities import CAPABILITIES, ToolCapabilities

__all__ = [
    "ApprovalGate",
    "ApprovalRequest",
    "approval_log_path",
    "build_approval_gate",
    "build_preview",
    "preview_for",
]

logger = logging.getLogger(f"{APP_ID}.approval")

ONCE = "once"
SESSION = "session"
DENY = "deny"

_SECRET_KEYS = {
    "password",
    "passphrase",
    "secret",
    "token",
    "value",
    "content",
    "data",
    "cookie",
    "otp",
}
_PREVIEW_MAX = 1600


def approval_log_path(settings: Settings) -> Path:
    return settings.paths.data_dir / "approvals.jsonl"


def _redact_text(text: str) -> str:
    try:
        from security.redaction import redact

        return str(redact(text))
    except Exception:  # noqa: BLE001 - le masquage ne doit pas bloquer l'aperçu
        return str(text)


def _digest(arguments: Mapping[str, Any]) -> str:
    body = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _text(path: Path, limit: int = 200_000) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None


def _unified(old: str, new: str) -> str:
    diff = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile="actuel",
            tofile="propose",
            lineterm="",
            n=2,
        )
    )
    return "\n".join(diff[:60])


def _tree_summary(path: Path, budget: int = 200) -> str:
    """Compte et taille d'une cible, bornés pour rester un aperçu."""
    if path.is_file():
        return f"1 fichier, {path.stat().st_size} octet(s)"
    if not path.is_dir():
        return "cible absente"
    count = 0
    size = 0
    for item in path.rglob("*"):
        if count >= budget:
            return f"{count}+ fichier(s), {size}+ octet(s)"
        if item.is_file():
            try:
                size += item.stat().st_size
                count += 1
            except OSError:
                continue
    return f"{count} fichier(s), {size} octet(s)"


def preview_for(tool: str, arguments: Mapping[str, Any]) -> str:
    """Aperçu lisible de l'effet, sans exécuter l'outil."""
    name = str(tool)
    path = str(arguments.get("path") or arguments.get("source") or "")
    if name == "fs_write_text":
        target = Path(path) if path else None
        mode = "ajout" if arguments.get("append") else "remplacement"
        content = str(arguments.get("content") or "")
        if target is None:
            return f"Ecriture ({mode}) sans chemin."
        old = _text(target)
        if old is None:
            head = content[:1200]
            return f"Creation de {target} ({len(content)} caracteres) :\n{head}"
        diff = _unified(old, content)
        return f"{mode.capitalize()} de {target} :\n{diff or '(contenu identique)'}"
    if name == "fs_delete":
        target = Path(path) if path else None
        if target is None:
            return "Suppression sans chemin."
        return f"Suppression definitive de {target} ({_tree_summary(target)})."
    if name in {"fs_move", "fs_copy"}:
        destination = arguments.get("destination") or arguments.get("dest") or ""
        action = "Deplacement" if name == "fs_move" else "Copie"
        return f"{action} : {path} -> {destination}"
    if name == "shell_run":
        command = str(arguments.get("command") or "")
        cwd = str(arguments.get("cwd") or "")
        return f"Commande shell : {command}" + (f"\nRepertoire : {cwd}" if cwd else "")
    if name in {"web_fetch", "browser_open", "browser_fetch_rendered", "web_search"}:
        url = next(
            (
                str(arguments.get(key))
                for key in ("url", "href", "start_url", "page_url")
                if arguments.get(key)
            ),
            "",
        )
        host = extract_host(arguments) or "(moteur de recherche)"
        return f"Acces reseau ({host}) : {url or arguments.get('query', '')}"
    if name.startswith("vault_"):
        service = arguments.get("service") or arguments.get("name") or ""
        return f"Operation coffre-fort : {name}" + (f" sur {service}" if service else "")
    if name in {"archive", "fs_archive"}:
        return f"Creation d'archive : {arguments.get('destination') or ''}"
    if name == "fs_extract":
        return f"Extraction de {path} vers {arguments.get('destination') or ''}"
    return f"{name} : {_compact(arguments)}"


def _compact(arguments: Mapping[str, Any]) -> str:
    safe = {
        key: ("***" if any(word in str(key).lower() for word in _SECRET_KEYS) else value)
        for key, value in arguments.items()
    }
    text = json.dumps(safe, ensure_ascii=False, default=str)
    return text[:500]


@dataclass(slots=True)
class ApprovalRequest:
    """Ce que l'utilisateur doit voir et décider."""

    tool: str
    scope: str
    effect: str
    preview: str
    arguments_digest: str = ""
    dangerous: bool = False

    def format(self) -> str:
        return f"{self.tool} ({self.scope}, {self.effect})\n\n{self.preview}"


@dataclass(slots=True)
class ApprovalGate:
    """Décision once/session/deny, journalisée, avec session en mémoire."""

    decide: Callable[[ApprovalRequest], str | bool] | None = None
    mode: str = "dangerous"
    enabled: bool = True
    session_grants: bool = True
    settings: Settings | None = None
    _grants: set[str] = field(default_factory=set, repr=False)

    def required(self, tool: Tool) -> bool:
        if not self.enabled or self.mode == "none":
            return False
        capabilities = CAPABILITIES.get(str(getattr(tool, "name", "")))
        if self.mode == "all":
            return True
        if self.mode == "write":
            if capabilities is not None:
                return capabilities.writes
            return not bool(getattr(tool, "read_only", True))
        if capabilities is not None:
            return capabilities.dangerous
        return bool(getattr(tool, "dangerous", False))

    def build_request(self, tool: Tool, arguments: Mapping[str, Any]) -> ApprovalRequest:
        name = str(getattr(tool, "name", ""))
        capabilities: ToolCapabilities | None = CAPABILITIES.get(name)
        return ApprovalRequest(
            tool=name,
            scope=capabilities.scope if capabilities else "outil",
            effect=capabilities.effect if capabilities else (
                "read" if getattr(tool, "read_only", True) else "write"
            ),
            preview=preview_for(name, arguments)[:_PREVIEW_MAX],
            arguments_digest=_digest(arguments),
            dangerous=bool(getattr(tool, "dangerous", False)),
        )

    def authorize(self, tool: Tool, arguments: Mapping[str, Any]) -> tuple[bool, str]:
        """(autorisé, raison) ; toute panne ou absence de décideur refuse."""
        name = str(getattr(tool, "name", ""))
        if self.session_grants and name in self._grants:
            return True, "deja autorise pour la session"
        request = self.build_request(tool, arguments)
        if self.decide is None:
            self._journal(request, DENY, "aucun decideur")
            return False, "confirmation requise : aucun decideur disponible"
        try:
            answer = self.decide(request)
        except Exception:  # noqa: BLE001 - un decideur defaillant refuse
            logger.exception("approbation impossible pour %s", name)
            self._journal(request, DENY, "decideur en echec")
            return False, "confirmation impossible : refus par defaut"
        decision = _normalize(answer)
        if decision == SESSION and self.session_grants:
            self._grants.add(name)
        self._journal(request, decision, "")
        if decision == DENY:
            return False, f"action '{name}' refusee par l'utilisateur"
        return True, decision

    def clear_session(self) -> None:
        self._grants.clear()

    def _journal(self, request: ApprovalRequest, decision: str, note: str) -> None:
        if self.settings is None:
            return
        record = {
            "at": datetime.now(tz=timezone.utc).isoformat(),
            "tool": request.tool,
            "scope": request.scope,
            "effect": request.effect,
            "decision": decision,
            "arguments_digest": request.arguments_digest,
            "note": note,
            # Résumé masqué : ni contenu de fichier, ni secret, ni commande brute.
            "summary": _redact_text(request.preview.splitlines()[0][:200]),
        }
        path = approval_log_path(self.settings)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file() and path.stat().st_size > 2 * 1024 * 1024:
                path.replace(path.with_name(path.name + ".1"))
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            logger.debug("decision d'approbation non journalisee", exc_info=True)


def build_approval_gate(
    settings: Settings,
    decide: Callable[[ApprovalRequest], str | bool] | None = None,
) -> ApprovalGate:
    """Porte réglée par `approvals.*`, avec le décideur fourni (facultatif)."""
    cfg = getattr(settings, "approvals", None)
    return ApprovalGate(
        decide=decide,
        mode=str(getattr(cfg, "mode", "dangerous") or "dangerous"),
        enabled=bool(getattr(cfg, "enabled", True)),
        session_grants=bool(getattr(cfg, "session_grants", True)),
        settings=settings,
    )


def _normalize(answer: str | bool) -> str:
    if isinstance(answer, bool):
        return ONCE if answer else DENY
    value = str(answer or "").strip().lower()
    if value in {"once", "une fois", "oui", "allow", "true"}:
        return ONCE
    if value in {"session", "pour la session", "always", "toujours"}:
        return SESSION
    return DENY


def build_preview(tool: str, arguments: Mapping[str, Any]) -> str:
    """Compatibilité : aperçu seul, sans décision."""
    return preview_for(tool, arguments)
