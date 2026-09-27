"""Minimal project identity, usage adapters, and durable SQLite storage for BurnLog."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit


class IdentityConflict(ValueError):
    """Raised when evidence would combine distinct authoritative repositories."""


class RecordConflict(ValueError):
    """Raised when an immutable record key is reused with different data."""


def default_database_path() -> Path:
    if state_dir := os.environ.get("HERDR_PLUGIN_STATE_DIR"):
        return Path(state_dir).expanduser() / "burnlog.sqlite3"
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return base.expanduser() / "herdr-burnlog" / "burnlog.sqlite3"


def normalize_remote(remote: str, base: Path | None = None) -> str:
    """Normalize common Git URL forms without contacting the remote."""
    remote = remote.strip()
    scp = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", remote)
    if scp and "://" not in remote and not re.match(r"^[A-Za-z]:[\\/]", remote):
        host, path = scp.groups()
        value = f"{host.lower()}/{path.lstrip('/')}"
    elif "://" in remote:
        parsed = urlsplit(remote)
        if parsed.scheme == "file":
            value = "file:" + str(Path(unquote(parsed.path)).resolve())
        else:
            host = (parsed.hostname or "").lower()
            if parsed.port:
                host += f":{parsed.port}"
            value = f"{host}/{unquote(parsed.path).lstrip('/')}"
    else:
        path = Path(remote).expanduser()
        if not path.is_absolute() and base:
            path = base / path
        value = "file:" + str(path.resolve())
    value = value.rstrip("/")
    if not value.startswith("file:") and value.endswith(".git"):
        value = value[:-4]
    if value.lower().startswith("github.com/"):
        value = value.lower()
    return value


def _git(path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _repository_evidence(path: Path) -> dict:
    path = path.expanduser().resolve()
    if path.is_file():
        path = path.parent
    root_text = _git(path, "rev-parse", "--show-toplevel")
    if not root_text:
        return {"location": str(path), "root": None, "common": None, "remotes": [], "marker": None}

    root = Path(root_text).resolve()
    common_text = _git(path, "rev-parse", "--git-common-dir")
    common = None
    if common_text:
        candidate = Path(common_text)
        common = str((candidate if candidate.is_absolute() else path / candidate).resolve())

    remotes = []
    config = _git(path, "config", "--get-regexp", r"^remote\..*\.url$") or ""
    for line in config.splitlines():
        key, separator, url = line.partition(" ")
        if separator:
            name = key[len("remote.") : -len(".url")]
            remotes.append((name, normalize_remote(url, root)))
    remotes.sort(key=lambda item: (item[0] != "origin", item[0], item[1]))

    marker = None
    for marker_path in (Path(common) / "burnlog-project-id" if common else None, root / ".burnlog-project-id"):
        if marker_path and marker_path.is_file():
            value = marker_path.read_text(encoding="utf-8").strip()
            if value and len(value) <= 256:
                marker = value
                break

    return {
        "location": str(root),
        "root": str(root),
        "common": common,
        "remotes": remotes,
        "marker": marker,
    }


class Store:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else default_database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                github_id TEXT UNIQUE,
                identity_kind TEXT NOT NULL,
                identity_value TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS aliases (
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                PRIMARY KEY (kind, value)
            );
            CREATE TABLE IF NOT EXISTS locations (
                path TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (source, external_id)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
                cwd TEXT,
                source_version TEXT,
                started_at TEXT,
                updated_at TEXT,
                PRIMARY KEY (agent, session_id)
            );
            CREATE TABLE IF NOT EXISTS usage_records (
                agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                evidence_id TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                timestamp TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                total_tokens INTEGER,
                cost_input REAL,
                cost_output REAL,
                cost_cache_read REAL,
                cost_cache_write REAL,
                cost_total REAL,
                cost_provenance TEXT NOT NULL CHECK(cost_provenance IN ('recorded','estimated','unavailable')),
                PRIMARY KEY (agent, session_id, evidence_id),
                FOREIGN KEY (agent, session_id) REFERENCES sessions(agent, session_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS model_events (
                agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                evidence_id TEXT NOT NULL,
                provider TEXT,
                model TEXT NOT NULL,
                timestamp TEXT,
                PRIMARY KEY (agent, session_id, evidence_id),
                FOREIGN KEY (agent, session_id) REFERENCES sessions(agent, session_id) ON DELETE CASCADE
            );
            """
        )

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _new_project(self, kind: str, value: str, github_id: str | None = None) -> str:
        project_id = str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?)",
            (project_id, github_id, kind, value, self._now()),
        )
        return project_id

    def _alias_project(self, kind: str, value: str) -> str | None:
        row = self.db.execute(
            "SELECT project_id FROM aliases WHERE kind = ? AND value = ?", (kind, value)
        ).fetchone()
        return row[0] if row else None

    def _bind_alias(self, project_id: str, kind: str, value: str) -> None:
        existing = self._alias_project(kind, value)
        if existing and existing != project_id:
            raise IdentityConflict(f"{kind} alias already belongs to another project")
        self.db.execute(
            "INSERT OR IGNORE INTO aliases(kind, value, project_id) VALUES (?, ?, ?)",
            (kind, value, project_id),
        )

    def _github_id(self, project_id: str) -> str | None:
        row = self.db.execute("SELECT github_id FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            raise KeyError(project_id)
        return row[0]

    def _merge(self, source_id: str, target_id: str) -> None:
        if source_id == target_id:
            return
        source_github = self._github_id(source_id)
        target_github = self._github_id(target_id)
        if source_github and target_github and source_github != target_github:
            raise IdentityConflict("cannot merge projects with different GitHub repository IDs")
        self.db.execute("UPDATE aliases SET project_id = ? WHERE project_id = ?", (target_id, source_id))
        self.db.execute("UPDATE locations SET project_id = ? WHERE project_id = ?", (target_id, source_id))
        self.db.execute("UPDATE records SET project_id = ? WHERE project_id = ?", (target_id, source_id))
        self.db.execute("UPDATE sessions SET project_id = ? WHERE project_id = ?", (target_id, source_id))
        if source_github and not target_github:
            self.db.execute("UPDATE projects SET github_id = ? WHERE id = ?", (source_github, target_id))
        self.db.execute("DELETE FROM projects WHERE id = ?", (source_id,))

    def reconcile(self, project_id: str, github_id: str | int) -> str:
        """Attach verified GitHub identity, merging only provisional history."""
        github_id = str(github_id).strip()
        if not github_id:
            raise ValueError("github_id must be a non-empty verified repository ID")
        with self.db:
            current = self._github_id(project_id)
            if current and current != github_id:
                raise IdentityConflict("project already has a different GitHub repository ID")
            row = self.db.execute("SELECT id FROM projects WHERE github_id = ?", (github_id,)).fetchone()
            target = row[0] if row else project_id
            if target != project_id:
                self._merge(project_id, target)
            else:
                self.db.execute("UPDATE projects SET github_id = ? WHERE id = ?", (github_id, target))
            self._bind_alias(target, "github-id", github_id)
            return target

    def resolve_project(self, path: str | os.PathLike, github_id: str | int | None = None) -> str:
        """Resolve a path using only local Git evidence and optional verified GitHub ID."""
        evidence = _repository_evidence(Path(path))
        remote_values = [value for _name, value in evidence["remotes"][:1]]
        ordered_aliases = [("git-remote", value) for value in remote_values]
        if evidence["marker"]:
            ordered_aliases.append(("local-marker", evidence["marker"]))
        if evidence["common"]:
            ordered_aliases.append(("git-common-dir", evidence["common"]))
        ordered_aliases.append(("git-root" if evidence["root"] else "cwd", evidence["location"]))

        with self.db:
            known = []
            for kind, value in ordered_aliases:
                project = self._alias_project(kind, value)
                if project and project not in known:
                    known.append(project)
            location_row = self.db.execute(
                "SELECT project_id FROM locations WHERE path = ?", (evidence["location"],)
            ).fetchone()
            if location_row and location_row[0] not in known:
                known.append(location_row[0])

            # A reused checkout path is not evidence that a new remote is the old repo.
            if github_id is None and remote_values:
                for candidate in known:
                    stored_remotes = {
                        row[0] for row in self.db.execute(
                            "SELECT value FROM aliases WHERE kind = 'git-remote' AND project_id = ?",
                            (candidate,),
                        )
                    }
                    if stored_remotes and remote_values[0] not in stored_remotes:
                        raise IdentityConflict("changed primary remote requires verified reconciliation")

            if github_id is not None:
                github_id = str(github_id).strip()
                if not github_id:
                    raise ValueError("github_id must be a non-empty verified repository ID")
                row = self.db.execute("SELECT id FROM projects WHERE github_id = ?", (github_id,)).fetchone()
                project_id = row[0] if row else (known[0] if known else None)
                if project_id is None:
                    project_id = self._new_project("github-id", github_id, github_id)
                for candidate in known:
                    candidate_github = self._github_id(candidate)
                    if candidate_github and candidate_github != github_id:
                        raise IdentityConflict("local evidence conflicts with the supplied GitHub repository ID")
                if not self._github_id(project_id):
                    self.db.execute("UPDATE projects SET github_id = ? WHERE id = ?", (github_id, project_id))
                for candidate in known:
                    self._merge(candidate, project_id)
                self._bind_alias(project_id, "github-id", github_id)
            else:
                authoritative = {self._github_id(candidate) for candidate in known if self._github_id(candidate)}
                if len(authoritative) > 1 or len(known) > 1:
                    raise IdentityConflict("repository evidence points to different stored projects")
                project_id = known[0] if known else None
                if project_id is None:
                    if remote_values:
                        kind, value = "git-remote", remote_values[0]
                    elif evidence["marker"]:
                        kind, value = "local-marker", evidence["marker"]
                    else:
                        kind, value = ("git-root" if evidence["root"] else "cwd", evidence["location"])
                    project_id = self._new_project(kind, value)

            # Bind only the primary remote: secondary remotes may be unrelated upstream forks.
            if remote_values:
                self._bind_alias(project_id, "git-remote", remote_values[0])
            if evidence["marker"]:
                self._bind_alias(project_id, "local-marker", evidence["marker"])
            if evidence["common"]:
                self._bind_alias(project_id, "git-common-dir", evidence["common"])
            if not remote_values and not evidence["marker"]:
                self._bind_alias(
                    project_id, "git-root" if evidence["root"] else "cwd", evidence["location"]
                )
            self.db.execute(
                """INSERT INTO locations(path, project_id, last_seen) VALUES (?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET project_id=excluded.project_id, last_seen=excluded.last_seen""",
                (evidence["location"], project_id, self._now()),
            )
            return project_id

    def add_record(self, project_id: str, source: str, external_id: str, payload: dict) -> bool:
        """Persist an immutable source record; return False for an exact duplicate."""
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self.db:
            row = self.db.execute(
                "SELECT project_id, payload FROM records WHERE source = ? AND external_id = ?",
                (source, external_id),
            ).fetchone()
            if row:
                if row[0] != project_id or row[1] != encoded:
                    raise RecordConflict("record key already exists with different project or payload")
                return False
            self.db.execute(
                "INSERT INTO records(project_id, source, external_id, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (project_id, source, external_id, encoded, self._now()),
            )
            return True

    def records(self, project_id: str) -> list[dict]:
        return [
            {"source": row[0], "external_id": row[1], "payload": json.loads(row[2])}
            for row in self.db.execute(
                "SELECT source, external_id, payload FROM records WHERE project_id = ? ORDER BY id",
                (project_id,),
            )
        ]

    def save_session(
        self,
        agent: str,
        session_id: str,
        project_id: str | None,
        cwd: str | None,
        source_version: str | None,
        started_at: str | None,
        updated_at: str | None,
        usage: list[dict],
        model_events: list[dict],
    ) -> int:
        """Upsert one parsed session and return the number of changed usage rows."""
        with self.db:
            existing = self.db.execute(
                "SELECT project_id FROM sessions WHERE agent=? AND session_id=?",
                (agent, session_id),
            ).fetchone()
            if existing and existing[0] and project_id and existing[0] != project_id:
                raise IdentityConflict("session cwd points to a different project")
            self.db.execute(
                """INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(agent, session_id) DO UPDATE SET
                     project_id=COALESCE(sessions.project_id, excluded.project_id),
                     cwd=COALESCE(excluded.cwd, sessions.cwd),
                     source_version=COALESCE(excluded.source_version, sessions.source_version),
                     started_at=COALESCE(sessions.started_at, excluded.started_at),
                     updated_at=MAX(COALESCE(sessions.updated_at, ''), COALESCE(excluded.updated_at, ''))""",
                (agent, session_id, project_id, cwd, source_version, started_at, updated_at),
            )
            changed = 0
            columns = (
                "provider", "model", "timestamp", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_tokens", "total_tokens", "cost_input",
                "cost_output", "cost_cache_read", "cost_cache_write", "cost_total",
                "cost_provenance",
            )
            assignments = ", ".join(f"{name}=excluded.{name}" for name in columns)
            differs = " OR ".join(f"usage_records.{name} IS NOT excluded.{name}" for name in columns)
            sql = f"""INSERT INTO usage_records
                (agent, session_id, evidence_id, {', '.join(columns)})
                VALUES ({', '.join('?' for _ in range(3 + len(columns)))})
                ON CONFLICT(agent, session_id, evidence_id) DO UPDATE SET {assignments}
                WHERE {differs}"""
            for item in usage:
                before = self.db.total_changes
                self.db.execute(
                    sql,
                    (agent, session_id, item["evidence_id"], *(item.get(name) for name in columns)),
                )
                changed += self.db.total_changes > before
            for event in model_events:
                self.db.execute(
                    """INSERT INTO model_events VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(agent, session_id, evidence_id) DO UPDATE SET
                         provider=excluded.provider, model=excluded.model, timestamp=excluded.timestamp
                       WHERE model_events.provider IS NOT excluded.provider
                          OR model_events.model IS NOT excluded.model
                          OR model_events.timestamp IS NOT excluded.timestamp""",
                    (agent, session_id, event["evidence_id"], event.get("provider"),
                     event["model"], event.get("timestamp")),
                )
            return changed

    def sessions(self) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM sessions ORDER BY agent, session_id"
        )]

    def usage(self, agent: str, session_id: str) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            """SELECT evidence_id, provider, model, timestamp, input_tokens, output_tokens,
                      cache_read_tokens, cache_write_tokens, total_tokens, cost_input,
                      cost_output, cost_cache_read, cost_cache_write, cost_total, cost_provenance
               FROM usage_records WHERE agent=? AND session_id=? ORDER BY timestamp, evidence_id""",
            (agent, session_id),
        )]

    def models(self, agent: str, session_id: str) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            """SELECT evidence_id, provider, model, timestamp FROM model_events
               WHERE agent=? AND session_id=? ORDER BY timestamp, evidence_id""",
            (agent, session_id),
        )]


def _jsonl_files(paths, default: Path) -> list[Path]:
    if paths is None:
        return sorted(default.glob("**/*.jsonl"))
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    files = []
    for value in paths:
        path = Path(value).expanduser()
        files.extend(sorted(path.glob("**/*.jsonl")) if path.is_dir() else [path])
    return files


def _read_jsonl(path: Path) -> tuple[list[dict], int]:
    records, incomplete = [], 0
    try:
        with path.open(encoding="utf-8", errors="replace") as source:
            for line in source:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        records.append(value)
                except json.JSONDecodeError:
                    incomplete += 1
    except OSError:
        return [], 1
    return records, incomplete


def _source_project(store: Store, cwd) -> tuple[str | None, str | None]:
    if not isinstance(cwd, str):
        return None, None
    path = Path(cwd).expanduser()
    if not path.is_absolute() or not path.exists():
        return None, None
    try:
        return store.resolve_project(path), str(path.resolve())
    except IdentityConflict:
        return None, str(path.resolve())


def _usage(evidence_id: str, provider=None, model=None, timestamp=None, **values) -> dict:
    cost = values.pop("cost", None)
    recorded = isinstance(cost, dict) and any(
        name in cost for name in ("input", "output", "cacheRead", "cacheWrite", "total")
    )
    return {
        "evidence_id": evidence_id,
        "provider": provider,
        "model": model,
        "timestamp": timestamp,
        "input_tokens": values.get("input_tokens"),
        "output_tokens": values.get("output_tokens"),
        "cache_read_tokens": values.get("cache_read_tokens"),
        "cache_write_tokens": values.get("cache_write_tokens"),
        "total_tokens": values.get("total_tokens"),
        "cost_input": cost.get("input") if recorded else None,
        "cost_output": cost.get("output") if recorded else None,
        "cost_cache_read": cost.get("cacheRead") if recorded else None,
        "cost_cache_write": cost.get("cacheWrite") if recorded else None,
        "cost_total": cost.get("total") if recorded else None,
        "cost_provenance": "recorded" if recorded else "unavailable",
    }


def collect_codex(store: Store, paths=None) -> dict:
    """Collect locally verified Codex 0.153.4--0.157.1 JSONL records."""
    result = {"files": 0, "sessions": 0, "usage_changed": 0, "incomplete": 0, "unattributed": 0}
    for path in _jsonl_files(paths, Path.home() / ".codex/sessions"):
        rows, incomplete = _read_jsonl(path)
        result["files"] += 1
        result["incomplete"] += incomplete
        meta = next((r.get("payload") or {} for r in rows if r.get("type") == "session_meta"), None)
        if not meta or not (session_id := meta.get("session_id") or meta.get("id")):
            continue
        contexts = {}
        events = []
        for row in rows:
            if row.get("type") != "turn_context":
                continue
            payload = row.get("payload") or {}
            if payload.get("turn_id"):
                contexts[payload["turn_id"]] = payload
            if payload.get("model"):
                events.append({"evidence_id": f"turn:{payload.get('turn_id') or row.get('timestamp')}",
                               "provider": meta.get("model_provider"), "model": payload["model"],
                               "timestamp": row.get("timestamp")})
        usage = []
        for row in rows:
            if row.get("type") != "token_usage_record":
                continue
            payload = row.get("payload") or {}
            evidence = payload.get("response_id") or payload.get("turn_id")
            if not evidence:
                continue
            raw = payload.get("usage") or {}
            context = contexts.get(payload.get("turn_id"), {})
            usage.append(_usage(
                f"response:{evidence}", meta.get("model_provider"), context.get("model"),
                row.get("timestamp"), input_tokens=raw.get("input_tokens"),
                output_tokens=raw.get("output_tokens"),
                cache_read_tokens=raw.get("cached_input_tokens"),
                cache_write_tokens=raw.get("cache_write_input_tokens"),
                total_tokens=raw.get("total_tokens"),
            ))
        project, cwd = _source_project(store, meta.get("cwd"))
        result["unattributed"] += project is None
        result["usage_changed"] += store.save_session(
            "codex", str(session_id), project, cwd, meta.get("cli_version"), meta.get("timestamp"),
            max((r.get("timestamp") or "" for r in rows), default=None), usage, events,
        )
        result["sessions"] += 1
    return result


def _sum_known(*values):
    return sum(values) if all(isinstance(v, int) for v in values) else None


def collect_claude(store: Store, paths=None) -> dict:
    """Collect locally verified Claude Code 2.1.220--2.1.282 JSONL records."""
    result = {"files": 0, "sessions": 0, "usage_changed": 0, "incomplete": 0, "unattributed": 0}
    for path in _jsonl_files(paths, Path.home() / ".claude/projects"):
        rows, incomplete = _read_jsonl(path)
        result["files"] += 1
        result["incomplete"] += incomplete
        assistants = [r for r in rows if r.get("type") == "assistant"]
        session_id = next((r.get("sessionId") for r in assistants if r.get("sessionId")), None)
        if not session_id:
            continue
        # Streaming entries repeat message IDs; the last entry is the completed snapshot.
        messages = {}
        for row in assistants:
            message = row.get("message") or {}
            evidence = message.get("id") or row.get("requestId") or row.get("uuid")
            if evidence:
                messages[evidence] = row
        usage, events, previous_model = [], [], None
        for evidence, row in messages.items():
            message = row.get("message") or {}
            raw = message.get("usage") or {}
            model = message.get("model")
            usage.append(_usage(
                f"message:{evidence}", model=model, timestamp=row.get("timestamp"),
                input_tokens=raw.get("input_tokens"), output_tokens=raw.get("output_tokens"),
                cache_read_tokens=raw.get("cache_read_input_tokens"),
                cache_write_tokens=raw.get("cache_creation_input_tokens"),
                # Anthropic input_tokens excludes cache tokens, so the four parts add up.
                total_tokens=_sum_known(raw.get("input_tokens"), raw.get("output_tokens"),
                                        raw.get("cache_read_input_tokens"), raw.get("cache_creation_input_tokens")),
            ))
            if model and model != previous_model:
                events.append({"evidence_id": f"message:{evidence}", "model": model,
                               "timestamp": row.get("timestamp")})
                previous_model = model
        first = assistants[0]
        project, cwd = _source_project(store, first.get("cwd"))
        result["unattributed"] += project is None
        result["usage_changed"] += store.save_session(
            "claude", str(session_id), project, cwd, first.get("version"),
            min((r.get("timestamp") or "" for r in assistants), default=None),
            max((r.get("timestamp") or "" for r in assistants), default=None), usage, events,
        )
        result["sessions"] += 1
    return result


def collect_pi(store: Store, paths=None) -> dict:
    """Collect locally verified Pi session format version 3 JSONL records."""
    result = {"files": 0, "sessions": 0, "usage_changed": 0, "incomplete": 0, "unattributed": 0}
    for path in _jsonl_files(paths, Path.home() / ".pi/agent/sessions"):
        rows, incomplete = _read_jsonl(path)
        result["files"] += 1
        result["incomplete"] += incomplete
        header = next((r for r in rows if r.get("type") == "session"), None)
        if not header or not (session_id := header.get("id")):
            continue
        usage, events, active_provider, active_model = [], [], None, None
        for row in rows:
            if row.get("type") == "model_change":
                active_provider, active_model = row.get("provider"), row.get("modelId")
                if active_model and row.get("id"):
                    events.append({"evidence_id": f"change:{row['id']}", "provider": active_provider,
                                   "model": active_model, "timestamp": row.get("timestamp")})
            elif row.get("type") == "message" and (row.get("message") or {}).get("role") == "assistant":
                message = row["message"]
                active_provider = message.get("provider") or active_provider
                active_model = message.get("model") or active_model
                if not row.get("id"):
                    continue
                raw = message.get("usage") or {}
                usage.append(_usage(
                    f"message:{row['id']}", active_provider, active_model, row.get("timestamp"),
                    input_tokens=raw.get("input"), output_tokens=raw.get("output"),
                    cache_read_tokens=raw.get("cacheRead"), cache_write_tokens=raw.get("cacheWrite"),
                    total_tokens=raw.get("totalTokens"), cost=raw.get("cost"),
                ))
            elif row.get("type") == "compaction" and row.get("id"):
                # Pi records compaction as a separate paid model call, not an assistant duplicate.
                raw = row.get("usage") or {}
                usage.append(_usage(
                    f"compaction:{row['id']}", active_provider, active_model, row.get("timestamp"),
                    input_tokens=raw.get("input"), output_tokens=raw.get("output"),
                    cache_read_tokens=raw.get("cacheRead"), cache_write_tokens=raw.get("cacheWrite"),
                    total_tokens=raw.get("totalTokens"), cost=raw.get("cost"),
                ))
        project, cwd = _source_project(store, header.get("cwd"))
        result["unattributed"] += project is None
        result["usage_changed"] += store.save_session(
            "pi", str(session_id), project, cwd, str(header.get("version")) if header.get("version") is not None else None,
            header.get("timestamp"), max((r.get("timestamp") or "" for r in rows), default=None),
            usage, events,
        )
        result["sessions"] += 1
    return result


def collect_all(store: Store, sources: dict[str, list[str] | None] | None = None) -> dict:
    """Run verified collectors. A sources mapping limits collection to named sources."""
    collectors = {"codex": collect_codex, "claude": collect_claude, "pi": collect_pi}
    selected = collectors if sources is None else {name: collectors[name] for name in sources}
    return {name: collector(store, None if sources is None else sources[name])
            for name, collector in selected.items()}


def _period_bounds(period: str, now: datetime | None = None) -> tuple[str | None, str | None]:
    """Return UTC calendar boundaries; all-time has no bounds."""
    if period == "all-time":
        return None, None
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "monthly":
        start = start.replace(day=1)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    else:
        end = datetime.fromordinal(start.toordinal() + 1).replace(tzinfo=timezone.utc)
    return start.isoformat(), end.isoformat()


def _project_name(store: Store, project_id: str) -> str:
    rows = store.db.execute(
        "SELECT kind, value FROM aliases WHERE project_id=? ORDER BY kind='git-remote' DESC, kind, value",
        (project_id,),
    ).fetchall()
    for kind, value in rows:
        if kind == "git-remote":
            return value.rstrip("/").rsplit("/", 1)[-1]
    for kind, value in rows:
        if kind == "git-root":  # local repository without a remote
            return Path(value).name
    row = store.db.execute(
        "SELECT path FROM locations WHERE project_id=? ORDER BY last_seen DESC LIMIT 1", (project_id,)
    ).fetchone()
    if row:
        # No git remote: a bare folder name ("om", "tests") is ambiguous, so show the ~-relative path.
        home = str(Path.home())
        return "~" + row[0][len(home):] if row[0] == home or row[0].startswith(home + "/") else row[0]
    row = store.db.execute("SELECT identity_value FROM projects WHERE id=?", (project_id,)).fetchone()
    return row[0] if row else project_id


def _projects(store: Store) -> list[dict]:
    return [{"id": row[0], "name": _project_name(store, row[0])}
            for row in store.db.execute("SELECT id FROM projects ORDER BY created_at, id")]


def _select_project(store: Store, selector: str) -> dict:
    projects = _projects(store)
    exact_id = [project for project in projects if project["id"] == selector]
    if exact_id:
        return exact_id[0]
    matches = [project for project in projects
               if selector.casefold() in (project["name"].casefold(), Path(project["name"]).name.casefold())]
    if not matches:
        raise ValueError(f"unknown project: {selector}")
    if len(matches) != 1:
        ids = ", ".join(project["id"] for project in matches)
        raise ValueError(f"ambiguous project name {selector!r}; use an exact project ID ({ids})")
    return matches[0]


def _where_period(period: str, alias: str = "u") -> tuple[str, list[str]]:
    start, end = _period_bounds(period)
    if start is None:
        return "", []
    return f" AND datetime({alias}.timestamp) >= datetime(?) AND datetime({alias}.timestamp) < datetime(?)", [start, end]


def _totals(store: Store, project_id: str, period: str, group_models: bool = False) -> list[dict]:
    where, params = _where_period(period)
    groups = "u.agent, COALESCE(u.provider, ''), COALESCE(u.model, '(unknown)')" if group_models else "''"
    select_groups = ("u.agent AS agent, COALESCE(u.provider, '') AS provider, "
                     "COALESCE(u.model, '(unknown)') AS model, " if group_models else "")
    numeric = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens")
    aggregates = ", ".join(
        # Tokens: sum what the sources recorded (NULL only if none did); cost stays all-or-nothing.
        f"SUM(u.{column}) AS {column}"
        for column in numeric
    )
    sql = f"""SELECT {select_groups}COUNT(*) AS records, {aggregates},
                     CASE WHEN COUNT(*)=COUNT(u.cost_total) THEN SUM(u.cost_total) END AS cost_total
              FROM usage_records u
              JOIN sessions s ON s.agent=u.agent AND s.session_id=u.session_id
              WHERE s.project_id=?{where}
              GROUP BY {groups}
              ORDER BY {groups}"""
    return [dict(row) for row in store.db.execute(sql, (project_id, *params))]


def _context_cwd() -> Path:
    """Use direct caller cwd, or a cwd supplied by Herdr's verified invocation context."""
    if os.environ.get("HERDR_PLUGIN_ID"):
        try:
            context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
        except json.JSONDecodeError as error:
            raise ValueError("invalid HERDR_PLUGIN_CONTEXT_JSON") from error
        for key in ("focused_pane_cwd", "workspace_cwd"):
            value = context.get(key)
            if isinstance(value, str) and Path(value).is_absolute() and Path(value).exists():
                return Path(value).resolve()
        raise ValueError("Herdr invocation has no verified focused-pane or workspace cwd")
    return Path.cwd().resolve()


def _open_project_ids(store: Store) -> set[str] | None:
    """Projects of panes open in Herdr, or None outside Herdr."""
    herdr = os.environ.get("HERDR_BIN_PATH") or (os.environ.get("HERDR_ENV") and "herdr")
    if not herdr:
        return None
    try:
        out = subprocess.run([herdr, "pane", "list"], capture_output=True, text=True, timeout=5, check=True).stdout
        panes = json.loads(out)["result"]["panes"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None
    cwds = {pane.get("foreground_cwd") or pane.get("cwd") for pane in panes}
    return {store.resolve_project(cwd) for cwd in cwds if isinstance(cwd, str) and Path(cwd).is_dir()}


def _human(value) -> str:
    if value is None:
        return "?"
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if value >= limit:
            return f"{value / limit:.1f}{suffix}"
    return str(value)


def _usage_cells(row: dict) -> list:
    """TOTAL, INPUT, OUTPUT, CACHE, COST cells for one totals row (empty dict = no usage)."""
    if not row:
        return ["0", "0", "0", "0", "$0.00"]
    reads, writes = row.get("cache_read_tokens"), row.get("cache_write_tokens")
    cache = None if reads is None and writes is None else (reads or 0) + (writes or 0)
    cost = row.get("cost_total")
    return [_human(row.get("total_tokens")), _human(row.get("input_tokens")), _human(row.get("output_tokens")),
            _human(cache), "?" if cost is None else f"${cost:.2f}"]


def _print_pretty(labels: list[str], label_styles: list[str], rows: list[tuple]) -> None:
    """rows: (sort_total, label_cells, usage_row), colored on a terminal."""
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    paint = (lambda code, text: f"\033[{code}m{text}\033[0m") if color else (lambda _code, text: text)
    headers = [*labels, "TOTAL", "INPUT", "OUTPUT", "CACHE", "COST"]
    styles = [*label_styles, "1;33", "32", "35", "2", "1;32"]
    table = [[*cells, *_usage_cells(usage)] for _total, cells, usage in rows]
    widths = [max([len(h)] + [len(r[i]) for r in table]) for i, h in enumerate(headers)]
    left = len(labels)
    fit = lambda i, v, w: v.ljust(w) if i < left else v.rjust(w)
    print(paint("1", "  ".join(fit(i, h, w) for i, (h, w) in enumerate(zip(headers, widths)))))
    for r in table:
        print("  ".join(paint(st, fit(i, v, w)) for i, (v, w, st) in enumerate(zip(r, widths, styles))))


def _visible_projects(store: Store, show_all: bool) -> tuple[list[dict], bool]:
    open_ids = None if show_all else _open_project_ids(store)
    projects = [p for p in _projects(store) if open_ids is None or p["id"] in open_ids]
    if show_all:  # the full list is for git projects; plain folders only show while open in Herdr
        git_ids = {row[0] for row in store.db.execute(
            "SELECT project_id FROM aliases WHERE kind IN ('git-remote', 'git-root', 'git-common-dir')")}
        projects = [p for p in projects if p["id"] in git_ids]
    return projects, open_ids is not None


def _footer(filtered: bool, empty: bool) -> None:
    dim = (lambda t: f"\033[2m{t}\033[0m") if sys.stdout.isatty() and not os.environ.get("NO_COLOR") else str
    if empty:
        print(dim("No usage for open projects yet." if filtered else "No usage recorded yet."))
    elif filtered:
        print(dim("\nOpen Herdr projects only; use --all-projects for every project."))


def _print_projects(store: Store, period: str, show_all: bool = False) -> None:
    projects, filtered = _visible_projects(store, show_all)
    rows = []
    for project in projects:
        totals = _totals(store, project["id"], period)
        usage = totals[0] if totals else {}
        rows.append((usage.get("total_tokens", 0), [project["name"]], usage))
    rows.sort(key=lambda item: -(item[0] or 0))
    _print_pretty(["PROJECT"], ["1;36"], rows)
    _footer(filtered, not rows)


_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens")


def _real_usage(row: dict) -> bool:
    """Skip Claude Code <synthetic> placeholders and rows whose source recorded no tokens."""
    return row["model"] != "<synthetic>" and any(row[key] for key in _TOKEN_KEYS)


def _print_models(store: Store, period: str, show_all: bool = False) -> None:
    projects, filtered = _visible_projects(store, show_all)
    rows = [(row["total_tokens"], [project["name"], row["agent"], row["model"] or "?"], row)
            for project in projects for row in _totals(store, project["id"], period, True)
            if _real_usage(row)]
    rows.sort(key=lambda item: (item[1][0], -(item[0] or 0)))
    _print_pretty(["PROJECT", "AGENT", "MODEL"], ["1;36", "34", "37"], rows)
    _footer(filtered, not rows)


def _print_project(store: Store, project: dict, period: str) -> None:
    totals = _totals(store, project["id"], period)
    usage = totals[0] if totals else {}
    _print_pretty(["PROJECT"], ["1;36"], [(0, [project["name"]], usage)])
    models = [(row["total_tokens"], [row["agent"], row["model"] or "?"], row)
              for row in _totals(store, project["id"], period, True) if _real_usage(row)]
    if models:
        models.sort(key=lambda item: -(item[0] or 0))
        print()
        _print_pretty(["AGENT", "MODEL"], ["34", "37"], models)


def _add_period(parser: argparse.ArgumentParser) -> None:
    periods = parser.add_mutually_exclusive_group()
    periods.add_argument("--daily", dest="period", action="store_const", const="daily")
    periods.add_argument("--monthly", dest="period", action="store_const", const="monthly")
    periods.add_argument("--all-time", dest="period", action="store_const", const="all-time")
    parser.set_defaults(period="all-time")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persistent local AI usage by project")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("projects", "current", "models"):
        _add_period(commands.add_parser(name))
    for name in ("projects", "models"):
        commands.choices[name].add_argument("--all-projects", action="store_true")
    project = commands.add_parser("project")
    project.add_argument("name")
    _add_period(project)
    collect = commands.add_parser("collect")
    for source in ("codex", "claude", "pi"):
        collect.add_argument(f"--{source}", action="append", metavar="PATH")
    commands.add_parser("event")
    commands.add_parser("startup")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with Store() as store:
            if args.command == "collect":
                supplied = {name: getattr(args, name) for name in ("codex", "claude", "pi")
                            if getattr(args, name) is not None}
                print(json.dumps(collect_all(store, supplied or None), sort_keys=True))
            elif args.command in ("event", "startup"):
                if args.command == "event":
                    try:
                        event = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON", "{}"))
                    except json.JSONDecodeError:
                        event = {}
                    data = event.get("data") if isinstance(event, dict) else None
                    status = (data if isinstance(data, dict) else {}).get("agent_status")
                    if status not in ("idle", "done", "blocked"):
                        return 0
                print(json.dumps(collect_all(store), sort_keys=True))
            elif args.command == "projects":
                _print_projects(store, args.period, args.all_projects)
            elif args.command == "models":
                _print_models(store, args.period, args.all_projects)
            else:
                project = (_select_project(store, args.name) if args.command == "project"
                           else {"id": store.resolve_project(_context_cwd())})
                project.setdefault("name", _project_name(store, project["id"]))
                _print_project(store, project, args.period)
    except (IdentityConflict, ValueError) as error:
        print(f"burnlog: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        # Herdr closes popup terminals as soon as their command exits.
        if os.environ.get("HERDR_PLUGIN_ENTRYPOINT_ID") and sys.stdin.isatty():
            try:
                input("\nPress Enter to close.")
            except (EOFError, KeyboardInterrupt):
                pass
