import pytest
import os
import zipfile
import shutil
from pathlib import Path
from pipeline.archive_parser import extract_archive

def test_normal_zip(tmp_path):
    # Create a normal zip
    zip_path = tmp_path / "normal.zip"
    staging_dir = tmp_path / "staging"

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("file1.txt", "hello world")
        zf.writestr("subdir/file2.txt", "nested content")

    result = extract_archive(zip_path, staging_dir)

    assert result["complete"] is True
    assert len(result["errors"]) == 0
    assert len(result["files"]) == 2

    # Verify content
    assert (staging_dir / "file1.txt").read_text() == "hello world"
    assert (staging_dir / "subdir/file2.txt").read_text() == "nested content"

    # Verify provenance
    provs = [f["provenance"] for f in result["files"]]
    assert "normal.zip!/file1.txt" in provs
    assert "normal.zip!/subdir/file2.txt" in provs

def test_zip_slip_attack(tmp_path):
    zip_path = tmp_path / "evil.zip"
    staging_dir = tmp_path / "staging"

    with zipfile.ZipFile(zip_path, "w") as zf:
        # Try to escape staging via ../
        zf.writestr("../evil.txt", "traversal")
        # Try absolute path
        zf.writestr("/etc/passwd_fake", "absolute")

    result = extract_archive(zip_path, staging_dir)

    assert result["complete"] is False
    assert any("Traversal attempt" in e or "Zip-Slip" in e for e in result["errors"])
    # Check that nothing was written outside staging
    assert not (tmp_path / "evil.txt").exists()

def test_symlink_attack(tmp_path):
    zip_path = tmp_path / "symlink.zip"
    staging_dir = tmp_path / "staging"

    with zipfile.ZipFile(zip_path, "w") as zf:
        # Create a symlink entry (external_attr bits 16-31 for UNIX mode)
        # 0o120000 is S_IFLNK
        zinfo = zipfile.ZipInfo("link_to_secret")
        zinfo.external_attr = 0o120000 << 16
        zf.writestr(zinfo, "")

    result = extract_archive(zip_path, staging_dir)
    assert result["complete"] is False
    assert any("Symlink detected" in e for e in result["errors"])

def test_oversized_file(tmp_path):
    zip_path = tmp_path / "big.zip"
    staging_dir = tmp_path / "staging"

    with zipfile.ZipFile(zip_path, "w") as zf:
        # Create a file that claims to be huge (simulated by large zip info)
        # Since we can't easily write a 200MB file in a test quickly,
        # we test the limit check.
        zf.writestr("large.txt", "a" * 1024)
        # We'll mock the check or use a very small limit in a real scenario,
        # but here let's try to actually write a larger one if possible.
        # For speed, let's just test the logic if we could.
        # Instead, let's test the max_file_bytes argument.
        pass

    # Test with a tiny limit
    result = extract_archive(zip_path, staging_dir, max_file_bytes=500)
    assert result["complete"] is False
    assert any("File too large" in e for e in result["errors"])

def test_zip_bomb_ratio(tmp_path):
    zip_path = tmp_path / "bomb.zip"
    staging_dir = tmp_path / "staging"

    # Create a file that is highly compressed
    # 1MB of 'a's compressed is tiny
    large_content = "a" * (2 * 1024 * 1024)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.txt", large_content)

    # Ratio will be ~2MB / small_size.
    # We set a low ratio limit for the test.
    result = extract_archive(zip_path, staging_dir, max_ratio=2.0)
    assert result["complete"] is False
    assert any("Compression ratio too high" in e for e in result["errors"])

def test_nested_zips(tmp_path):
    zip_path = tmp_path / "outer.zip"
    staging_dir = tmp_path / "staging"

    inner_zip_path = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner_zip_path, "w") as zf:
        zf.writestr("inner_file.txt", "inner content")

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(inner_zip_path, "inner.zip")
        zf.writestr("outer_file.txt", "outer content")

    result = extract_archive(zip_path, staging_dir, max_depth=2)

    assert result["complete"] is True
    # outer_file.txt, inner.zip, inner.zip!/inner_file.txt
    assert len(result["files"]) == 3

    provs = [f["provenance"] for f in result["files"]]
    assert "outer.zip!/inner.zip!/inner_file.txt" in provs

def test_nested_zip_depth_limit(tmp_path):
    zip_path = tmp_path / "outer.zip"
    staging_dir = tmp_path / "staging"

    # Create 3 levels
    z3 = tmp_path / "level3.zip"
    with zipfile.ZipFile(z3, "w") as zf: zf.writestr("f3.txt", "3")

    z2 = tmp_path / "level2.zip"
    with zipfile.ZipFile(z2, "w") as zf: zf.writestr("level3.zip", z3.read_bytes())

    z1 = tmp_path / "level1.zip"
    with zipfile.ZipFile(z1, "w") as zf: zf.writestr("level2.zip", z2.read_bytes())

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("level1.zip", z1.read_bytes())

    # Max depth 1 means it should stop after level 1's contents
    result = extract_archive(zip_path, staging_dir, max_depth=1)
    assert result["complete"] is False
    assert any("Max depth 1 reached" in e for e in result["errors"])

def test_corrupt_zip(tmp_path):
    zip_path = tmp_path / "corrupt.zip"
    staging_dir = tmp_path / "staging"

    zip_path.write_text("this is not a zip file")

    result = extract_archive(zip_path, staging_dir)
    assert result["complete"] is False
    assert any("Failed to process" in e for e in result["errors"])

def test_provenance_format(tmp_path):
    zip_path = tmp_path / "prov.zip"
    staging_dir = tmp_path / "staging"

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("dir/file.txt", "content")

    result = extract_archive(zip_path, staging_dir)
    assert result["files"][0]["provenance"] == "prov.zip!/dir/file.txt"

def test_nothing_written_outside_staging(tmp_path):
    zip_path = tmp_path / "escape.zip"
    staging_dir = tmp_path / "staging"

    # We can't easily test actual file writing outside because _is_safe_extraction_path
    # should catch it, but let's ensure the check works.
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../outside.txt", "malicious")

    result = extract_archive(zip_path, staging_dir)
    assert result["complete"] is False
    assert not (tmp_path / "outside.txt").exists()


# A header that lies about the size must not bypass the caps: the extractor
# has to count the bytes it really writes.
def test_zip_bomb_lies_about_declared_size(tmp_path):
    zip_path = tmp_path / "liar.zip"
    staging_dir = tmp_path / "staging"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.bin", os.urandom(200_000))

    # Rewrite the central-directory declared size to a tiny value.
    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = zf.infolist()
    data = bytearray(zip_path.read_bytes())
    real = (200_000).to_bytes(4, "little")
    fake = (100).to_bytes(4, "little")
    pos = data.rfind(real)
    while pos != -1:
        data[pos:pos + 4] = fake
        pos = data.rfind(real, 0, pos)
    zip_path.write_bytes(bytes(data))

    result = extract_archive(zip_path, staging_dir, max_file_bytes=50_000, max_ratio=1e9)
    assert result["complete"] is False
    assert any("too large" in e.lower() or "failed" in e.lower() for e in result["errors"])
    assert not (staging_dir / "big.bin").exists() or (staging_dir / "big.bin").stat().st_size <= 50_000
