"""OCR local via pytesseract, quand Tesseract est installé.

Le module s'importe sans la dépendance : `available()` répond, et l'outil
`ocr_image` renvoie un échec explicite (`ocr_unavailable`) au lieu de planter.
La reconnaissance se fait sur le poste — l'image n'est jamais envoyée à un
service distant.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from config import APP_ID, Settings
from tools.base import FunctionTool, Tool, ToolError, object_schema, param
from tools.file_manager import FileManager

__all__ = ["Ocr", "build_ocr_tools"]

logger = logging.getLogger(f"{APP_ID}.ocr")

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif"}


class Ocr:
    """Reconnaissance de texte locale, chargée à la demande."""

    def __init__(self, settings: Settings, manager: FileManager | None = None) -> None:
        self.settings = settings
        self.files = manager

    # -- disponibilité ------------------------------------------------------

    @staticmethod
    def probe(executable: str | None = None) -> tuple[bool, str]:
        """(disponible, raison) sans lever d'exception.

        `executable` vient de `files.tesseract_cmd` : sur une machine où
        Tesseract n'est pas dans le PATH, c'est le seul moyen de le trouver.
        Les trois causes possibles (module, Pillow, binaire) sont distinguées
        pour que `ocr_status` dise quoi installer.
        """
        try:
            import pytesseract
        except ImportError as exc:
            missing = str(getattr(exc, "name", "") or exc)
            if "PIL" in missing:
                return False, "Pillow (PIL) absent du paquet, requis par pytesseract"
            return False, "pytesseract absent du paquet (pip install pytesseract)"
        try:
            from PIL import Image  # noqa: F401 - requis par pytesseract
        except ImportError:
            return False, "Pillow (PIL) absent du paquet, requis par pytesseract"
        configured = (executable or "").strip()
        if configured:
            pytesseract.pytesseract.tesseract_cmd = configured
        try:
            version = pytesseract.get_tesseract_version()
        except Exception as exc:  # noqa: BLE001 - binaire absent ou illisible
            where = f" (chemin configuré : {configured})" if configured else ""
            return False, f"binaire Tesseract introuvable ou en échec{where} : {exc}"
        return True, str(version)

    def _executable(self) -> str:
        """Chemin facultatif de `tesseract.exe` (section `files`)."""
        return str(getattr(self.settings.files, "tesseract_cmd", "") or "").strip()

    def status(self) -> dict[str, Any]:
        available, reason = self.probe(self._executable())
        return {
            "available": available,
            "engine": "tesseract" if available else "",
            "detail": reason,
            "local": True,
        }

    # -- reconnaissance -----------------------------------------------------

    def _resolve(self, path: str) -> Path:
        if self.files is not None:
            target = self.files.resolve(path, must_exist=True)
        else:
            target = Path(path).expanduser()
            if not target.is_file():
                raise ToolError(f"image introuvable : {target}", code="file_not_found")
            target = target.resolve()
        if target.suffix.lower() not in IMAGE_SUFFIXES:
            raise ToolError(
                f"format non pris en charge : {target.suffix or '(aucun)'} ; "
                f"attendu {', '.join(sorted(IMAGE_SUFFIXES))}",
                code="unsupported_format",
            )
        return target

    def image_to_text(
        self, path: str, *, lang: str = "", max_chars: int = 20000
    ) -> dict[str, Any]:
        target = self._resolve(path)
        available, reason = self.probe(self._executable())
        if not available:
            raise ToolError(reason, code="ocr_unavailable")
        import pytesseract

        options: dict[str, Any] = {"lang": lang} if lang else {}
        try:
            text = pytesseract.image_to_string(str(target), **options)
        except Exception as exc:  # noqa: BLE001 - image illisible, langue inconnue…
            raise ToolError(f"reconnaissance impossible : {exc}", code="ocr_failed") from exc
        cleaned = str(text or "").strip()
        return {
            "path": str(target),
            "language": lang or "(défaut)",
            "chars": len(cleaned),
            "truncated": len(cleaned) > max_chars,
            "text": cleaned[: max(1, int(max_chars))],
        }


def build_ocr_tools(ocr: Ocr) -> list[Tool]:
    """Outils `ocr_*` liés à ce moteur local."""

    def ocr_image(path: str, lang: str = "", max_chars: int = 20000) -> dict[str, Any]:
        return ocr.image_to_text(path, lang=lang, max_chars=max_chars)

    def ocr_status() -> dict[str, Any]:
        return ocr.status()

    return [
        FunctionTool(
            "ocr_image",
            "Extrait le texte d'une image (capture, scan) avec Tesseract, en local. "
            "Échoue proprement si Tesseract n'est pas installé.",
            object_schema(
                {
                    "path": param("string", "Chemin de l'image"),
                    "lang": param(
                        "string",
                        "Langue Tesseract (ex. fra, eng) ; vide = langue par défaut",
                        default="",
                        pattern="^[A-Za-z0-9+_]{0,32}$",
                    ),
                    "max_chars": param(
                        "integer",
                        "Longueur maximale du texte renvoyé",
                        default=20000,
                        minimum=100,
                        maximum=200000,
                    ),
                },
                required=["path"],
            ),
            ocr_image,
        ),
        FunctionTool(
            "ocr_status",
            "Indique si l'OCR local est disponible (pytesseract + Tesseract) et sa version.",
            object_schema({}),
            ocr_status,
        ),
    ]
