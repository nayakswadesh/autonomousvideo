import json
import shutil
from datetime import datetime

import pytest
from google.auth.exceptions import RefreshError

from autonomous_studio.domain.models import PublicationPackage, State
from autonomous_studio.providers.youtube import GoogleYouTubeClient, YouTubePublisher
from autonomous_studio.storage.repository import write_json
from autonomous_studio.workflow import Studio


class FakeYouTube:
    def __init__(self, *, upload_error=None, thumbnail_error=None):
        self.upload_error = upload_error
        self.thumbnail_error = thumbnail_error
        self.uploads = []
        self.thumbnails = []

    def confirmed_channel(self):
        return {"id": "UC-confirmed", "title": "Confirmed Channel"}

    def upload_video(self, video, body):
        self.uploads.append((video, body))
        if self.upload_error:
            raise self.upload_error
        return {"id": "private-video-1", "snippet": {"channelId": "UC-confirmed"}}

    def set_thumbnail(self, video_id, thumbnail):
        self.thumbnails.append((video_id, thumbnail))
        if self.thumbnail_error:
            raise self.thumbnail_error
        return {"items": []}


def test_manual_token_refresh_verifies_channel_before_saving(tmp_path, monkeypatch):
    import googleapiclient.discovery
    from google.oauth2.credentials import Credentials

    token = tmp_path / "token.json"
    token.write_text("old token", encoding="utf-8")
    channel = tmp_path / "channel.json"
    write_json(channel, {"channel_id": "UC-confirmed"})
    monkeypatch.setattr(
        "autonomous_studio.providers.youtube.oauth_paths",
        lambda: (tmp_path / "client.json", token, channel),
    )

    class FakeCredentials:
        refresh_token = "saved-refresh-token"
        valid = True
        expiry = datetime(2030, 1, 1)
        refreshes = 0

        def refresh(self, request):
            self.refreshes += 1

        def to_json(self):
            return '{"token":"new token"}'

    credentials = FakeCredentials()
    monkeypatch.setattr(
        Credentials, "from_authorized_user_file", lambda *args, **kwargs: credentials
    )
    actual_channel_id = ["UC-confirmed"]

    class FakeChannels:
        def list(self, **kwargs):
            return self

        def execute(self):
            return {
                "items": [
                    {"id": actual_channel_id[0], "snippet": {"title": "CryoKnowledge"}}
                ]
            }

    class FakeService:
        def channels(self):
            return FakeChannels()

    monkeypatch.setattr(googleapiclient.discovery, "build", lambda *args, **kwargs: FakeService())
    result = GoogleYouTubeClient().refresh_saved_access_token()

    assert credentials.refreshes == 1
    assert result["channel_title"] == "CryoKnowledge"
    assert token.read_text(encoding="utf-8") == '{"token":"new token"}'
    token.write_text("old token", encoding="utf-8")
    actual_channel_id[0] = "UC-different"
    with pytest.raises(ValueError, match="differs from the confirmed channel"):
        GoogleYouTubeClient().refresh_saved_access_token()
    assert token.read_text(encoding="utf-8") == "old token"


def test_manual_token_refresh_rejection_keeps_saved_token(tmp_path, monkeypatch):
    from google.oauth2.credentials import Credentials

    token = tmp_path / "token.json"
    token.write_text("old token", encoding="utf-8")
    channel = tmp_path / "channel.json"
    write_json(channel, {"channel_id": "UC-confirmed"})
    monkeypatch.setattr(
        "autonomous_studio.providers.youtube.oauth_paths",
        lambda: (tmp_path / "client.json", token, channel),
    )

    class RejectedCredentials:
        refresh_token = "saved-refresh-token"

        def refresh(self, request):
            raise RefreshError("invalid_grant")

    monkeypatch.setattr(
        Credentials, "from_authorized_user_file", lambda *args, **kwargs: RejectedCredentials()
    )
    with pytest.raises(ValueError, match="Google rejected"):
        GoogleYouTubeClient().refresh_saved_access_token()
    assert token.read_text(encoding="utf-8") == "old token"


def completed_project(tmp_path, brief):
    studio = Studio(tmp_path)
    project = studio.create(brief)
    project.state = State.COMPLETED
    output = studio.repo.attempt_dir(project, "publication-test")
    video = output / "final-video.mp4"
    video.write_bytes(b"deterministic-video")
    metadata = output / "youtube-metadata.json"
    write_json(
        metadata,
        {
            "upload": {
                "snippet": {
                    "title": "A private test",
                    "description": "Description",
                    "tags": ["science"],
                    "categoryId": "27",
                    "defaultLanguage": "en",
                },
                "status": {
                    "privacyStatus": "private",
                    "selfDeclaredMadeForKids": False,
                    "containsSyntheticMedia": True,
                },
            }
        },
    )
    thumbnail = output / "selected-thumbnail.png"
    thumbnail.write_bytes(b"deterministic-thumbnail")
    release = output / "release-status.json"
    write_json(
        release,
        {"status": "cleared", "project_id": project.id, "project_version": project.version},
    )
    artifacts = [
        studio.repo.artifact(project, video, "prototype"),
        studio.repo.artifact(project, metadata, "youtube_metadata"),
        studio.repo.artifact(project, thumbnail, "thumbnail_candidate"),
        studio.repo.artifact(project, release, "release_status"),
    ]
    project.artifacts.extend(artifacts)
    project.publication = PublicationPackage(
        title="A private test",
        description="Description",
        hashtags=["#science"],
        title_artifact_id=artifacts[1].id,
        description_artifact_id=artifacts[1].id,
        metadata_artifact_id=artifacts[1].id,
        thumbnail_artifact_id=artifacts[2].id,
        usage_artifact_id=artifacts[1].id,
        release_status_artifact_id=artifacts[3].id,
    )
    root = studio.repo.project_dir(project.id)
    for name, source in {
        "final-video.mp4": video,
        "youtube-metadata.json": metadata,
        "selected-thumbnail.png": thumbnail,
        "release-status.json": release,
    }.items():
        shutil.copyfile(source, root / name)
    studio.repo.save(project, "completed_fixture", {})
    return studio, project


def test_private_upload_uses_cleared_package_and_is_idempotent(tmp_path, brief):
    studio, project = completed_project(tmp_path, brief)
    client = FakeYouTube()
    publisher = YouTubePublisher(studio.repo, client)

    result = publisher.publish(project.id)

    assert result.status == "uploaded" and result.video_id == "private-video-1"
    assert result.channel_id == "UC-confirmed" and result.thumbnail_set is True
    assert client.uploads[0][1]["status"] == {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": True,
    }
    assert len(client.thumbnails) == 1
    payload = client.uploads[0][1]
    assert payload["snippet"]["defaultLanguage"] == "en"
    assert payload["snippet"]["defaultAudioLanguage"] == "en"
    assert payload["paidProductPlacementDetails"] == {"hasPaidProductPlacement": False}
    repeated = publisher.publish(project.id)
    assert repeated.video_id == result.video_id and len(client.uploads) == 1
    receipt = json.loads(
        (studio.repo.project_dir(project.id) / "youtube-upload.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "uploaded" and receipt["made_for_kids"] is False


def test_unknown_upload_outcome_is_not_replayed(tmp_path, brief):
    studio, project = completed_project(tmp_path, brief)
    client = FakeYouTube(upload_error=TimeoutError("connection ended"))
    publisher = YouTubePublisher(studio.repo, client)

    result = publisher.publish(project.id)

    assert result.status == "ambiguous" and not result.video_id
    with pytest.raises(ValueError, match="unresolved outcome"):
        publisher.publish(project.id)
    assert len(client.uploads) == 1


def test_thumbnail_failure_retries_thumbnail_without_duplicate_video(tmp_path, brief):
    studio, project = completed_project(tmp_path, brief)
    client = FakeYouTube(thumbnail_error=RuntimeError("thumbnail unavailable"))
    publisher = YouTubePublisher(studio.repo, client)
    first = publisher.publish(project.id)
    assert first.status == "uploaded_thumbnail_failed" and len(client.uploads) == 1

    client.thumbnail_error = None
    second = publisher.publish(project.id)

    assert second.status == "uploaded" and second.thumbnail_set is True
    assert len(client.uploads) == 1 and len(client.thumbnails) == 2


def test_upload_rejects_incomplete_project(tmp_path, brief):
    studio = Studio(tmp_path)
    project = studio.create(brief)
    with pytest.raises(ValueError, match="completed project"):
        YouTubePublisher(studio.repo, FakeYouTube()).publish(project.id)


def test_upload_uses_final_selected_thumbnail_not_earlier_candidate(tmp_path, brief):
    from autonomous_studio.storage.repository import digest

    studio, project = completed_project(tmp_path, brief)
    original = project.publication.thumbnail_artifact_id
    output = studio.repo.attempt_dir(project, "final-thumbnail-selection")
    selected = output / "approved-thumbnail.png"
    selected.write_bytes(b"new final publication selection")
    artifact = studio.repo.artifact(project, selected, "thumbnail_candidate")
    project.artifacts.append(artifact)
    project.publication.thumbnail_artifact_id = artifact.id
    root = studio.repo.project_dir(project.id)
    shutil.copyfile(selected, root / "selected-thumbnail.png")
    metadata_before = (root / "youtube-metadata.json").read_bytes()
    studio.repo.save(project, "final_thumbnail_selected", {})
    client = FakeYouTube()
    upload = YouTubePublisher(studio.repo, client).publish(project.id)
    assert project.publication.thumbnail_artifact_id != original
    assert client.thumbnails[0][1].read_bytes() == selected.read_bytes()
    assert upload.source_sha256["thumbnail"] == digest(selected)
    assert (root / "youtube-metadata.json").read_bytes() == metadata_before


def test_upload_rejects_thumbnail_that_differs_from_final_selection(tmp_path, brief):
    studio, project = completed_project(tmp_path, brief)
    root = studio.repo.project_dir(project.id)
    (root / "selected-thumbnail.png").write_bytes(b"unapproved replacement")
    client = FakeYouTube()
    with pytest.raises(ValueError, match="selected-thumbnail.png"):
        YouTubePublisher(studio.repo, client).publish(project.id)
    assert not client.uploads and not client.thumbnails


def test_google_upload_requests_the_paid_placement_part(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fixture")
    calls = []

    class Service:
        def videos(self):
            return self

        def insert(self, **kwargs):
            calls.append(kwargs)
            return self

        def next_chunk(self):
            return None, {"id": "test-video"}

    client = GoogleYouTubeClient()
    monkeypatch.setattr(client, "_service", lambda: Service())
    body = {"paidProductPlacementDetails": {"hasPaidProductPlacement": False}}
    assert client.upload_video(video, body)["id"] == "test-video"
    assert calls[0]["part"] == "snippet,status,paidProductPlacementDetails"
    assert calls[0]["body"] == body
