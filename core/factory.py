"""Fabrique : assemble l'agent complet avec ses outils réels.

Un seul point d'entrée, pour que l'interface graphique et les tests construisent
exactement le même objet :

* `FileManager` + outils `fs_*` ;
* `Vault` + outils `vault_*` ;
* `WebSearcher` + outils web (`web_*`, `fs_*` de lecture distante) ;
* `BrowserAgent` + outils navigateur, optionnel (Playwright peut être absent) ;
* `DocIndex` + outils `doc_index_*` (SQLite FTS5) ;
* `Ocr` + outils `ocr_*` (Tesseract local, échoue proprement s'il est absent),
  enregistrés seulement si `files.ocr_enabled` ;
* `ShellRunner` + outils `shell_*` (exécution confirmée, répertoire confiné).

Les ressources créées sont refermées par `AgentBundle.close()` ; le client LLM
n'est fermé que s'il a été construit ici, jamais celui injecté par l'appelant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from config import APP_ID, Settings
from core.agent_core import AgentCore
from core.events import EventBus
from core.llm_client import LLMClient
from core.metrics import build_metrics
from core.tool_registry import ConfirmHandler, ToolRegistry
from tools.base import Tool
from tools.browser_agent import BrowserAgent, build_browser_tools
from tools.capabilities import assert_capabilities
from tools.doc_index import DocIndex, build_doc_tools
from tools.file_manager import FileManager, build_file_tools
from tools.ocr import Ocr, build_ocr_tools
from tools.shell import ShellRunner, build_shell_tools
from tools.vault import Vault, build_vault_tools
from tools.web_search import WebSearcher, build_web_tools

__all__ = ["AgentBundle", "build_agent"]

logger = logging.getLogger(f"{APP_ID}.factory")


@dataclass(slots=True)
class AgentBundle:
    """Agent prêt à l'emploi et ressources associées."""

    agent: AgentCore
    registry: ToolRegistry
    settings: Settings
    vault: Vault
    files: FileManager
    searcher: WebSearcher | None = None
    browser: BrowserAgent | None = None
    index: DocIndex | None = None
    ocr: Ocr | None = None
    shell: ShellRunner | None = None
    owns_llm: bool = False
    metrics: Any = None
    _unsubscribe_metrics: Any = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

    def tool_names(self) -> list[str]:
        return self.registry.names()

    def close(self) -> None:
        """Referme navigateur, recherche, client LLM et verrouille le coffre."""
        if self._closed:
            return
        self._closed = True
        if self._unsubscribe_metrics is not None:
            try:
                self._unsubscribe_metrics()
            except Exception:  # noqa: BLE001 - désabonnement best effort
                logger.debug("désabonnement des métriques impossible", exc_info=True)
            self._unsubscribe_metrics = None
        if self.browser is not None:
            try:
                self.browser.stop()
            except Exception:  # noqa: BLE001 - fermeture best effort
                logger.debug("arrêt du navigateur impossible", exc_info=True)
        if self.searcher is not None:
            try:
                self.searcher.close()
            except Exception:  # noqa: BLE001
                logger.debug("fermeture du client de recherche impossible", exc_info=True)
        if self.index is not None:
            try:
                self.index.close()
            except Exception:  # noqa: BLE001
                logger.debug("fermeture de l'index documentaire impossible", exc_info=True)
        if self.owns_llm:
            try:
                self.agent.llm.close()
            except Exception:  # noqa: BLE001
                logger.debug("fermeture du client LLM impossible", exc_info=True)
        if self.vault.unlocked:
            try:
                self.vault.lock(reason="fermeture de l'agent")
            except Exception:  # noqa: BLE001
                logger.debug("verrouillage du coffre impossible", exc_info=True)

    def __enter__(self) -> "AgentBundle":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def build_agent(
    settings: Settings,
    *,
    llm: LLMClient | None = None,
    bus: EventBus | None = None,
    confirm: ConfirmHandler | None = None,
    with_web: bool = True,
    with_browser: bool = True,
    vault: Vault | None = None,
) -> AgentBundle:
    """Construit l'agent et enregistre les outils réels.

    `with_web=False` et `with_browser=False` permettent de tourner sans réseau
    ni Playwright (tests, poste minimal) tout en gardant les outils fichiers et
    le coffre.
    """
    files = FileManager(settings)
    vault = vault or Vault(settings, file_manager=files)
    index = DocIndex(settings, files)
    # `files.ocr_enabled` est le drapeau d'activation de la famille OCR : sans
    # lui, les outils seraient proposés au modèle même quand l'utilisateur a
    # désactivé la reconnaissance de texte.
    ocr = Ocr(settings, files) if settings.files.ocr_enabled else None
    shell = ShellRunner(settings, files)

    tools: list[Tool] = []
    tools.extend(build_file_tools(files))
    tools.extend(build_vault_tools(vault))
    tools.extend(build_doc_tools(index))
    if ocr is not None:
        tools.extend(build_ocr_tools(ocr))
    tools.extend(build_shell_tools(shell))

    searcher: WebSearcher | None = None
    if with_web:
        searcher = WebSearcher(settings)
        tools.extend(build_web_tools(searcher))

    browser: BrowserAgent | None = None
    if with_browser:
        browser = BrowserAgent(settings, vault, file_manager=files)
        tools.extend(build_browser_tools(browser))

    registry = ToolRegistry(
        tools,
        max_output_chars=settings.agent.tool_output_max_chars,
        confirm=confirm,
    )
    # Contrat des capacités : un outil non classé, ou dont les drapeaux ne
    # correspondent plus à `tools/capabilities.py`, bloque le démarrage plutôt
    # que d'hériter silencieusement des valeurs par défaut.
    assert_capabilities(registry.toolset.tools())
    owns_llm = llm is None
    client = llm or LLMClient(settings.llm)
    agent = AgentCore(settings, client, registry, bus=bus)

    logger.debug(
        "agent construit : %s outils (%s)",
        len(registry),
        ", ".join(registry.names()),
    )
    # Métriques par run : un enregistrement à `run_finished`, borné. Le
    # désabonnement est porté par le bundle, sinon un bus refermé garderait la
    # référence au fichier.
    metrics = build_metrics(settings)
    unsubscribe_metrics = agent.bus.subscribe(metrics) if metrics is not None else None
    return AgentBundle(
        agent=agent,
        registry=registry,
        metrics=metrics,
        _unsubscribe_metrics=unsubscribe_metrics,
        settings=settings,
        vault=vault,
        files=files,
        searcher=searcher,
        browser=browser,
        index=index,
        ocr=ocr,
        shell=shell,
        owns_llm=owns_llm,
    )
