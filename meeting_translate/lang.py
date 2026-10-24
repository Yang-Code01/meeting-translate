"""翻译方向：日→中 / 中→日。提示词、断句规则、整理稿用语按方向取值。

记录里的字段名沿用 `ja`（原文）与 `zh`（译文），中→日时分别存放中文原文与日文译文。
"""

from __future__ import annotations

from dataclasses import dataclass

JA_ZH = "ja-zh"
ZH_JA = "zh-ja"


@dataclass(frozen=True)
class Direction:
    key: str
    label: str  # 日→中
    src: str  # 提示词中的原文语言名
    dst: str  # 提示词中的译文语言名
    # 整理稿用语（使用译文语言）
    doc_title: str
    minutes: str
    topics: str
    conclusions: str
    todos: str
    todo_header: str
    unclear: str
    none: str
    fulltext: str
    generated: str
    usage: str
    fallback_title: str


DIRECTIONS: dict[str, Direction] = {
    JA_ZH: Direction(
        JA_ZH, "日→中", "日语", "简体中文",
        doc_title="会议整理",
        minutes="会议纪要",
        topics="议题",
        conclusions="结论",
        todos="待办事项",
        todo_header="| 负责人 | 内容 | 期限 |",
        unclear="未明确",
        none="无",
        fulltext="双语全文（已清洗）",
        generated="生成于 {now}，模型 {model}。原始记录：{source}",
        usage="本场用量：{total} AIC（翻译 {tr} + 整理 {sm}）",
        fallback_title="会议整理",
    ),
    ZH_JA: Direction(
        ZH_JA, "中→日", "中文", "日语",
        doc_title="会議まとめ",
        minutes="議事録",
        topics="議題",
        conclusions="結論",
        todos="ToDo",
        todo_header="| 担当者 | 内容 | 期限 |",
        unclear="未定",
        none="なし",
        fulltext="対訳全文（整形済み）",
        generated="{now} 生成、モデル {model}。元の記録：{source}",
        usage="今回の使用量：{total} AIC（翻訳 {tr} + まとめ {sm}）",
        fallback_title="会議まとめ",
    ),
}


def get(key: str | None) -> Direction:
    return DIRECTIONS.get(key or JA_ZH, DIRECTIONS[JA_ZH])


def from_label(label: str) -> str:
    for d in DIRECTIONS.values():
        if d.label == label:
            return d.key
    return JA_ZH
