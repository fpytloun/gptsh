import glob
import os
import re
from typing import Any, Dict, Optional

import yaml

CONFIG_PATHS = [
    os.path.expanduser("~/.config/gptsh/config.yml"),
    os.path.abspath(".gptsh/config.yml"),
]

_ENV_PATTERN = re.compile(r"\$\{(\w+)\}")

def _expand_env(content: str) -> str:
    """Expand ${VAR_NAME} from environment in the given content string."""
    def repl(match):
        var_name = match.group(1)
        return os.getenv(var_name, match.group(0))
    return _ENV_PATTERN.sub(repl, content)

def _parse_yaml_with_includes(text: str, base_dir: str) -> Any:
    """
    Parse YAML supporting a custom !include tag.
    - Usage: key: !include path/to/file.yml
    - Supports wildcards: key: !include path/pattern/*
    - Paths are resolved relative to base_dir (config file directory).
    Merging rules for multiple matches:
      - All dicts => deep-merge in sorted filename order (later overrides earlier)
      - All lists => concatenate
      - Mixed types => return list of loaded values in sorted order
    """
    class IncludeLoader(yaml.SafeLoader):
        pass

    def construct_include(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
        # Expect a scalar path/pattern
        try:
            pattern = loader.construct_scalar(node)  # type: ignore[arg-type]
        except Exception:
            return {}
        if not isinstance(pattern, str):
            return {}

        full_pattern = os.path.join(base_dir, pattern)
        matches = sorted(glob.glob(full_pattern))
        results: list[Any] = []

        if not matches:
            # If no wildcard characters and file exists, try direct include
            if not any(ch in pattern for ch in ("*", "?", "[")) and os.path.isfile(full_pattern):
                loaded = _load_yaml_any(full_pattern)
                return loaded if loaded is not None else {}
            return {}

        for p in matches:
            loaded = _load_yaml_any(p)
            if loaded is not None:
                results.append(loaded)

        if not results:
            return {}
        if len(results) == 1:
            return results[0]
        if all(isinstance(r, dict) for r in results):
            merged: Dict[str, Any] = {}
            for r in results:
                merged = merge_dicts(merged, r)  # type: ignore[arg-type]
            return merged
        if all(isinstance(r, list) for r in results):
            out_list: list[Any] = []
            for r in results:
                out_list.extend(r)
            return out_list
        return results

    IncludeLoader.add_constructor("!include", construct_include)
    return yaml.load(text, Loader=IncludeLoader)

def _load_yaml_any(path: str) -> Any:
    """Load a YAML file (any top-level type) with env expansion and !include support."""
    if not os.path.isfile(path):
        return None
    with open(path, "r") as f:
        text = f.read()
    text = _expand_env(text)
    base_dir = os.path.dirname(os.path.abspath(path))
    return _parse_yaml_with_includes(text, base_dir)

def load_yaml(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.isfile(path):
        return None
    # Use include-aware parser; ensure dict output for config merging
    data = _load_yaml_any(path)
    return data if isinstance(data, dict) else {}

def merge_dicts(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Merge two dictionaries recursively, b overrides a."""
    result = a.copy()
    for k, v in b.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = merge_dicts(result[k], v)
        else:
            result[k] = v
    return result

def load_config(paths=CONFIG_PATHS) -> Dict[str, Any]:
    config: Dict[str, Any] = {}
    # Determine standard global main config and optional config.d directory
    global_main = os.path.expanduser("~/.config/gptsh/config.yml")
    snippets_dir = os.path.expanduser("~/.config/gptsh/config.d")

    for path in paths:
        loaded = load_yaml(path)
        if loaded:
            config = merge_dicts(config, loaded)
        # If this is the global main config, also merge any *.yml snippets from config.d
        try:
            if os.path.abspath(path) == os.path.abspath(global_main) and os.path.isdir(snippets_dir):
                for snip in sorted(glob.glob(os.path.join(snippets_dir, "*.yml"))):
                    snip_loaded = load_yaml(snip)
                    if snip_loaded:
                        config = merge_dicts(config, snip_loaded)
        except Exception:
            # Do not fail if directory reading/parsing fails
            pass
    return config
