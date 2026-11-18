"""Recherche web multi-moteurs, extraction de contenu et dossiers de recherche.

Trois étages indépendants :

1. **Découverte** — adaptateurs de moteurs (`duckduckgo_html`, `duckduckgo_lite`,
   `duckduckgo_api`, `searx`, `brave`, `bing`) interrogés en cascade : si un
   moteur échoue ou renvoie peu, le suivant prend le relais. Les URL sont
   normalisées (paramètres de pistage retirés) et dédupliquées.
2. **Extraction** — `httpx` récupère la page (plafond de taille, `robots.txt`
   respecté, délai par hôte), puis `trafilatura` isole le contenu principal ;
   `beautifulsoup4` sert de repli quand l'extraction échoue.
3. **Dossier** — `deep_research()` croise plusieurs requêtes, récupère les
   pages, déduplique les sources et produit un rapport Markdown hiérarchisé.

Aucune clé API n'est requise par défaut (`duckduckgo_html`/`lite` + SearxNG).
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qs, quote_plus, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx

from config import APP_ID, Settings
from tools.base import FunctionTool, Tool, ToolError, object_schema, param

__all__ = [
    "HostBlocked",
    "PageContent",
    "SearchHit",
    "WebSearchError",
    "WebSearcher",
    "build_web_tools",
    "extract_content",
    "normalize_url",
    "parse_brave_json",
    "parse_duckduckgo_html",
    "parse_duckduckgo_lite",
    "parse_searx_json",
    "render_report",
]

logger = logging.getLogger(f"{APP_ID}.web")

MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
ROBOTS_TTL_S = 3600.0
_TEXTUAL_TYPES = (
    "text/html",
    "text/plain",
    "text/xml",
    "application/xhtml+xml",
    "application/xml",
    "application/json",
    "application/rss+xml",
    "application/atom+xml",
)
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "fbclid", "gclid", "dclid", "msclkid", "yclid",
    "mc_cid", "mc_eid", "igshid", "spm", "ref_src", "ref_url", "_gl",
}
_DDG_HOSTS = {"duckduckgo.com", "www.duckduckgo.com", "html.duckduckgo.com", "lite.duckduckgo.com"}


# ---------------------------------------------------------------------------
# Erreurs
# ---------------------------------------------------------------------------


class WebSearchError(ToolError):
    """Erreur de recherche ou de récupération de page."""

    code = "web_error"


class SearchError(WebSearchError):
    code = "search_failed"


class FetchError(WebSearchError):
    code = "fetch_failed"


class HostBlocked(FetchError):
    """Cible interne refusée (loopback, link-local, plage privée)."""

    code = "host_blocked"


class RobotsDenied(FetchError):
    code = "robots_denied"


# Suffixes qui désignent la machine locale ou le réseau du domicile/entreprise.
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan")
_BLOCKED_HOST_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
_HOST_CACHE_TTL_S = 300.0
# Fréquence minimale des purges (§ caches de résolution) : assez rare pour ne
# pas coûter un parcours complet à chaque requête, assez fréquent pour qu'un
# long run ne conserve pas les hôtes d'une heure plus tôt.
_PURGE_INTERVAL_S = 60.0
# Plafond **dur** par dictionnaire : la purge périodique ne traite que les
# entrées périmées, pas les milliers d'hôtes encore frais d'un run très long.
_CACHE_MAX_ENTRIES = 512
# Adresses en échec mémorisées par hôte : au-delà, les plus anciennes sautent.
_FAILED_IPS_PER_HOST = 32


def _parse_ip_literal(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Analyse une adresse littérale, y compris les formes entières de Windows.

    `http://2130706433/` et `http://0x7f000001/` sont acceptées par les piles
    réseau et désignent 127.0.0.1 : elles doivent être reconnues comme locales.
    """
    candidate = str(text or "").strip().strip("[]").split("%")[0]
    if not candidate:
        return None
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        pass
    try:
        numeric = int(candidate, 0)
    except ValueError:
        return None
    if 0 <= numeric <= 0xFFFFFFFF:
        return ipaddress.IPv4Address(numeric)
    return None


def _is_blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Vrai si l'adresse n'est pas une destination publique routable."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    )


# ---------------------------------------------------------------------------
# Modèles
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    engine: str = ""
    rank: int = 0
    domain: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "engine": self.engine,
            "rank": self.rank,
            "domain": self.domain or urlparse(self.url).netloc,
        }


@dataclass(slots=True)
class PageContent:
    url: str
    final_url: str = ""
    title: str = ""
    text: str = ""
    excerpt: str = ""
    word_count: int = 0
    language: str = ""
    author: str = ""
    published: str = ""
    status: int = 0
    content_type: str = ""
    fetched_at: str = ""
    from_cache: bool = False
    truncated: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.text)

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        payload = asdict(self)
        if not include_text:
            payload.pop("text", None)
        return payload


# ---------------------------------------------------------------------------
# Utilitaires d'URL
# ---------------------------------------------------------------------------


def _unwrap_redirect(url: str) -> str:
    """Déballe les redirections DuckDuckGo (`/l/?uddg=`) et Bing (`/ck/a?u=`)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    host = parsed.netloc.lower()
    if host in _DDG_HOSTS and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        return target[0] if target else url
    if host.endswith("bing.com") and parsed.path.startswith("/ck/a"):
        encoded = parse_qs(parsed.query).get("u", [""])[0]
        if encoded.startswith("a1"):
            payload = encoded[2:]
            payload += "=" * (-len(payload) % 4)
            try:
                return base64.urlsafe_b64decode(payload).decode("utf-8", errors="replace")
            except (ValueError, TypeError):
                return url
    return url


def normalize_url(url: str) -> str:
    """Normalise une URL pour la déduplication (hôte, schéma, pistage, ancre)."""
    candidate = _unwrap_redirect(url.strip())
    if not candidate:
        return ""
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return candidate
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = [
        (key, value)
        for key, value in parse_qs(parsed.query, keep_blank_values=True).items()
        if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    query = "&".join(f"{key}={value[0]}" for key, value in sorted(query_pairs))
    return urlunparse((scheme, netloc, path, "", query, ""))


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Cache disque
# ---------------------------------------------------------------------------


class _Cache:
    """Cache JSON élémentaire, un fichier par clé, expiration par TTL."""

    def __init__(self, directory: Path, ttl_s: int) -> None:
        self.directory = directory
        self.ttl_s = max(0, ttl_s)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.directory / f"{digest}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        if self.ttl_s <= 0:
            return None
        path = self._path(key)
        try:
            if not path.is_file():
                return None
            if time.time() - path.stat().st_mtime > self.ttl_s:
                path.unlink(missing_ok=True)
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def set(self, key: str, payload: dict[str, Any]) -> None:
        if self.ttl_s <= 0:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self._path(key)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            logger.debug("cache web indisponible pour %s", key)

    def purge(self) -> int:
        if not self.directory.is_dir():
            return 0
        removed = 0
        for path in self.directory.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed


# ---------------------------------------------------------------------------
# Extraction de contenu
# ---------------------------------------------------------------------------

_BOILERPLATE_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "aside", "form",
                     "iframe", "svg", "button", "template")


def _bs4_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_BOILERPLATE_TAGS):
        tag.decompose()
    for element in soup.find_all(attrs={"class": re.compile(r"(cookie|consent|newsletter|advert|banner)", re.I)}):
        element.decompose()
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)


def _decode_body(raw: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w\-]+)", content_type, re.I)
    if match:
        try:
            return raw.decode(match.group(1), errors="replace")
        except LookupError:
            pass
    try:
        from bs4 import UnicodeDammit

        converted = UnicodeDammit(raw, is_html=True)
        if converted.unicode_markup:
            return converted.unicode_markup
    except Exception:  # noqa: BLE001 - détection best effort
        pass
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_content(html: str, url: str = "") -> dict[str, str]:
    """Isole le contenu principal : `trafilatura` d'abord, `bs4` en repli."""
    title = author = published = language = ""
    text = ""

    try:
        import trafilatura

        try:
            metadata = trafilatura.extract_metadata(html)
            if metadata is not None:
                title = (getattr(metadata, "title", "") or "").strip()
                author = (getattr(metadata, "author", "") or "").strip()
                published = (getattr(metadata, "date", "") or "").strip()
                language = (getattr(metadata, "language", "") or "").strip()
        except Exception:  # noqa: BLE001 - métadonnées absentes
            pass

        text = (
            trafilatura.extract(
                html,
                url=url or None,
                output_format="txt",
                include_comments=False,
                include_tables=True,
                favor_precision=True,
            )
            or ""
        )
    except ImportError:  # pragma: no cover - dépend de l'installation
        logger.debug("trafilatura absent : repli sur BeautifulSoup")
    except Exception as exc:  # noqa: BLE001 - extraction défensive
        logger.debug("trafilatura a échoué (%s) : repli bs4", exc)

    if not text.strip():
        try:
            text = _bs4_text(html)
        except Exception as exc:  # noqa: BLE001
            raise FetchError(f"extraction impossible : {exc}") from exc

    if not title:
        try:
            from bs4 import BeautifulSoup

            node = BeautifulSoup(html, "html.parser").find("title")
            title = node.get_text(strip=True) if node else ""
        except Exception:  # noqa: BLE001
            title = ""

    text = re.sub(r"[ \t]+\n", "\n", text).strip()
    return {
        "title": title[:300],
        "author": author[:200],
        "published": published[:40],
        "language": language[:12],
        "text": text,
    }


# ---------------------------------------------------------------------------
# Parseurs de moteurs (purs : testables sans réseau)
# ---------------------------------------------------------------------------


#: Moteurs HTML de DuckDuckGo, seuls concernés par la page anti-robot.
_DDG_HTML_ENGINES = frozenset({"duckduckgo_html", "duckduckgo_lite"})


def is_block_page(status: int, body: str) -> bool:
    """Détecte la page anti-robot de DuckDuckGo (HTTP 202 « anomaly »).

    Le moteur répond 202 avec un formulaire « Unfortunately, bots use
    DuckDuckGo too » : le HTML ne contient aucun résultat, donc le parseur
    renverrait une liste vide et l'erreur finale parlerait d'une recherche
    infructueuse au lieu du vrai motif.
    """
    if status == 202:
        return True
    return "anomaly" in (body or "").lower()


def parse_duckduckgo_html(html: str) -> list[SearchHit]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    hits: list[SearchHit] = []
    for anchor in soup.select("a.result__a"):
        href = anchor.get("href", "")
        if not href:
            continue
        container = anchor.find_parent(class_=re.compile(r"result"))
        snippet_node = container.select_one(".result__snippet") if container else None
        hits.append(
            SearchHit(
                title=anchor.get_text(" ", strip=True),
                url=normalize_url(href),
                snippet=snippet_node.get_text(" ", strip=True) if snippet_node else "",
                engine="duckduckgo_html",
            )
        )
    return hits


def parse_duckduckgo_lite(html: str) -> list[SearchHit]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    hits: list[SearchHit] = []
    for anchor in soup.select("a.result-link"):
        href = anchor.get("href", "")
        if not href:
            continue
        row = anchor.find_parent("tr")
        snippet_node = None
        if row is not None and row.find_next_sibling("tr") is not None:
            snippet_node = row.find_next_sibling("tr").select_one("td.result-snippet")
        hits.append(
            SearchHit(
                title=anchor.get_text(" ", strip=True),
                url=normalize_url(href),
                snippet=snippet_node.get_text(" ", strip=True) if snippet_node else "",
                engine="duckduckgo_lite",
            )
        )
    return hits


def parse_searx_json(payload: dict[str, Any]) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for item in payload.get("results") or []:
        url = str(item.get("url") or "")
        if not url:
            continue
        hits.append(
            SearchHit(
                title=str(item.get("title") or url),
                url=normalize_url(url),
                snippet=str(item.get("content") or "")[:500],
                engine="searx",
            )
        )
    return hits


def parse_searx_html(html: str) -> list[SearchHit]:
    """Repli quand l'instance SearxNG n'expose pas l'API JSON."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    if re.search(r"verifying your browser|captcha", html[:4000], re.I):
        return []
    hits: list[SearchHit] = []
    for article in soup.select("article.result"):
        anchor = article.select_one("h3 a") or article.select_one("a.url_wrapper") or article.select_one("a")
        href = anchor.get("href", "") if anchor is not None else ""
        if not href:
            continue
        snippet = article.select_one("p.content") or article.select_one(".content")
        hits.append(
            SearchHit(
                title=anchor.get_text(" ", strip=True),
                url=normalize_url(href),
                snippet=snippet.get_text(" ", strip=True)[:500] if snippet else "",
                engine="searx",
            )
        )
    return hits


def parse_brave_json(payload: dict[str, Any]) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for item in (payload.get("web") or {}).get("results") or []:
        url = str(item.get("url") or "")
        if not url:
            continue
        hits.append(
            SearchHit(
                title=str(item.get("title") or url),
                url=normalize_url(url),
                snippet=str(item.get("description") or "")[:500],
                engine="brave",
            )
        )
    return hits


def parse_bing_html(html: str) -> list[SearchHit]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    hits: list[SearchHit] = []
    for item in soup.select("li.b_algo"):
        anchor = item.select_one("h2 a")
        if anchor is None or not anchor.get("href"):
            continue
        paragraph = item.select_one("p")
        hits.append(
            SearchHit(
                title=anchor.get_text(" ", strip=True),
                url=normalize_url(anchor["href"]),
                snippet=paragraph.get_text(" ", strip=True) if paragraph else "",
                engine="bing",
            )
        )
    return hits


# ---------------------------------------------------------------------------
# Moteur de recherche
# ---------------------------------------------------------------------------


class WebSearcher:
    """Recherche, récupération et extraction, avec cache et politesse réseau."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        cache_enabled: bool = True,
        logger_: logging.Logger | None = None,
        max_cache_entries: int = _CACHE_MAX_ENTRIES,
    ) -> None:
        self.settings = settings
        self.cfg = settings.search
        self.logger = logger_ or logger
        self._client = client
        self._owns_client = client is None
        self.cache = _Cache(settings.paths.cache_dir / "web", self.cfg.cache_ttl_s) if cache_enabled else None
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, tuple[RobotFileParser | None, float]] = {}
        self._host_cache: dict[str, tuple[float, str]] = {}
        self._ip_cache: dict[str, tuple[float, str]] = {}
        # Adresses déjà en échec de connexion, par hôte : ignorées jusqu'à
        # expiration pour que le prochain essai vise vraiment une autre IP.
        self._failed_ips: dict[str, dict[str, float]] = {}
        # Dernier usage réel d'une IP épinglée : une adresse qui vient de servir
        # survit à l'éviction au lieu de disparaître en pleine connexion.
        self._ip_touched: dict[str, float] = {}
        self._max_entries = max(1, int(max_cache_entries))
        self._searx_cursor = 0
        self.request_count = 0
        # Les TTL ne s'appliquent qu'à la lecture : sans purge, un hôte visité
        # une seule fois laisserait son IP, son verdict robots et ses échecs en
        # mémoire pour toute la durée du processus.
        self._next_purge = time.monotonic() + _PURGE_INTERVAL_S

    # -- cycle de vie -------------------------------------------------------

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                follow_redirects=True,
                timeout=httpx.Timeout(self.cfg.request_timeout_s, connect=10.0),
                headers={
                    "User-Agent": self.cfg.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
                },
                limits=httpx.Limits(max_connections=max(4, self.cfg.max_concurrent_fetches), max_keepalive_connections=8),
                # L'IP validée est épinglée pour la connexion réelle (anti rebinding).
                event_hooks={"request": [self._pin_request]},
            )
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "WebSearcher":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- politesse ----------------------------------------------------------

    # -- entretien des caches ----------------------------------------------

    def _maybe_purge(self) -> None:
        """Purge les caches si l'intervalle est écoulé (coût amorti)."""
        if time.monotonic() >= self._next_purge:
            self._purge_caches()
            self._cap_caches()

    def _purge_caches(self, now: float | None = None) -> int:
        """Retire les entrées expirées et retourne le nombre d'entrées ôtées.

        Les purges sont périodiques plutôt qu'à chaque accès : les dictionnaires
        sont petits, mais un run de plusieurs heures peut accumuler des milliers
        d'hôtes distincts sans jamais les relire.
        """
        moment = time.monotonic() if now is None else now
        wall = time.time()
        self._next_purge = moment + _PURGE_INTERVAL_S
        removed = 0

        for host, (stamp, _address) in list(self._ip_cache.items()):
            if moment - stamp >= _HOST_CACHE_TTL_S:
                self._ip_cache.pop(host, None)
                removed += 1
        for host, (stamp, _reason) in list(self._host_cache.items()):
            if moment - stamp >= _HOST_CACHE_TTL_S:
                self._host_cache.pop(host, None)
                removed += 1
        for origin, (_parser, stamp) in list(self._robots.items()):
            if wall - stamp >= ROBOTS_TTL_S:
                self._robots.pop(origin, None)
                removed += 1
        for host, entries in list(self._failed_ips.items()):
            fresh = {
                address: stamp
                for address, stamp in entries.items()
                if moment - stamp < _HOST_CACHE_TTL_S
            }
            removed += len(entries) - len(fresh)
            if fresh:
                self._failed_ips[host] = fresh
            else:
                self._failed_ips.pop(host, None)
        for host, stamp in list(self._last_request.items()):
            if moment - stamp >= _HOST_CACHE_TTL_S:
                self._last_request.pop(host, None)
                removed += 1
        return removed

    def _cap_caches(self, now: float | None = None) -> int:
        """Ramène chaque cache sous `_max_entries`, sans perdre une IP active.

        Le plafond est dur : si toutes les entrées sont protégées, les plus
        anciennes sautent quand même. La protection ne fait que *retarder*
        l'éviction d'une IP épinglée utilisée récemment — l'ôter pendant que la
        connexion s'appuie dessus rouvrirait la fenêtre de DNS-rebinding que le
        pinning cherche à fermer.
        """
        moment = time.monotonic() if now is None else now
        limit = self._max_entries
        removed = 0

        # Un horodatage d'usage trop vieux ne protège plus rien.
        self._ip_touched = {
            host: stamp
            for host, stamp in self._ip_touched.items()
            if moment - stamp < _HOST_CACHE_TTL_S
        }

        excess = len(self._ip_cache) - limit
        if excess > 0:
            def ip_rank(host: str) -> tuple[bool, float]:
                recent = moment - self._ip_touched.get(host, float("-inf")) < _HOST_CACHE_TTL_S
                return (recent, self._ip_cache[host][0])

            for host in sorted(self._ip_cache, key=ip_rank)[:excess]:
                self._ip_cache.pop(host, None)
                self._ip_touched.pop(host, None)
                removed += 1

        excess = len(self._host_cache) - limit
        if excess > 0:
            for host in sorted(self._host_cache, key=lambda h: self._host_cache[h][0])[:excess]:
                self._host_cache.pop(host, None)
                removed += 1

        excess = len(self._robots) - limit
        if excess > 0:
            for origin in sorted(self._robots, key=lambda o: self._robots[o][1])[:excess]:
                self._robots.pop(origin, None)
                removed += 1

        for host, entries in list(self._failed_ips.items()):
            if len(entries) <= _FAILED_IPS_PER_HOST:
                continue
            kept = dict(
                sorted(entries.items(), key=lambda item: item[1], reverse=True)[
                    :_FAILED_IPS_PER_HOST
                ]
            )
            removed += len(entries) - len(kept)
            self._failed_ips[host] = kept

        excess = len(self._failed_ips) - limit
        if excess > 0:
            oldest = sorted(
                self._failed_ips, key=lambda host: min(self._failed_ips[host].values())
            )
            for host in oldest[:excess]:
                removed += len(self._failed_ips.pop(host))
        return removed

    def _throttle(self, host: str) -> None:
        delay = max(0.0, self.cfg.per_host_delay_s)
        if delay <= 0:
            return
        last = self._last_request.get(host, 0.0)
        remaining = delay - (time.monotonic() - last)
        if remaining > 0:
            time.sleep(remaining)
        self._last_request[host] = time.monotonic()

    def robots_allowed(self, url: str) -> bool:
        if not self.cfg.respect_robots:
            return True
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cached = self._robots.get(origin)
        parser: RobotFileParser | None = None
        if cached and time.time() - cached[1] < ROBOTS_TTL_S:
            parser = cached[0]
        else:
            try:
                # Même boucle validée que le reste : une redirection de
                # robots.txt ne peut pas non plus viser le réseau local.
                status, content_type, _url, raw, _truncated = self._get_following(
                    f"{origin}/robots.txt",
                    min(10.0, self.cfg.request_timeout_s),
                    check_robots=False,
                    allow_client_error=True,
                )
                # Un 4xx vaut « robots absent » : ce n'est pas une panne.
                if status == 200:
                    parser = RobotFileParser()
                    parser.parse(_decode_body(raw, content_type).splitlines())
            except (FetchError, HostBlocked):
                parser = None
            self._robots[origin] = (parser, time.time())
            self._cap_caches()
        if parser is None:
            return True
        try:
            return parser.can_fetch("*", url)
        except Exception:  # noqa: BLE001 - robots malformé
            return True

    def validate_url(self, url: str) -> str:
        """Vérifie le schéma et refuse les cibles internes (anti-SSRF).

        Un contenu web lu par l'agent peut proposer n'importe quelle URL :
        sans ce filtre, `web_fetch` servirait de scanner du réseau local
        (`http://127.0.0.1:…`, `http://192.168.1.1/…`, `http://169.254.169.254/`).
        """
        self._maybe_purge()
        if not url or not isinstance(url, str):
            raise FetchError("URL vide")
        candidate = url.strip()
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            raise FetchError(f"schéma d'URL non autorisé : {parsed.scheme or '(absent)'}")
        if not parsed.netloc or not parsed.hostname:
            raise FetchError(f"URL incomplète : {candidate}")
        if not self.cfg.allow_private_networks:
            reason = self._blocked_host(parsed.hostname)
            if reason:
                raise HostBlocked(f"cible interne refusée : {reason}")
        return candidate

    def _blocked_address(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        """Vrai si l'adresse est interdite au regard de `allow_private_networks`."""
        return not self.cfg.allow_private_networks and _is_blocked_address(address)

    def _forget_pinned(self, url: str) -> None:
        """Oublie l'IP épinglée d'un hôte dont la connexion vient d'échouer.

        Sans cela, l'adresse choisie resterait imposée pendant tout le TTL alors
        qu'un autre nœud du même nom répond peut-être. L'entrée est supprimée
        pour que le prochain essai reparte d'une résolution neuve.
        """
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return
        cached = self._ip_cache.pop(host, None)
        self._ip_touched.pop(host, None)
        if cached and cached[1]:
            self._failed_ips.setdefault(host, {})[cached[1]] = time.monotonic()
            self._cap_caches()

    def _failed_addresses(self, host: str, now: float) -> set[str]:
        """Adresses encore exclues pour `host` (les entrées périmées sont purgées)."""
        entries = self._failed_ips.get(host)
        if not entries:
            return set()
        fresh = {
            address: stamp
            for address, stamp in entries.items()
            if now - stamp < _HOST_CACHE_TTL_S
        }
        if fresh:
            self._failed_ips[host] = fresh
        else:
            self._failed_ips.pop(host, None)
        return set(fresh)

    def _pinned_address(self, host: str, port: int) -> str:
        """Adresse IP à utiliser pour `host`, résolue une fois puis mémorisée.

        Retourne une chaîne vide si aucune adresse publique n'est disponible.
        """
        literal = _parse_ip_literal(host)
        if literal is not None:
            return "" if self._blocked_address(literal) else str(literal)

        cached = self._ip_cache.get(host)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _HOST_CACHE_TTL_S:
            self._ip_touched[host] = now
            return cached[1]

        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except (OSError, UnicodeError):
            infos = []
        candidates = [
            candidate
            for info in infos
            if (candidate := _parse_ip_literal(str(info[4][0]))) is not None
            and not self._blocked_address(candidate)
        ]

        excluded = self._failed_addresses(host, now)
        address = next((str(c) for c in candidates if str(c) not in excluded), "")
        if not address and excluded:
            # Toutes les adresses connues ont échoué : on redonne sa chance à
            # l'ensemble plutôt que de rendre l'hôte inutilisable pour le TTL.
            self._failed_ips.pop(host, None)
            address = str(candidates[0]) if candidates else ""
        self._ip_cache[host] = (now, address)
        self._ip_touched[host] = now
        self._cap_caches(now)
        return address

    def _pin_request(self, request: httpx.Request) -> None:
        """Épingle l'IP déjà validée pour la connexion réelle (anti DNS-rebinding).

        httpx résoudrait le nom une seconde fois au moment de se connecter :
        entre la validation et la connexion, un répondeur hostile peut renvoyer
        une adresse interne. L'URL envoyée porte donc l'IP contrôlée, tandis que
        l'en-tête `Host` et l'extension `sni_hostname` conservent le nom
        d'origine pour le routage HTTP et la vérification du certificat.
        """
        host = request.url.host
        if _parse_ip_literal(host) is not None:
            return
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        address = self._pinned_address(host, port)
        if not address:
            raise httpx.ConnectError(
                f"aucune adresse publique pour {host}", request=request
            )
        request.url = request.url.copy_with(host=address)
        request.headers["host"] = host
        request.extensions = {**request.extensions, "sni_hostname": host}

    def _blocked_host(self, host: str) -> str:
        """Retourne la raison du refus, ou une chaîne vide si l'hôte est public."""
        name = str(host or "").strip().strip("[]").rstrip(".").lower()
        if not name:
            return "hôte vide"
        if name in _BLOCKED_HOST_NAMES:
            return f"hôte local « {name} »"
        if any(name.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES):
            return f"hôte local « {name} »"

        literal = _parse_ip_literal(name)
        if literal is not None:
            return f"adresse non publique {literal}" if self._blocked_address(literal) else ""

        cached = self._host_cache.get(name)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _HOST_CACHE_TTL_S:
            return cached[1]

        reason = ""
        try:
            infos = socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
        except (OSError, UnicodeError):
            # Résolution impossible ici : la requête échouera d'elle-même.
            infos = []
        for info in infos:
            address = _parse_ip_literal(str(info[4][0]))
            if address is not None and self._blocked_address(address):
                reason = f"« {name} » résout vers l'adresse non publique {address}"
                break
        self._host_cache[name] = (now, reason)
        self._cap_caches(now)
        return reason

    # -- recherche ----------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        max_results: int | None = None,
        engines: Sequence[str] | None = None,
    ) -> list[SearchHit]:
        query = (query or "").strip()
        if not query:
            raise SearchError("requête vide")
        target = max_results or self.cfg.max_results_per_query
        selected = list(engines) if engines else list(self.cfg.engines)
        if not selected:
            raise SearchError("aucun moteur configuré")

        seen: set[str] = set()
        collected: list[SearchHit] = []
        failures: list[str] = []
        for engine in selected:
            if len(collected) >= target:
                break
            try:
                hits = self._run_engine(engine, query, target)
            except Exception as exc:  # noqa: BLE001 - un moteur en échec ne bloque pas les autres
                failures.append(f"{engine}: {exc}")
                self.logger.debug("moteur %s en échec : %s", engine, exc)
                continue
            for hit in hits:
                key = normalize_url(hit.url)
                if not key or key in seen:
                    continue
                seen.add(key)
                hit.rank = len(collected) + 1
                hit.domain = _domain(hit.url)
                collected.append(hit)
                if len(collected) >= target:
                    break

        if not collected:
            detail = " | ".join(failures) if failures else "aucun résultat"
            raise SearchError(f"recherche infructueuse pour « {query} » ({detail})")
        return collected[:target]

    def _run_engine(self, engine: str, query: str, limit: int) -> list[SearchHit]:
        if engine == "duckduckgo_api":
            return self._engine_duckduckgo_api(query, limit)
        if engine == "searx":
            return self._engine_searx(query, limit)
        if engine == "brave":
            return self._engine_brave(query, limit)

        urls = {
            "duckduckgo_html": f"https://html.duckduckgo.com/html/?q={quote_plus(query)}&kl={self.cfg.region}",
            "duckduckgo_lite": f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}&kl={self.cfg.region}",
            "bing": f"https://www.bing.com/search?q={quote_plus(query)}&count={min(limit, 30)}",
        }
        parsers = {
            "duckduckgo_html": parse_duckduckgo_html,
            "duckduckgo_lite": parse_duckduckgo_lite,
            "bing": parse_bing_html,
        }
        if engine not in urls:
            raise SearchError(f"moteur inconnu : {engine}")
        # Aucune requête du moteur ne doit échapper au filtre anti-SSRF, ni
        # suivre une redirection sans revalidation : même boucle que `fetch`.
        status, content_type, _final_url, raw, _truncated = self._get_following(
            urls[engine], check_robots=False
        )
        body = _decode_body(raw, content_type)
        if engine in _DDG_HTML_ENGINES and is_block_page(status, body):
            raise SearchError(
                f"bloqué par le moteur {engine} (page anti-robot « anomaly », HTTP {status})"
            )
        if status != 200:
            raise SearchError(f"HTTP {status} sur {engine}")
        return parsers[engine](body)[:limit]

    def _engine_duckduckgo_api(self, query: str, limit: int) -> list[SearchHit]:
        try:
            from ddgs import DDGS
        except ImportError as exc:  # pragma: no cover
            raise SearchError("paquet 'ddgs' absent (pip install ddgs)") from exc
        hits: list[SearchHit] = []
        with DDGS(timeout=int(self.cfg.request_timeout_s)) as ddgs:
            for item in ddgs.text(query, region=self.cfg.region, max_results=limit) or []:
                url = str(item.get("href") or item.get("url") or "")
                if not url:
                    continue
                hits.append(
                    SearchHit(
                        title=str(item.get("title") or url),
                        url=normalize_url(url),
                        snippet=str(item.get("body") or "")[:500],
                        engine="duckduckgo_api",
                    )
                )
        return hits[:limit]

    def _engine_searx(self, query: str, limit: int) -> list[SearchHit]:
        instances = list(self.cfg.searx_instances)
        if not instances:
            raise SearchError("aucune instance SearxNG configurée")
        # Rotation : évite de marteler une seule instance.
        instance = instances[self._searx_cursor % len(instances)].rstrip("/")
        self._searx_cursor = (self._searx_cursor + 1) % len(instances)
        # Même boucle que `fetch` : instance filtrée, puis chaque redirection
        # revalidée avant d'être suivie (pas de rebond vers le réseau local).
        target = f"{instance}/search?" + urlencode(
            {"q": query, "format": "json", "language": self.cfg.region.split("-")[0]}
        )
        status, content_type, _final_url, raw, _truncated = self._get_following(
            target, check_robots=False
        )
        if status != 200:
            raise SearchError(f"HTTP {status} sur {instance}")
        text = _decode_body(raw, content_type)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            hits = parse_searx_html(text)
            if not hits:
                raise SearchError(
                    f"{instance} renvoie une page inexploitable (API JSON désactivée ou captcha)"
                ) from None
            return hits[:limit]
        return parse_searx_json(payload)[:limit]

    def _engine_brave(self, query: str, limit: int) -> list[SearchHit]:
        token = os.environ.get(self.cfg.brave_api_key_env, "").strip()
        if not token:
            raise SearchError(f"variable {self.cfg.brave_api_key_env} absente")
        target = "https://api.search.brave.com/res/v1/web/search?" + urlencode(
            {
                "q": query,
                "count": min(limit, 20),
                "country": self.cfg.region.split("-")[-1].upper(),
            }
        )
        # Comme SearxNG : aucune requête moteur hors de la boucle qui valide
        # chaque URL (dont chaque redirection).
        status, content_type, _final_url, raw, _truncated = self._get_following(
            target,
            check_robots=False,
            headers={"Accept": "application/json", "X-Subscription-Token": token},
        )
        if status != 200:
            raise SearchError(f"HTTP {status} sur l'API Brave")
        try:
            payload = json.loads(_decode_body(raw, content_type))
        except json.JSONDecodeError as exc:
            raise SearchError("réponse Brave illisible (JSON attendu)") from exc
        return parse_brave_json(payload)[:limit]

    # -- récupération de page ----------------------------------------------

    def _stream_once(
        self,
        url: str,
        timeout: float | None,
        headers: dict[str, str] | None = None,
        *,
        allow_client_error: bool = False,
    ) -> tuple[int, str, str, bytes, bool, str]:
        """Récupère une URL **sans** suivre les redirections.

        Retourne ``(statut, content-type, url finale, corps, tronqué, location)``.
        `location` est vide tant que la réponse est définitive.
        """
        self.request_count += 1
        self._maybe_purge()
        chunks: list[bytes] = []
        size = 0
        truncated = False
        try:
            with self._http().stream(
                "GET",
                url,
                timeout=timeout or self.cfg.request_timeout_s,
                follow_redirects=False,
                headers=headers,
            ) as response:
                status = response.status_code
                content_type = response.headers.get("content-type", "")
                # L'URL demandée reste la référence : l'IP épinglée n'est qu'un
                # détail de transport, jamais une identité de page.
                final_url = url
                if 300 <= status < 400:
                    location = response.headers.get("location", "")
                    return status, content_type, final_url, b"", False, location
                if 400 <= status < 500 and allow_client_error:
                    # 4xx attendu (robots.txt absent) : statut rendu à l'appelant.
                    return status, content_type, final_url, b"", False, ""
                if status >= 400:
                    raise FetchError(f"HTTP {status} pour {url}")
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_PAGE_BYTES:
                        truncated = True
                        break
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Échec au niveau de la connexion : l'IP épinglée est peut-être en
            # cause, on ne la réimpose pas au prochain essai.
            self._forget_pinned(url)
            raise FetchError(f"récupération impossible ({type(exc).__name__}) : {exc}") from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"récupération impossible ({type(exc).__name__}) : {exc}") from exc
        return status, content_type, final_url, b"".join(chunks), truncated, ""

    def _get_following(
        self,
        url: str,
        timeout: float | None = None,
        *,
        check_robots: bool = True,
        headers: dict[str, str] | None = None,
        allow_client_error: bool = False,
    ) -> tuple[int, str, str, bytes, bool]:
        """GET avec redirections suivies **après** validation de chaque saut.

        Retourne ``(statut, content-type, url finale, corps, tronqué)``.
        Les `headers` fournis (jeton d'API) ne sont pas propagés à un autre hôte.
        """
        current = self.validate_url(url)
        if check_robots and not self.robots_allowed(current):
            raise RobotsDenied(f"robots.txt interdit la récupération de {current}")

        origin = (urlparse(current).hostname or "").lower()
        hop_headers = headers
        for hop in range(MAX_REDIRECTS + 1):
            self._throttle(_domain(current))
            status, content_type, final_url, raw, truncated, location = self._stream_once(
                current, timeout, hop_headers, allow_client_error=allow_client_error
            )
            if not location:
                return status, content_type, final_url, raw, truncated
            if hop == MAX_REDIRECTS:
                raise FetchError(f"trop de redirections depuis {url}")
            current = urljoin(current, location)
            self.validate_url(current)
            if (urlparse(current).hostname or "").lower() != origin:
                hop_headers = None  # un jeton d'API ne suit pas une redirection hors hôte
            if check_robots and not self.robots_allowed(current):
                raise RobotsDenied(f"robots.txt interdit la récupération de {current}")
        raise FetchError(f"trop de redirections depuis {url}")  # pragma: no cover - boucle fermée

    def fetch(
        self,
        url: str,
        *,
        max_chars: int = 0,
        timeout: float | None = None,
        use_cache: bool = True,
    ) -> PageContent:
        target = self.validate_url(url)
        key = f"page:{normalize_url(target)}"
        if use_cache and self.cache:
            cached = self.cache.get(key)
            if cached:
                page = PageContent(**{k: v for k, v in cached.items() if k in PageContent.__dataclass_fields__})
                page.from_cache = True
                if max_chars and len(page.text) > max_chars:
                    page.text = page.text[:max_chars]
                    page.truncated = True
                return page

        # Redirections suivies à la main : httpx les suivrait sans repasser par
        # `validate_url`, ce qui laissait une page publique renvoyer un 302 vers
        # 127.0.0.1 ou 192.168.x et faire sonder le réseau local.
        status, content_type, final_url, raw, truncated = self._get_following(target, timeout)

        lowered = content_type.lower()
        if lowered and not any(lowered.startswith(prefix) for prefix in _TEXTUAL_TYPES):
            raise FetchError(f"type de contenu non textuel : {content_type.split(';')[0]}")

        body = _decode_body(raw, content_type)

        if "json" in lowered:
            try:
                text = json.dumps(json.loads(body), ensure_ascii=False, indent=2)
                extracted = {"title": "", "author": "", "published": "", "language": "", "text": text}
            except json.JSONDecodeError:
                extracted = {"title": "", "author": "", "published": "", "language": "", "text": body}
        else:
            extracted = extract_content(body, final_url or target)

        page = PageContent(
            url=target,
            final_url=final_url,
            title=extracted["title"],
            text=extracted["text"],
            excerpt=re.sub(r"\s+", " ", extracted["text"])[:400],
            word_count=len(extracted["text"].split()),
            language=extracted["language"],
            author=extracted["author"],
            published=extracted["published"],
            status=status,
            content_type=lowered.split(";")[0],
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            truncated=truncated,
        )
        if max_chars and len(page.text) > max_chars:
            page.text = page.text[:max_chars]
            page.truncated = True
        if use_cache and self.cache and page.ok:
            self.cache.set(key, asdict(page))
        return page

    def search_and_fetch(self, query: str, *, top_n: int = 3) -> dict[str, Any]:
        hits = self.search(query, max_results=max(1, top_n))
        pages: list[PageContent] = []
        for hit in hits:
            try:
                pages.append(self.fetch(hit.url, max_chars=20000))
            except WebSearchError as exc:
                self.logger.debug("page ignorée %s : %s", hit.url, exc)
        return {
            "query": query,
            "hits": [hit.to_dict() for hit in hits],
            "pages": [page.to_dict(include_text=False) for page in pages],
        }

    # -- dossier de recherche ----------------------------------------------

    def deep_research(
        self,
        queries: Iterable[str],
        *,
        pages_per_query: int | None = None,
        max_pages: int | None = None,
        save_report: bool = True,
        include_text: bool = False,
    ) -> dict[str, Any]:
        """Croise plusieurs requêtes et récupère les pages les plus pertinentes."""
        cleaned = [str(query).strip() for query in queries if str(query).strip()]
        if not cleaned:
            raise SearchError("aucune requête fournie")

        per_query = pages_per_query or self.settings.agent.deep_research_pages_per_query
        limit_pages = max_pages or self.settings.agent.deep_research_max_pages

        sections: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        pages: list[PageContent] = []
        failures: list[dict[str, str]] = []

        for query in cleaned:
            try:
                hits = self.search(query, max_results=max(per_query, self.cfg.max_results_per_query))
            except WebSearchError as exc:
                sections.append({"query": query, "hits": [], "error": str(exc)})
                failures.append({"query": query, "error": str(exc)})
                continue
            sections.append({"query": query, "hits": [hit.to_dict() for hit in hits]})
            for hit in hits[:per_query]:
                if len(pages) >= limit_pages:
                    break
                key = normalize_url(hit.url)
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                try:
                    pages.append(self.fetch(hit.url))
                except WebSearchError as exc:
                    failures.append({"query": query, "url": hit.url, "error": str(exc)})

        dossier = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "queries": cleaned,
            "sections": sections,
            "pages": [page.to_dict(include_text=include_text) for page in pages],
            "sources": [
                {"rank": index + 1, "title": page.title, "url": page.final_url or page.url,
                 "words": page.word_count, "excerpt": page.excerpt}
                for index, page in enumerate(pages)
            ],
            "stats": {
                "queries": len(cleaned),
                "hits": sum(len(section["hits"]) for section in sections),
                "pages": len(pages),
                "failed": len(failures),
            },
            "failures": failures,
        }

        if save_report:
            try:
                dossier["report_path"] = str(self.save_report(dossier, pages))
            except OSError as exc:
                dossier["report_error"] = str(exc)
        return dossier

    def save_report(self, dossier: dict[str, Any], pages: Sequence[PageContent] | None = None) -> Path:
        directory = self.settings.paths.report_dir
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = _slugify(dossier["queries"][0]) if dossier.get("queries") else "recherche"
        path = directory / f"{stamp}-{slug}.md"
        path.write_text(render_report(dossier, pages), encoding="utf-8")
        return path


def _slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^\w\-]+", "-", text.lower(), flags=re.UNICODE).strip("-")
    return (slug[:limit] or "recherche").strip("-")


def render_report(dossier: dict[str, Any], pages: Sequence[PageContent] | None = None) -> str:
    """Rapport Markdown hiérarchisé : requêtes → résultats → sources → extraits."""
    lines: list[str] = []
    queries = dossier.get("queries") or []
    stats = dossier.get("stats") or {}
    lines.append("# Dossier de recherche")
    lines.append("")
    lines.append(f"Généré le {dossier.get('generated_at', '')} — "
                 f"{stats.get('queries', 0)} requête(s), {stats.get('hits', 0)} résultat(s), "
                 f"{stats.get('pages', 0)} page(s) analysée(s).")
    lines.append("")

    lines.append("## Requêtes")
    for index, query in enumerate(queries, start=1):
        lines.append(f"{index}. {query}")
    lines.append("")

    lines.append("## Résultats par requête")
    for index, section in enumerate(dossier.get("sections") or [], start=1):
        lines.append("")
        lines.append(f"### {index}. {section.get('query', '')}")
        if section.get("error"):
            lines.append(f"_Échec : {section['error']}_")
            continue
        for hit in section.get("hits") or []:
            lines.append(f"- **[{hit['title']}]({hit['url']})** — `{hit.get('domain', '')}`")
            if hit.get("snippet"):
                lines.append(f"  {hit['snippet']}")
    lines.append("")

    lines.append("## Sources analysées")
    sources = dossier.get("sources") or []
    if sources:
        lines.append("| # | Source | Mots | Extrait |")
        lines.append("|---|---|---|---|")
        for source in sources:
            excerpt = (source.get("excerpt") or "").replace("|", "\\|")[:200]
            lines.append(
                f"| {source['rank']} | [{source['title'] or source['url']}]({source['url']}) "
                f"| {source['words']} | {excerpt} |"
            )
    else:
        lines.append("_Aucune page récupérée._")
    lines.append("")

    if pages:
        lines.append("## Extraits")
        for index, page in enumerate(pages, start=1):
            lines.append("")
            lines.append(f"### [{index}] {page.title or page.url}")
            lines.append(f"{page.final_url or page.url} — {page.word_count} mots")
            lines.append("")
            lines.append(page.excerpt or "(contenu vide)")

    failures = dossier.get("failures") or []
    if failures:
        lines.append("")
        lines.append("## Échecs")
        for failure in failures:
            target = failure.get("url") or failure.get("query") or ""
            lines.append(f"- `{target}` — {failure.get('error', '')}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Outils exposés au modèle
# ---------------------------------------------------------------------------


def build_web_tools(searcher: WebSearcher) -> list[Tool]:
    """Construit la liste d'outils `web_*` liés à ce moteur de recherche."""

    def web_search(query: str, max_results: int = 8) -> dict[str, Any]:
        hits = searcher.search(query, max_results=max_results)
        return {"query": query, "count": len(hits), "results": [hit.to_dict() for hit in hits]}

    def web_fetch(url: str, max_chars: int = 20000) -> dict[str, Any]:
        page = searcher.fetch(url, max_chars=max_chars)
        return page.to_dict()

    def web_search_and_fetch(query: str, top_n: int = 3) -> dict[str, Any]:
        return searcher.search_and_fetch(query, top_n=top_n)

    def web_deep_research(
        queries: list[str],
        pages_per_query: int = 3,
        save_report: bool = True,
        include_text: bool = False,
    ) -> dict[str, Any]:
        return searcher.deep_research(
            queries,
            pages_per_query=pages_per_query,
            save_report=save_report,
            include_text=include_text,
        )

    return [
        FunctionTool(
            "web_search",
            "Recherche web multi-moteurs (DuckDuckGo, SearxNG, Brave) et retourne titre, URL et extrait dédupliqués.",
            object_schema(
                {
                    "query": param("string", "Requête de recherche"),
                    "max_results": param("integer", "Nombre de résultats", default=8, minimum=1, maximum=30),
                },
                required=["query"],
            ),
            web_search,
        ),
        FunctionTool(
            "web_fetch",
            "Récupère une page web et en extrait le contenu principal (navigation et publicités retirées).",
            object_schema(
                {
                    "url": param("string", "URL http(s) de la page"),
                    "max_chars": param("integer", "Troncature du texte", default=20000, minimum=500, maximum=200000),
                },
                required=["url"],
            ),
            web_fetch,
        ),
        FunctionTool(
            "web_search_and_fetch",
            "Recherche puis récupère les pages les plus pertinentes en un seul appel.",
            object_schema(
                {
                    "query": param("string", "Requête de recherche"),
                    "top_n": param("integer", "Nombre de pages à récupérer", default=3, minimum=1, maximum=8),
                },
                required=["query"],
            ),
            web_search_and_fetch,
        ),
        FunctionTool(
            "web_deep_research",
            "Recherche approfondie : croise 3 à 5 requêtes complémentaires, récupère les pages, déduplique les sources et écrit un dossier Markdown dans data/reports/.",
            object_schema(
                {
                    "queries": param("array", "Requêtes complémentaires (3 à 5 recommandées)",
                                     items=param("string", "Requête"), min_items=1, max_items=10),
                    "pages_per_query": param("integer", "Pages récupérées par requête", default=3, minimum=1, maximum=10),
                    "save_report": param("boolean", "Écrire le dossier Markdown sur disque", default=True),
                    "include_text": param("boolean", "Inclure le texte intégral des pages", default=False),
                },
                required=["queries"],
            ),
            web_deep_research,
        ),
    ]
