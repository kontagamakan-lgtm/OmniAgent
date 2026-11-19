"""Paquet de support : de quoi diagnostiquer une machine sans copier ses données.

Le `--export` d'une trace ne couvre qu'un run. Ici on rassemble l'état de
l'installation — versions, configuration effective (sans secret), cohérence de
l'index, rétention, cinq derniers runs — dans une archive ZIP lisible par un
tiers. Aucun contenu de session, aucune trace, aucune clé : uniquement des
faits et des compteurs, repassés par le masquage des secrets.
"""

from __future__ import annotations

import json
import logging
import platform
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import APP_ID, APP_NAME, APP_VERSION, Settings, is_frozen
from core.index_doctor import diagnose, format_issues
from core.retention import format_plans, plan_cleanup
from core.run_index import RunIndex
from security.redaction import redact

__all__ = ["build_support_bundle", "bundle_summary"]

logger = logging.getLogger(f"{APP_ID}.support")

_REDACT_FIELDS = ("api_key", "token", "secret", "password", "passphrase")


def _redact_tree(value: Any) -> Any:
    """Masque les champs sensibles par leur nom, et les valeurs qui y ressemblent."""
    if isinstance(value, dict):
        return {
            str(key): ("***" if any(word in str(key).lower() for word in _REDACT_FIELDS) else _redact_tree(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_tree(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def bundle_summary(settings: Settings) -> dict[str, Any]:
    """Faits de l'installation, sans contenu de session ni secret."""
    runs_dir = settings.paths.log_dir / "runs"
    try:
        entries = RunIndex(runs_dir).entries()
    except Exception:  # noqa: BLE001 - un indice absent n'empêche pas le paquet
        entries = {}
    recent = list(entries.values())[:5]
    return _redact_tree(
        {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "app": {
                "name": APP_NAME,
                "version": APP_VERSION,
                "frozen": is_frozen(),
                "python": platform.python_version(),
                "platform": platform.platform(),
                "executable": sys.executable if not is_frozen() else "",
            },
            "settings": settings.summary(),
            "index": [
                {
                    "run_id": record.run_id,
                    "state": record.state,
                    "model": record.model,
                    "stop_reason": record.stop_reason,
                    "ok": record.ok,
                    "bytes": record.bytes,
                    "updated": record.updated,
                }
                for record in recent
            ],
            "index_files": len(list(runs_dir.glob("*.jsonl*"))) if runs_dir.is_dir() else 0,
        }
    )


def _default_target(settings: Settings) -> Path:
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    return settings.paths.report_dir / f"support-{stamp}.zip"


def build_support_bundle(settings: Settings, target: Path | str | None = None) -> Path:
    """Écrit l'archive et retourne son chemin ; ne lève pas sur un indice illisible."""
    path = Path(target) if target else _default_target(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    runs_dir = settings.paths.log_dir / "runs"
    try:
        doctor = format_issues(diagnose(runs_dir, active_ttl_s=settings.trace.active_ttl_s))
    except Exception as exc:  # noqa: BLE001 - le paquet doit rester produisible
        doctor = f"diagnostic impossible : {type(exc).__name__}: {exc}"
    retention = format_plans(plan_cleanup(settings))
    readme = (
        "Paquet de support OmniAgent\n"
        "===========================\n\n"
        "Contenu : versions, configuration effective sans secret, diagnostic de\n"
        "l'index, plan de rétention et cinq derniers runs. Aucune trace, aucun\n"
        "contenu de session, aucune clé d'API.\n"
    )
    payloads: dict[str, str] = {
        "README.txt": readme,
        "summary.json": json.dumps(bundle_summary(settings), indent=2, ensure_ascii=False),
        "index-doctor.txt": doctor,
        "retention.txt": retention,
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in payloads.items():
            archive.writestr(name, body)
    return path


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m core.support_bundle")
    parser.add_argument("target", nargs="?", type=Path, help="archive de sortie")
    args = parser.parse_args(argv)
    from config import Settings as _Settings

    settings = _Settings.load(apply_env=True, strict=False)
    print(build_support_bundle(settings, args.target))
    return 0


if __name__ == "__main__":  # pragma: no cover - entrée CLI
    raise SystemExit(main())
