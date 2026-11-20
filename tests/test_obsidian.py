"""Cerveau Obsidian : notes, liens, mémorisation automatique et bootstrap."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

import tools.obsidian as obsidian  # noqa: E402
from core.events import Event  # noqa: E402
from core.factory import build_agent  # noqa: E402
from core.tool_registry import ToolRegistry  # noqa: E402
from tools.base import FunctionTool, ToolResult  # noqa: E402
from tools.obsidian import (  # noqa: E402
    ObsidianBrain,
    build_obsidian_tools,
    download_obsidian_installer,
    ensure_obsidian,
    install_obsidian,
    resolve_obsidian_installer,
    verify_authenticode,
)


class _FakeLLM:
    supports_stream = False

    def complete(self, *args, **kwargs):
        raise AssertionError("le test ne doit pas appeler le fournisseur")

    def close(self) -> None:
        pass


def _enabled(settings):
    settings.obsidian.enabled = True
    settings.obsidian.auto_install = False
    return settings


def test_vault_creates_index_and_interconnected_notes() -> None:
    with tmpdir("obsidian-vault") as tmp:
        settings = _enabled(make_settings(tmp))
        brain = ObsidianBrain(settings)

        assert (brain.root / "00_CORTEX_INDEX.md").is_file()
        assert {folder for folder in obsidian.FOLDERS} <= {
            path.name for path in brain.root.iterdir() if path.is_dir()
        }
        note = brain.remember(
            "Projet Atlas",
            "Le budget du projet Atlas est de 42 EUR.",
            tags=["projet", "budget"],
            links=["00_CORTEX_INDEX", "Fournisseur"],
        )
        assert Path(note["path"]).is_file()
        text = Path(note["path"]).read_text(encoding="utf-8")
        assert "[[00_CORTEX_INDEX]]" in text
        assert "[[Fournisseur]]" in text
        assert "#projet" in text and "#budget" in text

        index = (brain.root / "00_CORTEX_INDEX.md").read_text(encoding="utf-8")
        assert "[[Projet Atlas]]" in index
        assert "02_Knowledge/projet-atlas.md" in index
        hits = brain.search("budget")
        assert hits and hits[0]["title"] == "Projet Atlas"
        assert "42 EUR" in hits[0]["excerpt"]


def test_remember_accepts_a_custom_subfolder_and_indexes_it() -> None:
    with tmpdir("obsidian-custom-folder") as tmp:
        settings = _enabled(make_settings(tmp))
        brain = ObsidianBrain(settings)
        note = brain.remember(
            "Atlas fournisseur",
            "Le fournisseur Atlas livre le 12.",
            folder="02_Knowledge/Projets/Atlas",
        )
        custom = brain.root / "02_Knowledge" / "Projets" / "Atlas"
        assert custom.is_dir()
        assert Path(note["path"]).resolve() == (custom / "atlas-fournisseur.md").resolve()
        assert note["folder"] == "02_Knowledge/Projets/Atlas"
        assert "Le fournisseur Atlas livre le 12." in Path(note["path"]).read_text(
            encoding="utf-8"
        )

        index = (brain.root / "00_CORTEX_INDEX.md").read_text(encoding="utf-8")
        assert "[[Atlas fournisseur]]" in index
        assert "`02_Knowledge/Projets/Atlas/`" in index
        assert "`02_Knowledge/Projets/Atlas/atlas-fournisseur.md`" in index
        assert "02_Knowledge/Projets/Atlas" in brain.status()["folders"]
        assert brain.search("livre le 12")

        backslash = brain.remember(
            "Atlas plan",
            "Plan Atlas 2027.",
            folder="02_Knowledge\\Projets\\Atlas",
        )
        assert backslash["folder"] == "02_Knowledge/Projets/Atlas"
        assert (custom / "atlas-plan.md").is_file()

        tools = {tool.name: tool for tool in build_obsidian_tools(brain)}
        folder_spec = tools["obsidian_remember"].parameters["properties"]["folder"]
        assert "enum" not in folder_spec


def test_remember_rejects_folder_traversal_and_invalid_paths() -> None:
    with tmpdir("obsidian-traversal") as tmp:
        settings = _enabled(make_settings(tmp))
        brain = ObsidianBrain(settings)
        invalid = [
            "..",
            "../escape",
            "02_Knowledge/../../escape",
            "02_Knowledge/./Atlas",
            "/etc",
            "C:\\Windows",
            "\\\\server\\share",
            ".obsidian",
            "02_Knowledge/con",
            "02_Knowledge/at:las",
            "02_Knowledge/con.md",
            "02_Knowledge/At las/con",
            "02_Knowledge/A/B/C/D/E",
            "00_CORTEX_INDEX",
        ]
        for folder in invalid:
            try:
                brain.remember("Escape", "contenu", folder=folder)
            except ValueError:
                continue
            raise AssertionError(f"dossier accepté à tort : {folder!r}")
        assert not (brain.root.parent / "escape").exists()
        assert not (brain.root / ".obsidian").exists()
        assert not (brain.root / "00_CORTEX_INDEX").exists()


def test_hidden_obsidian_folders_are_neither_indexed_nor_searched() -> None:
    with tmpdir("obsidian-hidden") as tmp:
        settings = _enabled(make_settings(tmp))
        brain = ObsidianBrain(settings)
        trash = brain.root / ".trash"
        trash.mkdir()
        (trash / "supprimee.md").write_text("SECRETANCIEN budget", encoding="utf-8")
        plugin = brain.root / ".obsidian" / "plugins" / "demo"
        plugin.mkdir(parents=True)
        (plugin / "lisezmoi.md").write_text("PLUGIN budget", encoding="utf-8")
        brain.remember("Note visible", "budget visible")

        index = (brain.root / "00_CORTEX_INDEX.md").read_text(encoding="utf-8")
        assert "supprimee" not in index and "lisezmoi" not in index
        assert ".trash" not in index and ".obsidian" not in index
        assert ".trash" not in brain.status()["folders"]
        assert brain.search("SECRETANCIEN") == []
        assert all(
            ".trash" not in hit["relative"] for hit in brain.search("budget")
        )


def test_titles_and_sources_are_redacted_before_writing() -> None:
    with tmpdir("obsidian-redaction") as tmp:
        settings = _enabled(make_settings(tmp))
        brain = ObsidianBrain(settings)
        key = "sk-" + "A1b2C3d4E5f6G7h8I9j0"
        note = brain.remember(
            f"Clé {key}",
            f"Utilise {key} pour l'API.",
            source="https://api.example.com/v1?token=supersecretvalue&x=1",
        )
        text = Path(note["path"]).read_text(encoding="utf-8")
        assert key not in text
        assert "supersecretvalue" not in text
        assert "[MASQUÉ" in text

        injected = brain.remember(
            "Note source", "contenu", source="https://x/\ntags: [pwn]"
        )
        frontmatter = Path(injected["path"]).read_text(encoding="utf-8").split("---")[1]
        assert 'source: "https://x/ tags: [pwn]"' in frontmatter
        assert "\ntags: [pwn]\n" not in frontmatter


def test_vault_root_cannot_be_a_drive_root() -> None:
    with tmpdir("obsidian-root") as tmp:
        settings = _enabled(make_settings(tmp))
        try:
            ObsidianBrain(settings, path=Path(Path.cwd().anchor), create=False)
        except ValueError as exc:
            assert "racine" in str(exc)
        else:
            raise AssertionError("une racine de disque a été acceptée comme vault")


def test_tool_results_become_notes_but_sensitive_results_are_ignored() -> None:
    with tmpdir("obsidian-auto") as tmp:
        settings = _enabled(make_settings(tmp))
        brain = ObsidianBrain(settings)
        result = ToolResult(
            ok=True,
            data={"url": "https://example.com/atlas", "text": "ATLAS budget 42"},
        )
        remembered = brain.remember_tool_result(
            "web_fetch", {"url": "https://example.com/atlas"}, result
        )
        assert remembered is not None
        assert Path(remembered["path"]).is_file()
        assert "ATLAS budget 42" in Path(remembered["path"]).read_text(encoding="utf-8")
        assert brain.search("ATLAS")

        secret = ToolResult(ok=True, data={"password": "secret"}, sensitive=True)
        assert brain.remember_tool_result("web_fetch", {}, secret) is None
        assert brain.remember_tool_result("fs_write_text", {}, result) is None


def test_final_run_is_journaled_and_indexed() -> None:
    with tmpdir("obsidian-journal") as tmp:
        settings = _enabled(make_settings(tmp))
        brain = ObsidianBrain(settings)
        brain.on_event(
            Event(type="run_started", run_id="run-1", payload={"task": "préparer Atlas"})
        )
        brain.on_event(
            Event(
                type="final",
                run_id="run-1",
                payload={"text": "Le rapport Atlas est prêt."},
            )
        )
        journal = list((brain.root / "04_Journal").glob("*.md"))
        assert len(journal) == 1
        text = journal[0].read_text(encoding="utf-8")
        assert "préparer Atlas" in text and "Le rapport Atlas est prêt." in text
        assert "[[00_CORTEX_INDEX]]" in text


def test_registry_hook_persists_a_real_tool_result() -> None:
    with tmpdir("obsidian-hook") as tmp:
        settings = _enabled(make_settings(tmp))
        brain = ObsidianBrain(settings)
        tool = FunctionTool(
            "web_search",
            "recherche",
            {"type": "object", "properties": {}},
            lambda: {"results": [{"title": "Atlas", "url": "https://example.com"}]},
        )
        registry = ToolRegistry([tool], on_result=brain.remember_tool_result)
        assert registry.dispatch("web_search", {}).ok
        assert brain.search("Atlas")


def test_build_agent_wires_obsidian_tools_and_automatic_memory() -> None:
    with tmpdir("obsidian-agent") as tmp:
        settings = _enabled(make_settings(tmp))
        source = tmp / "atlas.txt"
        source.write_text("Le budget ATLAS est 42.", encoding="utf-8")
        bundle = build_agent(
            settings, llm=_FakeLLM(), with_web=False, with_browser=False
        )
        try:
            assert {"obsidian_remember", "obsidian_search", "obsidian_status"} <= set(
                bundle.tool_names()
            )
            assert bundle.brain is not None
            assert bundle.registry.dispatch(
                "fs_read_text", {"path": str(source)}
            ).ok
            bundle.agent.bus.emit(
                "run_started", {"task": "analyser Atlas"}, run_id="run-test"
            )
            bundle.agent.bus.emit(
                "final", {"text": "Atlas utilise 42."}, run_id="run-test"
            )
            assert list((bundle.brain.root / "04_Journal").glob("*.md"))
        finally:
            bundle.close()
        assert bundle.brain is not None
        assert bundle.brain.search("ATLAS")


def test_install_uses_winget_and_rechecks_the_executable(monkeypatch) -> None:
    with tmpdir("obsidian-install") as tmp:
        settings = _enabled(make_settings(tmp))
        installed = [False]

        def find():
            return Path(r"C:\Obsidian\Obsidian.exe") if installed[0] else None

        def which(name):
            return r"C:\winget.exe" if name == "winget" else None

        calls: list[list[str]] = []

        def runner(command, **kwargs):
            calls.append(command)
            installed[0] = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(obsidian, "find_obsidian", find)
        monkeypatch.setattr(obsidian.shutil, "which", which)
        ok, detail = install_obsidian(settings, runner=runner)
        assert ok and "installé" in detail
        assert calls and calls[0][:3] == [r"C:\winget.exe", "install", "--id"]
        assert "Obsidian.Obsidian" in calls[0]


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._stream = io.BytesIO(payload)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _opener(routes: dict[str, bytes]):
    def _open(request, *, timeout=60):
        for fragment, payload in routes.items():
            if fragment in request.full_url:
                return _FakeResponse(payload)
        raise OSError(f"URL inattendue : {request.full_url}")

    return _open


def _signature_json(
    status: str = "Valid", subject: str = "CN=Dynalist Inc, O=Dynalist Inc, C=CA"
) -> str:
    return json.dumps({"status": status, "subject": subject})


def _is_signature_call(command: list[str]) -> bool:
    return any("Get-AuthenticodeSignature" in str(part) for part in command)


def _which_with_powershell(name: str, *, winget: str | None = None) -> str | None:
    if name == "winget":
        return winget
    if name in {"powershell", "powershell.exe", "pwsh"}:
        return r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    return None


def _release_payload() -> bytes:
    return json.dumps(
        {
            "tag_name": "v1.15.1",
            "assets": [
                {
                    "name": "Obsidian-1.15.1.exe",
                    "browser_download_url": "https://github.com/obsidianmd/obsidian-releases/download/v1.15.1/Obsidian-1.15.1.exe",
                },
                {
                    "name": "Obsidian-1.15.1.exe.blockmap",
                    "browser_download_url": "https://example.com/Obsidian-1.15.1.exe.blockmap",
                },
            ],
        }
    ).encode("utf-8")


def test_official_installer_is_resolved_and_downloaded(monkeypatch) -> None:
    monkeypatch.delenv("OBSIDIAN_INSTALLER_URL", raising=False)
    with tmpdir("obsidian-download") as tmp:
        settings = _enabled(make_settings(tmp))
        opener = _opener(
            {
                "api.github.com": _release_payload(),
                "Obsidian-1.15.1.exe": b"MZ" + b"0" * 2048,
            }
        )
        url, version = resolve_obsidian_installer(opener=opener)
        assert url.endswith("Obsidian-1.15.1.exe")
        assert version == "1.15.1"
        path = download_obsidian_installer(settings, opener=opener)
        try:
            assert path.is_file() and path.name == "Obsidian-1.15.1.exe"
            assert path.read_bytes().startswith(b"MZ")
        finally:
            obsidian._cleanup_installer(path)


def test_installer_download_rejects_an_inexecutable_payload(monkeypatch) -> None:
    monkeypatch.delenv("OBSIDIAN_INSTALLER_URL", raising=False)
    with tmpdir("obsidian-download-bad") as tmp:
        settings = _enabled(make_settings(tmp))
        opener = _opener(
            {"api.github.com": _release_payload(), "Obsidian-1.15.1.exe": b"<html>"}
        )
        try:
            download_obsidian_installer(settings, opener=opener)
        except RuntimeError as exc:
            assert "exécutable" in str(exc)
        else:
            raise AssertionError("un fichier non exécutable a été accepté")


def test_install_falls_back_to_the_official_installer_without_winget(monkeypatch) -> None:
    with tmpdir("obsidian-fallback") as tmp:
        settings = _enabled(make_settings(tmp))
        installed = [False]
        monkeypatch.setattr(obsidian, "find_obsidian", lambda: Path(r"C:\Obsidian\Obsidian.exe") if installed[0] else None)
        monkeypatch.setattr(obsidian.shutil, "which", _which_with_powershell)
        installer = Path(tmp) / "Obsidian-1.15.1.exe"
        installer.write_bytes(b"MZ" + b"0" * 64)
        calls: list[list[str]] = []

        def runner(command, **kwargs):
            calls.append(command)
            if _is_signature_call(command):
                return SimpleNamespace(returncode=0, stdout=_signature_json(), stderr="")
            installed[0] = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ok, detail = install_obsidian(
            settings, runner=runner, downloader=lambda _settings: installer
        )
        assert ok and "installateur officiel" in detail
        install_calls = [call for call in calls if not _is_signature_call(call)]
        assert install_calls == [[str(installer), "/S", "/currentuser"]]


def test_install_falls_back_when_winget_fails(monkeypatch) -> None:
    with tmpdir("obsidian-fallback-winget") as tmp:
        settings = _enabled(make_settings(tmp))
        installed = [False]
        monkeypatch.setattr(obsidian, "find_obsidian", lambda: Path(r"C:\Obsidian\Obsidian.exe") if installed[0] else None)
        monkeypatch.setattr(
            obsidian.shutil,
            "which",
            lambda name: _which_with_powershell(name, winget=r"C:\winget.exe"),
        )
        installer = Path(tmp) / "Obsidian-1.15.1.exe"
        installer.write_bytes(b"MZ" + b"0" * 64)
        calls: list[list[str]] = []

        def runner(command, **kwargs):
            calls.append(command)
            if _is_signature_call(command):
                return SimpleNamespace(returncode=0, stdout=_signature_json(), stderr="")
            if command[0].endswith("winget.exe"):
                return SimpleNamespace(returncode=1, stdout="", stderr="échec winget")
            installed[0] = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ok, detail = install_obsidian(
            settings, runner=runner, downloader=lambda _settings: installer
        )
        assert ok and "installateur officiel" in detail
        used = [call for call in calls if not _is_signature_call(call)]
        assert used[0][:3] == [r"C:\winget.exe", "install", "--id"]
        assert used[1] == [str(installer), "/S", "/currentuser"]


def test_install_reports_both_winget_and_download_failures(monkeypatch) -> None:
    with tmpdir("obsidian-fallback-fail") as tmp:
        settings = _enabled(make_settings(tmp))
        monkeypatch.setattr(obsidian, "find_obsidian", lambda: None)
        monkeypatch.setattr(obsidian.shutil, "which", lambda name: None)

        def downloader(_settings):
            raise RuntimeError("réseau indisponible")

        ok, detail = install_obsidian(settings, downloader=downloader)
        assert not ok
        assert "winget absent" in detail
        assert "téléchargement" in detail and "réseau indisponible" in detail
        assert obsidian.OBSIDIAN_DOWNLOAD_URL in detail


def test_install_requires_the_executable_after_running_the_installer(monkeypatch) -> None:
    with tmpdir("obsidian-fallback-noverify") as tmp:
        settings = _enabled(make_settings(tmp))
        monkeypatch.setattr(obsidian, "find_obsidian", lambda: None)
        monkeypatch.setattr(obsidian.shutil, "which", _which_with_powershell)
        installer = Path(tmp) / "Obsidian-1.15.1.exe"
        installer.write_bytes(b"MZ" + b"0" * 64)

        def runner(command, **kwargs):
            stdout = _signature_json() if _is_signature_call(command) else ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        ok, detail = install_obsidian(
            settings, runner=runner, downloader=lambda _settings: installer
        )
        assert not ok
        assert "n'a pas confirmé l'installation" in detail


def test_install_refuses_an_unsigned_installer(monkeypatch) -> None:
    with tmpdir("obsidian-unsigned") as tmp:
        settings = _enabled(make_settings(tmp))
        monkeypatch.setattr(obsidian, "find_obsidian", lambda: None)
        monkeypatch.setattr(obsidian.shutil, "which", _which_with_powershell)
        installer = Path(tmp) / "Obsidian-1.15.1.exe"
        installer.write_bytes(b"MZ" + b"0" * 64)
        calls: list[list[str]] = []

        def runner(command, **kwargs):
            calls.append(command)
            if _is_signature_call(command):
                return SimpleNamespace(
                    returncode=0,
                    stdout=_signature_json(status="NotSigned", subject=""),
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ok, detail = install_obsidian(
            settings, runner=runner, downloader=lambda _settings: installer
        )
        assert not ok
        assert "installateur refusé" in detail and "absente ou invalide" in detail
        assert not [call for call in calls if not _is_signature_call(call)]


def test_install_refuses_an_installer_signed_by_another_publisher(monkeypatch) -> None:
    with tmpdir("obsidian-foreign-signer") as tmp:
        settings = _enabled(make_settings(tmp))
        monkeypatch.setattr(obsidian, "find_obsidian", lambda: None)
        monkeypatch.setattr(obsidian.shutil, "which", _which_with_powershell)
        installer = Path(tmp) / "Obsidian-1.15.1.exe"
        installer.write_bytes(b"MZ" + b"0" * 64)
        calls: list[list[str]] = []

        def runner(command, **kwargs):
            calls.append(command)
            if _is_signature_call(command):
                return SimpleNamespace(
                    returncode=0,
                    stdout=_signature_json(subject="CN=Evil Corp, O=Evil Corp, C=XX"),
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ok, detail = install_obsidian(
            settings, runner=runner, downloader=lambda _settings: installer
        )
        assert not ok
        assert "signataire inattendu" in detail
        assert not [call for call in calls if not _is_signature_call(call)]


def test_authenticode_refuses_a_lookalike_publisher(monkeypatch) -> None:
    with tmpdir("obsidian-lookalike-signer") as tmp:
        installer = Path(tmp) / "Obsidian-1.15.1.exe"
        installer.write_bytes(b"MZ" + b"0" * 64)
        monkeypatch.setattr(obsidian.shutil, "which", _which_with_powershell)

        def runner(command, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=_signature_json(
                    subject="CN=Evil Obsidian Ltd, O=Evil Obsidian Ltd, C=FR"
                ),
                stderr="",
            )

        ok, detail = verify_authenticode(installer, runner=runner)
        assert not ok
        assert "signataire inattendu" in detail


def test_authenticode_ignores_obsidian_outside_cn_and_o(monkeypatch) -> None:
    with tmpdir("obsidian-subject-lookalike") as tmp:
        installer = Path(tmp) / "Obsidian-1.15.1.exe"
        installer.write_bytes(b"MZ" + b"0" * 64)
        monkeypatch.setattr(obsidian.shutil, "which", _which_with_powershell)

        def runner(command, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=_signature_json(
                    subject="CN=Evil Corp, O=Evil Corp, L=Obsidian, C=FR"
                ),
                stderr="",
            )

        ok, detail = verify_authenticode(installer, runner=runner)
        assert not ok
        assert "signataire inattendu" in detail


def test_authenticode_accepts_an_exact_obsidian_subject(monkeypatch) -> None:
    with tmpdir("obsidian-exact-signer") as tmp:
        installer = Path(tmp) / "Obsidian-1.15.1.exe"
        installer.write_bytes(b"MZ" + b"0" * 64)
        monkeypatch.setattr(obsidian.shutil, "which", _which_with_powershell)

        def runner(command, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=_signature_json(subject="CN=Obsidian, O=Obsidian, C=US"),
                stderr="",
            )

        ok, detail = verify_authenticode(installer, runner=runner)
        assert ok and "signature valide" in detail


def test_authenticode_parses_quoted_cn_and_o_and_rejects_a_quoted_lookalike(
    monkeypatch,
) -> None:
    with tmpdir("obsidian-quoted-subject") as tmp:
        installer = Path(tmp) / "Obsidian-1.15.1.exe"
        installer.write_bytes(b"MZ" + b"0" * 64)
        monkeypatch.setattr(obsidian.shutil, "which", _which_with_powershell)

        def verdict(subject: str):
            def runner(command, **kwargs):
                return SimpleNamespace(
                    returncode=0,
                    stdout=_signature_json(subject=subject),
                    stderr="",
                )

            return verify_authenticode(installer, runner=runner)

        ok, detail = verdict('CN="Dynalist Inc", O="Obsidian", C=CA')
        assert ok and "signature valide" in detail

        ok, detail = verdict('CN="Evil Obsidian Ltd", O="Dynalist Inc", C=CA')
        assert not ok and "signataire inattendu" in detail


def test_authenticode_verification_fails_closed_without_powershell(monkeypatch) -> None:
    with tmpdir("obsidian-no-powershell") as tmp:
        installer = Path(tmp) / "Obsidian-1.15.1.exe"
        installer.write_bytes(b"MZ" + b"0" * 64)
        monkeypatch.setattr(obsidian.shutil, "which", lambda name: None)
        ok, detail = verify_authenticode(installer)
        assert not ok and "PowerShell" in detail


def test_install_is_not_attempted_when_auto_install_is_off(monkeypatch) -> None:
    with tmpdir("obsidian-off") as tmp:
        settings = _enabled(make_settings(tmp))
        settings.obsidian.auto_install = False
        monkeypatch.setattr(obsidian, "find_obsidian", lambda: None)
        ok, detail = ensure_obsidian(settings)
        assert not ok and "désactivée" in detail


if __name__ == "__main__":
    from support import run

    sys.exit(run(globals()))
