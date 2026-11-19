"""Approbation par outil : rien n'est exécuté avant la décision, once ≠ session."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

from core.approval import (  # noqa: E402
    ApprovalGate,
    ApprovalRequest,
    approval_log_path,
    build_approval_gate,
    decision_from_confirm,
    preview_for,
)
from core.tool_registry import ToolRegistry  # noqa: E402
from tools.base import FunctionTool  # noqa: E402

_SCHEMA = {"type": "object", "properties": {}}


def _tool(name: str, calls: list[dict], *, dangerous: bool = False) -> FunctionTool:
    def run(**kwargs):
        calls.append(dict(kwargs))
        return "fait"

    return FunctionTool(name, name, _SCHEMA, run, dangerous=dangerous, read_only=not dangerous)


def _registry(tool: FunctionTool, decider, *, mode: str = "dangerous", settings=None) -> ToolRegistry:
    gate = ApprovalGate(decide=decider, mode=mode, settings=settings)
    return ToolRegistry([tool], approval=gate)


def test_nothing_runs_before_the_decision_and_a_denial_is_clean() -> None:
    calls: list[dict] = []
    seen: list[ApprovalRequest] = []

    def deny(request):
        seen.append(request)
        return "deny"

    registry = _registry(_tool("fs_delete", calls, dangerous=True), deny)
    result = registry.dispatch("fs_delete", {"path": r"C:\tmp\x"})
    assert result.ok is False
    assert result.error_code == "confirmation_required"
    assert calls == [], "l'outil ne doit jamais être dispatché avant la décision"
    assert seen and seen[0].tool == "fs_delete"
    assert "Suppression" in seen[0].preview


def test_once_does_not_carry_to_the_next_call() -> None:
    calls: list[dict] = []
    decisions: list[str] = []

    def once(request):
        decisions.append("once")
        return "once"

    registry = _registry(_tool("fs_delete", calls, dangerous=True), once)
    assert registry.dispatch("fs_delete", {"path": "a"}).ok
    assert registry.dispatch("fs_delete", {"path": "b"}).ok
    assert len(decisions) == 2, "une autorisation « une fois » ne vaut que pour un appel"
    assert [call["path"] for call in calls] == ["a", "b"]


def test_session_approval_is_reused_without_asking_again() -> None:
    calls: list[dict] = []
    decisions: list[str] = []

    def session(request):
        decisions.append("session")
        return "session"

    registry = _registry(_tool("fs_delete", calls, dangerous=True), session)
    assert registry.dispatch("fs_delete", {"path": "a"}).ok
    assert registry.dispatch("fs_delete", {"path": "b"}).ok
    assert len(decisions) == 1
    assert len(calls) == 2


def test_mode_write_covers_edits_but_dangerous_mode_does_not() -> None:
    gate_dangerous = build_approval_gate(make_settings(Path(".")))
    gate_dangerous.mode = "dangerous"
    assert gate_dangerous.required(_tool("fs_delete", [], dangerous=True))
    assert not gate_dangerous.required(_tool("fs_write_text", []))

    gate_write = ApprovalGate(decide=lambda _request: "deny", mode="write")
    assert gate_write.required(_tool("fs_write_text", []))
    assert not gate_write.required(_tool("fs_read_text", []))
    assert ApprovalGate(mode="all").required(_tool("fs_read_text", []))
    assert not ApprovalGate(mode="none").required(_tool("fs_delete", [], dangerous=True))


def test_previews_show_diff_command_domain_and_hide_secrets() -> None:
    with tmpdir("approbation-apercu") as tmp:
        target = tmp / "notes.txt"
        target.write_text("ligne a\nligne b\n", encoding="utf-8")
        diff = preview_for("fs_write_text", {"path": str(target), "content": "ligne a\nligne c\n"})
        assert "-ligne b" in diff and "+ligne c" in diff

        assert "dir /s" in preview_for("shell_run", {"command": "dir /s"})
        assert "example.com" in preview_for("web_fetch", {"url": "https://example.com/x"})
        vault = preview_for(
            "vault_add_entry",
            {"service": "mail", "password": "s3cret-value", "username": "moi"},
        )
        assert "s3cret-value" not in vault
        assert "mail" in vault

        gate = ApprovalGate(decide=lambda request: "deny", settings=make_settings(tmp))
        tool = _tool("vault_add_entry", [])
        gate.authorize(tool, {"service": "mail", "password": "s3cret-value"})
        journal = approval_log_path(make_settings(tmp)).read_text(encoding="utf-8")
        assert "s3cret-value" not in journal
        record = json.loads(journal.splitlines()[0])
        assert record["decision"] == "deny" and record["tool"] == "vault_add_entry"


def test_missing_or_broken_decider_denies() -> None:
    calls: list[dict] = []
    registry = ToolRegistry([_tool("fs_delete", calls, dangerous=True)], approval=ApprovalGate())
    assert registry.dispatch("fs_delete", {"path": "x"}).error_code == "confirmation_required"
    assert calls == []

    def broken(request):
        raise RuntimeError("interface fermée")

    registry = _registry(_tool("fs_delete", calls, dangerous=True), broken)
    assert registry.dispatch("fs_delete", {"path": "x"}).error_code == "confirmation_required"
    assert calls == []


def test_legacy_confirm_is_adapted_once_at_construction() -> None:
    calls: list[dict] = []
    seen: list[tuple[str, dict]] = []
    tool = _tool("fs_delete", calls, dangerous=True)

    def confirm(received, arguments) -> bool:
        seen.append((received.name, dict(arguments)))
        return True

    gate = build_approval_gate(
        make_settings(Path(".")), decision_from_confirm(confirm)
    )
    registry = ToolRegistry([tool], approval=gate)
    assert registry.dispatch("fs_delete", {"path": "x"}).ok
    assert calls == [{"path": "x"}]
    # Le pont reçoit l'objet outil et les arguments, comme avant.
    assert seen == [("fs_delete", {"path": "x"})]

    refused = ToolRegistry(
        [_tool("fs_delete", [], dangerous=True)],
        approval=build_approval_gate(
            make_settings(Path(".")), decision_from_confirm(lambda _t, _a: False)
        ),
    )
    assert refused.dispatch("fs_delete", {"path": "y"}).error_code == (
        "confirmation_required"
    )


if __name__ == "__main__":
    from support import run

    sys.exit(run(globals()))
