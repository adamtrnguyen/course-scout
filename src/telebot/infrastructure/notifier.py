import logging
import os

from telethon import TelegramClient

from telebot.domain.services import NotifierInterface

logger = logging.getLogger(__name__)


class TelethonNotifier(NotifierInterface):
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session_path: str,
        default_peer: str | None = None,
        bot_token: str | None = None,
    ):
        """Initialize with Telethon credentials, optional default peer, and optional bot token."""
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path
        self.default_peer = default_peer
        self.bot_token = bot_token

    async def _send(self, action_fn):
        # Use a unique session file for bot if provided
        session = self.session_path
        if self.bot_token:
            session = f"bot_{self.session_path}"

        client = TelegramClient(session, self.api_id, self.api_hash)
        await client.connect()
        try:
            if self.bot_token and not await client.is_user_authorized():
                await client.start(bot_token=self.bot_token)
            return await action_fn(client)
        except Exception as e:
            logger.error(f"Telethon notification error: {e}")
            return False
        finally:
            await client.disconnect()

    async def send_message(self, text: str, chat_id: str | None = None) -> bool:
        target = chat_id or self.default_peer
        if not target:
            logger.error("No target peer provided for Telethon notification.")
            return False

        async def action(client):
            await client.send_message(target, text)
            logger.info(f"Successfully sent Telethon message to {target}")
            return True

        return await self._send(action)

    async def send_document(
        self, file_path: str, caption: str | None = None, chat_id: str | None = None
    ) -> bool:
        target = chat_id or self.default_peer
        if not target:
            logger.error("No target peer provided for Telethon notification.")
            return False

        async def action(client):
            if not os.path.exists(file_path):
                logger.error(f"File not found: {file_path}")
                return False
            await client.send_file(target, file_path, caption=caption)
            logger.info(f"Successfully sent Telethon document {file_path} to {target}")
            return True

        return await self._send(action)
