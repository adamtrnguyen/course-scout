import asyncio
import datetime
import logging
import re

from course_scout.domain.models import ChannelDigest, TelegramMessage
from course_scout.domain.services import ScraperInterface, SummarizerInterface
from course_scout.infrastructure.agents import (
    AgentOrchestrator,
    StructuredMessage,
    SummarizerInputSchema,
    SummarizerOutputSchema,
)

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 25


class OrchestratedSummarizer(SummarizerInterface):
    """AISummarizer using Claude with chunked pipeline."""

    def __init__(
        self,
        summarizer_model: str | None = None,
        system_prompt: str | None = None,
        thinking: str = "adaptive",
        effort: str = "medium",
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        scraper: ScraperInterface | None = None,
    ):
        """Initialize with per-topic agent configuration."""
        self.orchestrator = AgentOrchestrator(
            summarizer_model=summarizer_model,
            system_prompt=system_prompt,
            thinking=thinking,
            effort=effort,
        )
        self.chunk_size = chunk_size
        self.scraper = scraper

    async def summarize(
        self, messages: list[TelegramMessage], topic_id: int | None = None
    ) -> ChannelDigest:
        """Summarize messages using chunked pipeline.

        1. Chunk messages into batches of CHUNK_SIZE
        2. Summarize each chunk in parallel
        3. Merge chunk summaries
        4. Verify merged result
        5. Ground links
        """
        try:
            structured_messages = self._prepare_structured_input(messages)
            link_map = {m.id: m.link for m in structured_messages if m.link}
            url_pattern = re.compile(r"https?://\S+")
            all_raw_urls = {
                url for m in structured_messages for url in url_pattern.findall(m.content)
            }

            digest_date = datetime.date.today()
            topic_title = f"Topic {topic_id}" if topic_id else "General Channel"

            # Chunk and summarize
            chunks = self._chunk_messages(structured_messages)
            logger.info(f"Chunked {len(structured_messages)} messages into {len(chunks)} batches")

            if len(chunks) == 1:
                # Single chunk — no merge needed
                draft = await self._summarize_chunk(chunks[0], topic_title, digest_date)
            else:
                # Multiple chunks — summarize in parallel, then merge
                chunk_summaries = await asyncio.gather(
                    *[self._summarize_chunk(c, topic_title, digest_date) for c in chunks]
                )
                draft = self._merge_summaries(chunk_summaries)

            # Programmatic grounding (replaces LLM verifier)
            grounded_links = await self._ground_links(
                draft.key_links, link_map, all_raw_urls, messages, topic_id
            )
            self._ground_items(draft.items, link_map, all_raw_urls)

            return ChannelDigest(
                channel_name=topic_title,
                date=digest_date,
                summaries=[],
                items=draft.items,
                key_links=grounded_links,
            )

        except Exception as e:
            logger.error(f"Error during summarization: {e}", exc_info=True)
            return self._build_error_digest()

    def _chunk_messages(self, messages: list[StructuredMessage]) -> list[list[StructuredMessage]]:
        """Split messages into chunks of chunk_size."""
        cs = self.chunk_size
        return [messages[i : i + cs] for i in range(0, len(messages), cs)]

    async def _summarize_chunk(
        self, chunk: list[StructuredMessage], topic_title: str, digest_date: datetime.date
    ) -> SummarizerOutputSchema:
        """Summarize a single chunk of messages."""
        summarizer_input = SummarizerInputSchema(
            messages=chunk,
            topic_context=f"Topic: {topic_title}, Date: {digest_date}",
            chat_message=(
                "Extract courses, discussions, files, and requests. "
                "Focus on grounding every item in a source message ID."
            ),
        )
        summarizer = self.orchestrator.get_summarizer_agent()
        return await summarizer.run(summarizer_input)

    @staticmethod
    def _merge_summaries(summaries: list[SummarizerOutputSchema]) -> SummarizerOutputSchema:
        """Merge multiple chunk summaries into one."""
        merged_items = []
        merged_links = []
        for s in summaries:
            merged_items.extend(s.items)
            merged_links.extend(s.key_links)
        return SummarizerOutputSchema(
            items=merged_items,
            key_links=merged_links,
        )

    def _prepare_structured_input(
        self, messages: list[TelegramMessage]
    ) -> list[StructuredMessage]:
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

    @staticmethod
    def _ground_items(items, link_map, raw_urls):
        """Filter hallucinated links in items."""
        for item in items:
            item.links = [
                link for link in item.links if link in link_map.values() or link in raw_urls
            ]

    async def _repair_link(self, msg_id, messages, topic_id):
        """Attempt active repair of a missing link via Telegram scraper."""
        if msg_id > 2_147_483_647 or msg_id < 0:
            logger.warning(f"Dropping hallucinated message ID: {msg_id}")
            return None

        batch_cid = None
        if messages and "/c/" in messages[0].link:
            batch_cid = messages[0].link.split("/")[4]

        if batch_cid and self.scraper:
            full_cid = f"-100{batch_cid}" if not batch_cid.startswith("-") else batch_cid
            try:
                fetched = await self.scraper.get_message_by_id(
                    full_cid, msg_id, topic_id=topic_id
                )
                return fetched.link if fetched else None
            except Exception as e:
                logger.warning(f"Link repair failed for msg {msg_id}: {e}")
                return None
        return None

    @staticmethod
    def _build_error_digest():
        """Create a placeholder digest for graceful failure handling."""
        return ChannelDigest(
            channel_name="Error Notice",
            date=datetime.date.today(),
            summaries=[
                "### Summarization Incomplete",
                "We encountered an issue while processing the messages for this digest "
                "(likely a service rate limit or connection timeout).",
                "Please check the system logs for technical details.",
            ],
        )
