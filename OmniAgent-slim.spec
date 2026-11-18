# -*- mode: python ; coding: utf-8 -*-
"""Recette PyInstaller « slim » d'OmniAgent Windows.

    python -m PyInstaller --noconfirm --clean OmniAgent-slim.spec

**Alias de la variante allégée** (`OmniAgent-lite.spec`) : même contenu, même
exclusions, seul le dossier produit change (`dist/OmniAgent-slim`). L'ancienne
différence — l'absence de `playwright/driver/node.exe` — a disparu : plus aucun
paquet n'embarque Node.js, les outils `browser_*` utilisent celui du système.

Ce fichier existe pour les scripts qui appellent `build.bat --slim` ou cette
recette directement ; la recette commune vit dans `pyinstaller_recipe.py`.
"""

import sys
from pathlib import Path

ROOT = Path(SPECPATH)  # noqa: F821 - fourni par PyInstaller
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pyinstaller_recipe import build_package  # noqa: E402

#: Mêmes exclusions que `--lite` ; Pillow reste requis par l'OCR.
SLIM_EXCLUDES = ["numpy"]

build_package(  # noqa: F821
    ROOT,
    namespace=globals(),
    excludes=SLIM_EXCLUDES,
    collect_name="OmniAgent-slim",
    drop_node=True,
)
