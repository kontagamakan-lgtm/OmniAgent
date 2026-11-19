"""Profils de capacités : outils hors profil invisibles, domaines et journal."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

import app  # noqa: E402
from core.factory import build_agent  # noqa: E402
from core.profile import (  # noqa: E402
    NetworkGuard,
    apply_profile,
    extract_host,
    network_log_path,
    profile_for,
    resolve_profile,
)
from core.tool_registry import ToolRegistry  # noqa: E402
from tools.base import FunctionTool  # noqa: E402
from tools.capabilities import CAPABILITIES  # noqa: E402


def _schema() -> dict:
    return {"type": "object", "properties": {}}


def test_read_only_profile_hides_every_write_network_and_shell_tool() -> None:
    tools = [
        FunctionTool(
            name,
            name,
            _schema(),
            lambda: "ok",
            read_only=CAPABILITIES[name].read_only,
            dangerous=CAPABILITIES[name].dangerous,
            sensitive=CAPABILITIES[name].sensitive,
        )
        for name in ("fs_read_text", "fs_write_text", "web_search", "shell_run", "vault_get_entry")
    ]
    kept = {tool.name for tool in apply_profile(tools, profile_for("read-only"))}
    assert kept == {"fs_read_text"}
    assert profile_for("files").allows("fs_write_text")
    assert not profile_for("files").allows("web_search")
    assert profile_for("files-web").allows("web_search")
    assert not profile_for("files-web").allows("shell_run")
    assert profile_for("full").allows("shell_run")


def test_agent_built_read_only_cannot_dispatch_network_or_shell() -> None:
    with tmpdir("profil-agent") as tmp:
        settings = make_settings(tmp)
        bundle = build_agent(settings, with_web=True, with_browser=False, profile="read-only")
        try:
            names = set(bundle.tool_names())
            assert "fs_read_text" in names
            assert "fs_write_text" not in names
            assert "web_search" not in names
            assert "shell_run" not in names
            refused = bundle.registry.dispatch("web_search", {"query": "test"})
            assert refused.ok is False and refused.error_code == "unknown_tool"
            refused_shell = bundle.registry.dispatch("shell_run", {"command": "dir"})
            assert refused_shell.error_code == "unknown_tool"
        finally:
            bundle.close()


def test_profiles_are_resolved_from_settings_and_refuse_unknown_names() -> None:
    with tmpdir("profil-config") as tmp:
        settings = make_settings(tmp)
        assert resolve_profile(settings).name == "full"
        settings.isolation.profile = "files"
        assert resolve_profile(settings).name == "files"
        assert resolve_profile(settings, "read-only").name == "read-only"
        try:
            profile_for("root")
        except ValueError as exc:
            assert "inconnu" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("un profil inconnu doit lever")


def test_domain_allowlist_denies_before_dispatch_and_logs_the_host() -> None:
    with tmpdir("profil-domaines") as tmp:
        settings = make_settings(tmp)
        settings.isolation.allowed_domains = ["example.com"]
        guard = NetworkGuard(profile=profile_for("files-web"), settings=settings)

        allowed, reason = guard("web_fetch", {"url": "https://docs.example.com/page"})
        assert allowed and reason == ""
        allowed, reason = guard("web_fetch", {"url": "https://evil.test/x"})
        assert not allowed and "evil.test" in reason

        executed: list[str] = []

        def fetch(url: str = "") -> str:
            executed.append(url)
            return "contenu"

        tool = FunctionTool(
            "web_fetch", "lit une page", _schema(), fetch, read_only=True
        )
        registry = ToolRegistry([tool], network_guard=guard)
        result = registry.dispatch("web_fetch", {"url": "https://evil.test/x"})
        assert result.error_code == "network_denied"
        assert executed == [], "l'outil ne doit jamais être exécuté sans autorisation"
        assert registry.dispatch("web_fetch", {"url": "https://example.com/x"}).ok

        records = [
            json.loads(line)
            for line in network_log_path(settings).read_text(encoding="utf-8").splitlines()
        ]
        assert [record["allowed"] for record in records] == [True, False, False, True]
        assert any(record["host"] == "evil.test" for record in records)


def test_extract_host_handles_relative_and_missing_urls() -> None:
    assert extract_host({"url": "https://Docs.Example.COM:8443/a"}) == "docs.example.com"
    assert extract_host({"query": "test"}) == ""
    assert extract_host({"url": "pas une url"}) == ""


def test_cli_exposes_the_profile_option() -> None:
    args = app.build_parser().parse_args(["--task", "x", "--profile", "read-only"])
    assert args.profile == "read-only"


if __name__ == "__main__":
    from support import run

    sys.exit(run(globals()))
