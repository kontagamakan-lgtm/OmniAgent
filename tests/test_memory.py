"""Mémoire projet : provenance, péremption par la source et injection bornée."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

from core.agent_core import AgentCore  # noqa: E402
from core.llm_client import LLMResponse  # noqa: E402
from core.memory import (  # noqa: E402
    add_note,
    delete_note,
    edit_note,
    format_notes,
    load_notes,
    memory_prompt,
    note_status,
)
from core.tool_registry import ToolRegistry  # noqa: E402


class _CaptureLLM:
    supports_stream = False

    def __init__(self) -> None:
        self.messages: list[list[dict]] = []

    def complete(self, messages, tools=None, **kwargs) -> LLMResponse:
        self.messages.append([dict(item) for item in messages])
        return LLMResponse(text="fini", finish_reason="stop")


def _user_text(messages: list[dict]) -> str:
    return "\n".join(
        str(item.get("content") or "")
        for item in messages
        if item.get("role") == "user"
    )


def test_note_with_source_is_injected_with_its_provenance() -> None:
    with tmpdir("memoire-note") as tmp:
        settings = make_settings(tmp)
        source = tmp / "convention.md"
        source.write_text("Les rapports vont dans rapports/.\n", encoding="utf-8")
        note = add_note(
            settings,
            "Les rapports vont dans rapports/",
            source=str(source),
            confidence=0.9,
        )
        assert note is not None
        assert note_status(load_notes(settings)[0]) == "active"

        prompt = memory_prompt(settings)
        assert "Les rapports vont dans rapports/" in prompt
        assert str(source) in prompt
        assert "confiance 0.9" in prompt


def test_a_changed_source_invalidates_the_note_but_keeps_provenance() -> None:
    with tmpdir("memoire-derive") as tmp:
        settings = make_settings(tmp)
        source = tmp / "convention.md"
        source.write_text("version 1\n", encoding="utf-8")
        assert add_note(settings, "Le dossier est en lecture seule", source=str(source))

        source.write_text("version 2, la regle a change\n", encoding="utf-8")
        note = load_notes(settings)[0]
        assert note_status(note) == "stale"
        assert memory_prompt(settings) == "", "une note périmée ne doit plus être une vérité"
        listing = format_notes(load_notes(settings))
        assert "source modifiee" in listing
        assert str(source) in listing and note.id in listing

        # Corriger la note rafraîchit l'empreinte : elle redevient active.
        refreshed = edit_note(
            settings, note.id, text="La regle v2 s'applique", source=str(source)
        )
        assert refreshed is not None
        assert note_status(load_notes(settings)[0]) == "active"
        assert "La regle v2 s'applique" in memory_prompt(settings)


def test_expired_note_is_not_injected_and_can_be_forgotten() -> None:
    with tmpdir("memoire-expire") as tmp:
        settings = make_settings(tmp)
        note = add_note(settings, "Note temporaire", ttl_days=-1)
        assert note is not None
        assert note_status(load_notes(settings)[0]) == "expired"
        assert memory_prompt(settings) == ""
        assert delete_note(settings, note.id)
        assert load_notes(settings) == []
        assert not delete_note(settings, note.id)


def test_prompt_is_bounded_by_max_chars() -> None:
    with tmpdir("memoire-borne") as tmp:
        settings = make_settings(tmp)
        settings.memory.max_chars = 120
        for index in range(10):
            assert add_note(settings, f"fait numero {index} " + "x" * 80)
        prompt = memory_prompt(settings)
        body = prompt.split("\n", 1)[1] if "\n" in prompt else prompt
        assert len(body) <= 120
        assert prompt.count("\n- ") <= 2


def test_agent_receives_active_notes_and_not_stale_ones() -> None:
    with tmpdir("memoire-agent") as tmp:
        settings = make_settings(tmp)
        source = tmp / "etat.md"
        source.write_text("base en lecture seule\n", encoding="utf-8")
        assert add_note(settings, "La base est en lecture seule le lundi", source=str(source))

        llm = _CaptureLLM()
        agent = AgentCore(settings, llm, ToolRegistry([]))
        assert agent.run("prepare le rapport").ok
        assert "La base est en lecture seule le lundi" in _user_text(llm.messages[-1])

        source.write_text("base ouverte\n", encoding="utf-8")
        assert agent.run("prepare le rapport").ok
        assert "La base est en lecture seule" not in _user_text(llm.messages[-1])


def test_a_note_is_stored_verbatim_even_when_it_looks_like_a_key() -> None:
    """Décision figée : la mémoire n'est pas masquée, contrairement aux journaux.

    Une note est un texte de l'utilisateur, relu et modifié dans l'éditeur : un
    masque silencieux remplacerait sa note par un texte qu'il n'a pas saisi.
    `save_notes` ne passe donc pas par `redact_data`.
    """
    with tmpdir("memoire-verbatim") as tmp:
        settings = make_settings(tmp)
        key = "sk-proj-AbCdEf0123456789AbCdEf0123456789"
        assert add_note(settings, f"la clé de test est {key}")

        stored = [note.text for note in load_notes(settings)]
        assert stored and key in stored[0], stored
        # La mémoire n'est pas un journal : l'épuration ne la relit pas.
        assert "MASQUÉ" not in stored[0]


if __name__ == "__main__":
    from support import run

    sys.exit(run(globals()))
