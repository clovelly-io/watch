"""The Claude Agent SDK, configured for a Watch session.

The SDK reaches the model only through Watch's gateway: ``ANTHROPIC_BASE_URL`` is the
gateway and ``ANTHROPIC_API_KEY`` the job's gateway token, so the bundled Claude Code
sends ``x-api-key: cwg_…`` there. Every model alias resolves to the job's model, so
background calls name a model the gateway offers. Claude Code's own traffic elsewhere
(telemetry, error reports, feature flags, updates, the plugin marketplace) is switched
off; the sandbox's policy refuses it anyway, and ``api.anthropic.com`` is not a
destination. The monitors job's tools are read-only (``Read``, ``Grep``, ``Glob``); the
alert job's add ``Bash``, ``Edit`` and ``Write`` (:data:`WRITE_TOOLS`); anything else is
denied (``dontAsk``). Nothing is loaded from the cloned repository: no settings, hooks,
instruction files (``CLAUDE.md``) or MCP servers (``setting_sources=[]``,
``strict_mcp_config``), and Claude Code's own configuration lives in ``/job/.claude``.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Read-only tools: the monitors job writes nothing but its answer.
TOOLS = ["Read", "Grep", "Glob"]
#: The alert job's: it writes a test and a fix and runs them.
WRITE_TOOLS = [*TOOLS, "Bash", "Edit", "Write"]
MAX_TURNS = 40

#: Variables a session's Claude Code must not inherit from the image.
_DROPPED = ("ANTHROPIC_", "CLAUDE_")


def environment(gateway: dict[str, Any], config_dir: Path) -> dict[str, str]:
    """The environment Claude Code runs with: the gateway, the job's token, its model for
    every alias, and its non-essential traffic off."""
    model = gateway["model"]
    env = {k: v for k, v in os.environ.items() if not k.startswith(_DROPPED)}
    env.update(
        {
            "ANTHROPIC_BASE_URL": gateway["url"],
            "ANTHROPIC_API_KEY": gateway["token"],
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "CLAUDE_CODE_SUBAGENT_MODEL": model,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_FEATURE_FLAGS": "1",
            "DISABLE_FEEDBACK_COMMAND": "1",
            "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
            "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
            "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
            "CLAUDE_CODE_MAX_RETRIES": "4",
            "CLAUDE_CONFIG_DIR": str(config_dir),
        }
    )
    return env


@dataclass
class Reply:
    answer: dict[str, Any] | None
    budget_spent: bool
    error: str
    turns: int


class Session:
    """One conversation with the model; :meth:`ask` continues it."""

    def __init__(
        self,
        gateway: dict[str, Any],
        *,
        cwd: Path,
        system: str,
        output_schema: dict[str, Any],
        tools: list[str] | None = None,
        max_turns: int = MAX_TURNS,
    ) -> None:
        config_dir = Path("/job/.claude")
        config_dir.mkdir(parents=True, exist_ok=True)
        self._gateway = gateway
        self._cwd = cwd
        self._system = system
        self._schema = output_schema
        self._env = environment(gateway, config_dir)
        self._tools = tools or TOOLS
        self._max_turns = max_turns
        self._session_id: str | None = None

    def ask(self, prompt: str, output_schema: dict[str, Any] | None = None) -> Reply:
        """Continues the conversation with ``prompt``; the answer fits ``output_schema``, or
        the session's own."""
        return asyncio.run(self._ask(prompt, output_schema or self._schema))

    async def _ask(self, prompt: str, schema: dict[str, Any]) -> Reply:
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

        options = ClaudeAgentOptions(
            model=self._gateway["model"],
            system_prompt=self._system,
            tools=self._tools,
            allowed_tools=self._tools,
            permission_mode="dontAsk",
            max_turns=self._max_turns,
            setting_sources=[],
            strict_mcp_config=True,
            cwd=str(self._cwd),
            env=self._env,
            effort="medium",
            output_format={"type": "json_schema", "schema": schema},
            resume=self._session_id,
        )
        result: ResultMessage | None = None
        billing = False
        failure = ""
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage) and getattr(message, "error", None) == "billing_error":
                    billing = True
                if isinstance(message, ResultMessage):
                    result = message
        except Exception as e:  # noqa: BLE001 (the SDK raises when Claude Code exits on an error)
            failure = f"{type(e).__name__}: {str(e)[:300]}"
        if result is None:
            spent = billing or "budget is spent" in failure or "402" in failure
            return Reply(None, spent, failure or "the agent ended without a result", 0)
        self._session_id = result.session_id
        said = " ".join([result.result or "", *(result.errors or [])])
        # The gateway's refusal at the budget: 402 billing_error, its message naming it.
        spent = billing or result.api_error_status == 402 or "budget is spent" in said
        answer = result.structured_output if isinstance(result.structured_output, dict) else None
        error = "" if answer is not None else f"{result.subtype} {result.terminal_reason} {result.api_error_status}: {said[:300]}"
        return Reply(answer, spent, error, result.num_turns)
