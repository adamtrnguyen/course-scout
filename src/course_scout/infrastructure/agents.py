"""3-Agent Synchronous Architecture for Course Scout.

Agents:
- SummarizerAgent: Takes raw messages, produces a structured digest.
- VerifierAgent: Cross-references summary with original messages.
"""

import logging
import time
from enum import Enum

from pydantic import BaseModel, Field

from course_scout.domain.models import DigestItem, LinkItem
from course_scout.domain.services import AIProvider
from course_scout.infrastructure.providers.claude_provider import ClaudeProvider

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


class SummarizerOutputSchema(BaseModel):
    """Output from the Summarizer Agent."""

    items: list[DigestItem] = Field(default_factory=list, description="Extracted items")
    key_links: list[LinkItem] = Field(default_factory=list, description="Important URLs mentioned")



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

    DEFAULT_PROMPT = """You are a Telegram Chat Summarizer for art community channels.

EXTRACT from messages:
1. Digest items — categorize each as: course, file, discussion, request, or announcement
2. Key links — URLs shared in messages (course pages, downloads, references)

INTEREST FILTER — prioritize:
- 2D illustration, character design, concept art
- Anatomy, figure drawing, gesture drawing
- Color theory, lighting, rendering techniques
- Asian artists, anime/manga art styles
- Digital painting workflows (Photoshop, CSP, Procreate)
- Art courses (Coloso, Schoolism, CGMA, Domestika, Class 101, Wingfox, etc.)

De-prioritize (still include, but mark as low priority):
- 3D modeling, game dev, photography, UI/UX, motion graphics

RULES:
- Every item MUST reference a specific message ID from the input
- Preserve download passwords (Baidu Pan, Quark Pan, etc.) exactly as written
- Translate non-English content to English but preserve original names/titles
- Group related messages (reply chains) into single items
- For requests: note if fulfilled or unfulfilled in this batch
- Do not hallucinate links not present in the input
- Return valid JSON matching the schema."""

    def __init__(
        self,
        summarizer_model: str | None = None,
        system_prompt: str | None = None,
        thinking: str = "adaptive",
        effort: str = "medium",
    ):
        """Initialize with Claude Agent SDK (auth handled automatically)."""
        self.provider = ClaudeProvider(thinking=thinking, effort=effort)
        self.custom_prompt = system_prompt

        self.summarizer_models = summarizer_model or [ClaudeModel.SONNET]
        if isinstance(self.summarizer_models, str):
            self.summarizer_models = [self.summarizer_models]

        self.rate_limiter = RateLimiter(rpm=50)

    def _get_agent(
        self, models: list[str], system_prompt: str, output_schema: type[BaseModel]
    ) -> AIAgent:
        return AIAgent(
            self.provider,
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

