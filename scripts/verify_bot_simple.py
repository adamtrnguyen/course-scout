import asyncio
import logging
from telebot.infrastructure.config import load_settings
from telebot.infrastructure.notifier import TelethonNotifier

async def main():
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    
    print("🚀 Initializing TelethonNotifier with Bot Token...")
    notifier = TelethonNotifier(
        settings.tg_api_id,
        settings.tg_api_hash,
        settings.session_path,
        default_peer=settings.tg_notify_target,
        bot_token=settings.telegram_bot_token
    )
    
    print(f"Sending test notification to {settings.tg_notify_target} via Orion Bot...")
    success = await notifier.send_message("🤖 Hello from the Orion Bot! This is a test notification for the Telebot worker.")
    
    if success:
        print("✅ Bot notification sent successfully!")
    else:
        print("❌ Failed to send bot notification.")

if __name__ == "__main__":
    asyncio.run(main())
