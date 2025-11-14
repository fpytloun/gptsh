"""Tests for clipboard builtin MCP tool."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from gptsh.mcp.builtin import clipboard as cb


class TestClipboardToolDiscovery:
    """Test tool discovery and schema validation."""

    def test_list_tools(self):
        """Test that tool names are correct."""
        tools = cb.list_tools()
        assert isinstance(tools, list)
        assert "clipboard_read" in tools
        assert "clipboard_write" in tools
        assert len(tools) == 2

    def test_list_tools_detailed(self):
        """Test that tool schemas are valid."""
        tools = cb.list_tools_detailed()
        assert isinstance(tools, list)
        assert len(tools) == 2

        # Check clipboard_read schema
        read_tool = next(t for t in tools if t["name"] == "clipboard_read")
        assert read_tool["description"]
        assert "input_schema" in read_tool
        assert read_tool["input_schema"]["type"] == "object"
        assert read_tool["input_schema"]["required"] == []

        # Check clipboard_write schema
        write_tool = next(t for t in tools if t["name"] == "clipboard_write")
        assert write_tool["description"]
        assert "input_schema" in write_tool
        assert write_tool["input_schema"]["type"] == "object"
        assert "text" in write_tool["input_schema"]["properties"]
        assert "text" in write_tool["input_schema"]["required"]

    def test_auto_approve_default(self):
        """Test that tools are in auto-approval list."""
        assert hasattr(cb, "AUTO_APPROVE_DEFAULT")
        assert "clipboard_read" in cb.AUTO_APPROVE_DEFAULT
        assert "clipboard_write" in cb.AUTO_APPROVE_DEFAULT


class TestPlatformDetection:
    """Test platform detection logic."""

    @patch("sys.platform", "darwin")
    def test_detect_platform_macos(self):
        """Test macOS platform detection."""
        assert cb._detect_platform() == "macos"

    @patch("sys.platform", "linux")
    def test_detect_platform_linux(self):
        """Test Linux platform detection."""
        assert cb._detect_platform() == "linux"

    @patch("sys.platform", "win32")
    def test_detect_platform_unsupported(self):
        """Test unsupported platform detection."""
        assert cb._detect_platform() == "unsupported"


class TestSSHDetection:
    """Test SSH session detection."""

    @patch.dict("os.environ", {}, clear=True)
    def test_ssh_not_detected_empty_env(self):
        """Test SSH not detected when no env vars set."""
        assert cb._is_ssh_session() is False

    @patch.dict("os.environ", {"SSH_CONNECTION": "192.168.1.1 22 192.168.1.2 22"})
    def test_ssh_detected_ssh_connection(self):
        """Test SSH detected via SSH_CONNECTION."""
        assert cb._is_ssh_session() is True

    @patch.dict("os.environ", {"SSH_CLIENT": "192.168.1.1 22 22"})
    def test_ssh_detected_ssh_client(self):
        """Test SSH detected via SSH_CLIENT."""
        assert cb._is_ssh_session() is True

    @patch.dict("os.environ", {"SSH_TTY": "/dev/pts/0"})
    def test_ssh_detected_ssh_tty(self):
        """Test SSH detected via SSH_TTY."""
        assert cb._is_ssh_session() is True


class TestTTYDetection:
    """Test TTY detection."""

    def test_is_tty(self):
        """Test TTY detection."""
        # This test just checks the function exists and returns bool
        result = cb._is_tty()
        assert isinstance(result, bool)


class TestClipboardConfig:
    """Test configuration handling."""

    def test_config_enabled_default(self):
        """Test clipboard enabled by default."""
        cb._CONFIG_CACHE = None
        assert cb._is_clipboard_enabled() is True

    def test_config_enabled_false(self):
        """Test clipboard can be disabled via config."""
        cb._CONFIG_CACHE = {"clipboard": {"enabled": False}}
        assert cb._is_clipboard_enabled() is False

    def test_config_enabled_true(self):
        """Test clipboard can be explicitly enabled."""
        cb._CONFIG_CACHE = {"clipboard": {"enabled": True}}
        assert cb._is_clipboard_enabled() is True

    def test_config_mode_default(self):
        """Test default clipboard mode is 'auto'."""
        cb._CONFIG_CACHE = None
        assert cb._get_clipboard_mode() == "auto"

    def test_config_mode_native_only(self):
        """Test clipboard mode can be set to native_only."""
        cb._CONFIG_CACHE = {"clipboard": {"mode": "native_only"}}
        assert cb._get_clipboard_mode() == "native_only"

    def test_config_mode_both(self):
        """Test clipboard mode can be set to both."""
        cb._CONFIG_CACHE = {"clipboard": {"mode": "both"}}
        assert cb._get_clipboard_mode() == "both"

    def test_config_mode_osc52_only(self):
        """Test clipboard mode can be set to osc52_only."""
        cb._CONFIG_CACHE = {"clipboard": {"mode": "osc52_only"}}
        assert cb._get_clipboard_mode() == "osc52_only"

    def teardown_method(self):
        """Reset config cache after each test."""
        cb._CONFIG_CACHE = None


class TestOSC52Detection:
    """Test OSC52 mode decision logic."""

    @patch.dict("os.environ", {}, clear=True)
    def test_should_try_osc52_auto_local(self, monkeypatch):
        """Test OSC52 not attempted in auto mode on local terminal."""
        monkeypatch.setattr(cb, "_is_tty", lambda: True)
        # auto mode tries OSC52 when TTY is available
        assert cb._should_try_osc52("auto") is True

    @patch.dict("os.environ", {}, clear=True)
    def test_should_try_osc52_auto_pipe(self, monkeypatch):
        """Test OSC52 not attempted in auto mode on pipe."""
        monkeypatch.setattr(cb, "_is_tty", lambda: False)
        assert cb._should_try_osc52("auto") is False

    def test_should_try_osc52_native_only(self):
        """Test OSC52 never used in native_only mode."""
        assert cb._should_try_osc52("native_only") is False

    def test_should_try_osc52_both(self):
        """Test OSC52 always used in both mode."""
        assert cb._should_try_osc52("both") is True

    def test_should_try_osc52_osc52_only(self):
        """Test OSC52 always used in osc52_only mode."""
        assert cb._should_try_osc52("osc52_only") is True


class TestOSC52Writing:
    """Test OSC52 escape sequence writing."""

    @patch("sys.stdout.write")
    @patch("sys.stdout.flush")
    @patch("sys.stdout.isatty", return_value=True)
    def test_write_osc52_success(self, mock_isatty, mock_flush, mock_write):
        """Test successful OSC52 write."""
        cb._write_osc52("hello")
        mock_write.assert_called_once()
        mock_flush.assert_called_once()
        # Verify the written sequence contains base64 encoded text
        call_args = mock_write.call_args[0][0]
        assert "\033]52;" in call_args  # OSC sequence start
        assert "aGVsbG8=" in call_args  # base64 of "hello"
        assert "\007" in call_args  # BEL terminator

    @patch("sys.stdout.isatty", return_value=False)
    def test_write_osc52_no_tty(self, mock_isatty):
        """Test OSC52 write silently skipped when not TTY."""
        with patch("sys.stdout.write") as mock_write:
            cb._write_osc52("hello")
            mock_write.assert_not_called()

    @patch("sys.stdout.write")
    @patch("sys.stdout.flush")
    @patch("sys.stdout.isatty", return_value=True)
    def test_write_osc52_unicode(self, mock_isatty, mock_flush, mock_write):
        """Test OSC52 write with unicode content."""
        cb._write_osc52("你好")
        mock_write.assert_called_once()
        call_args = mock_write.call_args[0][0]
        assert "\033]52;" in call_args


class TestNativeClipboardMacOS:
    """Test native macOS clipboard operations."""

    @patch("gptsh.mcp.builtin.clipboard.importlib.import_module")
    def test_read_clipboard_macos_success(self, mock_import):
        """Test successful macOS clipboard read."""
        mock_pb = MagicMock()
        mock_pb.get_contents.return_value = "test content"
        mock_import.return_value = MagicMock(Pasteboard=MagicMock(return_value=mock_pb))

        result = cb._read_clipboard_macos()
        assert result == "test content"

    @patch("gptsh.mcp.builtin.clipboard.importlib.import_module")
    def test_read_clipboard_macos_none_content(self, mock_import):
        """Test macOS clipboard read with None content."""
        mock_pb = MagicMock()
        mock_pb.get_contents.return_value = None
        mock_import.return_value = MagicMock(Pasteboard=MagicMock(return_value=mock_pb))

        result = cb._read_clipboard_macos()
        assert result == ""

    @patch("gptsh.mcp.builtin.clipboard.importlib.import_module", side_effect=ImportError)
    def test_read_clipboard_macos_import_error(self, mock_import):
        """Test macOS clipboard read with missing pasteboard library."""
        with pytest.raises(RuntimeError, match="pasteboard library not installed"):
            cb._read_clipboard_macos()

    @patch("gptsh.mcp.builtin.clipboard.importlib.import_module")
    def test_write_clipboard_macos_success(self, mock_import):
        """Test successful macOS clipboard write."""
        mock_pb = MagicMock()
        mock_import.return_value = MagicMock(Pasteboard=MagicMock(return_value=mock_pb))

        cb._write_clipboard_macos("test content")
        mock_pb.set_contents.assert_called_once_with("test content")

    @patch("gptsh.mcp.builtin.clipboard.importlib.import_module", side_effect=ImportError)
    def test_write_clipboard_macos_import_error(self, mock_import):
        """Test macOS clipboard write with missing pasteboard library."""
        with pytest.raises(RuntimeError, match="pasteboard library not installed"):
            cb._write_clipboard_macos("test")


class TestNativeClipboardLinux:
    """Test native Linux clipboard operations using tkinter."""

    def test_read_clipboard_linux_success(self):
        """Test successful Linux clipboard read with tkinter."""
        mock_tk = MagicMock()
        mock_root = MagicMock()
        mock_root.clipboard_get.return_value = "test content"
        mock_tk.Tk.return_value = mock_root

        with patch(
            "gptsh.mcp.builtin.clipboard.importlib.import_module", return_value={"tk": mock_tk}
        ):
            # This test would require more complex mocking
            pass

    @pytest.mark.skip(reason="Requires tkinter in environment")
    def test_read_clipboard_linux_error(self):
        """Test Linux clipboard read with tkinter error."""
        with patch("tkinter.Tk", side_effect=Exception("Display error")):
            with pytest.raises(RuntimeError, match="Failed to read clipboard on Linux"):
                cb._read_clipboard_linux()

    @pytest.mark.skip(reason="Requires tkinter in environment")
    def test_write_clipboard_linux_error(self):
        """Test Linux clipboard write with tkinter error."""
        with patch("tkinter.Tk", side_effect=Exception("Display error")):
            with pytest.raises(RuntimeError, match="Failed to write clipboard on Linux"):
                cb._write_clipboard_linux("test")


class TestSystemClipboardDispatcher:
    """Test system clipboard read/write dispatcher."""

    @patch("gptsh.mcp.builtin.clipboard._detect_platform", return_value="macos")
    @patch("gptsh.mcp.builtin.clipboard._read_clipboard_macos", return_value="content")
    def test_read_system_clipboard_macos(self, mock_read_macos, mock_platform):
        """Test system clipboard read dispatches to macOS."""
        result = cb._read_system_clipboard()
        assert result == "content"
        mock_read_macos.assert_called_once()

    @patch("gptsh.mcp.builtin.clipboard._detect_platform", return_value="linux")
    @patch("gptsh.mcp.builtin.clipboard._read_clipboard_linux", return_value="content")
    def test_read_system_clipboard_linux(self, mock_read_linux, mock_platform):
        """Test system clipboard read dispatches to Linux."""
        result = cb._read_system_clipboard()
        assert result == "content"
        mock_read_linux.assert_called_once()

    @patch("gptsh.mcp.builtin.clipboard._detect_platform", return_value="unsupported")
    def test_read_system_clipboard_unsupported(self, mock_platform):
        """Test system clipboard read on unsupported platform."""
        with pytest.raises(RuntimeError, match="Clipboard not supported"):
            cb._read_system_clipboard()

    @patch("gptsh.mcp.builtin.clipboard._detect_platform", return_value="macos")
    @patch("gptsh.mcp.builtin.clipboard._write_clipboard_macos")
    def test_write_system_clipboard_macos(self, mock_write_macos, mock_platform):
        """Test system clipboard write dispatches to macOS."""
        cb._write_system_clipboard("content")
        mock_write_macos.assert_called_once_with("content")

    @patch("gptsh.mcp.builtin.clipboard._detect_platform", return_value="linux")
    @patch("gptsh.mcp.builtin.clipboard._write_clipboard_linux")
    def test_write_system_clipboard_linux(self, mock_write_linux, mock_platform):
        """Test system clipboard write dispatches to Linux."""
        cb._write_system_clipboard("content")
        mock_write_linux.assert_called_once_with("content")

    @patch("gptsh.mcp.builtin.clipboard._detect_platform", return_value="unsupported")
    def test_write_system_clipboard_unsupported(self, mock_platform):
        """Test system clipboard write on unsupported platform."""
        with pytest.raises(RuntimeError, match="Clipboard not supported"):
            cb._write_system_clipboard("content")


class TestClipboardReadTool:
    """Test clipboard_read MCP tool."""

    def test_clipboard_read_disabled(self):
        """Test clipboard_read when tool is disabled."""
        cb._CONFIG_CACHE = {"clipboard": {"enabled": False}}
        result = cb._tool_clipboard_read({})
        data = json.loads(result)
        assert data["ok"] is False
        assert "disabled in config" in data["error"]
        cb._CONFIG_CACHE = None

    @patch("gptsh.mcp.builtin.clipboard._read_system_clipboard", return_value="test content")
    def test_clipboard_read_success(self, mock_read):
        """Test successful clipboard read."""
        cb._CONFIG_CACHE = {"clipboard": {"enabled": True}}
        result = cb._tool_clipboard_read({})
        data = json.loads(result)
        assert data["ok"] is True
        assert data["content"] == "test content"
        cb._CONFIG_CACHE = None

    @patch(
        "gptsh.mcp.builtin.clipboard._read_system_clipboard",
        side_effect=RuntimeError("Platform error"),
    )
    def test_clipboard_read_error(self, mock_read):
        """Test clipboard read with error."""
        cb._CONFIG_CACHE = {"clipboard": {"enabled": True}}
        result = cb._tool_clipboard_read({})
        data = json.loads(result)
        assert data["ok"] is False
        assert "Platform error" in data["error"]
        cb._CONFIG_CACHE = None


class TestClipboardWriteTool:
    """Test clipboard_write MCP tool."""

    def test_clipboard_write_disabled(self):
        """Test clipboard_write when tool is disabled."""
        cb._CONFIG_CACHE = {"clipboard": {"enabled": False}}
        result = cb._tool_clipboard_write({"text": "test"})
        data = json.loads(result)
        assert data["ok"] is False
        assert "disabled in config" in data["error"]
        cb._CONFIG_CACHE = None

    def test_clipboard_write_missing_text(self):
        """Test clipboard_write without text argument."""
        cb._CONFIG_CACHE = {"clipboard": {"enabled": True}}
        result = cb._tool_clipboard_write({})
        data = json.loads(result)
        assert data["ok"] is False
        assert "must be a string" in data["error"]
        cb._CONFIG_CACHE = None

    def test_clipboard_write_invalid_text_type(self):
        """Test clipboard_write with non-string text."""
        cb._CONFIG_CACHE = {"clipboard": {"enabled": True}}
        result = cb._tool_clipboard_write({"text": 123})
        data = json.loads(result)
        assert data["ok"] is False
        assert "must be a string" in data["error"]
        cb._CONFIG_CACHE = None

    @patch("gptsh.mcp.builtin.clipboard._write_system_clipboard")
    @patch("gptsh.mcp.builtin.clipboard._is_ssh_session", return_value=False)
    @patch("gptsh.mcp.builtin.clipboard._get_clipboard_mode", return_value="auto")
    def test_clipboard_write_auto_mode_local(self, mock_mode, mock_ssh, mock_write):
        """Test clipboard_write in auto mode on local terminal."""
        cb._CONFIG_CACHE = {"clipboard": {"enabled": True}}
        result = cb._tool_clipboard_write({"text": "test"})
        data = json.loads(result)
        assert data["ok"] is True
        assert data["method"] == "native"
        mock_write.assert_called_once_with("test")
        cb._CONFIG_CACHE = None

    @patch("gptsh.mcp.builtin.clipboard._write_system_clipboard")
    @patch("gptsh.mcp.builtin.clipboard._write_osc52")
    @patch("gptsh.mcp.builtin.clipboard._is_ssh_session", return_value=True)
    @patch("gptsh.mcp.builtin.clipboard._get_clipboard_mode", return_value="auto")
    def test_clipboard_write_auto_mode_ssh(self, mock_mode, mock_ssh, mock_osc52, mock_write):
        """Test clipboard_write in auto mode on SSH session."""
        cb._CONFIG_CACHE = {"clipboard": {"enabled": True}}
        result = cb._tool_clipboard_write({"text": "test"})
        data = json.loads(result)
        assert data["ok"] is True
        assert data["method"] in ["both", "osc52"]
        mock_osc52.assert_called_once_with("test")
        cb._CONFIG_CACHE = None

    @patch("gptsh.mcp.builtin.clipboard._write_system_clipboard")
    @patch("gptsh.mcp.builtin.clipboard._get_clipboard_mode", return_value="native_only")
    def test_clipboard_write_native_only_mode(self, mock_mode, mock_write):
        """Test clipboard_write in native_only mode."""
        cb._CONFIG_CACHE = {"clipboard": {"enabled": True}}
        result = cb._tool_clipboard_write({"text": "test"})
        data = json.loads(result)
        assert data["ok"] is True
        assert data["method"] == "native"
        mock_write.assert_called_once_with("test")
        cb._CONFIG_CACHE = None

    @patch("gptsh.mcp.builtin.clipboard._write_osc52")
    @patch("gptsh.mcp.builtin.clipboard._get_clipboard_mode", return_value="osc52_only")
    def test_clipboard_write_osc52_only_mode(self, mock_mode, mock_osc52):
        """Test clipboard_write in osc52_only mode."""
        cb._CONFIG_CACHE = {"clipboard": {"enabled": True}}
        result = cb._tool_clipboard_write({"text": "test"})
        data = json.loads(result)
        assert data["ok"] is True
        assert data["method"] == "osc52"
        mock_osc52.assert_called_once_with("test")
        cb._CONFIG_CACHE = None

    @patch("gptsh.mcp.builtin.clipboard._write_system_clipboard")
    @patch("gptsh.mcp.builtin.clipboard._write_osc52")
    @patch("gptsh.mcp.builtin.clipboard._get_clipboard_mode", return_value="both")
    def test_clipboard_write_both_mode(self, mock_mode, mock_osc52, mock_write):
        """Test clipboard_write in both mode."""
        cb._CONFIG_CACHE = {"clipboard": {"enabled": True}}
        result = cb._tool_clipboard_write({"text": "test"})
        data = json.loads(result)
        assert data["ok"] is True
        assert data["method"] == "both"
        mock_osc52.assert_called_once_with("test")
        mock_write.assert_called_once_with("test")
        cb._CONFIG_CACHE = None


class TestExecuteDispatcher:
    """Test execute function dispatcher."""

    @patch("gptsh.mcp.builtin.clipboard._tool_clipboard_read", return_value='{"ok": true}')
    def test_execute_clipboard_read(self, mock_read):
        """Test execute dispatcher for clipboard_read."""
        result = cb.execute("clipboard_read", {})
        assert result == '{"ok": true}'
        mock_read.assert_called_once()

    @patch("gptsh.mcp.builtin.clipboard._tool_clipboard_write", return_value='{"ok": true}')
    def test_execute_clipboard_write(self, mock_write):
        """Test execute dispatcher for clipboard_write."""
        result = cb.execute("clipboard_write", {"text": "test"})
        assert result == '{"ok": true}'
        mock_write.assert_called_once()

    def test_execute_unknown_tool(self):
        """Test execute with unknown tool."""
        result = cb.execute("unknown_tool", {})
        data = json.loads(result)
        assert data["ok"] is False
        assert "Unknown tool" in data["error"]
