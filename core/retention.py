"""Rétention des dossiers de travail : rapport, cache, téléchargements, profil.

Les traces et le journal d'autonomie sont bornés par leurs propres modules.
Ceux-ci ne l'étaient pas. Le principe est le même que pour `runs.index` : une
règle par âge et une par taille, un plan explicite, puis une application qui ne
touche qu'aux fichiers listés. Aucune purge au démarrage.

Le profil navigateur est traité à part : seuls ses sous-dossiers de cache sont
concernés, jamais les cookies, l'historique ou les mots de passe enregistrés.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import Settings

__all__ = [
    "BROWSER_CACHE_DIRS",
    "CleanupPlan",
    "apply_cleanup",
    "format_plans",
    "plan_cleanup",
]

#: Sous-dossiers du profil Playwright/Chromium qui ne contiennent que du cache
#: régénérable. Le reste du profil (Cookies, Login Data, History) est intouché.
BROWSER_CACHE_DIRS = (
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/DawnGraphiteCache",
    "Default/DawnWebGPUCache",
    "Default/Service Worker/CacheStorage",
    "ShaderCache",
    "GrShaderCache",
    "Crashpad",
)


@dataclass(slots=True)
class CleanupPlan:
    """Ce qui serait supprimé, et ce qui resterait."""

    label: str
    directory: Path
    files: list[Path] = field(default_factory=list)
    bytes: int = 0
    total_bytes: int = 0
    total_files: int = 0
    max_age_days: int = 0
    max_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "directory": str(self.directory),
            "files": [str(path) for path in self.files],
            "bytes": self.bytes,
            "total_bytes": self.total_bytes,
            "total_files": self.total_files,
        }


def _files(paths: list[Path]) -> list[tuple[float, int, Path]]:
    found: list[tuple[float, int, Path]] = []
    for root in paths:
        try:
            candidates = list(root.rglob("*")) if root.is_dir() else []
        except OSError:
            continue
        for path in candidates:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            found.append((stat.st_mtime, stat.st_size, path))
    found.sort()
    return found


def _select(
    entries: list[tuple[float, int, Path]], *, max_age_days: int, max_bytes: int
) -> list[Path]:
    """Fichiers à supprimer : trop vieux, puis les plus anciens sous la taille."""
    now = time.time()
    cutoff = now - max_age_days * 86_400 if max_age_days else None
    chosen: list[Path] = []
    remaining = sum(size for _, size, _ in entries)
    for mtime, size, path in entries:
        if cutoff is not None and mtime < cutoff:
            chosen.append(path)
            remaining -= size
    if max_bytes:
        for _mtime, size, path in entries:
            if remaining <= max_bytes:
                break
            if path in chosen:
                continue
            chosen.append(path)
            remaining -= size
    return chosen


def _plan(
    label: str,
    directory: Path,
    roots: list[Path],
    *,
    max_age_days: int,
    max_bytes: int,
) -> CleanupPlan | None:
    entries = _files(roots)
    if not entries:
        return None
    selected = _select(entries, max_age_days=max_age_days, max_bytes=max_bytes)
    chosen = set(selected)
    return CleanupPlan(
        label=label,
        directory=directory,
        files=selected,
        bytes=sum(size for _mtime, size, path in entries if path in chosen),
        total_bytes=sum(size for _mtime, size, _path in entries),
        total_files=len(entries),
        max_age_days=max_age_days,
        max_bytes=max_bytes,
    )


def plan_cleanup(settings: Settings) -> list[CleanupPlan]:
    """Plan de rétention pour les dossiers actifs ; les vides sont omis."""
    cfg = settings.storage
    plans: list[CleanupPlan] = []
    for plan in (
        _plan(
            "rapports",
            settings.paths.report_dir,
            [settings.paths.report_dir],
            max_age_days=cfg.reports_max_age_days,
            max_bytes=cfg.reports_max_bytes,
        ),
        _plan(
            "cache",
            settings.paths.cache_dir,
            [settings.paths.cache_dir],
            max_age_days=cfg.cache_max_age_days,
            max_bytes=cfg.cache_max_bytes,
        ),
    ):
        if plan is not None:
            plans.append(plan)

    if cfg.downloads_max_age_days or cfg.downloads_max_bytes:
        plan = _plan(
            "telechargements",
            settings.paths.download_dir,
            [settings.paths.download_dir],
            max_age_days=cfg.downloads_max_age_days,
            max_bytes=cfg.downloads_max_bytes,
        )
        if plan is not None:
            plans.append(plan)

    profile = settings.paths.browser_profile_dir
    cache_roots = [profile / name for name in BROWSER_CACHE_DIRS]
    plan = _plan(
        "profil navigateur (cache seulement)",
        profile,
        cache_roots,
        max_age_days=cfg.browser_cache_max_age_days,
        max_bytes=cfg.browser_cache_max_bytes,
    )
    if plan is not None:
        plans.append(plan)
    return plans


def apply_cleanup(plans: list[CleanupPlan]) -> list[dict[str, Any]]:
    """Supprime les fichiers des plans ; un verrou Windows n'arrête pas le reste."""
    results: list[dict[str, Any]] = []
    for plan in plans:
        deleted = 0
        freed = 0
        failed = 0
        for path in plan.files:
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                failed += 1
                continue
            deleted += 1
            freed += size
        results.append(
            {
                "label": plan.label,
                "deleted": deleted,
                "bytes": freed,
                "failed": failed,
            }
        )
    return results


def _mio(size: int) -> str:
    return f"{size / 1_048_576:.1f} Mio"


def format_plans(plans: list[CleanupPlan]) -> str:
    """Aperçu lisible : une ligne par dossier, avec ses règles."""
    if not plans:
        return "Rien à purger dans les dossiers de travail."
    lines: list[str] = []
    for plan in plans:
        rules = []
        if plan.max_age_days:
            rules.append(f"{plan.max_age_days} j")
        if plan.max_bytes:
            rules.append(f"{_mio(plan.max_bytes)} max")
        lines.append(
            f"{plan.label} ({', '.join(rules) or 'aucune règle'}) : "
            f"{plan.total_files} fichier(s), {_mio(plan.total_bytes)} — "
            f"{len(plan.files)} à supprimer ({_mio(plan.bytes)})"
        )
        lines.append(f"  {plan.directory}")
    return "\n".join(lines)
