"""OCR local, sans service distant ni installation système obligatoire.

Deux moteurs, essayés dans cet ordre :

1. `tesseract.exe` via `pytesseract` (binaire installé, ou `files.tesseract_cmd`) ;
2. `tesserocr`, qui embarque Tesseract et Leptonica dans le paquet : aucune
   installation système, les données de langue (`assets/tessdata`) sont livrées
   avec l'application.

Le module s'importe sans les dépendances : l'outil `ocr_image` renvoie un échec
explicite (`ocr_unavailable`) plutôt que de planter, et `ocr_status` nomme le
moteur retenu ou les deux causes d'échec.
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

#: Installeur Windows officiel : builds UB Mannheim cités par le projet
#: Tesseract. C'est le lien que `--check` affiche quand le binaire manque.
TESSERACT_DOWNLOAD_URL = "https://github.com/UB-Mannheim/tesseract/wiki"
#: Emplacement de l'installateur standard (choix par défaut de l'installeur).
TESSERACT_DEFAULT_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def tessdata_dir() -> Path:
    """Données de langue embarquées (`assets/tessdata`), source comme paquet.

    Dans un exe PyInstaller, `__file__` vaut `<_internal>/tools/ocr.pyc` : le
    dossier calculé est donc `<_internal>/assets/tessdata`, où la recette
    dépose les fichiers `.traineddata`.
    """
    return Path(__file__).resolve().parent.parent / "assets" / "tessdata"


class Ocr:
    """Reconnaissance de texte locale, chargée à la demande."""

    def __init__(self, settings: Settings, manager: FileManager | None = None) -> None:
        self.settings = settings
        self.files = manager

    # -- disponibilité ------------------------------------------------------

    @staticmethod
    def probe_cli(executable: str | None = None) -> tuple[bool, str]:
        """(disponible, raison) pour le binaire `tesseract.exe` (pytesseract).

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
        return True, f"Tesseract {version} (binaire)"

    @staticmethod
    def probe_inprocess() -> tuple[bool, str]:
        """(disponible, raison) pour `tesserocr`, qui ne dépend d'aucun binaire.

        Le module et les DLL viennent du paquet ; il ne reste à vérifier que les
        données de langue, faute desquelles la reconnaissance échouerait.
        """
        try:
            import tesserocr
        except ImportError as exc:
            return False, f"backend en processus absent du paquet (tesserocr) : {exc}"
        data = tessdata_dir()
        if not data.is_dir() or not any(data.glob("*.traineddata")):
            return False, f"données de langue absentes : {data} (attendu eng/fra.traineddata)"
        try:
            version = str(tesserocr.tesseract_version()).splitlines()[0].strip()
            languages = list(tesserocr.get_languages(str(data))[1])
        except Exception as exc:  # noqa: BLE001 - DLL manquante, données illisibles…
            return False, f"tesserocr inutilisable : {exc}"
        if not languages:
            return False, f"aucune langue utilisable dans {data}"
        version = version.removeprefix("tesseract ").strip()
        return True, (
            f"Tesseract {version} en processus (tesserocr), langues : {', '.join(languages)}"
        )

    @staticmethod
    def probe(executable: str | None = None) -> tuple[bool, str]:
        """(disponible, raison) sans lever d'exception.

        Le binaire est préféré (il peut avoir plus de langues installées) ; à
        défaut, le moteur en processus prend le relais. Si les deux échouent, la
        raison cite les deux causes : `ocr_status` reste honnête.
        """
        cli_ok, cli_detail = Ocr.probe_cli(executable)
        if cli_ok:
            return True, cli_detail
        process_ok, process_detail = Ocr.probe_inprocess()
        if process_ok:
            return True, process_detail
        return False, f"{cli_detail} ; {process_detail}"

    @staticmethod
    def install_hint(executable: str | None = None, *, config_file: str = "") -> list[str]:
        """Trois indications autosuffisantes quand le binaire manque.

        Le diagnostic ne doit pas laisser l'utilisateur chercher : lien de
        téléchargement officiel, clé de configuration exacte (avec le chemin
        par défaut de l'installeur) et commandes de vérification.
        """
        configured = (executable or "").strip() or TESSERACT_DEFAULT_EXE
        escaped = TESSERACT_DEFAULT_EXE.replace("\\", "\\\\")
        return [
            f"téléchargement : {TESSERACT_DOWNLOAD_URL} "
            "(installeur Windows officiel, builds UB Mannheim)",
            f'clé à renseigner dans {config_file or "config.json"} : '
            f'files.tesseract_cmd = "{escaped}" '
            "(ou variable OMNIAGENT_FILES_TESSERACT_CMD)",
            f'vérification : "{configured}" --version puis python app.py --check',
        ]

    def _executable(self) -> str:
        """Chemin facultatif de `tesseract.exe` (section `files`)."""
        return str(getattr(self.settings.files, "tesseract_cmd", "") or "").strip()

    def _backend(self) -> tuple[str, str]:
        """Moteur retenu et sa description (`tesseract-cli`, `tesserocr`, ou aucun)."""
        cli_ok, cli_detail = self.probe_cli(self._executable())
        if cli_ok:
            return "tesseract-cli", cli_detail
        process_ok, process_detail = self.probe_inprocess()
        if process_ok:
            return "tesserocr", process_detail
        return "", f"{cli_detail} ; {process_detail}"

    def status(self) -> dict[str, Any]:
        backend, reason = self._backend()
        return {
            "available": bool(backend),
            "engine": "tesseract" if backend else "",
            "backend": backend,
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

    def _text_cli(self, target: Path, lang: str) -> str:
        import pytesseract

        options: dict[str, Any] = {"lang": lang} if lang else {}
        return str(pytesseract.image_to_string(str(target), **options) or "")

    def _text_inprocess(self, target: Path, lang: str) -> tuple[str, str]:
        """Texte lu par tesserocr, et langue réellement utilisée.

        `files.ocr_languages` demande par défaut `fra+eng` : seules les langues
        présentes dans `assets/tessdata` sont demandées à Tesseract, sinon il
        échouerait sur une langue manquante.
        """
        import tesserocr
        from PIL import Image

        data = tessdata_dir()
        available = list(tesserocr.get_languages(str(data))[1])
        requested = [
            part.strip()
            for part in str(lang or getattr(self.settings.files, "ocr_languages", "") or "")
            .replace(",", "+")
            .split("+")
            if part.strip()
        ]
        usable = [part for part in requested if part in available] or available[:1]
        if not usable:
            raise ToolError(
                f"aucune langue OCR disponible dans {data}", code="ocr_unavailable"
            )
        chosen = "+".join(usable)
        with Image.open(target) as image, tesserocr.PyTessBaseAPI(
            path=str(data), lang=chosen
        ) as api:
            api.SetImage(image)
            text = str(api.GetUTF8Text() or "")
        return text, chosen

    def image_to_text(
        self, path: str, *, lang: str = "", max_chars: int = 20000
    ) -> dict[str, Any]:
        target = self._resolve(path)
        backend, reason = self._backend()
        if not backend:
            raise ToolError(reason, code="ocr_unavailable")
        try:
            if backend == "tesseract-cli":
                text = self._text_cli(target, lang)
                used_language = lang or "(défaut)"
            else:
                text, used_language = self._text_inprocess(target, lang)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - image illisible, langue inconnue…
            raise ToolError(f"reconnaissance impossible : {exc}", code="ocr_failed") from exc
        cleaned = str(text or "").strip()
        return {
            "path": str(target),
            "backend": backend,
            "language": used_language,
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
            "Extrait le texte d'une image (capture, scan) en local, avec le binaire "
            "Tesseract s'il est installé, sinon avec le moteur embarqué (tesserocr). "
            "Échoue proprement si aucun des deux n'est disponible.",
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
            "Indique si l'OCR local est disponible, le moteur retenu (binaire "
            "`tesseract-cli` ou moteur embarqué `tesserocr`) et sa version.",
            object_schema({}),
            ocr_status,
        ),
    ]
