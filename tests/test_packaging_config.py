"""打包与 CI 配置：OCR 的数据文件与 onnxruntime 动态库必须被 PyInstaller 收集，CI 必须安装 rapidocr 与 onnxruntime。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_spec_collects_rapidocr_data_files_and_onnxruntime_dlls():
    spec = (ROOT / "meeting-translate.spec").read_text(encoding="utf-8")
    assert re.search(r'collect_data_files\("rapidocr"\)', spec), "spec 必须收集 rapidocr 数据文件（配置与内置模型）"
    assert re.search(r'collect_dynamic_libs\("onnxruntime"\)', spec), "spec 必须收集 onnxruntime 动态库"


def test_ci_test_job_installs_rapidocr_and_onnxruntime():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    install_lines = [line for line in ci.splitlines() if "pip install" in line]
    assert install_lines, "ci.yml 缺少 pip install 步骤"
    joined = " ".join(install_lines)
    assert "rapidocr" in joined
    assert "onnxruntime" in joined
