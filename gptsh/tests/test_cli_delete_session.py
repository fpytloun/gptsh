import json
import os
from pathlib import Path

from click.testing import CliRunner

import gptsh.cli.entrypoint as ep
from gptsh.cli.entrypoint import main


def _write_session(dirpath: Path, dtprefix: str, sid: str, mtime: int):
    p = dirpath / f"{dtprefix}-{sid}.json"
    doc = {"id": sid, "created_at": "2025-01-01T00:00:00Z", "agent": {"name": "dev", "model": "m"}}
    p.write_text(json.dumps(doc), encoding="utf-8")
    os.utime(p, (mtime, mtime))
    return p


def test_delete_session_by_id_and_index(monkeypatch, tmp_path):
    # Minimal config to allow CLI run
    monkeypatch.setattr(
        ep,
        "load_config",
        lambda paths=None: {"agents": {"default": {}}, "default_agent": "default"},
    )

    sessions_dir = tmp_path / "gptsh" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Create three sessions with increasing mtimes (id02 is newest)
    _write_session(sessions_dir, "20250101-000000", "id01", 1)
    _write_session(sessions_dir, "20250101-000001", "id02", 2)
    _write_session(sessions_dir, "20250101-000002", "id03", 3)

    runner = CliRunner()

    # Delete by id
    res = runner.invoke(main, ["--delete-session", "id02"])  # delete middle/newer
    assert res.exit_code == 0
    assert "Deleted session id02" in res.output
    assert not (sessions_dir / "20250101-000001-id02.json").exists()

    # Delete by index: 0 -> newest remaining (id03)
    res = runner.invoke(main, ["--delete-session", "0"])  # deletes id03
    assert res.exit_code == 0
    assert "Deleted session id03" in res.output
    assert not (sessions_dir / "20250101-000002-id03.json").exists()

    # Delete missing
    res = runner.invoke(main, ["--delete-session", "nope"])  # not found
    assert res.exit_code == 2
    assert "Session not found" in res.output
