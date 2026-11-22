"""Garde-fous sur `build.bat` : options documentées et recettes `.spec`.

Des scripts extérieurs (CI, raccourcis, alias `--slim` historique) appellent ces
options : en supprimer une, ou déplacer une variante vers un `.spec` disparu,
casserait ces appels sans qu'aucun autre test ne le voie. Ces contrôles lisent
le script réel — sans l'exécuter — et échouent dès qu'une option documentée
n'est plus acceptée ou qu'une recette manque.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build.bat"
README = ROOT / "README.md"

#: Options que des scripts extérieurs peuvent appeler (CI incluse).
EXPECTED_OPTIONS = {"--check", "--lite", "--slim", "--all", "--no-smoke"}


def _text() -> str:
    return BUILD.read_text(encoding="utf-8", errors="replace")


def _accepted(text: str) -> set[str]:
    """Options réellement lues par la boucle de parsing."""
    return set(re.findall(r'if /i "%~1"=="(--[a-z-]+)"', text))


def _documented(text: str) -> set[str]:
    """Options citées par les lignes d'aide `rem ... build.bat --xxx`."""
    documented: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("rem"):
            continue
        documented.update(re.findall(r"build\.bat\s+(--[a-z-]+)", stripped))
    return documented


def _variant_block(text: str, variant: str) -> str:
    """Corps du bloc `if /i "%VARIANT%"=="<variante>" ( ... )`."""
    match = re.search(
        rf'if /i "%VARIANT%"=="{re.escape(variant)}" \((.*?)\n\) else', text, re.S
    )
    assert match is not None, f"bloc de variante introuvable : {variant}"
    return match.group(1)


def test_documented_options_are_accepted() -> None:
    text = _text()
    documented = _documented(text)
    accepted = _accepted(text)

    assert documented, "aucune option documentée trouvée dans l'en-tête de build.bat"
    assert documented <= accepted, (
        "option documentée mais non acceptée : " + ", ".join(sorted(documented - accepted))
    )
    assert EXPECTED_OPTIONS <= documented, (
        "option attendue absente de la documentation : "
        + ", ".join(sorted(EXPECTED_OPTIONS - documented))
    )
    assert EXPECTED_OPTIONS <= accepted, (
        "option attendue non acceptée par le script : "
        + ", ".join(sorted(EXPECTED_OPTIONS - accepted))
    )


def test_readme_options_are_accepted_by_the_script() -> None:
    """Le README ne doit pas promettre une option que le script ignore."""
    documented = set(re.findall(r"build\.bat\s+(--[a-z-]+)", README.read_text(encoding="utf-8")))

    assert {"--lite", "--slim", "--all", "--no-smoke"} <= documented, documented
    assert documented <= _accepted(_text()), (
        "option citée dans le README mais inconnue du script : "
        + ", ".join(sorted(documented - _accepted(_text())))
    )


def test_each_variant_uses_an_existing_spec_and_its_own_folder() -> None:
    text = _text()
    for variant, spec, outdir in (
        ("slim", "OmniAgent-slim.spec", "dist\\OmniAgent-slim"),
        ("lite", "OmniAgent-lite.spec", "dist\\OmniAgent-lite"),
    ):
        block = _variant_block(text, variant)
        assert f'set "SPEC={spec}"' in block, block
        assert f'set "OUTDIR={outdir}"' in block, block
        assert (ROOT / spec).is_file(), f"recette absente : {spec}"

    # Le bloc final de `:build_variant` est le seul `) else (` suivi du calcul
    # de `EXE` ; le premier `) else (` du fichier est la distribution des options.
    main_match = re.search(r'\) else \(\n(?P<body>.*?)\n\)\nset "EXE=', text, re.S)
    assert main_match is not None, "bloc de la variante complète introuvable"
    assert 'set "SPEC=OmniAgent.spec"' in main_match.group("body"), main_match.group("body")
    assert (ROOT / "OmniAgent.spec").is_file()


def test_package_embeds_the_interface_theme_and_the_installer_copies_it() -> None:
    """Le thème doit voyager dans le paquet et dans l'installateur.

    `ui/app_gui.py` lit `assets/theme.json` à côté du paquet ; une ressource
    oubliée ne casse rien visiblement, la fenêtre retombe sur le thème par
    défaut de CustomTkinter. L'installateur doit ensuite copier l'arbre entier
    de `dist\\OmniAgent`, sinon le fichier reste dans le dossier de build.
    """
    from pyinstaller_recipe import project_assets

    assets = {
        Path(source).name: destination for source, destination in project_assets(ROOT)
    }
    assert assets.get("theme.json") == "assets", assets
    assert assets.get("icon.ico") == "assets", assets

    installer = (ROOT / "installer" / "OmniAgent.iss").read_text(encoding="utf-8")
    assert 'Source: "{#SourcePath}\\..\\dist\\OmniAgent\\*"' in installer, installer
    assert "recursesubdirs createallsubdirs" in installer, (
        "l'installateur doit copier les sous-dossiers du paquet"
    )


def test_all_builds_main_and_lite_and_no_smoke_disables_smokes() -> None:
    text = _text()
    all_match = re.search(r'if "%ALL%"=="1" \((.*?)\n\)', text, re.S)
    assert all_match is not None, "branche --all introuvable"
    all_block = all_match.group(1)

    assert "call :build_variant main" in all_block, all_block
    assert "call :build_variant lite" in all_block, all_block
    assert 'set "NOSMOKE=1"' in text, "--no-smoke doit être enregistré"
    assert 'if not "%NOSMOKE%"=="1"' in text, "--no-smoke doit garder les smokes conditionnels"
