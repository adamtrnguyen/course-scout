import datetime
import logging
import os

from telethon import TelegramClient

from telebot.domain.models import TelegramMessage
from telebot.domain.services import ScraperInterface

logger = logging.getLogger(__name__)


class TelethonScraper(ScraperInterface):
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session_path: str,
        phone: str | None = None,
        login_code: str | None = None,
    ):
        """Initialize the scraper with API credentials and session info."""
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path
        self.phone = phone
        self.login_code = login_code

    async def get_messages(
        self,
        channel_id: str | int,
        start_date: datetime.datetime,
        end_date: datetime.datetime | None = None,
        topic_id: int | None = None,
    ) -> list[TelegramMessage]:
        """Fetch messages from a channel/topic starting from a specific date."""
        # Prepare media directory
        media_dir = os.path.join(os.getcwd(), "media_cache")
        os.makedirs(media_dir, exist_ok=True)

        messages = []

        # Create client fresh for each call to avoid state issues
        from typing import Any

        client: Any = TelegramClient(self.session_path, self.api_id, self.api_hash)

        try:
            await client.connect()

            if not await client.is_user_authorized():

                def get_code() -> str:
                    return str(self.login_code) if self.login_code else input("Enter code: ")

                await client.start(
                    phone=self.phone,
                    code_callback=get_code,
                )

            logger.info(
                f"Fetching messages from {channel_id}, topic={topic_id}, since {start_date}"
            )

            async for message in client.iter_messages(
                channel_id, offset_date=start_date, reverse=True, reply_to=topic_id
            ):
                # Apply end_date filter if provided
                if end_date and message.date > end_date:
                    logger.debug(f"Reached end_date {end_date}. Stopping fetch.")
                    break

                if message.text or message.media:
                    telegram_msg = await self._process_message(
                        channel_id, message, topic_id, media_dir
                    )
                    messages.append(telegram_msg)
                    logger.debug(f"Fetched message ID {message.id}")

            logger.info(f"Fetched {len(messages)} messages from {channel_id}")
        finally:
            await client.disconnect()

        return messages

    async def get_message_by_id(
        self, channel_id: str | int, message_id: int, topic_id: int | None = None
    ) -> TelegramMessage | None:
        """Fetch a specific message by ID and verify it exists."""
        client = TelegramClient(self.session_path, self.api_id, self.api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.start(phone=self.phone, code_callback=lambda: self.login_code)

            # Telethon requires numeric IDs to be integers
            try:
                entity = int(channel_id)
            except ValueError:
                entity = channel_id

            message = await client.get_messages(entity, ids=[message_id])
            if message and message[0]:
                return await self._process_message(channel_id, message[0], topic_id)
            return None
        finally:
            await client.disconnect()

    async def search_messages(
        self, channel_id: str | int, query: str, topic_id: int | None = None, limit: int = 5
    ) -> list[TelegramMessage]:
        """Search for messages containing the given query string."""
        client = TelegramClient(self.session_path, self.api_id, self.api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.start(phone=self.phone, code_callback=lambda: self.login_code)

            try:
                entity = int(channel_id)
            except ValueError:
                entity = channel_id

            messages = []
            async for message in client.iter_messages(
                entity, search=query, limit=limit, reply_to=topic_id
            ):
                messages.append(await self._process_message(channel_id, message, topic_id))
            return messages
        finally:
            await client.disconnect()

    async def list_topics(self, channel_id: str | int) -> list[dict]:
        """List forum topics for a given channel."""
        from telethon import functions

        client = TelegramClient(self.session_path, self.api_id, self.api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.start(phone=self.phone, code_callback=lambda: self.login_code)

            try:
                entity = int(channel_id)
            except ValueError:
                entity = channel_id

            result = await client(
                functions.messages.GetForumTopicsRequest(
                    peer=entity, offset_date=None, offset_id=0, offset_topic=0, limit=100
                )  # type: ignore
            )
            return [{"id": t.id, "title": t.title} for t in result.topics]
        finally:
            await client.disconnect()

    def _format_message_link(self, cid: str | int, mid: int, topic_id: int | None = None) -> str:
        """Format private chat links correctly for forum-aware deep-linking."""
        cid_str = str(cid)
        topic_suffix = f"/{topic_id}" if topic_id else ""

        if cid_str.startswith("-100") and cid_str[4:].isdigit():
            # Private supergroup/channel format for deep-linking
            stripped_id = cid_str[4:]
            return f"https://t.me/c/{stripped_id}{topic_suffix}/{mid}"
        elif cid_str.startswith("-") and cid_str[1:].isdigit():
            # Basic group
            stripped_id = cid_str[1:]
            return f"https://t.me/c/{stripped_id}{topic_suffix}/{mid}"
        return f"https://t.me/{cid_str}/{mid}"

    async def _process_message(
        self, channel_id: str | int, message, topic_id: int | None, media_dir: str | None = None
    ) -> TelegramMessage:
        """Convert a Telethon message to our domain model."""
        forward_from_author = None
        if message.fwd_from and message.fwd_from.from_name:
            forward_from_author = message.fwd_from.from_name

        reply_to_id = message.reply_to.reply_to_msg_id if message.reply_to else None

        # Download media if present (only images)
        local_path = None
        if message.media and media_dir:
            is_image = False
            if hasattr(message, "photo") and message.photo:
                is_image = True
            elif hasattr(message, "document") and message.document:
                mime_type = getattr(message.document, "mime_type", "")
                if mime_type and mime_type.startswith("image/"):
                    is_image = True

            if is_image:
                try:
                    ext = message.file.ext or ".jpg"
                    filename = f"media_{message.id}{ext}"
                    full_path = os.path.join(media_dir, filename)
                    if os.path.exists(full_path):
                        local_path = full_path
                    else:
                        local_path = await message.download_media(file=full_path)
                except Exception as e:
                    logger.error(f"Failed to download image for message {message.id}: {e}")

        m_author = getattr(message.sender, "username", None)
        return TelegramMessage(
            id=message.id,
            text=message.text or "",
            date=message.date,
            author=m_author,
            link=self._format_message_link(channel_id, message.id, topic_id),
            reply_to_id=reply_to_id,
            forward_from_chat=None,
            forward_from_author=forward_from_author,
            local_media_path=local_path,
        )
