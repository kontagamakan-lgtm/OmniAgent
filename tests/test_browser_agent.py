"""Tests du pilote de navigateur Playwright (headless, serveur local)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from support import make_settings, run, serve, tmpdir  # noqa: I001

from tools.base import ToolSet
from tools.browser_agent import (
    BrowserAgent,
    BrowserError,
    BrowserUnavailable,
    LoginError,
    NavigationError,
    SelectorError,
    build_browser_tools,
    bundled_browser_available,
    ensure_browsers_path,
    ensure_system_node,
    resolved_channel,
    system_browser_channel,
)
from tools.vault import Vault, VaultNotFound

PASSPHRASE = "Correct-Horse-Battery-9!"

FORM_PAGE = """<!doctype html><html><head><title>Formulaire</title></head><body>
<h1>Formulaire de test</h1>
<form id="f">
  <input id="nom" name="nom" type="text">
  <input id="mail" name="email" type="email">
  <button id="go" type="button"
    onclick="document.getElementById('res').textContent='ENVOYE:'+document.getElementById('nom').value">
    Envoyer
  </button>
</form>
<div id="res">vide</div>
</body></html>"""

SPA_PAGE = """<!doctype html><html><head><title>Application</title></head><body>
<div id="app">chargement…</div>
<script>document.getElementById('app').textContent = 'CONTENU DYNAMIQUE RENDU';</script>
</body></html>"""

LOGIN_FORM = """
<form action="/auth" method="post">
  <input name="username" type="text" autocomplete="username">
  <input name="password" type="password" autocomplete="current-password">
  <button type="submit">Se connecter</button>
</form>"""

SIGNUP_PAGE = """<!doctype html><html><head><title>Inscription</title></head><body>
<h1>Créer un compte</h1>
<form action="/inscription" method="post">
  <input name="username" type="text" autocomplete="username">
  <input name="password" type="password" autocomplete="new-password">
  <input name="password_confirm" type="password" autocomplete="new-password">
  <button type="submit">Créer le compte</button>
</form></body></html>"""


SIGNUP_REJECT_PAGE = """<!doctype html><html><head><title>Inscription</title></head><body>
<h1>Créer un compte</h1>
<p class="erreur">Cette adresse e-mail est déjà utilisée.</p>
<form action="/inscription" method="post">
  <input name="username" type="text" autocomplete="username">
  <input name="password" type="password" autocomplete="new-password">
  <input name="password_confirm" type="password" autocomplete="new-password">
  <button type="submit">Créer le compte</button>
</form></body></html>"""


def _signup(form: dict[str, str]) -> tuple[int, str, str]:
    password = form.get("password", "")
    if password and password == form.get("password_confirm"):
        return 200, "text/html; charset=utf-8", SUCCESS_PAGE
    return 200, "text/html; charset=utf-8", SIGNUP_PAGE


LOGIN_PAGE = f"<!doctype html><html><head><title>Connexion</title></head><body><h1>Connexion</h1>{LOGIN_FORM}</body></html>"
SUCCESS_PAGE = "<!doctype html><html><head><title>Accueil</title></head><body><h1>Bienvenue jean — connexion réussie.</h1></body></html>"
ERROR_PAGE = f"<!doctype html><html><head><title>Connexion</title></head><body><p>Identifiants incorrects.</p>{LOGIN_FORM}</body></html>"


def _settings(tmp):
    settings = make_settings(tmp)
    settings.browser.headless = True
    settings.browser.slow_mo_ms = 0
    settings.browser.action_timeout_ms = 8000
    settings.browser.navigation_timeout_ms = 15000
    return settings


# Comptes réellement acceptés par le serveur de test : le mot de passe du test
# d'inscription y figure, sinon la connexion de contrôle échouerait toujours.
_ACCOUNTS = {"jean": {"MotDePasse#1", "Nouveau#Secret42"}}


def _auth(form: dict[str, str]) -> tuple[int, str, str]:
    if form.get("password") in _ACCOUNTS.get(form.get("username", ""), set()):
        return 200, "text/html; charset=utf-8", SUCCESS_PAGE
    return 200, "text/html; charset=utf-8", ERROR_PAGE


ROUTES = {
    "/form": (200, "text/html; charset=utf-8", FORM_PAGE),
    "/spa": (200, "text/html; charset=utf-8", SPA_PAGE),
    "/login": (200, "text/html; charset=utf-8", LOGIN_PAGE),
    "/inscription": (200, "text/html; charset=utf-8", SIGNUP_PAGE),
}
POST_ROUTES = {"/auth": _auth, "/inscription": _signup}


def test_goto_rejects_non_http_schemes() -> None:
    """`file:` et `data:` doivent être refusés : ils contourneraient le confinement."""
    with tmpdir("browser") as tmp:
        agent = BrowserAgent(_settings(tmp))
        try:
            for url in (
                "data:text/html,<h1>Bonjour le monde</h1>",
                "file:///C:/Windows/win.ini",
                "javascript:alert(1)",
            ):
                try:
                    agent.goto(url)
                except NavigationError as exc:
                    assert "non autorisé" in str(exc)
                else:
                    raise AssertionError(f"le schéma de {url!r} doit être refusé")
        finally:
            agent.stop()


def test_download_filename_is_confined() -> None:
    with tmpdir("browser") as tmp:
        agent = BrowserAgent(_settings(tmp))
        try:
            assert agent._safe_download_name(r"C:\Windows\System32\evil.bat") == "evil.bat"
            assert agent._safe_download_name("../../evil.sh") == "evil.sh"
            assert agent._safe_download_name("/etc/passwd") == "passwd"
            for rejected in ("", ".", "..", "bad|name.txt"):
                try:
                    agent._safe_download_name(rejected)
                except BrowserError as exc:
                    assert "invalide" in str(exc)
                else:
                    raise AssertionError(f"{rejected!r} doit être refusé")
        finally:
            agent.stop()


def test_fill_form_and_click() -> None:
    with tmpdir("browser") as tmp, serve(ROUTES, post_routes=POST_ROUTES) as (base, _counts):
        agent = BrowserAgent(_settings(tmp))
        try:
            agent.goto(f"{base}/form")
            filled = agent.fill_form("#f", {"#nom": "Jean", "#mail": "jean@exemple.fr"})
            assert sorted(filled["filled"]) == ["#mail", "#nom"]
            assert filled["errors"] == []
            agent.click_element("#go")
            assert "ENVOYE:Jean" in agent.text()["text"]
        finally:
            agent.stop()


def test_click_reports_invalid_selector() -> None:
    with tmpdir("browser") as tmp, serve(ROUTES) as (base, _counts):
        agent = BrowserAgent(_settings(tmp))
        try:
            agent.goto(f"{base}/form")
            try:
                agent.click_element("#inexistant")
            except SelectorError as exc:
                assert "aucun élément" in str(exc)
            else:
                raise AssertionError("un sélecteur absent doit lever SelectorError")
        finally:
            agent.stop()


def test_fetch_rendered_executes_javascript() -> None:
    with tmpdir("browser") as tmp, serve(ROUTES) as (base, _counts):
        agent = BrowserAgent(_settings(tmp))
        try:
            page = agent.fetch_rendered(f"{base}/spa")
            assert "CONTENU DYNAMIQUE RENDU" in page["text"]
            assert "chargement" not in page["text"]
        finally:
            agent.stop()


def test_screenshot_and_cookies() -> None:
    with tmpdir("browser") as tmp, serve(ROUTES) as (base, _counts):
        agent = BrowserAgent(_settings(tmp))
        try:
            agent.goto(f"{base}/form")
            agent.page.evaluate("document.cookie = 'session=abc123; path=/'")
            cookies = agent.extract_cookies()
            assert cookies["count"] >= 1
            assert any(cookie["name"] == "session" for cookie in cookies["cookies"])

            shot = agent.screenshot("preuve.png")
            assert shot["bytes"] > 0
            assert shot["path"].endswith("preuve.png")
        finally:
            agent.stop()


def test_screenshot_cannot_escape_or_overwrite() -> None:
    with tmpdir("browser") as tmp, serve(ROUTES) as (base, _counts):
        settings = _settings(tmp)
        vault = Vault(settings)
        vault.create(PASSPHRASE)
        vault_bytes = settings.paths.vault_file.read_bytes()

        agent = BrowserAgent(settings, vault)
        try:
            agent.goto(f"{base}/form")
            for escape in ("../../vault.enc", "../vault.enc"):
                try:
                    agent.screenshot(escape)
                except BrowserError as exc:
                    assert "relatif" in str(exc)
                else:
                    raise AssertionError(f"{escape} doit être refusé")
            assert settings.paths.vault_file.read_bytes() == vault_bytes, "le coffre a été modifié"

            first = agent.screenshot("preuve.png")
            assert Path(first["path"]).is_file()
            try:
                agent.screenshot("preuve.png")
            except BrowserError as exc:
                assert "existe déjà" in str(exc)
            else:
                raise AssertionError("une capture ne doit pas écraser un fichier existant")
        finally:
            agent.stop()


def test_cookie_tool_result_is_withheld_from_model() -> None:
    with tmpdir("browser") as tmp, serve(ROUTES) as (base, _counts):
        agent = BrowserAgent(_settings(tmp))
        try:
            agent.goto(f"{base}/form")
            agent.page.evaluate("document.cookie = 'session=secret-cookie-123; path=/';")
            tools = ToolSet(build_browser_tools(agent))
            result = tools.dispatch("extract_cookies", {})
            assert result.ok and result.sensitive is True
            # Le code local garde les valeurs…
            assert any(cookie["value"] == "secret-cookie-123" for cookie in result.data["cookies"])

            # …mais le modèle ne reçoit qu'un résumé : contexte sans secrets.
            text = result.to_text()
            assert "secret-cookie-123" not in text
            assert "cookie(s)" in text
            assert "session" in text  # nom conservé
            assert "127.0.0.1" in text  # domaine conservé
            assert "secret-cookie-123" not in json.dumps(result.to_dict())
        finally:
            agent.stop()


def test_signup_form_is_filled_from_vault_without_leaking() -> None:
    with tmpdir("browser") as tmp, serve(ROUTES, post_routes=POST_ROUTES) as (base, _counts):
        settings = _settings(tmp)
        vault = Vault(settings)
        vault.create(PASSPHRASE)
        vault.add("forum.local", "jean", "Nouveau#Secret42", url=f"{base}/inscription")

        agent = BrowserAgent(settings, vault)
        try:
            tools = ToolSet(build_browser_tools(agent))
            result = tools.dispatch("browser_fill_signup", {"service_name": "forum.local"})
            assert result.ok, result.error

            # Les deux champs portent bien la valeur du coffre…
            values = agent.page.evaluate(
                "Array.from(document.querySelectorAll('input[type=password]'))"
                ".map(input => input.value)"
            )
            assert values == ["Nouveau#Secret42", "Nouveau#Secret42"], values
            assert agent.page.evaluate(
                "document.querySelector('input[name=username]').value"
            ) == "jean"

            # …mais elle n'apparaît ni dans la vue sérialisée ni dans le texte.
            assert result.data["password_fields"] == 2
            assert result.data["filled"] == ["username", "password", "password_confirm"]
            assert result.data["submitted"] is False
            text = result.to_text()
            assert "Nouveau#Secret42" not in text
            assert "Nouveau#Secret42" not in json.dumps(result.to_dict())
            assert "forum.local" in text and "jean" in text
        finally:
            agent.stop()


def test_signup_submit_really_posts_without_leaking() -> None:
    received: list[dict[str, str]] = []

    def recording_signup(form: dict[str, str]) -> tuple[int, str, str]:
        received.append(dict(form))
        return _signup(form)

    with tmpdir("browser") as tmp, serve(
        ROUTES, post_routes={**POST_ROUTES, "/inscription": recording_signup}
    ) as (base, _counts):
        settings = _settings(tmp)
        vault = Vault(settings)
        vault.create(PASSPHRASE)
        vault.add("forum.local", "jean", "Nouveau#Secret42", url=f"{base}/inscription")

        agent = BrowserAgent(settings, vault)
        try:
            tools = ToolSet(build_browser_tools(agent))
            result = tools.dispatch(
                "browser_fill_signup", {"service_name": "forum.local", "submit": True}
            )
            assert result.ok, result.error
            assert result.data["submitted"] is True

            # Le serveur a bien reçu le formulaire complet, avec la valeur du coffre.
            assert received == [
                {
                    "username": "jean",
                    "password": "Nouveau#Secret42",
                    "password_confirm": "Nouveau#Secret42",
                }
            ], received

            # La soumission a abouti (page de succès), sans exposer le secret.
            assert "Bienvenue" in agent.page.content()
            assert "Nouveau#Secret42" not in agent.page.content()
            assert result.data["created"] is True, result.data
            assert result.data["signup_status"] == "created"
            assert "confirmation" in result.data["evidence"]

            # Le verdict est journalisé… puis la connexion de contrôle aboutit.
            audit = vault.read_audit()
            signup_records = [record for record in audit if record["action"] == "signup"]
            assert len(signup_records) == 1, audit
            record = signup_records[0]
            assert record["success"] is True
            assert record["service"] == "forum.local" and record["status"] == "created"
            assert "confirmation" in record["evidence"]
            assert "Nouveau#Secret42" not in json.dumps(audit, ensure_ascii=False)

            assert result.data["verified"] is True, result.data
            assert "connexion confirmée" in result.data["verification"]
            # Le journal est relu du plus récent au plus ancien.
            chronological = [item["action"] for item in reversed(audit)]
            assert "signup" in chronological and "verified" in chronological
            assert chronological.index("signup") < chronological.index("verified")

            entry = vault.find("forum.local")
            assert entry is not None and entry.verified_at

            # La connexion de contrôle n'a pas fait fuiter le secret non plus.
            assert "Nouveau#Secret42" not in result.to_text()
            assert "Nouveau#Secret42" not in json.dumps(result.data)

            text = result.to_text()
            assert "Nouveau#Secret42" not in text
            assert "Nouveau#Secret42" not in json.dumps(result.to_dict())
            assert "forum.local" in text and "jean" in text
        finally:
            agent.stop()


def test_signup_is_marked_verified_only_after_a_successful_login() -> None:
    """Une page « compte créé » ne suffit pas : sans connexion, rien n'est vérifié."""

    def always_reject_auth(_form: dict[str, str]) -> tuple[int, str, str]:
        return 200, "text/html; charset=utf-8", ERROR_PAGE

    with tmpdir("browser") as tmp, serve(
        ROUTES, post_routes={**POST_ROUTES, "/auth": always_reject_auth}
    ) as (base, _counts):
        settings = _settings(tmp)
        vault = Vault(settings)
        vault.create(PASSPHRASE)
        vault.add("forum.local", "jean", "Nouveau#Secret42", url=f"{base}/inscription")
        baseline = vault.find("forum.local")
        assert baseline is not None
        baseline_uses = baseline.use_count
        baseline_records = sum(
            1 for item in vault.read_audit() if item["action"] == "credentials"
        )

        agent = BrowserAgent(settings, vault)
        try:
            tools = ToolSet(build_browser_tools(agent))
            result = tools.dispatch(
                "browser_fill_signup", {"service_name": "forum.local", "submit": True}
            )
            assert result.ok, result.error
            assert result.data["created"] is True, result.data
            assert result.data["verified"] is False, result.data
            assert result.data["verification"], "la raison du refus doit être rapportée"

            entry = vault.find("forum.local")
            assert entry is not None and entry.verified_at == ""
            audit = vault.read_audit()
            assert any(item["action"] == "signup" for item in audit)
            assert not any(item["action"] == "verified" for item in audit), audit
            # Remplir un formulaire d'inscription n'authentifie personne : ni le
            # remplissage ni les tentatives de connexion de contrôle ne comptent
            # un usage des identifiants, ni n'écrivent d'audit « credentials ».
            credentials_records = [
                item for item in audit if item["action"] == "credentials"
            ]
            assert entry.use_count == baseline_uses, entry.use_count
            assert len(credentials_records) == baseline_records, audit
            assert "Nouveau#Secret42" not in json.dumps(audit, ensure_ascii=False)
        finally:
            agent.stop()


# Le formulaire de connexion vit sur un autre chemin que /login : la
# vérification ne peut réussir que si l'URL lui est fournie explicitement.
OFF_LOGIN_ROUTES = {route: value for route, value in ROUTES.items() if route != "/login"}
OFF_LOGIN_ROUTES["/compte/se-connecter"] = (200, "text/html; charset=utf-8", LOGIN_PAGE)


def test_signup_verification_uses_an_explicit_login_url() -> None:
    with tmpdir("browser") as tmp, serve(
        OFF_LOGIN_ROUTES, post_routes=POST_ROUTES
    ) as (base, _counts):
        settings = _settings(tmp)
        vault = Vault(settings)
        vault.create(PASSPHRASE)
        vault.add("forum.local", "jean", "Nouveau#Secret42", url=f"{base}/inscription")

        agent = BrowserAgent(settings, vault)
        try:
            tools = ToolSet(build_browser_tools(agent))
            result = tools.dispatch(
                "browser_fill_signup",
                {
                    "service_name": "forum.local",
                    "submit": True,
                    "login_url": f"{base}/compte/se-connecter",
                },
            )
            assert result.ok, result.error
            assert result.data["created"] is True, result.data
            assert result.data["verified"] is True, result.data
            assert "/compte/se-connecter" in result.data["verification"]

            entry = vault.find("forum.local")
            assert entry is not None and entry.verified_at
        finally:
            agent.stop()


def test_signup_verification_reuses_the_login_url_of_the_vault() -> None:
    with tmpdir("browser") as tmp, serve(
        OFF_LOGIN_ROUTES, post_routes=POST_ROUTES
    ) as (base, _counts):
        settings = _settings(tmp)
        vault = Vault(settings)
        vault.create(PASSPHRASE)
        vault.add(
            "forum.local",
            "jean",
            "Nouveau#Secret42",
            url=f"{base}/inscription",
            login_url=f"{base}/compte/se-connecter",
        )
        assert vault.find("forum.local").login_url.endswith("/compte/se-connecter")

        agent = BrowserAgent(settings, vault)
        try:
            tools = ToolSet(build_browser_tools(agent))
            # Aucune URL passée à l'appel : c'est celle du coffre qui sert.
            result = tools.dispatch(
                "browser_fill_signup", {"service_name": "forum.local", "submit": True}
            )
            assert result.ok, result.error
            assert result.data["verified"] is True, result.data
            assert "/compte/se-connecter" in result.data["verification"]
        finally:
            agent.stop()


def test_signup_submit_reports_rejection() -> None:
    def reject(_form: dict[str, str]) -> tuple[int, str, str]:
        return 200, "text/html; charset=utf-8", SIGNUP_REJECT_PAGE

    with tmpdir("browser") as tmp, serve(
        ROUTES, post_routes={**POST_ROUTES, "/inscription": reject}
    ) as (base, _counts):
        settings = _settings(tmp)
        vault = Vault(settings)
        vault.create(PASSPHRASE)
        vault.add("pris.local", "jean", "Nouveau#Secret42", url=f"{base}/inscription")

        agent = BrowserAgent(settings, vault)
        try:
            tools = ToolSet(build_browser_tools(agent))
            result = tools.dispatch(
                "browser_fill_signup", {"service_name": "pris.local", "submit": True}
            )
            assert result.ok, result.error
            assert result.data["submitted"] is True
            # La soumission a eu lieu mais le compte n'existe pas.
            assert result.data["created"] is False
            assert result.data["signup_status"] == "rejected"
            assert "déjà utilisé" in result.data["evidence"], result.data["evidence"]
            assert result.data["url_changed"] is False

            # Le refus est journalisé, et l'entrée reste non vérifiée.
            audit = vault.read_audit()
            signup_records = [record for record in audit if record["action"] == "signup"]
            assert len(signup_records) == 1, audit
            assert signup_records[0]["success"] is False
            assert signup_records[0]["status"] == "rejected"
            entry = vault.find("pris.local")
            assert entry is not None and entry.verified_at == ""
            assert not any(item["action"] == "verified" for item in audit)
            assert "Nouveau#Secret42" not in json.dumps(audit, ensure_ascii=False)

            # Le diagnostic ne contient pas le mot de passe.
            assert "Nouveau#Secret42" not in result.to_text()
            assert "Nouveau#Secret42" not in json.dumps(result.data)
        finally:
            agent.stop()


def test_login_success_uses_vault_secret() -> None:
    with tmpdir("browser") as tmp, serve(ROUTES, post_routes=POST_ROUTES) as (base, _counts):
        settings = _settings(tmp)
        vault = Vault(settings)
        vault.create(PASSPHRASE)
        vault.add("site.local", "jean", "MotDePasse#1", url=f"{base}/login")

        agent = BrowserAgent(settings, vault)
        try:
            result = agent.login_to_service("site.local")
            assert result["logged_in"] is True, result
            assert result["submitted"] is True
            assert result["totp_used"] is False
            assert "MotDePasse#1" not in json.dumps(result)

            # Une connexion réussie reste un usage réel : comptée et auditée.
            entry = vault.find("site.local")
            assert entry is not None and entry.use_count == 1
            assert entry.last_used_at
            audit = vault.read_audit()
            assert any(item["action"] == "credentials" for item in audit), audit
        finally:
            agent.stop()


def test_login_falls_back_to_the_login_url_of_the_vault() -> None:
    """Sans `url`, l'entrée dont seul `login_url` est renseigné se connecte."""
    with tmpdir("browser") as tmp, serve(ROUTES, post_routes=POST_ROUTES) as (base, _counts):
        settings = _settings(tmp)
        vault = Vault(settings)
        vault.create(PASSPHRASE)
        entry = vault.add(
            "login-url.local", "jean", "MotDePasse#1", login_url=f"{base}/login"
        )
        assert entry.url == "", entry.url
        assert entry.login_url.endswith("/login"), entry.login_url

        agent = BrowserAgent(settings, vault)
        try:
            # Aucune URL passée : seule `login_url` peut mener au formulaire.
            # L'origine déduite du nom (`https://login-url.local`) n'existerait pas.
            result = agent.login_to_service("login-url.local")
            assert result["logged_in"] is True, result
            assert result["title"] == "Accueil", result
            assert result["url"].endswith("/auth"), result
        finally:
            agent.stop()


def test_login_failure_is_detected() -> None:
    with tmpdir("browser") as tmp, serve(ROUTES, post_routes=POST_ROUTES) as (base, _counts):
        settings = _settings(tmp)
        vault = Vault(settings)
        vault.create(PASSPHRASE)
        vault.add("echec.local", "jean", "mauvais-mot-de-passe", url=f"{base}/login")

        agent = BrowserAgent(settings, vault)
        try:
            result = agent.login_to_service("echec.local")
            assert result["logged_in"] is False, result
            assert result["evidence"]
            assert "mauvais-mot-de-passe" not in json.dumps(result)

            # Un échec d'authentification n'est pas un usage des identifiants.
            entry = vault.find("echec.local")
            assert entry is not None and entry.use_count == 0
            assert entry.last_used_at == ""
            audit = vault.read_audit()
            assert not any(item["action"] == "credentials" for item in audit), audit
        finally:
            agent.stop()


def test_login_requires_vault_and_credentials() -> None:
    with tmpdir("browser") as tmp, serve(ROUTES) as (base, _counts):
        settings = _settings(tmp)
        agent = BrowserAgent(settings, None)
        try:
            try:
                agent.login_to_service("site.local")
            except LoginError:
                pass
            else:
                raise AssertionError("sans coffre-fort la connexion doit échouer")
        finally:
            agent.stop()

        vault = Vault(settings)
        vault.create(PASSPHRASE)
        agent = BrowserAgent(settings, vault)
        try:
            try:
                agent.login_to_service("inconnu.local")
            except VaultNotFound:
                pass
            else:
                raise AssertionError("un service absent du coffre doit lever VaultNotFound")
        finally:
            agent.stop()


def test_status_and_close() -> None:
    with tmpdir("browser") as tmp:
        agent = BrowserAgent(_settings(tmp))
        assert agent.status().running is False
        agent.start()
        status = agent.status()
        assert status.running is True
        agent.stop()
        assert agent.running is False


def test_browser_tools_expose_required_names() -> None:
    with tmpdir("browser") as tmp:
        tools = ToolSet(build_browser_tools(BrowserAgent(_settings(tmp))))
        names = tools.names()
        for required in ("login_to_service", "fill_form", "click_element", "extract_cookies"):
            assert required in names, names

        bad = tools.dispatch("click_element", {})
        assert bad.ok is False and bad.error_code == "invalid_arguments"


def test_bundled_driver_node_wins_over_the_system_one(tmp_path, monkeypatch) -> None:
    """Le pilote embarqué reste utilisé : aucune variable n'est posée."""
    monkeypatch.delenv("PLAYWRIGHT_NODEJS_PATH", raising=False)
    driver = tmp_path / "driver"
    driver.mkdir()
    (driver / "node.exe").write_text("", encoding="utf-8")

    def _no_lookup(_name: str) -> str:
        raise AssertionError("aucune recherche de node ne doit avoir lieu")

    monkeypatch.setattr("tools.browser_agent.shutil.which", _no_lookup)
    assert ensure_system_node(driver) == ""
    assert "PLAYWRIGHT_NODEJS_PATH" not in os.environ


def test_system_node_is_exported_when_driver_node_is_absent(tmp_path, monkeypatch) -> None:
    """Variante slim : le node du PATH est transmis à Playwright."""
    # `ensure_system_node` écrit dans le vrai `os.environ` : on lui donne une
    # copie, sinon la valeur de test fuiterait vers les lancements suivants.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    os.environ.pop("PLAYWRIGHT_NODEJS_PATH", None)
    node = str(tmp_path / "nodejs" / "node.exe")
    monkeypatch.setattr("tools.browser_agent.shutil.which", lambda name: node if name == "node" else None)

    assert ensure_system_node(tmp_path / "vide") == node
    assert os.environ["PLAYWRIGHT_NODEJS_PATH"] == node


def test_existing_node_path_is_left_untouched(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", r"D:\nodejs\node.exe")

    def _no_lookup(_name: str) -> str:
        raise AssertionError("la variable déjà définie doit suffire")

    monkeypatch.setattr("tools.browser_agent.shutil.which", _no_lookup)
    assert ensure_system_node(tmp_path) == r"D:\nodejs\node.exe"


def test_missing_node_yields_a_clear_empty_result(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PLAYWRIGHT_NODEJS_PATH", raising=False)
    monkeypatch.setattr("tools.browser_agent.shutil.which", lambda _name: None)
    assert ensure_system_node(tmp_path) == ""
    assert "PLAYWRIGHT_NODEJS_PATH" not in os.environ


def test_frozen_app_points_playwright_to_the_user_cache(tmp_path, monkeypatch) -> None:
    """Dans l'exe, Playwright doit lire `%LOCALAPPDATA%\\ms-playwright`, pas `_internal`. """
    monkeypatch.setattr(os, "environ", dict(os.environ))
    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    monkeypatch.setattr("tools.browser_agent.sys.frozen", True, raising=False)
    monkeypatch.setattr("tools.browser_agent.playwright_browsers_root", lambda: tmp_path)

    assert ensure_browsers_path() == str(tmp_path)
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path)


def test_installed_python_keeps_playwright_default_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(os, "environ", dict(os.environ))
    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    monkeypatch.setattr("tools.browser_agent.sys", type("S", (), {})(), raising=False)
    monkeypatch.setattr("tools.browser_agent.playwright_browsers_root", lambda: tmp_path)

    assert ensure_browsers_path() == ""
    assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ


def test_start_explains_a_missing_node_runtime(tmp_path, monkeypatch) -> None:
    """Sans Node embarqué ni Node système, l'erreur dit quoi installer."""
    with tmpdir("browser") as tmp:
        agent = BrowserAgent(_settings(tmp), None)
        monkeypatch.setattr("tools.browser_agent.resolved_channel", lambda _config: "msedge")
        monkeypatch.setattr("tools.browser_agent.ensure_system_node", lambda *_a, **_k: "")
        monkeypatch.setattr(
            "tools.browser_agent.driver_node_available", lambda *_a, **_k: False
        )
        try:
            try:
                agent.start()
            except BrowserUnavailable as exc:
                assert "Node.js" in str(exc), str(exc)
            else:
                raise AssertionError("sans Node.js le lancement doit échouer clairement")
        finally:
            agent.stop()


class _FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.handlers: dict[str, object] = {}

    def on(self, name: str, handler: object) -> None:
        self.handlers[name] = handler

    def title(self) -> str:
        return ""


class _FakeContext:
    def __init__(self) -> None:
        self.page = _FakePage()
        self.pages = [self.page]
        self.closed = False

    def set_default_timeout(self, _value: float) -> None:
        pass

    def set_default_navigation_timeout(self, _value: float) -> None:
        pass

    def new_page(self) -> _FakePage:
        return self.page

    def close(self) -> None:
        self.closed = True


class _FakeEngine:
    def __init__(self) -> None:
        self.options: dict[str, object] | None = None
        self.context: _FakeContext | None = None

    def launch_persistent_context(self, _profile: str, **options) -> _FakeContext:
        self.options = options
        self.context = _FakeContext()
        return self.context


class _FakePlaywright:
    def __init__(self) -> None:
        self.chromium = _FakeEngine()
        self.stopped = False

    def start(self) -> "_FakePlaywright":
        return self

    def stop(self) -> None:
        self.stopped = True


def _install_fake_playwright(monkeypatch, agent: BrowserAgent) -> _FakePlaywright:
    fake = _FakePlaywright()
    # Pilote embarqué simulé : `ensure_system_node` n'a rien à exporter, et le
    # contrôle « aucun Node du tout » ne doit pas se déclencher.
    monkeypatch.setattr("tools.browser_agent.ensure_system_node", lambda: "")
    monkeypatch.setattr("tools.browser_agent.driver_node_available", lambda: True)
    monkeypatch.setattr(agent, "_import", lambda: (lambda: fake))
    return fake


def test_slim_falls_back_to_the_system_browser_without_config_edits(monkeypatch, tmp_path) -> None:
    """`bundled` sans navigateur embarqué : Edge/Chrome de la machine prend le relais."""
    settings = _settings(tmp_path)
    assert settings.browser.channel == "", "le défaut de configuration reste vide"
    settings.browser.channel = "bundled"
    monkeypatch.setattr("tools.browser_agent.bundled_browser_available", lambda: False)
    monkeypatch.setattr("tools.browser_agent.system_browser_channel", lambda: "msedge")

    agent = BrowserAgent(settings)
    fake = _install_fake_playwright(monkeypatch, agent)
    try:
        agent.start()
        assert fake.chromium.options is not None
        assert fake.chromium.options["channel"] == "msedge"
        assert agent.status().channel == "msedge", "le statut doit refléter le repli"
    finally:
        agent.stop()
    assert agent.status().running is False


def test_explicit_channel_wins_over_detection(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    settings.browser.channel = "chrome"

    def _no_probe() -> bool:
        raise AssertionError("un canal explicite ne doit rien détecter")

    monkeypatch.setattr("tools.browser_agent.bundled_browser_available", _no_probe)
    agent = BrowserAgent(settings)
    fake = _install_fake_playwright(monkeypatch, agent)
    try:
        agent.start()
        assert fake.chromium.options is not None
        assert fake.chromium.options["channel"] == "chrome"
    finally:
        agent.stop()


def test_missing_browser_reports_both_remedies(monkeypatch, tmp_path) -> None:
    """Sans navigateur embarqué ni système : erreur explicite, pas un plantage."""
    settings = _settings(tmp_path)
    settings.browser.channel = "bundled"
    monkeypatch.setattr("tools.browser_agent.bundled_browser_available", lambda: False)
    monkeypatch.setattr("tools.browser_agent.system_browser_channel", lambda: "")

    agent = BrowserAgent(settings)

    def _no_import() -> object:
        raise AssertionError("Playwright ne doit pas être importé sans navigateur")

    monkeypatch.setattr(agent, "_import", _no_import)
    try:
        agent.start()
    except BrowserUnavailable as exc:
        message = str(exc)
        assert "Edge" in message and "Chrome" in message, message
        assert "playwright install chromium" in message, message
    else:
        raise AssertionError("un lancement sans navigateur doit échouer clairement")
    assert agent.running is False


def test_browser_detection_uses_cache_then_system_paths(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "ms-playwright"
    cache.mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    assert bundled_browser_available() is False
    (cache / "chromium-1148").mkdir()
    assert bundled_browser_available() is True

    # Le repli ne s'applique que si rien d'embarqué n'existe.
    settings = _settings(tmp_path)
    settings.browser.channel = "bundled"
    assert resolved_channel(settings.browser) == ""

    if os.name != "nt":  # pragma: no cover - spécifique à Windows
        return
    program_files = tmp_path / "ProgramFiles"
    program_files.mkdir()
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "absent-x86"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert system_browser_channel() == ""

    edge = program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.write_text("", encoding="utf-8")
    assert system_browser_channel() == "msedge"

    # Et `resolved_channel` l'utilise quand le cache est vide.
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "cache-vide"))
    assert resolved_channel(settings.browser) == "msedge"


if __name__ == "__main__":
    sys.exit(run(globals()))
