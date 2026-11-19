"""Boucle ReAct : raisonner, agir, observer, jusqu'à la réponse finale.

La boucle est volontairement déterministe et bornée :

* un nombre maximal d'étapes (`agent.max_steps`) — au-delà, l'agent conclut
  avec ce qu'il a, au lieu de tourner indéfiniment ;
* des reprises limitées (`agent.max_recovery_attempts`) sur les erreurs
  transitoires du fournisseur, avec attente exponentielle ;
* un garde-fou anti-boucle : une action identique déjà lancée trois fois n'est
  plus **exécutée** du tout, la boucle s'arrête ;
* une interruption coopérative (`cancel()`) vérifiée avant chaque étape et
  chaque outil.

L'historique envoyé au fournisseur doit rester protocolaire : chaque
`tool_call` listé dans un message assistant reçoit exactement une réponse
`tool`, les appels non exécutés n'apparaissent pas, et les identifiants sont
uniques même quand le fournisseur n'en fournit pas.
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from config import APP_ID, APP_VERSION, TRACE_FORMAT_VERSION, Settings
from core.context_manager import TrimResult, count_tokens, messages_tokens, plan_trim
from core.errors import (
    AgentError,
    BudgetExhausted,
    Cancelled,
    LoopGuardTripped,
    RecoveryExhausted,
    describe,
    is_retryable,
    retry_delay,
)
from core.events import EventBus, EventType
from core.fingerprint import read_fingerprint
from core.llm_client import LLMClient, LLMResponse
from core.observability import reset_run_id, set_run_id
from core.prompts import budget_notice, finalization_prompt, recovery_notice, system_prompt
from core.react_parser import ParsedToolCall, final_text
from core.tool_registry import ToolRegistry
from core.trace import workspace_slug
from tools.base import ToolResult

__all__ = ["AgentCore", "AgentReply", "AgentStep", "StepCall"]

logger = logging.getLogger(f"{APP_ID}.agent")

_UNSET = object()
_SIGNATURE_LIMIT = 3
_BUDGET_FALLBACK = "Limite d'étapes atteinte sans réponse finale exploitable."


@dataclass(slots=True)
class StepCall:
    """Un appel d'outil exécuté pendant une étape."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    error_code: str = ""
    duration_ms: float = 0.0
    sensitive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": self.arguments,
            "ok": self.ok,
            "error_code": self.error_code,
            "duration_ms": self.duration_ms,
            "sensitive": self.sensitive,
        }


@dataclass(slots=True)
class AgentStep:
    """Une itération de la boucle et ce qu'elle a produit."""

    index: int
    thought: str = ""
    calls: list[StepCall] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "thought": self.thought,
            "calls": [call.to_dict() for call in self.calls],
        }


@dataclass(slots=True)
class AgentReply:
    """Résultat complet d'une exécution."""

    text: str
    steps: list[AgentStep] = field(default_factory=list)
    stop_reason: str = "final"
    ok: bool = True
    error: str = ""
    error_code: str = ""
    recoveries: int = 0

    @property
    def tool_calls(self) -> int:
        return sum(len(step.calls) for step in self.steps)

    def transcript(self) -> str:
        """Trace lisible des étapes, pour le journal ou l'interface."""
        lines: list[str] = []
        for step in self.steps:
            header = f"Étape {step.index}"
            if step.thought:
                header += f" — {step.thought.strip().splitlines()[0][:160]}"
            lines.append(header)
            for call in step.calls:
                status = "ok" if call.ok else f"échec {call.error_code}"
                lines.append(f"  · {call.name} ({status})")
        if self.stop_reason != "final":
            lines.append(f"Arrêt : {self.stop_reason}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "stop_reason": self.stop_reason,
            "ok": self.ok,
            "error": self.error,
            "error_code": self.error_code,
            "recoveries": self.recoveries,
            "tool_calls": self.tool_calls,
            "steps": [step.to_dict() for step in self.steps],
        }


class AgentCore:
    """Orchestrateur ReAct indépendant de l'interface et du fournisseur."""

    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        registry: ToolRegistry,
        *,
        bus: EventBus | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.registry = registry
        # `bus or …` serait un piège : un bus vide est faux (via __len__).
        self.bus = bus if bus is not None else EventBus()
        self._sleep = sleep
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        # `run_id` et les événements appartiennent à une exécution à la fois :
        # ce verrou refuse la seconde plutôt que de mélanger les deux.
        self._run_guard = threading.Lock()
        self._schemas: list[dict[str, Any]] | None = None
        self._schema_tokens: int | None = None
        #: Replis déjà calculés, par empreinte exacte de l'historique. Une
        #: reprise après panne réémet la même requête : réutiliser le plan garde
        #: le préfixe envoyé au fournisseur **identique à l'octet**, condition
        #: d'un cache de prompt côté fournisseur, et évite un nouveau résumé.
        self._trim_cache: dict[str, TrimResult] = {}
        self._trim_cache_hits = 0
        self._trim_hits_baseline = 0
        self._manifest_digest = ""
        #: Compteurs d'un run, remis à zéro à chaque exécution et publiés dans
        #: `run_finished.summary` : c'est ce qui rend deux runs comparables et le
        #: travail sur le cache de prompt mesurable.
        self._metrics: dict[str, int] = {}
        self._run_started_at = 0.0
        self._run_token: Any = None
        self._reservation_id = ""
        self.run_count = 0
        self.run_id = ""

    # -- pilotage -----------------------------------------------------------

    def cancel(self) -> None:
        """Demande l'arrêt de l'exécution en cours, sans attendre le fournisseur.

        Le drapeau seul serait vérifié au mieux au fragment suivant ; on ferme
        donc aussi le flux HTTP ouvert, ce qui rend la main immédiatement même
        si le fournisseur s'est tu au milieu d'une réponse.
        """
        self._cancel.set()
        abort = getattr(self.llm, "abort", None)
        if callable(abort):
            abort()

    def reset_cancel(self) -> None:
        self._cancel.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def tool_schemas(self) -> list[dict[str, Any]] | None:
        """Schémas transmis au modèle, calculés une seule fois."""
        if self._schemas is None:
            self._schemas = self.registry.schemas()
        return self._schemas or None

    # -- exécution ----------------------------------------------------------

    def run(
        self, task: str, *, max_steps: int | None = None, context: str = ""
    ) -> AgentReply:
        """Exécute la boucle ReAct, une exécution à la fois.

        `run_id` et le flux d'événements sont uniques à l'agent : une seconde
        exécution simultanée mélangerait les deux. Elle est donc refusée avec
        une erreur explicite, plutôt que de corrompre la première.
        """
        if not self._run_guard.acquire(blocking=False):
            return AgentReply(
                text="Une exécution est déjà en cours sur cet agent.",
                stop_reason="busy",
                ok=False,
                error="exécution déjà en cours : attendez la fin ou annulez-la",
                error_code="agent_busy",
            )
        try:
            return self._run_locked(task, max_steps=max_steps, context=context)
        finally:
            # Filet de sécurité : un retour exceptionnel ne doit pas laisser
            # une réservation orpheline bloquer les runs suivants.
            self._settle_budget()
            self._run_guard.release()
            # Le contexte ne doit pas survivre au run : un journal d'interface
            # ne doit pas hériter de l'identifiant de la dernière exécution.
            self._clear_run_context()

    def _clear_run_context(self) -> None:
        token, self._run_token = self._run_token, None
        if token is not None:
            reset_run_id(token)

    def _run_locked(
        self, task: str, *, max_steps: int | None = None, context: str = ""
    ) -> AgentReply:
        """Corps de `run`, exécuté sous le verrou d'exécution."""
        prompt = str(task or "").strip()
        if not prompt:
            return AgentReply(
                text="Tâche vide : rien à exécuter.",
                stop_reason="error",
                ok=False,
                error="tâche vide",
                error_code="empty_task",
            )

        cfg = self.settings.agent
        limit = max(1, int(max_steps or cfg.max_steps))
        self.run_count += 1
        self.run_id = uuid.uuid4().hex[:12]
        # Le journal et les rapports d'outils peuvent alors se rattacher au run.
        self._run_token = set_run_id(self.run_id)
        self._metrics = {
            "trims": 0,
            "trimmed_tokens": 0,
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
        self._trim_hits_baseline = self._trim_cache_hits
        self._run_started_at = time.monotonic()
        with self._lock:
            self.reset_cancel()

        user_content = prompt
        if str(context or "").strip():
            # Reprise d'une trace : l'etat deja atteint est une donnee, pas une
            # instruction systeme ; il reste dans le message utilisateur.
            user_content = (
                f"{prompt}\n\nContexte de reprise (deja effectue, ne pas rejouer) :\n"
                f"{str(context).strip()}"
            )
        try:
            from core.memory import memory_prompt

            memory_text = memory_prompt(self.settings)
        except Exception:  # noqa: BLE001 - une mémoire illisible ne bloque pas le run
            logger.debug("mémoire projet illisible", exc_info=True)
            memory_text = ""
        if memory_text:
            user_content = f"{user_content}\n\n{memory_text}"

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": system_prompt(
                    language=cfg.language,
                    tools=self.registry.toolset.tools(),
                    max_steps=limit,
                    action_first=cfg.action_first,
                ),
            },
            {"role": "user", "content": user_content},
        ]
        steps: list[AgentStep] = []
        signatures: Counter[tuple[str, str]] = Counter()
        recoveries = 0

        self._emit(EventType.RUN_STARTED, self._manifest(prompt, limit))
        # Réservation atomique du coût estimé : deux runs simultanés ne peuvent
        # plus lire le même reste de budget avant que l'un des deux n'écrive.
        blocked = self._reserve_budget(prompt)
        if not blocked:
            blocked = self._budget_block()
        if blocked:
            self._emit(
                EventType.ERROR,
                {"step": 0, "code": "budget_exceeded", "message": blocked},
            )
            return self._finish(
                AgentReply(
                    text=blocked,
                    stop_reason="budget",
                    ok=False,
                    error=blocked,
                    error_code="budget_exceeded",
                )
            )

        for index in range(1, limit + 1):
            if self.cancelled:
                return self._finish(self._cancelled(steps, recoveries))

            self._emit(EventType.STEP_STARTED, {"index": index, "max_steps": limit})
            blocked = self._budget_block()
            if blocked:
                self._emit(
                    EventType.ERROR,
                    {"step": index, "code": "budget_exceeded", "message": blocked},
                )
                return self._finish(
                    AgentReply(
                        text=blocked,
                        steps=steps,
                        stop_reason="budget",
                        ok=False,
                        error=blocked,
                        error_code="budget_exceeded",
                        recoveries=recoveries,
                    )
                )
            try:
                response = self._ask(messages, recoveries, step=index)
            except Cancelled:
                return self._finish(self._cancelled(steps, recoveries))
            except AgentError as exc:
                if is_retryable(exc) and recoveries < cfg.max_recovery_attempts:
                    recoveries += 1
                    delay = retry_delay(exc, recoveries, self.settings.llm.retry_backoff_s)
                    self._emit(
                        EventType.LLM_RETRY,
                        {"attempt": recoveries, "delay_s": delay, **describe(exc)},
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": recovery_notice(
                                exc, attempt=recoveries, max_attempts=cfg.max_recovery_attempts
                            ),
                        }
                    )
                    if delay:
                        self._sleep(delay)
                    continue
                if is_retryable(exc):
                    # Budget de reprises épuisé sur une erreur transitoire :
                    # l'échec est requalifié pour que l'appelant sache pourquoi.
                    exc = RecoveryExhausted(
                        f"{recoveries} reprise(s) sans succès : {exc}",
                        details=describe(exc),
                    )
                return self._finish(self._failure(exc, steps, recoveries, index))

            requested = list(response.tool_calls)
            if not requested:
                # Un « Annuler » pendant la génération de la réponse finale doit
                # être honoré : l'utilisateur a demandé l'arrêt, pas un texte.
                if self.cancelled:
                    return self._finish(self._cancelled(steps, recoveries))
                answer = final_text(response.text)
                if answer:
                    steps.append(AgentStep(index=index, thought=answer))
                    self._emit(EventType.FINAL, {"step": index, "text": answer[:2000]})
                    return self._finish(
                        AgentReply(
                            text=answer,
                            steps=steps,
                            stop_reason="final",
                            recoveries=recoveries,
                        )
                    )
                if recoveries >= cfg.max_recovery_attempts:
                    return self._finish(
                        AgentReply(
                            text="Le modèle n'a produit ni action ni réponse exploitable.",
                            steps=steps,
                            stop_reason="error",
                            ok=False,
                            error="réponse vide",
                            error_code="empty_response",
                            recoveries=recoveries,
                        )
                    )
                recoveries += 1
                messages.append({"role": "user", "content": finalization_prompt()})
                continue

            # On n'exécute qu'un nombre borné d'appels, et on n'inscrit dans
            # l'historique que ceux qui recevront une observation : un
            # `tool_call` sans réponse `tool` rendrait le contexte invalide.
            step = AgentStep(index=index, thought=response.text)
            selected: list[ParsedToolCall] = []
            blocked: str = ""
            for position, call in enumerate(requested):
                if len(selected) >= max(1, int(cfg.parallel_tool_calls)):
                    break
                if signatures[call.signature()] >= _SIGNATURE_LIMIT:
                    blocked = call.name
                    break
                if not call.call_id:
                    call.call_id = f"call_{index}_{position}"
                selected.append(call)

            response.tool_calls = selected
            if selected or response.text.strip():
                messages.append(response.to_message())
            for call in selected:
                signatures[call.signature()] += 1
                record, observation = self._execute(call, index)
                step.calls.append(record)
                messages.append(self._tool_message(call, observation))

            steps.append(step)
            usage = response.usage if isinstance(response.usage, dict) else {}
            self._emit(
                EventType.LLM_RESPONSE,
                {
                    "step": index,
                    "text": response.text[:1000],
                    "tool_calls": len(selected),
                    "deferred": len(requested) - len(selected),
                    "finish_reason": response.finish_reason,
                    # Tokens facturés de la requête : le mode autonome peut
                    # ainsi s'arrêter sur un budget, sans attendre la fin.
                    "tokens": {
                        "prompt": int(
                            usage.get("prompt_tokens")
                            or usage.get("prompt_eval_count")
                            or 0
                        ),
                        "completion": int(
                            usage.get("completion_tokens")
                            or usage.get("eval_count")
                            or 0
                        ),
                    },
                },
            )
            if blocked:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[GARDE-FOU] L'action {blocked} a déjà été lancée "
                            f"{_SIGNATURE_LIMIT} fois à l'identique, elle n'est pas relancée. "
                            "Change d'approche ou réponds avec ce que tu sais."
                        ),
                    }
                )
                note = (
                    f"L'agent a répété la même action ({blocked}) sans progresser : "
                    f"arrêt après {index} étapes."
                )
                return self._finish(
                    AgentReply(
                        text=note,
                        steps=steps,
                        stop_reason="loop_guard",
                        ok=False,
                        error=note,
                        error_code=LoopGuardTripped.code,
                        recoveries=recoveries,
                    )
                )
            if self.cancelled:
                return self._finish(self._cancelled(steps, recoveries))

        # Budget épuisé : une dernière synthèse sans outil. Comme le reste de la
        # boucle, elle réessaie les erreurs transitoires, avec son propre
        # décompte aligné sur `max_recovery_attempts` : les reprises déjà
        # consommées par les étapes ne doivent pas priver la clôture de la
        # dernière chance d'obtenir un bilan. La demande est réémise à
        # l'identique : sans outil à changer, une panne de transport ne se
        # corrige pas par un message supplémentaire.
        messages.append({"role": "user", "content": budget_notice(limit)})
        closing_attempts = 0
        while True:
            try:
                closing = self._ask(
                    messages, recoveries, tools=None, step=limit + 1
                )
                # Un « Annuler » tombé pendant la génération doit l'emporter :
                # sinon le bilan s'afficherait comme un succès de budget.
                if self.cancelled:
                    return self._finish(self._cancelled(steps, recoveries))
                break
            except Cancelled:
                return self._finish(self._cancelled(steps, recoveries))
            except AgentError as exc:
                if is_retryable(exc) and closing_attempts < cfg.max_recovery_attempts:
                    closing_attempts += 1
                    recoveries += 1
                    delay = retry_delay(
                        exc, closing_attempts, self.settings.llm.retry_backoff_s
                    )
                    self._emit(
                        EventType.LLM_RETRY,
                        {
                            "attempt": closing_attempts,
                            "delay_s": delay,
                            "closing": True,
                            **describe(exc),
                        },
                    )
                    if delay:
                        self._sleep(delay)
                    continue
                if is_retryable(exc):
                    # Même requalification que la boucle : le bilan doit dire
                    # que les reprises de la clôture sont parties sans succès.
                    exc = RecoveryExhausted(
                        f"{closing_attempts} reprise(s) sans succès : {exc}",
                        details=describe(exc),
                    )
                return self._finish(
                    AgentReply(
                        text=(
                            f"{_BUDGET_FALLBACK} La synthèse finale a échoué : {exc}"
                        ),
                        steps=steps,
                        stop_reason="budget",
                        ok=False,
                        error=str(exc),
                        error_code=BudgetExhausted.code,
                        recoveries=recoveries,
                    )
                )

        text = final_text(closing.text) or _BUDGET_FALLBACK
        steps.append(AgentStep(index=limit + 1, thought=text))
        self._emit(EventType.FINAL, {"step": limit + 1, "text": text[:2000], "budget": True})
        return self._finish(
            AgentReply(
                text=text,
                steps=steps,
                stop_reason="budget",
                ok=True,
                error_code=BudgetExhausted.code,
                recoveries=recoveries,
            )
        )

    # -- étapes internes ----------------------------------------------------

    def _ask(
        self,
        messages: Sequence[dict[str, Any]],
        recoveries: int,
        *,
        tools: Any = _UNSET,
        step: int = 0,
    ) -> LLMResponse:
        """Un appel au fournisseur, avec vérification d'annulation et flux.

        `tools` par défaut reprend les schémas de l'agent ; la synthèse de
        clôture passe explicitement `None` pour interdire toute nouvelle action.
        Le texte est publié fragment par fragment quand le client et la
        configuration le permettent. Les fragments sont émis **avant** le retour
        de l'appel : l'interface peut les afficher au fil de l'eau.
        """
        if self.cancelled:
            raise Cancelled("exécution annulée")
        trimmed = self._trim(messages)
        chosen = self.tool_schemas if tools is _UNSET else tools
        self._metrics["requests"] = self._metrics.get("requests", 0) + 1
        self._metrics["prompt_tokens"] = self._metrics.get(
            "prompt_tokens", 0
        ) + messages_tokens(trimmed)
        self._emit(EventType.LLM_REQUEST, {"messages": len(trimmed), "recoveries": recoveries})
        if self.settings.llm.stream and getattr(self.llm, "supports_stream", False):
            def _forward(chunk: str) -> None:
                if self.cancelled:
                    raise Cancelled("exécution annulée")
                self._emit(EventType.STREAM_CHUNK, {"step": step, "text": chunk})

            try:
                response = self.llm.stream_complete(trimmed, chosen, on_token=_forward)
            except Cancelled:
                raise
            except AgentError as exc:
                # Le flux a été refermé par `cancel()` : c'est un arrêt voulu,
                # jamais une panne à réessayer.
                if self.cancelled:
                    raise Cancelled("exécution annulée") from exc
                raise
            if self.cancelled:
                raise Cancelled("exécution annulée")
            self._count_response(response)
            return response
        response = self.llm.complete(trimmed, tools=chosen)
        if self.cancelled:
            raise Cancelled("exécution annulée")
        self._count_response(response)
        return response

    def _execute(self, call: ParsedToolCall, step_index: int) -> tuple[StepCall, str]:
        """Exécute un appel d'outil, publie le résultat et retourne son observation."""
        tool = self.registry.get(call.name)
        sensitive = bool(getattr(tool, "sensitive", False))
        payload: dict[str, Any] = {
            "step": step_index,
            "tool": call.name,
            "sensitive": sensitive,
            # Un outil sensible ne publie aucun argument : le masquage par
            # nom de champ ne couvre pas une valeur libre passée sous une
            # clé au nom neutre, et la trace est un fichier durable.
            "arguments": {} if sensitive else _safe_arguments(call.arguments),
        }
        if not sensitive:
            # Empreinte legere des fichiers lus : elle permet a la reprise de
            # dire au modele si le contenu a change depuis la trace.
            fingerprint = read_fingerprint(call.name, call.arguments)
            if fingerprint is not None:
                payload["fingerprint"] = fingerprint
        self._emit(EventType.TOOL_STARTED, payload)
        result: ToolResult = self.registry.dispatch(call.name, call.arguments)
        record = StepCall(
            name=call.name,
            arguments=_safe_arguments(call.arguments),
            ok=result.ok,
            error_code=result.error_code,
            duration_ms=result.duration_ms,
            sensitive=result.sensitive,
        )
        self._emit(
            EventType.TOOL_FINISHED,
            {
                "step": step_index,
                "tool": call.name,
                "ok": result.ok,
                "error_code": result.error_code,
                "duration_ms": result.duration_ms,
                "sensitive": result.sensitive,
            },
        )
        return record, self.registry.observation(result)

    def _tool_message(self, call: ParsedToolCall, observation: str) -> dict[str, Any]:
        """Message `tool` correspondant à un appel, texte borné, jamais de secret."""
        return {
            "role": "tool",
            "tool_call_id": call.call_id,
            "name": call.name,
            "content": observation,
        }

    def _manifest(self, prompt: str, limit: int) -> dict[str, Any]:
        """En-tête de run : de quoi comparer deux exécutions et migrer un format.

        Rien de secret : un condensat des réglages, pas leur contenu ; le nom du
        modèle et du backend, pas la clé. Le condensat ne dépend pas de `paths`,
        qui changent d'une machine à l'autre sans changer de comportement.
        """
        return {
            "task": prompt[:500],
            "max_steps": limit,
            "app_version": APP_VERSION,
            "trace_format": TRACE_FORMAT_VERSION,
            "model": self.settings.llm.model,
            "backend": self.settings.llm.backend,
            "budget_tokens": int(self.settings.agent.max_context_tokens),
            "settings_digest": self._settings_digest(),
            "workspace": workspace_slug(),
            "python": platform.python_version(),
        }

    def _settings_digest(self) -> str:
        """Empreinte courte des réglages non secrets, calculée une fois."""
        if self._manifest_digest:
            return self._manifest_digest
        try:
            payload = self.settings.to_dict()
            payload.pop("paths", None)
            text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001 - un condensat manquant ne bloque rien
            logger.debug("condensat des réglages impossible", exc_info=True)
            return ""
        self._manifest_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        return self._manifest_digest

    def _count_response(self, response: LLMResponse) -> None:
        """Comptabilise la sortie du modèle pour le bilan de run."""
        text = str(getattr(response, "text", "") or "")
        self._metrics["completion_tokens"] = self._metrics.get(
            "completion_tokens", 0
        ) + count_tokens(text)

    def _trim(self, messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Borne l'historique en repartant d'un tour utilisateur complet.

        Couper au hasard séparerait un message assistant porteur de
        `tool_calls` de ses réponses `tool`, ce que le fournisseur rejette.
        Les schémas d'outils, envoyés à chaque appel mais absents de
        `messages`, sont réservés dans le budget.

        Un historique strictement identique réutilise le repli déjà calculé :
        c'est le cas d'une reprise après panne, qui réémet la même requête. Le
        préfixe reste alors identique à l'octet, ce dont dépend le cache de
        prompt du fournisseur.
        """
        key = _messages_digest(messages)
        plan = self._trim_cache.get(key)
        if plan is not None:
            self._trim_cache_hits += 1
            return plan.messages
        plan = plan_trim(
            messages,
            max_messages=max(4, int(self.settings.agent.history_max_messages)),
            max_tokens=int(self.settings.agent.max_context_tokens),
            reserved_tokens=self._reserved_tokens(),
            sensitive_tools=self._sensitive_tool_names(),
        )
        if len(self._trim_cache) >= 8:
            self._trim_cache.clear()
        self._trim_cache[key] = plan
        if plan.trimmed:
            self._metrics["trims"] = self._metrics.get("trims", 0) + 1
            self._metrics["trimmed_tokens"] = self._metrics.get("trimmed_tokens", 0) + max(
                0, plan.before_tokens - plan.after_tokens
            )
            # Sans cet événement, l'utilisateur voit l'agent « oublier » sans
            # savoir que c'est le budget qui l'a demandé.
            self._emit(
                EventType.CONTEXT_TRIMMED,
                {
                    "before_messages": plan.before_messages,
                    "after_messages": plan.after_messages,
                    "before_tokens": plan.before_tokens,
                    "after_tokens": plan.after_tokens,
                    "summarized": plan.summarized,
                    "reserved_tokens": plan.reserved_tokens,
                },
            )
        return plan.messages

    def _sensitive_tool_names(self) -> frozenset[str]:
        """Outils dont les valeurs d'arguments ne doivent pas rejoindre le prompt."""
        return frozenset(
            name
            for name in self.registry.names()
            if getattr(self.registry.get(name), "sensitive", False)
        )

    def _reserved_tokens(self) -> int:
        """Coût fixe des schémas d'outils, calculé une fois par agent."""
        if self._schema_tokens is None:
            schemas = self.tool_schemas or []
            self._schema_tokens = (
                count_tokens(json.dumps(schemas, ensure_ascii=False)) if schemas else 0
            )
        return self._schema_tokens

    def _cancelled(self, steps: list[AgentStep], recoveries: int) -> AgentReply:
        text = "Exécution interrompue à la demande de l'utilisateur."
        return AgentReply(
            text=text,
            steps=steps,
            stop_reason="cancelled",
            ok=False,
            error="annulé",
            error_code=Cancelled.code,
            recoveries=recoveries,
        )

    def _failure(
        self, exc: BaseException, steps: list[AgentStep], recoveries: int, index: int
    ) -> AgentReply:
        text = f"Échec à l'étape {index} : {exc}"
        self._emit(EventType.ERROR, {"step": index, **describe(exc)})
        return AgentReply(
            text=text,
            steps=steps,
            stop_reason="error",
            ok=False,
            error=str(exc),
            error_code=getattr(exc, "code", "agent_error"),
            recoveries=recoveries,
        )

    def _reserve_budget(self, prompt: str) -> str:
        """Réserve le coût estimé du run ; vide si accepté."""
        cfg = getattr(self.settings, "budget", None)
        if cfg is None or not getattr(cfg, "enabled", True):
            return ""
        from core.budget import estimate_run, reserve_run

        cost, tokens = estimate_run(
            self.settings, prompt_tokens=count_tokens(prompt)
        )
        allowed, reason, reservation = reserve_run(
            self.settings, run_id=self.run_id, cost=cost, tokens=tokens
        )
        if not allowed:
            return reason or "budget epuise"
        self._reservation_id = reservation
        return ""

    def _settle_budget(self) -> None:
        """Libère ou consolide la réservation du run avec ses tokens réels."""
        reservation = self._reservation_id
        if not reservation:
            return
        prompt_tokens = max(0, int(self._metrics.get("prompt_tokens", 0) or 0))
        completion_tokens = max(0, int(self._metrics.get("completion_tokens", 0) or 0))
        from core.budget import record_run, release_run

        if prompt_tokens + completion_tokens <= 0:
            if release_run(self.settings, reservation):
                self._reservation_id = ""
            return
        if record_run(
            self.settings,
            run_id=reservation,
            model=str(self.settings.llm.model),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ) is not None:
            self._reservation_id = ""

    def _budget_block(self) -> str:
        """Raison du refus si le budget global est déjà consommé, sinon vide."""
        cfg = getattr(self.settings, "budget", None)
        if cfg is None or not getattr(cfg, "enabled", True):
            return ""
        from core.budget import check, run_cost

        prompt_tokens = int(self._metrics.get("prompt_tokens", 0) or 0)
        completion_tokens = int(self._metrics.get("completion_tokens", 0) or 0)
        cost = run_cost(self.settings, prompt_tokens, completion_tokens)
        state = check(
            self.settings,
            pending_cost=cost,
            pending_tokens=prompt_tokens + completion_tokens,
            # Sa propre estimation est déjà validée : ne pas la compter deux fois.
            exclude_reservation=self._reservation_id,
        )
        return "" if state.allowed else state.reason

    def _finish(self, reply: AgentReply) -> AgentReply:
        """Publie la fin du run avec son bilan chiffré.

        Le manifeste dit dans quelles conditions le run a commencé ; ce résumé
        dit comment il s'est terminé (durée, tokens, replis, reprises). C'est ce
        qui permet de comparer deux exécutions et de mesurer l'effet d'un
        changement de budget.
        """
        # Consolider avant l'événement : le compteur du bus ne doit pas
        # enregistrer une seconde fois le même run.
        self._settle_budget()
        self._emit(
            EventType.RUN_FINISHED,
            {
                "stop_reason": reply.stop_reason,
                "ok": reply.ok,
                "tool_calls": reply.tool_calls,
                "recoveries": reply.recoveries,
                "summary": self._run_summary(reply),
            },
        )
        return reply

    def _run_summary(self, reply: AgentReply) -> dict[str, Any]:
        """Compteurs du run courant, sans contenu de conversation."""
        duration = (
            round(time.monotonic() - self._run_started_at, 3)
            if self._run_started_at
            else 0.0
        )
        return {
            "duration_s": duration,
            "steps": len(reply.steps),
            "tool_calls": reply.tool_calls,
            "recoveries": reply.recoveries,
            "requests": self._metrics.get("requests", 0),
            "prompt_tokens": self._metrics.get("prompt_tokens", 0),
            "completion_tokens": self._metrics.get("completion_tokens", 0),
            "trims": self._metrics.get("trims", 0),
            "trimmed_tokens": self._metrics.get("trimmed_tokens", 0),
            "trim_cache_hits": max(0, self._trim_cache_hits - self._trim_hits_baseline),
        }

    def _emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        self.bus.emit(event_type, payload, run_id=self.run_id)


def _messages_digest(messages: Sequence[dict[str, Any]]) -> str:
    """Empreinte exacte d'un historique, pour reconnaître une requête répétée."""
    try:
        text = json.dumps(messages, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # contenu non sérialisable : pas de cache
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Copie affichable des arguments (tronquée, sérialisable)."""
    cleaned: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str) and len(value) > 200:
            cleaned[key] = value[:200] + "…"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            cleaned[key] = value
        else:
            cleaned[key] = json.dumps(value, ensure_ascii=False, default=str)[:200]
    return cleaned
