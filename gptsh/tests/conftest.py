import os
import pytest


@pytest.fixture(autouse=True)
def _isolate_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    yield
