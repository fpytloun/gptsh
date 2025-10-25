from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
from typing import Any, Dict, List

# Default auto-approval for all tools in this builtin server
AUTO_APPROVE_DEFAULT = ["search_history", "get_history"]


def list_tools() -> List[str]:
    return ["execute", "get_history", "search_history"]

def list_tools_detailed() -> List[Dict[str, Any]]:
    return [
        {
            "name": "execute",
            "description": "Execute a shell command and return JSON with exit code, stdout, and stderr.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Command string to execute using /bin/sh -c",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory for the command (optional)",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (optional). If exceeded, process is killed and exit_code is -1.",
                    },
                    "env": {
                        "type": "object",
                        "description": "Environment variable overrides (string-to-string map).",
                        "additionalProperties": True,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_history",
            "description": "Return the last n shell commands from the history file specified by $HISTFILE. Fails with error if $HISTFILE is not set or file is unreadable.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "description": "Number of last history entries.",
                        "default": 20,
                        "minimum": 1,
                        "maximum": 100
                    },
                },
                "required": ["n"],
                "additionalProperties": False,
            },
        },
        {
            "name": "search_history",
            "description": "Search for commands in shell history matching a regex or substring. Returns each match and up to 'context' commands before and after it. Reads $HISTFILE. Fails with error if $HISTFILE is not set or file is unreadable.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex or substring to match against history.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Return this many last matches.",
                        "default": 20,
                        "minimum": 1,
                        "maximum": 100
                    },
                    "context": {
                        "type": "integer",
                        "description": "Number of commands before and after each match to include in output.",
                        "default": 5,
                        "minimum": 0,
                        "maximum": 20
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    ]

def _get_histfile() -> str:
    candidates = []
    histfile_path = os.environ.get("HISTFILE")
    if histfile_path:
        candidates.append(histfile_path)
    # Common history file locations
    candidates.extend([
        "~/.zhistory",
        "~/.zsh_history",
        "~/.bash_history",
    ])
    for fp in candidates:
        resolved = os.path.expanduser(fp)
        if os.path.isfile(resolved):
            return resolved
    raise RuntimeError("No shell history file found; checked $HISTFILE and common paths.")

def _read_history(path: str) -> List[str]:
    with open(path, "r", errors="ignore") as f:
        lines = f.readlines()
    # Reverse lines: newest first
    lines = lines[::-1]
    entries = []
    local_tz = datetime.datetime.now().astimezone().tzinfo
    for line in lines:
        orig = line.rstrip("\n")
        # Extended zsh format: ': 1678997800:0;command'
        if orig.startswith(": ") and ";" in orig:
            try:
                parts = orig.split(";", 1)
                meta = parts[0]
                command = parts[1].strip()
                meta_fields = meta.split(":")
                ts_raw = meta_fields[1] if len(meta_fields) > 1 else None
                timestamp = None
                if ts_raw:
                    try:
                        ts_int = int(ts_raw)
                        timestamp = datetime.datetime.fromtimestamp(ts_int, tz=local_tz).isoformat()
                    except Exception:
                        timestamp = None
                entry = {"command": command}
                if timestamp:
                    entry["timestamp"] = timestamp
                entries.append(entry)
            except Exception:
                # fallback: plain
                entries.append({"command": orig})
        else:
            plaincmd = orig.strip()
            if plaincmd:
                entries.append({"command": plaincmd})
    return entries

def _tool_get_history(arguments: Dict[str, Any]) -> str:
    n = arguments.get("n", 20)
    if not isinstance(n, int) or n < 1 or n > 100:
        return json.dumps({"ok": False, "error": "Argument 'n' must be integer 1..100."})
    try:
        histfile = _get_histfile()
        history = _read_history(histfile)
        return json.dumps({"ok": True, "history": history[:n]})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

def _tool_search_history(arguments: Dict[str, Any]) -> str:
    pattern = arguments.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return json.dumps({"ok": False, "error": "Argument 'pattern' must be a non-empty string."})
    max_results = arguments.get("max_results", 20)
    if not isinstance(max_results, int) or max_results < 1 or max_results > 100:
        return json.dumps({"ok": False, "error": "Argument 'max_results' must be integer 1..100."})
    try:
        histfile = _get_histfile()
        history = _read_history(histfile)
        context_n = arguments.get("context", 5)
        if not isinstance(context_n, int) or context_n < 0 or context_n > 20:
            context_n = 5
        try:
            regex = re.compile(pattern)
            matches = [i for i, entry in enumerate(history) if regex.search(entry["command"])]
        except re.error:
            # fallback substring search
            matches = [i for i, entry in enumerate(history) if pattern in entry["command"]]
        results = []
        for idx in matches[:max_results]:
            start = max(0, idx - context_n)
            end = min(len(history), idx + context_n + 1)
            bundle = {
                "match": history[idx],
                "before": history[start:idx],
                "after": history[idx+1:end],
            }
            results.append(bundle)
        return json.dumps({"ok": True, "results": results})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

def _tool_execute(arguments: Dict[str, Any]) -> str:
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        raise RuntimeError("Field 'command' (string) is required")
    cwd = arguments.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise RuntimeError("Field 'cwd' must be a string if provided")
    if cwd is None:
        cwd = os.getcwd()
    timeout_val = arguments.get("timeout")
    if timeout_val is not None:
        try:
            timeout_val = float(timeout_val)
            if timeout_val <= 0:
                timeout_val = None
        except Exception:
            timeout_val = None
    env_overrides = arguments.get("env") or {}
    if env_overrides is not None and not isinstance(env_overrides, dict):
        raise RuntimeError("Field 'env' must be an object if provided")
    env = os.environ.copy()
    # Coerce all env values to strings
    for k, v in (env_overrides or {}).items():
        try:
            env[str(k)] = "" if v is None else str(v)
        except Exception:
            continue
    # Figure out shell
    shell_path = None
    shell_env = os.environ.get("SHELL")
    if shell_env and os.path.isfile(shell_env):
        shell_path = shell_env
    else:
        try:
            import pwd
            shell_path = pwd.getpwuid(os.getuid()).pw_shell
        except Exception:
            shell_path = None
    if not shell_path or not os.path.isfile(shell_path):
        shell_path = "/bin/sh"
    try:
        completed = subprocess.run(
            [shell_path, "-c", command],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_val if isinstance(timeout_val, (int, float)) else None,
        )
        result = {
            "exit_code": int(completed.returncode),
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
        }
        return json.dumps(result, ensure_ascii=False)
    except subprocess.TimeoutExpired as e:
        partial_stdout = ""
        partial_stderr = ""
        try:
            partial_stdout = e.stdout if isinstance(e.stdout, str) else (e.stdout.decode("utf-8", "replace") if e.stdout else "")
            partial_stderr = e.stderr if isinstance(e.stderr, str) else (e.stderr.decode("utf-8", "replace") if e.stderr else "")
        except Exception:
            pass
        result = {
            "exit_code": -1,
            "stdout": partial_stdout or "",
            "stderr": str(partial_stderr) + ("\n[Timed out]" if partial_stderr else "[Timed out]"),
        }
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        result = {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"[Execution error] {e}",
        }
        return json.dumps(result, ensure_ascii=False)

def execute(tool: str, arguments: Dict[str, Any]) -> str:
    if tool == "execute":
        return _tool_execute(arguments)
    elif tool == "get_history":
        return _tool_get_history(arguments)
    elif tool == "search_history":
        return _tool_search_history(arguments)
    else:
        raise RuntimeError(f"Unknown tool: shell:{tool}")
