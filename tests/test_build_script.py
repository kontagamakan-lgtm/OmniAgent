"""Garde-fous sur `build.bat` : options documentées et recettes `.spec`.

Des scripts extérieurs (CI, raccourcis, alias `--slim` historique) appellent ces
options : en supprimer une, ou déplacer une variante vers un `.spec` disparu,
casserait ces appels sans qu'aucun autre test ne le voie. Ces contrôles lisent
le script réel — sans l'exécuter — et échouent dès qu'une option documentée
n'est plus acceptée ou qu'une recette manque.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
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


#: Répertoires dont les imports ne partent pas dans l'exécutable.
_NOT_SHIPPED = frozenset({"tests", "build", "dist", "data", ".git", ".venv", "installer"})


def _dynamic_targets() -> dict[str, str]:
    """Cible -> « fichier:ligne » pour chaque import dynamique littéral.

    PyInstaller ne suit pas un nom calculé à l'exécution : `__import__`,
    `importlib.import_module` et les aides du type `FileManager._require`
    doivent déclarer leur cible en `hiddenimports`, sinon le module manque
    silencieusement une fois figé.
    """
    targets: dict[str, str] = {}
    for path in sorted(ROOT.rglob("*.py")):
        if _NOT_SHIPPED & set(path.relative_to(ROOT).parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # fichier de données ou fragment : hors périmètre
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            function = node.func
            name = (
                function.id
                if isinstance(function, ast.Name)
                else getattr(function, "attr", "")
            )
            if name not in {"__import__", "import_module"} and not name.endswith(
                "_require"
            ):
                continue
            argument = node.args[0]
            if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)):
                continue
            targets.setdefault(
                argument.value,
                f"{path.relative_to(ROOT).as_posix()}:{node.lineno}",
            )
    return targets


def test_dynamic_imports_are_declared_as_hidden_imports() -> None:
    """Tout module importé par une chaîne doit être déclaré et installable."""
    from pyinstaller_recipe import hidden_imports

    declared = {name.split(".")[0] for name in hidden_imports()}
    declared |= {"core", "tools", "ui", "security"}  # analysés statiquement
    targets = _dynamic_targets()
    assert targets, "aucun import dynamique détecté : le balayage est cassé"

    undeclared = {
        top: origin
        for target, origin in targets.items()
        for top in [target.split(".")[0]]
        if top not in declared and top not in sys.stdlib_module_names
    }
    assert undeclared == {}, (
        "module importé dynamiquement mais absent des imports cachés : "
        + ", ".join(f"{top} ({origin})" for top, origin in sorted(undeclared.items()))
    )

    unresolvable = [
        name for name in hidden_imports() if importlib.util.find_spec(name) is None
    ]
    assert unresolvable == [], (
        "import caché introuvable (faute de frappe ou dépendance manquante) : "
        + ", ".join(unresolvable)
    )


def test_hidden_imports_cover_the_lazily_resolved_core_submodules() -> None:
    """`core` résout ses sous-modules par PEP 562 : l'analyse ne les voit pas.

    Sans déclaration, `from core import AgentRunner` lève dans le paquet figé
    `ModuleNotFoundError: No module named 'core.runner'` ; le contrôle
    `--compat-smoke` de la CI y est resté bloqué jusqu'au délai de 240 s.
    """
    import core
    from pyinstaller_recipe import hidden_imports

    declared = set(hidden_imports())
    missing = set(core.SUBMODULES) - declared
    assert missing == set(), "sous-module de core absent des imports cachés : " + ", ".join(
        sorted(missing)
    )


#: Lignes de vérification du README : `python tests/test_x.py :: N/M`.
_COUNTER = re.compile(r"^python tests/(test_\w+)\.py\s+:: (\d+)/(\d+)$", re.M)
_GOLDEN = re.compile(r"^python tests/golden_traces\.py\s+:: (\d+) scénarios", re.M)


def test_readme_test_counters_match_the_test_files() -> None:
    """Les compteurs du README sont une promesse : ils doivent être vrais.

    Quatre des cinq annoncés avaient dérivé (crypto, redaction, vault,
    integration) : un lecteur lançait la commande et croyait à une régression
    ou à un test manquant. Le nombre affiché est celui des `test_*` du module,
    exactement ce que `support.run` exécute.
    """
    readme = README.read_text(encoding="utf-8")
    counters = _COUNTER.findall(readme)
    assert counters, "aucun compteur `python tests/test_*.py :: N/M` dans le README"

    mismatches = []
    for module, shown, total in counters:
        source = (ROOT / "tests" / f"{module}.py").read_text(
            encoding="utf-8", errors="replace"
        )
        declared = len(re.findall(r"^def test_", source, re.M))
        if shown != str(declared) or total != str(declared):
            mismatches.append(
                f"{module}.py : README {shown}/{total}, {declared} test(s) réels"
            )
    assert mismatches == [], "compteurs du README périmés : " + "; ".join(mismatches)

    golden = _GOLDEN.search(readme)
    assert golden is not None, "le README ne cite plus les golden traces"
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        import golden_traces
    finally:
        sys.path.pop(0)
    assert int(golden.group(1)) == len(golden_traces.SCENARIOS), (
        f"golden traces : README {golden.group(1)}, "
        f"{len(golden_traces.SCENARIOS)} scénario(s) réels"
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
