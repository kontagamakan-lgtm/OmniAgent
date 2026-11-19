"""Preuves visuelles Windows : images réelles, géométrie, boutons et textes."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageStat

from tools.visual_check import _button_piece, _crop_virtual

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts" / "visual"


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", str(text))
    return "".join(char for char in folded if not unicodedata.combining(char)).lower()


def _image(relative: str) -> Image.Image:
    path = ROOT / relative
    assert path.is_file(), f"artefact visuel absent : {path}"
    return Image.open(path).convert("RGB")


def _assert_real_render(image: Image.Image, minimum: tuple[int, int]) -> None:
    assert image.width >= minimum[0] and image.height >= minimum[1], image.size
    stats = ImageStat.Stat(image.convert("L"))
    colors = image.getcolors(maxcolors=1_000_000)
    assert stats.stddev[0] > 8.0, f"image quasi uniforme : {stats.stddev[0]}"
    assert colors is not None and len(colors) > 100, "trop peu de couleurs"


def test_visual_manifest_and_committed_renders() -> None:
    manifest = json.loads((ARTIFACTS / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["capture_backend"].startswith("PIL.ImageGrab")
    assert manifest["screen"] == [1920, 1080]
    assert manifest["session"] == "Console"

    approval = manifest["approval"]
    window = _image(approval["files"]["crop"])
    _assert_real_render(window, (250, 120))
    assert approval["geometry"] == list(window.size)
    assert approval["window"][2] - approval["window"][0] == window.width
    assert approval["window"][3] - approval["window"][1] == window.height
    labels = {_normalize(button["label"]) for button in approval["buttons"]}
    assert labels == {
        "autoriser une fois",
        "autoriser pour la session",
        "refuser",
    }, approval["buttons"]
    for button in approval["buttons"]:
        left, top, right, bottom = button["rect"]
        assert approval["window"][0] <= left < right <= approval["window"][2]
        assert approval["window"][1] <= top < bottom <= approval["window"][3]
        assert button["width"] >= 40 and button["height"] >= 15
    approval_text = _normalize(approval["ocr"] + "\n" + approval["buttons_ocr"])
    for label in ("autoriser une fois", "autoriser pour la session", "refuser"):
        assert label in approval_text, approval["ocr"]

    route = manifest["notification_path"]
    path_image = _image(route["files"]["crop"])
    _assert_real_render(path_image, (300, 150))
    assert route["shortcut"].lower().endswith("omniagent.lnk")
    assert "notify-action" in route["protocol"]
    assert route["aumid_in_shortcut"] is True
    route_text = _normalize(route["ocr"])
    for word in ("raccourci", "protocole", "omniagent"):
        assert word in route_text, route["ocr"]

    toast = manifest["toast"]
    toast_image = _image(toast["files"]["crop"])
    _assert_real_render(toast_image, (200, 70))
    assert toast["geometry"] == list(toast_image.size)
    assert toast["changed_pixels"] > 5_000
    toast_text = _normalize(toast["ocr"])
    assert "omniagent" in toast_text and "notification" in toast_text, toast["ocr"]


def test_button_crop_uses_the_captured_frame_and_virtual_origin() -> None:
    """Le crop des boutons ne doit jamais relire une capture antérieure."""
    frame = Image.new("RGB", (300, 200), "white")
    draw = ImageDraw.Draw(frame)
    draw.rectangle((20, 30, 120, 70), fill="#c00000")
    draw.rectangle((50, 45, 70, 55), fill="white")
    piece = _button_piece(
        frame,
        (100, 50),
        {"rect": [120, 80, 220, 120]},
    )
    assert piece.size == (96, 36)
    assert ImageStat.Stat(piece).stddev[0] > 1.0

    virtual = Image.new("RGB", (200, 100), "white")
    ImageDraw.Draw(virtual).rectangle((10, 10, 60, 30), fill="#00c000")
    cropped = _crop_virtual(virtual, (-50, 0), (-40, 10, 10, 30))
    assert cropped.size == (50, 20)
    assert cropped.getpixel((5, 5))[1] > 100


def test_operations_documents_visual_paths_audit_anchor_and_isolation_limit() -> None:
    docs = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    for artifact in (
        "artifacts/visual/approval_dialog.png",
        "artifacts/visual/toast.png",
        "artifacts/visual/notification_path.png",
    ):
        assert artifact in docs
    assert "--audit-anchor" in docs
    assert "--verify-audit --audit-anchor" in docs
    assert "hors de portée de l'attaquant" in docs
    assert "system_sandbox" not in docs  # le statut est dans l'outil, pas la doc
    assert "bac à sable système" in docs
    assert "AppContainer" in docs and "machine virtuelle" in docs
