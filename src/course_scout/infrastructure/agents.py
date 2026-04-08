"""3-Agent Synchronous Architecture for Course Scout.

Agents:
- SummarizerAgent: Takes raw messages, produces a structured digest.
- VerifierAgent: Cross-references summary with original messages.
"""

import json
import logging
import time
from enum import Enum

from pydantic import BaseModel, Field, model_validator

from course_scout.domain.models import (
    AnnouncementItem,
    CourseItem,
    DiscussionItem,
    FileItem,
    LinkItem,
    RequestItem,
)
from course_scout.domain.services import AIProvider
from course_scout.infrastructure.providers.claude_provider import ClaudeProvider
from course_scout.infrastructure.providers.openai_provider import OpenAIProvider

logger = logging.getLogger(__name__)


class ClaudeModel(str, Enum):
    """Model identifiers for the Anthropic Claude SDK."""

    SONNET = "claude-sonnet-4-6"
    HAIKU = "claude-haiku-4-5"
    OPUS = "claude-opus-4-6"


# --- Schemas ---


class StructuredMessage(BaseModel):
    """A single message from the chat."""

    id: int = Field(..., description="Unique message ID")
    author: str | None = Field(None, description="Username/Name of sender")
    content: str = Field(..., description="Message text content")
    timestamp: str = Field(..., description="ISO formatted timestamp")
    link: str | None = Field(None, description="Direct link to the message")
    reply_to_id: int | None = Field(None, description="ID of message being replied to")
    forward_from: str | None = Field(None, description="Original author if forwarded")


class SummarizerInputSchema(BaseModel):
    """Input for the Summarizer Agent."""

    messages: list[StructuredMessage] = Field(..., description="List of messages to process")
    topic_context: str = Field(..., description="Topic ID and title for context")
    chat_message: str = Field(
        default="Summarize these messages into a digest.", description="Instruction"
    )


class RawDigestItem(BaseModel):
    """Flat schema the LLM produces. Converted to discriminated types post-parse."""

    title: str = Field(..., description="Exact course/file/topic name.")
    description: str = Field(
        ...,
        description=(
            "Telegraphic notes — key facts only, no narrative filler. "
            "Just: what it is, what's useful, actionable details."
        ),
    )
    category: str = Field(
        ..., description="One of: course, file, discussion, request, announcement"
    )
    msg_ids: list[int] = Field(default_factory=list, description="Source message IDs")
    links: list[str] = Field(default_factory=list, description="Related URLs")
    author: str | None = Field(None, description="Who posted it (Telegram username)")
    instructor: str | None = Field(None, description="Course instructor or artist name")
    platform: str | None = Field(None, description="Coloso, Baidu Pan, Proko, etc.")
    status: str | None = Field(None, description="FULFILLED, UNFULFILLED, or DISCUSSING")
    priority: str | None = Field(
        None,
        description=(
            "HIGH = downloadable course/file with link. "
            "MEDIUM = review, technique discussion, or fulfilled request. "
            "LOW = unfulfilled request, off-topic, or 3D/photo/UI."
        ),
    )
    password: str | None = Field(None, description="Download password, preserved exactly")

    def to_domain(self) -> CourseItem | FileItem | DiscussionItem | RequestItem | AnnouncementItem:
        """Convert flat LLM output to the correct discriminated domain type."""
        shared = dict(
            title=self.title,
            description=self.description,
            msg_ids=self.msg_ids,
            links=self.links,
            author=self.author,
            instructor=self.instructor,
            priority=self.priority,
        )
        actionable = dict(
            **shared,
            platform=self.platform,
            status=self.status,
            password=self.password,
        )
        type_map = {
            "course": (CourseItem, actionable),
            "file": (FileItem, actionable),
            "request": (RequestItem, actionable),
            "announcement": (AnnouncementItem, actionable),
            "discussion": (DiscussionItem, shared),
        }
        cls, fields = type_map.get(self.category, (CourseItem, actionable))
        return cls(**fields)


class SummarizerOutputSchema(BaseModel):
    """Output from the Summarizer Agent. Uses flat items for LLM compatibility."""

    items: list[RawDigestItem] = Field(default_factory=list, description="Extracted items")
    key_links: list[LinkItem] = Field(default_factory=list, description="Important URLs mentioned")

    @model_validator(mode="before")
    @classmethod
    def parse_json_string_fields(cls, data):
        """Handle Claude SDK returning list fields as JSON strings instead of parsed lists."""
        if isinstance(data, dict):
            for key in ("items", "key_links"):
                if key in data and isinstance(data[key], str):
                    data[key] = json.loads(data[key])
        return data

    def to_domain_items(self) -> list:
        """Convert raw LLM items to discriminated domain types."""
        return [item.to_domain() for item in self.items]



# --- Synchronous Rate Limiter ---


class RateLimiter:
    """Synchronous Rate Limiter for AI API calls.

    Enforces a maximum number of requests per minute (RPM).
    """

    def __init__(self, rpm: int = 10):
        """Initialize with RPM limit."""
        self.rpm = rpm
        self.interval = 60.0 / rpm
        self.last_request_time = 0.0

    def acquire(self):
        """Wait if necessary to comply with the rate limit."""
        current_time = time.time()
        elapsed = current_time - self.last_request_time

        if elapsed < self.interval:
            wait_time = self.interval - elapsed
            logger.debug(f"Rate limit: waiting {wait_time:.2f}s")
            time.sleep(wait_time)

        self.last_request_time = time.time()


# --- Generic AI Agent ---


class AIAgent:
    """Provider-agnostic agent that uses an AIProvider for execution."""

    def __init__(
        self,
        provider: AIProvider,
        models: list[str],
        system_prompt: str,
        output_schema: type[BaseModel],
        rate_limiter: RateLimiter,
    ):
        """Initialize with provider, models, prompt, and schema."""
        self.provider = provider
        self.models = models
        self.system_prompt = system_prompt
        self.output_schema = output_schema
        self.rate_limiter = rate_limiter

    async def run(self, input_data: BaseModel) -> BaseModel:
        """Execute the agent using the injected provider with fallback support."""
        last_error = None

        for model in self.models:
            retries = 0
            max_retries = 3

            while retries < max_retries:
                try:
                    self.rate_limiter.acquire()
                    logger.info(f"Agent {model} starting request (Attempt {retries + 1})...")

                    logger.debug(f"Agent {model} input data: {input_data.model_dump_json()}")

                    result = await self.provider.generate_structured(
                        model_id=model,
                        system_prompt=self.system_prompt,
                        input_data=input_data.model_dump_json(),
                        output_schema=self.output_schema,
                    )

                    logger.debug(f"Agent {model} raw result: {result}")
                    logger.info(f"Agent {model} request completed.")
                    return result

                except Exception as e:
                    last_error = e
                    error_str = str(e).upper()
                    if "RATE" in error_str or "429" in error_str:
                        retries += 1
                        wait_time = 65.0
                        logger.warning(
                            f"Rate limit hit for {model}. Sleeping {wait_time}s "
                            f"before retry {retries}/{max_retries}..."
                        )
                        time.sleep(wait_time)
                    else:
                        logger.error(f"Error in agent {model}: {e}")
                        break

            logger.warning(f"Model {model} failed. Trying next model in list if available...")

        raise Exception(f"All models failed. Last error: {last_error}")


# --- Agent Orchestrator ---


class AgentOrchestrator:
    """Manages the Claude provider and agents."""

    DEFAULT_PROMPT = """You extract structured data from art community Telegram messages.

STYLE: Telegraphic notes. No narrative filler. No "A member asked...", "It was noted that...".
Just facts: what it is, what's useful, actionable details.

ITEM TYPES (set category to pick the right schema):

  course — A course recommendation, review, or shared course.
    Fields: instructor, platform, status, priority, password (if download).
    Priority: HIGH if downloadable with link. MEDIUM if review/recommendation. LOW if just a mention.

  file — A shared file, archive, or download link.
    Fields: instructor, platform, status, priority, password.
    Priority: HIGH if working download link. MEDIUM if partial/needs re-upload. LOW if broken/inaccessible.

  discussion — A technique discussion, debate, or tool comparison.
    Fields: instructor (if about a specific artist/method), priority.
    No platform/status/password — these are conversations, not resources.
    Priority: MEDIUM if concrete technique tips or verdicts. LOW if vague chat.

  request — Someone asking for a course/resource (no download shared).
    Fields: instructor, platform, status (FULFILLED/UNFULFILLED/DISCUSSING), priority.
    Priority: LOW for unfulfilled requests (no actionable value). MEDIUM if fulfilled with link.

  announcement — Community news, event, or moderation notice.
    Fields: instructor (optional), priority.
    Priority: MEDIUM if relevant event. LOW if housekeeping.

INTEREST FILTER:
Prioritize: 2D illustration, character design, concept art, anatomy, figure drawing,
color theory, lighting, rendering, Asian artists, anime/manga, digital painting.
De-prioritize: 3D, game dev, photography, UI/UX, motion graphics.

DESCRIPTION: 1-3 short lines of key facts. Put structured data in the typed fields,
not in the description. Description = what you can't express in the fields.

RULES:
- Every item MUST include msg_ids from the input
- Preserve download passwords exactly as written
- Translate non-English content to English, keep original names/titles
- Group related messages (reply chains) into single items
- Do not hallucinate links not present in the input
- Return valid JSON matching the schema."""

    # Models that route to OpenAI-compatible providers
    _OPENAI_PROVIDERS = {
        "deepseek-chat": {
            "base_url": "https://api.deepseek.com",
            "env_key": "DEEPSEEK_API_KEY",
        },
        "deepseek-reasoner": {
            "base_url": "https://api.deepseek.com",
            "env_key": "DEEPSEEK_API_KEY",
        },
    }

    def __init__(
        self,
        summarizer_model: str | None = None,
        system_prompt: str | None = None,
        thinking: str = "adaptive",
        effort: str = "medium",
    ):
        """Initialize with auto-detected provider based on model name."""
        self.thinking = thinking
        self.effort = effort
        self.custom_prompt = system_prompt

        self.summarizer_models = summarizer_model or [ClaudeModel.SONNET]
        if isinstance(self.summarizer_models, str):
            self.summarizer_models = [self.summarizer_models]

        self.rate_limiter = RateLimiter(rpm=50)

        # Cache providers — created lazily per model
        self._providers: dict[str, AIProvider] = {}

    def _get_provider(self, model: str) -> AIProvider:
        """Get or create the right provider for a model."""
        if model in self._providers:
            return self._providers[model]

        if model in self._OPENAI_PROVIDERS:
            import os
            cfg = self._OPENAI_PROVIDERS[model]
            api_key = os.environ.get(cfg["env_key"], "")
            import os as _os
            proxy = _os.environ.get("DEEPSEEK_PROXY")
            provider = OpenAIProvider(
                api_key=api_key,
                base_url=cfg["base_url"],
                default_model=model,
                proxy=proxy,
            )
        else:
            # Default: Claude Agent SDK
            provider = ClaudeProvider(thinking=self.thinking, effort=self.effort)

        self._providers[model] = provider
        return provider

    def _get_agent(
        self, models: list[str], system_prompt: str, output_schema: type[BaseModel]
    ) -> AIAgent:
        # Use the first model's provider
        provider = self._get_provider(models[0])
        return AIAgent(
            provider,
            models,
            system_prompt,
            output_schema,
            self.rate_limiter,
        )

    def get_summarizer_agent(self) -> AIAgent:
        prompt = self.custom_prompt or self.DEFAULT_PROMPT
        return self._get_agent(
            self.summarizer_models,
            prompt,
            SummarizerOutputSchema,
        )

