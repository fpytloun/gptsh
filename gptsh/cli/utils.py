from __future__ import annotations

import sys
from typing import Any, Dict, List, Literal, Optional, Tuple

import click

from gptsh.core.config_resolver import build_agent
from gptsh.mcp.api import get_auto_approved_tools


async def resolve_agent_and_settings(
    *,
    config: Dict[str, Any],
    agent_name: Optional[str],
    provider_name: Optional[str],
    model_override: Optional[str],
    tools_filter_labels: Optional[List[str]],
    no_tools_flag: bool,
    output_format: str,
) -> Tuple[Any, Dict[str, Any], Dict[str, Any], str, bool, Optional[List[str]]]:
    from gptsh.core.config_api import (
        compute_tools_policy,
        effective_output,
        select_agent_provider_dicts,
    )

    agent_conf, provider_conf = select_agent_provider_dicts(
        config, cli_agent=agent_name, cli_provider=provider_name
    )
    labels = tools_filter_labels if tools_filter_labels is not None else None
    no_tools_effective, allowed = compute_tools_policy(agent_conf, labels, no_tools_flag)
    output_effective = effective_output(output_format, agent_conf)
    agent_obj = await build_agent(
        config,
        cli_agent=agent_name,
        cli_provider=provider_name,
        cli_tools_filter=labels,
        cli_model_override=model_override,
        cli_no_tools=no_tools_effective,
    )
    return agent_obj, agent_conf, provider_conf, output_effective, no_tools_effective, allowed


def print_tools_listing(tools_map: Dict[str, List[str]], approved_map: Dict[str, List[str]]) -> None:
    total_servers = len(tools_map)
    click.echo(f"Discovered tools ({total_servers} server{'s' if total_servers != 1 else ''}):")
    for server, names in tools_map.items():
        click.echo(f"{server} ({len(names)}):")
        if names:
            approved_set = set(approved_map.get(server, []) or [])
            global_tools = set(approved_map.get("*", []) or [])
            for n in names:
                badge = " 󰁪" if ("*" in approved_set or n in approved_set or n in global_tools) else ""
                click.echo(f"  - {n}{badge}")
        else:
            click.echo("  (no tools found or discovery failed)")


def print_agents_listing(
    config: Dict[str, Any], agents_conf: Dict[str, Any], tools_map: Dict[str, List[str]], no_tools: bool
) -> None:
    providers_conf = config.get("providers", {}) or {}
    default_provider_name = config.get("default_provider") or (
        next(iter(providers_conf)) if providers_conf else None
    )
    click.echo("Configured agents:")
    for agent_name, aconf in agents_conf.items():
        if not isinstance(aconf, dict):
            aconf = {}
        agent_provider = aconf.get("provider") or default_provider_name
        chosen_model = aconf.get("model") or ((providers_conf.get(agent_provider) or {}).get("model")) or "?"
        click.echo(f"- {agent_name}")
        click.echo(f"  provider: {agent_provider or '?'}")
        click.echo(f"  model: {chosen_model}")
        tools_field = aconf.get("tools")
        allowed_servers: Optional[List[str]] = None
        if isinstance(tools_field, list):
            allowed_servers = [str(x) for x in tools_field if x is not None]
            if len(allowed_servers) == 0:
                click.echo("  tools: (disabled)")
                continue
        try:
            approved_map = get_auto_approved_tools(config, agent_conf=aconf)
        except Exception:
            approved_map = {}
        if no_tools:
            click.echo("  tools: (disabled by --no-tools)")
            continue
        server_names = list(tools_map.keys())
        if allowed_servers is not None:
            server_names = [s for s in server_names if s in allowed_servers]
        if not server_names:
            click.echo("  tools: (none discovered)")
            continue
        click.echo("  tools:")
        for server in server_names:
            names = tools_map.get(server, []) or []
            click.echo(f"    {server} ({len(names)}):")
            if names:
                approved_set = set(approved_map.get(server, []) or [])
                global_set = set(approved_map.get("*", []) or [])
                for t in names:
                    badge = " 󰁪" if ("*" in approved_set or t in approved_set or t in global_set) else ""
                    click.echo(f"      - {t}{badge}")
            else:
                click.echo("      (no tools found or discovery failed)")


def is_tty(assume_tty: bool = False, stream: Literal["stdin", "stdout", "stderr"] = "stdout") -> bool:
    """Return True if we should treat the session as attached to a TTY.

    When assume_tty is True, always return True (useful for tests/CI).
    Otherwise, check Click's stdout stream for TTY capability.
    """
    if assume_tty:
        return True
    try:
        iostream = click.get_text_stream(stream)
    except Exception:
        iostream = getattr(sys, stream)
    return bool(getattr(iostream, 'isatty', lambda: False)())
