"""Testers' notes of October 2026: night tasks, clearing the chat, tags from the chat, honest statistics,
and tasks without a day staying on today."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import ConversationMessage, Event, User
from tests.test_agent import agent, call, say  # noqa: F401 — the fixture is used by name

TZ = ZoneInfo("Europe/Moscow")


def today():
    return datetime.now(TZ).date()


async def test_night_shift_ends_the_next_morning(client, user):
    headers, _ = user
    reply = await say(client, headers, "завтра смена с 22:00 до 6:00")
    [item] = reply["events"]
    tomorrow = today() + timedelta(days=1)
    assert (item["date"], item["time"], item["end_time"], item["end_date"]) == (tomorrow.isoformat(), "22:00", "06:00", (tomorrow + timedelta(days=1)).isoformat())
    created = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()
    event = (await client.get(f"/api/events/{created['event_ids'][0]}", headers=headers)).json()
    start, end = datetime.fromisoformat(event["start_at"]).astimezone(TZ), datetime.fromisoformat(event["end_at"]).astimezone(TZ)
    assert end - start == timedelta(hours=8) and end.time() == time(6) and not event["all_day"]


async def test_cleared_chat_is_hidden_but_kept(client, user):
    headers, _ = user
    await say(client, headers, "Сегодня")
    assert len((await client.get("/api/assistant/history", headers=headers)).json()) == 2
    cleared = await client.delete("/api/assistant/history", headers=headers)
    assert cleared.status_code == 200 and cleared.json()["cleared"] == 2
    assert (await client.get("/api/assistant/history", headers=headers)).json() == []
    async with session_factory() as session:
        me = await session.scalar(select(User).order_by(User.id.desc()).limit(1))
        rows = list(await session.scalars(select(ConversationMessage).where(ConversationMessage.user_id == me.id)))
        assert len(rows) == 2 and all(row.hidden_at for row in rows)
    # New messages show as usual
    await say(client, headers, "Завтра")
    assert len((await client.get("/api/assistant/history", headers=headers)).json()) == 2


async def test_deleting_the_account_deletes_the_chat(client, user):
    headers, _ = user
    await say(client, headers, "Сегодня")
    async with session_factory() as session:
        me = await session.scalar(select(User).order_by(User.id.desc()).limit(1))
    assert (await client.delete("/api/me", headers=headers)).status_code == 204
    async with session_factory() as session:
        assert not list(await session.scalars(select(ConversationMessage).where(ConversationMessage.user_id == me.id)))


async def test_hashtag_becomes_the_users_tag(client, user):
    headers, _ = user
    existing = (await client.post("/api/tags", json={"name": "Работа"}, headers=headers)).json()
    reply = await say(client, headers, "завтра отчёт #работа #учёба")
    [item] = reply["events"]
    tags = {tag["name"]: tag["id"] for tag in (await client.get("/api/tags", headers=headers)).json()}
    assert set(tags) == {"Работа", "учёба"} and tags["Работа"] == existing["id"]
    assert sorted(item["tag_ids"]) == sorted(tags.values()) and "#" not in item["title"]
    assert "учёба" in reply["note"]


async def test_agent_tags_new_and_existing_tasks(client, user, agent):  # noqa: F811
    headers, _ = user
    await client.post("/api/tags", json={"name": "Дом"}, headers=headers)
    agent([call("create_events", events=[{"title": "Купить лампу", "date": "завтра", "tags": ["дом", "покупки"]}]), "Показала черновик."])
    reply = await say(client, headers, "завтра купить лампу, теги дом и покупки")
    tags = {tag["name"]: tag["id"] for tag in (await client.get("/api/tags", headers=headers)).json()}
    assert set(tags) == {"Дом", "покупки"} and sorted(reply["events"][0]["tag_ids"]) == sorted(tags.values())
    event_id = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()["event_ids"][0]

    def tag_it(messages):
        import re

        ref = re.search(r"\[id (\d+)\][^\n]*Купить лампу", messages[0]["content"]).group(1)
        assert "ТЕГИ пользователя: Дом, покупки" in messages[0]["content"]
        return call("update_event", event_id=int(ref), tags=["срочное"])

    agent([tag_it, "Показала изменение."])
    reply = await say(client, headers, "поставь лампе тег срочное")
    assert reply["kind"] == "proposal"
    await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)
    tags = {tag["name"]: tag["id"] for tag in (await client.get("/api/tags", headers=headers)).json()}
    event = (await client.get(f"/api/events/{event_id}", headers=headers)).json()
    assert sorted(event["tag_ids"]) == sorted(tags.values())


async def test_unmarked_past_tasks_are_not_done(client, user):
    headers, _ = user
    yesterday = today() - timedelta(days=1)
    reply = await say(client, headers, "завтра в 10:00 созвон")
    event_id = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json()["event_ids"][0]
    async with session_factory() as session:
        event = await session.get(Event, event_id)
        event.start_at = datetime.combine(yesterday, time(10), TZ)
        event.end_at = datetime.combine(yesterday, time(11), TZ)
        await session.commit()
    stats = (await client.get("/api/stats", headers=headers)).json()
    day = next(item for item in stats["days"] if item["date"] == yesterday.isoformat())
    assert (day["total"], day["done"]) == (1, 0)
    # "Выполнено" lists it, so it can be marked now
    done = await say(client, headers, "Выполнено")
    assert done["mark"] and [event["id"] for group in done["days"] for event in group["events"]] == [event_id]


async def test_task_without_a_day_is_for_today(client, user, agent):  # noqa: F811
    headers, _ = user
    tomorrow = (today() + timedelta(days=1)).isoformat()
    agent([call("create_events", events=[{"title": "Купить хлеб", "date": tomorrow}]), "Показала черновик."])
    reply = await say(client, headers, "купить хлеб")
    assert reply["events"][0]["date"] == today().isoformat() and reply["events"][0]["time"] is None
