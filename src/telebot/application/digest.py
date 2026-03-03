from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telebot.domain.models import ChannelDigest
from telebot.domain.services import ScraperInterface, SummarizerInterface


class GenerateDigestUseCase:
    def __init__(self, scraper: ScraperInterface, summarizer: SummarizerInterface):
        """Initialize with scraper and summarizer services."""
        self.scraper = scraper
        self.summarizer = summarizer

    async def execute(
        self,
        channel_id: str | int,
        topic_id: int | None = None,
        lookback_days: int = 1,
        timezone: str = "UTC",
        window_mode: str = "rolling",
        today_only: bool = False,
    ) -> ChannelDigest:
        """Execute the digest generation pipeline."""
        tz = ZoneInfo(timezone)
        now = datetime.now(tz)

        if today_only:
            # Mode: Start of today until now
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end_date = now
        elif window_mode == "fixed":
            # Mode: Fixed blocks relative to most recent 12 AM
            last_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end_date = last_midnight
            start_date = last_midnight - timedelta(days=lookback_days)
        else:
            # Default: Rolling last 24h
            start_date = now - timedelta(days=lookback_days)
            end_date = now

        messages = await self.scraper.get_messages(
            channel_id, start_date, end_date=end_date, topic_id=topic_id
        )

        if not messages:
            return None

        try:
            digest = await self.summarizer.summarize(messages, topic_id=topic_id)
            return digest
        except Exception as e:
            import logging

            logging.getLogger(__name__).error(f"Error during summarization: {e}")
            return ChannelDigest(
                channel_name="Error Notice",
                summaries=[f"Summarization Incomplete: {e}"],
                items=[],
                key_links=[],
                action_items=[],
            )
