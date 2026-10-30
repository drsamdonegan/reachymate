from unittest.mock import AsyncMock

import pytest
from dotenv import dotenv_values
from pydantic import SecretStr

from app.scripts.configure_luma import configure, save_credentials
from app.services.events.luma import LumaError


def test_key_is_saved_atomically_without_changing_unrelated_settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# existing config\nOPENAI_API_KEY='synthetic-existing'\nLUMA_API_KEY=old\n")
    save_credentials(env, SecretStr("synthetic-new-key"), "evt-demo")
    parsed = dotenv_values(env)
    assert parsed["OPENAI_API_KEY"] == "synthetic-existing"
    assert parsed["LUMA_API_KEY"] == "synthetic-new-key"
    assert parsed["LUMA_EVENT_ID"] == "evt-demo"
    assert env.stat().st_mode & 0o777 == 0o600
    assert "# existing config" in env.read_text()
    assert list(tmp_path.iterdir()) == [env]


@pytest.mark.asyncio
async def test_failed_validation_does_not_replace_key_and_closes_client(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LUMA_API_KEY=previous\n")
    client = AsyncMock()
    client.event.side_effect = LumaError("Access denied")
    with pytest.raises(LumaError):
        await configure(env, SecretStr("synthetic-key"), "evt-demo", client_factory=lambda _: client)
    assert env.read_text() == "LUMA_API_KEY=previous\n"
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_successful_read_only_validation_precedes_save(tmp_path):
    client = AsyncMock()
    env = tmp_path / ".env"
    await configure(env, SecretStr("synthetic-key"), "evt-demo", client_factory=lambda _: client)
    client.event.assert_awaited_once_with("evt-demo")
    client.close.assert_awaited_once()
    assert dotenv_values(env)["LUMA_API_KEY"] == "synthetic-key"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,event_id", [("", "evt-demo"), ("private\nheader", "evt-demo"), ("secret", "wrong")]
)
async def test_invalid_input_never_contacts_provider(tmp_path, key, event_id):
    def factory(_):
        pytest.fail("Invalid input reached Luma")

    with pytest.raises(ValueError):
        await configure(tmp_path / ".env", SecretStr(key), event_id, client_factory=factory)
    assert not list(tmp_path.iterdir())


def test_refuse_symlink_and_preserve_its_target(tmp_path):
    target = tmp_path / "target"
    target.write_text("original")
    link = tmp_path / ".env"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symbolic"):
        save_credentials(link, SecretStr("secret"), "evt-demo")
    assert target.read_text() == "original"
