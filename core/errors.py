"""Hiérarchie d'exceptions de l'agent et politique de reprise.

Chaque erreur porte trois informations exploitables par la boucle ReAct :
un `code` stable (journalisation, tests, interface), un drapeau `retryable`
(faut-il réessayer la même étape ?) et un éventuel délai d'attente imposé par
le fournisseur (`retry_after_s`). La boucle ne devine donc jamais quoi faire
d'un échec : elle interroge `is_retryable()` et `retry_delay()`.
"""

from __future__ import annotations

__all__ = [
    "AgentError",
    "BudgetExhausted",
    "Cancelled",
    "ConfigError",
    "LLMAuthError",
    "LLMError",
    "LLMRateLimited",
    "LLMResponseError",
    "LoopGuardTripped",
    "RecoveryExhausted",
    "ToolCallError",
    "describe",
    "is_retryable",
    "retry_delay",
]


class AgentError(Exception):
    """Erreur de l'agent, attendue et qualifiée."""

    code = "agent_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        retry_after_s: float | None = None,
        details: object = None,
    ) -> None:
        super().__init__(message)
        if code:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.retry_after_s = retry_after_s
        self.details = details

    def __str__(self) -> str:  # pragma: no cover - confort de message
        return super().__str__()


class ConfigError(AgentError):
    """Configuration incomplète : réessayer ne changerait rien."""

    code = "config_error"


class LLMError(AgentError):
    """Échec de communication avec le fournisseur de génération."""

    code = "llm_error"
    retryable = True


class LLMAuthError(LLMError):
    """Clé absente, invalide ou révoquée : inutile d'insister."""

    code = "llm_auth"
    retryable = False


class LLMRateLimited(LLMError):
    """Quota atteint : réessayer après le délai indiqué par le fournisseur."""

    code = "llm_rate_limited"
    retryable = True


class LLMResponseError(LLMError):
    """Réponse illisible ou incomplète (JSON invalide, structure inattendue)."""

    code = "llm_bad_response"
    retryable = False


class ToolCallError(AgentError):
    """Appel d'outil refusé avant exécution (nom inconnu, arguments invalides)."""

    code = "tool_call_invalid"
    retryable = False


class BudgetExhausted(AgentError):
    """Nombre maximal d'étapes atteint sans réponse finale."""

    code = "budget_exhausted"


class RecoveryExhausted(AgentError):
    """Toutes les tentatives de reprise d'une étape ont échoué."""

    code = "recovery_exhausted"


class LoopGuardTripped(AgentError):
    """L'agent répète la même action : la boucle est interrompue."""

    code = "loop_guard"


class Cancelled(AgentError):
    """Interruption demandée par l'utilisateur ou l'interface."""

    code = "cancelled"


def is_retryable(error: BaseException) -> bool:
    """Indique si l'erreur justifie une nouvelle tentative de la même étape."""
    return isinstance(error, AgentError) and error.retryable


def retry_delay(
    error: BaseException,
    attempt: int,
    base_delay_s: float = 2.0,
    *,
    max_delay_s: float = 60.0,
) -> float:
    """Délai avant la prochaine tentative.

    Le fournisseur gagne toujours (`retry_after_s`), sinon croissance
    exponentielle 1, 2, 4, 8… plafonnée. `attempt` commence à 1.
    """
    imposed = getattr(error, "retry_after_s", None)
    if imposed is not None:
        return max(0.0, min(float(imposed), max_delay_s))
    attempt = max(1, int(attempt))
    delay = max(0.0, float(base_delay_s)) * (2 ** (attempt - 1))
    return min(delay, max_delay_s)


def describe(error: BaseException) -> dict[str, object]:
    """Vue sérialisable pour les événements et le journal."""
    return {
        "type": type(error).__name__,
        "code": getattr(error, "code", "internal_error"),
        "message": str(error),
        "retryable": is_retryable(error),
    }
