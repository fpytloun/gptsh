from typing import List, Dict

def list_tools_stub() -> Dict[str, List[str]]:
    # MVP stub: Return dummy tools grouped by server
    return {
        'filesystem': ['echo_tool', 'shell_run'],
        'tavily': ['search_web', 'summarize']
    }
