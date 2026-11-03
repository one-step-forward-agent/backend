"""Feedback of 2 November 2026: the activity grid, deleting an account whose Google credentials cannot be read,
and the limit message that no longer promises an answer later."""

from datetime import datetime, timedelta, timezone

from app.core.database import session_factory
from app.models.models import Integration, LlmUsage
from tests.test_limits import user_id


async def task(client, headers, title: str, tag_ids: list[int] | None = None) -> int:
    start = datetime.now(timezone.utc).replace(microsecond=0)
    body = {"title": title, "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat(), "tag_ids": tag_ids or []}
    created = await client.post("/api/events", json=body, headers=headers)
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def test_activity_counts_ticked_tasks_per_day_and_per_tag(client, user):
    headers, _ = user
    sport = (await client.post("/api/tags", json={"name": "Спорт"}, headers=headers)).json()
    walk = (await client.post("/api/tags", json={"name": "прогулка", "counts_progress": False}, headers=headers)).json()
    gym = await task(client, headers, "Спортзал", [sport["id"]])
    report = await task(client, headers, "Отчёт")
    stroll = await task(client, headers, "Прогулка", [walk["id"]])
    await task(client, headers, "Не сделано")
    for event in (gym, report, stroll):
        assert (await client.post(f"/api/events/{event}/complete", json={}, headers=headers)).status_code == 200

    everything = (await client.get("/api/stats/activity", headers=headers)).json()
    assert len(everything["days"]) == 53 * 7
    # The walk's tag is left out of the progress, so it is left out here too
    assert everything["total"] == 2 and everything["days"][-1]["done"] == 2
    assert {item["id"]: item["done"] for item in everything["tags"]} == {sport["id"]: 1, walk["id"]: 1}

    by_tag = (await client.get(f"/api/stats/activity?tag_id={sport['id']}", headers=headers)).json()
    assert by_tag["total"] == 1 and by_tag["days"][-1]["done"] == 1
    walks = (await client.get(f"/api/stats/activity?tag_id={walk['id']}", headers=headers)).json()
    assert walks["total"] == 1


async def test_unreadable_google_credentials_do_not_block_deleting_the_account(client, user):
    headers, _ = user
    async with session_factory() as session:
        session.add(Integration(user_id=await user_id(client, headers), provider="google", credentials_encrypted="not-a-ciphertext"))
        await session.commit()
    assert (await client.delete("/api/me", headers=headers)).status_code == 204
    assert (await client.get("/api/me", headers=headers)).status_code == 401


async def test_the_limit_message_asks_to_send_again_instead_of_promising_an_answer(client, user):
    headers, _ = user
    async with session_factory() as session:
        session.add(LlmUsage(user_id=await user_id(client, headers), purpose="agent", model="test", total_tokens=10**6, created_at=datetime.now(timezone.utc) - timedelta(minutes=30)))
        await session.commit()
    detail = (await client.post("/api/assistant/chat", json={"text": "Созвон завтра в 11"}, headers=headers)).json()["detail"]
    assert "не обработал" in detail and "Отправьте его снова через 30 минут" in detail
    assert "ответит" not in detail
