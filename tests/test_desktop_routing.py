"""Desktop direct commands must not guess side effects from similar text."""
import json

import pytest

from app.services.desktop_routing import direct_command, direct_turn, relevant_tools


@pytest.mark.parametrize("text,app", [("открой калькулятор", "calculator"), ("Launch Word", "word"), ("open notepad", "notepad")])
def test_exact_application_commands(text, app):
    assert direct_command(text) == {"name": "open_app", "arguments": {"app": app}}


@pytest.mark.parametrize("text", ["don't open calculator", "не открывай калькулятор", "открой калькулятор и посчитай", "open calculator then close", "start cmd", "открой файл с паролями"])
def test_compound_negative_and_unknown_commands_need_planning(text):
    assert direct_command(text) is None


def test_direct_completion_only_for_matching_action_results():
    command = direct_command("open notepad")
    history = [{"role": "user", "content": "open notepad"}]
    assert direct_turn(history, {"open_app"}) == {"action": command}
    history += [{"role": "assistant", "function_call": command}, {"role": "function", "content": json.dumps({"ok": False, "error": "denied"})}]
    assert "denied" in direct_turn(history, {"open_app"})["content"]
    history[-1]["content"] = json.dumps({"ok": True})
    assert "Launch requested" in direct_turn(history, {"open_app"})["content"]
    history[-2]["function_call"] = {"name": "open_app", "arguments": {"app": "word"}}
    assert direct_turn(history, {"open_app"}) is None


async def test_semantic_retrieval_retains_related_tools_and_handles_outage(monkeypatch):
    from app.services import desktop_routing as routing
    class Fake:
        enabled = True
        async def rank(self, *args): return [(0.9, 0), (0.3, 1)]
    monkeypatch.setattr(routing, "embeddings", Fake())
    tools = [{"name": name, "description": name} for name in ["browser_open", "system_info", "browser_fill", "windows_list", "read_text_file"]]
    history = [{"role": "user", "content": "website"}, {"role": "assistant", "function_call": {"name": "windows_list"}}]
    result = await relevant_tools(history, tools)
    assert {tool["name"] for tool in result} == {"browser_open", "browser_fill", "windows_list", "read_text_file"}
    async def failed(*args): raise RuntimeError("synthetic provider outage")
    monkeypatch.setattr(routing.embeddings, "rank", failed)
    assert await relevant_tools(history, tools) == tools
