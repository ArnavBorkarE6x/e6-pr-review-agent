"""Tests for AI client JSON extraction."""

from __future__ import annotations

import json

import pytest

from agent.ai_client import AIClient


class TestExtractJson:
    def test_plain_object(self):
        assert AIClient._extract_json('{"key": "value"}') == {"key": "value"}

    def test_array(self):
        assert AIClient._extract_json('[{"a": 1}]') == [{"a": 1}]

    def test_empty_array(self):
        assert AIClient._extract_json("[]") == []

    def test_markdown_json_fence(self):
        text = '```json\n{"key": "value"}\n```'
        assert AIClient._extract_json(text) == {"key": "value"}

    def test_plain_fence(self):
        text = "```\n[1, 2, 3]\n```"
        assert AIClient._extract_json(text) == [1, 2, 3]

    def test_whitespace_padding(self):
        assert AIClient._extract_json('  \n  {"x": 1}  \n  ') == {"x": 1}

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            AIClient._extract_json("not json at all")
