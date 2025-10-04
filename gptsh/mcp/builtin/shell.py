from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Dict, List


def list_tools() -> List[str]:
    return ["execute"]

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
        }
    ]

def execute(tool: str, arguments: Dict[str, Any]) -> str:
    if tool != "execute":
        raise RuntimeError(f"Unknown tool: shell:{tool}")

    if not isinstance(arguments, dict):
        raise RuntimeError("Arguments must be an object")

    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        raise RuntimeError("Field 'command' (string) is required")

    cwd = arguments.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise RuntimeError("Field 'cwd' must be a string if provided")

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
            # Skip un-coercible keys/values
            continue

    try:
        completed = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=cwd or None,
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
        # Terminated due to timeout
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
            "stderr": partial_stderr + ("\n[Timed out]" if partial_stderr else "[Timed out]"),
        }
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        # Unexpected failure to spawn/execute
        result = {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"[Execution error] {e}",
        }
        return json.dumps(result, ensure_ascii=False)
