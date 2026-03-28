import asyncio
import os
from datetime import datetime

import typer

from course_scout.application.digest import GenerateDigestUseCase
from course_scout.domain.models import ChannelDigest
from course_scout.infrastructure.config import Settings, load_settings
from course_scout.infrastructure.logging_config import setup_logging
from course_scout.infrastructure.persistence import SqliteReportRepository
from course_scout.infrastructure.reporting import PDFRenderer
from course_scout.infrastructure.summarization import OrchestratedSummarizer
from course_scout.infrastructure.telegram import TelethonScraper

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
        summarizer_model=settings.agent_defaults.summarizer_model,
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


def _setup_run_logs():
    """Create a per-run log directory and return the path."""
    run_id = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = os.path.join("logs", "scans", run_id)
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def _add_topic_logger(run_dir, topic_name):
    """Add a file handler for a specific topic, return the logger."""
    import logging

    safe_name = topic_name.replace(" ", "_").replace("/", "_").lower()
    log_path = os.path.join(run_dir, f"{safe_name}.log")
    topic_logger = logging.getLogger(f"course_scout.topic.{safe_name}")
    topic_logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    topic_logger.addHandler(fh)
    return topic_logger


async def _fetch_all_topics(scraper, tasks, start_date, end_date):
    """Fetch messages for all topics sequentially via Telethon."""
    from course_scout.infrastructure.config import ResolvedTaskConfig

    fetched: dict[str, tuple[ResolvedTaskConfig, list]] = {}
    for task in tasks:
        name = task.name
        try:
            messages = await scraper.get_messages(
                task.channel_id, start_date, end_date=end_date, topic_id=task.topic_id
            )
            # Cap at per-topic max_messages
            messages = messages[: task.max_messages]
            if len(messages) >= 3:
                fetched[name] = (task, messages)
                typer.echo(f"   📨 {name}: {len(messages)} messages")
            elif messages:
                typer.echo(f"   ⏭️  {name}: {len(messages)} messages (skipped, <3)")
            else:
                typer.echo(f"   ⏭️  {name}: no messages")
        except Exception as e:
            typer.echo(f"   ❌ {name}: fetch error — {e}")
    return fetched


async def _scan_all_tasks(scraper, settings, tasks, days, include_today=False):
    """Scan all tasks: fetch messages sequentially, then summarize in parallel."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)

    if include_today:
        # Rolling window from N days ago to now
        start_date = now - timedelta(days=days)
        end_date = now
    else:
        # Fixed complete days: yesterday midnight to today midnight
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = today_midnight
        start_date = today_midnight - timedelta(days=days)

    start_str = start_date.strftime("%Y-%m-%d %H:%M")
    end_str = end_date.strftime("%Y-%m-%d %H:%M")
    typer.echo(f"📅 Window: {start_str} → {end_str}")

    run_dir = _setup_run_logs()
    typer.echo(f"📁 Run logs: {run_dir}/")

    # Phase 1: Fetch
    typer.echo("📡 Fetching messages from all topics...")
    fetched = await _fetch_all_topics(scraper, tasks, start_date, end_date)

    if not fetched:
        return []

    # Phase 2: Summarize all topics in parallel (each gets its own summarizer + logger)
    active = len(fetched)
    typer.echo(f"\n🧠 Summarizing {active} topics in parallel...")

    async def _summarize_one(name, task, messages):
        topic_log = _add_topic_logger(run_dir, name)
        topic_log.info(
            f"Starting: {len(messages)} msgs, topic={task.topic_id}, "
            f"model={task.summarizer_model}, thinking={task.thinking}, "
            f"effort={task.effort}, chunk_size={task.chunk_size}"
        )
        try:
            summarizer = OrchestratedSummarizer(
                summarizer_model=task.summarizer_model,
                system_prompt=task.system_prompt,
                thinking=task.thinking,
                effort=task.effort,
                chunk_size=task.chunk_size,
                scraper=scraper,
            )
            digest = await summarizer.summarize(messages, topic_id=task.topic_id)
            if digest:
                msg_count = len(digest.items)
                topic_log.info(f"Completed: {msg_count} items extracted")
                typer.echo(f"   ✅ {name}: {msg_count} items")
                # Log usage to topic file
                provider = summarizer.orchestrator.provider
                if hasattr(provider, "usage"):
                    for call in provider.usage.calls:
                        topic_log.info(
                            f"  {call['model']}: {call['input_tokens']} in / "
                            f"{call['output_tokens']} out / {call['duration_ms']}ms"
                        )
                return (name, digest, provider)
        except Exception as e:
            topic_log.error(f"Failed: {e}", exc_info=True)
            typer.echo(f"   ❌ {name}: {e}")
        return None

    coros = [_summarize_one(name, task, msgs) for name, (task, msgs) in fetched.items()]
    raw_results = await asyncio.gather(*coros)

    # Collect results and merge usage stats
    results = []
    for r in raw_results:
        if r is not None:
            name, digest, provider = r
            results.append((name, digest, provider))

    return results


async def _generate_executive_summary(all_results, date_str):
    """Generate a personalized executive summary from all topic digests."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        TextBlock,
        query,
    )

    # Build condensed version with enough detail for good ranking
    condensed = ""
    for name, result in all_results:
        condensed += f"\n### {name}\n"
        for item in result.items[:8]:
            cat = item.category.upper()
            links = f" | Links: {', '.join(item.links[:2])}" if item.links else ""
            condensed += f"- [{cat}] {item.title}: {item.description[:150]}{links}\n"

    prompt = f"""Today's ({date_str}) scan results from art community Telegram channels:

{condensed}

Write an executive summary for Adam. He focuses on:
- 2D illustration, character design, anatomy, figure drawing
- Color theory, lighting, rendering techniques
- Asian artists, anime/manga art styles
- Art courses (Coloso, Schoolism, CGMA, Domestika, etc.)

FORMAT (use this exact structure):

## Top 5 Finds

1. **[Most relevant item]** — why it matters, source topic
2. **[Second item]** — why it matters, source topic
3. **[Third item]** — why it matters, source topic
4. **[Fourth item]** — why it matters, source topic
5. **[Fifth item]** — why it matters, source topic

## Summary

1-2 paragraphs covering the rest. Flag time-sensitive items
(expiring links, group buys closing, new course drops).

RANKING CRITERIA (in order of value — what Adam can ACT on today):
1. Downloadable files — courses, lesson videos, art resources shared with links/passwords
2. Course reviews with ratings — helps decide what to study next
3. Technique discussions — actionable art tips, workflow breakdowns, style references
4. Community resources — spreadsheets, tool links, guides, artist recommendation lists
5. Group buy activity — only if actively organizing with participants

DO NOT rank unfulfilled course requests highly — they signal demand but Adam
can't act on them. Mention them briefly in the Summary section if relevant."""

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        system_prompt="You write concise executive summaries for daily art community digests.",
        max_turns=1,
        permission_mode="bypassPermissions",
        effort="low",
        thinking={"type": "disabled"},
    )

    last_text = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    last_text = block.text

    if last_text:
        return f"## Executive Summary\n\n{last_text}"
    return "## Executive Summary\n\n*Summary generation failed.*"


def _output_combined_report(all_results, pdf=False):
    """Print and save combined digest from all scan results."""
    typer.echo(f"\n{'━' * 60}")
    typer.echo(f"📋 COMBINED DIGEST — {datetime.now().strftime('%Y-%m-%d')}")
    typer.echo(f"{'━' * 60}\n")

    for name, result in all_results:
        typer.echo(f"## 📌 {name}\n")
        typer.echo(result.to_markdown())
        typer.echo(f"\n{'─' * 40}\n")

    today_str = datetime.now().strftime("%Y-%m-%d")
    report_dir = os.path.join("reports", today_str)
    os.makedirs(report_dir, exist_ok=True)

    # Build executive summary
    typer.echo("📝 Generating executive summary...")
    exec_summary = asyncio.run(
        _generate_executive_summary(all_results, today_str)
    )

    combined_md = f"# Course Scout Daily Scan — {today_str}\n\n"
    combined_md += exec_summary + "\n\n---\n\n"
    for name, result in all_results:
        combined_md += f"## 📌 {name}\n\n{result.to_markdown()}\n\n---\n\n"

    md_path = os.path.join(report_dir, f"scan_{today_str}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(combined_md)
    typer.echo(f"📝 Combined report: {md_path}")

    if pdf:
        renderer = PDFRenderer()
        pdf_path = renderer.render_from_markdown(
            combined_md, f"scan_{today_str}.pdf", output_dir=report_dir
        )
        typer.echo(f"📄 PDF report: {pdf_path}")

    repository = SqliteReportRepository()
    for name, result in all_results:
        repository.add_report(
            date=result.date,
            channel_id=str(result.channel_name),
            task_name=name,
            md_path=md_path,
            summary="\n".join(result.summaries),
        )
    typer.echo("🗄️ Reports saved to database.")


@app.command()
def scan(
    days: int = typer.Option(1, "--days", "-d", help="Number of complete days to scan"),
    pdf: bool = typer.Option(True, "--pdf/--no-pdf", help="Generate PDF report (default: on)"),
    today: bool = typer.Option(False, "--today", help="Include today (incomplete day)"),
):
    """Scan all configured topics. Defaults to yesterday (last complete day)."""
    setup_logging()
    settings = load_settings()

    scraper = TelethonScraper(
        settings.tg_api_id,
        settings.tg_api_hash,
        settings.session_path,
        phone=settings.phone_number,
        login_code=settings.login_code,
    )

    if not settings.tasks:
        typer.echo("No tasks configured in config.yaml.")
        raise typer.Exit(code=1)

    label = "today" if today else f"last {days} complete day(s)"
    typer.echo(
        f"━━━ Course Scout — Scanning {len(settings.tasks)} topics ({label}) ━━━\n"
    )

    all_results = asyncio.run(
        _scan_all_tasks(scraper, settings, settings.resolved_tasks, days, include_today=today)
    )

    if not all_results:
        typer.echo("\nNo activity found across any topics.")
        return

    # Separate results from providers for usage aggregation
    display_results = [(name, digest) for name, digest, _provider in all_results]
    _output_combined_report(display_results, pdf)

    # Merge usage from all parallel providers
    from course_scout.infrastructure.providers.claude_provider import UsageStats

    merged = UsageStats()
    for _name, _digest, provider in all_results:
        if hasattr(provider, "usage"):
            u = provider.usage
            merged.total_input_tokens += u.total_input_tokens
            merged.total_output_tokens += u.total_output_tokens
            merged.total_cache_read_tokens += u.total_cache_read_tokens
            merged.total_cache_creation_tokens += u.total_cache_creation_tokens
            merged.total_cost_usd += u.total_cost_usd
            merged.total_duration_ms += u.total_duration_ms
            merged.call_count += u.call_count
            merged.calls.extend(u.calls)

    typer.echo(f"\n{merged.summary()}")


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
