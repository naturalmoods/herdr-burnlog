"""Minimal release manifest check: python3 tests/check_manifest.py."""
import tomllib
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = tomllib.loads((root / "herdr-plugin.toml").read_text(encoding="utf-8"))
assert manifest["id"] == "herdr-burnlog"
assert manifest["version"] == "0.4.1"
assert manifest["min_herdr_version"] == "0.9.1"
assert set(manifest["platforms"]) == {"linux", "macos"}
assert {pane["id"] for pane in manifest["panes"]} == {"current", "projects", "models"}
for entry in manifest["startup"] + manifest["events"] + manifest["panes"]:
    command = entry["command"]
    assert command and isinstance(command, list)
    if command[0] == "python3":
        assert (root / command[1]).is_file(), command[1]
print("PASS: herdr-plugin.toml release metadata and command files")
