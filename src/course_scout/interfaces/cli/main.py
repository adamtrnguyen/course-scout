"""Course Scout CLI.

Single command for digest generation: `scan`. With `--topic` it digests
one topic; without, it batch-processes every configured topic. Both
paths route through BatchScanUseCase so they share post-processing
semantics by construction.

Auxiliary commands: resolve-channel-id, list-topics, post-task.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

import typer

from course_scout.application.batch_scan import BatchScanUseCase
from course_scout.application.executive_summary import generate_executive_summary
from course_scout.domain.models import ChannelDigest
from course_scout.infrastructure.config import ResolvedTaskConfig, load_settings
from course_scout.infrastructure.logging_config import setup_logging
from course_scout.infrastructure.persistence import SqliteReportRepository
from course_scout.infrastructure.reporting import PDFRenderer
from course_scout.infrastructure.summarization import OrchestratedSummarizer
from course_scout.infrastructure.telegram import TelethonScraper

app = typer.Typer()


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _resolve_channel_id(channel_raw: str) -> str | int:
    """Resolve a channel alias or string ID to a Telegram peer (pure function)."""
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


# Backwards-compat alias used by other modules.
resolve_channel_id = _resolve_channel_id


@app.command(name="resolve-channel-id")
def resolve_channel_id_command(channel_raw: str) -> None:
    """Resolve a channel alias or string ID to a proper Telegram peer."""
    typer.echo(_resolve_channel_id(channel_raw))


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


def _setup_run_logs() -> str:
    """Create a per-run log directory and return the path."""
    from course_scout.infrastructure.logging_config import DEFAULT_LOG_DIR

    run_id = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    root = os.environ.get("COURSE_SCOUT_LOG_DIR", DEFAULT_LOG_DIR)
    run_dir = os.path.join(root, "scans", run_id)
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def _make_summarizer_factory(scraper: TelethonScraper):
    """Return a factory closure that builds OrchestratedSummarizer per task."""

    def _factory(task: ResolvedTaskConfig) -> OrchestratedSummarizer:
        return OrchestratedSummarizer(
            summarizer_model=task.summarizer_model,
            system_prompt=task.system_prompt,
            thinking=task.thinking,
            effort=task.effort,
            chunk_size=task.chunk_size,
            scraper=scraper,
            include_media=task.include_media,
        )

    return _factory


def _filter_tasks_by_topic(
    tasks: list[ResolvedTaskConfig], topic: str | None, scraper: TelethonScraper
) -> list[ResolvedTaskConfig]:
    """If --topic was given, narrow to one task. Otherwise return all."""
    if topic is None:
        return tasks

    if topic.isdigit():
        topic_id = int(topic)
        narrowed = [t for t in tasks if t.topic_id == topic_id]
    else:
        # Resolve by name across configured tasks first; fall back to API lookup
        search = topic.lower()
        narrowed = [t for t in tasks if search in t.name.lower()]
        if not narrowed:
            channel_ids = {t.channel_id for t in tasks}
            for cid in channel_ids:
                resolved_id = asyncio.run(_resolve_topic_by_name(scraper, cid, topic))
                if resolved_id:
                    narrowed = [t for t in tasks if t.topic_id == resolved_id]
                    break

    if not narrowed:
        typer.echo(f"❌ Topic '{topic}' not found among configured tasks.")
        raise typer.Exit(code=1)
    if len(narrowed) > 1:
        typer.echo(f"⚠️  Topic '{topic}' matched {len(narrowed)} tasks; using all of them.")
    return narrowed


def _maybe_publish_task(md_path: str, pdf_generated: bool) -> None:
    """Publish a TaskNotes Inbox stub from the just-written report.

    Three branches:
      1. OBSIDIAN_VAULT_HOST set → ssh+cat the stub into the remote vault
         (NAS Docker → Mac via tailscale, primary path).
      2. Local vault dir exists → write directly (Mac-native runs).
      3. Neither → silently skip.
    """
    from pathlib import Path

    from course_scout.infrastructure.tasknotes import TaskNotesPublisher

    ssh_host = os.environ.get("OBSIDIAN_VAULT_HOST")
    if ssh_host:
        _publish_via_ssh(md_path, pdf_generated, ssh_host)
        return

    try:
        publisher = TaskNotesPublisher()
        # Skip if the resolved vault dir doesn't exist on this machine
        # (typical for NAS Docker without a vault mount).
        if not publisher.vault_dir.exists():
            logger.info(
                "TaskNotes vault not available at %s — skipping publish",
                publisher.vault_dir,
            )
            return
        md = Path(md_path)
        pdf_path = md.with_suffix(".pdf") if pdf_generated else None
        if pdf_path is not None and not pdf_path.is_file():
            pdf_path = None
        stub = publisher.publish(md, pdf_path)
        typer.echo(f"📌 TaskNotes stub: {stub}")
    except Exception as e:
        logger.warning("TaskNotes publish failed: %s", e, exc_info=True)
        typer.echo(f"⚠️  TaskNotes publish skipped: {e}")


def _publish_via_ssh(md_path: str, pdf_generated: bool, host: str) -> None:
    """ssh+cat the TaskNote stub to Mac vault with NAS-via-SMB URIs.

    Reports themselves stay on the NAS (bind-mounted to /app/reports inside
    the container, served to Mac as /Volumes/<share>/course-scout-reports
    via SMB). The TaskNote stub is just lightweight markdown — that goes in
    the vault. The skim:// / file:// URIs point at the Mac-side SMB path so
    clicks open the NAS-hosted file.
    """
    import shlex
    import subprocess
    import tempfile
    from pathlib import Path

    from course_scout.infrastructure.tasknotes import TaskNotesPublisher

    user = os.environ.get("OBSIDIAN_VAULT_USER", "adam")
    remote_vault = os.environ.get(
        "OBSIDIAN_VAULT_DIR",
        "/Users/adam/Library/CloudStorage/OneDrive-Personal/Obsidian Vault",
    )
    # Mac-side path that resolves to the same files we wrote at /app/reports.
    # Default: /app/reports → /Volumes/personal_folder/course-scout-reports.
    container_reports_root = os.environ.get("REPORTS_DIR_LOCAL", "/app/reports")
    mac_reports_root = os.environ.get(
        "REPORTS_DIR_REMOTE",
        "/Volumes/personal_folder/course-scout-reports",
    )
    target = f"{user}@{host}"

    def to_mac_path(local: Path) -> str:
        s = str(local)
        if s.startswith(container_reports_root):
            return mac_reports_root + s[len(container_reports_root) :]
        return s

    try:
        # Resolve to absolute — caller may pass "reports/..." relative to /app.
        md = Path(md_path).resolve()
        pdf_path = md.with_suffix(".pdf") if pdf_generated else None
        if pdf_path is not None and not pdf_path.is_file():
            pdf_path = None

        remote_md = to_mac_path(md)
        remote_pdf = to_mac_path(pdf_path) if pdf_path else None

        with tempfile.TemporaryDirectory() as tmpdir:
            publisher = TaskNotesPublisher(vault_dir=Path(tmpdir))
            stub_local = publisher.publish(
                md,
                pdf_path,
                report_md_url=f"file://{remote_md}",
                report_pdf_url=f"skim://{remote_pdf}" if remote_pdf else None,
            )

            relative = stub_local.relative_to(Path(tmpdir))
            remote_stub = f"{remote_vault}/{relative.as_posix()}"
            remote_parent = str(Path(remote_stub).parent)

            remote_cmd = (
                f"mkdir -p {shlex.quote(remote_parent)} && cat > {shlex.quote(remote_stub)}"
            )
            subprocess.run(
                ["ssh", "-o", "BatchMode=yes", target, remote_cmd],
                input=stub_local.read_bytes(),
                check=True,
                timeout=30,
            )
        typer.echo(f"📌 TaskNotes stub via ssh → {target}:{remote_stub}")
        typer.echo(f"   reports referenced via {mac_reports_root}/ (NAS-served)")
    except subprocess.CalledProcessError as e:
        logger.warning("ssh publish failed (exit %d): %s", e.returncode, e)
        typer.echo(f"⚠️  TaskNotes ssh publish failed: exit {e.returncode}")
    except Exception as e:
        logger.warning("ssh publish failed: %s", e, exc_info=True)
        typer.echo(f"⚠️  TaskNotes ssh publish failed: {e}")


def _output_combined_report(
    all_results: list[tuple[str, ChannelDigest]],
    pdf: bool,
    label_suffix: str = "",
) -> str:
    """Print + save the combined report. Returns the markdown path."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    typer.echo(f"\n{'━' * 60}")
    typer.echo(f"📋 COMBINED DIGEST — {today_str}")
    typer.echo(f"{'━' * 60}\n")

    for name, result in all_results:
        typer.echo(f"## 📌 {name}\n")
        typer.echo(result.to_markdown())  # type: ignore[attr-defined]
        typer.echo(f"\n{'─' * 40}\n")

    report_dir = os.path.join("reports", today_str)
    os.makedirs(report_dir, exist_ok=True)

    typer.echo("📝 Generating executive summary...")
    exec_summary = asyncio.run(generate_executive_summary(all_results, today_str))

    combined_md = f"# Course Scout Daily Scan — {today_str}\n\n"
    combined_md += exec_summary + "\n\n---\n\n"
    for name, result in all_results:
        combined_md += f"## 📌 {name}\n\n"
        combined_md += result.to_markdown() + "\n\n---\n\n"  # type: ignore[attr-defined]

    from course_scout.infrastructure.deep_links import deep_linkify

    combined_md = deep_linkify(combined_md)

    md_filename = f"scan_{today_str}{label_suffix}.md"
    md_path = os.path.join(report_dir, md_filename)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(combined_md)
    typer.echo(f"📝 Combined report: {md_path}")

    if pdf:
        renderer = PDFRenderer()
        pdf_filename = f"scan_{today_str}{label_suffix}.pdf"
        pdf_path = renderer.render_from_markdown(combined_md, pdf_filename, output_dir=report_dir)
        typer.echo(f"📄 PDF report: {pdf_path}")

    repository = SqliteReportRepository()
    for name, result in all_results:
        repository.add_report(
            date=result.date,  # type: ignore[attr-defined]
            channel_id=str(result.channel_name),  # type: ignore[attr-defined]
            task_name=name,
            md_path=md_path,
            summary="\n".join(result.summaries),  # type: ignore[attr-defined]
        )
    typer.echo("🗄️ Reports saved to database.")
    return md_path


# ──────────────────────────────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────────────────────────────


@app.command()
def scan(
    topic: str | None = typer.Option(
        None,
        "--topic",
        "-t",
        help="Restrict to a single topic by ID, name fragment, or task name. "
        "If omitted, scans every configured topic.",
    ),
    days: int = typer.Option(1, "--days", "-d", help="Number of complete days to scan"),
    pdf: bool = typer.Option(True, "--pdf/--no-pdf", help="Generate PDF report (default: on)"),
    today: bool = typer.Option(False, "--today", help="Include today (incomplete day)"),
    dedup: bool = typer.Option(
        True,
        "--dedup/--no-dedup",
        help="Filter previously-seen course/file items (default: on). "
        "Use --no-dedup for a manual rerun showing everything.",
    ),
    publish_task: bool = typer.Option(
        True,
        "--publish-task/--no-publish-task",
        help="Auto-publish a TaskNotes Inbox stub from the report (default: on). "
        "Skipped automatically when --topic is used (single-topic runs are ad-hoc) "
        "or when the vault directory is unavailable. Use --no-publish-task to "
        "disable explicitly (e.g. NAS Docker runs).",
    ),
):
    """Generate a digest across configured topics (all by default; one with --topic)."""
    setup_logging()
    settings = load_settings()

    if not settings.tasks:
        typer.echo("No tasks configured in config.yaml.")
        raise typer.Exit(code=1)

    # Pre-flight: if Claude OAuth quota is exhausted, sleep until 5h reset.
    # No-op when CLAUDE_RATE_LIMIT_WAIT=0 or the usage endpoint is unavailable.
    if os.environ.get("CLAUDE_RATE_LIMIT_WAIT", "1") != "0":
        from course_scout.infrastructure.rate_limit import wait_for_quota

        wait_for_quota()

    scraper = TelethonScraper(
        settings.tg_api_id,
        settings.tg_api_hash,
        settings.session_path,
        phone=settings.phone_number,
        login_code=settings.login_code,
    )

    selected_tasks = _filter_tasks_by_topic(settings.resolved_tasks, topic, scraper)

    label = "today" if today else f"last {days} complete day(s)"
    scope = f"topic '{topic}'" if topic else f"{len(selected_tasks)} topics"
    typer.echo(f"━━━ Course Scout — Scanning {scope} ({label}) ━━━\n")

    run_dir = _setup_run_logs()
    typer.echo(f"📁 Run logs: {run_dir}/")

    use_case = BatchScanUseCase(
        scraper=scraper,
        summarizer_factory=_make_summarizer_factory(scraper),
    )
    all_results = asyncio.run(
        use_case.execute(
            tasks=selected_tasks,
            timezone=settings.timezone,
            days=days,
            include_today=today,
            dedup=dedup,
            run_dir=run_dir,
        )
    )

    if not all_results:
        typer.echo("\nNo activity found across any topics.")
        return

    # Render report from (name, digest) pairs.
    display_results = [(name, digest) for name, digest, _provider in all_results]
    label_suffix = f"_{topic.replace(' ', '_')}" if topic else ""
    md_path = _output_combined_report(display_results, pdf, label_suffix=label_suffix)

    # Publish TaskNotes Inbox stub for daily Mac-side flow. Skipped for
    # single-topic runs (ad-hoc) and silently no-ops when the vault is
    # unavailable (NAS container path).
    if publish_task and topic is None:
        _maybe_publish_task(md_path, pdf)

    # Aggregate usage across providers.
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


@app.command(name="post-task")
def post_task(
    date: str = typer.Option(
        None, "--date", help="YYYY-MM-DD; defaults to most recent dated subdir of reports/"
    ),
    reports_dir: str = typer.Option(
        "reports",
        "--reports-dir",
        help="Parent dir holding YYYY-MM-DD subdirs (default: ./reports/)",
    ),
    vault_dir: str = typer.Option(
        None,
        "--vault-dir",
        help="Override Obsidian vault path (else COURSE_SCOUT_VAULT_DIR env, "
        "else ~/Library/CloudStorage/OneDrive-Personal/Obsidian Vault)",
    ),
):
    """Publish a TaskNotes Inbox stub for a course-scout daily report."""
    import re as _re
    from pathlib import Path as _Path

    from course_scout.infrastructure.tasknotes import TaskNotesPublisher

    setup_logging()

    parent = _Path(reports_dir).expanduser().resolve()
    if not parent.is_dir():
        typer.echo(f"ERR: reports dir not found: {parent}", err=True)
        raise typer.Exit(2)

    if date is None:
        dated = sorted(
            d.name
            for d in parent.iterdir()
            if d.is_dir() and _re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name)
        )
        if not dated:
            typer.echo(f"ERR: no YYYY-MM-DD subdirs in {parent}", err=True)
            raise typer.Exit(2)
        date = dated[-1]

    report_dir = parent / date
    md_files = sorted(report_dir.glob("scan_*.md"))
    if not md_files:
        typer.echo(f"ERR: no scan_*.md in {report_dir}", err=True)
        raise typer.Exit(2)
    md_path = md_files[0]
    pdf_path = md_path.with_suffix(".pdf")
    if not pdf_path.is_file():
        pdf_path = None  # type: ignore[assignment]

    publisher = TaskNotesPublisher(_Path(vault_dir).expanduser() if vault_dir else None)
    stub = publisher.publish(md_path, pdf_path)
    typer.echo(f"wrote {stub}")


@app.command()
def list_topics(channel: str):
    """List all topics in a forum-enabled Telegram group/channel."""
    setup_logging()
    settings = load_settings()

    try:
        if channel.startswith("-") and channel[1:].isdigit():
            channel_id: str | int = int(channel)
        elif channel.isdigit():
            channel_id = int(channel)
        else:
            channel_id = channel
    except ValueError:
        channel_id = channel

    async def _list_them():
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

    asyncio.run(_list_them())


# Logger handle so module-level `logging.getLogger(__name__)` matches up
# with the BatchScanUseCase logger naming when needed.
logger = logging.getLogger(__name__)


if __name__ == "__main__":
    app()
