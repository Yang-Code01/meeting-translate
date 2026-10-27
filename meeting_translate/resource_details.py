"""首页资源卡「详细」弹窗：语音模型、虚拟声卡装了什么、怎么用、现在检测到什么。

正文是静态说明 + 现场检测结果（设备名、安装目录），所以尺寸/路径都从
tts_models 的清单和 duplex_audio 的检测结果现算，不在文案里手写数字。
"""

from __future__ import annotations

import html
import os
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from . import tts_runtime
from .duplex_audio import CableDevices
from .tts_models import download_groups, human_size, total_download_text

DETAIL_QSS = """
QDialog#detailDialog { background: #0f1115; }
QLabel#detailTitle { color: #f4f6fb; font-size: 22px; font-weight: 800; }
QLabel#detailSub { color: #8991a4; font-size: 12px; }
QTextBrowser#detailBody {
    background: #12151c; color: #c9d2e3; border: 1px solid #272d3a;
    border-radius: 12px; padding: 10px 12px;
}
QPushButton#detailGhost {
    color: #dce3f2; background: #202635; border: 1px solid #313a4f;
    border-radius: 9px; padding: 9px 16px;
}
QPushButton#detailGhost:hover { background: #29303f; }
QScrollBar:vertical { background: #12151c; width: 10px; margin: 4px; border-radius: 5px; }
QScrollBar::handle:vertical { background: #2c3444; border-radius: 5px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: #3a4557; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""

_P = "style='color:#c9d2e3;font-size:13px;line-height:150%'"
_H = "style='color:#f4f6fb;font-size:15px;font-weight:700'"
_MUTED = "style='color:#7f8aa1;font-size:12px'"
_CODE = "style='color:#9fc3e8;font-size:12px'"
_TH = (
    "style='padding:6px 10px;color:#8fa2c0;font-size:12px;font-weight:700;"
    "text-align:left;border-bottom:1px solid #2b3342'"
)
_TD = "style='padding:7px 10px;color:#dce3f2;font-size:13px;border-bottom:1px solid #232834'"


def _heading(text: str) -> str:
    return f"<p {_H}>{html.escape(text)}</p>"


def _para(text: str) -> str:
    return f"<p {_P}>{text}</p>"


def _table(headers: list[str], rows: list[list[str]], widths: list[str] | None = None) -> str:
    """表头转义、单元格按原样当 HTML（内容里的动态字符串由调用方 escape）。"""

    def cell(tag: str, style: str, text: str, index: int) -> str:
        width = f" width='{widths[index]}'" if widths else ""
        return f"<{tag}{width} {style} valign='top'>{text}</{tag}>"

    head = "".join(cell("th", _TH, html.escape(h), i) for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(cell("td", _TD, text, i) for i, text in enumerate(row)) + "</tr>" for row in rows
    )
    return (
        "<table width='100%' cellspacing='0' cellpadding='0' "
        "style='margin:6px 0 2px 0;border:1px solid #232834'>"
        f"<tr>{head}</tr>{body}</table>"
    )


def _block(head: str, lines: list[tuple[str, str]]) -> str:
    """标题行 + 若干「标签：内容」行，比宽表格好读，也不会因为长文件名撑出横向滚动条。"""
    body = "<br>".join(f"<span {_MUTED}>{html.escape(k)}</span> {html.escape(v)}" for k, v in lines)
    return f"<p {_P}><b style='color:#eaf0fb'>{html.escape(head)}</b><br>{body}</p>"


def _list(items: list[str]) -> str:
    lis = "".join(f"<li {_P}>{item}</li>" for item in items)
    return f"<ul style='margin:4px 0 4px 16px'>{lis}</ul>"


def _device(value: str | None) -> str:
    if value:
        return html.escape(value)
    return "<span style='color:#e0a45c'>未检测到</span>"


_APPDATA = os.environ.get("APPDATA", "")


def _short_path(path: Path | str) -> str:
    """路径太长会在窄窗口里难看地折行；目录在 %APPDATA% 下的换成 %APPDATA%\\… （粘进资源管理器能直接打开）。"""
    text = str(path)
    if _APPDATA and text.lower().startswith(_APPDATA.lower()):
        return "%APPDATA%" + text[len(_APPDATA):]
    return text


def models_html(root: Path | str) -> str:
    """语音模型卡的详细说明。"""
    rows = [
        [
            f"<b style='color:#eaf0fb'>{html.escape(g.role)}</b>",
            f"{html.escape(g.name)}<br>"
            f"<span {_MUTED}>{html.escape(g.source)}</span><br>"
            f"<span {_MUTED}>{html.escape(g.detail)}</span>",
            f"<div align='right'>{human_size(g.size)}</div>",
        ]
        for g in download_groups()
    ]
    return (
        _para(
            f"首次安装要下载三组文件，合计约 <b style='color:#eaf0fb'>{total_download_text()}</b>，"
            "卡片上的「下载」一次装齐。"
        )
        + _para(f"安装位置：<span {_CODE}>{html.escape(_short_path(root))}</span>")
        + _table(["方向", "模型 / 来源 / 明细", "大小"], rows, ["14%", "72%", "14%"])
        + _heading("两个方向各用哪一组")
        + _list(
            [
                "对方说日语：MeloTTS 把译好的中文读给你（本地耳机）。",
                "你说中文：Style-Bert-VITS2 把译好的日语送进 CABLE-B，对方才听得到。",
                "日文 BERT 不直接出声音：SBV2 先用它给日语分词、注音、标韵律，再送声学模型。",
            ]
        )
        + _para(
            "<span style='color:#e0a45c'>只用一个方向也要三组装齐</span>：开始同传前会校验 melo 与 sbv2，"
            "缺任一组就提示「TTS 模型尚未下载」。"
        )
        + _heading("为什么光读音模型就要几百 MB")
        + _para(
            "日文 BERT 是 DeBERTa-v2-large（约 0.4B 参数）的 ONNX 版本，已经是 fp16 半精度；"
            "退回 CPU 上的 PyTorch 推理约 2.4 秒/句，会让每一句日语都晚两秒以上，所以不再压缩。"
        )
        + _heading("还要一个 Python 运行时（首次 约 400MB）")
        + _para(
            "Style-Bert-VITS2 要求 <span " + _CODE + ">numpy&lt;2</span>，而主程序跑在 numpy 2 上，"
            "两个约束装不进同一个解释器，所以程序会在你的机器上用 <b>Python 3.12</b> 建一个独立环境，"
            "只装 onnxruntime（约 70MB）和几个纯 Python 依赖。卡片上的「安装运行时」就干这件事。"
        )
        + _list(
            [
                "需要机器上装有 Python 3.12（3.13/3.14 目前还没有 numba、llvmlite 的轮子）。",
                "没有的话点「安装运行时」会提示，并给一个 python.org 的下载链接。",
                f"装好的环境在 <span {_CODE}>{html.escape(_short_path(tts_runtime.runtime_dir()))}</span>，"
                "想重装直接删掉这个目录再点一次。",
            ]
        )
        + _heading("装完以后")
        + _list(
            [
                "下载完成后卡片变成「已安装 · 启动时加载」，模型随程序启动加载。",
                "开始同传时后台预热两个引擎，避免第一句卡 20 秒。",
                "想腾出磁盘空间：直接删掉 melo 与 sbv2 两个引擎目录即可（日文 BERT 在 sbv2 目录下，一并删掉）。",
                "想放到别的盘：设置环境变量 MEETING_TRANSLATE_TTS_DIR 指向新的模型目录。",
            ]
        )
    )


def cable_html(devices: CableDevices | None, downloads: Path | str, error: str = "") -> str:
    """虚拟声卡卡的详细说明；devices 为空表示检测失败。"""
    if devices is None:
        checks = [["<b style='color:#eaf0fb'>检测失败</b>", html.escape(error or "未知错误"), ""]]
    else:
        checks = [
            [
                "<b style='color:#eaf0fb'>CABLE-A Input</b><br>"
                f"<span {_MUTED}>播放端点（扬声器）</span>",
                "会议软件把对方声音输出到这里，本程序对它的回环做采集",
                _device(devices.a_input),
            ],
            [
                "<b style='color:#eaf0fb'>CABLE-A Output</b><br>"
                f"<span {_MUTED}>录音端点（麦克风）</span>",
                "只用于自检时回采测试音，同传不占用",
                _device(devices.a_output),
            ],
            [
                "<b style='color:#eaf0fb'>CABLE-B Input</b><br>"
                f"<span {_MUTED}>播放端点（扬声器）</span>",
                "本程序把日语 TTS 写进来",
                _device(devices.b_input),
            ],
            [
                "<b style='color:#eaf0fb'>CABLE-B Output</b><br>"
                f"<span {_MUTED}>录音端点（麦克风）</span>",
                "会议软件的麦克风选它，对方才听得到日语",
                _device(devices.b_output),
            ],
        ]
    packages = [
        (
            "VBCABLE_Driver_Pack45.zip",
            [("解开后运行", "VBCABLE_Setup_x64.exe"), ("提供", "CABLE Input / CABLE Output（CABLE-A 这一对）")],
        ),
        (
            "HiFiCableAsioBridgeSetup_v1007.zip",
            [
                ("解开后运行", "HiFiCableAsioBridgeSetup"),
                ("提供", "Hi-Fi Cable Input / Output（CABLE-B 这一对）"),
            ],
        ),
    ]
    return (
        _para(
            "虚拟声卡用的是 VB-Audio 的虚拟声卡驱动：对会议软件来说它是一块真声卡，对本程序来说是可读写的音频流。"
            "装好后系统里会多出四个端点，A 对收对方声音，B 对送日语。"
        )
        + _heading("要装的两个安装包")
        + "".join(_block(name, lines) for name, lines in packages)
        + _para(
            "来源 download.vb-audio.com（官方直链）。安装需要管理员权限，装完建议重启；"
            f"下载目录：<span {_CODE}>{html.escape(_short_path(downloads))}</span>"
        )
        + _heading("四个端点各干什么")
        + _table(["端点", "角色", "现在检测到"], checks, ["22%", "42%", "36%"])
        + _heading("会议软件里怎么选")
        + _para(
            "以 Teams 为例，其他会议软件（Zoom、腾讯会议、飞书、Google Meet）同理，"
            "只是入口在各自的音频设置里，选的就这两个设备："
        )
        + _list(
            [
                "扬声器（对方声音输出）：CABLE Input (VB-Audio Virtual Cable)。",
                "麦克风（你说的话送出去）：Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)。",
                "你自己听中文译文用的是设置里的「本地耳机（中文 TTS）」，别选成 CABLE。",
            ]
        )
        + _heading("名字别的叫法也认")
        + _para(
            "检测除了 CABLE-A/B，还认这些别名："
            "<span style='color:#9fc3e8'>CABLE Input / CABLE In 16ch</span>、"
            "<span style='color:#9fc3e8'>CABLE Output</span>、"
            "<span style='color:#9fc3e8'>Hi-Fi Cable Input / HiFi Cable Input / Voicemeeter Input</span>、"
            "<span style='color:#9fc3e8'>Hi-Fi Cable Output / HiFi Cable Output / Voicemeeter Output</span>"
            " —— 装过 Voicemeeter 的机器也能直接用。"
        )
    )


class DetailDialog(QDialog):
    """只读说明窗：标题 + 一句话摘要 + 富文本正文。"""

    def __init__(self, title: str, subtitle: str, body: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("detailDialog")
        self.setWindowTitle(title)
        self.resize(880, 660)
        self.setMinimumSize(560, 420)
        self.setStyleSheet(DETAIL_QSS)

        title_lbl = QLabel(title)
        title_lbl.setObjectName("detailTitle")
        sub_lbl = QLabel(subtitle)
        sub_lbl.setObjectName("detailSub")
        sub_lbl.setWordWrap(True)

        self.body = QTextBrowser()
        self.body.setObjectName("detailBody")
        self.body.setOpenExternalLinks(True)
        self.body.setHtml(body)

        close_btn = QPushButton("关闭")
        close_btn.setObjectName("detailGhost")
        close_btn.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(close_btn)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(8)
        lay.addWidget(title_lbl)
        lay.addWidget(sub_lbl)
        lay.addSpacing(4)
        lay.addWidget(self.body, 1)
        lay.addLayout(buttons)
