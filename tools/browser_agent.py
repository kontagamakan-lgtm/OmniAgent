"""Automatisation navigateur Playwright et connexion aux comptes.

Le navigateur est lancé **paresseusement** : aucune dépendance Playwright n'est
importée avant le premier usage, ce qui laisse l'application démarrer même si
les navigateurs ne sont pas encore installés.

Le contexte est *persistant* (`launch_persistent_context`) : cookies, sessions
et stockage local survivent entre deux exécutions, ce qui évite de se
réauthentifier à chaque tâche.

Les identifiants proviennent exclusivement du coffre-fort (`Vault.credentials`).
Ils sont injectés directement dans le DOM par Playwright et ne sont **jamais**
retournés au modèle ni écrits dans un journal.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

from config import APP_ID, Settings
from tools.base import FunctionTool, Tool, ToolError, ToolResult, object_schema, param
from tools.file_manager import FileManager, app_file_manager
from tools.vault import Vault, normalize_service, totp_code
from tools.web_search import extract_content

__all__ = [
    "BrowserAgent",
    "BrowserError",
    "BrowserUnavailable",
    "LoginError",
    "NavigationError",
    "SelectorError",
    "build_browser_tools",
    "ensure_system_node",
]

logger = logging.getLogger(f"{APP_ID}.browser")


def driver_node_available(driver_dir: Path | None = None) -> bool:
    """Vrai si le Node.js du pilote Playwright est présent dans le paquet.

    Les trois recettes (`OmniAgent.spec`, `-lite`, `-slim`) le retirent : le
    paquet ne pèse plus ~95 Mio de plus, et Playwright utilise le Node.js du
    système. Cette fonction sert à distinguer « Node fourni » de « aucun Node ».
    """
    if driver_dir is None:
        try:
            import playwright
        except ImportError:  # Playwright absent : aucun pilote à chercher
            return False
        driver_dir = Path(playwright.__file__).parent / "driver"
    return (driver_dir / "node.exe").is_file() or (driver_dir / "node").is_file()


def ensure_browsers_path() -> str:
    """Fait chercher à Playwright ses navigateurs dans le cache utilisateur.

    Dans un exe PyInstaller, le pilote vit sous `_internal/playwright/driver` :
    Playwright en déduit un cache local `driver/package/.local-browsers`, vide,
    au lieu de `%LOCALAPPDATA%\\ms-playwright` utilisé par les installations
    pip. On force donc `PLAYWRIGHT_BROWSERS_PATH` vers le cache de la machine,
    sans jamais écraser une valeur déjà définie.

    Renvoie le chemin forcé, ou ``""`` si rien n'a été changé.
    """
    current = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if current:
        return current
    if not getattr(sys, "frozen", False):
        return ""
    root = playwright_browsers_root()
    if not root.is_dir():
        return ""
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root)
    return str(root)


def ensure_system_node(driver_dir: Path | None = None) -> str:
    """Fait piloter Playwright par le Node.js du système quand il en manque un.

    Aucun paquet livré n'embarque ``playwright/driver/node.exe``. Playwright
    accepte alors l'exécutable désigné par ``PLAYWRIGHT_NODEJS_PATH`` ; on le
    renseigne avec le ``node`` du PATH. Le pilote embarqué reste prioritaire :
    la variable n'est posée que lorsqu'il est absent, et jamais si elle l'est
    déjà.

    Renvoie le chemin utilisé, ou ``""`` si le pilote embarqué assure le travail
    (ou si aucun Node.js n'est disponible : `start()` lève alors une erreur
    explicite).
    """
    current = os.environ.get("PLAYWRIGHT_NODEJS_PATH", "")
    if current:
        return current
    if driver_node_available(driver_dir):
        return ""
    node = shutil.which("node")
    if node:
        os.environ["PLAYWRIGHT_NODEJS_PATH"] = node
    return node or ""

#: Répertoires de cache où Playwright installe ses navigateurs.
_BUNDLED_DIR_PREFIXES = ("chromium-", "chromium_headless_shell-")
#: Exécutables des navigateurs système utilisables comme canal Playwright.
_SYSTEM_BROWSERS: tuple[tuple[str, str], ...] = (
    ("msedge", r"Microsoft\Edge\Application\msedge.exe"),
    ("chrome", r"Google\Chrome\Application\chrome.exe"),
)


def playwright_browsers_root() -> Path:
    """Répertoire où Playwright cherche ses navigateurs installés."""
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if custom and custom != "0":
        return Path(custom)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def bundled_browser_available() -> bool:
    """Vrai si un Chromium livré par Playwright est installé localement.

    Le paquet « slim » n'embarque aucun navigateur : sur une cible qui n'a pas
    lancé `python -m playwright install chromium`, il n'y a rien à démarrer.
    """
    root = playwright_browsers_root()
    try:
        return any(
            entry.is_dir() and entry.name.startswith(_BUNDLED_DIR_PREFIXES)
            for entry in root.iterdir()
        )
    except OSError:
        return False


def system_browser_channel() -> str:
    """Canal Playwright du navigateur système disponible (`msedge`, `chrome`)."""
    if os.name != "nt":
        return ""
    roots = (
        os.environ.get("ProgramFiles(x86)", "").strip(),
        os.environ.get("ProgramFiles", "").strip(),
        os.environ.get("LOCALAPPDATA", "").strip(),
    )
    for channel, relative in _SYSTEM_BROWSERS:
        for root in roots:
            if root and (Path(root) / relative).is_file():
                return channel
    return ""


def resolved_channel(config: Any) -> str:
    """Canal à transmettre à Playwright, avec repli sur le navigateur système.

    `browser.channel` vide ou `"bundled"` signifie « le navigateur fourni par
    Playwright ». Quand celui-ci n'est pas installé, les navigateurs de la
    machine prennent le relais sans édition manuelle de `config.json`.

    Lève `BrowserUnavailable` si aucun navigateur n'existe, avec les deux
    remèdes possibles, plutôt que de laisser échouer le lancement.
    """
    channel = str(getattr(config, "channel", "") or "").strip()
    if channel.lower() == "bundled":
        channel = ""
    if channel or str(getattr(config, "engine", "")) != "chromium":
        return channel
    if bundled_browser_available():
        return ""
    fallback = system_browser_channel()
    if fallback:
        logger.info("navigateur Playwright absent : repli sur le canal système %s", fallback)
        return fallback
    raise BrowserUnavailable(
        "aucun navigateur disponible : installez Microsoft Edge ou Google Chrome, "
        "ou exécutez « python -m playwright install chromium »"
    )


USERNAME_SELECTORS: tuple[str, ...] = (
    'input[autocomplete="username"]',
    'input[type="email"]',
    'input[name*="user" i]',
    'input[id*="user" i]',
    'input[name*="login" i]',
    'input[id*="login" i]',
    'input[name*="email" i]',
    'input[type="text"]',
)
PASSWORD_SELECTORS: tuple[str, ...] = (
    'input[type="password"]',
    'input[name*="pass" i]',
    'input[id*="pass" i]',
)
SUBMIT_SELECTORS: tuple[str, ...] = (
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Se connecter")',
    'button:has-text("Connexion")',
    'button:has-text("Sign in")',
    'button:has-text("Log in")',
    'button:has-text("Valider")',
    "form button",
)
OTP_SELECTORS: tuple[str, ...] = (
    'input[autocomplete="one-time-code"]',
    'input[name*="otp" i]',
    'input[name*="totp" i]',
    'input[id*="otp" i]',
    'input[name*="code" i]',
)
_ERROR_MARKERS = (
    "incorrect", "invalide", "invalid", "identifiant ou mot de passe",
    "mot de passe oublié", "échec de la connexion", "login failed",
    "authentication failed", "wrong password", "erreur d'authentification",
)
_SIGNUP_ERROR_MARKERS = (
    "déjà utilisé", "déjà utilisée", "déjà pris", "deja utilise", "existe déjà",
    "existe deja", "already exists", "already registered", "already in use",
    "already taken", "email existant", "adresse e-mail invalide",
    "mot de passe trop faible", "password too weak", "passwords do not match",
    "les mots de passe ne correspondent pas", "captcha", "inscription refusée",
    "erreur", "échec", "echec", "failed",
)
_SIGNUP_SUCCESS_MARKERS = (
    "compte créé", "compte cree", "compte est créé", "bienvenue", "welcome",
    "inscription réussie", "inscription reussie", "félicitations", "felicitations",
    "vérifiez votre", "verifiez votre", "confirmez votre adresse",
    "verify your email", "check your email", "account created",
    "registration complete", "successfully registered", "inscription terminée",
)


class BrowserError(ToolError):
    """Erreur d'automatisation navigateur."""

    code = "browser_error"


class BrowserUnavailable(BrowserError):
    code = "browser_unavailable"


class NavigationError(BrowserError):
    code = "navigation_failed"


class SelectorError(BrowserError):
    code = "selector_failed"


class LoginError(BrowserError):
    code = "login_failed"


@dataclass(slots=True)
class BrowserStatus:
    running: bool
    engine: str = ""
    channel: str = ""
    headless: bool = False
    current_url: str = ""
    title: str = ""
    pages: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "engine": self.engine,
            "channel": self.channel,
            "headless": self.headless,
            "current_url": self.current_url,
            "title": self.title,
            "pages": self.pages,
        }


class BrowserAgent:
    """Pilote Playwright : navigation, formulaires, connexion, cookies."""

    def __init__(
        self,
        settings: Settings,
        vault: Vault | None = None,
        *,
        file_manager: FileManager | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.vault = vault
        self.logger = logger_ or logger
        self._file_manager = file_manager
        self._lock = threading.RLock()
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._channel = ""
        self._console_errors: list[str] = []

    # -- cycle de vie -------------------------------------------------------

    @property
    def file_manager(self) -> FileManager:
        """Gestionnaire de fichiers partagé, utilisé pour confiner les écritures."""
        if self._file_manager is None:
            self._file_manager = app_file_manager(self.settings, self.logger)
        return self._file_manager

    @property
    def running(self) -> bool:
        return self._context is not None

    def _import(self) -> Any:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - dépend de l'installation
            raise BrowserUnavailable(
                "Playwright est absent : exécutez « pip install playwright » "
                "puis « python -m playwright install chromium »"
            ) from exc
        return sync_playwright

    def start(self, *, headless: bool | None = None) -> "BrowserAgent":
        """Lance le contexte persistant si nécessaire."""
        with self._lock:
            if self._context is not None:
                return self
            config = self.settings.browser
            # Résolu avant l'import : sans navigateur, Playwright est inutile et
            # le paquet slim doit échouer avec un message actionnable.
            channel = resolved_channel(config)
            self._channel = channel
            browsers_path = ensure_browsers_path()
            if browsers_path:
                logger.debug("Navigateurs Playwright dans %s", browsers_path)
            sync_playwright = self._import()
            node = ensure_system_node()
            if node:
                logger.debug("Playwright pilote par le Node.js du systeme : %s", node)
            elif not driver_node_available():
                # Aucun paquet n'embarque plus Node : sans Node système il n'y a
                # pas de pilote, et l'erreur brute de Playwright serait opaque.
                raise BrowserUnavailable(
                    "Node.js introuvable : installez Node.js 18 ou plus récent "
                    "(nodejs.org) ou renseignez PLAYWRIGHT_NODEJS_PATH ; "
                    "les outils browser_* en ont besoin."
                )
            profile = self.settings.paths.browser_profile_dir
            profile.mkdir(parents=True, exist_ok=True)
            self.settings.paths.download_dir.mkdir(parents=True, exist_ok=True)

            try:
                self._playwright = sync_playwright().start()
                engine = getattr(self._playwright, config.engine)
                options: dict[str, Any] = {
                    "headless": config.headless if headless is None else headless,
                    "slow_mo": config.slow_mo_ms,
                    "locale": config.locale,
                    "timezone_id": config.timezone,
                    "viewport": {"width": config.viewport_width, "height": config.viewport_height},
                    "ignore_https_errors": config.ignore_https_errors,
                    "accept_downloads": config.download_allowed,
                    "downloads_path": str(self.settings.paths.download_dir),
                }
                if channel:
                    options["channel"] = channel
                if config.user_agent:
                    options["user_agent"] = config.user_agent
                if config.proxy_server:
                    options["proxy"] = {"server": config.proxy_server}

                self._context = engine.launch_persistent_context(str(profile), **options)
                self._context.set_default_timeout(config.action_timeout_ms)
                self._context.set_default_navigation_timeout(config.navigation_timeout_ms)
                self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
                self._install_handlers(self._page)
            except Exception as exc:  # noqa: BLE001 - message d'installation explicite
                self.stop()
                message = str(exc)
                if "Executable doesn't exist" in message or "playwright install" in message:
                    raise BrowserUnavailable(
                        "navigateur non installé : exécutez « python -m playwright install chromium »"
                    ) from exc
                raise BrowserUnavailable(f"démarrage du navigateur impossible : {message}") from exc
            return self

    def _install_handlers(self, page: Any) -> None:
        try:
            page.on("dialog", lambda dialog: dialog.dismiss())
            page.on("pageerror", lambda error: self._console_errors.append(str(error)[:300]))
        except Exception:  # noqa: BLE001 - handlers best effort
            pass
        self._console_errors = self._console_errors[-20:]

    def stop(self) -> None:
        with self._lock:
            if self._context is not None:
                try:
                    self._context.close()
                except Exception:  # noqa: BLE001
                    pass
                self._context = None
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._playwright = None
            self._page = None
            self._channel = ""

    def __enter__(self) -> "BrowserAgent":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- accès page ---------------------------------------------------------

    def _ensure_page(self) -> Any:
        if self._context is None or self._page is None:
            self.start()
        if self._page is None:  # pragma: no cover - défense
            raise BrowserError("aucune page active")
        return self._page

    @property
    def page(self) -> Any:
        return self._ensure_page()

    def status(self) -> BrowserStatus:
        if not self.running:
            return BrowserStatus(running=False, engine=self.settings.browser.engine)
        try:
            return BrowserStatus(
                running=True,
                engine=self.settings.browser.engine,
                channel=self._channel or "bundled",
                headless=self.settings.browser.headless,
                current_url=self._page.url if self._page else "",
                title=self._page.title() if self._page else "",
                pages=len(self._context.pages) if self._context else 0,
            )
        except Exception:  # noqa: BLE001 - page fermée entre-temps
            return BrowserStatus(running=False, engine=self.settings.browser.engine)

    # -- navigation ---------------------------------------------------------

    def goto(self, url: str, *, wait_until: str = "domcontentloaded", timeout: int | None = None) -> dict[str, Any]:
        with self._lock:
            page = self._ensure_page()
            raw = (url or "").strip()
            if not raw:
                raise NavigationError("URL vide")
            parsed = urlparse(raw)
            target = raw if parsed.scheme else f"https://{raw}"
            parsed = urlparse(target)
            # `file:` et `data:` sont volontairement exclus : `browser_text()`
            # lirait alors n'importe quel fichier local hors de `files.allowed_roots`.
            if parsed.scheme not in {"http", "https", "about"}:
                raise NavigationError(f"schéma non autorisé : {parsed.scheme}")
            try:
                response = page.goto(target, wait_until=wait_until, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                raise NavigationError(f"navigation impossible vers {target} : {exc}") from exc
            try:
                page.wait_for_load_state("domcontentloaded", timeout=timeout or self.settings.browser.navigation_timeout_ms)
            except Exception:  # noqa: BLE001 - page lente mais utilisable
                pass
            return {
                "url": page.url,
                "status": getattr(response, "status", 0) or 0,
                "title": self._safe_title(page),
            }

    @staticmethod
    def _safe_title(page: Any) -> str:
        try:
            return page.title()
        except Exception:  # noqa: BLE001
            return ""

    def text(self, *, max_chars: int = 20000) -> dict[str, Any]:
        with self._lock:
            page = self._ensure_page()
            try:
                body = page.inner_text("body")
            except Exception as exc:  # noqa: BLE001
                raise BrowserError(f"lecture du texte impossible : {exc}") from exc
            truncated = bool(max_chars and len(body) > max_chars)
            return {
                "url": page.url,
                "title": self._safe_title(page),
                "chars": len(body),
                "truncated": truncated,
                "text": body[:max_chars] if max_chars else body,
            }

    def fetch_rendered(self, url: str, *, max_chars: int = 20000, wait_selector: str = "") -> dict[str, Any]:
        """Récupère une page après exécution du JavaScript, puis en extrait le contenu."""
        with self._lock:
            opened = self.goto(url)
            page = self._ensure_page()
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=self.settings.browser.action_timeout_ms)
                except Exception as exc:  # noqa: BLE001
                    raise SelectorError(f"sélecteur absent après chargement : {wait_selector} ({exc})") from exc
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001 - pages à flux continu
                pass
            try:
                html = page.content()
            except Exception as exc:  # noqa: BLE001
                raise BrowserError(f"récupération du DOM impossible : {exc}") from exc
            extracted = extract_content(html, page.url)
            text = extracted["text"] or self.text(max_chars=max_chars)["text"]
            truncated = bool(max_chars and len(text) > max_chars)
            return {
                "url": page.url,
                "status": opened.get("status", 0),
                "title": extracted["title"] or self._safe_title(page),
                "text": text[:max_chars] if max_chars else text,
                "word_count": len(text.split()),
                "truncated": truncated,
            }

    def html(self, *, max_chars: int = 50000) -> str:
        with self._lock:
            page = self._ensure_page()
            content = page.content()
            return content[:max_chars] if max_chars else content

    # -- interactions -------------------------------------------------------

    @staticmethod
    def _check_selector(selector: str) -> str:
        text = (selector or "").strip()
        if not text:
            raise SelectorError("sélecteur vide")
        if len(text) > 500:
            raise SelectorError("sélecteur trop long (500 caractères maximum)")
        return text

    def _first_visible(self, selectors: Iterable[str], *, scope: str = "") -> Any:
        page = self._ensure_page()
        for selector in selectors:
            full = f"{scope} {selector}".strip() if scope else selector
            try:
                locator = page.locator(full)
                count = min(locator.count(), 5)
            except Exception:  # noqa: BLE001 - sélecteur invalide pour ce DOM
                continue
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if candidate.is_visible():
                        return candidate
                except Exception:  # noqa: BLE001
                    continue
        return None

    def fill_form(
        self,
        selector: str = "",
        data: dict[str, Any] | None = None,
        *,
        submit: bool = False,
    ) -> dict[str, Any]:
        """Remplit des champs. `selector` cible le conteneur (formulaire), `data` associe sélecteur → valeur."""
        with self._lock:
            page = self._ensure_page()
            scope = self._check_selector(selector) if selector else ""
            fields = data or {}
            if not isinstance(fields, dict) or not fields:
                raise SelectorError("aucun champ à remplir")
            if len(fields) > 40:
                raise SelectorError("trop de champs simultanés (40 maximum)")

            filled: list[str] = []
            errors: list[dict[str, str]] = []
            for field_selector, value in fields.items():
                target = self._check_selector(str(field_selector))
                full = f"{scope} {target}".strip() if scope else target
                try:
                    locator = page.locator(full).first
                    if not locator.is_visible():
                        errors.append({"selector": target, "error": "champ non visible"})
                        continue
                    if locator.evaluate("el => el.tagName.toLowerCase()") == "select":
                        locator.select_option(str(value))
                    else:
                        locator.fill(str(value))
                    filled.append(target)
                except Exception as exc:  # noqa: BLE001
                    errors.append({"selector": target, "error": str(exc)[:200]})

            submitted = False
            if submit:
                submitted = self._submit(scope)

            return {
                "url": page.url,
                "filled": filled,
                "errors": errors,
                "submitted": submitted,
            }

    def _submit(self, scope: str = "") -> bool:
        page = self._ensure_page()
        button = self._first_visible(SUBMIT_SELECTORS, scope=scope)
        try:
            if button is not None:
                button.click()
            else:
                page.keyboard.press("Enter")
        except Exception as exc:  # noqa: BLE001
            raise SelectorError(f"soumission impossible : {exc}") from exc
        self._wait_after_submit()
        return True

    def _wait_after_submit(self) -> None:
        page = self._ensure_page()
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001 - page qui reste active (SPA)
            time.sleep(0.5)

    def click_element(self, selector: str, *, index: int = 0, wait_after_ms: int = 0) -> dict[str, Any]:
        with self._lock:
            page = self._ensure_page()
            target = self._check_selector(selector)
            try:
                locator = page.locator(target)
                count = locator.count()
            except Exception as exc:  # noqa: BLE001
                raise SelectorError(f"sélecteur invalide : {exc}") from exc
            if count == 0:
                raise SelectorError(f"aucun élément ne correspond à « {target} »")
            if index >= count:
                raise SelectorError(f"index {index} hors bornes ({count} éléments)")
            try:
                locator.nth(index).click()
            except Exception as exc:  # noqa: BLE001
                raise SelectorError(f"clic impossible : {exc}") from exc
            if wait_after_ms:
                page.wait_for_timeout(wait_after_ms)
            return {"url": page.url, "clicked": target, "index": index, "matches": count}

    def press_key(self, key: str) -> dict[str, Any]:
        with self._lock:
            page = self._ensure_page()
            page.keyboard.press(key)
            return {"ok": True, "key": key}

    def wait_for_selector(self, selector: str, *, timeout_ms: int = 10000) -> dict[str, Any]:
        with self._lock:
            page = self._ensure_page()
            target = self._check_selector(selector)
            try:
                page.wait_for_selector(target, timeout=timeout_ms)
            except Exception as exc:  # noqa: BLE001
                raise SelectorError(f"sélecteur jamais apparu : {target}") from exc
            return {"ok": True, "selector": target}

    def extract_links(self, *, selector: str = "a[href]", max_links: int = 200) -> dict[str, Any]:
        with self._lock:
            page = self._ensure_page()
            target = self._check_selector(selector)
            try:
                locator = page.locator(target)
                count = min(locator.count(), max_links)
            except Exception as exc:  # noqa: BLE001
                raise SelectorError(f"sélecteur invalide : {exc}") from exc
            base_url = page.url
            links: list[dict[str, str]] = []
            for index in range(count):
                try:
                    item = locator.nth(index)
                    href = item.get_attribute("href") or ""
                    if not href or href.startswith(("javascript:", "#", "mailto:", "tel:")):
                        continue
                    links.append(
                        {
                            "text": (item.inner_text() or "").strip()[:200],
                            "url": urljoin(base_url, href),
                        }
                    )
                except Exception:  # noqa: BLE001 - lien détaché du DOM
                    continue
            return {"page": base_url, "count": len(links), "links": links}

    # -- création de compte --------------------------------------------------

    def _password_fields(self, limit: int = 4) -> list[Any]:
        """Champs mot de passe visibles, dans l'ordre du DOM."""
        page = self._ensure_page()
        try:
            locator = page.locator('input[type="password"]')
            count = min(locator.count(), limit)
        except Exception:  # noqa: BLE001 - DOM sans champ mot de passe
            return []
        fields: list[Any] = []
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible():
                    fields.append(candidate)
            except Exception:  # noqa: BLE001 - élément détaché du DOM
                continue
        return fields

    def fill_signup(
        self,
        service_name: str,
        username: str = "",
        *,
        url: str = "",
        submit: bool = False,
        fill_username: bool = True,
        login_url: str = "",
    ) -> dict[str, Any]:
        """Remplit un formulaire d'inscription depuis le coffre-fort.

        Le mot de passe et sa confirmation reçoivent la valeur stockée sans
        jamais passer par la conversation : la valeur n'apparaît pas dans le
        résultat, seul le compte rendu des champs remplis remonte.

        `login_url` désigne la page de connexion à utiliser pour vérifier le
        compte ; à défaut, le champ `login_url` de l'entrée du coffre est pris.

        La lecture des identifiants se fait avec `count_use=False` : remplir un
        formulaire d'inscription n'authentifie personne, ce n'est donc pas un
        usage réel du secret. Seule une connexion réussie incrémente
        `use_count` (voir `login_to_service`).
        """
        if self.vault is None:
            raise LoginError("aucun coffre-fort associé à l'agent navigateur")

        credentials = self.vault.credentials(service_name, username or None, count_use=False)
        target = (url or credentials.url or "").strip()
        if target and "://" not in target:
            target = f"https://{target}"

        with self._lock:
            if target:
                self.goto(target)
            page = self._ensure_page()

            filled: list[str] = []
            if fill_username and credentials.username:
                user_field = self._first_visible(USERNAME_SELECTORS)
                if user_field is not None:
                    user_field.fill(credentials.username)
                    filled.append("username")

            fields = self._password_fields()
            if not fields:
                raise LoginError("aucun champ de mot de passe détecté sur la page")
            for field in fields:
                field.fill(credentials.password)
            filled.append("password")
            filled.extend("password_confirm" for _ in fields[1:])

            submitted = False
            verdict: dict[str, Any] = {
                "status": "not_submitted",
                "created": False,
                "evidence": "formulaire rempli sans soumission",
                "url_changed": False,
                "title": self._safe_title(page),
            }
            if submit:
                before_url = page.url
                submitted = self._submit()
                if submitted:
                    self._wait_after_submit()
                    verdict = self._detect_signup_state(page, before_url)
                    self._record_signup(credentials.service, verdict)

            result = {
                "service": credentials.service,
                "username": credentials.username,
                "url": page.url,
                "filled": filled,
                "password_fields": len(fields),
                "submitted": submitted,
                "created": verdict["created"],
                "signup_status": verdict["status"],
                "evidence": verdict["evidence"],
                "url_changed": verdict["url_changed"],
                "title": verdict["title"],
            }
            signup_url = page.url

        # Hors du verrou : `login_to_service` le reprend lui-même.
        # Le verdict de la page ne prouve rien : seule une connexion réussie
        # avec les identifiants du coffre confirme que le compte existe.
        if verdict["created"]:
            result.update(
                self._confirm_by_login(
                    credentials.service, signup_url=signup_url, login_url=login_url
                )
            )
        else:
            result.update(
                {
                    "verified": False,
                    "verification": "compte non créé : aucune connexion de contrôle",
                }
            )
        return result

    # -- connexion ----------------------------------------------------------

    def login_to_service(
        self,
        service_name: str,
        username: str = "",
        *,
        url: str = "",
        submit: bool = True,
        count_use: bool = True,
    ) -> dict[str, Any]:
        """Connexion automatique avec les identifiants du coffre-fort.

        Le mot de passe est lu depuis le coffre, injecté dans le DOM, et n'est
        jamais inclus dans la valeur de retour ni dans un message d'erreur.

        `count_use=False` réserve la connexion au contrôle de compte : elle
        n'incrémente pas `use_count` et n'écrit rien dans l'audit. Avec la
        valeur par défaut, l'usage n'est compté qu'en cas de succès — une
        tentative refusée n'est pas un usage réel des identifiants.

        Page ouverte, par ordre de priorité : `url` fournie par l'appelant,
        puis `login_url` de l'entrée, puis son `url`, puis l'origine déduite du
        nom du service. Une entrée qui ne stocke que sa page de connexion
        fonctionne donc sans que l'appelant ait à la répéter.
        """
        if self.vault is None:
            raise LoginError("aucun coffre-fort associé à l'agent navigateur")

        credentials = self.vault.credentials(service_name, username or None, count_use=False)
        target = (url or credentials.login_url or credentials.url or "").strip()
        if not target:
            host = normalize_service(credentials.service)
            if not host:
                raise LoginError(f"impossible de déduire une URL pour « {service_name} »")
            target = f"https://{host}"
        if "://" not in target:
            target = f"https://{target}"

        with self._lock:
            opened = self.goto(target)
            page = self._ensure_page()

            user_field = self._first_visible(USERNAME_SELECTORS)
            if user_field is None:
                raise LoginError(
                    "aucun champ d'identifiant détecté : la page est peut-être déjà connectée "
                    "ou utilise un formulaire non standard"
                )
            try:
                user_field.fill(credentials.username)
            except Exception as exc:  # noqa: BLE001
                raise LoginError(f"remplissage de l'identifiant impossible : {exc}") from exc

            password_field = self._first_visible(PASSWORD_SELECTORS)
            if password_field is None:
                raise LoginError("aucun champ de mot de passe détecté sur la page")
            try:
                password_field.fill(credentials.password)
            except Exception as exc:  # noqa: BLE001
                raise LoginError(f"remplissage du mot de passe impossible : {exc}") from exc

            submitted = False
            if submit:
                if password_field is not None:
                    try:
                        if self._first_visible(SUBMIT_SELECTORS) is not None:
                            self._submit()
                        else:
                            page.keyboard.press("Enter")
                            self._wait_after_submit()
                        submitted = True
                    except Exception as exc:  # noqa: BLE001
                        raise LoginError(f"soumission du formulaire impossible : {exc}") from exc

            totp_used = False
            if credentials.totp_secret:
                otp_field = self._first_visible(OTP_SELECTORS)
                if otp_field is not None:
                    try:
                        otp_field.fill(totp_code(credentials.totp_secret))
                        self._submit()
                        totp_used = True
                    except Exception as exc:  # noqa: BLE001
                        raise LoginError(f"saisie du code 2FA impossible : {exc}") from exc

            verdict = self._detect_login_state(page)
            if count_use and verdict["logged_in"]:
                try:
                    self.vault.record_use(credentials.entry_id)
                except Exception:  # noqa: BLE001 - comptage best effort
                    self.logger.warning("comptage de l'usage impossible", exc_info=True)
            return {
                "service": credentials.service,
                "url": page.url,
                "username": credentials.username,
                "submitted": submitted,
                "totp_used": totp_used,
                "logged_in": verdict["logged_in"],
                "evidence": verdict["evidence"],
                "title": self._safe_title(page),
                "opened_status": opened.get("status", 0),
            }

    def _detect_login_state(self, page: Any) -> dict[str, Any]:
        password_still_there = self._first_visible(PASSWORD_SELECTORS) is not None
        snippet = ""
        try:
            snippet = (page.inner_text("body") or "")[:6000].lower()
        except Exception:  # noqa: BLE001
            snippet = ""
        markers = [marker for marker in _ERROR_MARKERS if marker in snippet]
        logged_in = not password_still_there and not markers
        if password_still_there:
            evidence = "le formulaire de connexion est toujours affiché"
        elif markers:
            evidence = f"message d'erreur détecté : {markers[0]!r}"
        else:
            evidence = "formulaire de connexion absent après soumission"
        return {"logged_in": logged_in, "evidence": evidence}

    def _record_signup(self, service: str, verdict: dict[str, Any]) -> None:
        """Journalise le verdict d'inscription, sans rien vérifier.

        Le journal ne doit jamais faire échouer l'action : une erreur d'écriture
        est consignée puis ignorée. Le marquage `verified_at` n'est **pas** ici :
        il dépend d'une connexion de contrôle (`_confirm_by_login`).
        """
        if self.vault is None:
            return
        try:
            self.vault.record_signup(
                service,
                created=bool(verdict.get("created")),
                status=str(verdict.get("status", "unknown")),
                evidence=str(verdict.get("evidence", "")),
            )
        except Exception:  # noqa: BLE001 - journalisation best effort
            self.logger.warning("journalisation de l'inscription impossible", exc_info=True)

    def _confirm_by_login(
        self, service: str, *, signup_url: str = "", login_url: str = ""
    ) -> dict[str, Any]:
        """Confirme le compte par une vraie connexion, puis marque l'entrée.

        Tant que l'authentification échoue, l'entrée reste non vérifiée : un
        message « compte créé » peut venir d'une page trompeuse ou d'un site
        qui affiche la confirmation avant de valider l'adresse.

        Les pages essayées vont de la plus fiable à la plus devinée : URL de
        connexion fournie par l'appelant, puis `login_url` du coffre, puis
        « origine + /login », puis la page d'inscription elle-même.
        """
        candidates: list[str] = []
        stored = ""
        if self.vault is not None:
            entry = self.vault.find(service)
            stored = entry.login_url if entry is not None else ""
        for explicit in (login_url, stored):
            candidate = str(explicit or "").strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        parsed = urlparse(signup_url or "")
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            candidates.append(f"{parsed.scheme}://{parsed.netloc}/login")
        if signup_url:
            candidates.append(signup_url)

        verification = "aucune page de connexion essayée"
        for candidate in candidates:
            try:
                attempt = self.login_to_service(
                    service, url=candidate, submit=True, count_use=False
                )
            except Exception as exc:  # noqa: BLE001 - tentative suivante possible
                verification = f"{candidate} : {exc}"
                continue
            if attempt.get("logged_in"):
                if self.vault is not None:
                    try:
                        self.vault.mark_verified(service)
                    except Exception:  # noqa: BLE001
                        self.logger.warning("marquage vérifié impossible", exc_info=True)
                return {
                    "verified": True,
                    "verification": f"connexion confirmée sur {candidate}",
                }
            verification = f"{candidate} : {attempt.get('evidence', 'connexion non confirmée')}"
        self.logger.info("compte non confirmé pour %s (%s)", service, verification)
        return {"verified": False, "verification": verification}

    def _detect_signup_state(self, page: Any, before_url: str) -> dict[str, Any]:
        """Interprète la page après soumission d'une inscription.

        Même logique que `_detect_login_state` : l'absence de formulaire ne
        suffit pas, un message de confirmation est exigé ; un message d'erreur
        l'emporte toujours.
        """
        password_still_there = self._first_visible(PASSWORD_SELECTORS) is not None
        snippet = ""
        try:
            snippet = (page.inner_text("body") or "")[:6000].lower()
        except Exception:  # noqa: BLE001
            snippet = ""
        errors = [marker for marker in _SIGNUP_ERROR_MARKERS if marker in snippet]
        successes = [marker for marker in _SIGNUP_SUCCESS_MARKERS if marker in snippet]
        moved = bool(before_url) and page.url != before_url

        if errors and not successes:
            status = "rejected"
            evidence = f"message d'erreur détecté : {errors[0]!r}"
        elif successes:
            status = "created"
            evidence = f"confirmation détectée : {successes[0]!r}"
        elif password_still_there:
            status = "rejected"
            evidence = "le formulaire d'inscription est toujours affiché après soumission"
        elif moved:
            status = "unknown"
            evidence = "page changée mais aucune confirmation explicite"
        else:
            status = "unknown"
            evidence = "aucun signe de création ni d'échec détecté"
        return {
            "status": status,
            "created": status == "created",
            "evidence": evidence,
            "url_changed": moved,
            "title": self._safe_title(page),
        }

    # -- cookies / captures -------------------------------------------------

    def extract_cookies(self, domain: str = "") -> dict[str, Any]:
        with self._lock:
            self._ensure_page()
            if self._context is None:
                raise BrowserError("navigateur non démarré")
            try:
                raw = self._context.cookies()
            except Exception as exc:  # noqa: BLE001
                raise BrowserError(f"lecture des cookies impossible : {exc}") from exc
            needle = (domain or "").strip().lower()
            cookies = [
                {
                    "name": cookie.get("name", ""),
                    "value": cookie.get("value", ""),
                    "domain": cookie.get("domain", ""),
                    "path": cookie.get("path", "/"),
                    "expires": cookie.get("expires"),
                    "httpOnly": cookie.get("httpOnly", False),
                    "secure": cookie.get("secure", False),
                    "sameSite": cookie.get("sameSite", ""),
                }
                for cookie in raw
                if not needle or needle in str(cookie.get("domain", "")).lower()
            ]
            return {"count": len(cookies), "cookies": cookies}

    def screenshot(self, path: str = "", *, full_page: bool = True) -> dict[str, Any]:
        with self._lock:
            page = self._ensure_page()
            screens_dir = (self.settings.paths.cache_dir / "screens").resolve()
            if path:
                candidate = Path(path).expanduser()
            else:
                candidate = Path(f"capture-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png")
            if candidate.is_absolute():
                # Chemin explicite : confiné aux racines autorisées.
                target = self.file_manager.resolve(candidate)
            else:
                # Un relatif reste sous cache/screens : sinon « ../../vault.enc »
                # résolvait dans data/ et la capture remplaçait le coffre.
                target = self.file_manager.resolve(screens_dir / candidate)
                if not target.is_relative_to(screens_dir):
                    raise BrowserError(
                        "une capture en chemin relatif doit rester sous "
                        f"{screens_dir} (reçu : {target})"
                    )
            if target.exists():
                raise BrowserError(
                    f"capture refusée : {target} existe déjà et ne sera pas écrasé"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                page.screenshot(path=str(target), full_page=full_page)
            except Exception as exc:  # noqa: BLE001
                raise BrowserError(f"capture impossible : {exc}") from exc
            return {"path": str(target), "bytes": target.stat().st_size, "url": page.url}

    @staticmethod
    def _safe_download_name(name: str) -> str:
        """Réduit un nom fourni par le serveur à un simple nom de fichier.

        `suggested_filename` vient du `Content-Disposition` de la page distante,
        donc d'une source non fiable : tout chemin absolu, toute traversée `..`,
        tout séparateur résiduel et tout caractère interdit par Windows sont
        rejetés plutôt que normalisés en silence.
        """
        raw = str(name or "").replace("\\", "/").strip()
        candidate = raw.rsplit("/", 1)[-1].strip()
        if not candidate or candidate in {".", ".."}:
            raise BrowserError(f"nom de téléchargement invalide : {name!r}")
        if any(character in candidate for character in '<>:"|?*') or candidate.endswith((".", " ")):
            raise BrowserError(f"nom de téléchargement invalide : {name!r}")
        return candidate[:255]

    def download(self, *, selector: str = "", url: str = "") -> dict[str, Any]:
        with self._lock:
            page = self._ensure_page()
            if not selector and not url:
                raise BrowserError("précisez un sélecteur ou une URL")
            try:
                with page.expect_download(timeout=self.settings.browser.action_timeout_ms) as pending:
                    if selector:
                        page.locator(self._check_selector(selector)).first.click()
                    else:
                        page.goto(url)
                download = pending.value
                filename = self._safe_download_name(download.suggested_filename or "fichier")
                directory = self.settings.paths.download_dir
                stem, suffix = Path(filename).stem, Path(filename).suffix
                target = directory / filename
                counter = 1
                while target.exists():
                    target = directory / f"{stem} ({counter}){suffix}"
                    counter += 1
                # `_safe_download_name` garantit un nom sans séparateur : cette
                # résolution ne peut viser que le répertoire de téléchargement.
                target = self.file_manager.resolve(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                download.save_as(str(target))
            except Exception as exc:  # noqa: BLE001
                raise BrowserError(f"téléchargement impossible : {exc}") from exc
            return {"path": str(target), "bytes": target.stat().st_size, "filename": target.name}


# ---------------------------------------------------------------------------
# Outils exposés au modèle
# ---------------------------------------------------------------------------


def _cookie_summary(cookies: list[dict[str, Any]], *, max_items: int = 12) -> str:
    """Résumé des cookies utilisable par le modèle : noms et domaines, pas de valeurs."""
    domains = sorted({str(cookie.get("domain") or "") for cookie in cookies} - {""})
    names = sorted({str(cookie.get("name") or "") for cookie in cookies} - {""})

    def _join(values: list[str]) -> str:
        if not values:
            return "aucun"
        shown = ", ".join(values[:max_items])
        extra = len(values) - max_items
        return f"{shown} (+{extra})" if extra > 0 else shown

    return (
        f"{len(cookies)} cookie(s) — domaines : {_join(domains)} ; "
        f"noms : {_join(names)} ; valeurs masquées."
    )


def build_browser_tools(agent: BrowserAgent) -> list[Tool]:
    """Construit les outils navigateur, dont les quatre requis par le cahier des charges."""

    def browser_open(url: str) -> dict[str, Any]:
        return agent.goto(url)

    def browser_text(max_chars: int = 20000) -> dict[str, Any]:
        return agent.text(max_chars=max_chars)

    def browser_fetch_rendered(url: str, max_chars: int = 20000, wait_selector: str = "") -> dict[str, Any]:
        return agent.fetch_rendered(url, max_chars=max_chars, wait_selector=wait_selector)

    def login_to_service(service_name: str, username: str = "", url: str = "", submit: bool = True) -> dict[str, Any]:
        return agent.login_to_service(service_name, username, url=url, submit=submit)

    def browser_fill_signup(
        service_name: str,
        username: str = "",
        url: str = "",
        submit: bool = False,
        login_url: str = "",
    ) -> dict[str, Any]:
        return agent.fill_signup(
            service_name, username, url=url, submit=submit, login_url=login_url
        )

    def fill_form(selector: str, data: dict[str, Any], submit: bool = False) -> dict[str, Any]:
        return agent.fill_form(selector, data, submit=submit)

    def click_element(selector: str, index: int = 0, wait_after_ms: int = 0) -> dict[str, Any]:
        return agent.click_element(selector, index=index, wait_after_ms=wait_after_ms)

    def extract_cookies(domain: str = "") -> ToolResult:
        payload = agent.extract_cookies(domain)
        # Les valeurs restent locales ; le modèle ne reçoit qu'un résumé.
        return ToolResult(
            data=payload,
            sensitive=True,
            safe_summary=_cookie_summary(payload["cookies"]),
        )

    def browser_extract_links(selector: str = "a[href]", max_links: int = 200) -> dict[str, Any]:
        return agent.extract_links(selector=selector, max_links=max_links)

    def browser_screenshot(path: str = "", full_page: bool = True) -> dict[str, Any]:
        return agent.screenshot(path, full_page=full_page)

    def browser_download(selector: str = "", url: str = "") -> dict[str, Any]:
        return agent.download(selector=selector, url=url)

    def browser_status() -> dict[str, Any]:
        return agent.status().to_dict()

    def browser_close() -> dict[str, Any]:
        agent.stop()
        return {"closed": True}

    return [
        FunctionTool(
            "login_to_service",
            "Ouvre la page de connexion d'un service et s'authentifie avec les identifiants du coffre-fort chiffré. "
            "Le mot de passe n'est jamais renvoyé dans le résultat.",
            object_schema(
                {
                    "service_name": param("string", "Nom du service enregistré (ex. github.com)"),
                    "username": param("string", "Identifiant, si plusieurs comptes existent", default=""),
                    "url": param("string", "URL de connexion (sinon déduite du coffre)", default=""),
                    "submit": param("boolean", "Soumettre le formulaire", default=True),
                },
                required=["service_name"],
            ),
            login_to_service,
            read_only=False,
        ),
        FunctionTool(
            "browser_fill_signup",
            "Remplit un formulaire d'inscription avec l'identifiant et le mot de passe du "
            "coffre-fort, y compris le champ de confirmation. Avec submit=true, lit la page "
            "de résultat et indique si le compte a été créé (created, signup_status, evidence). "
            "Le mot de passe n'est jamais renvoyé ni affiché.",
            object_schema(
                {
                    "service_name": param("string", "Nom du service enregistré dans le coffre"),
                    "username": param("string", "Identifiant, si plusieurs comptes existent", default=""),
                    "url": param("string", "Page d'inscription (sinon la page courante)", default=""),
                    "submit": param("boolean", "Soumettre le formulaire", default=False),
                    "login_url": param(
                        "string",
                        "Page de connexion utilisée pour vérifier le compte "
                        "(sinon celle de l'entrée du coffre)",
                        default="",
                    ),
                },
                required=["service_name"],
            ),
            browser_fill_signup,
            read_only=False,
        ),
        FunctionTool(
            "fill_form",
            "Remplit un formulaire : `selector` cible le conteneur, `data` associe chaque sélecteur de champ à sa valeur.",
            object_schema(
                {
                    "selector": param("string", "Sélecteur du formulaire (vide pour la page entière)", default=""),
                    "data": param("object", "Dictionnaire sélecteur CSS → valeur"),
                    "submit": param("boolean", "Soumettre après remplissage", default=False),
                },
                required=["data"],
            ),
            fill_form,
            read_only=False,
        ),
        FunctionTool(
            "click_element",
            "Clique sur un élément de la page (index pour lever l'ambiguïté).",
            object_schema(
                {
                    "selector": param("string", "Sélecteur CSS de l'élément"),
                    "index": param("integer", "Occurrence à cliquer", default=0, minimum=0),
                    "wait_after_ms": param("integer", "Attente après le clic", default=0, minimum=0, maximum=30000),
                },
                required=["selector"],
            ),
            click_element,
            read_only=False,
        ),
        FunctionTool(
            "extract_cookies",
            "Extrait les cookies de la session navigateur (valeurs sensibles, à ne pas journaliser).",
            object_schema({"domain": param("string", "Filtre sur le domaine", default="")}),
            extract_cookies,
            sensitive=True,
        ),
        FunctionTool(
            "browser_open",
            "Navigue vers une URL dans le navigateur automatisé.",
            object_schema({"url": param("string", "URL à ouvrir")}, required=["url"]),
            browser_open,
            read_only=False,
        ),
        FunctionTool(
            "browser_text",
            "Retourne le texte visible de la page courante.",
            object_schema(
                {"max_chars": param("integer", "Troncature", default=20000, minimum=500, maximum=200000)}
            ),
            browser_text,
        ),
        FunctionTool(
            "browser_fetch_rendered",
            "Charge une page après exécution du JavaScript (SPA) et en extrait le contenu principal.",
            object_schema(
                {
                    "url": param("string", "URL de la page"),
                    "max_chars": param("integer", "Troncature", default=20000, minimum=500, maximum=200000),
                    "wait_selector": param("string", "Sélecteur à attendre avant extraction", default=""),
                },
                required=["url"],
            ),
            browser_fetch_rendered,
        ),
        FunctionTool(
            "browser_extract_links",
            "Liste les liens de la page courante (URL absolues résolues).",
            object_schema(
                {
                    "selector": param("string", "Sélecteur des liens", default="a[href]"),
                    "max_links": param("integer", "Nombre maximal", default=200, minimum=1, maximum=1000),
                }
            ),
            browser_extract_links,
        ),
        FunctionTool(
            "browser_screenshot",
            "Capture d'écran de la page courante.",
            object_schema(
                {
                    "path": param("string", "Nom de fichier ou chemin", default=""),
                    "full_page": param("boolean", "Page entière", default=True),
                }
            ),
            browser_screenshot,
        ),
        FunctionTool(
            "browser_download",
            "Télécharge un fichier en cliquant un lien ou en ouvrant une URL.",
            object_schema(
                {
                    "selector": param("string", "Sélecteur du lien de téléchargement", default=""),
                    "url": param("string", "URL directe du fichier", default=""),
                }
            ),
            browser_download,
            read_only=False,
        ),
        FunctionTool(
            "browser_status",
            "État du navigateur : démarré ou non, URL et titre courants.",
            object_schema({}),
            browser_status,
        ),
        FunctionTool(
            "browser_close",
            "Ferme le navigateur et libère le contexte persistant.",
            object_schema({}),
            browser_close,
            read_only=False,
        ),
    ]
