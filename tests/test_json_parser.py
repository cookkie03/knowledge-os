"""Structural JSON/JSONL parsing (ticket T6)."""

from __future__ import annotations

import json

from pipeline.json_parser import (
    JSON_PARSER_VERSION,
    convert_json_text,
    convert_jsonl_text,
    detect_and_parse_json,
)
from pipeline.parsers import parse_document


def _chatgpt_export():
    return [{
        "id": "conv-1",
        "title": "Tailwind setup",
        "create_time": 1700000000,
        "update_time": 1700000100,
        "current_node": "n3",
        "mapping": {
            "n0": {"id": "n0", "parent": None, "message": None},
            "n1": {"id": "n1", "parent": "n0", "message": {
                "id": "m-sys", "author": {"role": "system"}, "content": {"parts": ["hidden"]}}},
            "n2": {"id": "n2", "parent": "n1", "message": {
                "id": "m-user", "author": {"role": "user"}, "create_time": 1700000010,
                "content": {"parts": ["How do I install it?"]}}},
            "n3": {"id": "n3", "parent": "n2", "message": {
                "id": "m-bot", "author": {"role": "assistant"}, "create_time": 1700000020,
                "metadata": {"model_slug": "gpt-x"}, "content": {"parts": ["Run npm install."]}}},
        },
    }]


def test_chatgpt_export_keeps_roles_order_timestamps_and_ids():
    result = convert_json_text(json.dumps(_chatgpt_export()), "conversations.json")
    markdown = result["markdown"]
    assert result["structure"] == "chatgpt"
    assert "structure: chatgpt" in markdown
    assert "- id: conv-1" in markdown
    assert markdown.index("### User") < markdown.index("### Assistant")
    assert "id: m-user" in markdown and "id: m-bot" in markdown
    assert "Model: gpt-x" in markdown
    assert "2023-11-14T22:13:30+00:00" in markdown
    assert "hidden" not in markdown


def test_claude_export_is_recognised_by_structure():
    export = [{"uuid": "u1", "name": "Plan", "created_at": "2024-01-01T00:00:00Z", "chat_messages": [
        {"uuid": "a", "sender": "human", "text": "hello", "created_at": "2024-01-01T00:00:01Z"},
        {"uuid": "b", "sender": "assistant", "text": "hi there", "created_at": "2024-01-01T00:00:02Z"},
    ]}]
    result = convert_json_text(json.dumps(export), "claude.json")
    assert result["structure"] == "claude"
    assert "### User (2024-01-01T00:00:01Z, id: a)" in result["markdown"]
    assert "hi there" in result["markdown"]


def test_generic_json_is_rendered_without_truncation():
    long_text = "x" * 20000
    data = {"name": "config", "enabled": True, "missing": None, "nested": {"items": [1, 2, {"deep": long_text}]}}
    result = convert_json_text(json.dumps(data), "config.json")
    markdown = result["markdown"]
    assert result["structure"] == "generic_json"
    assert long_text in markdown
    assert "- **enabled**: true" in markdown
    assert "- **missing**: null" in markdown
    # The raw JSON must not simply be pasted under a frontmatter.
    assert '"enabled"' not in markdown


def test_multiline_string_values_stay_under_their_key():
    result = convert_json_text(json.dumps({"note": "line one\nline two"}), "n.json")
    assert "- **note**:\n  line one\n  line two" in result["markdown"]


def test_notion_blocks_become_headings_and_lists():
    export = {"object": "list", "results": [
        {"type": "heading_1", "heading_1": {"rich_text": [{"plain_text": "Title"}]}},
        {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"plain_text": "point"}]}},
    ]}
    result = convert_json_text(json.dumps(export), "notion.json")
    assert result["structure"] == "notion"
    assert "# Title" in result["markdown"] and "- point" in result["markdown"]


def test_output_is_deterministic():
    text = json.dumps({"a": [1, 2, 3], "b": "x"})
    first = convert_json_text(text, "a.json")["markdown"]
    second = convert_json_text(text, "a.json")["markdown"]
    assert first == second
    # Notion/generic ids come from content, not from the clock.
    assert detect_and_parse_json(text)[0]["id"] == detect_and_parse_json(text)[0]["id"]


def test_cyclic_chatgpt_parent_chain_terminates():
    export = _chatgpt_export()
    export[0]["mapping"]["n1"]["parent"] = "n3"
    result = convert_json_text(json.dumps(export), "loop.json")
    assert result is not None


def test_invalid_json_returns_none_for_text_fallback():
    assert convert_json_text('{"broken": ', "bad.json") is None


def test_jsonl_is_rendered_record_by_record_and_keeps_bad_lines():
    text = '{"id": 1, "msg": "first"}\n\nnot json\n{"id": 2, "msg": "second"}\n'
    result = convert_jsonl_text(text, "log.jsonl")
    markdown = result["markdown"]
    assert result["records"] == 2 and result["invalid_lines"] == 1
    assert "## Record 1" in markdown and "## Record 4" in markdown
    assert "## Line 3 (invalid JSON)" in markdown and "not json" in markdown
    assert markdown.index("first") < markdown.index("second")
    assert "invalid_lines: 1" in markdown


def test_parse_document_routes_json_and_records_parser_version(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"k": "v"}), encoding="utf-8")
    parsed = parse_document(path)
    assert parsed["category"] == "text"
    assert parsed["parser_name"] == "json_structural"
    assert parsed["parser_version"] == JSON_PARSER_VERSION
    assert parsed["text_content"].startswith("---\nsource_file: \"data.json\"")
    assert "- **k**: v" in parsed["text_content"]
    assert parsed["metadata"]["json_valid"] is True
    assert parsed["derived_hash"] == parse_document(path)["derived_hash"]


def test_parse_document_falls_back_to_text_for_invalid_json(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"a": 1,}', encoding="utf-8")
    parsed = parse_document(path)
    assert parsed["parser_name"] == "text_plain"
    assert parsed["text_content"] == '{"a": 1,}'
    assert parsed["metadata"]["json_valid"] is False


def test_parse_document_routes_jsonl(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
    parsed = parse_document(path)
    assert parsed["metadata"]["records"] == 2
    assert "## Record 2" in parsed["text_content"]


def test_markdown_files_are_untouched(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("# hello", encoding="utf-8")
    assert parse_document(path)["text_content"] == "# hello"
