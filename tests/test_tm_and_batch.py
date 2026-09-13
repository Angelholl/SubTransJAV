"""
Tests for Translation Memory (TM) and batch processing.
"""

import os
import sys

import pytest

# Ensure project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── TM Tests ─────────────────────────────────────────────────────────


class TestTranslationMemory:
    """Test TranslationMemory class."""

    def _make_tm(self, tmp_path):
        from subtransjav.refine.tm import TranslationMemory
        db_path = os.path.join(str(tmp_path), "test_tm.db")
        return TranslationMemory(db_path)

    def test_store_and_lookup_exact(self, tmp_path):
        tm = self._make_tm(tmp_path)
        try:
            # Store
            added = tm.store("おはよう", "早上好", stage=1)
            assert added is True

            # Lookup exact
            result = tm.lookup_exact("おはよう", stage=1)
            assert result == "早上好"

            # Different stage
            result2 = tm.lookup_exact("おはよう", stage=2)
            assert result2 is None
        finally:
            tm.close()

    def test_store_duplicate_updates(self, tmp_path):
        tm = self._make_tm(tmp_path)
        try:
            tm.store("おはよう", "早安", stage=1)
            added = tm.store("おはよう", "早上好", stage=1)
            assert added is False  # not new

            result = tm.lookup_exact("おはよう", stage=1)
            assert result == "早上好"  # updated
        finally:
            tm.close()

    def test_store_batch(self, tmp_path):
        tm = self._make_tm(tmp_path)
        try:
            pairs = [
                ("おはよう", "早上好", 1),
                ("こんにちは", "你好", 1),
                ("さようなら", "再见", 1),
            ]
            added = tm.store_batch(pairs)
            assert added == 3

            assert tm.lookup_exact("おはよう", 1) == "早上好"
            assert tm.lookup_exact("こんにちは", 1) == "你好"
            assert tm.lookup_exact("さようなら", 1) == "再见"
        finally:
            tm.close()

    def test_lookup_exact_all_stages(self, tmp_path):
        tm = self._make_tm(tmp_path)
        try:
            tm.store("テスト", "测试", stage=1)
            tm.store("テスト", "test", stage=2)

            result = tm.lookup_exact_all_stages("テスト")
            assert result == {1: "测试", 2: "test"}
        finally:
            tm.close()

    def test_lookup_fuzzy(self, tmp_path):
        tm = self._make_tm(tmp_path)
        try:
            # Use ASCII text for reliable fuzzy matching in tests
            tm.store("hello world good morning", "hello world good morning translated", 1)
            tm.store("hello world", "hello translated", 1)

            results = tm.lookup_fuzzy("hello world good", stage=1, threshold=0.5)
            assert len(results) > 0
            # Should find at least one match
            assert any(r[2] >= 0.5 for r in results)
        finally:
            tm.close()

    def test_stats(self, tmp_path):
        tm = self._make_tm(tmp_path)
        try:
            tm.store("a", "A", stage=1)
            tm.store("b", "B", stage=1)
            tm.store("c", "C", stage=2)

            stats = tm.stats()
            assert stats["total"] == 3
            assert stats["by_stage"][1] == 2
            assert stats["by_stage"][2] == 1
        finally:
            tm.close()

    def test_clear(self, tmp_path):
        tm = self._make_tm(tmp_path)
        try:
            tm.store("a", "A", stage=1)
            tm.store("b", "B", stage=2)

            tm.clear(stage=1)
            assert tm.lookup_exact("a", 1) is None
            assert tm.lookup_exact("b", 2) == "B"

            tm.clear()
            assert tm.lookup_exact("b", 2) is None
        finally:
            tm.close()

    def test_export_import_csv(self, tmp_path):
        tm = self._make_tm(tmp_path)
        try:
            tm.store("おはよう", "早上好", stage=1)
            tm.store("こんにちは", "你好", stage=1)

            csv_path = os.path.join(str(tmp_path), "export.csv")
            tm.export_csv(csv_path, stage=1)
            assert os.path.isfile(csv_path)

            # Import into the SAME TM (should not add duplicates)
            added = tm.import_csv(csv_path)
            assert added == 0  # same content, not "new"

            # Import into a NEW TM (should add all)
            from subtransjav.refine.tm import TranslationMemory
            db2 = os.path.join(str(tmp_path), "test_tm2.db")
            tm2 = TranslationMemory(db2)
            added2 = tm2.import_csv(csv_path)
            assert added2 == 2  # new database, all entries are new
            assert tm2.lookup_exact("おはよう", 1) == "早上好"
            tm2.close()
        finally:
            tm.close()

    def test_hit_count(self, tmp_path):
        tm = self._make_tm(tmp_path)
        try:
            tm.store("test", "测试", stage=1)
            tm.lookup_exact("test", 1)
            tm.lookup_exact("test", 1)
            tm.lookup_exact("test", 1)

            stats = tm.stats()
            assert stats["total_hits"] == 3
        finally:
            tm.close()

    def test_normalize_whitespace(self, tmp_path):
        tm = self._make_tm(tmp_path)
        try:
            tm.store("  おはよう  ございます  ", "早上好", stage=1)
            # Same content with different whitespace should match
            result = tm.lookup_exact("おはよう ございます", stage=1)
            assert result == "早上好"
        finally:
            tm.close()

    def test_empty_input_rejected(self, tmp_path):
        tm = self._make_tm(tmp_path)
        try:
            assert tm.store("", "test", stage=1) is False
            assert tm.store("test", "", stage=1) is False
        finally:
            tm.close()


# ── Batch Processing Tests ───────────────────────────────────────────


class TestBatchProcessing:
    """Test batch directory scanning."""

    def test_find_srt_files_flat(self, tmp_path):
        from subtransjav.refine.batch import find_srt_files

        # Create test files
        (tmp_path / "a.srt").write_text("test")
        (tmp_path / "b.srt").write_text("test")
        (tmp_path / "c.txt").write_text("test")

        files = find_srt_files(str(tmp_path), recursive=False)
        assert len(files) == 2
        assert all(f.endswith(".srt") for f in files)

    def test_find_srt_files_recursive(self, tmp_path):
        from subtransjav.refine.batch import find_srt_files

        subdir = tmp_path / "sub"
        subdir.mkdir()
        (tmp_path / "a.srt").write_text("test")
        (subdir / "b.srt").write_text("test")

        # Non-recursive: only root
        files_flat = find_srt_files(str(tmp_path), recursive=False)
        assert len(files_flat) == 1

        # Recursive: both
        files_rec = find_srt_files(str(tmp_path), recursive=True)
        assert len(files_rec) == 2

    def test_find_srt_files_min_size(self, tmp_path):
        from subtransjav.refine.batch import find_srt_files

        (tmp_path / "small.srt").write_text("x")
        (tmp_path / "large.srt").write_text("x" * 1000)

        files = find_srt_files(str(tmp_path), min_size=100)
        assert len(files) == 1
        assert "large.srt" in files[0]

    def test_find_srt_files_max_size(self, tmp_path):
        from subtransjav.refine.batch import find_srt_files

        (tmp_path / "small.srt").write_text("x")
        (tmp_path / "large.srt").write_text("x" * 1000)

        files = find_srt_files(str(tmp_path), max_size=100)
        assert len(files) == 1
        assert "small.srt" in files[0]

    def test_find_srt_files_exclude(self, tmp_path):
        from subtransjav.refine.batch import find_srt_files

        (tmp_path / "good.srt").write_text("test")
        (tmp_path / "video_raw.srt").write_text("test")
        (tmp_path / "cache").mkdir()
        (tmp_path / "cache" / "cached.srt").write_text("test")

        files = find_srt_files(
            str(tmp_path), recursive=True,
            exclude_patterns=["*_raw.srt", "*/cache/*"]
        )
        assert len(files) == 1
        assert "good.srt" in files[0]

    def test_find_srt_files_pattern(self, tmp_path):
        from subtransjav.refine.batch import find_srt_files

        (tmp_path / "video.ja.srt").write_text("test")
        (tmp_path / "video.cn.srt").write_text("test")
        (tmp_path / "video.srt").write_text("test")

        # Pattern: only .ja.srt files
        files = find_srt_files(str(tmp_path), pattern="*.ja.srt")
        assert len(files) == 1
        assert "video.ja.srt" in files[0]

    def test_scan_summary(self, tmp_path):
        from subtransjav.refine.batch import scan_summary

        (tmp_path / "a.srt").write_text("x" * 100)
        (tmp_path / "b.srt").write_text("x" * 200)

        files = [str(tmp_path / "a.srt"), str(tmp_path / "b.srt")]
        summary = scan_summary(files)

        assert summary["count"] == 2
        assert summary["total_size"] == 300
        assert summary["total_size_mb"] < 1

    def test_empty_directory(self, tmp_path):
        from subtransjav.refine.batch import find_srt_files

        files = find_srt_files(str(tmp_path))
        assert files == []

    def test_nonexistent_directory(self):
        from subtransjav.refine.batch import find_srt_files

        with pytest.raises(FileNotFoundError):
            find_srt_files("/nonexistent/path")

    def test_sorted_output(self, tmp_path):
        from subtransjav.refine.batch import find_srt_files

        (tmp_path / "c.srt").write_text("test")
        (tmp_path / "a.srt").write_text("test")
        (tmp_path / "b.srt").write_text("test")

        files = find_srt_files(str(tmp_path), recursive=False)
        basenames = [os.path.basename(f) for f in files]
        assert basenames == ["a.srt", "b.srt", "c.srt"]


# ── CLI Integration Tests ────────────────────────────────────────────


class TestCLIBatchArgs:
    """Test CLI argument parsing for new batch and TM features."""

    def test_parse_input_dir(self):
        from subtransjav.refine.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["--input-dir", "/tmp/srt", "-r"])
        assert args.input_dir == "/tmp/srt"
        assert args.recursive is True

    def test_parse_filter_options(self):
        from subtransjav.refine.cli import build_parser
        parser = build_parser()
        args = parser.parse_args([
            "--input-dir", "/tmp",
            "--filter-pattern", "*.ja.srt",
            "--min-size", "100",
            "--max-size", "10000",
        ])
        assert args.filter_pattern == "*.ja.srt"
        assert args.min_size == 100
        assert args.max_size == 10000

    def test_parse_tm_options(self):
        from subtransjav.refine.cli import build_parser
        parser = build_parser()
        args = parser.parse_args([
            "-i", "test.srt",
            "--tm-db", "/tmp/tm.db",
            "--tm-threshold", "0.9",
        ])
        assert args.tm_db == "/tmp/tm.db"
        assert args.tm_threshold == 0.9

    def test_parse_no_tm(self):
        from subtransjav.refine.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["-i", "test.srt", "--no-tm"])
        assert args.no_tm is True

    def test_parse_tm_stats(self):
        from subtransjav.refine.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["--tm-stats"])
        assert args.tm_stats is True

    def test_collect_input_files_from_dir(self, tmp_path):
        from argparse import Namespace

        from subtransjav.refine.cli import _collect_input_files

        (tmp_path / "a.srt").write_text("test")
        (tmp_path / "b.srt").write_text("test")

        args = Namespace(
            input=[],
            input_dir=str(tmp_path),
            recursive=False,
            filter_pattern="*.srt",
            min_size=0,
            max_size=0,
            min_date="",
            max_date="",
            exclude=[],
        )
        files = _collect_input_files(args)
        assert len(files) == 2

    def test_collect_input_files_combined(self, tmp_path):
        """Test combining -i files with --input-dir scanning."""
        from argparse import Namespace

        from subtransjav.refine.cli import _collect_input_files

        extra = tmp_path / "extra.srt"
        extra.write_text("test")
        (tmp_path / "dir").mkdir()
        (tmp_path / "dir" / "dir_file.srt").write_text("test")

        args = Namespace(
            input=[str(extra)],
            input_dir=str(tmp_path / "dir"),
            recursive=False,
            filter_pattern="*.srt",
            min_size=0,
            max_size=0,
            min_date="",
            max_date="",
            exclude=[],
        )
        files = _collect_input_files(args)
        assert len(files) == 2

    def test_collect_deduplication(self, tmp_path):
        """Test that duplicate files are removed."""
        from argparse import Namespace

        from subtransjav.refine.cli import _collect_input_files

        f = tmp_path / "test.srt"
        f.write_text("test")

        args = Namespace(
            input=[str(f), str(f)],  # duplicate
            input_dir="",
            recursive=False,
            filter_pattern="*.srt",
            min_size=0,
            max_size=0,
            min_date="",
            max_date="",
            exclude=[],
        )
        files = _collect_input_files(args)
        assert len(files) == 1
