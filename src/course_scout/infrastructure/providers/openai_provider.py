"""OpenAI-compatible provider for structured output generation.

Works with any OpenAI-compatible API: OpenAI, DeepSeek, Together, etc.
Uses JSON mode + schema-in-prompt for structured output.
"""

import json
import logging
import time
from dataclasses import dataclass, field

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel

from course_scout.domain.services import AIProvider

logger = logging.getLogger(__name__)


# ── Pricing table (USD per million tokens) ──

_PRICING: dict[str, dict[str, float]] = {
    "deepseek-chat": {
        "input": 0.27,
        "input_cache_hit": 0.07,
        "output": 1.10,
    },
    "deepseek-reasoner": {
        "input": 0.55,
        "input_cache_hit": 0.14,
        "output": 2.19,
    },
}


def _estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_hit_tokens: int = 0,
    cache_miss_tokens: int = 0,
) -> float:
    """Calculate cost from token counts and pricing table."""
    prices = _PRICING.get(model)
    if not prices:
        return 0.0
    # If cache breakdown available, use it; otherwise treat all input as cache miss
    if cache_hit_tokens or cache_miss_tokens:
        input_cost = (
            cache_miss_tokens * prices["input"]
            + cache_hit_tokens * prices.get("input_cache_hit", prices["input"])
        ) / 1_000_000
    else:
        input_cost = input_tokens * prices["input"] / 1_000_000
    output_cost = output_tokens * prices["output"] / 1_000_000
    return input_cost + output_cost


@dataclass
class OpenAIUsageStats:
    """Tracks cumulative usage across calls.

    Field names match ClaudeProvider.UsageStats for CLI merge compatibility.
    """

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0
    total_duration_ms: int = 0
    call_count: int = 0
    calls: list[dict] = field(default_factory=list)

    def record(self, usage, model: str, duration_ms: int):
        self.call_count += 1
        self.total_duration_ms += duration_ms

        input_tok = getattr(usage, "prompt_tokens", 0) if usage else 0
        output_tok = getattr(usage, "completion_tokens", 0) if usage else 0

        # DeepSeek cache fields via model_extra
        extra = getattr(usage, "model_extra", {}) or {}
        cache_hit = extra.get("prompt_cache_hit_tokens", 0)
        cache_miss = extra.get("prompt_cache_miss_tokens", 0)

        self.total_input_tokens += input_tok
        self.total_output_tokens += output_tok
        self.total_cache_read_tokens += cache_hit

        cost = _estimate_cost(model, input_tok, output_tok, cache_hit, cache_miss)
        self.total_cost_usd += cost

        self.calls.append({
            "model": model,
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "cache_read": cache_hit,
            "duration_ms": duration_ms,
            "cost_usd": cost,
        })
        logger.info(
            f"[{model}] {input_tok} in / {output_tok} out / "
            f"{cache_hit} cache_hit / {duration_ms}ms / ${cost:.4f}"
        )

    def summary(self) -> str:
        lines = [
            f"━━━ Usage Summary ({self.call_count} API calls) ━━━",
            f"  Input tokens:  {self.total_input_tokens:,}",
            f"  Output tokens: {self.total_output_tokens:,}",
            f"  Cache hits:    {self.total_cache_read_tokens:,}",
            f"  Total time:    {self.total_duration_ms / 1000:.1f}s",
            f"  Est. cost:     ${self.total_cost_usd:.4f}",
        ]
        return "\n".join(lines)


class OpenAIProvider(AIProvider):
    """Provider for OpenAI-compatible APIs (DeepSeek, OpenAI, Together, etc.)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        default_model: str = "deepseek-chat",
        proxy: str | None = None,
    ):
        http_client = None
        if proxy:
            http_client = httpx.AsyncClient(proxy=proxy)

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=http_client,
        )
        self.default_model = default_model
        self.usage = OpenAIUsageStats()

    async def generate_structured(
        self, model_id: str, system_prompt: str, input_data: str, output_schema: type
    ) -> BaseModel:
        """Generate structured output using OpenAI chat completions."""
        model = model_id or self.default_model
        schema = output_schema.model_json_schema()

        schema_instruction = (
            f"\n\nRESPOND WITH VALID JSON matching this schema:\n"
            f"```json\n{json.dumps(schema, indent=2)}\n```"
        )
        messages = [
            {"role": "system", "content": system_prompt + schema_instruction},
            {"role": "user", "content": input_data},
        ]

        start = time.monotonic()
        response = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        self.usage.record(response.usage, model, duration_ms)

        # Parse response
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError(f"Empty response from {model}")

        # Strip markdown fences if present
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0]

        return output_schema.model_validate_json(text.strip())
