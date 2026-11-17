"""Structural JSON and JSONL to Markdown conversion (ticket T6).

Pure functions: text in, Markdown out. Conversation exports (ChatGPT, Claude,
generic message lists), Notion exports and arbitrary JSON are recognised by
structure and rendered as readable Markdown with roles, order, timestamps and
ids preserved and nothing truncated. JSONL is rendered record by record.

Determinism is a hard requirement: the derived document is identified by the
hash of its Markdown plus JSON_PARSER_VERSION, so the same input must always
produce byte-identical output. Nothing here reads the clock or random state.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

# Bump when the rendering changes, so derived artifacts are regenerated.
JSON_PARSER_VERSION = "json-v2"


# Format an epoch timestamp as ISO 8601 UTC, or "" when it is missing/invalid.
def _iso_from_epoch(value: Any) -> str:
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


# Build a conversation record as a plain dict.
def _new_conversation(conv_id: str, title: str, created_at: str, updated_at: str, source: str) -> dict[str, Any]:
    return {"id": conv_id, "title": title, "created_at": created_at, "updated_at": updated_at,
            "source": source, "messages": []}


# Build a message record as a plain dict.
def _new_message(role: str, content: str, timestamp: str | None = None, model: str | None = None,
                 message_id: str | None = None) -> dict[str, Any]:
    return {"role": role, "content": content, "timestamp": timestamp, "model": model, "id": message_id}


# Content-derived id for documents that carry none, so ids stay deterministic.
def _content_id(prefix: str, body: str) -> str:
    return "{}_{}".format(prefix, hashlib.sha256(body.encode("utf-8")).hexdigest()[:12])


# Render any JSON value as Markdown bullet lines without losing or cutting data.
# Multi-line strings are indented under their key so the list structure survives.
def json_to_markdown_lines(obj: Any, depth: int = 1) -> list[str]:
    lines = []
    indent = "  " * (depth - 1)
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, (dict, list)):
                lines.append("{}- **{}**:".format(indent, key))
                lines.extend(json_to_markdown_lines(value, depth + 1))
            elif isinstance(value, str) and "\n" in value:
                lines.append("{}- **{}**:".format(indent, key))
                for text_line in value.split("\n"):
                    lines.append("{}  {}".format(indent, text_line))
            else:
                lines.append("{}- **{}**: {}".format(indent, key, _scalar(value)))
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            if isinstance(item, (dict, list)):
                lines.append("{}- Item {}:".format(indent, index + 1))
                lines.extend(json_to_markdown_lines(item, depth + 1))
            else:
                lines.append("{}- {}".format(indent, _scalar(item)))
    else:
        lines.append("{}{}".format(indent, _scalar(obj)))
    return lines


# Render a scalar the way JSON spells it (true/false/null), strings as-is.
def _scalar(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


# Walk a ChatGPT mapping tree from current_node back to the root and return the
# active linear thread in chronological order.
def _chatgpt_active_nodes(item: dict[str, Any]) -> list[dict[str, Any]]:
    mapping = item.get("mapping", {})
    nodes = []
    seen = set()
    current = item.get("current_node")
    # The seen set guards against a cyclic parent chain in a malformed export.
    while current and current in mapping and current not in seen:
        seen.add(current)
        node = mapping[current]
        nodes.append(node)
        current = node.get("parent")
    nodes.reverse()
    return nodes


# Parse ChatGPT conversations.json items into conversation dicts.
def parse_chatgpt_conversations(json_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conversations = []
    for item in json_data:
        conv_id = item.get("id") or item.get("conversation_id") or "unknown"
        created_at = _iso_from_epoch(item.get("create_time"))
        updated_at = _iso_from_epoch(item.get("update_time")) or created_at
        conv = _new_conversation(conv_id, item.get("title") or "Chat senza titolo", created_at, updated_at, "chatgpt")
        for node in _chatgpt_active_nodes(item):
            msg = node.get("message")
            if not msg:
                continue
            role = msg.get("author", {}).get("role", "unknown")
            if role == "system":
                continue
            text_parts = []
            for part in msg.get("content", {}).get("parts", []):
                if isinstance(part, str):
                    text_parts.append(part)
                elif isinstance(part, dict) and "text" in part:
                    text_parts.append(str(part["text"]))
            content = "\n".join(text_parts).strip()
            if not content:
                continue
            conv["messages"].append(_new_message(
                role, content, _iso_from_epoch(msg.get("create_time")) or None,
                msg.get("metadata", {}).get("model_slug"), msg.get("id") or node.get("id")))
        if conv["messages"]:
            conversations.append(conv)
    return conversations


# Extract the text of one Claude-style message (plain text or content blocks).
def _claude_message_text(msg: dict[str, Any]) -> str:
    content = msg.get("text", "")
    if not content and "content" in msg:
        field = msg["content"]
        if isinstance(field, list):
            pieces = []
            for block in field:
                if isinstance(block, dict):
                    pieces.append(str(block.get("text", "")))
            content = "\n".join(pieces).strip()
        elif isinstance(field, str):
            content = field
    return content if isinstance(content, str) else ""


# Parse Claude export (and generic "messages" list) items into conversation dicts.
def parse_claude_conversations(json_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conversations = []
    for item in json_data:
        conv_id = item.get("uuid") or item.get("id") or "unknown"
        created_at = item.get("created_at") or ""
        updated_at = item.get("updated_at") or created_at
        title = item.get("name") or item.get("title") or "Chat Claude"
        conv = _new_conversation(conv_id, title, created_at, updated_at, "claude")
        raw_messages = item.get("chat_messages")
        if raw_messages is None:
            raw_messages = item.get("messages", [])
        for msg in raw_messages:
            if not isinstance(msg, dict):
                continue
            sender = msg.get("sender") or ("human" if msg.get("role") == "user" else "assistant")
            role = "user" if sender == "human" else "assistant"
            content = _claude_message_text(msg).strip()
            if not content:
                continue
            conv["messages"].append(_new_message(role, content, msg.get("created_at"), None,
                                                 msg.get("uuid") or msg.get("id")))
        if conv["messages"]:
            conversations.append(conv)
    return conversations


# Join the plain_text of Notion rich-text fragments.
def _notion_plain_text(fragments: Any) -> str:
    pieces = []
    if isinstance(fragments, list):
        for fragment in fragments:
            if isinstance(fragment, dict):
                pieces.append(fragment.get("plain_text", ""))
    return "".join(pieces)


# Render one Notion block as a Markdown line ("" when it carries no text).
def _notion_block_line(block: dict[str, Any]) -> str:
    block_type = block.get("type", "")
    content = block.get(block_type, {})
    text = ""
    if isinstance(content, dict) and "rich_text" in content:
        text = _notion_plain_text(content["rich_text"])
    elif isinstance(content, dict) and "title" in content:
        text = _notion_plain_text(content["title"])
    if not text and "plain_text" in block:
        text = str(block["plain_text"])
    if block_type.startswith("heading_1"):
        return "# {}".format(text)
    if block_type.startswith("heading_2"):
        return "## {}".format(text)
    if block_type.startswith("heading_3"):
        return "### {}".format(text)
    if block_type == "bulleted_list_item":
        return "- {}".format(text)
    if block_type == "numbered_list_item":
        return "1. {}".format(text)
    if block_type == "quote":
        return "> {}".format(text)
    return text


# Parse a Notion API block list or database export into a single document.
def parse_notion_export(data: Any) -> list[dict[str, Any]]:
    title = "Notion Export"
    raw_blocks = []
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            raw_blocks = data["results"]
        if "title" in data:
            title = str(data["title"])
    elif isinstance(data, list):
        raw_blocks = data
    lines = []
    for block in raw_blocks:
        if isinstance(block, dict):
            line = _notion_block_line(block)
            if line:
                lines.append(line)
    body = "\n\n".join(lines).strip()
    if not body:
        return []
    conv = _new_conversation(_content_id("notion", body), title, "", "", "notion")
    conv["messages"].append(_new_message("document", body))
    return [conv]


# Wrap arbitrary JSON as one structured document.
def parse_generic_json(raw_data: Any, title: str = "Generic JSON Export") -> list[dict[str, Any]]:
    body = "\n".join(json_to_markdown_lines(raw_data)).strip()
    if not body:
        return []
    conv = _new_conversation(_content_id("json", body), title, "", "", "generic_json")
    conv["messages"].append(_new_message("document", body))
    return [conv]


# Decide which known export format an already-parsed JSON value has and convert it.
# The first matching structure wins; anything unrecognised becomes a generic
# structured document, so no valid JSON is ever rejected.
def detect_and_parse(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict) and ("results" in raw or raw.get("object") in {"list", "page", "block"}):
        notion = parse_notion_export(raw)
        if notion:
            return notion

    data = raw
    if isinstance(data, dict):
        if isinstance(data.get("conversations"), list):
            data = data["conversations"]
        elif isinstance(data.get("turns"), list):
            data = data["turns"]
        else:
            data = [data]
    if not data or not isinstance(data, list) or not isinstance(data[0], dict):
        return parse_generic_json(raw)

    first = data[0]
    if "mapping" in first or "current_node" in first:
        found = parse_chatgpt_conversations(data)
    elif "chat_messages" in first or "uuid" in first or isinstance(first.get("messages"), list):
        found = parse_claude_conversations(data)
    elif "type" in first and ("rich_text" in first.get(first.get("type", ""), {}) or "object" in first):
        found = parse_notion_export(data)
    else:
        found = []
    # A recognised format that yielded no messages still falls back to the
    # generic renderer instead of silently producing an empty document.
    return found or parse_generic_json(raw)


# Same as detect_and_parse but starting from JSON text; invalid JSON gives [].
def detect_and_parse_json(content: str) -> list[dict[str, Any]]:
    try:
        raw = json.loads(content)
    except (ValueError, RecursionError):
        return []
    return detect_and_parse(raw)


# Render the message part of a conversation: a metadata list, then one section
# per message with role, timestamp, model and id in its header.
def conversation_body_markdown(conv: dict[str, Any]) -> list[str]:
    lines = ["# {}".format(conv["title"] or "Conversazione"), ""]
    lines.append("- id: {}".format(conv["id"]))
    lines.append("- source: {}".format(conv["source"]))
    if conv["created_at"]:
        lines.append("- created_at: {}".format(conv["created_at"]))
    if conv["updated_at"]:
        lines.append("- updated_at: {}".format(conv["updated_at"]))
    lines.append("")
    for msg in conv["messages"]:
        header = "### {}".format(msg["role"].capitalize())
        meta = []
        if msg.get("timestamp"):
            meta.append(msg["timestamp"])
        if msg.get("model"):
            meta.append("Model: {}".format(msg["model"]))
        if msg.get("id"):
            meta.append("id: {}".format(msg["id"]))
        if meta:
            header += " ({})".format(", ".join(meta))
        lines.append(header)
        lines.append("")
        lines.append(msg["content"].strip())
        lines.append("")
    return lines


# Render one conversation as a standalone Markdown document with YAML frontmatter.
# extra_frontmatter lets callers add their own keys (the archive importer marks
# imported chats as episodic memories) without a second renderer existing.
def conversation_to_markdown(conv: dict[str, Any], extra_frontmatter: list[str] | None = None) -> str:
    lines = ["---",
             "id: {}".format(json.dumps(conv["id"])),
             "title: {}".format(json.dumps(conv["title"])),
             "source: {}".format(json.dumps(conv["source"])),
             "created_at: {}".format(json.dumps(conv["created_at"])),
             "updated_at: {}".format(json.dumps(conv["updated_at"]))]
    for extra in extra_frontmatter or []:
        lines.append(extra)
    lines.append("---")
    lines.extend(conversation_body_markdown(conv))
    return "\n".join(lines).strip() + "\n"


# Frontmatter shared by every converted JSON/JSONL document.
def _document_frontmatter(source_name: str, source_format: str, structure: str, records: int,
                          invalid_lines: int) -> list[str]:
    lines = ["---",
             "source_file: {}".format(json.dumps(source_name)),
             "source_format: {}".format(source_format),
             "structure: {}".format(structure),
             "records: {}".format(records)]
    if invalid_lines:
        lines.append("invalid_lines: {}".format(invalid_lines))
    lines.append("parser_version: {}".format(JSON_PARSER_VERSION))
    lines.append("---")
    lines.append("")
    return lines


# Convert JSON text to Markdown. Returns None when the text is not valid JSON so
# the caller can fall back to plain-text handling instead of failing the file.
# Result: {"markdown", "structure", "records", "invalid_lines"}.
def convert_json_text(text: str, source_name: str) -> dict[str, Any] | None:
    try:
        raw = json.loads(text)
        conversations = detect_and_parse(raw)
    except (ValueError, RecursionError):
        return None
    structure = conversations[0]["source"] if conversations else "generic_json"
    lines = _document_frontmatter(source_name, "json", structure, len(conversations), 0)
    for conv in conversations:
        lines.extend(conversation_body_markdown(conv))
    return {"markdown": "\n".join(lines).strip() + "\n", "structure": structure,
            "records": len(conversations), "invalid_lines": 0}


# Convert JSONL text to Markdown, one section per record, in file order.
# A line that is not valid JSON is kept verbatim in a fenced block and counted,
# so a corrupt line never hides the other records and never loses its content.
# Result: {"markdown", "structure", "records", "invalid_lines"}.
def convert_jsonl_text(text: str, source_name: str) -> dict[str, Any]:
    sections = []
    records = 0
    invalid = 0
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            body = json_to_markdown_lines(record)
        except (ValueError, RecursionError):
            invalid += 1
            sections.append("## Line {} (invalid JSON)\n\n```\n{}\n```\n".format(number, line))
            continue
        records += 1
        sections.append("## Record {}\n\n{}\n".format(number, "\n".join(body)))
    lines = _document_frontmatter(source_name, "jsonl", "jsonl", records, invalid)
    lines.extend(sections)
    return {"markdown": "\n".join(lines).strip() + "\n", "structure": "jsonl",
            "records": records, "invalid_lines": invalid}
