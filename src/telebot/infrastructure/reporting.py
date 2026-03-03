import logging
import os
import re

from markdown_pdf import MarkdownPdf, Section

from telebot.domain.models import ChannelDigest

logger = logging.getLogger(__name__)


class PDFRenderer:
    def __init__(self, output_dir: str = "reports"):
        """Initialize the renderer with an output directory."""
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    def render(self, digest: ChannelDigest, filename: str, output_dir: str | None = None) -> str:
        """Render a ChannelDigest to a PDF using the markdown-pdf library."""
        return self.render_from_markdown(digest.to_markdown(), filename, output_dir=output_dir)

    def _normalize_headers(self, md: str) -> str:
        """Normalize headers to avoid hierarchy errors (no skipping levels)."""
        lines = md.split("\n")
        current_max_level = 0
        new_lines = []
        for line in lines:
            match = re.match(r"^(#+)\s+(.*)", line)
            if match:
                hashes, title = match.groups()
                level = len(hashes)
                new_level = 1 if current_max_level == 0 else min(level, current_max_level + 1)
                current_max_level = max(current_max_level, new_level)
                new_lines.append(f"{'#' * new_level} {title}")
            else:
                new_lines.append(line)
        return "\n".join(new_lines)

    def _get_css(self) -> str:
        """Return the standard CSS for the PDF report."""
        return """
        body { font-family: Helvetica, Arial, sans-serif; color: #2c3e50; line-height: 1.6; }
        h1 { color: #2c3e50; text-align: center; border-bottom: 3px solid #3498db; }
        h2 { color: #2980b9; border-bottom: 1px solid #bdc3c7; margin-top: 25px; }
        p { margin-bottom: 10px; text-align: justify; }
        a { color: #3498db; text-decoration: underline; font-weight: bold; }
        ul { padding-left: 20px; }
        code { background-color: #f1f3f5; padding: 2px 4px; border-radius: 4px; color: #e74c3c; }
        """

    def render_from_markdown(
        self, markdown_text: str, filename: str, output_dir: str | None = None
    ) -> str:
        """Render a raw Markdown string to a professional PDF report."""
        target_dir = output_dir or self.output_dir
        output_path = os.path.join(target_dir, filename)
        os.makedirs(target_dir, exist_ok=True)
        markdown_text = self._normalize_headers(markdown_text)

        try:
            pdf = MarkdownPdf()
            pdf.meta["title"] = "Daily Digest Report"
            pdf.meta["author"] = "Telebot"

            clean_sections = self._split_sections(markdown_text)
            css = self._get_css()

            if not clean_sections:
                pdf.add_section(Section(markdown_text, toc=False, root="."), user_css=css)
            else:
                for idx, sect_text in enumerate(clean_sections):
                    cnt = f"{idx + 1}/{len(clean_sections)}"
                    logger.debug(f"Adding section {cnt} (Length: {len(sect_text)})")
                    pdf.add_section(Section(sect_text, toc=True, root="."), user_css=css)

            pdf.save(output_path)
            logger.info(f"PDF generated at {output_path} with {len(clean_sections)} sections")
            return output_path
        except Exception as e:
            logger.error(f"Failed to generate PDF: {e}", exc_info=True)
            return f"Error: {str(e)}"

    def _split_sections(self, md_text: str) -> list[str]:
        """Split markdown into sections by # or ## headers."""
        parts = re.split(r"(?m)^(#{1,2}\s.*)$", md_text)
        clean = []
        curr = ""
        for p in parts:
            if not p.strip():
                continue
            if re.match(r"^#{1,2}\s", p):
                if curr.strip():
                    clean.append(curr.strip())
                curr = p
            else:
                curr += "\n" + p
        if curr.strip():
            clean.append(curr.strip())
        return clean
