"""Selections retain every attachment across the API, model input and persisted history."""
from datetime import datetime, timedelta
from dataclasses import replace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from app.services import attachments, teams
from tests.test_agent import agent  # noqa: F401
from tests.test_friends import person
from tests.test_team_planning import assignment
from tests.test_teams import team_of


def photos():
    return [('files', ('one.png', b'first synthetic image', 'image/png')), ('files', ('two.png', b'second synthetic image', 'image/png'))]


async def test_selection_reaches_model_and_persisted_history(client, user, agent, monkeypatch):
    headers, _ = user
    monkeypatch.setattr(attachments, 'images_text', AsyncMock(side_effect=['Planning note alpha', 'Planning note beta']))
    requests = agent(['Both planning notes received.'])
    response = await client.post('/api/assistant/chat/files', headers=headers, files=photos(), data={'text': 'Compare the planning notes'})
    assert response.status_code == 200, response.text
    history = (await client.get('/api/assistant/history', headers=headers)).json()
    shown = next(item for item in reversed(history) if item['role'] == 'user')
    assert [file['name'] for file in shown['reply']['files']] == ['one.png', 'two.png']
    assert shown['reply']['caption'] == 'Compare the planning notes'
    message = next(item['content'] for item in reversed(requests[0]) if item['role'] == 'user')
    assert 'Planning note alpha' in message and 'Planning note beta' in message


async def test_team_chat_selection_and_nonmember_denial(client, agent, monkeypatch):
    owner = await person(client, 'Owner')
    stranger = await person(client, 'Stranger')
    team = await team_of(client, owner)
    reader = AsyncMock(side_effect=['Team plan alpha', 'Team plan beta'])
    monkeypatch.setattr(attachments, 'images_text', reader)
    requests = agent(['Both team planning notes received.'])
    monkeypatch.setattr(teams, 'settings', replace(teams.settings, gigachat_credentials='fake', assistant_agent=True))
    url = f"/api/teams/{team['id']}/chat/files"
    denied = await client.post(url, headers=stranger['headers'], files=photos(), data={'text': 'Read the notes'})
    assert denied.status_code == 404
    reader.assert_not_awaited()
    response = await client.post(url, headers=owner['headers'], files=photos(), data={'text': 'Compare the planning notes'})
    assert response.status_code == 200, response.text
    history = (await client.get(f"/api/teams/{team['id']}/chat", headers=owner['headers'])).json()
    sent = next(item for item in reversed(history) if item['role'] == 'user')
    assert 'one.png' in sent['text'] and 'two.png' in sent['text']
    model_text = next(item['content'] for item in reversed(requests[0]) if item['role'] == 'user')
    assert 'Team plan alpha' in model_text and 'Team plan beta' in model_text


async def test_future_team_task_starts_and_completes_without_rescheduling(client):
    owner, worker = await person(client, 'Owner'), await person(client, 'Worker')
    _, task = await assignment(client, owner, worker)
    day = (datetime.now(ZoneInfo('Europe/Moscow')).date() + timedelta(days=7)).isoformat()
    accepted = await client.post(f"/api/team-tasks/{task['id']}/accept", headers=worker['headers'], json={'day': day})
    assert accepted.status_code == 200, accepted.text
    [event] = (await client.get('/api/events', headers=worker['headers'])).json()
    start = event['start_at']
    assert (await client.post(f"/api/team-tasks/{task['id']}/start", headers=worker['headers'])).status_code == 200
    assert (await client.post(f"/api/team-tasks/{task['id']}/done", headers=worker['headers'])).status_code == 200
    saved = (await client.get(f"/api/events/{event['id']}", headers=worker['headers'])).json()
    assert saved['completed_at'] is not None and saved['team_task']['status'] == 'done'
    assert saved['start_at'] == start
