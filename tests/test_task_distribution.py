from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.core.database import session_factory
from app.models.models import SendDraft, TeamTask, User
from app.services import agent as service
from tests.test_agent import agent, act, op  # noqa: F401
from tests.test_friends import person
from tests.test_teams import team_of, member_id


async def setup(client):
    boss, worker, other = [await person(client, name) for name in ("Boss", "Worker", "Other")]
    team = await team_of(client, boss, (worker, "Worker"), (other, "Other"))
    return boss, worker, other, team


async def test_distribution_creates_individual_confirmable_drafts_with_distinct_years(client):
    boss, worker, other, team = await setup(client)
    async with session_factory() as session:
        user = await session.get(User, boss["id"])
        turn = service.Turn(session=session, user=user, tz=ZoneInfo("Europe/Moscow"), now=datetime(2026, 11, 11, 12, tzinfo=ZoneInfo("Europe/Moscow")), text="Распредели задачи по людям из документа")
        result = await service.act(turn, {"operations": [
            op("delegate", team_id=team["id"], team_member_id=member_id(team, "Worker"), task_text="Подготовить отчёт 14 ноября 2028 года"),
            op("delegate", team_id=team["id"], team_member_id=member_id(team, "Other"), task_text="Проверить отчёт 15 ноября 2029 года"),
        ]})
        assert turn.social_reply["kind"] == "send_drafts", result
        drafts = turn.social_reply["drafts"]
        assert len(drafts) == 2
        assert [card["date"] for card in drafts] == ["2028-11-14", "2029-11-15"]
        assert await session.scalar(select(func.count()).select_from(TeamTask).where(TeamTask.team_id == team["id"])) == 0
        await session.commit()
    first = await client.post(f"/api/send-drafts/{drafts[0]['draft_id']}/send", headers=boss["headers"])
    assert first.status_code == 200, first.text
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(TeamTask).where(TeamTask.team_id == team["id"])) == 1
        assert (await session.get(SendDraft, drafts[1]["draft_id"])).status == "open"


async def test_missing_fields_produce_question_list_and_no_partial_drafts(client):
    boss, _, _, team = await setup(client)
    async with session_factory() as session:
        user = await session.get(User, boss["id"])
        turn = service.Turn(session=session, user=user, tz=ZoneInfo("Europe/Moscow"), now=datetime.now(ZoneInfo("Europe/Moscow")), text="Распредели задачи из ТЗ")
        await service.act(turn, {"operations": [op("delegate", task_text="Сделать отчёт"), op("delegate", task_text="Проверить макет")]})
        reply = turn.social_reply
        assert reply["kind"] == "answer" and "1." in reply["text"] and "4." in reply["text"]
        assert await session.scalar(select(func.count()).select_from(SendDraft).where(SendDraft.user_id == user.id)) == 0


async def test_document_distribution_is_available_in_bot_and_preserves_source_for_followup(client, agent):
    boss, _, _, team = await setup(client)
    agent([act(op("ask", question="Уточните ТЗ:\n1. Кто готовит результат?\n2. Какой срок?\n3. Что считать готовым?", options=["Уточню детали", "Без срока"]))])
    from tests.conftest import BOT_HEADERS
    response = await client.post(f"/internal/bot/chat/{boss['chat_id']}/file", headers=BOT_HEADERS, data={"text": "Распредели задачи из ТЗ между людьми и командами"}, files={"file": ("spec.txt", "Подготовить отчёт. Проверить макет.".encode(), "text/plain")})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "answer" and "3." in reply["text"]
    assert "distribution_spec" in reply and "Подготовить отчёт" in reply["distribution_spec"]


async def test_invalid_recipient_rolls_back_entire_distribution(client):
    boss, _, _, team = await setup(client)
    async with session_factory() as session:
        user = await session.get(User, boss["id"])
        turn = service.Turn(session=session, user=user, tz=ZoneInfo("Europe/Moscow"), now=datetime.now(ZoneInfo("Europe/Moscow")), text="Распредели задачи")
        await service.act(turn, {"operations": [
            op("delegate", team_id=team["id"], team_member_id=member_id(team, "Worker"), task_text="Отчёт 14 ноября 2028 года"),
            op("delegate", team_id=team["id"], team_member_id=99999999, task_text="Проверка 15 ноября 2028 года"),
        ]})
        assert turn.social_reply["kind"] == "answer"
        assert await session.scalar(select(func.count()).select_from(SendDraft).where(SendDraft.user_id == user.id)) == 0


async def test_document_can_prepare_multiple_team_assignments_in_one_reply(client, agent):
    boss, _, _, team = await setup(client)
    agent([act(
        op("delegate", team_id=team["id"], team_member_id=member_id(team, "Worker"), task_text="Отчёт 14 ноября 2028 года"),
        op("delegate", team_id=team["id"], team_member_id=member_id(team, "Other"), task_text="Проверка 15 ноября 2028 года"),
    )])
    response = await client.post("/api/assistant/chat/file", headers=boss["headers"], data={"text": "Распредели задачи из документа между участниками команды"}, files={"file": ("spec.txt", "Отчёт — Worker, 14 ноября 2028 года. Проверка — Other, 15 ноября 2028 года.".encode(), "text/plain")})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "send_drafts" and len(reply["drafts"]) == 2
    history = (await client.get("/api/assistant/history", headers=boss["headers"])).json()
    assert any((message.get("reply") or {}).get("kind") == "send_drafts" for message in history)


async def test_team_wide_distribution_keeps_requirements_until_send(client):
    boss, _, _, team = await setup(client)
    async with session_factory() as session:
        user = await session.get(User, boss["id"])
        turn = service.Turn(session=session, user=user, tz=ZoneInfo("Europe/Moscow"), now=datetime.now(ZoneInfo("Europe/Moscow")), text="Распредели задачи между командами")
        await service.act(turn, {"operations": [
            op("delegate", team_id=team["id"], team_only=True, task_text="Подготовить отчёт 14 ноября 2028 года", description="Include figures and acceptance checklist"),
            op("delegate", team_id=team["id"], team_member_id=member_id(team, "Worker"), task_text="Проверить 15 ноября 2028 года"),
        ]})
        assert turn.social_reply["kind"] == "send_drafts"
        first = turn.social_reply["drafts"][0]["draft_id"]
        await session.commit()
    sent = await client.post(f"/api/send-drafts/{first}/send", headers=boss["headers"])
    assert sent.status_code == 200, sent.text
    [task] = (await client.get(f"/api/teams/{team['id']}/tasks", headers=boss["headers"])).json()
    assert task["assignee_id"] is None and task["description"] == "Include figures and acceptance checklist"


async def test_unavailable_distribution_never_creates_personal_tasks(client):
    from tests.test_friends import events_of
    boss = await person(client, "Boss")
    response = await client.post("/api/assistant/chat", headers=boss["headers"], json={"text": "Распредели задачи по людям: отчёт 14 ноября 2028 года"})
    assert response.status_code == 200 and response.json()["kind"] == "answer"
    assert not await events_of(boss["id"])
