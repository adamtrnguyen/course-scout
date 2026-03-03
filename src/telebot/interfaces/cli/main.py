import asyncio
import os
from datetime import datetime

import typer

from telebot.application.digest import GenerateDigestUseCase
from telebot.domain.models import ChannelDigest
from telebot.infrastructure.config import Settings, load_settings
from telebot.infrastructure.logging_config import setup_logging
from telebot.infrastructure.persistence import SqliteReportRepository
from telebot.infrastructure.reporting import PDFRenderer
from telebot.infrastructure.summarization import OrchestratedSummarizer
from telebot.infrastructure.telegram import TelethonScraper

# Removed local Settings definition, uses infrastructure.config.load_settings()

app = typer.Typer()


@app.command()
def resolve_channel_id(channel_raw: str) -> str | int:
    """Resolve a channel alias or string ID to a proper Telegram peer."""
    try:
        if channel_raw.startswith("-") and channel_raw[1:].isdigit():
            return int(channel_raw)
        if channel_raw.isdigit():
            return int(channel_raw)
    except ValueError:
        pass

    aliases = {
        "coursebusters": -1001603660516,
        "course busters": -1001603660516,
    }
    key = channel_raw.lstrip("@").lower()
    return aliases.get(key, channel_raw)


async def _resolve_topic_by_name(scraper: TelethonScraper, channel_id: str | int, name: str) -> int:
    """Find a topic ID by its title in a forum channel."""
    topics = await scraper.list_topics(channel_id)
    search_lower = name.lower()
    matches = [t for t in topics if search_lower in t["title"].lower()]
    if not matches:
        return 0
    exact = next((t for t in matches if t["title"].lower() == search_lower), None)
    target = exact or matches[0]
    return target["id"]


async def _handle_digest_delivery(
    result: ChannelDigest,
    channel: str,
    topic: str | None,
    pdf: bool,
    send_to: str | None,
    email: str | None,
    settings: Settings,
):
    """Handle the various output and delivery options for a digest."""
    # Console Output
    typer.echo(f"\n--- Digest for {channel} ({result.date}) ---\n")
    typer.echo(result.to_markdown())

    # Path Setup
    today_str = datetime.now().strftime("%Y-%m-%d")
    report_dir = os.path.join("reports", today_str)
    os.makedirs(report_dir, exist_ok=True)

    # Markdown File
    md_filename = f"digest_{topic or channel}_{result.date}.md"
    md_path = os.path.join(report_dir, md_filename)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(result.to_markdown())
    typer.echo(f"\n📝 Markdown Report generated: {md_path}")

    # PDF & Telegram
    pdf_path = None
    if pdf or send_to:
        renderer = PDFRenderer()
        filename = f"digest_{topic or channel}_{result.date}.pdf"
        pdf_path = renderer.render(result, filename=filename, output_dir=report_dir)
        typer.echo(f"📄 PDF Report generated: {pdf_path}")

        if send_to:
            typer.echo(f"\n📨 Sending PDF to {send_to}...")
            from telethon import TelegramClient

            client = TelegramClient(settings.session_path, settings.tg_api_id, settings.tg_api_hash)
            await client.connect()
            try:
                try:
                    _ = int(send_to)
                except ValueError:
                    pass
                # We no longer send the PDF to Telegram per user request
                # await client.send_file(peer, pdf_path, caption=f"Digest for {channel}")
                typer.echo("ℹ️ PDF generated but not sent to Telegram (Markdown only mode).")
            except Exception as e:
                typer.echo(f"❌ Failed to send: {e}")
            finally:
                await client.disconnect()

    if email:
        typer.echo(f"\n📧 Sending email to {email}... (Feature Coming Soon)")

    # Persist to Database
    repository = SqliteReportRepository()
    repository.add_report(
        date=result.date,
        channel_id=str(channel),
        task_name=topic or channel,
        md_path=md_path,
        pdf_path=pdf_path,
        summary="\n".join(result.summaries),
    )
    typer.echo("🗄️ Report metadata saved to database.")


@app.command()
def digest(
    channel: str,
    topic: str | None = typer.Option(None, "--topic", "-t", help="Topic ID or Name"),
    days: int = typer.Option(1, "--days", "-d", help="Days to look back"),
    pdf: bool = typer.Option(False, "--pdf", help="Generate a PDF report"),
    provider: str = typer.Option("groq", "--provider", help="AI Provider (gemini, groq)"),
    send_to: str | None = typer.Option(None, "--send-to", help="User/Chat to notify"),
    email: str | None = typer.Option(None, "--email", help="Email the report"),
    today: bool = typer.Option(False, "--today", help="Summarize from 12 AM today"),
):
    """Generate a daily digest for a Telegram channel or specific Topic."""
    setup_logging()
    settings = load_settings()
    channel_id = resolve_channel_id(channel)
    scraper = TelethonScraper(
        settings.tg_api_id,
        settings.tg_api_hash,
        settings.session_path,
        phone=settings.phone_number,
        login_code=settings.login_code,
    )

    resolved_topic_id = None
    if topic:
        if topic.isdigit():
            resolved_topic_id = int(topic)
        else:
            typer.echo(f"Resolving topic '{topic}' in {channel_id}...")
            resolved_topic_id = asyncio.run(_resolve_topic_by_name(scraper, channel_id, topic))
            if not resolved_topic_id:
                typer.echo(f"❌ Topic '{topic}' not found.")
                raise typer.Exit(code=1)
            typer.echo(f"✅ Resolved to Topic ID: {resolved_topic_id}")

    summarizer = OrchestratedSummarizer(
        gemini_key=settings.gemini_api_key,
        groq_key=settings.groq_api_key,
        provider=provider,
        summarizer_model=settings.summarizer_model,
        verifier_model=settings.verifier_model,
        scraper=scraper,
    )
    use_case = GenerateDigestUseCase(scraper, summarizer)
    result = asyncio.run(
        use_case.execute(
            channel_id,
            topic_id=resolved_topic_id,
            lookback_days=days,
            timezone=settings.timezone,
            window_mode=settings.window_mode,
            today_only=today,
        )
    )  # type: ignore

    if not result:
        typer.echo(f"ℹ️ No new messages found for {channel} in the last {days} days.")
        return

    asyncio.run(_handle_digest_delivery(result, channel, topic, pdf, send_to, email, settings))


@app.command()
def list_topics(channel: str):
    """List all topics in a forum-enabled Telegram group/channel."""
    setup_logging()
    settings = load_settings()

    # Handle numeric IDs
    try:
        if channel.startswith("-") and channel[1:].isdigit():
            channel_id: str | int = int(channel)
        elif channel.isdigit():
            channel_id = int(channel)
        else:
            channel_id = channel
    except ValueError:
        channel_id = channel

    async def list_them():
        scraper = TelethonScraper(
            settings.tg_api_id,
            settings.tg_api_hash,
            settings.session_path,
            phone=settings.phone_number,
            login_code=settings.login_code,
        )
        topics = await scraper.list_topics(channel_id)
        for topic in topics:
            typer.echo(f"ID: {topic['id']} | Title: {topic['title']}")

    asyncio.run(list_them())


if __name__ == "__main__":
    app()
