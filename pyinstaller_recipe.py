"""Recette PyInstaller commune aux paquets OmniAgent.

Les fichiers `.spec` ne portent plus que leurs différences (exclusions, nom du
dossier produit) ; le reste — collecte des données, imports cachés, EXE et
COLLECT — vit ici, pour qu'une évolution ne soit à faire qu'une fois.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

__all__ = ["build_package"]

#: Paquets dont les données accompagnent l'exécutable.
_DATA_PACKAGES = ("playwright", "certifi", "tiktoken")

#: Modules importés dynamiquement, invisibles pour l'analyse statique.
#: `tools.file_manager._require` passe par `__import__` : PyInstaller ne voit
#: ni pypdf (PDF), ni docx (DOCX), ni openpyxl (XLSX) sans cette déclaration —
#: l'indexation et la conversion de ces formats échouaient dans le paquet.
_EXTRA_HIDDENIMPORTS = ("darkdetect", "pytesseract", "tesserocr", "pypdf", "docx", "openpyxl")

#: DLL du moteur OCR en processus. Les roues `tesserocr` sont patchées par
#: delvewheel : le module charge ses dépendances depuis `tesserocr.libs/`, un
#: dossier que l'analyse PyInstaller ne voit pas (noms hachés, import dynamique).
#: On copie la fermeture utile des imports PE, sans ICU de libcurl ni libcrypto.
_TESSEROCR_LIB_PREFIXES = (
    "tesseract55-",
    "leptonica-",
    "archive-",
    "libcurl-",
    "gif-",
    "jpeg8-",
    "libpng16-",
    "libwebp",
    "openjp2-",
    "tiff-",
    "zlib-",
    "charset-",
    "iconv-",
    "libbz2-",
    "liblzma-",
    "libxml2-",
    "lz4-",
    "zstd-",
    "libssh2-",
    "psl-",
    "icuuc78-",
    "icudt78-",
    "msvcp140-",
    "vcruntime140",
)


def _tesserocr_binaries() -> list[tuple[str, str]]:
    """DLL de `tesserocr.libs` à livrer avec le paquet (vide si absent)."""
    try:
        import tesserocr
    except Exception:  # paquet non installé : la compilation reste possible
        return []
    libs = Path(tesserocr.__file__).resolve().parent.parent / "tesserocr.libs"
    if not libs.is_dir():
        return []
    return [
        (str(path), "tesserocr.libs")
        for path in sorted(libs.iterdir())
        if path.name.lower().startswith(_TESSEROCR_LIB_PREFIXES)
    ]


def _ocr_assets(root: Path) -> list[tuple[str, str]]:
    """Données de langue livrées avec l'application (`assets/tessdata`)."""
    data = root / "assets" / "tessdata"
    if not data.is_dir():
        return []
    return [(str(path), "assets/tessdata") for path in sorted(data.glob("*.traineddata"))]


def build_package(
    root: Path,
    *,
    namespace: dict[str, Any],
    excludes: Iterable[str] = (),
    collect_name: str = "OmniAgent",
    drop_node: bool = False,
) -> tuple[Any, Any, Any, Any]:
    """Construit Analysis, PYZ, EXE et COLLECT, puis les retourne.

    `namespace` est le dictionnaire de globals du fichier `.spec` : PyInstaller
    y injecte `Analysis`, `PYZ`, `EXE` et `COLLECT`. Ces classes travaillent
    dans leur constructeur, il suffit donc de les instancier.

    `drop_node` retire le pilote Node.js embarqué par Playwright (~96 Mio) :
    l'application utilise alors le Node.js du système, voir
    `tools.browser_agent.ensure_system_node`.
    """
    analysis_cls = namespace["Analysis"]
    pyz_cls = namespace["PYZ"]
    exe_cls = namespace["EXE"]
    collect_cls = namespace["COLLECT"]
    excluded = tuple(str(item) for item in excludes)

    datas = collect_data_files("customtkinter")
    for package in _DATA_PACKAGES:
        try:
            datas += collect_data_files(package)
        except Exception:  # paquet absent : la compilation reste possible
            pass

    # Les ressources du projet (config d'exemple, certificats éventuels) sont
    # copiées telles quelles ; les données utilisateur (coffre, index, journaux)
    # ne doivent jamais entrer dans le paquet.
    for name in ("config.example.json", "README.md"):
        candidate = root / "data" / name
        if candidate.is_file():
            datas.append((str(candidate), "data"))

    # Ressources de l'interface et schéma SQLite, chargés par chemin relatif.
    for asset in ("icon.ico", "theme.json"):
        candidate = root / "assets" / asset
        if candidate.is_file():
            datas.append((str(candidate), "assets"))
    schema = root / "storage" / "schema.sql"
    if schema.is_file():
        datas.append((str(schema), "storage"))

    # OCR sans installation système : DLL du moteur et données de langue.
    ocr_binaries = _tesserocr_binaries()
    datas += _ocr_assets(root)

    hiddenimports = collect_submodules("customtkinter") + list(_EXTRA_HIDDENIMPORTS)
    if "PIL" not in excluded:
        # Uniquement utile quand Pillow est présent : il ferait sinon rentrer
        # le paquet dans la variante qui l'exclut.
        hiddenimports.append("PIL._tkinter_finder")
    hiddenimports += [
        "core",
        "storage",
        "storage.db",
        "ui.app_gui",
        "tools.browser_agent",
        "tools.web_search",
        "tools.vault",
        # `pytesseract` est importé à l'intérieur des méthodes d'`Ocr` :
        # l'analyse statique ne le voit pas, il faut le déclarer.
        "tools.ocr",
    ]

    analysis = analysis_cls(
        [str(root / "app.py")],
        pathex=[str(root)],
        binaries=ocr_binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=[
            "pytest",
            "pytest_asyncio",
            "_pytest",
            "tests",
            "PyInstaller",
            "tkinter.test",
            *excluded,
        ],
        noarchive=False,
    )
    binaries = analysis.binaries
    if drop_node:
        binaries = [entry for entry in binaries if not _is_driver_node(entry[0])]

    pyz = pyz_cls(analysis.pure)

    exe = exe_cls(
        pyz,
        analysis.scripts,
        [],
        exclude_binaries=True,
        name="OmniAgent",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(root / "assets" / "icon.ico")
        if (root / "assets" / "icon.ico").is_file()
        else None,
    )

    collect = collect_cls(
        exe,
        binaries,
        analysis.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=collect_name,
    )
    return analysis, pyz, exe, collect


def _is_driver_node(name: str) -> bool:
    """Vrai pour le binaire `node.exe` du pilote Playwright (pas les DLL)."""
    parts = str(name).replace("\\", "/").split("/")
    return parts[-1] in {"node.exe", "node"} and "playwright" in parts and "driver" in parts
