from click.testing import CliRunner

import gptsh.cli.entrypoint as ep
from gptsh.cli.entrypoint import main


def test_list_sessions_filters(monkeypatch):
    fake = [
        {
            "id": "s1",
            "filename": "f1",
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-02T00:00:00Z",
            "title": "A",
            "agent": "dev",
            "model": "m1",
            "provider": "openai",
        },
        {
            "id": "s2",
            "filename": "f2",
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-03T00:00:00Z",
            "title": "B",
            "agent": "default",
            "model": "m2",
            "provider": "azure",
        },
    ]

    monkeypatch.setattr(
        ep,
        "load_config",
        lambda paths=None: {"agents": {"default": {}}, "default_agent": "default"},
    )
    monkeypatch.setattr(ep, "_list_saved_sessions", lambda: fake)

    runner = CliRunner()

    # Filter by agent
    res = runner.invoke(main, ["--list-sessions", "--agent", "dev"])
    assert res.exit_code == 0
    assert "(dev|m1)" in res.output
    assert "(default|m2)" not in res.output

    # Filter by provider
    res = runner.invoke(main, ["--list-sessions", "--provider", "azure"])
    assert res.exit_code == 0
    assert "(default|m2)" in res.output
    assert "(dev|m1)" not in res.output

    # Filter by model
    res = runner.invoke(main, ["--list-sessions", "--model", "m1"])
    assert res.exit_code == 0
    assert "(dev|m1)" in res.output
    assert "(default|m2)" not in res.output
