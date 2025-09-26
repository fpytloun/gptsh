from gptsh.core.stdin_handler import read_stdin

def test_read_stdin_handles_truncation(monkeypatch):
    class DummyBuffer:
        def __init__(self, data):
            self.data = data
            self.read_called = 0
        def read(self, n):
            self.read_called += 1
            return self.data
    data = b'a' * (10)
    class DummyStdin:
        isatty = lambda self: False
        buffer = DummyBuffer(data)
    monkeypatch.setattr('sys.stdin', DummyStdin())
    result = read_stdin(max_bytes=5)
    assert result.startswith('aaaaa')
    assert '[...STDIN truncated' in result
