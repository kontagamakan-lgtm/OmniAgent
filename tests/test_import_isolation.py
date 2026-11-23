"""Chaque fichier de tests doit pouvoir être importé seul.

Un import circulaire reste invisible dans la suite complète : le premier module
importé par un test amorce le paquet, et le cycle ne se referme jamais.
`python tests/test_vault.py` échouait ainsi (`tools.file_manager` →
`core.file_tx` → `core/__init__` → `core.factory` → `tools.browser_agent` →
`tools.file_manager`) alors que la suite passait. Chaque module est donc
importé dans un interpréteur neuf, exactement comme le fait
`python tests/<fichier>.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from support import run  # noqa: I001

TESTS_DIR = Path(__file__).resolve().parent
_SELF = Path(__file__).stem

#: Un module qui se protège par `pytest.importorskip` lève `Skipped` à
#: l'import ; ce n'est pas un cycle, donc ce n'est pas un échec.
_PROBE = """
import pytest
try:
    import {module}
except pytest.skip.Exception:
    pass
"""


def _import_alone(module: str) -> str:
    """Importe un module dans un interpréteur neuf ; retourne l'erreur éventuelle."""
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE.format(module=module)],
        cwd=str(TESTS_DIR),
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "PYTHONPATH": str(TESTS_DIR.parent)},
    )
    if completed.returncode == 0:
        return ""
    return f"{module}.py :\n{completed.stderr.strip()[-1200:]}"


def test_every_test_module_imports_on_its_own() -> None:
    modules = sorted(
        path.stem for path in TESTS_DIR.glob("test_*.py") if path.stem != _SELF
    )
    assert modules, "aucun module de tests trouvé"

    with ThreadPoolExecutor(max_workers=8) as pool:
        failures = [error for error in pool.map(_import_alone, modules) if error]

    assert failures == [], "\n\n".join(failures)


if __name__ == "__main__":
    sys.exit(run(globals()))
