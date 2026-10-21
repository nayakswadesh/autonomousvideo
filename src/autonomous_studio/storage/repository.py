from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from autonomous_studio.domain.models import Artifact, Provenance, VideoProject, now


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Repository:
    """SQLite is authoritative; project.json is a rebuildable convenience snapshot."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "studio.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runtime (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runtime_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL, key TEXT NOT NULL,
                    old_value TEXT, new_value TEXT NOT NULL,
                    actor TEXT NOT NULL, external_id TEXT
                );
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def project_dir(self, project_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", project_id):
            raise ValueError("invalid project ID")
        return self.root / project_id

    def resolve(self, project_id: str, relative: str) -> Path:
        base = self.project_dir(project_id).resolve()
        path = (base / relative).resolve()
        if not path.is_relative_to(base) or path == base:
            raise ValueError("artifact path escapes project")
        return path

    @contextmanager
    def lock(self, project_id: str):
        base = self.project_dir(project_id)
        base.mkdir(parents=True, exist_ok=True)
        with (base / ".lock").open("a+b") as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("project is busy in another process; retry later") from exc
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def create(self, project: VideoProject):
        with self.connect() as db:
            db.execute(
                "INSERT INTO projects VALUES (?, ?, ?)",
                (project.id, project.revision, project.model_dump_json()),
            )
            self._event(
                db,
                project.id,
                "created",
                {"state": project.state, "settings": project.creation_settings},
            )
        self.snapshot(project)

    def load(self, project_id: str) -> VideoProject:
        self.project_dir(project_id)
        with self.connect() as db:
            row = db.execute("SELECT body FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise ValueError(f"unknown project: {project_id}")
        return VideoProject.model_validate_json(row[0])

    def all_projects(self):
        with self.connect() as db:
            rows = db.execute("SELECT body FROM projects ORDER BY rowid").fetchall()
        return [VideoProject.model_validate_json(row[0]) for row in rows]

    def get_runtime(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM runtime WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_runtime(self, key, value):
        with self.connect() as db:
            db.execute(
                "INSERT INTO runtime VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=?",
                (key, json.dumps(value), json.dumps(value)),
            )

    def set_runtime_audited(self, key, value, *, actor, external_id=None):
        old = self.get_runtime(key)
        encoded = json.dumps(value)
        with self.connect() as db:
            db.execute(
                "INSERT INTO runtime VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=?",
                (key, encoded, encoded),
            )
            db.execute(
                "INSERT INTO runtime_events(timestamp,key,old_value,new_value,actor,external_id) "
                "VALUES (?,?,?,?,?,?)",
                (
                    now(),
                    key,
                    json.dumps(old) if old is not None else None,
                    encoded,
                    actor,
                    external_id,
                ),
            )

    def save(self, project: VideoProject, kind: str, payload: dict):
        previous = project.revision
        updated = project.model_copy(update={"revision": previous + 1})
        with self.connect() as db:
            result = db.execute(
                "UPDATE projects SET revision=?,body=? WHERE id=? AND revision=?",
                (updated.revision, updated.model_dump_json(), project.id, previous),
            )
            if result.rowcount != 1:
                raise RuntimeError("concurrent project update; reload and retry")
            self._event(db, project.id, kind, payload)
        project.revision = updated.revision
        self.snapshot(project)

    def snapshot(self, project: VideoProject):
        # A snapshot failure cannot roll back an already committed transition.
        try:
            write_json(
                self.project_dir(project.id) / "project.json", project.model_dump(mode="json")
            )
        except OSError:
            pass

    def _event(self, db, project_id, kind, payload):
        db.execute(
            "INSERT INTO events(project_id,timestamp,kind,payload) VALUES (?,?,?,?)",
            (project_id, now(), kind, json.dumps(payload)),
        )

    def events(self, project_id: str):
        with self.connect() as db:
            rows = db.execute(
                "SELECT sequence,timestamp,kind,payload FROM events "
                "WHERE project_id=? ORDER BY sequence",
                (project_id,),
            ).fetchall()
        return [
            dict(sequence=r[0], timestamp=r[1], kind=r[2], payload=json.loads(r[3])) for r in rows
        ]

    def attempt_dir(self, project: VideoProject, stage: str) -> Path:
        path = (
            self.project_dir(project.id)
            / "artifacts"
            / (f"v{project.version:03d}-{stage}-{uuid4().hex[:8]}")
        )
        path.mkdir(parents=True, exist_ok=False)
        return path

    def artifact(
        self,
        project: VideoProject,
        path: Path,
        role: str,
        *,
        provider="local",
        model="v0.1",
        duration=None,
        width=None,
        height=None,
        latency_seconds=0.0,
    ) -> Artifact:
        return Artifact(
            id=uuid4().hex,
            path=path.relative_to(self.project_dir(project.id)).as_posix(),
            role=role,
            version=project.version,
            sha256=digest(path),
            provider=provider,
            model=model,
            input_reference=f"{project.id}/v{project.version}",
            created_at=now(),
            cost=0,
            duration=duration,
            width=width,
            height=height,
            latency_seconds=latency_seconds,
            provenance=Provenance(
                source="studio local procedural/fixture output",
                provider=provider,
                asset_id=path.name,
                model=model,
                license_category="procedural-original",
                origin="original",
                usage_restrictions="Prototype only; review embedded sources.",
            ),
        )

    def verify(self, project: VideoProject, artifact: Artifact) -> Path:
        path = self.resolve(project.id, artifact.path)
        if not path.is_file() or digest(path) != artifact.sha256:
            raise ValueError(f"missing or modified artifact: {artifact.path}")
        return path
