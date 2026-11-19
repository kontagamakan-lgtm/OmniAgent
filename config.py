"""OmniAgent Windows — configuration centrale, validation et persistance.

Ce module est l'unique source de vérité pour tous les réglages de
l'application. Il n'utilise *que* la bibliothèque standard afin de rester
lisible, validable et exécutable avant même l'installation des paquets
(`python config.py --validate`).

Principes de conception
-----------------------
1. Sections typées (`LLMConfig`, `BrowserConfig`, ...) regroupées dans un
   objet `Settings` sérialisable en JSON.
2. Surcharge par variables d'environnement : `OMNIAGENT_<SECTION>_<CHAMP>`
   (ex. `OMNIAGENT_LLM_MODEL`, `OMNIAGENT_BROWSER_HEADLESS=1`).
3. Les chemins ne sont *jamais* persistés : ils sont recalculés au démarrage
   selon le mode d'exécution (sources, .exe PyInstaller, ou `OMNIAGENT_DATA_DIR`).
4. Toute valeur est validée avant démarrage ; les erreurs bloquent, les
   avertissements sont remontés à l'interface.

Usage CLI
---------
    python config.py --init        # crée data/, config.json, .env.example
    python config.py --validate    # code retour 1 si la config est invalide
    python config.py --show        # config effective (secrets masqués)
    python config.py --defaults    # config par défaut au format JSON
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field, fields, is_dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal, get_args, get_origin, get_type_hints

# ---------------------------------------------------------------------------
# Identité de l'application
# ---------------------------------------------------------------------------

APP_NAME = "OmniAgent Windows"
APP_ID = "OmniAgent"
APP_VERSION = "1.0.0"
CONFIG_SCHEMA_VERSION = 1
#: Version du format des fichiers de trace. À incrémenter quand la forme des
#: enregistrements change : un lecteur peut alors refuser ou migrer.
TRACE_FORMAT_VERSION = 3
ENV_PREFIX = "OMNIAGENT_"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

SEARCH_ENGINES = (
    "duckduckgo_html",
    "duckduckgo_lite",
    "duckduckgo_api",
    "searx",
    "brave",
    "bing",
)

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: Bornes par défaut de la rétention des traces. Elles vivent ici parce que
#: `config.py` est la source unique des réglages ajustables.
DEFAULT_TRACE_MAX_RUNS = 50
DEFAULT_TRACE_MAX_AGE_DAYS = 30.0
DEFAULT_TRACE_MAX_BYTES = 20 * 1024 * 1024
TRACE_CONTENTS = ("full", "metadata")

BROWSER_CHANNELS = (
    "",
    "chromium",
    "chrome",
    "chrome-beta",
    "chrome-dev",
    "msedge",
    "msedge-beta",
    "msedge-dev",
)


# ---------------------------------------------------------------------------
# Erreurs
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ConfigIssue:
    """Problème de configuration remonté à l'interface ou à la CLI."""

    level: Literal["error", "warning"]
    field: str
    message: str

    def __str__(self) -> str:
        prefix = "ERREUR" if self.level == "error" else "AVERTISSEMENT"
        return f"[{prefix}] {self.field}: {self.message}"


class ConfigError(Exception):
    """Configuration illisible ou invalide. Bloque le démarrage."""

    def __init__(self, message: str, issues: list[ConfigIssue] | None = None) -> None:
        super().__init__(message)
        self.issues: list[ConfigIssue] = issues or []


# ---------------------------------------------------------------------------
# Résolution des chemins
# ---------------------------------------------------------------------------


def expand_path(value: str | os.PathLike[str]) -> Path:
    """Développe les variables d'environnement (`%USERPROFILE%`) et `~`."""
    text = os.path.expandvars(str(value))
    return Path(text).expanduser().resolve()


def is_frozen() -> bool:
    """Vrai si l'application tourne depuis un exécutable PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    """Répertoire d'installation : dossier des sources ou de l'exécutable."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def portable_mode(install: Path | None = None) -> bool:
    """Mode portable : un fichier `portable.txt` à côté de l'exe force data/ local."""
    base = install or install_dir()
    return (base / "portable.txt").is_file()


def data_dir(install: Path | None = None) -> Path:
    """Répertoire de données inscriptible.

    Priorité : `OMNIAGENT_DATA_DIR` > mode portable > `%LOCALAPPDATA%\\OmniAgent`
    (exe) > `<projet>/data` (sources).
    """
    override = os.environ.get(f"{ENV_PREFIX}DATA_DIR")
    if override:
        return expand_path(override)
    base = install or install_dir()
    if is_frozen():
        if portable_mode(base):
            return base / "data"
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        return Path(root) / APP_ID
    return base / "data"


@dataclass(slots=True)
class Paths:
    """Emplacements calculés à l'exécution (jamais persistés en JSON)."""

    install_dir: Path
    data_dir: Path
    config_file: Path
    vault_file: Path
    database_file: Path
    index_dir: Path
    log_dir: Path
    download_dir: Path
    cache_dir: Path
    report_dir: Path
    browser_profile_dir: Path

    def as_dict(self) -> dict[str, str]:
        return {f.name: str(getattr(self, f.name)) for f in fields(self)}


def default_paths() -> Paths:
    install = install_dir()
    data = data_dir(install)
    return Paths(
        install_dir=install,
        data_dir=data,
        config_file=data / "config.json",
        vault_file=data / "vault.enc",
        database_file=data / "omniagent.db",
        index_dir=data / "index",
        log_dir=data / "logs",
        download_dir=data / "downloads",
        cache_dir=data / "cache",
        report_dir=data / "reports",
        browser_profile_dir=data / "browser_profile",
    )


def default_allowed_roots() -> list[str]:
    """Répertoires autorisés par défaut pour les outils fichiers."""
    home = Path.home()
    candidates = [
        home / "Documents",
        home / "Desktop",
        home / "Downloads",
        home / "Pictures",
        install_dir(),
    ]
    seen: set[str] = set()
    roots: list[str] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            roots.append(str(candidate))
    return roots


# ---------------------------------------------------------------------------
# Sections de configuration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LLMConfig:
    """Backend de génération (DeepSeek, endpoint compatible OpenAI, Ollama)."""

    backend: Literal["deepseek", "openai_compatible", "ollama"] = "deepseek"
    base_url: str = "https://api.deepseek.com/v1"
    api_key_env: str = "DEEPSEEK_API_KEY"
    model: str = "deepseek-chat"
    reasoning_model: str = "deepseek-reasoner"
    fast_model: str = "deepseek-chat"
    temperature: float = 0.25
    top_p: float = 0.95
    max_output_tokens: int = 4096
    context_window: int = 65536
    request_timeout_s: float = 180.0
    stream: bool = True
    max_retries: int = 4
    retry_backoff_s: float = 2.0
    force_json_tool_calls: bool = True
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_keep_alive: str = "15m"
    #: Prix par million de tokens (USD). 0 = table de `core.cost` selon le modele.
    price_prompt_per_million: float = 0.0
    price_completion_per_million: float = 0.0

    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "").strip()

    def has_credentials(self) -> bool:
        if self.backend == "ollama":
            return True
        return bool(self.api_key())


@dataclass(slots=True)
class EmbeddingConfig:
    """Fournisseur de vecteurs pour l'index sémantique local."""

    backend: Literal["ollama", "openai_compatible", "disabled"] = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    model: str = "nomic-embed-text"
    api_key_env: str = "OPENAI_API_KEY"
    dimension: int = 768
    batch_size: int = 32
    request_timeout_s: float = 60.0

    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "").strip()


@dataclass(slots=True)
class BrowserConfig:
    """Moteur Playwright et profil persistant."""

    engine: Literal["chromium", "firefox", "webkit"] = "chromium"
    channel: str = ""
    headless: bool = False
    slow_mo_ms: int = 0
    action_timeout_ms: int = 15000
    navigation_timeout_ms: int = 45000
    viewport_width: int = 1440
    viewport_height: int = 900
    locale: str = "fr-FR"
    timezone: str = "Europe/Paris"
    user_agent: str = ""
    block_media: bool = False
    ignore_https_errors: bool = False
    proxy_server: str = ""
    persist_cookies: bool = True
    screenshot_on_error: bool = True
    download_allowed: bool = True
    max_pages_per_task: int = 25


@dataclass(slots=True)
class SearchConfig:
    """Recherche web multi-moteurs et extraction de contenu."""

    engines: list[str] = field(
        default_factory=lambda: ["duckduckgo_api", "bing", "duckduckgo_html", "duckduckgo_lite", "searx"]
    )
    searx_instances: list[str] = field(
        default_factory=lambda: [
            "https://searx.be",
            "https://search.bus-hit.me",
            "https://priv.au",
        ]
    )
    brave_api_key_env: str = "BRAVE_API_KEY"
    region: str = "fr-fr"
    safe_search: Literal["off", "moderate", "strict"] = "off"
    max_results_per_query: int = 10
    max_concurrent_fetches: int = 6
    request_timeout_s: float = 25.0
    per_host_delay_s: float = 0.75
    user_agent: str = DEFAULT_USER_AGENT
    min_content_chars: int = 400
    cache_ttl_s: int = 3600
    respect_robots: bool = True
    # Désactivé par défaut : un contenu web lu par l'agent peut lui dicter une URL.
    # À n'activer que pour une cible locale maîtrisée (SearxNG auto-hébergé, tests).
    allow_private_networks: bool = False
    blocked_domains: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FilesConfig:
    """Portée et garde-fous des outils fichiers."""

    allowed_roots: list[str] = field(default_factory=default_allowed_roots)
    denied_globs: list[str] = field(
        default_factory=lambda: [
            "**/.git/**",
            "**/node_modules/**",
            "**/__pycache__/**",
            "**/$Recycle.Bin/**",
            "**/System Volume Information/**",
            "**/Windows/**",
            "**/Program Files/**",
            "**/Program Files (x86)/**",
        ]
    )
    max_read_bytes: int = 64 * 1024 * 1024
    max_write_bytes: int = 256 * 1024 * 1024
    follow_symlinks: bool = False
    backup_before_overwrite: bool = True
    use_recycle_bin: bool = True
    index_extensions: list[str] = field(
        default_factory=lambda: [
            ".pdf", ".docx", ".doc", ".txt", ".md", ".rtf",
            ".xlsx", ".xlsm", ".csv", ".pptx",
            ".json", ".xml", ".yaml", ".yml", ".html", ".htm",
            ".py", ".js", ".ts", ".ps1", ".bat", ".sh", ".sql",
            ".c", ".cpp", ".h", ".cs", ".java", ".go", ".rs",
            ".ini", ".toml", ".cfg", ".log",
            ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
        ]
    )
    max_index_file_mb: int = 32
    ocr_enabled: bool = True
    ocr_languages: str = "fra+eng"
    tesseract_cmd: str = ""
    watch_debounce_s: float = 1.5
    max_search_results: int = 500


@dataclass(slots=True)
class VaultConfig:
    """Paramètres cryptographiques du coffre-fort d'identifiants."""

    kdf: Literal["argon2id", "scrypt"] = "argon2id"
    argon2_time_cost: int = 3
    argon2_memory_kib: int = 65536
    argon2_parallelism: int = 4
    scrypt_n: int = 32768
    scrypt_r: int = 8
    scrypt_p: int = 1
    auto_lock_minutes: int = 15
    max_failed_unlocks: int = 5
    clipboard_clear_s: int = 30
    audit_log: bool = True
    # Désactivé par défaut : l'activer délègue la phrase secrète au Gestionnaire
    # d'identifiants Windows, qui la conserve de façon persistante. Le README
    # garantit la non-persistance de la phrase ; le dépôt doit rester opt-in.
    keyring_fallback: bool = False


@dataclass(slots=True)
class AgentConfig:
    """Boucle ReAct : budget, reprises, profondeur de recherche."""

    action_first: bool = True
    max_steps: int = 24
    parallel_tool_calls: int = 3
    max_recovery_attempts: int = 5
    require_plan_for_multistep: bool = True
    deep_research_enabled: bool = True
    deep_research_min_queries: int = 4
    deep_research_max_queries: int = 8
    deep_research_pages_per_query: int = 4
    deep_research_max_pages: int = 20
    min_sources_for_report: int = 3
    history_max_messages: int = 60
    max_context_tokens: int = 48000
    tool_output_max_chars: int = 12000
    final_answer_requires_evidence: bool = True
    language: str = "fr"


@dataclass(slots=True)
class UIConfig:
    """Apparence et comportement de l'interface CustomTkinter."""

    appearance: Literal["dark", "light", "system"] = "dark"
    accent: str = "#3B82F6"
    font_family: str = "Segoe UI"
    font_size: int = 13
    window_width: int = 1280
    window_height: int = 860
    stream_refresh_ms: int = 60
    show_reasoning: bool = False
    confirm_destructive: bool = False
    max_log_lines: int = 4000


@dataclass(slots=True)
class TraceConfig:
    """Traces JSONL : activation, contenu conservé et rétention."""

    enabled: bool = True
    #: `full` écrit la tâche et les réponses ; `metadata` les retire et ne garde
    #: que les faits (étapes, outils, compteurs), pour ne pas laisser le contenu
    #: d'une session sur disque.
    content: Literal["full", "metadata"] = "full"
    max_runs: int = DEFAULT_TRACE_MAX_RUNS
    max_age_days: float = DEFAULT_TRACE_MAX_AGE_DAYS
    #: Plafond global, appliqué aussi au fichier courant ; 0 le désactive.
    max_bytes: int = DEFAULT_TRACE_MAX_BYTES
    #: Compresser les vieilles traces (`.jsonl.gz`) au lieu de les supprimer.
    archive: bool = False
    #: Taille à partir de laquelle un run terminé est archivé immédiatement
    #: (0 : attendre la purge suivante).
    archive_min_bytes: int = 64 * 1024
    #: Chiffrer le contenu des traces au repos (AES-256-GCM, une enveloppe par
    #: ligne). Sans clé valide, aucune trace n'est écrite : jamais de repli clair.
    encrypt: bool = False
    #: Variable d'environnement portant la clé (32 octets en base64url ou hex).
    key_env: str = "OMNIAGENT_TRACE_KEY"
    #: Tracer chaque espace de travail dans son propre sous-dossier, donc avec
    #: son propre quota : un projet bavard ne purge pas l'historique d'un autre.
    per_workspace: bool = False
    #: Tenir `<runs>/runs.index` : liste et états sans relire chaque trace.
    index: bool = True
    #: `fsync` groupé toutes les N lignes (0 = jamais) : survit à une coupure.
    fsync_every: int = 0
    #: Durée de validité du marqueur d'activité d'une exécution concurrente.
    #: Supérieure au délai d'un appel au fournisseur (`llm.request_timeout_s`),
    #: sinon un run légitimement silencieux paraîtrait abandonné.
    active_ttl_s: float = 300.0


@dataclass(slots=True)
class AutonomyConfig:
    """Mode autonome à la demande : objectifs, cadence et bornes.

    Désactivé par défaut au sens où rien ne tourne sans un lancement explicite
    (bouton ou `--autonomy`). L'agent ne fait rien tant que `goals.md` est vide.
    """

    #: Fichier d'objectifs relu à chaque cycle ; vide = `data/autonomy/goals.md`.
    goals_file: str = ""
    #: Cycles par session (un cycle = une tâche choisie puis exécutée).
    max_cycles: int = 3
    #: Attente entre deux cycles ; 0 enchaîne sans pause (tests, une tâche).
    interval_minutes: float = 30.0
    #: Entrées conservées dans `journal.jsonl` (les plus récentes).
    journal_max_entries: int = 200
    #: Taille maximale du journal ; dépassée, les plus vieux cycles partent.
    journal_max_bytes: int = 256 * 1024
    #: Plafond de la session, en tokens facturés (prompt + complétion). 0 = aucun.
    max_total_tokens: int = 200_000
    #: Plafond d'un cycle ; l'exécution est annulée au dépassement. 0 = aucun.
    max_tokens_per_cycle: int = 60_000
    #: Appels d'outils par cycle avant annulation. 0 = aucun plafond.
    max_tool_calls_per_cycle: int = 40
    #: Une proposition non traitée au-delà de ce délai expire : l'environnement
    #: a pu changer, et une approbation tardive n'aurait plus de sens.
    proposal_max_age_days: int = 14
    #: Plafond de la session en devise (USD estimee). 0 = aucun.
    max_total_cost: float = 0.0


@dataclass(slots=True)
class StorageConfig:
    """Rétention des dossiers de travail (rapports, cache, téléchargements).

    Les traces et le journal d'autonomie ont leurs propres bornes ; ces
    dossiers-là n'en avaient aucune et grossissaient sans fin. Aucune purge
    n'est automatique : `--clean` montre le plan, `--clean-apply` l'exécute.
    Un âge ou une taille à 0 désactive la règle correspondante.
    """

    reports_max_age_days: int = 30
    reports_max_bytes: int = 32 * 1024 * 1024
    cache_max_age_days: int = 7
    cache_max_bytes: int = 128 * 1024 * 1024
    #: Téléchargements : fichiers de l'utilisateur, donc désactivé par défaut.
    downloads_max_age_days: int = 0
    downloads_max_bytes: int = 0
    #: Profil navigateur : seuls les sous-dossiers de cache sont concernés.
    browser_cache_max_age_days: int = 7
    browser_cache_max_bytes: int = 256 * 1024 * 1024


@dataclass(slots=True)
class MetricsConfig:
    """Journal de métriques par run (une ligne par exécution terminée)."""

    enabled: bool = True
    max_entries: int = 1000
    max_bytes: int = 2 * 1024 * 1024


@dataclass(slots=True)
class LoggingConfig:
    """Journalisation et masquage des secrets."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    file_max_bytes: int = 8 * 1024 * 1024
    backup_count: int = 5
    log_prompts: bool = True
    log_tool_arguments: bool = True
    log_tool_results: bool = False
    redact_secrets: bool = True
    console: bool = True


# ---------------------------------------------------------------------------
# Agrégat
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Settings:
    """Configuration complète de l'application."""

    schema_version: int = CONFIG_SCHEMA_VERSION
    paths: Paths = field(default_factory=default_paths)
    llm: LLMConfig = field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    files: FilesConfig = field(default_factory=FilesConfig)
    vault: VaultConfig = field(default_factory=VaultConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    trace: TraceConfig = field(default_factory=TraceConfig)
    autonomy: AutonomyConfig = field(default_factory=AutonomyConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)

    # -- persistance --------------------------------------------------------

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str] | None = None,
        *,
        apply_env: bool = True,
        strict: bool = True,
        dotenv: bool = True,
    ) -> "Settings":
        """Charge la configuration, applique l'environnement et valide."""
        if dotenv:
            load_dotenv(install_dir() / ".env")

        paths = default_paths()
        target = expand_path(path) if path else paths.config_file

        raw: dict[str, Any] = {}
        if target.is_file():
            try:
                raw = json.loads(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ConfigError(
                    f"Fichier de configuration illisible ({target}): {exc}"
                ) from exc
            except OSError as exc:
                raise ConfigError(
                    f"Impossible de lire la configuration ({target}): {exc}"
                ) from exc
            if not isinstance(raw, dict):
                raise ConfigError(f"Configuration invalide: objet JSON attendu ({target})")
            raw.pop("paths", None)
            declared = raw.get("schema_version", CONFIG_SCHEMA_VERSION)
            if isinstance(declared, int) and declared > CONFIG_SCHEMA_VERSION:
                raise ConfigError(
                    f"Configuration écrite par une version plus récente "
                    f"(schéma {declared} > {CONFIG_SCHEMA_VERSION}). Mettez l'application à jour."
                )

        settings = _build_dataclass(cls, raw)
        settings.paths = paths

        if apply_env:
            for key, _name in _apply_env_overrides(settings):
                logging.getLogger(APP_ID).debug("Surcharge environnement: %s", key)

        issues = settings.validate()
        errors = [i for i in issues if i.level == "error"]
        if strict and errors:
            raise ConfigError(
                "Configuration invalide:\n" + "\n".join(str(i) for i in errors), issues
            )
        return settings

    def save(self, path: str | os.PathLike[str] | None = None) -> Path:
        """Écrit la configuration en JSON (remplacement atomique)."""
        target = expand_path(path) if path else self.paths.config_file
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_dict()
        payload.pop("paths", None)
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        return target

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)  # type: ignore[return-value]

    # -- runtime ------------------------------------------------------------

    def ensure_directories(self) -> list[Path]:
        """Crée l'arborescence de travail et retourne les dossiers créés."""
        created: list[Path] = []
        p = self.paths
        for directory in (
            p.data_dir,
            p.index_dir,
            p.log_dir,
            p.download_dir,
            p.cache_dir,
            p.report_dir,
            p.browser_profile_dir,
        ):
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True)
                created.append(directory)
        return created

    def summary(self) -> dict[str, Any]:
        """Vue sérialisable pour l'interface (secrets jamais exposés)."""
        return {
            "app": {
                "name": APP_NAME,
                "version": APP_VERSION,
                "frozen": is_frozen(),
                "schema_version": self.schema_version,
            },
            "paths": self.paths.as_dict(),
            "llm": {
                "backend": self.llm.backend,
                "base_url": self.llm.base_url or self.llm.ollama_base_url,
                "model": self.llm.model,
                "reasoning_model": self.llm.reasoning_model,
                "credentials_configured": self.llm.has_credentials(),
                "stream": self.llm.stream,
            },
            "embedding": {
                "backend": self.embedding.backend,
                "model": self.embedding.model,
                "dimension": self.embedding.dimension,
            },
            "browser": {
                "engine": self.browser.engine,
                "channel": self.browser.channel or "bundled",
                "headless": self.browser.headless,
            },
            "search": {
                "engines": list(self.search.engines),
                "max_results_per_query": self.search.max_results_per_query,
            },
            "files": {
                "allowed_roots": list(self.files.allowed_roots),
                "index_extensions": len(self.files.index_extensions),
            },
            "vault": {
                "kdf": self.vault.kdf,
                "exists": self.paths.vault_file.is_file(),
                "auto_lock_minutes": self.vault.auto_lock_minutes,
            },
            "trace": {
                "enabled": self.trace.enabled,
                "content": self.trace.content,
                "max_runs": self.trace.max_runs,
                "max_age_days": self.trace.max_age_days,
                "max_bytes": self.trace.max_bytes,
                "archive": self.trace.archive,
                "encrypt": self.trace.encrypt,
                "per_workspace": self.trace.per_workspace,
                "trace_format": TRACE_FORMAT_VERSION,
            },
            "autonomy": {
                "max_cycles": self.autonomy.max_cycles,
                "interval_minutes": self.autonomy.interval_minutes,
                "goals_file": self.autonomy.goals_file,
                "journal_max_entries": self.autonomy.journal_max_entries,
                "journal_max_bytes": self.autonomy.journal_max_bytes,
                "max_total_tokens": self.autonomy.max_total_tokens,
                "max_tokens_per_cycle": self.autonomy.max_tokens_per_cycle,
                "max_tool_calls_per_cycle": self.autonomy.max_tool_calls_per_cycle,
                "proposal_max_age_days": self.autonomy.proposal_max_age_days,
                "max_total_cost": self.autonomy.max_total_cost,
            },
            "storage": {
                "reports_max_age_days": self.storage.reports_max_age_days,
                "reports_max_bytes": self.storage.reports_max_bytes,
                "cache_max_age_days": self.storage.cache_max_age_days,
                "cache_max_bytes": self.storage.cache_max_bytes,
                "downloads_max_age_days": self.storage.downloads_max_age_days,
                "downloads_max_bytes": self.storage.downloads_max_bytes,
                "browser_cache_max_age_days": self.storage.browser_cache_max_age_days,
                "browser_cache_max_bytes": self.storage.browser_cache_max_bytes,
            },
            "metrics": {
                "enabled": self.metrics.enabled,
                "max_entries": self.metrics.max_entries,
                "max_bytes": self.metrics.max_bytes,
            },
            "agent": {
                "action_first": self.agent.action_first,
                "max_steps": self.agent.max_steps,
                "max_recovery_attempts": self.agent.max_recovery_attempts,
                "deep_research": self.agent.deep_research_enabled,
            },
        }

    # -- validation ---------------------------------------------------------

    def validate(self) -> list[ConfigIssue]:
        """Contrôle de cohérence complet. Retourne erreurs et avertissements."""
        issues: list[ConfigIssue] = []

        def err(field_name: str, message: str) -> None:
            issues.append(ConfigIssue("error", field_name, message))

        def warn(field_name: str, message: str) -> None:
            issues.append(ConfigIssue("warning", field_name, message))

        # --- LLM
        llm = self.llm
        if not llm.model.strip():
            err("llm.model", "aucun modèle de génération défini")
        if not (0.0 <= llm.temperature <= 2.0):
            err("llm.temperature", "doit être comprise entre 0.0 et 2.0")
        if not (0.0 < llm.top_p <= 1.0):
            err("llm.top_p", "doit être comprise entre 0.0 exclu et 1.0")
        if not (128 <= llm.max_output_tokens <= 32768):
            err("llm.max_output_tokens", "doit être comprise entre 128 et 32768")
        if llm.context_window < llm.max_output_tokens:
            err("llm.context_window", "inférieure à max_output_tokens")
        if llm.request_timeout_s < 10:
            err("llm.request_timeout_s", "doit être >= 10 secondes")
        if not (0 <= llm.max_retries <= 10):
            err("llm.max_retries", "doit être comprise entre 0 et 10")
        if llm.backend != "ollama":
            if not llm.base_url.startswith(("http://", "https://")):
                err("llm.base_url", "URL http(s) attendue")
            if not llm.has_credentials():
                warn(
                    "llm.api_key_env",
                    f"variable {llm.api_key_env} absente : l'interface démarrera sans génération",
                )
        elif not llm.ollama_base_url.startswith(("http://", "https://")):
            err("llm.ollama_base_url", "URL http(s) attendue")

        # --- Embeddings
        emb = self.embedding
        if not (64 <= emb.dimension <= 8192):
            err("embedding.dimension", "doit être comprise entre 64 et 8192")
        if emb.backend != "disabled" and not emb.model.strip():
            err("embedding.model", "aucun modèle d'embedding défini")
        if emb.batch_size < 1:
            err("embedding.batch_size", "doit être >= 1")

        # --- Navigateur
        browser = self.browser
        if browser.channel not in BROWSER_CHANNELS:
            warn(
                "browser.channel",
                f"canal inconnu '{browser.channel}' (attendu: {', '.join(c for c in BROWSER_CHANNELS if c)})",
            )
        if browser.action_timeout_ms < 1000:
            err("browser.action_timeout_ms", "doit être >= 1000 ms")
        if browser.navigation_timeout_ms < 1000:
            err("browser.navigation_timeout_ms", "doit être >= 1000 ms")
        if not (320 <= browser.viewport_width <= 7680) or not (200 <= browser.viewport_height <= 4320):
            err("browser.viewport", "dimensions hors bornes (320-7680 x 200-4320)")
        if browser.slow_mo_ms < 0:
            err("browser.slow_mo_ms", "doit être >= 0")
        if browser.max_pages_per_task < 1:
            err("browser.max_pages_per_task", "doit être >= 1")

        # --- Recherche
        search = self.search
        if not search.engines:
            err("search.engines", "au moins un moteur doit être déclaré")
        for engine in search.engines:
            if engine not in SEARCH_ENGINES:
                warn("search.engines", f"moteur inconnu '{engine}'")
        if "searx" in search.engines and not search.searx_instances:
            err("search.searx_instances", "requis quand le moteur 'searx' est actif")
        if not (1 <= search.max_results_per_query <= 50):
            err("search.max_results_per_query", "doit être comprise entre 1 et 50")
        if search.per_host_delay_s < 0:
            err("search.per_host_delay_s", "doit être >= 0")
        if search.max_concurrent_fetches < 1:
            err("search.max_concurrent_fetches", "doit être >= 1")
        if search.min_content_chars < 0:
            err("search.min_content_chars", "doit être >= 0")

        # --- Fichiers
        files = self.files
        if not files.allowed_roots:
            err("files.allowed_roots", "aucun répertoire autorisé : les outils fichiers seraient inopérants")
        seen_roots: set[str] = set()
        for root in files.allowed_roots:
            key = str(root).lower()
            if key in seen_roots:
                warn("files.allowed_roots", f"doublon ignoré '{root}'")
            seen_roots.add(key)
            if not expand_path(root).exists():
                warn("files.allowed_roots", f"répertoire inexistant '{root}'")
        if not files.index_extensions:
            warn("files.index_extensions", "index sémantique désactivé (liste vide)")
        if files.max_read_bytes < 1024:
            err("files.max_read_bytes", "doit être >= 1 Kio")
        if files.max_write_bytes < files.max_read_bytes:
            warn("files.max_write_bytes", "inférieur à max_read_bytes")
        if files.max_index_file_mb < 1:
            err("files.max_index_file_mb", "doit être >= 1")

        # --- Coffre-fort
        vault = self.vault
        if vault.kdf == "argon2id":
            if vault.argon2_memory_kib < 19456:
                err("vault.argon2_memory_kib", "minimum OWASP : 19456 Kio (19 Mio)")
            if vault.argon2_time_cost < 2:
                err("vault.argon2_time_cost", "minimum OWASP : 2 passes")
            if not (1 <= vault.argon2_parallelism <= 8):
                err("vault.argon2_parallelism", "doit être comprise entre 1 et 8")
        else:
            n = vault.scrypt_n
            if n < 16384 or (n & (n - 1)) != 0:
                err("vault.scrypt_n", "doit être une puissance de 2 >= 16384")
            if not (1 <= vault.scrypt_r <= 32):
                err("vault.scrypt_r", "doit être comprise entre 1 et 32")
            if not (1 <= vault.scrypt_p <= 16):
                err("vault.scrypt_p", "doit être comprise entre 1 et 16")
        if not (1 <= vault.auto_lock_minutes <= 480):
            err("vault.auto_lock_minutes", "doit être comprise entre 1 et 480")
        if not (1 <= vault.max_failed_unlocks <= 20):
            err("vault.max_failed_unlocks", "doit être comprise entre 1 et 20")

        # --- Agent
        agent = self.agent
        if not (1 <= agent.max_steps <= 100):
            err("agent.max_steps", "doit être comprise entre 1 et 100")
        if not (1 <= agent.max_recovery_attempts <= 10):
            err("agent.max_recovery_attempts", "doit être comprise entre 1 et 10")
        if not (1 <= agent.parallel_tool_calls <= 8):
            err("agent.parallel_tool_calls", "doit être comprise entre 1 et 8")
        if agent.deep_research_enabled:
            if agent.deep_research_min_queries < 1:
                err("agent.deep_research_min_queries", "doit être >= 1")
            if agent.deep_research_min_queries > agent.deep_research_max_queries:
                err("agent.deep_research_min_queries", "supérieur à deep_research_max_queries")
            if agent.deep_research_pages_per_query < 1:
                err("agent.deep_research_pages_per_query", "doit être >= 1")
            if agent.deep_research_max_pages < agent.deep_research_pages_per_query:
                err("agent.deep_research_max_pages", "inférieur à deep_research_pages_per_query")
            if agent.min_sources_for_report < 1:
                err("agent.min_sources_for_report", "doit être >= 1")
        if agent.tool_output_max_chars < 500:
            err("agent.tool_output_max_chars", "doit être >= 500")
        if agent.max_context_tokens < agent.tool_output_max_chars:
            err("agent.max_context_tokens", "inférieur à tool_output_max_chars")
        if agent.history_max_messages < 2:
            err("agent.history_max_messages", "doit être >= 2")

        # --- Interface
        ui = self.ui
        if not (640 <= ui.window_width <= 3840) or not (480 <= ui.window_height <= 2160):
            err("ui.window", "géométrie hors bornes (640-3840 x 480-2160)")
        if not (8 <= ui.font_size <= 28):
            err("ui.font_size", "doit être comprise entre 8 et 28")
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", ui.accent):
            err("ui.accent", "couleur hexadécimale #RRGGBB attendue")
        if not (16 <= ui.stream_refresh_ms <= 500):
            err("ui.stream_refresh_ms", "doit être comprise entre 16 et 500 ms")

        # --- Journalisation
        if self.logging.level not in LOG_LEVELS:
            err("logging.level", f"niveau inconnu (attendu: {', '.join(LOG_LEVELS)})")
        if self.logging.file_max_bytes < 64 * 1024:
            err("logging.file_max_bytes", "doit être >= 64 Kio")
        if self.logging.backup_count < 1:
            err("logging.backup_count", "doit être >= 1")

        # --- Traces
        trace = self.trace
        if trace.content not in TRACE_CONTENTS:
            err("trace.content", f"valeur inconnue (attendu: {', '.join(TRACE_CONTENTS)})")
        if trace.max_runs < 1:
            err("trace.max_runs", "doit être >= 1")
        if trace.max_age_days < 0:
            err("trace.max_age_days", "doit être >= 0 (0 désactive la limite d'âge)")
        if trace.max_bytes < 0:
            err("trace.max_bytes", "doit être >= 0 (0 désactive le plafond)")
        if trace.fsync_every < 0:
            err("trace.fsync_every", "doit être >= 0 (0 désactive la synchronisation)")
        if trace.active_ttl_s < 1:
            err("trace.active_ttl_s", "doit être >= 1 seconde")
        if trace.archive_min_bytes < 0:
            err("trace.archive_min_bytes", "doit être >= 0 (0 désactive l'archivage immédiat)")
        if not trace.enabled and trace.archive:
            warn("trace.archive", "sans effet : la trace est désactivée")
        if trace.encrypt and not os.environ.get(trace.key_env, "").strip():
            warn(
                "trace.key_env",
                f"variable {trace.key_env} absente : les traces seront désactivées "
                "(aucun repli en clair)",
            )

        # --- Autonomie
        autonomy = self.autonomy
        if not (1 <= autonomy.max_cycles <= 100):
            err("autonomy.max_cycles", "doit être comprise entre 1 et 100")
        if not (0.0 <= autonomy.interval_minutes <= 1440.0):
            err(
                "autonomy.interval_minutes",
                "doit être comprise entre 0 et 1440 minutes (0 = enchaîner)",
            )
        if not (1 <= autonomy.journal_max_entries <= 10000):
            err("autonomy.journal_max_entries", "doit être comprise entre 1 et 10000")
        if autonomy.journal_max_bytes < 1024:
            err("autonomy.journal_max_bytes", "doit être >= 1024 octets")
        for name in ("max_total_tokens", "max_tokens_per_cycle"):
            if not (0 <= int(getattr(autonomy, name)) <= 10_000_000):
                err(f"autonomy.{name}", "doit être comprise entre 0 et 10 000 000")
        if not (0 <= int(autonomy.max_tool_calls_per_cycle) <= 1000):
            err("autonomy.max_tool_calls_per_cycle", "doit être comprise entre 0 et 1000")
        if not (0 <= int(autonomy.proposal_max_age_days) <= 3650):
            err(
                "autonomy.proposal_max_age_days",
                "doit être comprise entre 0 et 3650 jours",
            )
        if not (0.0 <= float(autonomy.max_total_cost) <= 1_000_000.0):
            err("autonomy.max_total_cost", "doit être comprise entre 0 et 1 000 000 USD")

        # --- Rétention des dossiers de travail
        storage = self.storage
        for name in (
            "reports_max_age_days",
            "cache_max_age_days",
            "downloads_max_age_days",
            "browser_cache_max_age_days",
        ):
            value = int(getattr(storage, name))
            if not (0 <= value <= 3650):
                err(f"storage.{name}", "doit être comprise entre 0 et 3650 jours")
        for name in (
            "reports_max_bytes",
            "cache_max_bytes",
            "downloads_max_bytes",
            "browser_cache_max_bytes",
        ):
            if int(getattr(storage, name)) < 0:
                err(f"storage.{name}", "doit être >= 0 (0 = pas de limite de taille)")

        if not (1 <= self.metrics.max_entries <= 100000):
            err("metrics.max_entries", "doit être comprise entre 1 et 100000")
        if self.metrics.max_bytes < 1024:
            err("metrics.max_bytes", "doit être >= 1024 octets")

        return issues


# ---------------------------------------------------------------------------
# Sérialisation / hydratation
# ---------------------------------------------------------------------------


def _dump(value: Any) -> Any:
    """Convertit récursivement dataclasses et chemins en types JSON."""
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _dump(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple, set)):
        return [_dump(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _dump(v) for k, v in value.items()}
    return value


def _hydrate(hint: Any, value: Any) -> Any:
    """Reconstruit une valeur typée depuis sa forme JSON."""
    if value is None:
        return None
    if hint is Path:
        return expand_path(value)
    if is_dataclass(hint):
        return _build_dataclass(hint, value)  # type: ignore[arg-type]
    origin = get_origin(hint)
    if origin in (list, tuple, set):
        args = get_args(hint)
        item_hint = args[0] if args else str
        if not isinstance(value, (list, tuple, set)):
            value = [value]
        return [_hydrate(item_hint, item) for item in value]
    return value


def _build_dataclass(cls: type, data: dict[str, Any]) -> Any:
    """Instancie `cls` en ignorant les clés inconnues du fichier JSON."""
    if not isinstance(data, dict):
        raise ConfigError(f"Section '{cls.__name__}' invalide : objet JSON attendu")
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name in data:
            kwargs[f.name] = _hydrate(hints.get(f.name, Any), data[f.name])
    try:
        return cls(**kwargs)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Section '{cls.__name__}' invalide : {exc}") from exc


# ---------------------------------------------------------------------------
# Variables d'environnement
# ---------------------------------------------------------------------------

_ENV_SKIP_SECTIONS = {"paths"}


def _coerce(raw: str, hint: Any) -> Any:
    """Convertit une chaîne d'environnement vers le type du champ."""
    text = raw.strip()
    origin = get_origin(hint)

    if hint is bool:
        lowered = text.lower()
        if lowered in {"1", "true", "yes", "y", "on", "oui", "vrai"}:
            return True
        if lowered in {"0", "false", "no", "n", "off", "non", "faux", ""}:
            return False
        raise ConfigError(f"booléen attendu, reçu '{raw}'")
    if hint is int:
        return int(text)
    if hint is float:
        return float(text)
    if hint is Path:
        return expand_path(text)
    if origin is Literal:
        allowed = get_args(hint)
        if text not in allowed:
            raise ConfigError(f"valeur '{text}' hors {allowed}")
        return text
    if origin in (list, tuple, set):
        args = get_args(hint)
        item_hint = args[0] if args else str
        if not text:
            return []
        if text.startswith("["):
            items = json.loads(text)
            if not isinstance(items, list):
                raise ConfigError("tableau JSON attendu")
        else:
            items = [part.strip() for part in re.split(r"[;|]", text) if part.strip()]
        return [_coerce(str(item), item_hint) for item in items]
    return text


def _apply_env_overrides(settings: Settings, environ: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Applique `OMNIAGENT_<SECTION>_<CHAMP>` sur les sections concernées."""
    env = os.environ if environ is None else environ
    applied: list[tuple[str, str]] = []
    for section_field in fields(settings):
        if section_field.name in _ENV_SKIP_SECTIONS:
            continue
        section = getattr(settings, section_field.name)
        if not is_dataclass(section):
            continue
        hints = get_type_hints(type(section))
        for f in fields(section):  # type: ignore[arg-type]
            key = f"{ENV_PREFIX}{section_field.name.upper()}_{f.name.upper()}"
            if key not in env:
                continue
            try:
                converted = _coerce(env[key], hints.get(f.name, str))
            except (ConfigError, ValueError) as exc:
                raise ConfigError(f"Variable {key} invalide : {exc}") from exc
            setattr(section, f.name, converted)
            applied.append((key, f"{section_field.name}.{f.name}"))
    return applied


def load_dotenv(path: str | os.PathLike[str]) -> bool:
    """Charge un fichier `.env` minimaliste sans écraser l'environnement réel."""
    target = Path(path)
    if not target.is_file():
        return False
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.lower().startswith("export "):
            stripped = stripped[7:].strip()
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
    return True


# ---------------------------------------------------------------------------
# Journalisation
# ---------------------------------------------------------------------------


class _RunFieldFilter(logging.Filter):
    """Garantit `record.run_id` : un format sans le champ ferait taire le journal."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "run_id", None):
            record.run_id = "-"
        return True


class _RedactingFormatter(logging.Formatter):
    """Applique un nettoyeur de secrets sur chaque ligne formatée."""

    def __init__(
        self,
        fmt: str,
        datefmt: str | None = None,
        redactor: Any = None,
    ) -> None:
        super().__init__(fmt, datefmt)
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return self._redactor(text) if callable(self._redactor) else text


def configure_logging(
    settings: Settings,
    redactor: Any = None,
    *,
    filters: tuple[logging.Filter, ...] = (),
) -> logging.Logger:
    """Installe les handlers console + fichier rotatif sur le logger applicatif.

    `redactor` est une fonction `str -> str` fournie par `security.redaction`
    (Livrable 2) afin de masquer clés API, mots de passe et cookies.
    `filters` reçoit le contexte d'exécution (`core.observability`), qui ajoute
    le `run_id` pour joindre une ligne de journal à une trace.
    """
    logger = logging.getLogger(APP_ID)
    logger.setLevel(settings.logging.level)
    logger.propagate = False

    for handler in list(logger.handlers):
        if getattr(handler, "_omniagent", False):
            logger.removeHandler(handler)
            handler.close()

    fmt = "%(asctime)s | %(levelname)-8s | %(run_id)s | %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    extra_filters = (_RunFieldFilter(), *filters)

    if settings.logging.console:
        stream = logging.StreamHandler(stream=sys.stderr)
        stream.setFormatter(_RedactingFormatter(fmt, datefmt, redactor))
        for item in extra_filters:
            stream.addFilter(item)
        stream._omniagent = True  # type: ignore[attr-defined]
        logger.addHandler(stream)

    try:
        settings.paths.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            settings.paths.log_dir / "omniagent.log",
            maxBytes=settings.logging.file_max_bytes,
            backupCount=settings.logging.backup_count,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(_RedactingFormatter(fmt, datefmt, redactor))
        for item in extra_filters:
            file_handler.addFilter(item)
        file_handler._omniagent = True  # type: ignore[attr-defined]
        logger.addHandler(file_handler)
    except OSError as exc:  # disque plein, droits, etc.
        logger.warning("Journal fichier indisponible: %s", exc)

    return logger


# ---------------------------------------------------------------------------
# Point d'entrée CLI
# ---------------------------------------------------------------------------

_DEFAULT_ENV_EXAMPLE = """\
# OmniAgent Windows — secrets locaux (ne jamais committer ce fichier)
# Copiez .env.example vers .env puis renseignez vos clés.

# Backend cloud (DeepSeek, compatible OpenAI)
DEEPSEEK_API_KEY=
OPENAI_API_KEY=

# Recherche Brave (optionnel, sans clé le moteur est ignoré)
BRAVE_API_KEY=

# Surcharges de configuration : OMNIAGENT_<SECTION>_<CHAMP>
# OMNIAGENT_LLM_BACKEND=ollama
# OMNIAGENT_LLM_MODEL=qwen2.5:14b
# OMNIAGENT_BROWSER_HEADLESS=1
# OMNIAGENT_AGENT_MAX_STEPS=32
"""


def _configure_stdio() -> None:
    """Force UTF-8 sur la console Windows (évite les accents mojibake en cp850)."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):  # flux redirigé ou fermé
                pass


def _print_issues(issues: list[ConfigIssue]) -> None:
    for issue in issues:
        print(str(issue), file=sys.stderr)


def _check_keyring(settings: Settings, *, purge: bool = False) -> int:
    """Contrôle de démarrage : phrase déposée alors que l'option est désactivée.

    L'import est tardif : `config.py` reste utilisable avec la seule bibliothèque
    standard tant que cette commande n'est pas demandée.
    """
    try:
        from tools.vault import Vault
    except ImportError as exc:  # paquets de chiffrement absents
        print(f"[ERREUR] contrôle du trousseau indisponible : {exc}", file=sys.stderr)
        return 1

    vault = Vault(settings)
    status = vault.keyring_status()
    if not status["available"]:
        print("Module 'keyring' absent : aucun contrôle possible.")
        return 0
    if not status["stale"]:
        print("Aucune phrase maîtresse résiduelle dans le Gestionnaire d'identifiants.")
        return 0

    print(
        "Une phrase maîtresse est encore déposée dans le Gestionnaire d'identifiants "
        "alors que vault.keyring_fallback est désactivé."
    )
    if purge:
        removed = vault.purge_stale_keyring()
        print("Entrée supprimée." if removed else "Suppression impossible (droits ou magasin indisponible).")
        return 0 if removed else 1
    if not sys.stdin.isatty():
        print("Purge : python config.py --purge-keyring")
        return 0

    answer = input("La purger maintenant ? [o/N] ").strip().lower()
    if answer in {"o", "oui", "y", "yes"}:
        removed = vault.purge_stale_keyring()
        print("Entrée supprimée." if removed else "Suppression impossible.")
        return 0 if removed else 1
    print("Conservée. Purge ultérieure : python config.py --purge-keyring")
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(
        prog="config.py",
        description=f"{APP_NAME} — gestion de la configuration",
    )
    parser.add_argument("-c", "--config", type=Path, default=None, help="chemin du fichier JSON")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--show", action="store_true", help="affiche la configuration effective")
    group.add_argument("--init", action="store_true", help="crée data/, config.json et .env.example")
    group.add_argument("--validate", action="store_true", help="valide et retourne un code d'erreur")
    group.add_argument("--defaults", action="store_true", help="affiche les valeurs par défaut")
    group.add_argument(
        "--check-keyring",
        action="store_true",
        help="signale (et propose de purger) une phrase maîtresse résiduelle",
    )
    group.add_argument(
        "--purge-keyring",
        action="store_true",
        help="supprime sans confirmation une phrase maîtresse résiduelle",
    )
    args = parser.parse_args(argv)

    if args.defaults:
        payload = _dump(Settings())
        payload.pop("paths", None)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    try:
        settings = Settings.load(args.config, strict=False)
    except ConfigError as exc:
        print(f"[ERREUR] {exc}", file=sys.stderr)
        return 1

    issues = settings.validate()

    if args.init:
        created = settings.ensure_directories()
        saved = settings.save(args.config)
        env_example = settings.paths.install_dir / ".env.example"
        if not env_example.exists():
            env_example.write_text(_DEFAULT_ENV_EXAMPLE, encoding="utf-8")
        print(f"Configuration écrite : {saved}")
        print(f"Dossiers créés       : {len(created)}")
        print(f"Exemple d'env        : {env_example}")
        _print_issues(issues)
        return 0 if not any(i.level == "error" for i in issues) else 1

    if args.show:
        print(json.dumps(settings.summary(), indent=2, ensure_ascii=False))
        _print_issues(issues)
        return 0 if not any(i.level == "error" for i in issues) else 1

    if args.check_keyring or args.purge_keyring:
        return _check_keyring(settings, purge=args.purge_keyring)

    if args.validate:
        _print_issues(issues)
        errors = sum(1 for i in issues if i.level == "error")
        warnings = len(issues) - errors
        print(f"{errors} erreur(s), {warnings} avertissement(s)")
        return 0 if errors == 0 else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
