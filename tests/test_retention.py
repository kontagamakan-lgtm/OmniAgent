"""Rétention des rapports, cache, téléchargements et cache du profil navigateur."""

from __future__ import annotations

import os
import time
from pathlib import Path

from support import make_settings, tmpdir  # noqa: I001

from core.retention import BROWSER_CACHE_DIRS, apply_cleanup, format_plans, plan_cleanup


def _write(path: Path, size: int, *, age_days: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if age_days:
        stamp = time.time() - age_days * 86_400
        os.utime(path, (stamp, stamp))
    return path


def _plan_for(settings, label: str):
    return next(plan for plan in plan_cleanup(settings) if plan.label == label)


def test_old_reports_are_selected_and_recent_ones_kept() -> None:
    with tmpdir("retention-age") as tmp:
        settings = make_settings(tmp)
        settings.storage.reports_max_bytes = 0
        old = _write(settings.paths.report_dir / "vieux.md", 10, age_days=45)
        recent = _write(settings.paths.report_dir / "recent.md", 10, age_days=1)

        plan = _plan_for(settings, "rapports")

        assert plan.files == [old]
        assert recent not in plan.files
        assert plan.total_files == 2
        assert "rapports" in format_plans([plan])


def test_the_size_cap_removes_the_oldest_files_first() -> None:
    with tmpdir("retention-taille") as tmp:
        settings = make_settings(tmp)
        settings.storage.cache_max_age_days = 0
        settings.storage.cache_max_bytes = 1_500
        oldest = _write(settings.paths.cache_dir / "a.bin", 1_000, age_days=10)
        middle = _write(settings.paths.cache_dir / "b.bin", 1_000, age_days=5)
        newest = _write(settings.paths.cache_dir / "c.bin", 1_000, age_days=1)

        plan = _plan_for(settings, "cache")

        assert plan.files == [oldest, middle]
        assert newest not in plan.files
        assert plan.bytes == 2_000


def test_only_the_browser_cache_is_touched_never_the_profile_secrets() -> None:
    with tmpdir("retention-profil") as tmp:
        settings = make_settings(tmp)
        settings.storage.browser_cache_max_age_days = 1
        settings.storage.browser_cache_max_bytes = 0
        profile = settings.paths.browser_profile_dir
        cookies = _write(profile / "Default" / "Cookies", 10, age_days=30)
        login = _write(profile / "Default" / "Login Data", 10, age_days=30)
        cached = _write(
            profile / BROWSER_CACHE_DIRS[0] / "data_0", 10, age_days=30
        )

        plan = _plan_for(settings, "profil navigateur (cache seulement)")

        assert plan.files == [cached]
        assert cookies not in plan.files and login not in plan.files


def test_downloads_stay_disabled_by_default_then_follow_the_rule() -> None:
    with tmpdir("retention-telechargements") as tmp:
        settings = make_settings(tmp)
        old = _write(settings.paths.download_dir / "vieux.zip", 10, age_days=60)

        assert "telechargements" not in {plan.label for plan in plan_cleanup(settings)}

        settings.storage.downloads_max_age_days = 30
        plan = _plan_for(settings, "telechargements")
        assert plan.files == [old]


def test_planning_changes_nothing_and_applying_deletes_only_the_plan() -> None:
    with tmpdir("retention-application") as tmp:
        settings = make_settings(tmp)
        settings.storage.reports_max_age_days = 30
        settings.storage.reports_max_bytes = 0
        old = _write(settings.paths.report_dir / "vieux.md", 12, age_days=60)
        recent = _write(settings.paths.report_dir / "recent.md", 12, age_days=2)

        plans = plan_cleanup(settings)
        assert old.exists(), "un plan n'est qu'un aperçu"
        results = apply_cleanup(plans)

        assert not old.exists()
        assert recent.exists()
        assert sum(item["deleted"] for item in results) == 1
        assert sum(item["bytes"] for item in results) == 12
        assert sum(item["failed"] for item in results) == 0


def test_empty_and_missing_directories_produce_no_plan() -> None:
    with tmpdir("retention-vide") as tmp:
        settings = make_settings(tmp)

        assert plan_cleanup(settings) == []
        assert format_plans([]) == "Rien à purger dans les dossiers de travail."
