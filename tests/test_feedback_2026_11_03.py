"""Feedback of 3 November 2026: the assistant declines what is not about plans (code, homework, recipes, essays)
instead of doing it, while planning requests that mention such things still work."""

import pytest

from app.services import chat
from tests.test_agent import agent, say  # noqa: F401 - agent is a fixture


@pytest.mark.parametrize("text", [
    "Дай рецепт блинов",
    "как приготовить борщ?",
    "реши задачу на python: сумма элементов списка",
    "напиши код сортировки пузырьком",
    "переведи hello world на английский",
    "напиши сочинение про лето",
    "```python\nprint(sum([1, 2]))\n```",
    "give me a recipe for pancakes",
])
async def test_requests_not_about_plans_are_declined_without_the_model(client, user, agent, text):
    headers, _ = user
    requests = agent([])
    reply = await say(client, headers, text)
    assert reply["kind"] == "answer" and reply["text"] == chat.OUT_OF_SCOPE_ANSWER
    assert requests == []


@pytest.mark.parametrize("text", [
    "напомни приготовить блины в 18:00",
    "запланируй время написать код завтра в 10",
    "добавь задачу: написать код для проекта в пятницу",
    "напиши пост в 18:00",
    "что у меня завтра?",
])
def test_planning_requests_that_mention_such_things_are_not_declined(text):
    assert not chat.out_of_scope(text)


def test_the_prompts_forbid_doing_it():
    from services import gigachat

    assert "не давай рецептов" in gigachat.SCOPE and "код" in gigachat.SCOPE
