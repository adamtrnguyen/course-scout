import datetime
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from course_scout.domain.models import (
    ChannelDigest,
    CourseItem,
    DiscussionItem,
    FileItem,
    LinkItem,
    RequestItem,
    TelegramMessage,
)
from course_scout.infrastructure.agents import RawDigestItem, SummarizerOutputSchema
from course_scout.infrastructure.summarization import OrchestratedSummarizer as Summarizer


class TestSummarizer(unittest.IsolatedAsyncioTestCase):
    @patch("course_scout.infrastructure.summarization.AgentOrchestrator")
    async def test_summarize_success(self, MockOrch):
        mock_orch = MockOrch.return_value
        summarizer = Summarizer()

        mock_summarizer_agent = MagicMock()
        mock_orch.get_summarizer_agent.return_value = mock_summarizer_agent

        sum_out = SummarizerOutputSchema(
            items=[
                RawDigestItem(
                    title="Item 1", description="Desc", category="course",
                    links=["http://l1"], msg_ids=[1],
                )
            ],
            key_links=[LinkItem(title="T1", url="http://l1")],
        )
        mock_summarizer_agent.run = AsyncMock(return_value=sum_out)

        messages = [TelegramMessage(id=1, text="msg1", date=datetime.datetime.now(), link="http://l1")]
        digest = await summarizer.summarize(messages, topic_id=123)

        self.assertIsInstance(digest, ChannelDigest)
        self.assertEqual(digest.channel_name, "Topic 123")
        self.assertEqual(digest.key_links[0].title, "T1")

    @patch("course_scout.infrastructure.summarization.AgentOrchestrator")
    async def test_summarize_error_suppression(self, MockOrch):
        mock_orch = MockOrch.return_value
        mock_orch.get_summarizer_agent.side_effect = Exception("AI Overload")
        summarizer = Summarizer()
        messages = [TelegramMessage(id=1, text="msg1", date=datetime.datetime.now(), link="http://l1")]

        digest = await summarizer.summarize(messages, topic_id=123)

        self.assertEqual(digest.channel_name, "Error Notice")
        self.assertTrue(any("Summarization Incomplete" in s for s in digest.summaries))

    @patch("course_scout.infrastructure.summarization.AgentOrchestrator")
    async def test_summarize_all_sections(self, MockOrch):
        mock_orch = MockOrch.return_value
        summarizer = Summarizer()

        mock_summarizer_agent = MagicMock()
        mock_orch.get_summarizer_agent.return_value = mock_summarizer_agent

        sum_out = SummarizerOutputSchema(
            items=[
                RawDigestItem(title="F1", description="File", category="file", links=["http://f"]),
                RawDigestItem(title="D1", description="Disc", category="discussion"),
                RawDigestItem(title="R1", description="Req", category="request", links=["http://r"]),
            ],
            key_links=[],
        )
        mock_summarizer_agent.run = AsyncMock(return_value=sum_out)

        messages = [TelegramMessage(id=1, text="m", date=datetime.datetime.now(), link="http://f")]
        digest = await summarizer.summarize(messages)

        full_md = digest.to_markdown()
        self.assertIn("## 📂 Files Shared", full_md)
        self.assertIn("## 🗣 Discussions", full_md)
        self.assertIn("## 🙋 Requests", full_md)

    @patch("course_scout.infrastructure.summarization.AgentOrchestrator")
    async def test_domain_type_conversion(self, MockOrch):
        """Verify RawDigestItem → discriminated domain types."""
        mock_orch = MockOrch.return_value
        summarizer = Summarizer()

        mock_agent = MagicMock()
        mock_orch.get_summarizer_agent.return_value = mock_agent

        sum_out = SummarizerOutputSchema(
            items=[
                RawDigestItem(title="C1", description="Course", category="course", instructor="Krenz"),
                RawDigestItem(title="F1", description="File", category="file", password="abc"),
                RawDigestItem(title="D1", description="Discussion", category="discussion"),
                RawDigestItem(title="R1", description="Request", category="request", status="UNFULFILLED"),
            ],
            key_links=[],
        )
        mock_agent.run = AsyncMock(return_value=sum_out)

        messages = [TelegramMessage(id=1, text="m", date=datetime.datetime.now(), link="http://x")]
        digest = await summarizer.summarize(messages)

        types = [type(i) for i in digest.items]
        self.assertIn(CourseItem, types)
        self.assertIn(FileItem, types)
        self.assertIn(DiscussionItem, types)
        self.assertIn(RequestItem, types)

    @patch("course_scout.infrastructure.summarization.AgentOrchestrator")
    async def test_link_backfill(self, MockOrch):
        """Verify msg_ids get backfilled as t.me links."""
        mock_orch = MockOrch.return_value
        summarizer = Summarizer()

        mock_agent = MagicMock()
        mock_orch.get_summarizer_agent.return_value = mock_agent

        sum_out = SummarizerOutputSchema(
            items=[
                RawDigestItem(
                    title="Discussion", description="Talk", category="discussion",
                    msg_ids=[100, 101], links=[],  # No links from LLM
                ),
            ],
            key_links=[],
        )
        mock_agent.run = AsyncMock(return_value=sum_out)

        messages = [
            TelegramMessage(id=100, text="msg1", date=datetime.datetime.now(), link="https://t.me/c/123/456/100"),
            TelegramMessage(id=101, text="msg2", date=datetime.datetime.now(), link="https://t.me/c/123/456/101"),
        ]
        digest = await summarizer.summarize(messages)

        disc_item = digest.items[0]
        self.assertEqual(len(disc_item.links), 2)
        self.assertIn("https://t.me/c/123/456/100", disc_item.links)
        self.assertIn("https://t.me/c/123/456/101", disc_item.links)

    def test_no_link_duplication_in_content(self):
        """Verify message links are NOT appended to content."""
        summarizer = Summarizer()
        messages = [
            TelegramMessage(id=1, text="Hello", date=datetime.datetime.now(), link="https://t.me/c/123/1"),
        ]
        structured = summarizer._prepare_structured_input(messages)
        self.assertEqual(structured[0].content, "Hello")
        self.assertNotIn("[Link:", structured[0].content)


class TestGrounding(unittest.TestCase):
    def test_ground_items_keeps_external_urls(self):
        item = CourseItem(
            title="Test", description="x",
            links=["https://coloso.global/en/products/test"],
        )
        Summarizer._ground_items([item], link_map={}, raw_urls=set())
        self.assertEqual(len(item.links), 1)

    def test_ground_items_keeps_valid_tme_links(self):
        item = FileItem(
            title="Test", description="x",
            msg_ids=[100],
            links=["https://t.me/c/123/456/100"],
        )
        Summarizer._ground_items([item], link_map={100: "https://t.me/c/123/456/100"}, raw_urls=set())
        self.assertEqual(len(item.links), 1)

    def test_ground_items_strips_hallucinated_tme_links(self):
        item = CourseItem(
            title="Test", description="x",
            msg_ids=[100],
            links=["https://t.me/c/123/456/999999"],  # msg 999999 doesn't exist
        )
        Summarizer._ground_items([item], link_map={100: "https://t.me/c/123/456/100"}, raw_urls=set())
        self.assertEqual(len(item.links), 0)

    def test_backfill_links_adds_missing_tme(self):
        item = DiscussionItem(
            title="Test", description="x",
            msg_ids=[100, 101], links=[],
        )
        link_map = {
            100: "https://t.me/c/123/456/100",
            101: "https://t.me/c/123/456/101",
        }
        Summarizer._backfill_links([item], link_map)
        self.assertEqual(len(item.links), 2)

    def test_backfill_links_no_duplicates(self):
        item = DiscussionItem(
            title="Test", description="x",
            msg_ids=[100],
            links=["https://t.me/c/123/456/100"],  # Already present
        )
        link_map = {100: "https://t.me/c/123/456/100"}
        Summarizer._backfill_links([item], link_map)
        self.assertEqual(len(item.links), 1)  # Not duplicated
