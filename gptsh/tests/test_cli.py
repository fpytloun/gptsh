import sys
import pytest

# Skip CLI tests if 'click' is not available in this environment
try:
    from click.testing import CliRunner
except Exception:
    pytest.skip("click not installed; skipping CLI tests", allow_module_level=True)

# Skip CLI tests if 'litellm' is not available
try:
    import litellm
except Exception:
    pytest.skip("litellm not installed; skipping CLI tests", allow_module_level=True)

from gptsh.cli.entrypoint import main

# Dummy completion for testing
class DummyCompletion:
    async def __call__(self, **kwargs):
        return {"choices": [{"message": {"content": "dummy response"}}]}

    async def stream(self, **kwargs):
        # Simulate streaming chunks
        for c in "dummy response":
            yield type("Chunk", (), {"text": c})

@pytest.fixture(autouse=True)
def patch_litellm(monkeypatch):
    dummy = DummyCompletion()
    # Replace completion with dummy callable object
    monkeypatch.setattr(litellm, 'completion', dummy)

def test_list_tools():
    runner = CliRunner()
    result = runner.invoke(main, ['--list-tools'])
    assert result.exit_code == 0
    assert 'filesystem:' in result.output
    assert 'tavily:' in result.output

def test_single_shot_completion():
    runner = CliRunner()
    result = runner.invoke(main, ['hello'], input=None)
    assert result.exit_code == 0
    assert 'dummy response' in result.output

def test_streaming_completion():
    runner = CliRunner()
    # --no-stream for single-shot override
    result = runner.invoke(main, ['--no-stream', 'hello'], input=None)
    assert result.exit_code == 0
    assert 'dummy response' in result.output

def test_stdin_input(monkeypatch):
    runner = CliRunner()
    # Provide input via stdin and no prompt arg
    input_text = 'stdin prompt'
    result = runner.invoke(main, [], input=input_text)
    assert result.exit_code == 0
    assert 'dummy response' in result.output
