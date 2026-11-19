"""Masquage : aucun secret ne doit entrer dans une trace, quel que soit l'événement.

Les tests nommés (clé `credential`, en-tête `Authorization`, ...) couvrent les
cas connus. Celui-ci génère le même jeu de secrets dans **tous** les types
d'événements, y compris les champs du manifeste de run : un champ ajouté plus
tard et oublié du nettoyage le fait échouer.
"""

from __future__ import annotations

from pathlib import Path

from support import make_settings, tmpdir  # noqa: I001

from core.events import Event, EventType
from core.trace import RunTrace, read_events
from tools.vault import Vault, build_vault_tools

#: Formes reconnues dans du texte libre par `security.redaction`.
SECRET_TEXTS = (
    "sk-proj-abcdef0123456789ABCDEF",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
    "password=hunter2-very-secret",
    "postgres://admin:motdepasse123@10.0.0.5:5432/base",
)

#: Valeurs uniques placées sous des clés sensibles : le masquage par nom de clé.
SECRET_VALUES = {
    "password": "valeur-mot-de-passe-42",
    "credential": "valeur-credential-42",
    "credentials": "valeur-credentials-42",
    "api_key": "valeur-api-key-42",
    "totp_secret": "JBSWY3DPEHPK3PXP",
    "authorization": "Bearer valeur-jeton-42",
}

ALL_EVENT_TYPES = (
    EventType.RUN_STARTED,
    EventType.STEP_STARTED,
    EventType.LLM_REQUEST,
    EventType.LLM_RESPONSE,
    EventType.LLM_RETRY,
    EventType.STREAM_CHUNK,
    EventType.CONTEXT_TRIMMED,
    EventType.TOOL_STARTED,
    EventType.TOOL_FINISHED,
    EventType.THOUGHT,
    EventType.FINAL,
    EventType.ERROR,
    EventType.RUN_FINISHED,
)


def test_no_secret_reaches_any_event_type() -> None:
    with tmpdir("fuite") as tmp:
        directory = Path(tmp) / "runs"
        trace = RunTrace(directory, max_age_days=0, max_bytes=0)

        trace(
            Event(
                type=EventType.RUN_STARTED,
                run_id="fuite",
                payload={"task": " ".join(SECRET_TEXTS), "model": "modèle-de-test", **SECRET_VALUES},
            )
        )
        for event_type in ALL_EVENT_TYPES[1:-1]:
            trace(
                Event(
                    type=event_type,
                    run_id="fuite",
                    payload={
                        "text": SECRET_TEXTS[0],
                        "task": SECRET_TEXTS[2],
                        "message": SECRET_TEXTS[3],
                        **SECRET_VALUES,
                    },
                )
            )
        trace(
            Event(
                type=EventType.RUN_FINISHED,
                run_id="fuite",
                payload={"stop_reason": "final", "ok": True},
            )
        )
        trace.close()

        raw = (directory / "fuite.jsonl").read_text(encoding="utf-8")
        assert raw.strip(), "la trace doit exister pour que le test ait un sens"
        for text in SECRET_TEXTS:
            assert text not in raw, f"secret tracé en clair : {text}"
        for key, value in SECRET_VALUES.items():
            assert value not in raw, f"valeur sensible tracée sous la clé {key}"
        assert "hunter2" not in raw
        assert "motdepasse123" not in raw
        # Le contenu non secret reste lisible : le nettoyage ne vide pas la trace.
        assert "modèle-de-test" in raw


def test_trace_records_only_keys_for_sensitive_tool_arguments() -> None:
    """Rappel de la règle sur un événement isolé, sans passer par l'agent."""
    with tmpdir("cles") as tmp:
        directory = Path(tmp) / "runs"
        trace = RunTrace(directory, max_age_days=0, max_bytes=0)
        trace(Event(type=EventType.RUN_STARTED, run_id="sens"))
        trace(
            Event(
                type=EventType.TOOL_STARTED,
                run_id="sens",
                payload={"tool": "vault_get_entry", "sensitive": True, "arguments": {}},
            )
        )
        trace.close()

        records = list(read_events(directory / "sens.jsonl"))
        started = [record for record in records if record["type"] == EventType.TOOL_STARTED]
        assert started and started[0]["payload"]["arguments"] == {}
        assert started[0]["payload"]["sensitive"] is True


def test_vault_tools_taking_secret_parameters_are_declared_sensitive() -> None:
    """Le drapeau `sensitive` reste déclaratif : un oubli annulerait le masquage."""
    with tmpdir("coffre") as tmp:
        settings = make_settings(tmp)
        tools = {tool.name: tool for tool in build_vault_tools(Vault(settings))}
        assert tools

        secret_params = {
            "password",
            "totp_secret",
            "passphrase",
            "credential",
            "credentials",
            "token",
            "api_key",
            "secret",
        }
        for name, tool in tools.items():
            properties = set((tool.parameters or {}).get("properties") or {})
            if properties & secret_params:
                assert tool.sensitive is True, (
                    f"{name} accepte un paramètre secret sans sensitive=True : "
                    "ses arguments seraient copiés dans la trace et dans le prompt"
                )

        # Ces outils reçoivent un secret en argument (mot de passe, secret TOTP) :
        # sans le drapeau, il serait écrit en clair dans la trace.
        for name in (
            "vault_create_credential",
            "vault_add_entry",
            "vault_update_entry",
        ):
            assert name in tools, f"outil attendu absent du registre : {name}"
            assert tools[name].sensitive is True, name
