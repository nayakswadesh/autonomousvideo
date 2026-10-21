"""A failed long-form Telegram upload must not strand the review gate."""

import asyncio
from pathlib import Path

from autonomous_studio.domain.models import Approval, State
from autonomous_studio.hitl.adapters import TelegramAdapter, TelegramConfig, review_message
from autonomous_studio.providers.http import RemoteError
from autonomous_studio.storage.repository import digest
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "longform-pilot.yaml"


def test_longform_upload_timeout_uses_verified_local_copy_and_sends_buttons(tmp_path):
    studio = Studio(tmp_path)
    project = studio.create(load_brief(EXAMPLE))
    movie = studio.repo.project_dir(project.id) / "review.mp4"
    movie.write_bytes(b"a validated test movie")
    artifact = studio.repo.artifact(project, movie, "prototype")
    project.artifacts.append(artifact)
    project.state = State.LONGFORM_REVIEW_APPROVAL
    approval = Approval(
        id="longform-review",
        project_id=project.id,
        gate="prototype",
        version=project.version,
        artifact_ids=[artifact.id],
    )
    project.approvals.append(approval)
    studio.repo.save(project, "test_review_ready", {})

    class TimeoutTelegram(TelegramAdapter):
        def __init__(self):
            super().__init__(
                TelegramConfig(bot_token="123:" + "a" * 30, chat_id=123, allowed_user_ids=[42]),
                studio.repo,
            )
            self.messages = []
            self.uploads = 0

        async def call(self, method, payload):
            assert method == "sendMessage"
            self.messages.append(payload)
            return {"message_id": len(self.messages)}

        async def upload(self, method, path, caption):
            self.uploads += 1
            raise RemoteError("Telegram upload", kind="timeout")

    adapter = TimeoutTelegram()
    message = review_message(project, approval)
    asyncio.run(adapter.deliver(message))

    local = studio.repo.project_dir(project.id) / (
        f"review-video-v{project.version:03d}-{artifact.sha256[:8]}.mp4"
    )
    assert local.is_file()
    assert digest(local) == artifact.sha256
    assert adapter.uploads == 1
    assert "could not confirm" in adapter.messages[-2]["text"]
    assert "inline_keyboard" in adapter.messages[-1]["reply_markup"]

    asyncio.run(adapter.deliver(message))
    assert adapter.uploads == 1
    assert len(adapter.messages) == 3
