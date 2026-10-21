"""Private-only YouTube upload with durable, fail-closed receipts."""

from __future__ import annotations

import json
import os
import shutil
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from autonomous_studio.domain.models import State, YouTubeUpload, now
from autonomous_studio.storage.repository import Repository, digest, write_json

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def oauth_paths():
    client = Path(os.getenv("YOUTUBE_OAUTH_CLIENT", "secrets/youtube-oauth-client.json")).resolve()
    token = Path(os.getenv("YOUTUBE_OAUTH_TOKEN", "secrets/youtube-oauth-token.json")).resolve()
    channel = Path(os.getenv("YOUTUBE_CHANNEL_PROFILE", "secrets/youtube-channel.json")).resolve()
    return client, token, channel


def _handle(value):
    handle = value.rstrip("/").rsplit("/", 1)[-1]
    if not handle.startswith("@"):
        handle = "@" + handle
    if len(handle) < 2 or any(character.isspace() for character in handle):
        raise ValueError("expected channel must be a public YouTube @handle or channel URL")
    return handle


def authorize_youtube(expected_channel):
    """Run the installed-app consent flow and persist only the refreshable user token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    client, token, channel_path = oauth_paths()
    if not client.is_file():
        raise ValueError(f"YouTube OAuth client file is missing: {client}")
    data = json.loads(client.read_text(encoding="utf-8"))
    installed = data.get("installed")
    if not isinstance(installed, dict) or not installed.get("client_id"):
        raise ValueError("YouTube OAuth JSON must be a Desktop app credential")
    flow = InstalledAppFlow.from_client_secrets_file(str(client), YOUTUBE_SCOPES)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    token.parent.mkdir(parents=True, exist_ok=True)
    temporary = token.with_name(f".{token.name}.{uuid4().hex}.tmp")
    temporary.write_text(credentials.to_json(), encoding="utf-8")
    os.replace(temporary, token)
    api = GoogleYouTubeClient()
    actual = api.channel()
    expected = api.channel_for_handle(_handle(expected_channel))
    if not expected or actual["id"] != expected["id"]:
        raise ValueError(
            f"OAuth selected channel {actual['title']} ({actual['id']}), not "
            f"{_handle(expected_channel)}. Set the intended YouTube channel as the account's "
            "default and authorize again."
        )
    profile = {
        "schema_version": 1,
        "channel_id": actual["id"],
        "channel_title": actual["title"],
        "expected_handle": _handle(expected_channel),
        "confirmed_at": now(),
    }
    channel_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(channel_path, profile)
    return profile


class GoogleYouTubeClient:
    def __init__(self):
        self.client_path, self.token_path, self.channel_path = oauth_paths()

    def _save_credentials(self, credentials):
        temporary = self.token_path.with_name(f".{self.token_path.name}.{uuid4().hex}.tmp")
        temporary.write_text(credentials.to_json(), encoding="utf-8")
        os.replace(temporary, self.token_path)

    def _credentials(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        if not self.token_path.is_file():
            raise ValueError("YouTube is not authorized; run `studio youtube-auth` first")
        credentials = Credentials.from_authorized_user_file(str(self.token_path), YOUTUBE_SCOPES)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            self._save_credentials(credentials)
        if not credentials.valid:
            raise ValueError("YouTube authorization is invalid; run `studio youtube-auth` again")
        return credentials

    def refresh_saved_access_token(self):
        """Force a token refresh and verify the channel before saving; never upload media."""
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        if not self.token_path.is_file():
            raise ValueError("No saved YouTube token; run `studio youtube-auth` locally")
        if not self.channel_path.is_file():
            raise ValueError("YouTube channel is not confirmed; run `studio youtube-auth` locally")
        confirmed = json.loads(self.channel_path.read_text(encoding="utf-8"))
        credentials = Credentials.from_authorized_user_file(str(self.token_path), YOUTUBE_SCOPES)
        if not credentials.refresh_token:
            raise ValueError(
                "Saved YouTube token cannot refresh; run `studio youtube-auth` locally"
            )
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            raise ValueError(
                "Google rejected the saved refresh token; run `studio youtube-auth` locally"
            ) from exc
        if not credentials.valid:
            raise ValueError(
                "YouTube authorization remains invalid; run `studio youtube-auth` locally"
            )
        try:
            service = build("youtube", "v3", credentials=credentials, cache_discovery=False)
            actual = self._channel(
                service.channels().list(part="id,snippet", mine=True, maxResults=1).execute()
            )
        except Exception as exc:
            raise RuntimeError(
                "Could not verify the authorized YouTube channel; saved token was not changed"
            ) from exc
        if actual["id"] != confirmed.get("channel_id"):
            raise ValueError(
                "Authorized YouTube channel differs from the confirmed channel; "
                "run `studio youtube-auth` locally"
            )
        self._save_credentials(credentials)
        expiry = credentials.expiry
        return {
            "channel_title": actual["title"],
            "expires_at_utc": expiry.replace(tzinfo=timezone.utc).isoformat() if expiry else None,
        }

    def _service(self):
        from googleapiclient.discovery import build

        return build("youtube", "v3", credentials=self._credentials(), cache_discovery=False)

    @staticmethod
    def _channel(response):
        items = response.get("items", [])
        if len(items) != 1:
            raise ValueError("YouTube authorization did not resolve exactly one channel")
        return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}

    def channel(self):
        response = (
            self._service().channels().list(part="id,snippet", mine=True, maxResults=1).execute()
        )
        return self._channel(response)

    def channel_for_handle(self, handle):
        response = (
            self._service()
            .channels()
            .list(part="id,snippet", forHandle=handle, maxResults=1)
            .execute()
        )
        return self._channel(response) if response.get("items") else None

    def confirmed_channel(self):
        if not self.channel_path.is_file():
            raise ValueError("YouTube channel is not confirmed; run `studio youtube-auth` first")
        confirmed = json.loads(self.channel_path.read_text(encoding="utf-8"))
        actual = self.channel()
        if actual["id"] != confirmed.get("channel_id"):
            raise ValueError(
                "authorized YouTube channel changed; run `studio youtube-auth` and confirm it again"
            )
        return actual

    def upload_video(self, video: Path, body: dict):
        from googleapiclient.http import MediaFileUpload

        request = (
            self._service()
            .videos()
            .insert(
                part="snippet,status,paidProductPlacementDetails",
                body=body,
                notifySubscribers=False,
                media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True),
            )
        )
        response = None
        while response is None:
            _, response = request.next_chunk()
        return response

    def set_thumbnail(self, video_id: str, thumbnail: Path):
        from googleapiclient.http import MediaFileUpload

        return (
            self._service()
            .thumbnails()
            .set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail), mimetype="image/png", resumable=False),
            )
            .execute()
        )


class YouTubePublisher:
    """Upload one cleared project version once; ambiguous outcomes never auto-replay."""

    ROOT_FILES = {
        "video": "final-video.mp4",
        "metadata": "youtube-metadata.json",
        "thumbnail": "selected-thumbnail.png",
        "release": "release-status.json",
    }

    def __init__(self, repo: Repository, client=None):
        self.repo = repo
        self.client = client or GoogleYouTubeClient()

    def refresh_authorization(self):
        return self.client.refresh_saved_access_token()

    @staticmethod
    def _artifact(project, artifact_id):
        return next((item for item in project.artifacts if item.id == artifact_id), None)

    def _package(self, project):
        if project.state != State.COMPLETED:
            raise ValueError("YouTube upload requires a completed project")
        if project.publication is None:
            raise ValueError("completed project has no cleared publication package")
        current_video = project.current_artifacts("prototype")
        if not current_video:
            raise ValueError("completed project has no final video")
        expected = {
            "video": current_video[-1],
            "metadata": self._artifact(project, project.publication.metadata_artifact_id),
            "thumbnail": self._artifact(project, project.publication.thumbnail_artifact_id),
            "release": self._artifact(project, project.publication.release_status_artifact_id),
        }
        if any(value is None for value in expected.values()):
            raise ValueError("publication package references a missing artifact")
        root = self.repo.project_dir(project.id)
        files = {key: root / name for key, name in self.ROOT_FILES.items()}
        for key, path in files.items():
            if not path.is_file() or digest(path) != expected[key].sha256:
                raise ValueError(f"root publication file is missing or modified: {path.name}")
        release = json.loads(files["release"].read_text(encoding="utf-8"))
        if release.get("status") != "cleared" or release.get("project_version") != project.version:
            raise ValueError("publication release is not cleared for the current project version")
        metadata = json.loads(files["metadata"].read_text(encoding="utf-8"))
        body = metadata.get("upload")
        if not isinstance(body, dict):
            raise ValueError("YouTube metadata has no upload payload")
        status = body.get("status", {})
        if status.get("privacyStatus") != "private":
            raise ValueError("automatic YouTube uploads must remain private")
        if status.get("selfDeclaredMadeForKids") is not False:
            raise ValueError("automatic YouTube uploads must declare not made for kids")
        # Apply upload defaults in memory, including for previously completed
        # packages. Never rewrite approved metadata files or their hashes.
        snippet = body.setdefault("snippet", {})
        snippet.setdefault("defaultLanguage", project.spec.brief.voice.language)
        snippet.setdefault("defaultAudioLanguage", project.spec.brief.voice.language)
        promotion = body.setdefault("paidProductPlacementDetails", {})
        if promotion.get("hasPaidProductPlacement") is True:
            raise ValueError("automatic uploads are configured for videos without paid promotion")
        promotion["hasPaidProductPlacement"] = False
        return files, body

    def _begin(self, project_id, channel):
        with self.repo.lock(project_id):
            project = self.repo.load(project_id)
            files, body = self._package(project)
            for upload in reversed(project.youtube_uploads):
                if upload.project_version != project.version:
                    continue
                if upload.status == "uploaded":
                    return project, files, body, upload, "already_uploaded"
                if upload.status == "uploaded_thumbnail_failed" and upload.video_id:
                    return project, files, body, upload, "thumbnail_only"
                raise ValueError(
                    "a previous upload has an unresolved outcome; inspect YouTube Studio "
                    "before attempting another upload"
                )
            upload = YouTubeUpload(
                id=uuid4().hex,
                project_version=project.version,
                status="uploading",
                channel_id=channel["id"],
                source_sha256={key: digest(path) for key, path in files.items()},
            )
            project.youtube_uploads.append(upload)
            self.repo.save(
                project,
                "youtube_upload_started",
                {
                    "upload_id": upload.id,
                    "version": project.version,
                    "privacy_status": "private",
                    "made_for_kids": False,
                    "audio_language": body["snippet"]["defaultAudioLanguage"],
                    "paid_promotion": body["paidProductPlacementDetails"][
                        "hasPaidProductPlacement"
                    ],
                    "thumbnail_sha256": upload.source_sha256["thumbnail"],
                },
            )
            return project, files, body, upload, "new"

    def _finish(self, project_id, upload_id, *, status, response=None, message=""):
        with self.repo.lock(project_id):
            project = self.repo.load(project_id)
            upload = next(item for item in project.youtube_uploads if item.id == upload_id)
            response = response or {}
            upload.status = status
            upload.completed_at = now()
            upload.video_id = response.get("id") or upload.video_id
            upload.channel_id = response.get("snippet", {}).get("channelId") or upload.channel_id
            upload.thumbnail_set = status == "uploaded"
            upload.message = message
            output = self.repo.attempt_dir(project, "youtube-upload")
            receipt = output / "youtube-upload.json"
            write_json(receipt, upload.model_dump(mode="json", exclude={"receipt_artifact_id"}))
            artifact = self.repo.artifact(
                project, receipt, "youtube_upload_receipt", provider="youtube", model="v3"
            )
            project.artifacts.append(artifact)
            upload.receipt_artifact_id = artifact.id
            root_receipt = self.repo.project_dir(project.id) / "youtube-upload.json"
            temporary = root_receipt.with_name(f".{root_receipt.name}.{uuid4().hex}.tmp")
            shutil.copyfile(receipt, temporary)
            os.replace(temporary, root_receipt)
            self.repo.save(
                project,
                "youtube_upload_finished",
                {
                    "upload_id": upload.id,
                    "version": upload.project_version,
                    "status": upload.status,
                    "video_id": upload.video_id,
                    "thumbnail_set": upload.thumbnail_set,
                },
            )
            return upload

    def _ambiguous(self, project_id, upload_id, exc):
        return self._finish(
            project_id,
            upload_id,
            status="ambiguous",
            message=(
                "The API outcome could not be proven. Inspect YouTube Studio before retrying: "
                + str(exc)[:500]
            ),
        )

    def publish(self, project_id):
        channel = self.client.confirmed_channel()
        _, files, body, upload, action = self._begin(project_id, channel)
        if action == "already_uploaded":
            return upload
        if action == "thumbnail_only":
            try:
                self.client.set_thumbnail(upload.video_id, files["thumbnail"])
            except Exception as exc:
                return self._finish(
                    project_id,
                    upload.id,
                    status="uploaded_thumbnail_failed",
                    response={"id": upload.video_id},
                    message=str(exc)[:500],
                )
            return self._finish(
                project_id,
                upload.id,
                status="uploaded",
                response={"id": upload.video_id},
                message="Private upload and custom thumbnail confirmed.",
            )
        try:
            response = self.client.upload_video(files["video"], body)
        except Exception as exc:
            return self._ambiguous(project_id, upload.id, exc)
        video_id = response.get("id")
        if not video_id:
            return self._ambiguous(project_id, upload.id, "YouTube returned no video ID")
        try:
            self.client.set_thumbnail(video_id, files["thumbnail"])
        except Exception as exc:
            return self._finish(
                project_id,
                upload.id,
                status="uploaded_thumbnail_failed",
                response=response,
                message=str(exc)[:500],
            )
        return self._finish(
            project_id,
            upload.id,
            status="uploaded",
            response=response,
            message="Private upload and custom thumbnail confirmed.",
        )
