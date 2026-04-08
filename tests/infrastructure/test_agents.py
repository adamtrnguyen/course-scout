import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from course_scout.infrastructure.agents import (
    AgentOrchestrator,
    AIAgent,
    ClaudeModel,
    RateLimiter,
    SummarizerOutputSchema,
)


class TestAgentOrchestrator(unittest.TestCase):
    def setUp(self):
        with patch("course_scout.infrastructure.agents.ClaudeProvider"):
            self.orch = AgentOrchestrator()

    def test_get_summarizer_agent(self):
        summarizer = self.orch.get_summarizer_agent()
        self.assertIsInstance(summarizer, AIAgent)
        self.assertEqual(summarizer.models, [ClaudeModel.SONNET])

    def test_rate_limiter_rpm(self):
        self.assertEqual(self.orch.rate_limiter.rpm, 50)


class TestAIAgent(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_provider = MagicMock()
        self.mock_provider.generate_structured = AsyncMock()
        self.rate_limiter = MagicMock(spec=RateLimiter)
        self.agent = AIAgent(
            self.mock_provider,
            ["claude-sonnet-4-6"],
            "System Prompt",
            SummarizerOutputSchema,
            self.rate_limiter,
        )

    async def test_run_success(self):
        mock_output = SummarizerOutputSchema(items=[], key_links=[])
        self.mock_provider.generate_structured.return_value = mock_output

        input_data = MagicMock()
        input_data.model_dump_json.return_value = "{}"

        result = await self.agent.run(input_data)

        self.assertEqual(result, mock_output)
        self.rate_limiter.acquire.assert_called_once()
        self.mock_provider.generate_structured.assert_called_once()

    @patch("time.sleep")
    async def test_run_rate_limit_retry(self, mock_sleep):
        mock_output = SummarizerOutputSchema(items=[], key_links=[])

        self.mock_provider.generate_structured.side_effect = [
            Exception("429 RATE limit exceeded"),
            mock_output,
        ]

        input_data = MagicMock()
        input_data.model_dump_json.return_value = "{}"

        result = await self.agent.run(input_data)

        self.assertEqual(result, mock_output)
        self.assertEqual(self.mock_provider.generate_structured.call_count, 2)
        mock_sleep.assert_called_with(65.0)
