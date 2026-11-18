# -*- mode: python ; coding: utf-8 -*-
"""Recette PyInstaller allégée d'OmniAgent Windows.

    python -m PyInstaller --noconfirm --clean OmniAgent-lite.spec

Même paquet que `OmniAgent.spec`, moins NumPy (~42 Mio avec Pillow). NumPy
n'est requis par aucun outil enregistré par `core.factory.build_agent` : il
n'entre que par les hooks de Pillow (`PIL._typing` l'importe sous
`if TYPE_CHECKING:`, jamais à l'exécution).

**Pillow reste inclus** : `pytesseract` l'importe au chargement pour ouvrir les
images, donc l'exclure rendrait la famille `ocr_*` définitivement indisponible
dans cette variante.

Comme le paquet complet, cette variante n'embarque **pas** le Node.js du pilote
Playwright (~95 Mio) : les outils `browser_*` utilisent le Node.js 18+ du
système (`PLAYWRIGHT_NODEJS_PATH` sinon le `node` du PATH).

La recette commune vit dans `pyinstaller_recipe.py` ; ce fichier ne décrit que
les exclusions et le nom du dossier produit.
"""

import sys
from pathlib import Path

ROOT = Path(SPECPATH)  # noqa: F821 - fourni par PyInstaller
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pyinstaller_recipe import build_package  # noqa: E402

#: Exclusions propres à la variante allégée, en plus de celles de la recette.
#: Pillow n'est pas exclu : pytesseract en dépend pour lire les images.
LIGHT_EXCLUDES = ["numpy"]

build_package(  # noqa: F821
    ROOT,
    namespace=globals(),
    excludes=LIGHT_EXCLUDES,
    collect_name="OmniAgent-lite",
    drop_node=True,
)
