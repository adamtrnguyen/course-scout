import logging
from dataclasses import dataclass, field

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from course_scout.domain.services import AIProvider

logger = logging.getLogger(__name__)


@dataclass
class UsageStats:
    """Tracks cumulative usage across calls."""

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0
    total_duration_ms: int = 0
    call_count: int = 0
    calls: list[dict] = field(default_factory=list)

    def record(self, result: ResultMessage, model: str):
        """Record usage from a ResultMessage."""
        self.call_count += 1
        self.total_duration_ms += result.duration_ms or 0
        self.total_cost_usd += result.total_cost_usd or 0.0

        usage = result.usage or {}
        input_tok = usage.get("input_tokens", 0)
        output_tok = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_create = usage.get("cache_creation_input_tokens", 0)

        self.total_input_tokens += input_tok
        self.total_output_tokens += output_tok
        self.total_cache_read_tokens += cache_read
        self.total_cache_creation_tokens += cache_create

        self.calls.append({
            "model": model,
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "cache_read": cache_read,
            "duration_ms": result.duration_ms or 0,
            "cost_usd": result.total_cost_usd or 0.0,
        })

    def summary(self) -> str:
        """Return a formatted usage summary with Max plan budget estimate."""
        daily_budget = 5_000_000
        five_hour_budget = daily_budget * (5 / 24)
        daily_pct = (self.total_output_tokens / daily_budget * 100) if daily_budget else 0
        window_pct = (self.total_output_tokens / five_hour_budget * 100) if five_hour_budget else 0

        lines = [
            f"━━━ Usage Summary ({self.call_count} API calls) ━━━",
            f"  Input tokens:  {self.total_input_tokens:,}",
            f"  Output tokens: {self.total_output_tokens:,}",
            f"  Cache read:    {self.total_cache_read_tokens:,}",
            f"  Total time:    {self.total_duration_ms / 1000:.1f}s",
            f"  Est. cost:     ${self.total_cost_usd:.4f}",
            "  ── Max Plan Budget (approx) ──",
            f"  5h window:     ~{window_pct:.1f}% used",
            f"  Daily:         ~{daily_pct:.1f}% used",
        ]
        return "\n".join(lines)


class ClaudeProvider(AIProvider):
    def __init__(self, thinking: str = "adaptive", effort: str = "medium"):
        """Initialize with thinking/effort config. Auth handled by Claude Agent SDK."""
        self.usage = UsageStats()
        self.thinking = thinking
        self.effort = effort

    def _thinking_config(self) -> dict:
        """Build thinking config dict for ClaudeAgentOptions."""
        if self.thinking == "enabled":
            return {"type": "enabled", "budget_tokens": 10000}
        if self.thinking == "disabled":
            return {"type": "disabled"}
        return {"type": "adaptive"}

    async def generate_structured(
        self, model_id: str, system_prompt: str, input_data: str, output_schema: type
    ) -> any:
        """Generate structured output using Claude Agent SDK."""
        schema = output_schema.model_json_schema()

        options = ClaudeAgentOptions(
            model=model_id,
            system_prompt=system_prompt,
            max_turns=1,
            setting_sources=[],
            disallowed_tools=[
                "Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
                "Glob", "Grep", "WebFetch", "WebSearch", "Agent",
            ],
            thinking=self._thinking_config(),
            effort=self.effort,
            output_format={"type": "json_schema", "schema": schema},
        )

        structured, tool_output, last_text = await self._collect_messages(
            input_data, options, model_id
        )
        return self._parse_output(output_schema, structured, tool_output, last_text)

    async def _collect_messages(self, input_data, options, model_id):
        """Iterate SDK messages and extract structured output, tool output, and text."""
        structured = None
        tool_output = None
        last_text = None

        async for message in query(prompt=input_data, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock) and block.name == "StructuredOutput":
                        tool_output = block.input
                    elif isinstance(block, TextBlock):
                        last_text = block.text
            elif isinstance(message, ResultMessage):
                if message.is_error:
                    logger.warning(f"ResultMessage error: {message.subtype}")
                self.usage.record(message, model_id)
                self._log_usage(message, model_id)
                if message.structured_output is not None:
                    structured = message.structured_output

        return structured, tool_output, last_text

    @staticmethod
    def _log_usage(message, model_id):
        """Log per-call usage stats."""
        u = message.usage or {}
        logger.info(
            f"[{model_id}] {u.get('input_tokens', 0)} in / "
            f"{u.get('output_tokens', 0)} out / "
            f"{u.get('cache_read_input_tokens', 0)} cache / "
            f"{message.duration_ms or 0}ms / "
            f"${message.total_cost_usd or 0:.4f}"
        )

    @staticmethod
    def _parse_output(output_schema, structured, tool_output, last_text):
        """Parse output in priority order: structured > tool > text."""
        if structured is not None:
            return output_schema.model_validate(structured)
        if tool_output is not None:
            return output_schema.model_validate(tool_output)
        if last_text:
            text = last_text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                text = text.rsplit("```", 1)[0]
            return output_schema.model_validate_json(text.strip())

        raise RuntimeError("No output received from Claude Agent SDK")
