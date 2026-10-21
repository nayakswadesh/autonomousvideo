import asyncio
import json

import pytest
from pydantic import ValidationError

from autonomous_studio.hitl.adapters import (
    LocalHITL,
    ReviewMessage,
    TelegramAdapter,
    TelegramConfig,
)


def test_local_request_and_telegram_seam(tmp_path):
    message = ReviewMessage(
        project_id="a" * 12,
        approval_id="b" * 32,
        version=3,
        gate="story",
        text="Please review",
        media=[],
    )
    path = asyncio.run(LocalHITL().send(message, tmp_path))
    assert json.loads(path.read_text())["version"] == 3
    token = "12345:" + "a" * 30
    config = TelegramConfig(bot_token=token, chat_id=123, allowed_user_ids=[42])
    adapter = TelegramAdapter(config)
    payload = adapter.payloads(message)[0]
    callback = payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert len(callback.encode()) <= 64
    assert token not in config.model_dump_json()
    adapter.validate_sender(123, 42)
    with pytest.raises(ValueError, match="unauthorized"):
        adapter.validate_sender(123, 7)
    with pytest.raises(NotImplementedError):
        asyncio.run(adapter.send(message, tmp_path))
    with pytest.raises(ValidationError):
        TelegramConfig(bot_token="invalid", chat_id=1, allowed_user_ids=[])
