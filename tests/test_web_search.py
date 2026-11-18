"""Tests du moteur de recherche web et de l'extraction de contenu."""

from __future__ import annotations

import socket
import sys
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from support import make_settings, run, serve, tmpdir  # noqa: I001

import tools.web_search as web_search
from tools.base import ToolSet
from tools.web_search import (
    FetchError,
    HostBlocked,
    RobotsDenied,
    SearchError,
    WebSearcher,
    build_web_tools,
    normalize_url,
    parse_brave_json,
    parse_duckduckgo_html,
    parse_duckduckgo_lite,
    parse_searx_json,
)

PARAGRAPHS = "".join(
    f"<p>Paragraphe {index} : CONTENU PRINCIPAL UNIQUE décrivant l'architecture des systèmes "
    "distribués, la réplication des journaux, la tolérance aux pannes et la cohérence "
    "eventuelle des répliques sous partition réseau.</p>"
    for index in range(1, 9)
)

ARTICLE_HTML = f"""<!doctype html>
<html lang="fr"><head><title>Analyse des systèmes distribués</title>
<meta name="author" content="Rédaction technique">
<script>var SCRIPT_MARKER = "trace-interne";</script>
</head><body>
<nav>MENU NAVIGATION ACCUEIL CONTACT</nav>
<div class="advert">PUBLICITE ACHETEZ MAINTENANT</div>
<main><article>
<h1>Analyse des systèmes distribués</h1>
{PARAGRAPHS}
</article></main>
<footer>PIED DE PAGE MENTIONS LEGALES</footer>
</body></html>"""

DDG_HTML = """<div class="result results_links">
<a class="result__a" href="https://exemple.fr/a?utm_source=newsletter">Premier résultat</a>
<a class="result__snippet">Extrait du premier résultat.</a></div>
<div class="result results_links">
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexemple.fr%2Fb">Deuxième résultat</a>
<a class="result__snippet">Extrait du second résultat.</a></div>"""

DDG_LITE_HTML = """<table>
<tr><td><a class="result-link" href="https://exemple.fr/c">Troisième résultat</a></td></tr>
<tr><td class="result-snippet">Extrait du résultat lite.</td></tr>
</table>"""

SEARX_PAYLOAD = {
    "results": [
        {"title": "Déjà vu", "url": "https://exemple.fr/a", "content": "doublon"},
        {"title": "Nouveau", "url": "https://exemple.fr/d", "content": "inédit"},
    ]
}

SEARX_HTML = """<div class="results">
<article class="result"><h3><a href="https://exemple.fr/c">Troisième résultat</a></h3>
<p class="content">Extrait du résultat lite.</p></article></div>"""


def test_expired_entries_are_purged_and_fresh_ones_survive() -> None:
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp)
        now = time.monotonic()
        stale = now - web_search._HOST_CACHE_TTL_S - 1
        fresh = now - 1.0

        searcher._ip_cache.update({"vieux.example": (stale, "1.2.3.4"), "frais.example": (fresh, "5.6.7.8")})
        searcher._host_cache.update({"vieux.example": (stale, ""), "frais.example": (fresh, "")})
        searcher._robots.update(
            {
                "http://vieux.example": (None, time.time() - web_search.ROBOTS_TTL_S - 1),
                "http://frais.example": (None, time.time() - 1),
            }
        )
        searcher._failed_ips.update(
            {
                "vieux.example": {"9.9.9.9": stale},
                "mixte.example": {"9.9.9.9": stale, "8.8.8.8": fresh},
            }
        )
        searcher._last_request.update({"vieux.example": stale, "frais.example": fresh})

        removed = searcher._purge_caches(now)

        # 1 ip + 1 host + 1 robots + 1 ip échouée + 1 dans l'hôte mixte + 1 throttle
        assert removed == 6, removed
        assert "vieux.example" not in searcher._ip_cache
        assert "vieux.example" not in searcher._host_cache
        assert "http://vieux.example" not in searcher._robots
        assert "vieux.example" not in searcher._failed_ips
        assert "vieux.example" not in searcher._last_request
        # Ce qui est encore dans le TTL reste utilisable.
        assert searcher._ip_cache["frais.example"] == (fresh, "5.6.7.8")
        assert searcher._host_cache["frais.example"] == (fresh, "")
        assert "http://frais.example" in searcher._robots
        assert searcher._failed_ips["mixte.example"] == {"8.8.8.8": fresh}
        assert searcher._last_request["frais.example"] == fresh


def test_purge_forces_robots_revalidation_and_new_ip_resolution(monkeypatch) -> None:
    """La purge jette l'ancien verdict robots et l'IP, sans casser la revalidation."""
    robots = "User-agent: *\nDisallow: /interdit\n"
    routes = {
        "/robots.txt": (200, "text/plain; charset=utf-8", robots),
        "/page": (200, "text/html; charset=utf-8", "<p>ok</p>"),
    }
    with tmpdir("web") as tmp, serve(routes) as (base, counts):
        searcher = _searcher(tmp)
        origin = "http://" + urlparse(base).netloc

        # 1. Premier passage : robots.txt est lu une fois, et le verdict mémorisé.
        assert searcher.robots_allowed(f"{base}/page") is True
        assert counts["/robots.txt"] == 1, counts
        assert origin in searcher._robots

        # 2. Cache frais : aucun aller-retour supplémentaire.
        assert searcher.robots_allowed(f"{base}/page") is True
        assert counts["/robots.txt"] == 1, counts

        # 3. Verdict périmé (inverse de celui du serveur) : la purge l'enlève.
        stale = RobotFileParser()
        stale.parse(["User-agent: *", "Disallow: /"])
        searcher._robots[origin] = (stale, time.time() - web_search.ROBOTS_TTL_S - 1)
        searcher._purge_caches()
        assert origin not in searcher._robots, "le verdict robots périmé doit partir"

        # 4. Requête suivante : robots.txt est **relu** (pas de faux refus hérité).
        assert searcher.robots_allowed(f"{base}/page") is True
        assert counts["/robots.txt"] == 2, counts
        assert origin in searcher._robots

        # 5. Et un verdict frais est bien respecté, sans nouvelle requête.
        stale_again = RobotFileParser()
        stale_again.parse(["User-agent: *", "Disallow: /"])
        searcher._robots[origin] = (stale_again, time.time())
        assert searcher.robots_allowed(f"{base}/page") is False
        assert counts["/robots.txt"] == 2, counts

        # 6. Une IP épinglée purgée est re-résolue à la requête suivante.
        lookups: list[str] = []

        def fake_getaddrinfo(host, port, *args, **kwargs):
            lookups.append(str(host))
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (f"93.184.216.{len(lookups)}", 0),
                )
            ]

        monkeypatch.setattr(web_search.socket, "getaddrinfo", fake_getaddrinfo)

        first = searcher._pinned_address("revalidation.example", 443)
        assert first == "93.184.216.1" and lookups == ["revalidation.example"]

        # Le cache épinglé évite une seconde résolution tant qu'il est frais.
        assert searcher._pinned_address("revalidation.example", 443) == first
        assert len(lookups) == 1

        # Entrée périmée : la purge l'enlève, la requête suivante résout à nouveau.
        searcher._ip_cache["revalidation.example"] = (
            time.monotonic() - web_search._HOST_CACHE_TTL_S - 1,
            first,
        )
        searcher._purge_caches()
        assert "revalidation.example" not in searcher._ip_cache

        second = searcher._pinned_address("revalidation.example", 443)
        assert len(lookups) == 2, lookups
        assert second == "93.184.216.2" and second != first


def test_caches_have_a_hard_cap_and_keep_the_pinned_ip_in_use() -> None:
    """Plafond dur par cache, sans évincer l'IP épinglée qui vient de servir."""
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp, max_cache_entries=8)
        now = time.monotonic()
        # Une IP épinglée utilisée à l'instant : elle doit survivre à l'éviction.
        searcher._ip_cache["actif.example"] = (now - 1000.0, "203.0.113.9")
        searcher._ip_touched["actif.example"] = now
        for index in range(20):
            host = f"h{index}.example"
            searcher._ip_cache[host] = (now - index, f"198.51.100.{index}")
            searcher._host_cache[host] = (now - index, "")
            searcher._robots[f"https://{host}"] = (None, now - index)
            searcher._failed_ips[host] = {"198.51.100.1": now - index}

        assert searcher._cap_caches(now) > 0
        assert len(searcher._ip_cache) == 8
        assert len(searcher._host_cache) == 8
        assert len(searcher._robots) == 8
        assert len(searcher._failed_ips) == 8
        assert "actif.example" in searcher._ip_cache, "l'IP utilisée à l'instant a été évincée"
        assert "h0.example" in searcher._ip_cache
        assert "h19.example" not in searcher._ip_cache, "l'entrée la plus ancienne part d'abord"

        # Plafond par hôte pour les adresses en échec.
        searcher._failed_ips = {
            "beaucoup.example": {f"192.0.2.{i}": now - i for i in range(40)}
        }
        searcher._cap_caches(now)
        assert len(searcher._failed_ips["beaucoup.example"]) == web_search._FAILED_IPS_PER_HOST

        # Le plafond reste dur même quand toutes les entrées sont protégées.
        small = _searcher(tmp, max_cache_entries=4)
        for index in range(6):
            host = f"p{index}.example"
            small._ip_cache[host] = (now - index, f"203.0.113.{index}")
            small._ip_touched[host] = now
        small._cap_caches(now)
        assert len(small._ip_cache) == 4
        assert "p0.example" in small._ip_cache and "p5.example" not in small._ip_cache


def test_pin_request_stamps_the_pinned_ip_for_the_cap(monkeypatch) -> None:
    """L'usage d'une IP épinglée est daté par la requête réelle, pas à la main."""
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp, max_cache_entries=8)
        host = "actif.example"
        address = "203.0.113.9"

        def fake_getaddrinfo(name, port, *args, **kwargs):
            assert name == host, name
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 443))]

        monkeypatch.setattr(web_search.socket, "getaddrinfo", fake_getaddrinfo)

        # Résolution par le chemin public : l'adresse est mémorisée et datée.
        assert searcher._pinned_address(host, 443) == address
        assert host in searcher._ip_touched

        # L'horodatage de résolution est effacé : seule la requête peut le remettre.
        searcher._ip_touched.pop(host)
        request = httpx.Request("GET", f"https://{host}/page")

        searcher._pin_request(request)

        assert request.url.host == address, "l'URL doit porter l'IP validée"
        assert request.headers["host"] == host, "l'en-tête Host garde le nom d'origine"
        assert request.extensions["sni_hostname"] == host
        stamp = searcher._ip_touched.get(host)
        assert stamp is not None, "la requête doit dater l'usage de l'IP"

        # Plafond dépassé : l'entrée de cet hôte est la plus ancienne du cache,
        # mais elle vient de servir — la protection doit venir de la requête.
        now = time.monotonic()
        searcher._ip_cache[host] = (now - 1000.0, address)
        searcher._ip_touched[host] = stamp
        for index in range(20):
            other = f"h{index}.example"
            searcher._ip_cache[other] = (now - index, f"198.51.100.{index}")
            searcher._host_cache[other] = (now - index, "")

        assert searcher._cap_caches(now) > 0
        assert host in searcher._ip_cache, "l'IP utilisée par une requête a été évincée"
        assert host in searcher._ip_touched
        assert len(searcher._ip_cache) == 8


def test_purge_runs_periodically_not_on_every_call() -> None:
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp)
        stale = time.monotonic() - web_search._HOST_CACHE_TTL_S - 1
        searcher._ip_cache["vieux.example"] = (stale, "1.2.3.4")
        searcher._next_purge = 0.0

        # Une opération publique déclenche la purge due…
        searcher.validate_url("http://93.184.216.34/page")
        assert "vieux.example" not in searcher._ip_cache

        # … puis l'intervalle suivant est respecté : pas de parcours immédiat.
        searcher._ip_cache["encore.example"] = (stale, "4.3.2.1")
        searcher.validate_url("http://93.184.216.34/autre")
        assert "encore.example" in searcher._ip_cache

        searcher._next_purge = 0.0
        searcher.validate_url("http://93.184.216.34/troisieme")
        assert "encore.example" not in searcher._ip_cache


def _searcher(tmp, *, client=None, cache_enabled=False, max_cache_entries=None) -> WebSearcher:
    settings = make_settings(tmp)
    settings.search.per_host_delay_s = 0
    settings.search.request_timeout_s = 5
    settings.search.cache_ttl_s = 60
    settings.search.max_results_per_query = 10
    settings.search.engines = ["duckduckgo_html", "searx"]
    settings.search.searx_instances = ["https://searx.be"]
    # Les fixtures HTTP des tests écoutent sur 127.0.0.1.
    settings.search.allow_private_networks = True
    options = {} if max_cache_entries is None else {"max_cache_entries": max_cache_entries}
    return WebSearcher(settings, client=client, cache_enabled=cache_enabled, **options)


ROUTES = {
    "/article": (200, "text/html; charset=utf-8", ARTICLE_HTML),
    "/private/data": (200, "text/html; charset=utf-8", ARTICLE_HTML),
    "/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /private\n"),
    "/doc.pdf": (200, "application/pdf", b"%PDF-1.4 contenu binaire"),
    "/donnees.json": (200, "application/json", '{"resultat": 42}'),
}


def test_normalize_url_strips_tracking_and_unwraps_redirects() -> None:
    assert normalize_url("https://Exemple.FR/page/?utm_source=x&id=3#ancre") == "https://exemple.fr/page?id=3"
    assert (
        normalize_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexemple.fr%2Fa")
        == "https://exemple.fr/a"
    )
    assert normalize_url("https://exemple.fr/") == "https://exemple.fr/"


def test_parse_duckduckgo_html() -> None:
    hits = parse_duckduckgo_html(DDG_HTML)
    assert len(hits) == 2
    assert hits[0].title == "Premier résultat"
    assert hits[0].url == "https://exemple.fr/a"  # utm_source retiré
    assert hits[1].url == "https://exemple.fr/b"  # redirection déballée
    assert "Extrait du second" in hits[1].snippet


def test_parse_duckduckgo_lite() -> None:
    hits = parse_duckduckgo_lite(DDG_LITE_HTML)
    assert len(hits) == 1
    assert hits[0].url == "https://exemple.fr/c"
    assert "lite" in hits[0].snippet


BRAVE_PAYLOAD = {
    "web": {
        "results": [
            {"title": "Brave", "url": "https://exemple.fr/brave", "description": "d"},
        ]
    }
}


def test_parse_searx_and_brave_payloads() -> None:
    searx = parse_searx_json(SEARX_PAYLOAD)
    assert [hit.url for hit in searx] == ["https://exemple.fr/a", "https://exemple.fr/d"]
    brave = parse_brave_json(
        {"web": {"results": [{"title": "Brave", "url": "https://exemple.fr/e", "description": "d"}]}}
    )
    assert brave[0].url == "https://exemple.fr/e"
    assert brave[0].engine == "brave"


def test_fetch_extracts_main_content() -> None:
    with tmpdir("web") as tmp, serve(ROUTES) as (base, _counts):
        searcher = _searcher(tmp)
        page = searcher.fetch(f"{base}/article")
        assert page.ok
        assert "CONTENU PRINCIPAL UNIQUE" in page.text
        assert "MENU NAVIGATION" not in page.text
        assert "SCRIPT_MARKER" not in page.text
        assert page.word_count > 40
        searcher.close()


def test_fetch_respects_robots_txt() -> None:
    with tmpdir("web") as tmp, serve(ROUTES) as (base, _counts):
        searcher = _searcher(tmp)
        try:
            searcher.fetch(f"{base}/private/data")
        except RobotsDenied:
            pass
        else:
            raise AssertionError("robots.txt aurait dû interdire /private")
        assert searcher.robots_allowed(f"{base}/article") is True
        searcher.close()


def test_fetch_rejects_binary_and_bad_schemes() -> None:
    with tmpdir("web") as tmp, serve(ROUTES) as (base, _counts):
        searcher = _searcher(tmp)
        try:
            searcher.fetch(f"{base}/doc.pdf")
        except FetchError as exc:
            assert "non textuel" in str(exc)
        else:
            raise AssertionError("un PDF ne doit pas être traité comme du texte")
        for candidate in ("ftp://exemple.fr/x", "javascript:alert(1)", ""):
            try:
                searcher.fetch(candidate)
            except FetchError:
                continue
            raise AssertionError(f"URL acceptée à tort : {candidate!r}")
        searcher.close()


def test_fetch_uses_disk_cache() -> None:
    with tmpdir("web") as tmp, serve(ROUTES) as (base, counts):
        searcher = _searcher(tmp, cache_enabled=True)
        first = searcher.fetch(f"{base}/article")
        second = searcher.fetch(f"{base}/article")
        assert first.text == second.text
        assert second.from_cache is True
        assert counts["/article"] == 1, counts
        searcher.close()


def test_fetch_truncates_and_parses_json() -> None:
    with tmpdir("web") as tmp, serve(ROUTES) as (base, _counts):
        searcher = _searcher(tmp)
        short = searcher.fetch(f"{base}/article", max_chars=120)
        assert len(short.text) == 120 and short.truncated is True
        payload = searcher.fetch(f"{base}/donnees.json")
        assert '"resultat": 42' in payload.text
        searcher.close()


def _mock_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://html.duckduckgo.com"):
            return httpx.Response(200, text=DDG_HTML, headers={"content-type": "text/html"})
        if url.startswith("https://searx.be"):
            return httpx.Response(200, json=SEARX_PAYLOAD)
        if url.startswith("https://exemple.fr") and url.endswith("/robots.txt"):
            return httpx.Response(404, text="")
        if url.startswith("https://exemple.fr"):
            return httpx.Response(200, text=ARTICLE_HTML, headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(404, text="")

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_search_deduplicates_across_engines() -> None:
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp, client=_mock_client())
        hits = searcher.search("systèmes distribués", engines=["duckduckgo_html", "searx"])
        urls = [hit.url for hit in hits]
        assert urls == ["https://exemple.fr/a", "https://exemple.fr/b", "https://exemple.fr/d"]
        assert [hit.rank for hit in hits] == [1, 2, 3]
        searcher.close()


def test_private_and_loopback_targets_are_blocked() -> None:
    """Un contenu web ne doit pas pouvoir faire sonder le réseau local à l'agent."""
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp, client=_mock_client())
        searcher.cfg.allow_private_networks = False
        try:
            for url in (
                "http://127.0.0.1:8080/admin",
                "http://localhost:11434/api/chat",
                "http://[::1]:8000/",
                "http://[::ffff:127.0.0.1]/",
                "http://192.168.1.1/",
                "http://10.0.0.5/x",
                "http://172.16.4.2/",
                "http://169.254.169.254/latest/meta-data/",
                "http://0.0.0.0/",
                "http://2130706433/",
                "http://0x7f000001/",
                "http://imprimante.local/",
            ):
                try:
                    searcher.fetch(url)
                except HostBlocked as exc:
                    assert "interne" in str(exc)
                else:
                    raise AssertionError(f"{url} doit être refusé")

            public = "http://93.184.216.34/page"
            assert searcher.validate_url(public) == public

            # Opt-in explicite, pour une cible locale maîtrisée.
            searcher.cfg.allow_private_networks = True
            assert searcher.validate_url("http://127.0.0.1:8080/admin").endswith("/admin")
        finally:
            searcher.close()


def test_fetch_rejects_redirect_to_private_address() -> None:
    """Une page publique ne doit pas pouvoir rebondir vers le réseau local."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/rebond"):
            return httpx.Response(302, headers={"location": "http://127.0.0.1:9/interne"})
        if url.endswith("/rebond-public"):
            return httpx.Response(302, headers={"location": "https://exemple.fr/final"})
        if url.endswith("/final"):
            return httpx.Response(
                200, text="<html><body>contenu public</body></html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )
        return httpx.Response(404, text="")

    # follow_redirects=True côté client : la protection doit venir de la requête.
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp, client=client)
        searcher.cfg.allow_private_networks = False
        try:
            try:
                searcher.fetch("https://exemple.fr/rebond")
            except HostBlocked as exc:
                assert "interne" in str(exc)
            else:
                raise AssertionError("une redirection vers loopback doit être refusée")

            # Une redirection vers une autre URL publique reste suivie.
            page = searcher.fetch("https://exemple.fr/rebond-public")
            assert page.ok and "contenu public" in page.text, page.to_dict(include_text=False)
        finally:
            searcher.close()


def test_request_is_pinned_to_validated_address() -> None:
    """La connexion doit viser l'IP validée, pas une seconde résolution du nom."""
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp, client=_mock_client())
        try:
            searcher._ip_cache["exemple.fr"] = (time.monotonic(), "93.184.216.34")
            request = httpx.Request("GET", "https://exemple.fr/page")
            searcher._pin_request(request)
            assert request.url.host == "93.184.216.34"
            assert request.url.path == "/page"
            assert request.headers["host"] == "exemple.fr"
            assert request.extensions["sni_hostname"] == "exemple.fr"

            # Aucune adresse publique disponible → refus avant toute connexion.
            searcher._ip_cache["interne.test"] = (time.monotonic(), "")
            try:
                searcher._pin_request(httpx.Request("GET", "https://interne.test/x"))
            except httpx.ConnectError:
                pass
            else:
                raise AssertionError("une adresse non résolue doit empêcher la requête")

            # Une IP littérale n'est jamais réécrite.
            literal = httpx.Request("GET", "https://93.184.216.34/page")
            searcher._pin_request(literal)
            assert literal.url.host == "93.184.216.34"
        finally:
            searcher.close()


def test_http_client_wires_the_pinning_hook() -> None:
    """Le hook doit être branché sur le client réel, pas seulement appelable."""
    with tmpdir("web") as tmp:
        # Client construit par _http() (aucun client injecté).
        searcher = WebSearcher(make_settings(tmp), cache_enabled=False)
        try:
            client = searcher._http()
            hooks = client.event_hooks["request"]
            assert any(
                getattr(hook, "__func__", None) is WebSearcher._pin_request for hook in hooks
            ), f"hook d'épinglage absent : {hooks}"

            # Et il s'exécute réellement lors d'une requête émise par le client.
            searcher._ip_cache["exemple.fr"] = (time.monotonic(), "93.184.216.34")
            seen: list[str] = []

            def handler(request: httpx.Request) -> httpx.Response:
                seen.append(str(request.url))
                return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

            client._transport = httpx.MockTransport(handler)
            client.get("https://exemple.fr/page")
            assert seen == ["https://93.184.216.34/page"], seen
        finally:
            searcher.close()


def test_brave_engine_goes_through_the_validated_loop(monkeypatch) -> None:
    """Aucune requête moteur hors de la boucle qui valide chaque saut."""
    monkeypatch.setenv("BRAVE_API_KEY", "jeton-test-brave")
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("x-subscription-token")))
        if "api.search.brave.com" in str(request.url):
            return httpx.Response(302, headers={"location": "http://127.0.0.1:9/brave"})
        return httpx.Response(200, json=BRAVE_PAYLOAD)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp, client=client)
        searcher.cfg.allow_private_networks = False
        try:
            try:
                searcher._engine_brave("test", 5)
            except HostBlocked as exc:
                assert "127.0.0.1" in str(exc) or "interne" in str(exc).lower()
            else:
                raise AssertionError("un rebond Brave vers la boucle locale doit être refusé")

            # Le rebond n'a jamais été dialé, et l'en-tête d'API est bien parti
            # sur la requête initiale, paramètres inclus dans l'URL.
            assert not any("127.0.0.1" in url for url, _ in seen), seen
            first_url, first_token = seen[0]
            assert first_token == "jeton-test-brave"
            assert "q=test" in first_url and "count=5" in first_url
            assert "api.search.brave.com/res/v1/web/search" in first_url
        finally:
            searcher.close()


def test_brave_token_does_not_follow_a_cross_host_redirect(monkeypatch) -> None:
    monkeypatch.setenv("BRAVE_API_KEY", "jeton-test-brave")
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("x-subscription-token")))
        if "api.search.brave.com" in str(request.url):
            return httpx.Response(302, headers={"location": "https://miroir.example/res"})
        return httpx.Response(200, json=BRAVE_PAYLOAD)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp, client=client)
        try:
            hits = searcher._engine_brave("test", 5)
        finally:
            searcher.close()

    assert hits and hits[0].engine == "brave"
    assert [token for _, token in seen] == ["jeton-test-brave", None], seen


def test_robots_txt_read_uses_the_validated_loop() -> None:
    """robots.txt ne peut pas être lue hors de la boucle validée."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(302, headers={"location": "http://127.0.0.1:9/robots.txt"})
        return httpx.Response(200, text=ARTICLE_HTML, headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp, client=client)
        searcher.cfg.respect_robots = True
        searcher.cfg.allow_private_networks = False
        try:
            # Le rebond est refusé ; robots.txt injoignable vaut « absent ».
            assert searcher.robots_allowed("https://robot.example/page") is True
            assert not any("127.0.0.1" in url for url in seen), seen
        finally:
            searcher.close()


def test_missing_robots_txt_is_not_an_error() -> None:
    """Un 404 sur robots.txt est traité comme « robots absent », sans lever."""
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp, client=client)
        searcher.cfg.respect_robots = True
        try:
            assert searcher.robots_allowed("https://absent.example/page") is True
            assert any(url.endswith("/robots.txt") for url in requests), requests
        finally:
            searcher.close()


def test_html_engine_uses_the_validated_loop() -> None:
    """DuckDuckGo HTML/Bing ne doivent plus suivre une redirection sans contrôle."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if "duckduckgo.com" in url:
            return httpx.Response(302, headers={"location": "http://127.0.0.1:9/html"})
        return httpx.Response(200, text=DDG_HTML, headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp, client=client)
        searcher.cfg.allow_private_networks = False
        try:
            try:
                searcher._run_engine("duckduckgo_html", "test", 5)
            except HostBlocked:
                pass
            else:
                raise AssertionError("un rebond vers la boucle locale doit être refusé")
            assert not any("127.0.0.1" in url for url in seen), seen

            # Une redirection publique reste suivie et la page est interprétée.
            def public_handler(request: httpx.Request) -> httpx.Response:
                if "duckduckgo.com" in str(request.url):
                    return httpx.Response(302, headers={"location": "https://relais.example/ddg"})
                return httpx.Response(200, text=DDG_HTML, headers={"content-type": "text/html"})

            searcher._http()._transport = httpx.MockTransport(public_handler)
            hits = searcher._run_engine("duckduckgo_html", "test", 5)
            assert hits and hits[0].url.startswith("https://exemple.fr/a")
        finally:
            searcher.close()


ANOMALY_HTML = """<html><head><title>DuckDuckGo</title></head><body>
<div class="anomaly-modal__title">Unfortunately, bots use DuckDuckGo too.</div>
<p>Please complete the following challenge to confirm this search was made by a human.</p>
</body></html>"""


def test_duckduckgo_anomaly_page_is_reported_as_blocked() -> None:
    """Un 202 « anomaly » donne « bloqué par le moteur », pas une page illisible."""
    for engine in ("duckduckgo_html", "duckduckgo_lite"):
        with tmpdir("web") as tmp:
            searcher = _searcher(tmp)
            try:
                searcher._http()._transport = httpx.MockTransport(
                    lambda request: httpx.Response(
                        202, text=ANOMALY_HTML, headers={"content-type": "text/html"}
                    )
                )
                try:
                    searcher._run_engine(engine, "test", 5)
                except SearchError as exc:
                    message = str(exc)
                    assert "bloqué par le moteur" in message, message
                    assert engine in message, message
                    assert "HTTP 202" in message, message
                else:
                    raise AssertionError(f"{engine} : une page 202 doit lever SearchError")

                # Le motif remonte aussi jusqu'à l'appelant de `search`.
                try:
                    searcher.search("test", engines=[engine])
                except SearchError as exc:
                    assert "bloqué par le moteur" in str(exc), str(exc)
                else:
                    raise AssertionError("search() doit relayer le blocage")
            finally:
                searcher.close()


def test_anomaly_marker_on_a_200_page_is_also_detected() -> None:
    """Le formulaire anomaly est reconnu même quand le statut reste 200."""
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp)
        try:
            searcher._http()._transport = httpx.MockTransport(
                lambda request: httpx.Response(
                    200, text=ANOMALY_HTML, headers={"content-type": "text/html"}
                )
            )
            try:
                searcher._run_engine("duckduckgo_html", "test", 5)
            except SearchError as exc:
                assert "bloqué par le moteur duckduckgo_html" in str(exc), str(exc)
            else:
                raise AssertionError("la page anomaly doit étre détectée même en HTTP 200")
        finally:
            searcher.close()


def test_failed_addresses_are_skipped_until_expiry(monkeypatch) -> None:
    """Après un échec, la résolution suivante ne redonne pas la même IP."""
    addresses = ["93.184.216.34", "93.184.216.35"]

    def fake_getaddrinfo(host, port, *args, **kwargs):
        # Le résolveur renvoie toujours les deux adresses dans le même ordre :
        # seule la mémoire des échecs peut faire changer la sélection.
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 443))
            for address in addresses
        ]

    monkeypatch.setattr(web_search.socket, "getaddrinfo", fake_getaddrinfo)

    with tmpdir("web") as tmp:
        searcher = _searcher(tmp)
        try:
            assert searcher._pinned_address("exemple.fr", 443) == "93.184.216.34"

            searcher._forget_pinned("https://exemple.fr/page")
            assert "exemple.fr" not in searcher._ip_cache
            assert searcher._pinned_address("exemple.fr", 443) == "93.184.216.35"

            # Les deux adresses ont échoué : l'ensemble est réessayé.
            searcher._forget_pinned("https://exemple.fr/page")
            assert searcher._pinned_address("exemple.fr", 443) == "93.184.216.34"

            # Une exclusion expirée n'est plus prise en compte.
            searcher._ip_cache.pop("exemple.fr", None)
            searcher._failed_ips = {"exemple.fr": {"93.184.216.34": time.monotonic() - 10_000}}
            assert searcher._pinned_address("exemple.fr", 443) == "93.184.216.34"
        finally:
            searcher.close()


def test_connect_failure_drops_the_pinned_address() -> None:
    """Une IP épinglée injoignable ne doit pas rester imposée pendant le TTL."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("injoignable", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp, client=client)
        searcher._ip_cache["exemple.fr"] = (time.monotonic(), "93.184.216.34")
        try:
            try:
                searcher.fetch("https://exemple.fr/page", use_cache=False)
            except FetchError as exc:
                assert "récupération impossible" in str(exc)
            else:
                raise AssertionError("une connexion impossible doit lever FetchError")
            assert "exemple.fr" not in searcher._ip_cache, "l'IP épinglée aurait dû être oubliée"
        finally:
            searcher.close()


def test_searx_instance_goes_through_ssrf_filter() -> None:
    with tmpdir("web") as tmp:
        client = httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=SEARX_PAYLOAD)),
            follow_redirects=True,
        )
        searcher = _searcher(tmp, client=client)
        searcher.cfg.allow_private_networks = False
        searcher.cfg.searx_instances = ["http://127.0.0.1:8888"]
        try:
            for instance in ("http://127.0.0.1:8888", "http://192.168.1.10:8888"):
                searcher.cfg.searx_instances = [instance]
                try:
                    searcher.search("test", engines=["searx"])
                except SearchError as exc:
                    assert "interne" in str(exc)
                else:
                    raise AssertionError(f"l'instance {instance} doit être refusée")
            assert searcher.request_count == 0, "aucune requête HTTP ne doit être émise"

            # Opt-in explicite : une instance auto-hébergée reste utilisable.
            searcher.cfg.allow_private_networks = True
            searcher.cfg.searx_instances = ["http://127.0.0.1:8888"]
            hits = searcher.search("test", engines=["searx"])
            assert hits and hits[0].engine == "searx"
        finally:
            searcher.close()


def test_searx_instance_cannot_redirect_to_local_network() -> None:
    """Une instance configurée ne doit pas pouvoir rebondir vers la boucle locale."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if url.startswith("https://searx.example/relais"):
            return httpx.Response(302, headers={"location": "https://searx.example/final"})
        if url.startswith("https://searx.example/final"):
            return httpx.Response(200, json=SEARX_PAYLOAD)
        if url.startswith("https://searx.example/search"):
            return httpx.Response(302, headers={"location": "http://127.0.0.1:9/interne"})
        if url.endswith("/interne"):
            return httpx.Response(200, json=SEARX_PAYLOAD)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp, client=client)
        searcher.cfg.allow_private_networks = False
        searcher.cfg.searx_instances = ["https://searx.example"]
        try:
            try:
                searcher.search("test", engines=["searx"])
            except SearchError as exc:
                assert "interne" in str(exc)
            else:
                raise AssertionError("un rebond vers la boucle locale doit être refusé")
            assert not any("127.0.0.1" in url for url in seen), seen

            # Un rebond vers une URL publique reste suivi.
            searcher.cfg.searx_instances = ["https://searx.example/relais"]
            hits = searcher._engine_searx("test", 5)
            assert hits and hits[0].engine == "searx"
        finally:
            searcher.close()


def test_search_reports_total_failure() -> None:
    with tmpdir("web") as tmp:
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        searcher = _searcher(tmp, client=client)
        try:
            searcher.search("requête impossible", engines=["duckduckgo_html"])
        except SearchError as exc:
            assert "infructueuse" in str(exc)
            return
        finally:
            searcher.close()
        raise AssertionError("une recherche sans résultat doit lever SearchError")


def test_deep_research_builds_hierarchical_report() -> None:
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp, client=_mock_client())
        dossier = searcher.deep_research(
            ["systèmes distribués", "consensus Raft"], pages_per_query=1, save_report=True
        )
        assert dossier["stats"]["queries"] == 2
        assert dossier["stats"]["pages"] >= 1
        assert dossier["sources"]
        report_path = dossier.get("report_path")
        assert report_path and report_path.endswith(".md")
        content = open(report_path, encoding="utf-8").read()
        for heading in ("# Dossier de recherche", "## Requêtes", "## Résultats par requête", "## Sources analysées"):
            assert heading in content
        searcher.close()


def test_deep_research_rejects_empty_queries() -> None:
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp, client=_mock_client())
        try:
            searcher.deep_research(["   ", ""])
        except SearchError:
            return
        finally:
            searcher.close()
        raise AssertionError("une liste de requêtes vide doit être refusée")


def test_web_tools_are_exposed_and_dispatch() -> None:
    with tmpdir("web") as tmp:
        searcher = _searcher(tmp, client=_mock_client())
        tools = ToolSet(build_web_tools(searcher))
        assert tools.names() == [
            "web_deep_research",
            "web_fetch",
            "web_search",
            "web_search_and_fetch",
        ]
        result = tools.dispatch("web_search", {"query": "test", "max_results": 2})
        assert result.ok and result.data["count"] == 2
        missing = tools.dispatch("web_search", {})
        assert missing.ok is False and missing.error_code == "invalid_arguments"
        searcher.close()


if __name__ == "__main__":
    sys.exit(run(globals()))
