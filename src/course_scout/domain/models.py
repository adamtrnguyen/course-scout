import datetime

from pydantic import BaseModel, Field


class DigestItem(BaseModel):
    """A single item in the digest (course, file, discussion, etc.)."""

    title: str = Field(..., description="Title or name of the item")
    description: str = Field(..., description="Brief description")
    category: str = Field(
        ..., description="Category: 'course', 'file', 'discussion', 'request', 'announcement'"
    )
    links: list[str] = Field(default_factory=list, description="Related URLs")
    author: str | None = Field(None, description="Author or source if known")


class LinkItem(BaseModel):
    title: str
    url: str


class TelegramMessage(BaseModel):
    id: int
    text: str | None  # Text can be empty if it's just media
    date: datetime.datetime
    author: str | None = None
    link: str
    reply_to_id: int | None = None
    forward_from_chat: str | None = None
    forward_from_author: str | None = None
    local_media_path: str | None = None


class ChannelDigest(BaseModel):
    channel_name: str
    date: datetime.date
    summaries: list[str]  # Used for the executive summary
    items: list[DigestItem] = Field(default_factory=list)
    key_links: list[LinkItem] = Field(default_factory=list)

    def to_markdown(self) -> str:
        """Standardized markdown representation of the digest."""
        md = f"# Daily Digest: {self.channel_name}\n"
        md += f"**Date**: {self.date}\n\n"

        md = self._add_executive_summary(md)
        md = self._add_categorized_items(md)
        md = self._add_key_links(md)

        return md.strip()

    def _add_executive_summary(self, md: str) -> str:
        """Add the executive summary section to markdown."""
        if self.summaries:
            if not self.summaries[0].startswith("##"):
                md += "## 📝 Executive Summary\n\n"
            for s in self.summaries:
                md += f"{s}\n\n"
        return md

    def _add_categorized_items(self, md: str) -> str:
        """Add the categorized items sections to markdown."""
        categories = {
            "course": "## 🎓 Courses & Tutorials",
            "file": "## 📂 Files Shared",
            "discussion": "## 🗣 Discussions",
            "request": "## 🙋 Requests",
            "announcement": "## 📢 Announcements",
        }
        for cat_key, cat_title in categories.items():
            cat_items = [i for i in self.items if i.category == cat_key]
            if cat_items:
                md += f"{cat_title}\n\n"
                for item in cat_items:
                    if item.links:
                        links_md = ", ".join(f"[link]({url})" for url in item.links)
                        md += f"- **{item.title}** ({links_md}): {item.description}\n"
                    else:
                        md += f"- **{item.title}**: {item.description}\n"
                md += "\n"
        return md

    def _add_key_links(self, md: str) -> str:
        """Add the key links section to markdown."""
        if self.key_links:
            md += "## 🔗 Key Links\n\n"
            for link in self.key_links:
                md += f"- [{link.title}]({link.url})\n"
            md += "\n"
        return md
