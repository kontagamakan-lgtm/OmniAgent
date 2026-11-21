"""Les assets publiés sont exactement les paquets que le workflow construit.

`OmniAgent-slim.zip` a été publié pendant plusieurs versions alors qu'il était le
paquet de `OmniAgent-lite.zip` sous un autre nom : environ 80 Mio d'assets en
double par release, pour un dossier que personne ne téléchargeait. Le doublon
n'était visible nulle part — la liste `$assets` du workflow, les appels de
compilation et les `Compress-Archive` vivaient côte à côte sans être confrontés.

Ces tests lisent `release.yml` et `build.bat` **sans les exécuter** : la liste
d'assets doit égaler ce que le workflow produit, et les zips publiés doivent
être ceux que `build.bat --all` annonce.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
BUILD = ROOT / "build.bat"
RECORD = ROOT / "build_smoke" / "record_build.py"


def _release() -> str:
    return RELEASE.read_text(encoding="utf-8", errors="replace")


def _assets() -> list[str]:
    """Entrées du bloc `$assets = @( ... )`, dans l'ordre publié."""
    match = re.search(r"\$assets\s*=\s*@\((.*?)\n\s*\)", _release(), re.S)
    assert match is not None, "bloc `$assets = @(` introuvable dans release.yml"
    return re.findall(r'"([^"]+)"', match.group(1))


def _compressed() -> set[str]:
    """Zips produits par `Compress-Archive`, sous la forme `dist/<nom>.zip`."""
    pairs = re.findall(
        r"Compress-Archive\s+-Path\s+dist/([\w.-]+)/\*\s+-DestinationPath\s+dist/([\w.-]+)\.zip",
        _release(),
    )
    assert pairs, "aucun `Compress-Archive` reconnu dans release.yml"
    for source, target in pairs:
        assert source == target, (
            f"dist/{target}.zip ne contient pas dist/{source} : le zip publié "
            "ne serait pas le paquet construit"
        )
    return {f"dist/{target}.zip" for _, target in pairs}


def _installer() -> str:
    """Chemin de l'installateur, tel que le workflow l'exige après compilation."""
    match = re.search(r'\$setup\s*=\s*"([^"]+)"', _release())
    assert match is not None, "`$setup` introuvable dans release.yml"
    return match.group(1).replace("\\", "/")


def _recorded() -> set[str]:
    """Fichiers que `record_build.py` écrit dans `dist/`."""
    written = set(
        re.findall(
            r'\(\s*root\s*/\s*"([^"]+)"\s*\)\s*\.write_text',
            RECORD.read_text(encoding="utf-8"),
        )
    )
    assert written, "aucun fichier écrit par record_build.py reconnu"
    return {f"dist/{name}" for name in written}


def test_asset_list_matches_exactly_what_the_workflow_builds() -> None:
    assets = _assets()
    built = _compressed() | {_installer()} | _recorded()

    published = set(assets)
    assert published == built, (
        "assets publiés sans être construits : "
        + ", ".join(sorted(published - built))
        + " ; construits sans être publiés : "
        + ", ".join(sorted(built - published))
    )


def test_no_asset_is_published_twice() -> None:
    assets = _assets()
    duplicated = sorted({name for name in assets if assets.count(name) > 1})

    assert not duplicated, "asset listé plusieurs fois : " + ", ".join(duplicated)


def test_published_zips_are_the_variants_build_all_announces() -> None:
    """`build.bat --all` dit ce qu'il produit ; les zips publiés doivent le suivre."""
    text = BUILD.read_text(encoding="utf-8", errors="replace")
    announced = set(re.findall(r"echo\s+dist\\([\w.-]+)\\OmniAgent\.exe", text))
    assert announced == {"OmniAgent", "OmniAgent-lite"}, (
        "la branche --all de build.bat doit annoncer deux paquets, pas "
        + str(sorted(announced))
    )

    zipped = {Path(asset).stem for asset in _compressed()}
    assert zipped == announced, (
        "zips publiés qui ne sont pas construits par --all : "
        + ", ".join(sorted(zipped - announced))
        + " ; construits mais non publiés : "
        + ", ".join(sorted(announced - zipped))
    )


def test_slim_is_not_published() -> None:
    """Régression : l'alias local `--slim` ne doit plus produire d'asset."""
    assets = _assets()

    assert not [name for name in assets if "slim" in name.lower()], (
        "OmniAgent-slim n'est plus publié : c'était le paquet de "
        "OmniAgent-lite.zip sous un autre nom"
    )
