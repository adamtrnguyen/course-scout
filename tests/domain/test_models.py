from datetime import datetime

from course_scout.domain.models import (
    AnnouncementItem,
    ChannelDigest,
    CourseItem,
    DiscussionItem,
    FileItem,
    LinkItem,
    RequestItem,
    TelegramMessage,
)


# ── TelegramMessage ──


def test_telegram_message_creation():
    msg = TelegramMessage(
        id=1,
        text="Hello world",
        date=datetime.now(),
        author="Test User",
        link="https://t.me/c/123/1",
    )
    assert msg.id == 1
    assert msg.text == "Hello world"
    assert msg.author == "Test User"


def test_telegram_message_optional_fields():
    msg = TelegramMessage(
        id=2, text=None, date=datetime.now(), link="https://t.me/c/123/2", reply_to_id=1
    )
    assert msg.text is None
    assert msg.reply_to_id == 1
    assert msg.local_media_path is None


# ── Discriminated DigestItem types ──


def test_course_item():
    item = CourseItem(
        title="Krenz Color", description="Beginner color course",
        instructor="Krenz", platform="Coloso", status="FULFILLED", priority="HIGH",
        msg_ids=[100, 101], links=["https://coloso.global/krenz"],
    )
    assert item.category == "course"
    assert item.instructor == "Krenz"
    assert item.platform == "Coloso"


def test_file_item_with_password():
    item = FileItem(
        title="Anatomy Pack", description="Split archive, 5 parts",
        platform="Baidu Pan", password="wf6g", status="FULFILLED", priority="HIGH",
        msg_ids=[200],
    )
    assert item.category == "file"
    assert item.password == "wf6g"


def test_discussion_item_no_platform():
    item = DiscussionItem(
        title="SAI vs CSP", description="Tool comparison thread",
        instructor="Kalen Chock", priority="MEDIUM", msg_ids=[300, 301],
    )
    assert item.category == "discussion"
    assert not hasattr(item, "platform")
    assert not hasattr(item, "password")


def test_request_item():
    item = RequestItem(
        title="Painting Light 102", description="Proko course",
        instructor="Jeremy Vickery", platform="Proko", status="UNFULFILLED", priority="LOW",
    )
    assert item.category == "request"
    assert item.status == "UNFULFILLED"


def test_announcement_item():
    item = AnnouncementItem(
        title="New Course Drop", description="Available now",
        priority="MEDIUM",
    )
    assert item.category == "announcement"


# ── Rendering ──


def test_course_item_render_links():
    item = CourseItem(
        title="Figure Sculpting", description="10-part archive",
        instructor="Logan", platform="Telegram", status="FULFILLED", priority="HIGH",
        links=["https://flippednormals.com/product/123", "https://t.me/c/160/3018/100"],
    )
    md = item.render()
    assert "🔥" in md
    assert "**Figure Sculpting**" in md
    assert "[course](https://flippednormals.com/product/123)" in md
    assert "[post](https://t.me/c/160/3018/100)" in md
    assert "Logan · Telegram · FULFILLED" in md


def test_file_item_render_password():
    item = FileItem(
        title="Color Pack", description="PDF handouts",
        password="abc123", priority="HIGH",
    )
    md = item.render()
    assert "**pwd: abc123**" in md


def test_discussion_item_render_thread_and_msgs():
    item = DiscussionItem(
        title="Tool Comparison", description="SAI vs CSP debate",
        priority="MEDIUM",
        links=[
            "https://t.me/c/160/3077/500",
            "https://t.me/c/160/3077/501",
            "https://t.me/c/160/3077/502",
        ],
    )
    md = item.render()
    assert "[thread](https://t.me/c/160/3077/500)" in md
    assert "msgs:" in md
    assert "[#500]" in md
    assert "[#501]" in md
    assert "[#502]" in md


def test_request_item_render_low_priority():
    item = RequestItem(
        title="Some Course", description="Unfulfilled",
        status="UNFULFILLED", priority="LOW",
    )
    md = item.render()
    assert "↓" in md


# ── ChannelDigest ──


def test_channel_digest_creation():
    digest = ChannelDigest(
        channel_name="Test Channel",
        date=datetime.now().date(),
        summaries=["Summary 1", "Summary 2"],
        key_links=[LinkItem(title="Example", url="http://example.com")],
    )
    assert digest.channel_name == "Test Channel"
    assert len(digest.summaries) == 2
    assert len(digest.key_links) == 1


def test_channel_digest_to_markdown():
    digest = ChannelDigest(
        channel_name="Test Header",
        date=datetime(2025, 1, 1).date(),
        summaries=["## Sub-summary\nDetail 1", "Points 2"],
        key_links=[LinkItem(title="Title", url="http://link.com")],
    )
    md = digest.to_markdown()

    assert "# Daily Digest: Test Header" in md
    assert "**Date**: 2025-01-01" in md
    assert "## Sub-summary" in md
    assert "## 🔗 Key Links" in md
    assert "- [Title](http://link.com)" in md


def test_channel_digest_no_duplicate_summary_header():
    digest = ChannelDigest(
        channel_name="Test",
        date=datetime(2025, 1, 1).date(),
        summaries=["## 📝 Executive Summary\nAlready here"],
        key_links=[],
    )
    md = digest.to_markdown()
    assert md.count("## 📝 Executive Summary") == 1


def test_channel_digest_priority_sorting():
    items = [
        RequestItem(title="Low", description="x", status="UNFULFILLED", priority="LOW"),
        CourseItem(title="High", description="x", priority="HIGH"),
        FileItem(title="Medium", description="x", priority="MEDIUM"),
    ]
    digest = ChannelDigest(
        channel_name="Test", date=datetime(2025, 1, 1).date(),
        summaries=[], items=items, key_links=[],
    )
    md = digest.to_markdown()
    # Courses section should list High before others
    # But these are in different sections, so check request section ordering
    # All are different categories — let's test with same category
    items2 = [
        RequestItem(title="Low Req", description="x", priority="LOW"),
        RequestItem(title="High Req", description="x", priority="HIGH"),
        RequestItem(title="Med Req", description="x", priority="MEDIUM"),
    ]
    digest2 = ChannelDigest(
        channel_name="Test", date=datetime(2025, 1, 1).date(),
        summaries=[], items=items2, key_links=[],
    )
    md2 = digest2.to_markdown()
    high_pos = md2.index("High Req")
    med_pos = md2.index("Med Req")
    low_pos = md2.index("Low Req")
    assert high_pos < med_pos < low_pos


def test_channel_digest_categorized_sections():
    items = [
        FileItem(title="F1", description="File", links=["http://f"]),
        DiscussionItem(title="D1", description="Disc"),
        RequestItem(title="R1", description="Req"),
    ]
    digest = ChannelDigest(
        channel_name="Test", date=datetime(2025, 1, 1).date(),
        summaries=[], items=items, key_links=[],
    )
    md = digest.to_markdown()
    assert "## 📂 Files Shared" in md
    assert "## 🗣 Discussions" in md
    assert "## 🙋 Requests" in md
