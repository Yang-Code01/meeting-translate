"""Joiner 按音源使用不同断句语言。"""

from __future__ import annotations

import itertools

from meeting_translate import lang
from meeting_translate.joiner import Joiner

ME = "我"
THEM = "对方"


def _joiner():
    opened, updated, committed = [], [], []
    j = Joiner(
        3.0,
        20.0,
        [],
        on_open=opened.append,
        on_update=updated.append,
        on_commit=lambda p, translate: committed.append((p, translate)),
        ids=itertools.count(1),
        lang="ja",
        langs={ME: "zh", THEM: "ja"},
    )
    return j, opened, updated, committed


def test_chinese_source_uses_chinese_completion_rules():
    j, opened, _, committed = _joiner()
    # 「的」是中文未完成结尾；如果误用日语规则会立刻定稿。
    j.add(ME, "这个方案是公司的", 0.0, 0.0)
    assert not committed
    j.add(ME, "。", 0.0, 0.1)
    assert len(committed) == 1
    assert committed[0][0].ja == "这个方案是公司的。"
    assert opened[0].source == ME


def test_japanese_source_uses_japanese_completion_rules():
    j, _, _, committed = _joiner()
    j.add(THEM, "この件については", 0.0, 0.0)
    assert not committed
    j.add(THEM, "検討します。", 0.0, 0.1)
    assert len(committed) == 1
    assert committed[0][0].ja == "この件については検討します。"


def test_default_language_still_works_for_old_callers():
    committed = []
    j = Joiner(
        3.0, 20.0, [], lambda p: None, lambda p: None,
        lambda p, t: committed.append(p), ids=itertools.count(1), lang="zh",
    )
    j.add("对方", "这个方案是公司的。", 0.0, 0.0)
    assert committed and committed[0].ja == "这个方案是公司的。"
