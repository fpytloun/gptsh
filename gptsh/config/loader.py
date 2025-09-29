import os
import yaml
import re
import glob
from typing import Any, Dict, Optional

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

def load_yaml(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.isfile(path):
        return None
    with open(path, 'r') as f:
        text = f.read()
    text = _expand_env(text)
    return yaml.safe_load(text) or {}

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
