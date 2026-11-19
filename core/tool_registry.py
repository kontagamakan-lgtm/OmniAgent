"""Registre d'outils : schémas pour le modèle, dispatch, garde-fous.

Il ajoute à `ToolSet` les règles qui relèvent de la boucle et non des outils :

* les outils marqués `dangerous` ne s'exécutent pas sans confirmation explicite ;
* la sortie transmise au modèle est bornée (`agent.tool_output_max_chars`) ;
* les compteurs par outil permettent de repérer un outil qui échoue en boucle ;
* un résultat sensible n'est jamais sérialisé vers le modèle (délégué à
  `ToolResult.to_text()`), mais reste disponible pour le code local.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

from config import APP_ID
from core.prompts import tool_catalogue
from tools.base import Tool, ToolError, ToolResult, ToolSet
from tools.spec import ToolSpec

if TYPE_CHECKING:  # pragma: no cover - import de typage uniquement
    from core.approval import ApprovalGate

__all__ = ["NetworkGuard", "ToolError", "ToolRegistry", "ToolSpec"]

logger = logging.getLogger(f"{APP_ID}.registry")

ConfirmHandler = Callable[[Tool, Mapping[str, Any]], bool]
NetworkGuard = Callable[[str, Mapping[str, Any]], tuple[bool, str]]


class ToolRegistry:
    """Façade d'exécution sûre autour d'un ensemble d'outils."""

    def __init__(
        self,
        tools: Iterable[Tool | ToolSpec] | ToolSet = (),
        *,
        max_output_chars: int = 12000,
        confirm: ConfirmHandler | None = None,
        network_guard: NetworkGuard | None = None,
        approval: "ApprovalGate | None" = None,
    ) -> None:
        if isinstance(tools, ToolSet):
            self._tools = tools
        else:
            self._tools = ToolSet(_as_tool(item) for item in tools)
        self.max_output_chars = max(0, int(max_output_chars))
        self._confirm = confirm
        self._network_guard = network_guard
        self._approval = approval
        # Décideur propre à la porte (souvent la boîte de dialogue de
        # l'interface), distinct du confirmateur historique que le mode
        # autonome remplace puis restaure.
        self._approval_decide = approval.decide if approval is not None else None
        self._calls: dict[str, int] = {}
        self._failures: dict[str, int] = {}
        self._total_ms: dict[str, float] = {}

    # -- introspection ------------------------------------------------------

    @property
    def toolset(self) -> ToolSet:
        return self._tools

    def names(self) -> list[str]:
        return self._tools.names()

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        """Schémas « function calling » transmis au modèle."""
        return self._tools.schemas()

    def catalogue(self) -> str:
        """Catalogue lisible inséré dans le prompt système."""
        return tool_catalogue(self._tools.tools())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    # -- exécution ----------------------------------------------------------

    def dispatch(self, name: str, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        """Exécute un outil après application des garde-fous."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.failure(
                f"outil inconnu '{name}' ; disponibles : {', '.join(self._tools.names())}",
                code="unknown_tool",
            )
        if arguments is not None and not isinstance(arguments, Mapping):
            return ToolResult.failure(
                "les arguments doivent former un objet JSON", code="invalid_arguments"
            )
        payload = dict(arguments or {})

        if self._network_guard is not None:
            try:
                allowed, reason = self._network_guard(tool.name, payload)
            except Exception:  # noqa: BLE001 - un garde défaillant refuse
                logger.exception("garde réseau en échec pour %s", tool.name)
                allowed, reason = False, "garde réseau en échec : refus par défaut"
            if not allowed:
                return ToolResult.failure(reason or "sortie réseau refusée", code="network_denied")

        if self._approval is not None and self._approval.required(tool):
            allowed, reason = self._approval.authorize(tool, payload)
            if not allowed:
                return ToolResult.failure(
                    reason or f"action '{tool.name}' non approuvee",
                    code="confirmation_required",
                )
        elif tool.dangerous and not self._authorize(tool, payload):
            return ToolResult.failure(
                f"action dangereuse '{tool.name}' non confirmée : demande l'accord "
                "de l'utilisateur avant de réessayer",
                code="confirmation_required",
            )

        self._calls[tool.name] = self._calls.get(tool.name, 0) + 1
        result = tool.execute(**payload)
        self._total_ms[tool.name] = self._total_ms.get(tool.name, 0.0) + result.duration_ms
        if not result.ok:
            self._failures[tool.name] = self._failures.get(tool.name, 0) + 1
        return result

    def set_confirm(self, confirm: ConfirmHandler | None) -> ConfirmHandler | None:
        """Remplace le confirmateur et retourne le précédent.

        Le mode autonome refuse les outils dangereux pendant sa session, puis
        restaure le confirmateur de l'interface : l'utilisateur ne doit pas
        hériter d'un refus silencieux après l'arrêt. Restaurer `None` ne doit
        pas non plus effacer le décideur propre à la porte d'approbation.
        """
        from core.approval import decision_from_confirm

        previous = self._confirm
        self._confirm = confirm
        if self._approval is not None:
            self._approval.decide = (
                decision_from_confirm(confirm)
                if confirm is not None
                else self._approval_decide
            )
        return previous

    def set_approval(self, approval: "ApprovalGate | None") -> "ApprovalGate | None":
        """Installe la porte d'approbation (interface) et retourne la précédente."""
        previous = self._approval
        self._approval = approval
        self._approval_decide = approval.decide if approval is not None else None
        return previous

    def _authorize(self, tool: Tool, arguments: Mapping[str, Any]) -> bool:
        if self._confirm is None:
            logger.warning("outil dangereux %s refusé faute de confirmation", tool.name)
            return False
        try:
            return bool(self._confirm(tool, arguments))
        except Exception:  # noqa: BLE001 - un confirmateur défaillant refuse
            logger.exception("confirmation impossible pour %s", tool.name)
            return False

    def observation(self, result: ToolResult, max_chars: int | None = None) -> str:
        """Texte transmis au modèle pour un résultat d'outil."""
        return result.to_text(max_chars=self.max_output_chars if max_chars is None else max_chars)

    # -- statistiques -------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Compteurs par outil : appels, échecs, temps cumulé."""
        return {
            name: {
                "calls": self._calls.get(name, 0),
                "failures": self._failures.get(name, 0),
                "total_ms": round(self._total_ms.get(name, 0.0), 1),
            }
            for name in sorted(self._calls)
        }

    def reset_stats(self) -> None:
        """Remet les compteurs à zéro (nouvelle conversation)."""
        self._calls.clear()
        self._failures.clear()
        self._total_ms.clear()


def _as_tool(item: Tool | ToolSpec) -> Tool:
    """Accepte indifféremment un `Tool` déjà formé ou une `ToolSpec`."""
    return item.as_tool() if isinstance(item, ToolSpec) else item
