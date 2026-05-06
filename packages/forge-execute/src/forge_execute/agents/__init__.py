"""Coding-Agent-Schicht.

Aus todos.txt: forge-Loop-1 nutzt Claude Code CLI als Coding-Agent. Architektonisch
ist das ein Plug-in, nicht das Fundament. Der Runner spricht ausschließlich
gegen das `CodingAgent`-Protocol; die konkrete Implementierung ist austauschbar.

In v1 produktiv: `ClaudeCodeCLIAgent`. Für Tests: `MockCodingAgent`.
"""

from forge_execute.agents.base import (
    CodingAgent,
    CodingAgentError,
    CodingAgentTimeout,
    ProposalResult,
)
from forge_execute.agents.claude_cli import ClaudeCodeCLIAgent
from forge_execute.agents.mock import MockCodingAgent

__all__ = [
    "ClaudeCodeCLIAgent",
    "CodingAgent",
    "CodingAgentError",
    "CodingAgentTimeout",
    "MockCodingAgent",
    "ProposalResult",
]
