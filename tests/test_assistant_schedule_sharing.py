"""Sharing my calendar entries is distinct from assigning tasks to a friend."""
import pytest
from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import FriendTask, Notification, User
from app.services import schedule_sharing
from tests.test_agent import agent, act, op
from tests.test_friends import befriend, person, say
from tests.test_schedule_sharing import create, schedule


@pytest.mark.parametrize("text", [
    'пусть все задачи "ЗАЛ" видит Маша',
    'Показать Маше все «ЗАЛ»',
    'Покажи Маше все «ЗАЛ»',
    'Открой Маше доступ к моим задачам «ЗАЛ»',
    'Share all my tasks "ЗАЛ" with Маша',
])
async def test_sharing_requires_confirmation_and_preserves_other_readers(client, text):
    owner, masha, bob = [await person(client, name, telegram=False) for name in ("Owner", "Маша", "Bob")]
    await befriend(client, owner, masha)
    await befriend(client, owner, bob)
    private = await create(client, owner, title="ЗАЛ")
    selected = await create(client, owner, title="зал", visibility="selected", shared_friend_ids=[bob['id']])
    public = await create(client, owner, title="ЗАЛ", visibility="friends")
    hidden = await create(client, owner, title="ЗАЛ с тренером")
    await create(client, masha, title="ЗАЛ")
    draft = await say(client, owner, text, send=False)
    assert draft['kind'] == 'send_draft' and draft['type'] == 'schedule_share'
    assert draft['operation'] == 'share' and draft['actions'] == ['send', 'cancel']
    assert 'Существующих задач: 3' in draft['text']
    assert {item['id'] for item in (await schedule(client, masha, owner)).json()['events']} == {public['id']}
    assert (await client.post(f"/api/send-drafts/{draft['draft_id']}/send", headers=masha['headers'])).status_code == 404
    assert (await client.patch(f"/api/send-drafts/{draft['draft_id']}", headers=owner['headers'], json={'title': 'Other'})).status_code == 409
    result = await client.post(f"/api/send-drafts/{draft['draft_id']}/send", headers=owner['headers'])
    assert result.status_code == 200, result.text
    assert result.json()['kind'] == 'updated'
    expected = {private['id'], selected['id'], public['id']}
    assert {item['id'] for item in (await schedule(client, masha, owner)).json()['events']} == expected
    assert {item['id'] for item in (await schedule(client, bob, owner)).json()['events']} == {selected['id'], public['id']}
    assert (await client.get(f"/api/events/{hidden['id']}", headers=owner['headers'])).json()['visibility'] == 'private'
    assert (await client.put(f"/api/events/{private['id']}", headers=masha['headers'], json={'title': 'Hijacked'})).status_code == 404
    assert (await client.post(f"/api/send-drafts/{draft['draft_id']}/send", headers=owner['headers'])).status_code == 409
    async with session_factory() as session:
        assert not await session.scalar(select(FriendTask.id).where(FriendTask.sender_id == owner['id']))
        assert not await session.scalar(select(Notification.id).where(Notification.user_id == masha['id']))


@pytest.mark.parametrize('change', ['cancel', 'unfriend', 'rename'])
async def test_sharing_cancellation_and_stale_drafts_never_grant_access(client, change):
    owner, masha = [await person(client, name, telegram=False) for name in ('Owner', 'Маша')]
    await befriend(client, owner, masha)
    event = await create(client, owner, title='ЗАЛ')
    draft = await say(client, owner, 'Покажи Маше все «ЗАЛ»', send=False)
    if change == 'cancel':
        assert (await client.post(f"/api/send-drafts/{draft['draft_id']}/cancel", headers=owner['headers'])).status_code == 200
    elif change == 'unfriend':
        assert (await client.delete(f"/api/friends/{masha['id']}", headers=owner['headers'])).status_code == 204
    else:
        assert (await client.put(f"/api/events/{event['id']}", headers=owner['headers'], json={'title': 'Private doctor'})).status_code == 200
    assert (await client.post(f"/api/send-drafts/{draft['draft_id']}/send", headers=owner['headers'])).status_code == 409
    saved = (await client.get(f"/api/events/{event['id']}", headers=owner['headers'])).json()
    assert saved['visibility'] == 'private' and saved['shared_friend_ids'] == []


async def test_no_match_unknown_friend_and_foreign_events_cannot_be_shared(client):
    owner, masha = [await person(client, name, telegram=False) for name in ('Owner', 'Маша')]
    await befriend(client, owner, masha)
    foreign = await create(client, masha, title='ЗАЛ')
    reply = await say(client, owner, 'Покажи Маше все «ЗАЛ»', send=False)
    assert reply['kind'] == 'answer' and 'Не нашла' in reply['text']
    reply = await say(client, owner, 'Покажи Незнакомцу все «ЗАЛ»', send=False)
    assert reply['kind'] == 'answer' and 'Уточните друга' in reply['text']
    async with session_factory() as session:
        user = await session.get(User, owner['id'])
        with pytest.raises(schedule_sharing.friends.FriendError):
            await schedule_sharing.sharing_draft(session, user, masha['id'], [foreign['id']], 'ЗАЛ')


async def test_agent_can_propose_sharing_without_turning_it_into_an_assignment(client, agent):
    owner, masha = [await person(client, name, telegram=False) for name in ('Owner', 'Маша')]
    await befriend(client, owner, masha)
    await create(client, owner, title='ЗАЛ')
    agent([act(op('share_schedule', recipient_id=masha['id'], filter={'query': 'ЗАЛ'})), 'Проверьте черновик.'])
    draft = await say(client, owner, 'Открой Маше просмотр моих задач ЗАЛ', send=False)
    assert draft['kind'] == 'send_draft' and draft['type'] == 'schedule_share'
    assert (await schedule(client, masha, owner)).json()['events'] == []
