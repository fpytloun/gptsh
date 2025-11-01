import os
from pathlib import Path

from click.testing import CliRunner

import gptsh.cli.entrypoint as ep
from gptsh.cli.entrypoint import main


def _touch_json(dirpath: Path, name: str, mtime: int):
    p = dirpath / name
    p.write_text("{}", encoding="utf-8")
    os.utime(p, (mtime, mtime))
    return p


def test_cleanup_sessions_default_keep_10(monkeypatch, tmp_path):
    # Configure sessions dir to tmp
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    # Minimal config
    monkeypatch.setattr(
        ep,
        "load_config",
        lambda paths=None: {"agents": {"default": {}}, "default_agent": "default"},
    )

    sessions_dir = tmp_path / "gptsh" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create 15 files with increasing mtime
    for i in range(15):
        _touch_json(sessions_dir, f"20250101-0000{i:02d}-id{i:02d}.json", 1 + i)

    runner = CliRunner()
    res = runner.invoke(main, ["--cleanup-sessions"])  # default keep=10
    assert res.exit_code == 0
    # Count remaining files
    files = list(sessions_dir.glob("*.json"))
    assert len(files) == 10
    assert "removed 5" in res.output.lower()


def test_cleanup_sessions_keep_param(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        ep,
        "load_config",
        lambda paths=None: {"agents": {"default": {}}, "default_agent": "default"},
    )
    sessions_dir = tmp_path / "gptsh" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    for i in range(7):
        _touch_json(sessions_dir, f"20250101-0000{i:02d}-id{i:02d}.json", 1 + i)

    runner = CliRunner()
    res = runner.invoke(main, ["--cleanup-sessions", "--keep-sessions", "3"])
    assert res.exit_code == 0
    files = list(sessions_dir.glob("*.json"))
    assert len(files) == 3
    assert "removed 4" in res.output.lower()


def test_cleanup_sessions_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        ep,
        "load_config",
        lambda paths=None: {"agents": {"default": {}}, "default_agent": "default"},
    )
    sessions_dir = tmp_path / "gptsh" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    for i in range(3):
        _touch_json(sessions_dir, f"20250101-0000{i:02d}-id{i:02d}.json", 1 + i)

    runner = CliRunner()
    res = runner.invoke(main, ["--cleanup-sessions", "--keep-sessions", "0"])
    assert res.exit_code == 0
    files = list(sessions_dir.glob("*.json"))
    assert len(files) == 0
    assert "removed 3" in res.output.lower()
