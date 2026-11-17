import zipfile

from pathlib import Path

from pipeline.expansion import is_archive, expand_archives


def test_is_archive_case_insensitive():
    assert is_archive("docs/a.ZIP") is True
    assert is_archive("docs/a.zip") is True
    assert is_archive("docs/a.txt") is False


def test_plain_files_pass_through(tmp_path):
    root = tmp_path
    (root / "docs").mkdir()
    (root / "docs" / "a.txt").write_text("hello")

    work = tmp_path / "work"
    result = expand_archives(root, ["docs/a.txt"], work)

    assert result["plain"] == ["docs/a.txt"]
    assert result["members"] == []
    assert result["errors"] == []
    assert result["complete"] is True


def test_simple_zip_member(tmp_path):
    root = tmp_path
    (root / "docs").mkdir()
    zip_path = root / "docs" / "a.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("inner/b.md", "body content")

    work = tmp_path / "work"
    result = expand_archives(root, ["docs/a.zip"], work)

    assert result["plain"] == []
    assert result["complete"] is True
    assert result["errors"] == []
    assert len(result["members"]) == 1

    member = result["members"][0]
    assert member["path"] == "docs/a.zip!/inner/b.md"
    assert member["root"] == str((work / "a000").resolve())
    assert member["rel"] == "inner/b.md"

    on_disk = Path(member["root"]) / member["rel"]
    assert on_disk.read_text() == "body content"


def test_nested_zip(tmp_path):
    root = tmp_path
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("c.txt", "deep")

    zip_path = root / "a.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("b.zip", inner.read_bytes())

    work = tmp_path / "work"
    result = expand_archives(root, ["a.zip"], work)

    assert result["complete"] is True
    paths = [m["path"] for m in result["members"]]
    assert paths == ["a.zip!/b.zip!/c.txt"]
    # b.zip itself must not appear as a leaf member.
    assert all("b.zip" not in p or p.endswith("/c.txt") for p in paths)


def test_two_zips_same_inner_name(tmp_path):
    root = tmp_path
    (root / "d1").mkdir()
    (root / "d2").mkdir()

    z1 = root / "d1" / "x.zip"
    with zipfile.ZipFile(z1, "w") as zf:
        zf.writestr("same.txt", "one")

    z2 = root / "d2" / "y.zip"
    with zipfile.ZipFile(z2, "w") as zf:
        zf.writestr("same.txt", "two")

    work = tmp_path / "work"
    result = expand_archives(root, ["d1/x.zip", "d2/y.zip"], work)

    assert result["complete"] is True
    assert len(result["members"]) == 2
    paths = sorted(m["path"] for m in result["members"])
    assert paths == ["d1/x.zip!/same.txt", "d2/y.zip!/same.txt"]
    roots = {m["root"] for m in result["members"]}
    assert len(roots) == 2


def test_zip_slip_member_rejected(tmp_path):
    root = tmp_path
    zip_path = root / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../evil.txt", "traversal")

    work = tmp_path / "work"
    result = expand_archives(root, ["evil.zip"], work)

    assert result["complete"] is False
    assert result["errors"]
    assert result["members"] == []
    assert not (tmp_path / "evil.txt").exists()


def test_max_file_bytes_rejects_oversized(tmp_path):
    root = tmp_path
    zip_path = root / "big.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("member.bin", b"x" * 100)

    work = tmp_path / "work"
    result = expand_archives(
        root, ["big.zip"], work, max_file_bytes=10
    )

    assert result["complete"] is False
    assert result["errors"]
    assert result["members"] == []


def test_invalid_zip_file(tmp_path):
    root = tmp_path
    (root / "fake.zip").write_text("not a zip at all")

    work = tmp_path / "work"
    result = expand_archives(root, ["fake.zip"], work)

    assert result["complete"] is False
    assert result["errors"]
    assert "fake.zip" in result["errors"][0]
    assert result["members"] == []

def test_archive_path_escaping_the_source_root_is_refused(tmp_path):
    outside = tmp_path / "outside.zip"
    with zipfile.ZipFile(outside, "w") as archive:
        archive.writestr("x.txt", "secret")
    root = tmp_path / "root"
    root.mkdir()
    result = expand_archives(root, ["../outside.zip"], tmp_path / "work")
    assert result["members"] == []
    assert result["complete"] is False
    assert "../outside.zip" in result["errors"][0]
