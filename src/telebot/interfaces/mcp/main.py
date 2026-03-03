import datetime
import logging

from mcp.server.fastmcp import FastMCP

from telebot.application.digest import GenerateDigestUseCase
from telebot.infrastructure.config import load_settings
from telebot.infrastructure.logging_config import setup_logging
from telebot.infrastructure.reporting import PDFRenderer
from telebot.infrastructure.summarization import OrchestratedSummarizer
from telebot.infrastructure.telegram import TelethonScraper

# Configure logging
setup_logging()
logger = logging.getLogger(__name__)


# Settings
# Using central Settings


try:
    settings = load_settings()
except Exception as e:
    logger.error(f"Configuration error: {e}")
    raise

# Initialize MCP Server
mcp = FastMCP("Telebot Summarizer")


def get_use_case(provider: str = "gemini"):
    scraper = TelethonScraper(
        api_id=settings.tg_api_id,
        api_hash=settings.tg_api_hash,
        session_path=settings.session_path,
        phone=settings.phone_number,
        login_code=settings.login_code,
    )
    summarizer = OrchestratedSummarizer(
        gemini_key=settings.gemini_api_key,
        groq_key=settings.groq_api_key,
        provider=provider,
        summarizer_model=settings.summarizer_model,
        verifier_model=settings.verifier_model,
        scraper=scraper,
    )
    return GenerateDigestUseCase(scraper, summarizer)


@mcp.tool()
async def list_topics(channel_id: str) -> str:
    """List available forum topics in a Telegram channel.

    Args:
        channel_id: The Telegram channel ID (e.g., '@channel_name' or '-100...').

    """
    try:
        scraper = TelethonScraper(
            api_id=settings.tg_api_id,
            api_hash=settings.tg_api_hash,
            session_path=settings.session_path,
            phone=settings.phone_number,
            login_code=settings.login_code,
        )
        topics = await scraper.list_topics(channel_id)
        if not topics:
            return "No topics found or channel is not a forum."

        lines = [f"ID: {t['id']} | Title: {t['title']}" for t in topics]
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing topics: {str(e)}"


@mcp.tool()
async def generate_digest(
    channel_id: str,
    topic_id: int | None = None,
    lookback_days: int = 1,
    provider: str = "gemini",
    pdf: bool = True,
) -> str:
    """Generate a summary digest of recent messages in a Telegram channel.

    Args:
        channel_id: The Telegram channel ID.
        topic_id: Optional forum topic ID.
        lookback_days: Number of days to look back for messages (default 1).
        provider: AI provider to use ('gemini' or 'groq').
        pdf: Whether to also generate a PDF report.

    """
    try:
        use_case = get_use_case(provider=provider)

        # Handle numeric IDs for channel_id
        try:
            if channel_id.startswith("-") and channel_id[1:].isdigit():
                peer: str | int = int(channel_id)
            elif channel_id.isdigit():
                peer = int(channel_id)
            else:
                peer = channel_id
        except ValueError:
            peer = channel_id

        digest = await use_case.execute(peer, topic_id=topic_id, lookback_days=lookback_days)

        markdown_content = digest.to_markdown()

        response = markdown_content

        if pdf:
            renderer = PDFRenderer(output_dir="reports")
            topic_str = str(topic_id) if topic_id else "general"
            filename = f"digest_{topic_str}_{datetime.datetime.now().strftime('%Y-%m-%d')}.pdf"
            pdf_path = renderer.render(digest, filename=filename)
            response += f"\n\n📄 **PDF Report generated**: {pdf_path}"

        return response
    except Exception as e:
        logger.error(f"Error generating digest: {e}", exc_info=True)
        return f"Error generating digest: {str(e)}"


if __name__ == "__main__":
    mcp.run()
