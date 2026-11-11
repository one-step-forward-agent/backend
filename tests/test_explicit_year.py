from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.services import chat, dates
from tests.test_assistant import TZ, today


@pytest.mark.parametrize("phrase", ["14 ноября 2028 года", "14 ноября, 2028", "14 ноября в 2028 году", "в 2028 году 14 ноября", "14.11.2028", "2028-11-14", "November 14, 2028", "14 November 2028"])
def test_explicit_year_in_date_and_model_shortened_phrase(phrase):
    now = datetime(2026, 11, 11, 12, tzinfo=ZoneInfo("Europe/Moscow"))
    assert dates.parse(phrase, now).date == date(2028, 11, 14)
    item = chat.normalize_item({"title": "Зал", "date_phrase": "14 ноября"}, f"Добавь зал {phrase}", now, True)
    assert item["date"] == "2028-11-14"


async def test_assistant_preserves_explicit_year_through_draft_confirmation(client, user, fake_gigachat):
    headers, _ = user
    year = today().year + 2
    fake_gigachat([{"title": "Зал", "date_phrase": "14 ноября", "time_phrase": "в 18:00"}])
    response = await client.post("/api/assistant/chat", headers=headers, json={"text": f"Добавь зал 14 ноября {year} года в 18:00"})
    reply = response.json()
    assert reply["kind"] in ("proposal", "created"), reply
    confirmed = (await client.post(f"/api/assistant/drafts/{reply['draft_id']}/confirm", headers=headers)).json() if reply["kind"] == "proposal" else reply
    saved = (await client.get(f"/api/events/{confirmed['event_ids'][0]}", headers=headers)).json()
    assert datetime.fromisoformat(saved["start_at"]).astimezone(TZ).year == year


def test_next_year_is_not_lost_when_model_omits_it():
    now = datetime(2026, 11, 11, 12, tzinfo=ZoneInfo("Europe/Moscow"))
    item = chat.normalize_item({"title": "Зал", "date_phrase": "14 ноября"}, "Зал 14 ноября следующего года", now, True)
    assert item["date"] == "2027-11-14"
