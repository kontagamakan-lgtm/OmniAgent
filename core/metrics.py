"""Métriques par run : un agrégat borné à côté des traces.

La trace raconte un run en détail ; ces métriques répondent aux questions de
suivi : combien de runs, combien échouent, quels outils échouent, combien de
tokens et de temps, quel modèle. Un seul enregistrement par run terminé, donc
un volume négligeable, et une borne en entrées et en octets comme le journal
d'autonomie.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import APP_ID, Settings
from core.cost import estimate_cost
from core.events import EventType
from core.locking import LockTimeout, file_lock

__all__ = [
    "METRICS_NAME",
    "MetricsRecorder",
    "build_metrics",
    "format_summary",
    "metrics_path",
    "summarize",
]

logger = logging.getLogger(f"{APP_ID}.metrics")

METRICS_NAME = "metrics.jsonl"


def metrics_path(settings: Settings) -> Path:
    return settings.paths.log_dir / METRICS_NAME


def build_metrics(settings: Settings) -> "MetricsRecorder | None":
    """Enregistreur si `metrics.enabled`, sinon `None` (rien sur disque)."""
    cfg = getattr(settings, "metrics", None)
    if cfg is None or not cfg.enabled:
        return None
    return MetricsRecorder(
        metrics_path(settings),
        max_entries=cfg.max_entries,
        max_bytes=cfg.max_bytes,
    )


class MetricsRecorder:
    """Abonné au bus : accumule un run, écrit une ligne à `run_finished`."""

    def __init__(
        self,
        path: Path | str,
        *,
        max_entries: int = 1000,
        max_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self.path = Path(path)
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1024, int(max_bytes))
        self._reset()

    def _reset(self) -> None:
        self._steps = 0
        self._tools: Counter[str] = Counter()
        self._errors: Counter[str] = Counter()
        self._trims = 0
        self._trimmed_tokens = 0
        self._retries = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._model = ""
        self._workspace = ""

    def __call__(self, event: Any) -> None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        kind = event.type
        if kind == EventType.RUN_STARTED:
            self._reset()
            self._model = str(payload.get("model") or "")
            self._workspace = str(payload.get("workspace") or "")
        elif kind == EventType.STEP_STARTED:
            self._steps += 1
        elif kind == EventType.LLM_RESPONSE:
            tokens = payload.get("tokens") or {}
            self._prompt_tokens += int(tokens.get("prompt", 0) or 0)
            self._completion_tokens += int(tokens.get("completion", 0) or 0)
        elif kind == EventType.LLM_RETRY:
            self._retries += 1
        elif kind == EventType.TOOL_STARTED:
            self._tools[str(payload.get("tool") or "?")] += 1
        elif kind == EventType.TOOL_FINISHED:
            if not payload.get("ok", True):
                self._errors[str(payload.get("tool") or "?")] += 1
        elif kind == EventType.CONTEXT_TRIMMED:
            self._trims += 1
            self._trimmed_tokens += int(payload.get("tokens_before", 0) or 0) - int(
                payload.get("tokens_after", 0) or 0
            )
        elif kind == EventType.RUN_FINISHED:
            self._finish(event.run_id, payload)

    def _finish(self, run_id: str, payload: dict[str, Any]) -> None:
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        record = {
            "at": datetime.now(tz=timezone.utc).isoformat(),
            "run_id": run_id,
            "ok": bool(payload.get("ok")),
            "stop_reason": str(payload.get("stop_reason") or ""),
            "model": self._model,
            "workspace": self._workspace,
            "duration_s": float(summary.get("duration_s", 0.0) or 0.0),
            "steps": int(summary.get("steps", self._steps) or 0),
            "tool_calls": int(
                summary.get("tool_calls", sum(self._tools.values())) or 0
            ),
            "tool_errors": dict(self._errors),
            "requests": int(summary.get("requests", 0) or 0),
            "prompt_tokens": int(summary.get("prompt_tokens", self._prompt_tokens) or 0),
            "completion_tokens": int(
                summary.get("completion_tokens", self._completion_tokens) or 0
            ),
            "trims": int(summary.get("trims", self._trims) or 0),
            "trimmed_tokens": int(
                summary.get("trimmed_tokens", self._trimmed_tokens) or 0
            ),
            "retries": self._retries,
        }
        record["cost"] = estimate_cost(
            record["model"], record["prompt_tokens"], record["completion_tokens"]
        )
        self._write(record)

    def _write(self, record: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(self.path.with_name(f"{self.path.name}.lock")):
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(record, ensure_ascii=False, default=str) + "\n"
                    )
                    handle.flush()
                self._prune_locked()
        except LockTimeout:
            logger.warning("metriques verrouillees : run non enregistre")
            return
        except OSError:
            logger.debug("métrique non écrite", exc_info=True)
            return

    def _prune_locked(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        lines = [line for line in raw.splitlines() if line.strip()]
        if len(lines) <= self.max_entries and len(raw.encode("utf-8")) <= self.max_bytes:
            return
        kept = lines[-self.max_entries :]
        total = sum(len(line.encode("utf-8")) + 1 for line in kept)
        while len(kept) > 1 and total > self.max_bytes:
            total -= len(kept[0].encode("utf-8")) + 1
            kept.pop(0)
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            with temp.open("w", encoding="utf-8") as handle:
                for line in kept:
                    handle.write(line + "\n")
            os.replace(temp, self.path)
        except OSError:
            logger.debug("compaction des métriques impossible", exc_info=True)
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def all(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                records.append(data)
        return records


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Agrégat : réussite, tokens, temps, outils en échec, modèles."""
    runs = len(records)
    ok = sum(1 for record in records if record.get("ok"))
    errors: Counter[str] = Counter()
    models: Counter[str] = Counter()
    for record in records:
        tool_errors = record.get("tool_errors")
        if isinstance(tool_errors, dict):
            for name, count in tool_errors.items():
                errors[str(name)] += int(count or 0)
        models[str(record.get("model") or "?")] += 1
    return {
        "runs": runs,
        "ok": ok,
        "failed": runs - ok,
        "duration_s": round(sum(float(r.get("duration_s", 0) or 0) for r in records), 1),
        "steps": sum(int(r.get("steps", 0) or 0) for r in records),
        "tool_calls": sum(int(r.get("tool_calls", 0) or 0) for r in records),
        "prompt_tokens": sum(int(r.get("prompt_tokens", 0) or 0) for r in records),
        "completion_tokens": sum(
            int(r.get("completion_tokens", 0) or 0) for r in records
        ),
        "cost": round(sum(float(r.get("cost", 0) or 0) for r in records), 4),
        "trims": sum(int(r.get("trims", 0) or 0) for r in records),
        "tool_errors": dict(errors.most_common(8)),
        "models": dict(models.most_common(5)),
    }


def format_summary(records: list[dict[str, Any]]) -> str:
    """Rendu court, sans caractère hors cp1252."""
    if not records:
        return "Aucune métrique enregistrée."
    data = summarize(records)
    lines = [
        f"Runs : {data['runs']} ({data['ok']} ok, {data['failed']} echec(s))",
        f"Tokens : {data['prompt_tokens'] + data['completion_tokens']} "
        f"(prompt {data['prompt_tokens']}, completion {data['completion_tokens']})",
        f"Duree : {data['duration_s']} s, {data['steps']} etape(s), "
        f"{data['tool_calls']} appel(s) d'outil, {data['trims']} repli(s) de contexte",
        f"Cout estime : {data['cost']:.4f} USD",
    ]
    if data["tool_errors"]:
        errors = ", ".join(f"{name}:{count}" for name, count in data["tool_errors"].items())
        lines.append(f"Outils en echec : {errors}")
    if data["models"]:
        models = ", ".join(f"{name}:{count}" for name, count in data["models"].items())
        lines.append(f"Modeles : {models}")
    from core.anomalies import detect_anomalies

    alerts = detect_anomalies(records)
    if alerts:
        lines.append("Alertes :")
        lines.extend(f"- {alert}" for alert in alerts)
    return "\n".join(lines)
