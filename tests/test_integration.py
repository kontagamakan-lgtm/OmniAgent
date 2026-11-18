"""Tests d'intégration : configuration → coffre-fort → fichiers → outils."""

from __future__ import annotations

import json
import os
import sys

from support import make_settings, run, serve, tmpdir  # noqa: I001

import config
from core.factory import build_agent
from core.llm_client import LLMResponse
from core.react_parser import ParsedToolCall
from security.redaction import redact
from tools.base import ToolSet
from tools.doc_index import DocIndex
from tools.file_manager import FileManager, build_file_tools
from tools.vault import MASK, Vault, build_vault_tools

PASSPHRASE = "Correct-Horse-Battery-9!"


def _write_minimal_pdf(path, text: str) -> None:
    """PDF d'une page (Helvetica) sans dépendance : pypdf en extrait `text`."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(bodies) + 1,
        xref,
    )
    path.write_bytes(bytes(out))


def test_config_roundtrip_drives_runtime_paths() -> None:
    with tmpdir("int") as tmp:
        settings = make_settings(tmp)
        settings.agent.max_steps = 33
        saved = settings.save()
        reloaded = config.Settings.load(saved, apply_env=False, strict=False)
        assert reloaded.agent.max_steps == 33
        assert reloaded.paths.vault_file.name == "vault.enc"


def test_all_tools_register_with_unique_names() -> None:
    with tmpdir("int") as tmp:
        settings = make_settings(tmp)
        registry = ToolSet([*build_vault_tools(Vault(settings)), *build_file_tools(FileManager(settings))])
        names = registry.names()
        assert len(names) == len(set(names))
        assert len(names) >= 25, names
        for schema in registry.schemas():
            function = schema["function"]
            assert function["name"] and function["description"]
            parameters = function["parameters"]
            assert parameters["type"] == "object"
            for name, spec in parameters["properties"].items():
                assert spec["type"] in {"string", "integer", "number", "boolean", "array", "object"}, name


def test_end_to_end_credentials_and_documents() -> None:
    with tmpdir("int") as tmp:
        settings = make_settings(tmp)
        vault = Vault(settings)
        vault.create(PASSPHRASE)
        registry = ToolSet([*build_vault_tools(vault), *build_file_tools(FileManager(settings))])

        added = registry.dispatch(
            "vault_add_entry",
            {"service": "intranet.local", "username": "jean", "password": "P@ssw0rd!2024"},
        )
        assert added.ok

        masked = registry.dispatch("vault_get_entry", {"service": "intranet.local"})
        assert masked.ok and masked.data["value"] == MASK
        assert "P@ssw0rd!2024" not in masked.to_text()

        written = registry.dispatch(
            "fs_write_text",
            {"path": "rapports/audit.md", "content": "# Audit\nClient : ACME\nClé : sk-abcdefghijklmnopqrstuvwx\n"},
        )
        assert written.ok

        found = registry.dispatch("fs_search", {"root": ".", "content_regex": "ACME"})
        assert found.ok and found.data["count"] == 1

        read = registry.dispatch("fs_read_text", {"path": "rapports/audit.md"})
        assert read.ok
        safe = redact(read.to_text())
        assert "sk-abcdefghijklmnopqrstuvwx" not in safe
        assert "MASQUÉ" in safe

        credentials = vault.credentials("intranet.local")
        assert credentials.password == "P@ssw0rd!2024"


class _ScriptedLLM:
    """Fournisseur scripté : une réponse par appel, et la trace des messages."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        item = self.script.pop(0)
        if isinstance(item, BaseException):  # pragma: no cover - chemin d'erreur
            raise item
        return item

    def last_tool_contents(self) -> list[str]:
        return [
            str(message.get("content"))
            for message in self.calls[-1]["messages"]
            if message.get("role") == "tool"
        ]


def _tool_call(name: str, **arguments) -> LLMResponse:
    return LLMResponse(
        text="",
        tool_calls=[ParsedToolCall(name=name, arguments=dict(arguments), source="test")],
        finish_reason="tool_calls",
    )


def test_agent_core_drives_every_real_tool_family() -> None:
    """Boucle ReAct sur les vrais outils : schémas, dispatch et masquage ensemble.

    Fichiers, HTTP réel (serveur local), navigateur Chromium headless et coffre.
    """
    page = (
        "<html><head><title>Bilan</title></head><body>"
        "<h1>Bilan</h1><p>Le prix est 42 EUR.</p></body></html>"
    )
    with serve({"/page": (200, "text/html", page)}) as (base, counts):
        with tmpdir("int") as tmp:
            settings = make_settings(tmp)
            settings.agent.max_steps = 8
            settings.agent.parallel_tool_calls = 3
            # Le serveur de test écoute sur la boucle locale.
            settings.search.allow_private_networks = True
            settings.browser.headless = True
            note = tmp / "note.txt"
            note.write_text("CONTENU REEL DU DOCUMENT", encoding="utf-8")

            llm = _ScriptedLLM(
                [
                    _tool_call("fs_read_text", path=str(note)),
                    _tool_call("web_fetch", url=f"{base}/page"),
                    _tool_call("browser_open", url=f"{base}/page"),
                    _tool_call("browser_text"),
                    _tool_call(
                        "vault_create_credential",
                        service="forum.local",
                        username="jean",
                        password_length=24,
                    ),
                    _tool_call(
                        "vault_get_entry",
                        service="forum.local",
                        field_name="password",
                        reveal=True,
                    ),
                    LLMResponse(text="terminé", finish_reason="stop"),
                ]
            )
            bundle = build_agent(settings, llm=llm, with_web=True, with_browser=True)
            bundle.vault.create(PASSPHRASE)
            # Affectés avant le `try` : si l'exécution échoue, `finally` ferme le
            # bundle et l'erreur d'origine remonte, au lieu d'un NameError sur
            # une variable jamais liée qui masquerait la vraie cause.
            reply = None
            password = ""
            try:
                names = set(bundle.tool_names())
                assert {
                    "fs_read_text",
                    "web_fetch",
                    "browser_open",
                    "vault_create_credential",
                } <= names
                schemas = bundle.registry.schemas()
                assert len(schemas) == len(names)
                assert all(s["function"]["parameters"]["type"] == "object" for s in schemas)

                reply = bundle.agent.run("lis la note, la page, crée un identifiant, relis-le")
                password = bundle.vault.credentials("forum.local").password
            finally:
                bundle.close()

            assert reply is not None, "run() n'a rien retourné"
            assert reply.ok and reply.stop_reason == "final", reply.to_dict()
            assert reply.text == "terminé"
            assert reply.tool_calls == 6
            assert [call.name for step in reply.steps for call in step.calls] == [
                "fs_read_text",
                "web_fetch",
                "browser_open",
                "browser_text",
                "vault_create_credential",
                "vault_get_entry",
            ]
            assert all(call.ok for step in reply.steps for call in step.calls), reply.transcript()
            assert len(password) == 24

            # La page a bien été servie aux deux clients : httpx puis Chromium.
            assert counts.get("/page", 0) >= 2, counts

            observations = llm.last_tool_contents()
            assert "CONTENU REEL DU DOCUMENT" in observations[0]
            # Contenu réellement extrait, par HTTP puis par le navigateur.
            assert "42 EUR" in observations[1], observations[1]
            assert "page" in observations[2] or "Bilan" in observations[2]
            assert "42 EUR" in observations[3], observations[3]

            # Les deux outils du coffre masquent, chacun à sa manière.
            assert "enregistré dans le coffre" in observations[4]
            assert "SENSIBLE" in observations[5]

            # Aucun secret ne subsiste dans le contexte envoyé au fournisseur.
            context = json.dumps(llm.calls[-1]["messages"], ensure_ascii=False)
            assert password not in context
            assert password not in json.dumps(reply.to_dict(), ensure_ascii=False)


def test_vault_tools_are_wired_through_the_factory() -> None:
    """Le coffre de `build_agent` est réellement pilotable par ses outils."""
    with tmpdir("int") as tmp:
        settings = make_settings(tmp)
        bundle = build_agent(settings, with_web=False, with_browser=False)
        # Lié avant le `try` : si l'exécution échoue, `finally` ferme le bundle
        # et l'erreur d'origine remonte, au lieu d'un NameError qui la masquerait.
        password = ""
        try:
            names = bundle.tool_names()
            assert {"vault_create_credential", "vault_list_entries"} <= set(names)

            # Déverrouillage du coffre construit par la fabrique.
            bundle.vault.create(PASSPHRASE)
            bundle.vault.lock()
            assert bundle.vault.unlocked is False
            bundle.vault.unlock(PASSPHRASE)
            assert bundle.vault.unlocked is True

            created = bundle.registry.dispatch(
                "vault_create_credential",
                {"service": "forum.local", "username": "jean", "password_length": 24},
            )
            assert created.ok, created.to_text()
            assert created.sensitive is True
            password = bundle.vault.credentials("forum.local").password
            assert len(password) == 24
            assert password not in created.to_text()

            listed = bundle.registry.dispatch("vault_list_entries", {"query": "forum"})
            assert listed.ok, listed.to_text()
            entries = listed.data["entries"]
            assert len(entries) == 1, entries
            assert entries[0]["service"] == "forum.local"
            assert entries[0]["username"] == "jean"
            assert entries[0]["password"] == MASK
            assert password not in listed.to_text()
            assert settings.paths.vault_file.is_file(), "l'écriture doit être persistée"
        finally:
            bundle.close()

        assert bundle.vault.unlocked is False, "close() verrouille le coffre"


def test_vault_tools_are_driven_by_the_react_loop_after_unlock() -> None:
    """Les outils `vault_*` répondent au modèle, pas seulement au registre.

    Le coffre construit par `build_agent` est créé, verrouillé, puis déverrouillé
    comme le fait l'interface ; la boucle ReAct pilote ensuite elle-même la
    création puis la relecture, et le mot de passe généré ne revient jamais
    dans le contexte envoyé au fournisseur.
    """
    with tmpdir("int") as tmp:
        settings = make_settings(tmp)
        settings.agent.max_steps = 4
        settings.agent.parallel_tool_calls = 1
        llm = _ScriptedLLM(
            [
                _tool_call(
                    "vault_create_credential",
                    service="comptes.local",
                    username="jean",
                    password_length=20,
                ),
                _tool_call("vault_list_entries", query="comptes"),
                LLMResponse(text="identifiant créé puis relu", finish_reason="stop"),
            ]
        )
        bundle = build_agent(settings, llm=llm, with_web=False, with_browser=False)
        bundle.vault.create(PASSPHRASE)
        bundle.vault.lock()
        bundle.vault.unlock(PASSPHRASE)
        assert bundle.vault.unlocked is True
        # Affectés avant le `try` : une erreur d'exécution doit remonter intacte.
        reply = None
        password = ""
        try:
            reply = bundle.agent.run("crée un identifiant puis relis la liste")
            password = bundle.vault.credentials("comptes.local").password
        finally:
            bundle.close()

        assert reply is not None, "run() n'a rien retourné"
        assert reply.ok and reply.stop_reason == "final", reply.to_dict()
        assert reply.text == "identifiant créé puis relu"
        assert [call.name for step in reply.steps for call in step.calls] == [
            "vault_create_credential",
            "vault_list_entries",
        ]
        assert all(call.ok for step in reply.steps for call in step.calls), reply.transcript()
        assert len(password) == 20

        observations = llm.last_tool_contents()
        assert "enregistré dans le coffre" in observations[0], observations[0]
        assert "comptes.local" in observations[1], observations[1]
        assert password not in observations[1]
        assert settings.paths.vault_file.is_file(), "l'écriture doit être persistée"

        # Ni les observations ni l'historique complet ne contiennent la valeur.
        context = json.dumps(llm.calls[-1]["messages"], ensure_ascii=False)
        assert password not in context
        assert password not in json.dumps(reply.to_dict(), ensure_ascii=False)


def test_agent_reaches_the_network_and_the_vault_through_the_factory() -> None:
    """Une tâche réelle relie `web_*` et `vault_*` sans doublure d'outil."""
    page = (
        "<html><head><title>Devis</title></head><body>"
        "<h1>Devis</h1><p>Le prix est 42 EUR, validité 30 jours.</p></body></html>"
    )
    with serve({"/devis": (200, "text/html", page)}) as (base, counts):
        with tmpdir("int") as tmp:
            settings = make_settings(tmp)
            settings.agent.max_steps = 4
            settings.search.allow_private_networks = True

            llm = _ScriptedLLM(
                [
                    _tool_call("web_fetch", url=f"{base}/devis"),
                    _tool_call(
                        "vault_create_credential",
                        service="acme.local",
                        username="jean",
                        password_length=16,
                    ),
                    LLMResponse(text="devis lu, identifiant créé", finish_reason="stop"),
                ]
            )
            bundle = build_agent(settings, llm=llm, with_browser=False)
            bundle.vault.create(PASSPHRASE)
            # Affectés avant le `try` : si l'exécution échoue, `finally` ferme le
            # bundle et l'erreur d'origine remonte, au lieu d'un NameError sur
            # une variable jamais liée qui masquerait la vraie cause.
            reply = None
            password = ""
            try:
                reply = bundle.agent.run("lis le devis puis crée un identifiant ACME")
                password = bundle.vault.credentials("acme.local").password
            finally:
                bundle.close()

        assert reply.ok and reply.stop_reason == "final", reply.to_dict()
        assert reply.text == "devis lu, identifiant créé"
        assert counts.get("/devis") == 1, counts
        assert len(password) == 16

        observations = llm.last_tool_contents()
        assert "42 EUR" in observations[0], observations[0]
        assert "enregistré dans le coffre" in observations[1]
        assert password not in json.dumps(llm.calls[-1]["messages"], ensure_ascii=False)


def test_pdf_is_indexed_and_a_passage_is_found() -> None:
    """Un PDF passe par la conversion de FileManager et reste cherchable."""
    with tmpdir("int") as tmp:
        settings = make_settings(tmp)
        docs = tmp / "docs"
        docs.mkdir()
        _write_minimal_pdf(docs / "rapport.pdf", "Le budget du projet Atlas est de 42 EUR.")
        (docs / "note.txt").write_text("Rien à voir ici.", encoding="utf-8")

        with DocIndex(settings, FileManager(settings)) as index:
            built = index.index_path(str(docs))
            assert built["documents"] == 2, built
            assert not built["skipped"], built

            found = index.search("budget Atlas")
            assert found["count"] >= 1, found
            best = found["results"][0]
            assert best["path"].endswith("rapport.pdf"), best
            assert "Atlas" in best["excerpt"], best


def test_doc_index_build_skips_unchanged_files() -> None:
    """Incrémental : date et taille identiques ⇒ le fichier n'est pas relu."""
    with tmpdir("int") as tmp:
        settings = make_settings(tmp)
        docs = tmp / "docs"
        docs.mkdir()
        note = docs / "note.txt"
        note.write_text("Le budget du projet Atlas est de 42 EUR.", encoding="utf-8")

        with DocIndex(settings, FileManager(settings)) as index:
            first = index.index_path(str(docs))
            assert first["documents"] == 1, first
            assert first["unchanged"] == 0, first

            reads: list[str] = []
            original = index._read

            def counting(file, max_chars):
                reads.append(str(file))
                return original(file, max_chars)

            index._read = counting  # type: ignore[method-assign]

            second = index.index_path(str(docs))
            assert second["documents"] == 0, second
            assert second["unchanged"] == 1, second
            assert reads == [], "un fichier inchangé ne doit pas être relu"
            assert index.search("Atlas")["count"] == 1, "pas de doublon dans l'index"

            # Même contenu mais date avancée : la date seule déclenche la relecture.
            stamp = note.stat().st_mtime_ns + 2_000_000_000
            os.utime(note, ns=(stamp, stamp))
            third = index.index_path(str(docs))
            assert third["documents"] == 1, third
            assert third["unchanged"] == 0, third
            assert reads == [str(note)], reads

            # Contenu modifié : relu, et l'ancien morceau est remplacé.
            note.write_text("Le budget du projet Atlas est de 43 EUR.", encoding="utf-8")
            fourth = index.index_path(str(docs))
            assert fourth["documents"] == 1, fourth
            assert len(reads) == 2, reads
            assert index.search("43")["count"] == 1, index.search("43")
            assert index.stats()["documents"] == 1, index.stats()


def test_ocr_probe_names_the_configured_tesseract_binary() -> None:
    """`files.tesseract_cmd` est honoré : la raison d'indisponibilité le cite."""
    from tools.ocr import Ocr

    with tmpdir("int") as tmp:
        settings = make_settings(tmp)
        missing = tmp / "tesseract-absent.exe"
        settings.files.tesseract_cmd = str(missing)

        ready, reason = Ocr.probe_cli(str(missing))
        assert ready is False, reason
        assert "chemin configuré" in reason, reason
        assert "tesseract-absent.exe" in reason, reason

        # Le binaire configuré échoue, mais le moteur embarqué prend le relais :
        # `ocr_status` doit rester disponible et dire lequel travaille.
        status = Ocr(settings).status()
        assert status["available"] is True, status
        assert status["backend"] == "tesserocr", status
        assert "tesserocr" in status["detail"], status


def test_document_ocr_and_shell_schemas_are_exposed_safely() -> None:
    """Les trois familles sont exposées, l'OCR est débrayable, un seul outil est dangereux."""
    with tmpdir("int") as tmp:
        settings = make_settings(tmp)
        bundle = build_agent(settings, with_web=False, with_browser=False)
        try:
            tools = {tool.name: tool for tool in bundle.registry.toolset.tools()}
            assert {"doc_index_build", "doc_index_search", "doc_index_stats"} <= set(tools)
            assert {"ocr_image", "ocr_status"} <= set(tools)
            assert {"shell_run", "shell_status"} <= set(tools)

            schemas = {
                schema["function"]["name"]: schema["function"]["parameters"]
                for schema in bundle.registry.schemas()
            }
            assert schemas["doc_index_search"]["required"] == ["query"]
            assert schemas["ocr_image"]["required"] == ["path"]
            assert schemas["shell_run"]["required"] == ["command"]

            # Aucun outil dangereux n'est ajouté « par défaut » dans ces familles :
            # seule l'exécution de commandes l'est, et elle exige une confirmation.
            families = {
                name: tool
                for name, tool in tools.items()
                if name.startswith(("doc_index_", "ocr_", "shell_"))
            }
            assert [name for name, tool in families.items() if tool.dangerous] == ["shell_run"]
            assert families["shell_run"].read_only is False
            assert families["ocr_status"].read_only is True
            assert families["doc_index_search"].read_only is True
        finally:
            bundle.close()

        # `files.ocr_enabled = False` retire la famille OCR, sans toucher au reste.
        settings.files.ocr_enabled = False
        reduced = build_agent(settings, with_web=False, with_browser=False)
        try:
            names = set(reduced.tool_names())
            assert not any(name.startswith("ocr_") for name in names), names
            assert {"doc_index_search", "shell_run"} <= names
        finally:
            reduced.close()


def test_document_ocr_and_shell_tools_are_wired_and_dispatch() -> None:
    """Index documentaire, OCR et shell sont exposés et réellement dispatchables."""
    with tmpdir("int") as tmp:
        settings = make_settings(tmp)
        notes = tmp / "notes"
        notes.mkdir()
        (notes / "projet.txt").write_text(
            "Le budget du projet Atlas est de 42 EUR.", encoding="utf-8"
        )
        confirmed: list[str] = []

        def confirm(tool, _arguments) -> bool:
            confirmed.append(tool.name)
            return True

        bundle = build_agent(
            settings, with_web=False, with_browser=False, confirm=confirm
        )
        try:
            names = set(bundle.tool_names())
            assert {
                "doc_index_build",
                "doc_index_search",
                "doc_index_stats",
                "ocr_image",
                "ocr_status",
                "shell_run",
                "shell_status",
            } <= names

            schemas = {
                schema["function"]["name"]: schema["function"]["parameters"]
                for schema in bundle.registry.schemas()
            }
            assert schemas["doc_index_search"]["required"] == ["query"]
            assert schemas["ocr_image"]["required"] == ["path"]
            assert schemas["shell_run"]["required"] == ["command"]

            built = bundle.registry.dispatch("doc_index_build", {"path": str(notes)})
            assert built.ok, built.to_text()
            assert built.data["documents"] == 1, built.data
            assert built.data["chunks"] >= 1, built.data

            found = bundle.registry.dispatch(
                "doc_index_search", {"query": "budget Atlas"}
            )
            assert found.ok, found.to_text()
            assert found.data["count"] >= 1, found.data
            assert "Atlas" in found.data["results"][0]["excerpt"], found.data

            stats = bundle.registry.dispatch("doc_index_stats", {})
            assert stats.ok and stats.data["documents"] == 1, stats.to_text()

            ocr_status = bundle.registry.dispatch("ocr_status", {})
            assert ocr_status.ok and "available" in ocr_status.data
            missing = bundle.registry.dispatch(
                "ocr_image", {"path": str(tmp / "absent.png")}
            )
            assert missing.ok is False and missing.error, missing.to_dict()

            # L'outil shell est dangereux : sans confirmation, le registre bloque.
            bare = build_agent(settings, with_web=False, with_browser=False)
            try:
                refused = bare.registry.dispatch("shell_run", {"command": "echo bonjour"})
            finally:
                bare.close()
            assert refused.ok is False, refused.to_dict()
            assert refused.error_code == "confirmation_required", refused.to_dict()

            ran = bundle.registry.dispatch(
                "shell_run", {"command": "echo bonjour", "cwd": str(tmp)}
            )
            assert ran.ok, ran.to_text()
            assert ran.data["returncode"] == 0, ran.data
            assert "bonjour" in ran.data["stdout"], ran.data
            assert confirmed == ["shell_run"], confirmed
        finally:
            bundle.close()


if __name__ == "__main__":
    sys.exit(run(globals()))
