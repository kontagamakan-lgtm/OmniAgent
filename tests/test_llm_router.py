"""Routeur multi-LLM : classement, états des clés, bascule et secours local."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

from core.llm_router import (  # noqa: E402
    ACTIVE,
    COOLDOWN,
    DEAD,
    EXHAUSTED,
    LLMRouter,
    detect_provider,
    to_anthropic_messages,
)
from tools.vault import Vault  # noqa: E402

KEY_OPENAI_1 = "sk-first-111111111111111111"
KEY_OPENAI_2 = "sk-second-22222222222222222"
KEY_DEEPSEEK = "sk-ds-deepseek-3333333333333"
KEY_GROQ = "gsk_groq-444444444444444444"
KEY_GEMINI = "AIzaGemini-5555555555555555"


def _response(model: str, text: str = "bonjour") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        },
    )


def _router(
    tmp: Path,
    keys: list[tuple[str, str]],
    handler,
    *,
    name: str = "router",
) -> tuple[LLMRouter, Vault]:
    settings = make_settings(tmp)
    settings.llm_router.max_attempts = 12
    settings.llm.request_timeout_s = 5.0
    vault = Vault(settings)
    vault.create("Phrase#Solide42")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    router = LLMRouter(
        settings,
        vault,
        data_dir=Path(tmp) / name,
        http_client=client,
    )
    for provider, key in keys:
        router.add_key(key, provider=provider)
    return router, vault


def test_detects_providers_from_key_prefixes() -> None:
    assert detect_provider("sk-ant-api03-abcdef") == "anthropic"
    assert detect_provider("sk-or-v1-abcdef") == "openrouter"
    assert detect_provider("gsk_abcdef123456") == "groq"
    assert detect_provider("AIzaSyAbcdef123456") == "gemini"
    assert detect_provider("sk-proj-abcdef123456") == "openai"
    assert detect_provider("sk-ds-abcdef123456") == "deepseek"
    try:
        detect_provider("inconnue-123456")
    except ValueError:
        pass
    else:  # pragma: no cover - le préfixe doit être refusé
        raise AssertionError("préfixe inconnu accepté")


def test_best_first_picks_the_highest_scored_model_with_an_active_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return _response(str(payload["model"]))

    with tmpdir("router-best") as tmp:
        router, _vault = _router(
            tmp,
            [("deepseek", KEY_DEEPSEEK), ("openai", KEY_OPENAI_1)],
            handler,
        )
        response = router.complete([{"role": "user", "content": "salut"}])
        assert response.text == "bonjour"
        assert router.current_model == "gpt-5"
        assert router.current_provider == "openai"
        ranking = router.ranked_models()
        assert ranking[0]["model"] == "gpt-5"
        assert ranking[0]["score"] == 98


def test_rate_limited_key_cools_down_and_the_next_key_of_the_same_model_runs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("authorization", "")
        if KEY_OPENAI_1 in authorization:
            return httpx.Response(
                429,
                headers={"retry-after": "12"},
                json={"error": {"message": "rate limit"}},
            )
        return _response("gpt-5")

    with tmpdir("router-cooldown") as tmp:
        router, _vault = _router(
            tmp, [("openai", KEY_OPENAI_1), ("openai", KEY_OPENAI_2)], handler
        )
        response = router.complete([{"role": "user", "content": "salut"}])
        assert response.text == "bonjour"
        states = {row["hint"]: row for row in router.key_statuses()}
        first = states["sk-f…1111"]
        second = states["sk-s…2222"]
        assert first["status"] == COOLDOWN
        assert 0 < first["remaining_s"] <= 12
        assert second["status"] == ACTIVE
        # La clé en pause ne doit plus être choisie au prochain appel.
        assert router.complete([{"role": "user", "content": "encore"}]).text == "bonjour"


def test_exhausted_key_falls_back_to_the_next_best_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.openai.com":
            return httpx.Response(
                402, json={"error": {"message": "insufficient_quota"}}
            )
        payload = json.loads(request.content)
        return _response(str(payload["model"]))

    with tmpdir("router-exhausted") as tmp:
        router, _vault = _router(
            tmp, [("openai", KEY_OPENAI_1), ("deepseek", KEY_DEEPSEEK)], handler
        )
        response = router.complete([{"role": "user", "content": "salut"}])
        assert response.text == "bonjour"
        assert router.current_provider == "deepseek"
        assert router.current_model == "deepseek-reasoner"
        states = {row["provider"]: row for row in router.key_statuses()}
        assert states["openai"]["status"] == EXHAUSTED
        assert states["openai"]["remaining_s"] > 0
        assert states["deepseek"]["status"] == ACTIVE


def test_dead_key_falls_back_to_ollama_when_no_cloud_key_remains() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.openai.com":
            return httpx.Response(401, json={"error": {"message": "invalid key"}})
        return httpx.Response(
            200,
            json={
                "model": "llama3.2",
                "message": {"content": "réponse locale"},
                "done": True,
                "eval_count": 5,
                "prompt_eval_count": 3,
            },
        )

    with tmpdir("router-local") as tmp:
        router, _vault = _router(tmp, [("openai", KEY_OPENAI_1)], handler)
        response = router.complete([{"role": "user", "content": "salut"}])
        assert response.text == "réponse locale"
        assert router.current_provider == "ollama"
        assert router.key_statuses()[0]["status"] == DEAD


def test_status_and_secrets_never_write_the_raw_key_on_disk() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response("gpt-5")

    with tmpdir("router-secret") as tmp:
        router, vault = _router(tmp, [("openai", KEY_OPENAI_1)], handler)
        router.complete([{"role": "user", "content": "salut"}])
        stored = (Path(tmp) / "router" / "keys.json").read_text(encoding="utf-8")
        assert KEY_OPENAI_1 not in stored
        assert "hint" in stored
        assert vault.unlocked
        credentials = vault.credentials("llm.openai", router.key_statuses()[0]["id"])
        assert credentials.password == KEY_OPENAI_1


def test_removing_a_key_deletes_its_secret_from_the_vault() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response("gpt-5")

    with tmpdir("router-remove") as tmp:
        router, vault = _router(tmp, [("openai", KEY_OPENAI_1)], handler)
        key_id = router.key_statuses()[0]["id"]
        assert router.remove_key(key_id) is True
        assert router.key_statuses() == []
        try:
            vault.credentials("llm.openai", key_id)
        except Exception:
            pass
        else:  # pragma: no cover - le secret doit avoir disparu
            raise AssertionError("secret encore présent après suppression")


def test_state_survives_a_restart_and_keeps_statuses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid"}})

    with tmpdir("router-persist") as tmp:
        router, vault = _router(tmp, [("openai", KEY_OPENAI_1)], handler)
        try:
            router.complete([{"role": "user", "content": "salut"}])
        except Exception:  # noqa: BLE001 - seule la persistance nous intéresse
            pass
        assert router.key_statuses()[0]["status"] == DEAD
        reloaded = LLMRouter(
            router.settings,
            vault,
            data_dir=Path(tmp) / "router",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        assert len(reloaded.key_statuses()) == 1
        assert reloaded.key_statuses()[0]["status"] == DEAD


def test_anthropic_history_conversion_keeps_tool_calls_and_results() -> None:
    system, converted = to_anthropic_messages(
        [
            {"role": "system", "content": "tu es bref"},
            {"role": "user", "content": "lis le fichier"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "fs_read_text", "arguments": '{"path": "a.txt"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "contenu"},
        ]
    )
    assert system == "tu es bref"
    assert converted[1]["role"] == "assistant"
    block = converted[1]["content"][0]
    assert block["type"] == "tool_use"
    assert block["name"] == "fs_read_text"
    assert block["input"] == {"path": "a.txt"}
    result = converted[2]["content"][0]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "call_1"


def test_anthropic_provider_is_called_with_its_own_protocol() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "claude-opus-4-1",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 7, "output_tokens": 3},
            },
        )

    with tmpdir("router-anthropic") as tmp:
        router, _vault = _router(
            tmp,
            [("anthropic", "sk-ant-api03-abcdef123456")],
            handler,
        )
        response = router.complete(
            [
                {"role": "system", "content": "systeme"},
                {"role": "user", "content": "salut"},
            ]
        )
        assert response.text == "ok"
        assert response.usage["prompt_tokens"] == 7
        assert seen["url"] == "https://api.anthropic.com/v1/messages"
        headers = {str(key).lower(): value for key, value in dict(seen["headers"]).items()}
        assert headers["x-api-key"] == "sk-ant-api03-abcdef123456"
        payload = seen["payload"]
        assert payload["system"] == "systeme"
        assert payload["messages"] == [{"role": "user", "content": "salut"}]


def test_optimize_scores_rewards_the_measured_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response("gpt-5")

    with tmpdir("router-optimize") as tmp:
        router, _vault = _router(tmp, [("openai", KEY_OPENAI_1)], handler)
        router.complete([{"role": "user", "content": "salut"}])
        ranking = router.optimize_scores()
        top = next(row for row in ranking if row["model"] == "gpt-5")
        assert top["adjustment"] > 0
        assert top["score"] > top["base_score"]


def test_locked_vault_refuses_to_add_a_key() -> None:
    with tmpdir("router-locked") as tmp:
        settings = make_settings(tmp)
        vault = Vault(settings)
        vault.create("Phrase#Solide42")
        vault.lock()
        router = LLMRouter(settings, vault, data_dir=Path(tmp) / "router")
        try:
            router.add_key(KEY_OPENAI_1, provider="openai")
        except RuntimeError:
            pass
        else:  # pragma: no cover - le coffre doit être requis
            raise AssertionError("ajout accepté avec un coffre verrouillé")


def test_factory_uses_the_router_when_enabled() -> None:
    with tmpdir("router-factory") as tmp:
        settings = make_settings(tmp)
        settings.llm_router.enabled = True
        from core.factory import build_agent

        bundle = build_agent(
            settings, llm=None, with_web=False, with_browser=False
        )
        try:
            assert bundle.router is not None
            assert bundle.agent.llm is bundle.router
            assert bundle.router.summary()["keys"] == 0
        finally:
            bundle.close()


def test_setup_wizard_is_skipped_when_the_router_is_enabled() -> None:
    from ui.setup_wizard import needs_setup

    with tmpdir("router-setup") as tmp:
        settings = make_settings(tmp)
        settings.llm.api_key_env = "OMNIAGENT_MISSING_KEY_FOR_TEST"
        assert needs_setup(settings) is True
        settings.llm_router.enabled = True
        assert needs_setup(settings) is False


def test_config_validates_router_bounds() -> None:
    with tmpdir("router-config") as tmp:
        settings = make_settings(tmp)
        settings.llm_router.cooldown_s = 0
        settings.llm_router.max_attempts = 0
        settings.llm_router.ollama_model = " "
        issues = settings.validate()
        assert any("llm_router.cooldown_s" in str(issue) for issue in issues)
        assert any("llm_router.max_attempts" in str(issue) for issue in issues)
        assert any("llm_router.ollama_model" in str(issue) for issue in issues)


if __name__ == "__main__":
    from support import run

    sys.exit(run(globals()))
