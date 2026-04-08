"""Tests for RawDigestItem → domain conversion and JSON string field parsing."""

import json

from course_scout.domain.models import (
    AnnouncementItem,
    CourseItem,
    DiscussionItem,
    FileItem,
    RequestItem,
)
from course_scout.infrastructure.agents import RawDigestItem, SummarizerOutputSchema


class TestRawDigestItemConversion:
    def test_to_course(self):
        raw = RawDigestItem(
            title="Krenz Color", description="Beginner course", category="course",
            instructor="Krenz", platform="Coloso", status="FULFILLED", priority="HIGH",
            password="abc", msg_ids=[1, 2],
        )
        item = raw.to_domain()
        assert isinstance(item, CourseItem)
        assert item.instructor == "Krenz"
        assert item.platform == "Coloso"
        assert item.password == "abc"

    def test_to_file(self):
        raw = RawDigestItem(
            title="Pack", description="Archive", category="file",
            password="wf6g",
        )
        item = raw.to_domain()
        assert isinstance(item, FileItem)
        assert item.password == "wf6g"

    def test_to_discussion(self):
        raw = RawDigestItem(
            title="SAI vs CSP", description="Tool debate", category="discussion",
            instructor="Kalen Chock", platform="Coloso",  # platform should be ignored
        )
        item = raw.to_domain()
        assert isinstance(item, DiscussionItem)
        assert item.instructor == "Kalen Chock"
        assert not hasattr(item, "platform")

    def test_to_request(self):
        raw = RawDigestItem(
            title="Some Course", description="Want it", category="request",
            status="UNFULFILLED", priority="LOW",
        )
        item = raw.to_domain()
        assert isinstance(item, RequestItem)
        assert item.status == "UNFULFILLED"

    def test_to_announcement(self):
        raw = RawDigestItem(
            title="Event", description="Live stream", category="announcement",
        )
        item = raw.to_domain()
        assert isinstance(item, AnnouncementItem)

    def test_unknown_category_defaults_to_course(self):
        raw = RawDigestItem(
            title="Unknown", description="x", category="unknown_type",
        )
        item = raw.to_domain()
        assert isinstance(item, CourseItem)


class TestSummarizerOutputSchemaJsonParsing:
    def test_parse_json_string_items(self):
        """Claude SDK sometimes returns list fields as JSON strings."""
        data = {
            "items": json.dumps([
                {"title": "T1", "description": "D1", "category": "course"},
            ]),
            "key_links": json.dumps([
                {"title": "L1", "url": "http://example.com"},
            ]),
        }
        schema = SummarizerOutputSchema.model_validate(data)
        assert len(schema.items) == 1
        assert schema.items[0].title == "T1"
        assert len(schema.key_links) == 1

    def test_parse_normal_list_items(self):
        """Normal case — lists are already parsed."""
        data = {
            "items": [
                {"title": "T1", "description": "D1", "category": "file"},
            ],
            "key_links": [],
        }
        schema = SummarizerOutputSchema.model_validate(data)
        assert len(schema.items) == 1

    def test_to_domain_items(self):
        schema = SummarizerOutputSchema(
            items=[
                RawDigestItem(title="C", description="x", category="course"),
                RawDigestItem(title="D", description="x", category="discussion"),
            ],
            key_links=[],
        )
        domain = schema.to_domain_items()
        assert isinstance(domain[0], CourseItem)
        assert isinstance(domain[1], DiscussionItem)

    def test_empty_items(self):
        schema = SummarizerOutputSchema(items=[], key_links=[])
        assert schema.to_domain_items() == []
