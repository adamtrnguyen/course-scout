import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


# ── Link helpers ──


def _split_links(links: list[str]) -> tuple[str | None, list[str]]:
    """Split links into (first external URL, telegram links)."""
    external = next((u for u in links if "t.me/" not in u), None)
    telegram = [u for u in links if "t.me/" in u]
    return external, telegram


def _tg_msg_refs(tg_links: list[str]) -> str:
    """Format telegram links as clickable msg refs: [#123](url), [#456](url)."""
    refs = []
    for url in tg_links:
        mid = url.rstrip("/").split("/")[-1]
        refs.append(f"[#{mid}]({url})")
    return ", ".join(refs)


# ── Priority markers ──

_PRIORITY_MARKER = {"HIGH": "🔥", "LOW": "↓"}


def _priority_prefix(priority: str | None) -> str:
    marker = _PRIORITY_MARKER.get(priority or "", "")
    return f"{marker} " if marker else ""


# ── Digest item types ──

_DESC_HELP = (
    "Telegraphic notes — key facts only, no narrative filler. "
    "No 'A member asked...', no 'It was noted that...'. "
    "Just: what it is, what's useful, actionable details."
)

_PRIORITY_HELP = (
    "HIGH = downloadable course/file with link. "
    "MEDIUM = review, technique discussion, or fulfilled request. "
    "LOW = unfulfilled request, off-topic, or 3D/photo/UI."
)


class _ActionableItem(BaseModel):
    """Base for items with course/platform/status metadata (course, file, request, announcement)."""

    title: str = Field(..., description="Exact course/file/topic name.")
    description: str = Field(..., description=_DESC_HELP)
    msg_ids: list[int] = Field(default_factory=list, description="Source message IDs")
    links: list[str] = Field(default_factory=list, description="Related URLs")
    author: str | None = Field(None, description="Who posted it (Telegram username)")
    instructor: str | None = Field(None, description="Course instructor or artist name")
    platform: str | None = Field(
        None, description="Coloso, Domestika, Baidu Pan, Quark Pan, Proko, etc."
    )
    status: str | None = Field(None, description="FULFILLED, UNFULFILLED, or DISCUSSING")
    priority: str | None = Field(None, description=_PRIORITY_HELP)
    password: str | None = Field(None, description="Download password, preserved exactly as written")

    def render(self) -> str:
        """Render as: [marker] **Title** — meta ([course](url) · [post](url))  desc"""
        course_url, tg_links = _split_links(self.links)
        first_post = tg_links[0] if tg_links else None

        # Title line
        meta = [x for x in (self.instructor, self.platform, self.status) if x]
        link_parts = []
        if course_url:
            link_parts.append(f"[course]({course_url})")
        if first_post:
            link_parts.append(f"[post]({first_post})")

        line = f"- {_priority_prefix(self.priority)}**{self.title}**"
        if meta:
            line += f" — {' · '.join(meta)}"
        if link_parts:
            line += f" ({' · '.join(link_parts)})"

        # Description
        desc = self.description
        if self.password:
            desc = f"**pwd: {self.password}** — {desc}"

        return f"{line}\n  {desc}\n\n"


class CourseItem(_ActionableItem):
    category: Literal["course"] = "course"


class FileItem(_ActionableItem):
    category: Literal["file"] = "file"


class RequestItem(_ActionableItem):
    category: Literal["request"] = "request"


class AnnouncementItem(_ActionableItem):
    category: Literal["announcement"] = "announcement"


class DiscussionItem(BaseModel):
    """A discussion thread — technique breakdowns, reviews, debates."""

    category: Literal["discussion"] = "discussion"
    title: str = Field(..., description="Discussion topic name.")
    description: str = Field(..., description=_DESC_HELP)
    msg_ids: list[int] = Field(default_factory=list, description="Source message IDs")
    links: list[str] = Field(default_factory=list, description="Related URLs")
    author: str | None = Field(None, description="Who posted it (Telegram username)")
    instructor: str | None = Field(None, description="Artist/instructor discussed (if any)")
    priority: str | None = Field(None, description=_PRIORITY_HELP)

    def render(self) -> str:
        """Render as: **Title** — Instructor ([thread])  desc  msgs: [#1], [#2]"""
        _, tg_links = _split_links(self.links)
        first_post = tg_links[0] if tg_links else None

        # Title line
        meta = [x for x in (self.instructor,) if x]
        line = f"- {_priority_prefix(self.priority)}**{self.title}**"
        if meta:
            line += f" — {' · '.join(meta)}"
        if first_post:
            line += f" ([thread]({first_post}))"

        # Description + msg refs below
        lines = [line, f"  {self.description}"]
        if tg_links:
            lines.append(f"  msgs: {_tg_msg_refs(tg_links)}")
        lines.append("")
        return "\n".join(lines) + "\n"


# Discriminated union — Pydantic picks the right type based on `category` field
DigestItem = Annotated[
    Union[CourseItem, FileItem, DiscussionItem, RequestItem, AnnouncementItem],
    Field(discriminator="category"),
]


# ── Other domain models ──


class LinkItem(BaseModel):
    title: str
    url: str


class TelegramMessage(BaseModel):
    id: int
    text: str | None
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
    summaries: list[str]
    items: list[DigestItem] = Field(default_factory=list)
    key_links: list[LinkItem] = Field(default_factory=list)

    def to_markdown(self) -> str:
        md = f"# Daily Digest: {self.channel_name}\n"
        md += f"**Date**: {self.date}\n\n"
        if self.summaries:
            if not self.summaries[0].startswith("##"):
                md += "## 📝 Executive Summary\n\n"
            for s in self.summaries:
                md += f"{s}\n\n"
        md = self._add_categorized_items(md)
        if self.key_links:
            md += "## 🔗 Key Links\n\n"
            for link in self.key_links:
                md += f"- [{link.title}]({link.url})\n"
            md += "\n"
        return md.strip()

    def _add_categorized_items(self, md: str) -> str:
        priority_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        sections = {
            "course": "## 🎓 Courses & Tutorials",
            "file": "## 📂 Files Shared",
            "discussion": "## 🗣 Discussions",
            "request": "## 🙋 Requests",
            "announcement": "## 📢 Announcements",
        }
        for cat_key, heading in sections.items():
            cat_items = [i for i in self.items if i.category == cat_key]
            if cat_items:
                cat_items.sort(key=lambda x: priority_order.get(x.priority or "", 2))
                md += f"{heading}\n\n"
                for item in cat_items:
                    md += item.render()
        return md
