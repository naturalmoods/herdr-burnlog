"""Isolated Herdr check: python3 tests/check_herdr.py [GitHub-owner/repo]."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

repo = Path(__file__).resolve().parents[1]
source = sys.argv[1] if len(sys.argv) > 1 else None
with tempfile.TemporaryDirectory(prefix="burnlog-herdr-") as directory:
    root = Path(directory)
    home = root / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(root / "config"),
               XDG_STATE_HOME=str(root / "state"), HERDR_CONFIG_PATH=str(root / "herdr.toml"))
    session = f"burnlog-check-{os.getpid()}"
    def herdr(*args, check=True):
        return subprocess.run(["herdr", "--session", session, *args], env=env,
                              text=True, capture_output=True, check=check, timeout=20)
    server = subprocess.Popen(["herdr", "--session", session, "server"], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if herdr("status", "server", "--json", check=False).returncode == 0:
                break
            time.sleep(.1)
        else:
            raise AssertionError("isolated server did not start")
        herdr("workspace", "create", "--cwd", str(repo), "--no-focus")
        state = root / "state" / "herdr" / "plugins" / "herdr-burnlog"
        assert not state.exists(), "test did not start with fresh plugin state"
        if source:
            herdr("plugin", "install", source, "--yes")
            manifests = list(root.rglob("herdr-plugin.toml"))
            assert len(manifests) == 1, manifests
            installed = manifests[0].parent
        else:
            herdr("plugin", "link", str(repo), "--enabled")
            installed = repo
        assert state.is_dir(), "unexpected Herdr state path"
        fixture = root / "pi.jsonl"
        fixture.write_text((repo / "tests/fixtures/pi.jsonl").read_text().replace(
            "/workspace/example", str(repo)))
        cli_env = env | {"HERDR_PLUGIN_STATE_DIR": str(state)}
        def cli(*args):
            return subprocess.check_output(["python3", str(installed / "burnlog.py"), *args],
                                           env=cli_env, text=True)
        assert json.loads(cli("collect", "--pi", str(fixture)))["pi"]["usage_changed"] == 4
        assert json.loads(cli("collect", "--pi", str(fixture)))["pi"]["usage_changed"] == 0
        assert "pi-model-a" in cli("models")
        assert "herdr-burnlog" in cli("projects")
        herdr("plugin", "pane", "open", "--plugin", "herdr-burnlog",
              "--entrypoint", "projects", "--no-focus")
        if source:
            herdr("plugin", "uninstall", "herdr-burnlog")
            assert not installed.exists(), "managed source not removed"
            assert (state / "burnlog.sqlite3").is_file(), "uninstall lost history"
            herdr("plugin", "install", source, "--yes")
        else:
            herdr("plugin", "unlink", "herdr-burnlog")
            assert "pi-model-a" in cli("models"), "unlink lost history"
            herdr("plugin", "link", str(repo), "--enabled")
        assert "pi-model-a" in cli("models"), "relink lost history"
        herdr("plugin", "uninstall", "herdr-burnlog")
        assert (state / "burnlog.sqlite3").is_file(), "uninstall deleted plugin state"
        installed = repo
        assert "pi-model-a" in cli("models"), "uninstall lost history"
        assert "herdr-burnlog" not in herdr("plugin", "list").stdout
        herdr("plugin", "link", str(repo), "--enabled")
        assert "pi-model-a" in cli("models"), "post-uninstall relink lost history"
        herdr("plugin", "unlink", "herdr-burnlog")
        print(f"PASS: isolated {source or 'local link'}, collect/rescan, reports, pane, reinstall, uninstall, persistent state")
    finally:
        herdr("server", "stop", check=False)
        server.wait(timeout=15)
