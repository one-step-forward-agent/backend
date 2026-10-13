"""LLM_PROVIDER=openai: every model request, the agent included, goes to an OpenAI-compatible API at BASE_URL.

A local aiohttp server plays the API, so the real HTTP request (address, key, tools, tool results) is checked.
"""

import dataclasses
import json

import pytest
from aiohttp import web
from sqlalchemy import select

from app.core.config import settings
from app.core.database import session_factory
from app.models.models import LlmUsage


@pytest.fixture
async def openai_api(monkeypatch):
    """openai_api(answers) serves the answers in order at /v1/chat/completions; returns the request bodies it got."""
    from app.services import chat
    from services import gigachat

    received: list[dict] = []
    answers: list[dict] = []

    async def completions(request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != "Bearer test-key":
            return web.json_response({"error": "unauthorized"}, status=401)
        received.append(await request.json())
        message = answers.pop(0)
        usage = {"prompt_tokens": 120, "completion_tokens": 8, "total_tokens": 128, "prompt_tokens_details": {"cached_tokens": 100}}
        return web.json_response({"model": "test-model-2026", "choices": [{"index": 0, "message": message}], "usage": usage})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completions)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    configured = dataclasses.replace(
        settings,
        llm_provider="openai",
        openai_api_key="test-key",
        openai_base_url=f"http://127.0.0.1:{port}/v1",
        openai_model="test-model",
        openai_agent_model="test-agent-model",
        gigachat_credentials="",
        assistant_agent=True,
    )
    monkeypatch.setattr(gigachat, "settings", configured)
    monkeypatch.setattr(chat, "settings", configured)

    def serve(messages: list[dict]) -> list[dict]:
        answers[:] = messages
        received.clear()
        return received

    yield serve
    await runner.cleanup()


def test_the_provider_needs_its_own_settings():
    openai = dataclasses.replace(settings, llm_provider="openai", openai_api_key="key", openai_base_url="https://example.test/v1", openai_model="m", openai_agent_model="m", gigachat_credentials="")
    assert openai.llm_enabled and openai.llm_model == "m"
    assert not dataclasses.replace(openai, openai_model="").llm_enabled
    assert not dataclasses.replace(openai, openai_api_key="").llm_enabled
    gigachat = dataclasses.replace(settings, llm_provider="gigachat", gigachat_credentials="fake", gigachat_model="GigaChat")
    assert gigachat.llm_enabled and gigachat.llm_model == "GigaChat"
    with pytest.raises(RuntimeError, match="LLM_PROVIDER"):
        dataclasses.replace(settings, llm_provider="gpt").validate()


async def test_the_agent_calls_functions_through_openai_tools(client, user, openai_api):
    headers, _ = user

    def tool_call(call_id: str, operations: list) -> dict:
        return {"id": call_id, "type": "function", "function": {"name": "act", "arguments": json.dumps({"operations": operations})}}

    received = openai_api(
        [
            # A call that does nothing: the model sees why and calls again
            {"role": "assistant", "content": None, "tool_calls": [tool_call("call_a", [{"action": "create", "events": []}])]},
            {"role": "assistant", "content": None, "tool_calls": [tool_call("call_b", [{"action": "create", "events": [{"title": "Купить хлеб", "date": "завтра"}]}])]},
        ]
    )
    response = await client.post("/api/assistant/chat", json={"text": "завтра купить хлеб, потом посмотрим"}, headers=headers)
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["draft_id"] and [item["title"] for item in reply["events"]] == ["Купить хлеб"]

    first, second = received
    assert first["model"] == "test-agent-model" and first["tool_choice"] == "auto"
    assert "functions" not in first and "function_call" not in first
    tools = {tool["function"]["name"]: tool["function"] for tool in first["tools"]}
    assert all(tool["type"] == "function" for tool in first["tools"]) and list(tools) == ["act"]
    # GigaChat's few-shot examples are not an OpenAI field: they are folded into the description
    assert "few_shot_examples" not in tools["act"] and "Примеры:" in tools["act"]["description"]
    # The result of the call goes back as a tool message tied to it
    calling, result = second["messages"][-2:]
    assert calling["role"] == "assistant" and calling["tool_calls"][0]["function"]["name"] == "act"
    assert json.loads(calling["tool_calls"][0]["function"]["arguments"])["operations"][0]["action"] == "create"
    assert result["role"] == "tool" and result["tool_call_id"] == calling["tool_calls"][0]["id"] and "events" in result["content"]

    async with session_factory() as session:
        row = await session.scalar(select(LlmUsage).where(LlmUsage.purpose == "agent").order_by(LlmUsage.id.desc()))
    assert row.model == "test-model-2026" and row.total_tokens == 128 and row.precached_prompt_tokens == 100


async def test_plain_requests_use_openai_model(openai_api):
    from services.gigachat import GigaChatClient

    received = openai_api([{"role": "assistant", "content": "Совет: начните с «Отчёта»."}])
    assert await GigaChatClient().analysis({"tasks": []}, "проанализируй день") == "Совет: начните с «Отчёта»."
    assert received[0]["model"] == "test-model" and received[0]["messages"][0]["role"] == "user"


async def test_a_parallel_tool_call_is_taken_one_at_a_time():
    from services.gigachat import gigachat_message

    message = gigachat_message(
        {
            "content": None,
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "get_events", "arguments": '{"query": "отчёт"}'}},
                {"id": "b", "type": "function", "function": {"name": "get_stats", "arguments": ""}},
            ],
        }
    )
    assert message == {"role": "assistant", "content": "", "function_call": {"name": "get_events", "arguments": {"query": "отчёт"}}}
