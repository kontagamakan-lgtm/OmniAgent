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
from core.llm_router import LLMRouter
from core.approval import build_approval_gate, decision_from_confirm
from core.budget import build_budget_recorder
from core.metrics import build_metrics
from core.profile import apply_profile, build_network_guard, resolve_profile
from core.self_evolver import SelfEvolver, build_evolution_tools, evolution_core_path
from core.skill_loader import SkillLoader
from core.tool_registry import ConfirmHandler, ToolRegistry
from tools.base import Tool
from tools.browser_agent import BrowserAgent, build_browser_tools
from tools.capabilities import assert_capabilities
from tools.doc_index import DocIndex, build_doc_tools
from tools.file_manager import FileManager, build_file_tools
from tools.obsidian import ObsidianBrain, build_obsidian_tools
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
    brain: Any = None
    evolver: Any = None
    skills: Any = None
    router: Any = None
    owns_llm: bool = False
    metrics: Any = None
    _unsubscribe_metrics: Any = field(default=None, repr=False)
    _unsubscribe_budget: Any = field(default=None, repr=False)
    _unsubscribe_file_tx: Any = field(default=None, repr=False)
    _unsubscribe_brain: Any = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

    def tool_names(self) -> list[str]:
        return self.registry.names()

    def close(self) -> None:
        """Referme navigateur, recherche, client LLM et verrouille le coffre."""
        if self._closed:
            return
        self._closed = True
        if self.evolver is not None:
            try:
                self.evolver.stop()
            except Exception:  # noqa: BLE001 - l'arrêt ne bloque pas la fermeture
                logger.debug("arrêt de l'auto-évolution impossible", exc_info=True)
        if self._unsubscribe_file_tx is not None:
            try:
                self._unsubscribe_file_tx()
            except Exception:  # noqa: BLE001 - désabonnement best effort
                logger.debug("désabonnement de la transaction fichiers impossible", exc_info=True)
            self._unsubscribe_file_tx = None
        if self._unsubscribe_budget is not None:
            try:
                self._unsubscribe_budget()
            except Exception:  # noqa: BLE001 - désabonnement best effort
                logger.debug("désabonnement du budget impossible", exc_info=True)
            self._unsubscribe_budget = None
        if self._unsubscribe_metrics is not None:
            try:
                self._unsubscribe_metrics()
            except Exception:  # noqa: BLE001 - désabonnement best effort
                logger.debug("désabonnement des métriques impossible", exc_info=True)
            self._unsubscribe_metrics = None
        if self._unsubscribe_brain is not None:
            try:
                self._unsubscribe_brain()
            except Exception:  # noqa: BLE001
                logger.debug("désabonnement du cerveau impossible", exc_info=True)
            self._unsubscribe_brain = None
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
        if self.router is not None and self.agent.llm is not self.router:
            try:
                self.router.close()
            except Exception:  # noqa: BLE001
                logger.debug("fermeture du routeur LLM impossible", exc_info=True)
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
    profile: str | None = None,
) -> AgentBundle:
    """Construit l'agent et enregistre les outils réels.

    `with_web=False` et `with_browser=False` permettent de tourner sans réseau
    ni Playwright (tests, poste minimal) tout en gardant les outils fichiers et
    le coffre.
    """
    run_profile = resolve_profile(settings, profile)
    files = FileManager(settings)
    vault = vault or Vault(settings, file_manager=files)
    index = DocIndex(settings, files)
    # `files.ocr_enabled` est le drapeau d'activation de la famille OCR : sans
    # lui, les outils seraient proposés au modèle même quand l'utilisateur a
    # désactivé la reconnaissance de texte.
    ocr = Ocr(settings, files) if settings.files.ocr_enabled else None
    shell = ShellRunner(settings, files)
    brain: ObsidianBrain | None = None
    if settings.obsidian.enabled:
        try:
            brain = ObsidianBrain(settings)
        except (OSError, ValueError):
            logger.warning("vault Obsidian indisponible", exc_info=True)

    tools: list[Tool] = []
    tools.extend(build_file_tools(files))
    tools.extend(build_vault_tools(vault))
    tools.extend(build_doc_tools(index))
    if ocr is not None:
        tools.extend(build_ocr_tools(ocr))
    tools.extend(build_shell_tools(shell))
    if brain is not None:
        tools.extend(build_obsidian_tools(brain))

    # Cortex évolutif : les compétences forgées ne deviennent jamais des outils
    # arbitraires ; trois outils fixes et classés les exposent. Le moteur créé
    # ici est raccordé à l'agent juste après sa construction.
    evolver: SelfEvolver | None = None
    skills: SkillLoader | None = None
    if settings.evolution.enabled:
        try:
            skills = SkillLoader(
                evolution_core_path(settings) / "skills",
                max_chars=int(settings.evolution.max_skill_chars),
            )
            skills.load_all()
            evolver = SelfEvolver(settings, skills)
            tools.extend(build_evolution_tools(evolver))
        except (OSError, ValueError):
            logger.warning("cortex évolutif indisponible", exc_info=True)
            evolver = None
            skills = None

    searcher: WebSearcher | None = None
    if with_web:
        searcher = WebSearcher(settings)
        tools.extend(build_web_tools(searcher))

    browser: BrowserAgent | None = None
    if with_browser:
        browser = BrowserAgent(settings, vault, file_manager=files)
        tools.extend(build_browser_tools(browser))

    # Contrat des capacités : un outil non classé, ou dont les drapeaux ne
    # correspondent plus à `tools/capabilities.py`, bloque le démarrage plutôt
    # que d'hériter silencieusement des valeurs par défaut. Il est vérifié
    # **avant** le profil : sinon un outil inconnu serait filtré sans bruit.
    # Les ressources déjà ouvertes (index SQLite, navigateur) sont refermées
    # avant de lever : sinon un échec de contrat laisse des fichiers verrouillés.
    try:
        assert_capabilities(tools)
    except Exception:
        for resource in (browser, searcher, index, vault):
            closer = getattr(resource, "close", None)
            if not callable(closer):
                continue
            try:
                closer()
            except Exception:  # noqa: BLE001 - fermeture best effort
                logger.debug("fermeture après contrat invalide impossible", exc_info=True)
        raise

    # Profil de capacités : les outils hors profil ne sont ni proposés au
    # modèle ni exécutables.
    tools = apply_profile(tools, run_profile)
    # Confirmateur historique adapté ici, une fois : la porte ne connaît plus
    # que `decide(request)`, et une approbation booléenne vaut « une fois ».
    approval = build_approval_gate(settings, decision_from_confirm(confirm))
    registry = ToolRegistry(
        tools,
        max_output_chars=settings.agent.tool_output_max_chars,
        confirm=confirm,
        network_guard=build_network_guard(settings, run_profile),
        approval=approval,
        on_result=brain.remember_tool_result if brain is not None else None,
    )
    owns_llm = llm is None
    # Le routeur multi-clés remplace le client unique quand il est activé ; il
    # reste construit en mode désactivé si un client est injecté, pour que le
    # panneau de gestion des clés puisse lire l'état sans créer de doublon.
    router = LLMRouter(settings, vault) if settings.llm_router.enabled else None
    client = llm or router or LLMClient(settings.llm)
    agent = AgentCore(settings, client, registry, bus=bus)
    if evolver is not None:
        evolver.attach(agent, registry, brain)
        if router is not None:
            evolver.attach_router(router)
        agent.dynamic_prompt = evolver.prompt_overlay

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
    # Registre persistant : le coût survit aux redémarrages et alimente le
    # plafond global vérifié avant chaque appel au modèle.
    budget = build_budget_recorder(settings)
    unsubscribe_budget = agent.bus.subscribe(budget) if budget is not None else None
    # Transaction par run : journal et instantanés des seuls fichiers touchés,
    # ouverts au début de l'exécution et refermés à la fin.
    unsubscribe_file_tx = files.attach_transactions(agent.bus)
    unsubscribe_brain = (
        agent.bus.subscribe(brain.on_event, types=("run_started", "final"))
        if brain is not None
        else None
    )
    return AgentBundle(
        agent=agent,
        registry=registry,
        metrics=metrics,
        _unsubscribe_metrics=unsubscribe_metrics,
        _unsubscribe_budget=unsubscribe_budget,
        _unsubscribe_file_tx=unsubscribe_file_tx,
        _unsubscribe_brain=unsubscribe_brain,
        settings=settings,
        vault=vault,
        files=files,
        searcher=searcher,
        browser=browser,
        index=index,
        ocr=ocr,
        shell=shell,
        brain=brain,
        evolver=evolver,
        skills=skills,
        router=router,
        owns_llm=owns_llm,
    )
