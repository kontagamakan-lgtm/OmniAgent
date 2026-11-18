# -*- mode: python ; coding: utf-8 -*-
"""Recette PyInstaller d'OmniAgent Windows (paquet complet).

    python -m PyInstaller --noconfirm --clean OmniAgent.spec

Produit `dist/OmniAgent/OmniAgent.exe` (mode dossier : démarrage plus rapide et
mise à jour du contenu plus simple qu'un exécutable unique).

Toute la recette — collecte des données, imports cachés, EXE, COLLECT — vit
dans `pyinstaller_recipe.py`, partagée avec `OmniAgent-lite.spec`. Ce fichier ne
décrit que ce qui distingue le paquet complet : aucune exclusion de modules
Python (NumPy et Pillow restent) et un dossier de sortie « OmniAgent ».

Comme les deux variantes allégées, ce paquet **n'embarque pas** le Node.js du
pilote Playwright (`playwright/driver/node.exe`, ~95 Mio) : les outils
`browser_*` pilotent Chromium avec le Node.js 18+ du système, désigné par
`PLAYWRIGHT_NODEJS_PATH` quand le `node` du PATH ne convient pas. Le navigateur
reste celui du cache Playwright, ou Edge/Chrome de la machine à défaut.
"""

import sys
from pathlib import Path

ROOT = Path(SPECPATH)  # noqa: F821 - fourni par PyInstaller
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pyinstaller_recipe import build_package  # noqa: E402

build_package(  # noqa: F821
    ROOT,
    namespace=globals(),
    collect_name="OmniAgent",
    drop_node=True,
)
