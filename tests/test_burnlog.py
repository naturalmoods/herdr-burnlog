import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from burnlog import (
    IdentityConflict,
    RecordConflict,
    Store,
    collect_claude,
    collect_codex,
    collect_pi,
    _context_cwd,
    _period_bounds,
    _select_project,
    default_database_path,
    normalize_remote,
)


def git(path, *args):
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def init_repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "BurnLog Test")
    (path / "file.txt").write_text("test\n", encoding="utf-8")
    git(path, "add", "file.txt")
    git(path, "commit", "-qm", "initial")


class BurnLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "state" / "burnlog.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, name):
        source = Path(__file__).parent / "fixtures" / name
        target = self.root / name
        target.write_text(
            source.read_text(encoding="utf-8").replace("/workspace/example", str(self.repo)),
            encoding="utf-8",
        )
        return target

    def test_reclone_and_restart_keep_identity_and_history(self):
        source = self.root / "source"
        origin = self.root / "origin.git"
        first = self.root / "first"
        second = self.root / "second"
        init_repo(source)
        subprocess.run(["git", "clone", "-q", "--bare", str(source), str(origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(first)], check=True)

        with Store(self.database) as store:
            project = store.resolve_project(first)
            self.assertTrue(store.add_record(project, "test", "record-1", {"tokens": 3}))

        subprocess.run(["git", "clone", "-q", str(origin), str(second)], check=True)
        with Store(self.database) as store:
            self.assertEqual(project, store.resolve_project(second))
            self.assertEqual([{"source": "test", "external_id": "record-1", "payload": {"tokens": 3}}], store.records(project))

    def test_worktrees_share_project(self):
        main = self.root / "main"
        worktree = self.root / "worktree"
        init_repo(main)
        git(main, "worktree", "add", "-qb", "other", str(worktree))
        with Store(self.database) as store:
            self.assertEqual(store.resolve_project(main), store.resolve_project(worktree))

    def test_unrelated_same_basename_stay_separate(self):
        first = self.root / "one" / "project"
        second = self.root / "two" / "project"
        init_repo(first)
        init_repo(second)
        with Store(self.database) as store:
            self.assertNotEqual(store.resolve_project(first), store.resolve_project(second))

    def test_duplicate_collection_is_idempotent(self):
        repo = self.root / "repo"
        init_repo(repo)
        with Store(self.database) as store:
            project = store.resolve_project(repo)
            self.assertTrue(store.add_record(project, "pi", "message-1", {"input": 2}))
            self.assertFalse(store.add_record(project, "pi", "message-1", {"input": 2}))
            with self.assertRaises(RecordConflict):
                store.add_record(project, "pi", "message-1", {"input": 4})
            self.assertEqual(1, len(store.records(project)))

    def test_reconciliation_moves_provisional_history(self):
        provisional_repo = self.root / "provisional"
        authoritative_repo = self.root / "authoritative"
        init_repo(provisional_repo)
        init_repo(authoritative_repo)
        with Store(self.database) as store:
            provisional = store.resolve_project(provisional_repo)
            store.add_record(provisional, "codex", "turn-1", {"output": 5})
            store.save_session("codex", "session-1", provisional, str(provisional_repo), None,
                               None, None, [], [])
            authoritative = store.resolve_project(authoritative_repo, github_id=12345)
            self.assertNotEqual(provisional, authoritative)
            self.assertEqual(authoritative, store.reconcile(provisional, 12345))
            self.assertEqual(1, len(store.records(authoritative)))
            self.assertEqual(authoritative, store.sessions()[0]["project_id"])
            self.assertEqual(authoritative, store.resolve_project(provisional_repo, github_id=12345))

    def test_conflicting_authoritative_ids_never_merge(self):
        repo = self.root / "repo"
        init_repo(repo)
        with Store(self.database) as store:
            project = store.resolve_project(repo, github_id=111)
            with self.assertRaises(IdentityConflict):
                store.reconcile(project, 222)
            with self.assertRaises(IdentityConflict):
                store.resolve_project(repo, github_id=222)

    def test_remote_normalization(self):
        for remote in ('https://github.com/Owner/Repo.git',
                       'ssh://git@github.com/owner/repo.git',
                       'git@github.com:owner/repo.git'):
            self.assertEqual('github.com/owner/repo', normalize_remote(remote))
        local = self.root / 'repo.git'
        self.assertEqual(normalize_remote(str(local)), normalize_remote(local.as_uri()))
        self.assertNotEqual(normalize_remote(str(local)), normalize_remote(str(self.root / 'repo')))

    def test_fork_with_known_upstream_stays_separate(self):
        upstream, fork = self.root / 'upstream', self.root / 'fork'
        init_repo(upstream)
        init_repo(fork)
        git(upstream, 'remote', 'add', 'origin', 'https://github.com/org/repo.git')
        git(fork, 'remote', 'add', 'origin', 'git@github.com:fork/repo.git')
        git(fork, 'remote', 'add', 'upstream', 'https://github.com/org/repo.git')
        with Store(self.database) as store:
            self.assertNotEqual(store.resolve_project(upstream), store.resolve_project(fork))

    def test_changed_remote_does_not_reuse_history(self):
        repo = self.root / 'repo'
        init_repo(repo)
        git(repo, 'remote', 'add', 'origin', 'https://github.com/one/repo.git')
        with Store(self.database) as store:
            project = store.resolve_project(repo)
            store.add_record(project, 'test', '1', {'input': 3})
            git(repo, 'remote', 'set-url', 'origin', 'https://github.com/two/repo.git')
            with self.assertRaises(IdentityConflict):
                store.resolve_project(repo)
            self.assertEqual(1, len(store.records(project)))

    def test_collectors_are_idempotent_across_rescan_and_restart(self):
        self.repo = self.root / "repo"
        init_repo(self.repo)
        codex = self.fixture("codex.jsonl")
        claude = self.fixture("claude.jsonl")
        pi = self.fixture("pi.jsonl")

        with Store(self.database) as store:
            self.assertEqual(2, collect_codex(store, codex)["usage_changed"])
            self.assertEqual(2, collect_claude(store, claude)["usage_changed"])
            self.assertEqual(4, collect_pi(store, pi)["usage_changed"])
            self.assertEqual(0, collect_codex(store, codex)["usage_changed"])
            self.assertEqual(0, collect_claude(store, claude)["usage_changed"])
            self.assertEqual(0, collect_pi(store, pi)["usage_changed"])
            self.assertEqual(3, len(store.sessions()))

        with Store(self.database) as store:
            codex_usage = store.usage("codex", "codex-session-example")
            self.assertEqual(2, len(codex_usage))  # token_count mirrors are deliberately ignored
            self.assertEqual((100, 60, 10, 120), (
                codex_usage[0]["input_tokens"], codex_usage[0]["cache_read_tokens"],
                codex_usage[0]["cache_write_tokens"], codex_usage[0]["total_tokens"],
            ))
            self.assertEqual("unavailable", codex_usage[0]["cost_provenance"])
            self.assertEqual(2, len(store.models("codex", "codex-session-example")))

            claude_usage = store.usage("claude", "claude-session-example")
            self.assertEqual(2, len(claude_usage))
            self.assertEqual(7, claude_usage[0]["output_tokens"])
            self.assertEqual(3 + 7 + 4 + 5, claude_usage[0]["total_tokens"])
            self.assertEqual(2, len(store.models("claude", "claude-session-example")))

            pi_usage = store.usage("pi", "pi-session-example")
            self.assertEqual(4, len(pi_usage))
            self.assertEqual("recorded", pi_usage[0]["cost_provenance"])
            unknown = next(row for row in pi_usage if row["evidence_id"] == "compaction:compact-example-unknown")
            self.assertIsNone(unknown["input_tokens"])
            self.assertIsNone(unknown["cost_total"])
            self.assertEqual("unavailable", unknown["cost_provenance"])
            self.assertEqual(["pi-model-a", "pi-model-b"], [
                row["model"] for row in store.models("pi", "pi-session-example")
            ])

    def test_claude_stream_growth_updates_one_message(self):
        self.repo = self.root / "repo"
        init_repo(self.repo)
        lines = (Path(__file__).parent / "fixtures" / "claude.jsonl").read_text().splitlines()
        path = self.root / "growing.jsonl"
        path.write_text(lines[0].replace("/workspace/example", str(self.repo)) + "\n")
        with Store(self.database) as store:
            self.assertEqual(1, collect_claude(store, path)["usage_changed"])
            self.assertEqual(2, store.usage("claude", "claude-session-example")[0]["output_tokens"])
            with path.open("a", encoding="utf-8") as target:
                target.write(lines[1].replace("/workspace/example", str(self.repo)) + "\n")
            self.assertEqual(1, collect_claude(store, path)["usage_changed"])
            usage = store.usage("claude", "claude-session-example")
            self.assertEqual(1, len(usage))
            self.assertEqual(7, usage[0]["output_tokens"])

    def test_partial_final_record_is_collected_after_completion(self):
        self.repo = self.root / "repo"
        init_repo(self.repo)
        path = self.root / "partial.jsonl"
        header = {"type": "session", "version": 3, "id": "partial-session",
                  "timestamp": "2026-01-04T00:00:00Z", "cwd": str(self.repo)}
        message = {"type": "message", "id": "partial-message", "timestamp": "2026-01-04T00:00:01Z",
                   "message": {"role": "assistant", "provider": "example", "model": "example-model",
                               "usage": {"input": 1, "output": 2, "cacheRead": 3, "cacheWrite": 4,
                                         "totalTokens": 10}}}
        import json
        encoded = json.dumps(message)
        path.write_text(json.dumps(header) + "\n" + encoded[:20], encoding="utf-8")
        with Store(self.database) as store:
            first = collect_pi(store, path)
            self.assertEqual(1, first["incomplete"])
            self.assertEqual([], store.usage("pi", "partial-session"))
            with path.open("a", encoding="utf-8") as target:
                target.write(encoded[20:] + "\n")
            self.assertEqual(1, collect_pi(store, path)["usage_changed"])
            self.assertEqual(1, len(store.usage("pi", "partial-session")))

    def test_untrusted_source_cwd_stays_unattributed(self):
        path = self.root / "unknown.jsonl"
        fixture = (Path(__file__).parent / "fixtures" / "codex.jsonl").read_text()
        path.write_text(fixture.replace("/workspace/example", "relative/project"), encoding="utf-8")
        with Store(self.database) as store:
            result = collect_codex(store, path)
            self.assertEqual(1, result["unattributed"])
            self.assertIsNone(store.sessions()[0]["project_id"])

    def test_cli_persists_and_reports_projects_models_and_plugin_current(self):
        self.repo = self.root / "repo"
        init_repo(self.repo)
        script = Path(__file__).parents[1] / "burnlog.py"
        env = os.environ | {"HERDR_PLUGIN_STATE_DIR": str(self.root / "plugin-state")}
        collect = subprocess.run(
            ["python3", str(script), "collect", "--codex", str(self.fixture("codex.jsonl")),
             "--claude", str(self.fixture("claude.jsonl")), "--pi", str(self.fixture("pi.jsonl"))],
            env=env, text=True, capture_output=True, check=True,
        )
        self.assertEqual(8, sum(item["usage_changed"] for item in json.loads(collect.stdout).values()))

        projects = subprocess.run(
            ["python3", str(script), "projects", "--all-time", "--all-projects"], env=env,
            text=True, capture_output=True, check=True,
        ).stdout
        models = subprocess.run(
            ["python3", str(script), "models", "--all-time", "--all-projects"], env=env,
            text=True, capture_output=True, check=True,
        ).stdout
        plugin_env = env | {
            "HERDR_PLUGIN_ID": "herdr-burnlog",
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"focused_pane_cwd": str(self.repo)}),
        }
        current = subprocess.run(
            ["python3", str(script), "current", "--all-time"], cwd=script.parent, env=plugin_env,
            text=True, capture_output=True, check=True,
        ).stdout
        self.assertIn("repo", projects)
        with Store(self.root / "plugin-state" / "burnlog.sqlite3") as store:
            project_id = store.sessions()[0]["project_id"]
        self.assertNotIn(project_id, projects)
        selected = subprocess.check_output(
            ["python3", str(script), "project", project_id], env=env, text=True)
        self.assertIn("pi-model-b", selected)
        self.assertIn("codex-model-a", models)
        self.assertIn("pi-model-b", models)
        self.assertIn("AGENT   MODEL", current)
        self.assertIn("codex-model-a", current)
        self.assertIn("?", current)  # unavailable mixed-source total/cost stays visible
        self.assertIn("120", current)  # Codex total does not add its overlapping cached input

    def test_project_selector_rejects_ambiguous_names(self):
        first = self.root / "one" / "same"
        second = self.root / "two" / "same"
        init_repo(first)
        init_repo(second)
        with Store(self.database) as store:
            store.resolve_project(first)
            store.resolve_project(second)
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                _select_project(store, "same")

    def test_period_boundaries_are_utc_calendar_boundaries(self):
        now = datetime(2026, 12, 31, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(
            ("2026-12-31T00:00:00+00:00", "2027-01-01T00:00:00+00:00"),
            _period_bounds("daily", now),
        )
        self.assertEqual(
            ("2026-12-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"),
            _period_bounds("monthly", now),
        )

    def test_event_ignores_working_agent(self):
        script = Path(__file__).parents[1] / "burnlog.py"
        env = os.environ | {
            "HERDR_PLUGIN_STATE_DIR": str(self.root / "event-state"),
            "HERDR_PLUGIN_EVENT_JSON": json.dumps({"data": {"agent_status": "working"}}),
        }
        result = subprocess.run(["python3", str(script), "event"], env=env,
                                text=True, capture_output=True, check=True)
        self.assertEqual("", result.stdout)
        # Herdr nests the status under "data"; a settled agent triggers collection.
        env |= {"HOME": str(self.root), "HERDR_PLUGIN_EVENT_JSON": json.dumps({"data": {"agent_status": "idle"}})}
        result = subprocess.run(["python3", str(script), "event"], env=env,
                                text=True, capture_output=True, check=True)
        self.assertIn("claude", json.loads(result.stdout))

    def test_plugin_current_requires_verified_context_cwd(self):
        old = {name: os.environ.get(name) for name in ("HERDR_PLUGIN_ID", "HERDR_PLUGIN_CONTEXT_JSON")}
        try:
            os.environ["HERDR_PLUGIN_ID"] = "herdr-burnlog"
            os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = "{}"
            with self.assertRaisesRegex(ValueError, "no verified"):
                _context_cwd()
            os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps({"workspace_cwd": str(self.root)})
            self.assertEqual(self.root.resolve(), _context_cwd())
        finally:
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_state_path_prefers_herdr_then_xdg(self):
        old = {name: os.environ.get(name) for name in ("HERDR_PLUGIN_STATE_DIR", "XDG_STATE_HOME")}
        try:
            os.environ["HERDR_PLUGIN_STATE_DIR"] = str(self.root / "herdr")
            self.assertEqual(self.root / "herdr" / "burnlog.sqlite3", default_database_path())
            del os.environ["HERDR_PLUGIN_STATE_DIR"]
            os.environ["XDG_STATE_HOME"] = str(self.root / "xdg")
            self.assertEqual(self.root / "xdg" / "herdr-burnlog" / "burnlog.sqlite3", default_database_path())
        finally:
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
