"""TaskNotes Inbox publisher — convert a course-scout report into a TaskNotes task stub.

The stub:

- Lives in `<vault>/TaskNotes/course-scout-YYYY-MM-DD.md` (the plugin's
  `tasksFolder` root — the "Inbox" view is a virtual filter on
  status=1-inbox, not a folder)
- Carries TaskNotes frontmatter (`tags: [task, inbox, course-scout]`,
  `status: open`, etc.) so the TaskNotes plugin recognizes it as an
  actionable task in the user's daily review.
- Embeds the Executive Summary + Top finds inline so the user can scan it
  in Obsidian without leaving the app.
- Links back to the full report (markdown + PDF) on disk via `file://`.
  Skim opens the PDF when set as macOS' default PDF handler.

Designed to run on the same machine that owns the vault filesystem (the
Mac, not the NAS Docker container) — see CLAUDE.md "Cross-machine
publishing" for the topology rationale.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# macOS default; override via env var COURSE_SCOUT_VAULT_DIR for other machines
# / non-OneDrive setups.
DEFAULT_VAULT_DIR = Path.home() / "Library/CloudStorage/OneDrive-Personal/Obsidian Vault"


def _resolve_vault_dir(vault_dir: Path | None) -> Path:
    if vault_dir is not None:
        return vault_dir.expanduser().resolve()
    env = os.environ.get("COURSE_SCOUT_VAULT_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_VAULT_DIR


def _extract_section(md: str, heading: str) -> str:
    """Pull the body of a `## Heading` section, up to the next `## ` or EOF.

    Tolerant of bracket-tagged variants like `## [SUMMARY] Executive Summary`
    that appear in some channel-level reports.
    """
    # Match either `## Heading` OR `## [TAG] Heading`
    pattern = rf"^##\s+(?:\[[A-Z]+\]\s+)?{re.escape(heading)}\s*\n(.*?)(?=^##\s|\Z)"
    m = re.search(pattern, md, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def _count_finds(top_finds_body: str) -> int:
    """Count numbered list items in the Top Finds body."""
    return len(re.findall(r"^\d+\.", top_finds_body, re.MULTILINE))


def _preserve_date_created(stub_path: Path, fallback: str) -> str:
    """Re-runs on the same date should not reset TaskNotes' age stamp."""
    if not stub_path.exists():
        return fallback
    try:
        text = stub_path.read_text(encoding="utf-8")
    except OSError:
        return fallback
    m = re.search(r"^dateCreated:\s*(\S+)", text, re.MULTILINE)
    return m.group(1) if m else fallback


class TaskNotesPublisher:
    """Write a TaskNotes Inbox stub from a course-scout daily report."""

    SOURCE = "course-scout"

    def __init__(self, vault_dir: Path | None = None) -> None:
        """Initialize publisher; vault_dir defaults to env or DEFAULT_VAULT_DIR."""
        self.vault_dir = _resolve_vault_dir(vault_dir)
        # The TaskNotes plugin's tasksFolder is `TaskNotes/` (root). Files
        # placed in `TaskNotes/Inbox/` end up auto-moved to Archive, while
        # the plugin's "Inbox" *view* is a virtual filter on
        # status=1-inbox across the whole tasksFolder. So we write to the
        # root, not to a subfolder named Inbox.
        self.tasks_dir = self.vault_dir / "TaskNotes"

    def publish(
        self,
        report_md: Path,
        report_pdf: Path | None = None,
        report_md_url: str | None = None,
        report_pdf_url: str | None = None,
    ) -> Path:
        """Generate the stub and write it to the Inbox. Returns the stub path.

        URL overrides let callers (e.g. SSH publisher) point the inline links
        at the destination machine's filesystem instead of the local one.
        """
        report_md = report_md.expanduser().resolve()
        if not report_md.is_file():
            raise FileNotFoundError(f"report markdown not found: {report_md}")
        if report_pdf is not None:
            report_pdf = report_pdf.expanduser().resolve()
            if not report_pdf.is_file():
                logger.warning("PDF not found, stub will omit Skim link: %s", report_pdf)
                report_pdf = None

        date = self._date_from_report(report_md)
        md_text = report_md.read_text(encoding="utf-8")
        exec_sum = _extract_section(md_text, "Executive Summary")
        top_finds = (
            _extract_section(md_text, "Top 5 Finds")
            or _extract_section(md_text, "Top Finds")
            or _extract_section(md_text, "Findings")
        )
        n_finds = _count_finds(top_finds)

        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        stub_path = self.tasks_dir / f"{self.SOURCE}-{date}.md"

        now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
        date_created = _preserve_date_created(stub_path, now_iso)

        title_suffix = f" — {n_finds} finds" if n_finds else ""
        title = f"{self.SOURCE} {date}{title_suffix}"

        body_parts: list[str] = []
        if exec_sum:
            body_parts.append("## Executive Summary\n")
            body_parts.append(exec_sum)
            body_parts.append("")
        if top_finds:
            body_parts.append("## Top finds\n")
            body_parts.append(top_finds)
            body_parts.append("")
        if not body_parts:
            body_parts.append("_(report had no Executive Summary or Top Finds section)_\n")

        body_parts.append("---")
        body_parts.append("")
        # `skim://<absolute-path>` opens the PDF in Skim.app on click.
        # Skim is the user's default PDF handler; this avoids the file:///
        # → Preview.app fallback that loses page navigation niceties.
        pdf_url = report_pdf_url or (f"skim://{report_pdf}" if report_pdf is not None else None)
        md_url = report_md_url or f"file://{report_md}"
        if pdf_url:
            body_parts.append(f"[📄 Open full report in Skim →]({pdf_url})")
        body_parts.append(f"[📝 Open full markdown →]({md_url})")

        # Frontmatter follows the user's TaskNotes plugin convention
        # (.obsidian/plugins/tasknotes/data.json):
        #   - status: "1-inbox" matches customStatuses[0].value (the plugin
        #     filters Inbox views by this exact value, NOT the literal "inbox")
        #   - priority: "normal" matches customPriorities[2].value
        #   - tag "task" matches taskTag (taskIdentificationMethod=tag)
        frontmatter = (
            "---\n"
            f'title: "{title}"\n'
            "status: 1-inbox\n"
            "priority: normal\n"
            f"dateCreated: {date_created}\n"
            f"dateModified: {now_iso}\n"
            "tags:\n"
            "  - task\n"
            f"  - {self.SOURCE}\n"
            "contexts:\n"
            "  - dailies\n"
            "---\n"
        )

        stub_path.write_text(frontmatter + "\n".join(body_parts) + "\n", encoding="utf-8")
        logger.info("wrote TaskNotes stub: %s", stub_path)
        return stub_path

    @staticmethod
    def _date_from_report(report_md: Path) -> str:
        """Extract YYYY-MM-DD from the report filename or its parent directory."""
        m = re.search(r"\d{4}-\d{2}-\d{2}", report_md.stem)
        if m:
            return m.group(0)
        m = re.search(r"\d{4}-\d{2}-\d{2}", report_md.parent.name)
        if m:
            return m.group(0)
        # Last resort: today.
        return datetime.now().strftime("%Y-%m-%d")
