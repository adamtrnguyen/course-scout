import datetime
import logging

from telebot.domain.models import ChannelDigest, TelegramMessage
from telebot.domain.services import ScraperInterface, SummarizerInterface
from telebot.infrastructure.agents import (
    AgentOrchestrator,
    StructuredMessage,
    SummarizerInputSchema,
    VerifierInputSchema,
)

logger = logging.getLogger(__name__)


class OrchestratedSummarizer(SummarizerInterface):
    """AISummarizer that handles multi-provider orchestration (Gemini/Groq)."""

    def __init__(
        self,
        gemini_key: str,
        groq_key: str | None = None,
        provider: str = "gemini",
        summarizer_model: str | None = None,
        verifier_model: str | None = None,
        scraper: ScraperInterface | None = None,
    ):
        """Initialize with API keys and preferred provider."""
        self.orchestrator = AgentOrchestrator(
            gemini_key=gemini_key,
            groq_key=groq_key,
            preferred_provider=provider,
            summarizer_model=summarizer_model,
            verifier_model=verifier_model,
        )
        self.scraper = scraper

    async def summarize(
        self, messages: list[TelegramMessage], topic_id: int | None = None
    ) -> ChannelDigest:
        """Summarize messages using a synchronous 3-agent pipeline.

        Even though this is an async method, it executes blocking synchronous agent calls.
        """
        try:
            # 1. Prepare Structured Input
            structured_messages = self._prepare_structured_input(messages)

            # 2. Map valid links for grounding
            link_map = {m.id: m.link for m in structured_messages if m.link}
            import re

            url_pattern = re.compile(r"https?://\S+")
            all_raw_urls = {
                url for m in structured_messages for url in url_pattern.findall(m.content)
            }

            digest_date = datetime.date.today()
            topic_title = f"Topic {topic_id}" if topic_id else "General Channel"

            # 3. Run Summarizer Agent
            summarizer_input = SummarizerInputSchema(
                messages=structured_messages,
                topic_context=f"Topic: {topic_title}, Date: {digest_date}",
                chat_message=(
                    "Extract courses, discussions, files, and requests. "
                    "Focus on grounding every item in a source message ID."
                ),
            )
            summarizer = self.orchestrator.get_summarizer_agent()
            draft_summary = summarizer.run(summarizer_input)

            # 4. Run Verifier Agent
            raw_msg_str = "\n".join(
                [f"[{m.id}] {m.author}: {m.content[:200]}" for m in structured_messages]
            )
            verifier_input = VerifierInputSchema(
                original_messages=raw_msg_str,
                summarizer_output=draft_summary,
                chat_message="Fix context errors and check logical consistency.",
            )
            verifier = self.orchestrator.get_verifier_agent()
            verified_data = verifier.run(verifier_input)

            # 5. Grounding & Repair
            grounded_links = await self._ground_links(
                verified_data.verified_links, link_map, all_raw_urls, messages, topic_id
            )
            self._ground_items(verified_data.verified_items, link_map, all_raw_urls)

            # 6. Map to ChannelDigest
            return ChannelDigest(
                channel_name=topic_title,
                date=digest_date,
                summaries=[verified_data.verified_summary],
                items=verified_data.verified_items,
                action_items=verified_data.verified_action_items,
                key_links=grounded_links,
            )

        except Exception as e:
            logger.error(f"Error during summarization: {e}", exc_info=True)
            return self._build_error_digest(messages)

    def _prepare_structured_input(self, messages: list[TelegramMessage]) -> list[StructuredMessage]:
        """Convert domain messages to structured agent input."""
        structured = []
        for m in messages:
            content = str(m.text) if m.text else "[Media/File]"
            if m.link:
                content += f" [Link: {m.link}]"
            structured.append(
                StructuredMessage(
                    id=m.id,
                    author=m.author or "Unknown",
                    content=content,
                    timestamp=str(m.date),
                    link=m.link,
                    reply_to_id=m.reply_to_id,
                    forward_from=m.forward_from_author,
                )
            )
        return structured

    async def _ground_links(self, links, link_map, raw_urls, messages, topic_id):
        """Verify and repair key links in the digest."""
        import re

        grounded = []
        for link in links:
            msg_id_match = re.search(r"/(\d+)$", link.url)
            msg_id = int(msg_id_match.group(1)) if msg_id_match else None
            if link.url in link_map.values() or link.url in raw_urls:
                grounded.append(link)
            elif msg_id and self.scraper:
                repaired_link = await self._repair_link(msg_id, messages, topic_id)
                if repaired_link:
                    link.url = repaired_link
                    grounded.append(link)
        return grounded

    def _ground_items(self, items, link_map, raw_urls):
        """Filter hallucinated links in items."""
        for item in items:
            item.links = [
                link for link in item.links if link in link_map.values() or link in raw_urls
            ]

    async def _repair_link(self, msg_id, messages, topic_id):
        """Attempt active repair of a missing link via Telegram scraper."""
        batch_cid = None
        if messages and "/c/" in messages[0].link:
            batch_cid = messages[0].link.split("/")[4]

        if batch_cid and self.scraper:
            full_cid = f"-100{batch_cid}" if not batch_cid.startswith("-") else batch_cid
            fetched = await self.scraper.get_message_by_id(full_cid, msg_id, topic_id=topic_id)
            return fetched.link if fetched else None
        return None

    def _build_error_digest(self, messages):
        """Create a placeholder digest for graceful failure handling."""
        return ChannelDigest(
            channel_name="Error Notice",
            date=datetime.date.today(),
            summaries=[
                "### ⚠️ Summarization Incomplete",
                "We encountered an issue while processing the messages for this digest "
                "(likely a service rate limit or connection timeout).",
                "Please check the system logs for technical details.",
            ],
        )
