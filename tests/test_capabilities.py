"""Contrat central des capacités des outils, validé au démarrage."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

import core.factory as factory  # noqa: E402
from core.resume import READ_ONLY_TOOLS  # noqa: E402
from tools.base import FunctionTool  # noqa: E402
from tools.browser_agent import BrowserAgent, build_browser_tools  # noqa: E402
from tools.capabilities import (  # noqa: E402
    CAPABILITIES,
    SCOPES,
    ToolContractError,
    assert_capabilities,
    capabilities_for,
    dangerous_tools,
    describe_table,
    replayable_tools,
    validate_capabilities,
)
from tools.doc_index import DocIndex, build_doc_tools  # noqa: E402
from tools.file_manager import FileManager, build_file_tools  # noqa: E402
from tools.obsidian import ObsidianBrain, build_obsidian_tools  # noqa: E402
from tools.ocr import Ocr, build_ocr_tools  # noqa: E402
from tools.shell import ShellRunner, build_shell_tools  # noqa: E402
from tools.vault import Vault, build_vault_tools  # noqa: E402
from tools.web_search import WebSearcher, build_web_tools  # noqa: E402

_SCHEMA = {"type": "object", "properties": {}}


def _all_family_tools(tmp: Path) -> list:
    """Toutes les familles réelles, y compris navigateur et OCR."""
    settings = make_settings(tmp)
    files = FileManager(settings)
    vault = Vault(settings, file_manager=files)
    index = DocIndex(settings, files)
    browser = BrowserAgent(settings, vault, file_manager=files)
    tools = [
        *build_file_tools(files),
        *build_vault_tools(vault),
        *build_doc_tools(index),
        *build_ocr_tools(Ocr(settings, files)),
        *build_obsidian_tools(ObsidianBrain(settings)),
        *build_shell_tools(ShellRunner(settings, files)),
        *build_web_tools(WebSearcher(settings)),
        *build_browser_tools(browser),
    ]
    index.close()
    return tools


def test_every_built_tool_is_classified_and_matches_the_contract() -> None:
    with tmpdir("capacites") as tmp:
        tools = _all_family_tools(tmp)

    names = {tool.name for tool in tools}
    assert names, "aucun outil construit : le test ne prouverait rien"
    assert validate_capabilities(tools) == []
    # Aucune entrée orpheline : un outil renommé ne doit pas rester classé
    # sous son ancien nom pendant que le nouveau passe inaperçu.
    assert names == set(CAPABILITIES)
    assert len(names) == 56


def test_an_unclassified_tool_is_refused() -> None:
    rogue = FunctionTool(
        "fs_shred_everything",
        "Outil ajouté sans classification.",
        _SCHEMA,
        lambda: "ok",
    )

    problems = validate_capabilities([rogue])
    assert problems and "fs_shred_everything" in problems[0]
    assert "non classé" in problems[0]
    with pytest.raises(ToolContractError):
        assert_capabilities([rogue])
    with pytest.raises(ToolContractError):
        capabilities_for("fs_shred_everything")


def test_flags_that_drift_from_the_contract_are_refused() -> None:
    # Un outil d'écriture qui se déclare en lecture seule : le rejeu d'une
    # trace l'exécuterait sans confirmation.
    liar = FunctionTool(
        "fs_read_text",
        "Lit un fichier, mais prétend être une écriture.",
        _SCHEMA,
        lambda: "",
        read_only=False,
    )

    problems = validate_capabilities([liar])
    assert any("fs_read_text" in problem for problem in problems)
    assert any("lecture" in problem for problem in problems)
    with pytest.raises(ToolContractError):
        assert_capabilities([liar])


def test_startup_refuses_an_unclassified_tool(monkeypatch) -> None:
    rogue = FunctionTool("fs_secret_shredder", "Non classé.", _SCHEMA, lambda: "ok")
    original = factory.build_file_tools

    def _with_rogue(files):
        return [*original(files), rogue]

    monkeypatch.setattr(factory, "build_file_tools", _with_rogue)

    with tmpdir("demarrage") as tmp:
        settings = make_settings(tmp)
        with pytest.raises(ToolContractError) as caught:
            factory.build_agent(settings, with_web=False, with_browser=False)

    assert "fs_secret_shredder" in str(caught.value)


def test_dangerous_and_secret_families_are_the_expected_ones() -> None:
    dangerous = {name for name, caps in CAPABILITIES.items() if caps.dangerous}
    sensitive = {name for name, caps in CAPABILITIES.items() if caps.sensitive}
    assert dangerous == {"fs_delete", "vault_delete_entry", "shell_run"}
    assert sensitive == {
        "vault_create_credential",
        "vault_add_entry",
        "vault_update_entry",
        "extract_cookies",
    }
    # Un outil secret ou dangereux n'est jamais rejoué comme une lecture.
    for name in dangerous | sensitive:
        assert not CAPABILITIES[name].read_only or name not in READ_ONLY_TOOLS


def test_resume_whitelist_matches_the_contract() -> None:
    assert READ_ONLY_TOOLS, "la liste blanche de reprise ne doit pas être vide"
    for name in READ_ONLY_TOOLS:
        capabilities = capabilities_for(name)
        assert capabilities.read_only, name
        assert not capabilities.dangerous, name


def test_replay_whitelist_is_derived_and_has_no_stale_name() -> None:
    assert replayable_tools() == READ_ONLY_TOOLS
    # Le nom disparu qui motivait ce contrat ne doit jamais revenir.
    assert "fs_read" not in READ_ONLY_TOOLS
    assert "fs_read" not in CAPABILITIES
    for name in READ_ONLY_TOOLS:
        assert CAPABILITIES[name].replayable, name
        assert not CAPABILITIES[name].sensitive, name
    # Un outil d'écriture ne peut pas être rejoué, même s'il se dit sans danger.
    assert not replayable_tools() & {n for n, c in CAPABILITIES.items() if c.writes}
    assert dangerous_tools() == {"fs_delete", "vault_delete_entry", "shell_run"}


def test_scopes_and_network_are_declared_for_every_tool() -> None:
    assert {caps.scope for caps in CAPABILITIES.values()} <= SCOPES
    assert {caps.scope for caps in CAPABILITIES.values()} == SCOPES
    network = {name for name, caps in CAPABILITIES.items() if caps.network}
    assert "web_search" in network and "browser_open" in network
    assert not any(name.startswith("fs_") for name in network)
    # La portée distingue ce que l'interface doit montrer avant confirmation.
    assert CAPABILITIES["shell_run"].scope == "shell"
    assert CAPABILITIES["vault_add_entry"].scope == "vault"


def test_documentation_table_covers_every_classified_tool() -> None:
    table = describe_table()
    assert table.startswith("| Outil |")
    for name in CAPABILITIES:
        assert f"`{name}`" in table
    assert "fs_delete" in table and "oui" in table
