# 第三方组件与许可 / Third-Party Components and Licenses

本仓库自身代码以 **AGPL-3.0** 发布（见根目录 `LICENSE`）。
以下组件随源码使用、并被打包进 Release 中的 `MeetingTranslate.exe`，各自遵循其原始许可。

| 组件 | 版本 | 许可 |
|---|---|---|
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| certifi | 2026.7.22 | Mozilla Public License 2.0 (MPL 2.0) |
| cffi | 2.1.1 | MIT-0 |
| github-copilot-sdk | 1.0.20 | MIT |
| h11 | 0.16.0 | MIT License |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD License |
| idna | 3.20 | BSD-3-Clause |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pycparser | 3.11 | BSD-3-Clause |
| pydantic | 2.14.1 | MIT |
| pydantic_core | 2.50.1 | MIT |
| PySide6_Essentials | 6.12.0 | 见上游发行包 |
| python-dateutil | 2.9.0.post0 | Apache Software License |
| pywin32-ctypes | 0.2.3 | 见上游发行包 |
| sherpa-onnx-core | 1.13.8 | 见上游发行包 |
| sherpa_onnx | 1.13.8 | 见上游发行包 |
| shiboken6 | 6.12.0 | 见上游发行包 |
| six | 1.17.0 | MIT License |
| SoundCard | 0.4.6 | BSD License |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |

## 许可全文

| 文件 | 覆盖对象 |
|---|---|
| `licenses/AGPL-3.0.txt` | 本项目自身代码 |
| `licenses/Copilot-CLI-LICENSE.md` | GitHub Copilot CLI（运行时原样随 exe 分发，见该许可第 2 节） |
| `licenses/FunASR-MODEL-LICENSE.txt` | SenseVoice 模型权重（FunAudioLLM/SenseVoiceSmall，阿里巴巴） |
| `licenses/ReazonSpeech-LICENSE.txt` | ReazonSpeech-k2-v2 模型权重（Apache-2.0） |
| `licenses/sherpa-onnx-LICENSE.txt` | sherpa-onnx / sherpa-onnx-core（Apache-2.0） |
| `licenses/silero-vad-LICENSE.txt` | silero-vad 语音活动检测模型（MIT） |
| `licenses/LGPL-3.0.txt` | PySide6 / shiboken6 / Qt 6（LGPL-3.0，另有商业许可可选） |
| `licenses/numpy-LICENSE.txt` | NumPy（BSD-3-Clause） |
| `licenses/soundcard-LICENSE.txt` | SoundCard（BSD-3-Clause） |

## 模型来源与署名

- **SenseVoice** — `FunAudioLLM/SenseVoiceSmall`，阿里巴巴通义实验室。依据 FunASR Model Open Source License Agreement v1.1 使用与再分发：注明出处与作者信息，保留模型名称。
- **ReazonSpeech-k2-v2** — `reazon-research/ReazonSpeech`，Reazon Human Interaction Lab，Apache-2.0。
- **silero-vad** — `snakers4/silero-vad`，MIT。

模型权重不存放于本仓库（`models/` 已被忽略），由 `scripts/fetch_models.py` 从上游镜像下载；Release 中的 exe 内置这些权重。

## GitHub Copilot CLI 再分发说明

exe 内置的 Copilot 运行时为 GitHub Copilot CLI 的**未修改副本**，仅作为本应用的一部分分发，不单独提供、也不作为主要产品。相关条件见 `licenses/Copilot-CLI-LICENSE.md` 第 2 节；该许可明确不限制本应用自身的许可选择。
使用本应用需要你自己拥有 GitHub Copilot 订阅，相关服务条款由你与 GitHub 之间约定。
