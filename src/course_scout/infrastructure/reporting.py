import logging
import os
import re
from datetime import datetime

from markdown_pdf import MarkdownPdf, Section

from course_scout.domain.models import ChannelDigest

logger = logging.getLogger(__name__)

CSS = """
body {
    font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif;
    color: #1a1a2e;
    line-height: 1.7;
    font-size: 11pt;
    max-width: 100%;
}
h1 {
    color: #16213e;
    font-size: 22pt;
    border-bottom: 3px solid #0f3460;
    padding-bottom: 8px;
    margin-top: 30px;
}
h2 {
    color: #0f3460;
    font-size: 16pt;
    border-bottom: 2px solid #e2e8f0;
    padding-bottom: 6px;
    margin-top: 25px;
}
h3 {
    color: #533483;
    font-size: 13pt;
    margin-top: 18px;
}
p { margin-bottom: 8px; }
a {
    color: #0f3460;
    text-decoration: underline;
}
ul, ol {
    padding-left: 22px;
    margin-bottom: 12px;
}
li {
    margin-bottom: 12px;
    line-height: 1.5;
}
li strong {
    color: #16213e;
}
li p {
    margin: 2px 0;
}
hr {
    border: none;
    border-top: 2px solid #e2e8f0;
    margin: 30px 0;
}
code {
    background-color: #f1f3f5;
    padding: 2px 5px;
    border-radius: 3px;
    font-size: 10pt;
}
blockquote {
    border-left: 4px solid #0f3460;
    padding-left: 12px;
    color: #4a5568;
    margin: 12px 0;
}
table {
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0;
}
th, td {
    border: 1px solid #e2e8f0;
    padding: 8px 12px;
    text-align: left;
}
th {
    background-color: #f7fafc;
    font-weight: bold;
}
"""


class PDFRenderer:
    def __init__(self, output_dir: str = "reports"):
        """Initialize the renderer with an output directory."""
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    def render(self, digest: ChannelDigest, filename: str, output_dir: str | None = None) -> str:
        """Render a ChannelDigest to a PDF."""
        return self.render_from_markdown(digest.to_markdown(), filename, output_dir=output_dir)

    def render_from_markdown(
        self, markdown_text: str, filename: str, output_dir: str | None = None
    ) -> str:
        """Render a Markdown string to a PDF report."""
        target_dir = output_dir or self.output_dir
        output_path = os.path.join(target_dir, filename)
        os.makedirs(target_dir, exist_ok=True)

        # Clean up the markdown for PDF
        markdown_text = self._clean_for_pdf(markdown_text)

        try:
            pdf = MarkdownPdf(toc_level=2)
            pdf.meta["title"] = f"Course Scout Daily Scan — {datetime.now().strftime('%Y-%m-%d')}"
            pdf.meta["author"] = "Course Scout"

            sections = self._split_by_topic(markdown_text)

            if not sections:
                pdf.add_section(Section(markdown_text, toc=False), user_css=CSS)
            else:
                for sect_text in sections:
                    pdf.add_section(Section(sect_text, toc=True), user_css=CSS)

            pdf.save(output_path)
            logger.info(f"PDF generated at {output_path} with {len(sections)} sections")
            return output_path
        except Exception as e:
            logger.error(f"Failed to generate PDF: {e}", exc_info=True)
            return f"Error: {str(e)}"

    @staticmethod
    def _clean_for_pdf(md: str) -> str:
        """Clean markdown for better PDF rendering."""
        # Remove the redundant "# Daily Digest: Topic XXXXX" lines
        md = re.sub(r"^# Daily Digest:.*\n", "", md, flags=re.MULTILINE)

        # Remove "**Date**: YYYY-MM-DD" lines (date is in the title)
        md = re.sub(r"^\*\*Date\*\*:.*\n\n?", "", md, flags=re.MULTILINE)

        # Convert checkbox items to bullet points
        md = re.sub(r"^- \[ \] ", "- ", md, flags=re.MULTILINE)

        # Promote ## 📌 Topic headers to # for top-level sections
        md = re.sub(r"^## (📌 .*)$", r"# \1", md, flags=re.MULTILINE)

        # Demote remaining ## to ### (category headers within topics)
        md = re.sub(r"^## (.*)$", r"### \1", md, flags=re.MULTILINE)

        # Convert bare URLs to clickable markdown links
        # Matches URLs not already inside []() or preceded by ](
        md = re.sub(
            r'(?<!\]\()(?<!\[)(https?://[^\s\),]+)',
            r'[\1](\1)',
            md,
        )

        # Fix double-wrapped links: [[url](url)](url) → [url](url)
        md = re.sub(r'\[\[([^\]]+)\]\(([^\)]+)\)\]\([^\)]+\)', r'[\1](\2)', md)

        # Parenthesized URL lists after bold titles are already handled
        # by the bare URL conversion above

        return md

    @staticmethod
    def _split_by_topic(md: str) -> list[str]:
        """Split markdown into sections by top-level # headers."""
        parts = re.split(r"(?m)^(# .+)$", md)
        sections = []
        current = ""

        for part in parts:
            if not part.strip():
                continue
            if re.match(r"^# ", part):
                if current.strip():
                    sections.append(current.strip())
                current = part
            else:
                current += "\n" + part

        if current.strip():
            sections.append(current.strip())

        return sections
