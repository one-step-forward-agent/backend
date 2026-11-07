"""Creation collision handling without an external database."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.dialects import postgresql

from app.services import teams


@pytest.mark.parametrize("name", ["Design", "D" * 100, "🎉", "A"])
async def test_automatic_tag_retries_collision_and_preserves_name(monkeypatch, name):
    team = SimpleNamespace(id=123)
    session = SimpleNamespace(scalar=AsyncMock(side_effect=[0, None, team]), add=Mock(), commit=AsyncMock())
    view = AsyncMock(return_value={"id": team.id})
    monkeypatch.setattr(teams, "team_view", view)
    user = SimpleNamespace(id=456)

    assert await teams.create_team(session, user, name, None, "Anna") == {"id": 123}
    first, second = [call.args[0].compile(dialect=postgresql.dialect()) for call in session.scalar.call_args_list[1:]]
    assert "ON CONFLICT (tag_key) DO NOTHING" in str(second)
    assert first.params["name"] == second.params["name"] == name
    assert first.params["tag"] != second.params["tag"]
    assert second.params["tag"].endswith("_2")
    assert teams.TAG_PATTERN.fullmatch(second.params["tag"])
    assert second.params["tag_key"] == teams.key_of(second.params["tag"])
    session.commit.assert_awaited_once()


async def test_custom_tag_collision_is_a_domain_error():
    session = SimpleNamespace(scalar=AsyncMock(side_effect=[0, None]))
    with pytest.raises(teams.TeamError, match="@Design"):
        await teams.create_team(session, SimpleNamespace(id=456), "Design", "@Design", "Anna")


async def test_invalid_custom_tag_is_rejected_before_database_access():
    session = SimpleNamespace(scalar=AsyncMock())
    with pytest.raises(teams.TeamError):
        await teams.create_team(session, SimpleNamespace(id=456), "Design", "bad tag", "Anna")
    session.scalar.assert_not_awaited()
