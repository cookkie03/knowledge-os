import ingest


def test_ordered_categories_prioritizes_text_before_audio():
    plan = [
        {"category": "audio", "needs_render": True, "needs_insert": True},
        {"category": "pdf", "needs_render": True, "needs_insert": True},
        {"category": "text", "needs_render": True, "needs_insert": True},
        {"category": "office", "needs_render": True, "needs_insert": True},
        {"category": "images", "needs_render": True, "needs_insert": True},
    ]

    assert ingest._ordered_categories(plan) == ["text", "office", "images", "pdf", "audio"]


def test_ingest_log_print_and_log_include_timestamps(capsys):
    import re

    ingest._log_print("test message")
    out = capsys.readouterr().out
    assert re.search(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] test message\n$", out)

    ingest._log("✓ render", "docs/test.md", "text")
    out2 = capsys.readouterr().out
    assert re.search(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ✓ render docs/test\.md — text\n$", out2)
