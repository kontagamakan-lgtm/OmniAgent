"""Snapshots visuels opt-in de la fenêtre principale.

Les captures sont locales à une session Windows avec affichage. Pour les
régénérer après une évolution volontaire :

    $env:OMNIAGENT_UI_SNAPSHOTS = "1"
    $env:UPDATE_UI_SNAPSHOTS = "1"
    python -m pytest tests/test_ui_snapshots.py -q

``OMNIAGENT_UI_SNAPSHOT_DIR`` choisit un autre dossier de références : la CI
l'utilise pour ses propres baselines (rendu du runner, DPI et thème différents
de ceux d'un poste de travail) sans jamais écraser les références locales.

Une référence manquante n'est jamais un succès : la capture est enregistrée,
puis le test échoue en demandant de la committer. En cas d'écart, la capture
réelle et l'image de différence sont écrites dans
``OMNIAGENT_UI_SNAPSHOT_ACTUALS`` (défaut ``artifacts/ui/actual``) afin d'être
publiées avec l'artefact ``ui-snapshots-windows``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import tmpdir  # noqa: E402
from ui_harness import make_window, settings_for_ui, settle  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS = Path(
    os.environ.get("OMNIAGENT_UI_SNAPSHOT_DIR", ROOT / "artifacts" / "ui")
)
ACTUALS = Path(
    os.environ.get(
        "OMNIAGENT_UI_SNAPSHOT_ACTUALS", ROOT / "artifacts" / "ui" / "actual"
    )
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIAGENT_UI_SNAPSHOTS") != "1",
    reason="snapshots opt-in : définir OMNIAGENT_UI_SNAPSHOTS=1 avec affichage",
)


def _image_grab(box: tuple[int, int, int, int]):
    from PIL import ImageGrab

    return ImageGrab.grab(bbox=box)


def _assert_real(image) -> None:
    from PIL import ImageStat

    assert image.width >= 700 and image.height >= 500, image.size
    assert ImageStat.Stat(image.convert("L")).stddev[0] > 8.0


def _record_actuals(name: str, actual, *, difference=None) -> Path:
    """Écrit la capture réelle hors des références, pour l'artefact CI."""
    ACTUALS.mkdir(parents=True, exist_ok=True)
    path = ACTUALS / f"{name}.actual.png"
    actual.save(path)
    if difference is not None:
        difference.save(ACTUALS / f"{name}.diff.png")
    return path


def _capture(geometry: str, name: str) -> None:
    with tmpdir(f"ui-snapshot-{name}") as tmp:
        window = make_window(settings_for_ui(tmp))
        try:
            window.geometry(geometry + "+0+0")
            settle(window, rounds=4, delay=0.12)
            x, y = window.winfo_rootx(), window.winfo_rooty()
            w, h = window.winfo_width(), window.winfo_height()
            image = _image_grab((x, y, x + w, y + h))
            _assert_real(image)
            SNAPSHOTS.mkdir(parents=True, exist_ok=True)
            baseline = SNAPSHOTS / f"{name}.png"
            if os.environ.get("UPDATE_UI_SNAPSHOTS") == "1":
                image.save(baseline)
                pytest.skip(f"baseline régénérée volontairement : {baseline}")
            if not baseline.is_file():
                image.save(baseline)
                pytest.fail(
                    f"aucune référence pour {name} : capture enregistrée dans "
                    f"{baseline}. Comparer et committer cette image pour que les "
                    "exécutions suivantes vérifient réellement le rendu "
                    "(l'artefact ui-snapshots-windows la contient en CI)."
                )
            from PIL import Image, ImageChops, ImageStat

            previous = Image.open(baseline).convert("RGB")
            actual = image.convert("RGB")
            if previous.size != actual.size:
                path = _record_actuals(name, actual)
                pytest.fail(
                    f"taille de capture différente sur {name} : référence "
                    f"{previous.size}, réel {actual.size}. Capture réelle : {path}"
                )
            difference = ImageChops.difference(previous, actual)
            mean = ImageStat.Stat(difference).mean
            if max(mean) > 1.0:
                path = _record_actuals(name, actual, difference=difference)
                pytest.fail(
                    f"écart visuel sur {name}: {mean} (tolérance moyenne 1/255). "
                    f"Capture réelle et différence dans {path.parent}."
                )
            assert max(mean) <= 1.0, f"écart visuel sur {name}: {mean}"
        finally:
            window._on_close()


def test_snapshot_compact_900x620() -> None:
    _capture("900x620", "compact_900x620")


def test_snapshot_normal_1280x860() -> None:
    _capture("1280x860", "normal_1280x860")


def test_snapshot_manifest_is_descriptive() -> None:
    if not SNAPSHOTS.is_dir():
        pytest.skip("snapshots non générés")
    manifest = {
        "capture_backend": "PIL.ImageGrab",
        "viewport": ["900x620", "1280x860"],
        "screens": sorted(path.name for path in SNAPSHOTS.glob("*.png")),
        "note": "captures locales, comparées avec une tolérance moyenne de 1/255",
    }
    path = SNAPSHOTS / "manifest.json"
    if os.environ.get("UPDATE_UI_SNAPSHOTS") == "1" or not path.is_file():
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["capture_backend"] == "PIL.ImageGrab"
    assert "compact_900x620.png" in loaded["screens"]
    assert "normal_1280x860.png" in loaded["screens"]
