"""Tests du moteur OCR : binaire, moteur en processus, diagnostic honnête."""

from __future__ import annotations

from pathlib import Path

from support import make_settings, tmpdir

from tools.ocr import Ocr, tessdata_dir


def _render(path: Path, text: str) -> None:
    """Image lisible : police assez grande pour que Tesseract la reconnaisse."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (900, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 40), text, fill="black", font=ImageFont.load_default(size=64))
    image.save(path)


def test_inprocess_backend_reads_a_rendered_image() -> None:
    """`tesserocr` reconnaît du texte sans aucun `tesseract.exe` installé."""
    with tmpdir("ocr") as tmp:
        settings = make_settings(tmp)
        available, detail = Ocr.probe_inprocess()
        assert available is True, detail
        assert tessdata_dir().is_dir(), tessdata_dir()

        image = tmp / "facture.png"
        _render(image, "ATLAS BUDGET 42")

        result = Ocr(settings).image_to_text(str(image), max_chars=1000)

        assert result["backend"] == "tesserocr", result
        assert result["chars"] > 0, result
        text = result["text"].upper()
        assert "ATLAS" in text and "42" in text, result["text"]
        assert result["language"] in {"fra", "eng", "fra+eng"}, result


def test_probe_keeps_both_causes_when_no_backend_works(monkeypatch) -> None:
    """Aucun moteur : la raison nomme le binaire *et* le backend embarqué."""
    monkeypatch.setattr(
        Ocr, "probe_cli", staticmethod(lambda executable=None: (False, "binaire absent : test"))
    )
    monkeypatch.setattr(
        Ocr, "probe_inprocess", staticmethod(lambda: (False, "moteur embarqué absent : test"))
    )

    available, reason = Ocr.probe("C:/absent/tesseract.exe")

    assert available is False, reason
    assert "binaire absent : test" in reason, reason
    assert "moteur embarqué absent : test" in reason, reason


def test_cli_wins_when_both_backends_are_available(monkeypatch) -> None:
    """Un binaire installé reste prioritaire : il peut avoir plus de langues."""
    monkeypatch.setattr(
        Ocr, "probe_cli", staticmethod(lambda executable=None: (True, "Tesseract 9.9 (binaire)"))
    )
    with tmpdir("ocr") as tmp:
        status = Ocr(make_settings(tmp)).status()

        assert status["backend"] == "tesseract-cli", status
        assert status["available"] is True, status
        assert "9.9" in status["detail"], status


def test_unknown_language_falls_back_to_a_bundled_one() -> None:
    """Demander une langue absente des données ne casse pas la reconnaissance."""
    with tmpdir("ocr") as tmp:
        settings = make_settings(tmp)
        image = tmp / "facture.png"
        _render(image, "ATLAS BUDGET 42")

        result = Ocr(settings).image_to_text(str(image), lang="xyz")

        assert result["backend"] == "tesserocr", result
        assert result["language"] in {"fra", "eng"}, result
        assert "ATLAS" in result["text"].upper(), result["text"]
