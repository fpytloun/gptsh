
from gptsh.core.session import ChatSession


def test_normalize_coerces_none_content():
    messages = [
        {"role": "system", "content": None},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None},
    ]
    out = ChatSession._normalize_messages(messages)
    assert out[0]["content"] == ""
    assert out[1]["content"] == "hi"
    assert out[2]["content"] == ""


def test_normalize_drops_incomplete_tool_calls():
    # assistant with tool_calls but missing following tool message
    messages = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "fs__mkd", "arguments": "{}"}},
            ],
        },
        {"role": "assistant", "content": "I tried"},
    ]
    out = ChatSession._normalize_messages(messages)
    # The assistant tool_calls message should be dropped to avoid invalid sequence
    roles = [m["role"] for m in out]
    assert roles == ["user", "assistant"]
    assert "tool_calls" not in out[1]


def test_normalize_keeps_complete_tool_calls():
    messages = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "fs__mkd", "arguments": "{}"}},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "fs__mkd",
            "content": "ok",
        },
        {"role": "assistant", "content": "done"},
    ]
    out = ChatSession._normalize_messages(messages)
    # The assistant tool_calls message remains because a matching tool message follows
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in out)
    # And the tool message is still present
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "call_1" for m in out)
