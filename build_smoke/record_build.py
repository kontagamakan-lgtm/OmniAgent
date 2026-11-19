"""Empreintes et versions de build des exécutables produits.

Les paquets compilés étaient jetés sans trace vérifiable : impossible de savoir
quel binaire un rapport d'erreur concernait, ni de le comparer à un autre build.
Ce script écrit deux fichiers dans `dist/`, publiés avec les paquets :

* `SHA256SUMS.txt` — une ligne par `OmniAgent.exe`, au format `sha256sum` ;
* `BUILD-INFO.txt` — Python, PyInstaller, comment et date de construction.

Aucun accès réseau, aucun secret : il ne lit que `dist/`.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

__all__ = ["main", "sha256"]


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    """Empreinte d'un fichier, lue par blocs (les exe pèsent plusieurs Mio)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "absent"


def _commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "inconnu"
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "inconnu"


def main() -> int:
    root = Path("dist")
    exes = sorted(root.glob("*/OmniAgent.exe"))
    if not exes:
        print("aucun exécutable sous dist/ : rien à empreindre", file=sys.stderr)
        return 1

    rows = [f"{sha256(exe)}  {exe.as_posix()}" for exe in exes]
    (root / "SHA256SUMS.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")

    info = [
        f"construit_le={datetime.now(tz=timezone.utc).isoformat()}",
        f"python={platform.python_version()}",
        f"pyinstaller={_package_version('pyinstaller')}",
        f"ruff={_package_version('ruff')}",
        f"pytest={_package_version('pytest')}",
        f"plateforme={platform.platform()}",
        f"commit={_commit()}",
    ]
    (root / "BUILD-INFO.txt").write_text("\n".join(info) + "\n", encoding="utf-8")

    for row in rows:
        print(row)
    print(f"{len(rows)} exécutable(s) empreint(s), versions écrites dans dist/BUILD-INFO.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
