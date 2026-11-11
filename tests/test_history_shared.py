"""划词与截图翻译共用一个历史文件：头部同时涵盖两者，按原文去重，不标注来源。"""

from __future__ import annotations

from datetime import datetime

from meeting_translate.selection import SelectionRecord, append_history

LEGACY_HEADER = "# 划词/句翻译历史\n\n"
SHARED_HEADER = "# 划词 / 截图翻译历史\n\n"


def record(source: str, chinese: str = "译") -> SelectionRecord:
    return SelectionRecord(datetime(2026, 1, 2, 3, 4, 5), source, chinese, "よみ")


def test_new_file_gets_the_shared_header_and_no_screenshot_label(tmp_path) -> None:
    path = tmp_path / "history.md"
    append_history(path, [record("日本語")])
    text = path.read_text(encoding="utf-8")
    assert text.startswith(SHARED_HEADER)
    assert "截图" not in text.replace("划词 / 截图翻译历史", "")


def test_legacy_header_is_replaced_and_existing_records_are_kept(tmp_path) -> None:
    path = tmp_path / "history.md"
    path.write_text(LEGACY_HEADER + "## 2025-01-01 00:00:00\n\n- 原文：\n  ```text\n  旧\n  ```\n\n", encoding="utf-8")
    result = append_history(path, [record("新")])
    text = path.read_text(encoding="utf-8")
    assert result.added == 1
    assert text.startswith(SHARED_HEADER)
    assert "# 划词/句翻译历史" not in text
    assert "  旧\n" in text and "  新\n" in text


def test_legacy_header_is_migrated_even_when_nothing_new_is_added(tmp_path) -> None:
    path = tmp_path / "history.md"
    path.write_text(LEGACY_HEADER + "## 2025-01-01 00:00:00\n\n- 原文：\n  ```text\n  旧\n  ```\n\n", encoding="utf-8")
    result = append_history(path, [record("旧")])
    assert result.added == 0 and result.skipped == 1
    assert path.read_text(encoding="utf-8").startswith(SHARED_HEADER)


def test_a_customised_header_is_left_untouched(tmp_path) -> None:
    path = tmp_path / "history.md"
    path.write_text("# 我的笔记\n\n## 2025-01-01 00:00:00\n\n- 原文：\n  ```text\n  旧\n  ```\n\n", encoding="utf-8")
    append_history(path, [record("新")])
    assert path.read_text(encoding="utf-8").startswith("# 我的笔记\n\n")


def test_screenshot_and_selection_records_with_the_same_source_are_stored_once(tmp_path) -> None:
    path = tmp_path / "history.md"
    append_history(path, [record("日本語", "划词译")])  # 划词结果先写入
    result = append_history(path, [record("日本語", "截图译")])  # 截图识别出相同原文
    text = path.read_text(encoding="utf-8")
    assert result.added == 0 and result.skipped == 1
    assert text.count("- 原文：") == 1
    assert "划词译" in text and "截图译" not in text


def test_screenshot_record_is_formatted_exactly_like_a_selection_record(tmp_path) -> None:
    sel = tmp_path / "sel.md"
    shot = tmp_path / "shot.md"
    append_history(sel, [record("日本語")])
    append_history(shot, [record("日本語")])
    assert sel.read_text(encoding="utf-8") == shot.read_text(encoding="utf-8")
