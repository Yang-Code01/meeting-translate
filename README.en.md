# Real-Time Meeting Translator (Japanese ⇄ Chinese)

[中文](README.md) ｜ [English](README.en.md) ｜ [日本語](README.ja.md)

[![CI](https://github.com/Yang-Code01/meeting-translate/actions/workflows/ci.yml/badge.svg)](https://github.com/Yang-Code01/meeting-translate/actions/workflows/ci.yml)

A small Windows desktop tool with three features:

- **Live meeting translation** — it listens to **any meeting app** (the system loopback is treated as *them*, your microphone as *you*), runs speech recognition locally, translates with GitHub Copilot or your own OpenAI-compatible endpoint, and shows bilingual Japanese/Chinese subtitles in an always-on-top overlay. When the meeting ends it can produce cleaned-up minutes, action items and a bilingual transcript.
- **Selection translation** — select a piece of Japanese text in any application and press **Alt+M**: a popup next to the cursor shows the Chinese translation and the original annotated with furigana. It runs on a separate, pre-warmed session, so it never interrupts a meeting.
- **Voice interpretation** (Beta) — two-way speech: you speak Chinese and the other side hears Japanese in the meeting; they speak Japanese and you hear Chinese in your headphones. Recognition and synthesis run locally, with audio routed through VB-Audio virtual cables.

No platform lock-in (Teams / Zoom / Webex / Meet / Tencent Meeting all work). The first two features need no virtual audio cable, and speech recognition runs entirely on your CPU — only text is sent to Copilot.

## Requirements

| Item | Requirement |
|---|---|
| OS | Windows 10 / 11 (uses WASAPI loopback capture) |
| Account | A GitHub Copilot subscription, or an API key for any OpenAI-compatible service (it performs translation and summarization) |
| Hardware | Any x86-64 CPU; no discrete GPU. Recognition uses 4 threads by default |
| Microphone | Optional. When enabled your own speech is tagged as *you*; headphones recommended |
| Voice interpretation | Additionally needs VB-Audio virtual cables (CABLE A+B and Hi-Fi Cable; the app helps you download them), a ~400 MB speech runtime (installed by the app), and ~1.1 GB of speech models |

## Installation

### Option 1: Download the packaged exe

1. Grab `MeetingTranslate.exe` (about 400 MB, with recognition models and the Copilot runtime bundled) from [Releases](https://github.com/Yang-Code01/meeting-translate/releases/latest).
2. Run it. The first launch unpacks bundled resources (~15 s, with a splash screen); a 「译」 tray icon appears when ready.
3. Log in to Copilot on first use — see [Sign in](#sign-in-to-copilot) — or enter your own API key in Settings.

Nothing is written to the registry and no admin rights are needed. Uninstalling means deleting the exe and `%APPDATA%\meeting-translate\`.

To use **voice interpretation** you need a one-time setup (all steps live on the resource cards of the home screen; each card's 「详细」 button has the full story).
Until the speech runtime, speech models and virtual cables are all in place, the 「进入双向同传」 button stays greyed out and the card says what is missing.

1. **Speech runtime and models** — click 「下载」 on the speech-model card to open the download list: the speech runtime, MeloTTS,
   Style-Bert-VITS2 and the Japanese BERT each get a row with its own download button, or click 「全部下载」 to fetch everything (models ~1.1 GB).
   Every row has a progress bar and can be paused/resumed at any time; several rows can download in parallel, and mirror rate limits (HTTP 429)
   or dropped connections are retried and resumed automatically. Items already present show as ready.
   The runtime is a separate Python 3.12 environment created with [uv](https://docs.astral.sh/uv/) for Style-Bert-VITS2
   (it needs `numpy<2`, which cannot live inside the main app; ~400 MB). No pre-installed Python is needed: without uv,
   the app downloads uv first, which then fetches a standalone Python 3.12.
2. **Virtual cables** — click 「下载安装」 to install VB-Audio CABLE and Hi-Fi Cable, then set your meeting app's
   speaker to `CABLE Input` and microphone to `Hi-Fi Cable Output` (the app has step-by-step guidance, using Teams as an example).

### Option 2: Run from source

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+ (developed on 3.14).

```powershell
uv sync
uv run python scripts/fetch_models.py   # downloads recognition models into models/ (~380 MB)
uv run python main.py                   # run; Copilot login required
```

`uv.lock` pins the Tsinghua PyPI mirror (`pypi.tuna.tsinghua.edu.cn`) used on the author's machine. Inside mainland China you can just run `uv sync`; **outside China**, delete `uv.lock` first or re-resolve explicitly:

```powershell
uv sync --index-url https://pypi.org/simple --refresh
```

### Sign in to Copilot

An unauthenticated launch shows a guide with two options:

- **Option 1 (recommended)** — if the `copilot` CLI is installed, click “打开控制台执行 copilot login”, finish the login in the console that opens, then click the re-detect button.
- **Option 2** — paste a fine-grained Personal Access Token with the **Copilot Requests** permission.

### Using your own model service (bring your own API key)

1. Settings → “Model service” → “Custom (OpenAI-compatible)” → “New…”.
2. Fill in a name, the base URL (e.g. `https://api.deepseek.com/v1`), the model name (e.g. `deepseek-chat`) and the API key. Leave the prices empty if you don't want a cost estimate.
3. It takes effect immediately after OK — no restart.

- Anything that speaks `/chat/completions` works: DeepSeek, Kimi, Qwen, OpenAI, Ollama, vLLM, a corporate gateway.
- The key is encrypted with Windows DPAPI before it is written to `config.json`; on another machine or Windows account you must enter it again.
- Usage is counted in tokens: the overlay shows input/output tokens for the meeting, and estimates money only if you filled in prices. There is no account-level quota to query.
- Conversation history is kept locally and resent with every sentence, so token usage grows with meeting length; the rotation interval is configurable.
- Self-signed internal endpoints can tick “skip certificate verification” on that profile only. Do not enable it for public endpoints.
- Switching providers mid-meeting summarizes with the old backend first, then opens a session on the new one; on failure the old session keeps running.

## Usage

### Running a meeting

1. Press **Ctrl+Alt+J** (or use the tray menu / the “▶ 开始” button) to start; press again to pause, “▶ 继续” to resume.
2. Japanese scrolls live in the overlay; when a sentence ends (VAD detects silence) the Chinese translation is appended. *Them* and *you* are tracked as separate streams.
3. Click “■ 结束” to end the meeting; you are then asked whether to generate the recap.

The translation direction (JA→ZH / ZH→JA) lives in settings and is **locked while a meeting is running**; it applies from the next start. ZH→JA always uses SenseVoice for Chinese recognition, the glossary is reversed for the prompt, and minutes/action items are written in Japanese.

### Overlay and tray

- The overlay is always-on-top and semi-transparent; drag it to move, drag any corner to resize, and double-click near an edge to toggle fullscreen. Font size and opacity live in Settings.
- “—” hides it and the tray item “显示/隐藏字幕窗” brings it back. Turn off “show overlay” in Settings to start with only the tray icon and hotkey.
- Enable “Subtitle mode” in Settings to show translations only, without source text or unfinished fragments—useful as subtitles while watching a video.
- The “🎤 麦克风” button toggles your own microphone. The state is remembered and takes effect immediately.
- Subtitles follow the newest line. Scroll up to read back and it stops jumping to the bottom; a “↓ 回到底部” button appears top-right—click it or drag the scrollbar to the bottom to resume following.
- Left-clicking the tray icon brings the overlay to front during a meeting, otherwise opens the home screen.
- Tray menu: start/pause, end meeting, show/hide overlay, settings, models (quick switch), edit glossary, open recordings folder, summarize past meeting, sign in, quit.
- The settings window is modeless, so the overlay stays usable. After changing anything, a hint appears next to OK: “设置已修改，点击 OK 后生效”; closing without OK discards the changes. When the overlay is visible, the settings window opens on whichever side has more room.

### Selection translation (Alt+M)

Select a piece of Japanese text in **any application** and press **Alt+M** (configurable). A small popup appears near the cursor with the Chinese translation and the original annotated with furigana:

```
会議の資料を送ります
会議（かいぎ）の資料（しりょう）を送（おく）ります
```

- The Chinese, kana and kanji lines can be styled independently; the kanji line can be hidden.
- The popup is resizable and remembers its size.
- It uses a separate, pre-warmed session, so it never blocks the meeting translation pipeline.
- Records accumulate in memory; clicking “保存划词/句翻译历史” in settings writes them to `selection_translation_history.md` (deduplicated).

> How it works: the hotkey triggers an emulated Ctrl+C to read the current selection, then restores your previous clipboard. So **select the text first, then press the hotkey**.

### Voice interpretation (two-way)

You speak Chinese and the other side hears Japanese; they speak Japanese and you hear Chinese. Two virtual cable endpoints make both happen at once:

```
you ──mic──▶ recognize Chinese ──translate──▶ Japanese TTS ──▶ [CABLE-B Input] ══▶ meeting app mic [CABLE-B Output] ──▶ them
them ──▶ meeting app speaker [CABLE-A Input] ══▶ loopback capture ──recognize Japanese──translate──▶ Chinese TTS ──▶ your headphones
```

- Run 「自检」 (self-check) and 「试听日语」 (preview Japanese) from the overlay the first time, to confirm all four endpoints work and that your headphones and the virtual cable are not swapped.
- **Turn off noise suppression and echo cancellation** in your meeting app, otherwise the Japanese arriving through the virtual cable is treated as noise and cut.
- Chinese TTS (MeloTTS) plays locally and returns immediately; Japanese TTS (Style-Bert-VITS2) synthesizes in a separate process and needs ~15 s to load for the first sentence, then 0.5–1 s per sentence. It is pre-warmed in the background when you start interpretation.
- Sizes, locations and what every file does: 「详细」 on the 「语音模型」 card.
- While interpreting, the app temporarily switches the Windows default output to CABLE-A and switches it back when you stop or quit. If the app was killed before it could switch back, it does so on the next launch.

### Audio troubleshooting (speech not recognized, no Chinese audio)

Settings → 「声音与设备」 has two self-test buttons:

- **测试麦克风 (Test microphone)** — records 6 s from the selected mic with a live level meter, then reports OK / silent / failed to open.
  「系统默认」 follows the Windows default recording device, which may have been changed to a virtual cable such as CABLE / Hi-Fi Cable—pick your real microphone instead.
- **播放测试音 (Play test sound)** — plays a short chime on 「本地耳机（中文 TTS）」 and shows which device it actually used.
  The local headphone must not be a virtual cable (Chinese TTS and 「试听日语」 would go into the meeting app instead); 「自动」 prefers headphones. Starting interpretation with a virtual cable selected prompts you to switch.

If every device is silent and the app says “Windows 音频服务未运行” (Windows Audio service not running): start “Windows Audio” in `services.msc`, run `Start-Service Audiosrv` as administrator, or reboot.


- **Splitting** — 0.6 s of silence ends a segment. If the segment ends in て/が/けど/ので, or is only 1–3 characters, it is treated as unfinished and held for continuation (Chinese uses 的/和/但是/因为 etc.). It waits at most 3 s, flushes anyway after 20 s of merging, and force-splits single segments longer than 10 s. All three are configurable.
- **Fillers** — standalone え/あ/えー/えーと/あの/うん/ん are shown but not translated; the Chinese list is maintained separately. Both lists are editable.
- **Echo suppression** — with the microphone on and no headphones, the mic picks up the other side. Sentences tagged as *you* that are ≥ 0.75 similar to something *they* said within 4 s are dropped. This is only a safety net — please use headphones.

### Glossary

Tray menu → “编辑术语表”, one entry per line:

```
取引種別            # no translation given = keep the original term
リリース = 发布      # explicit translation
```

Terms feed both the translation prompt and the ASR hotword list. Changes apply from the next start.

> Hotwords only work with **ReazonSpeech**; the default SenseVoice engine does not support them, but the glossary still constrains translation.

### Models and usage

- Settings lists every available model from cheapest to most expensive, annotated with estimated AIC/hour, a multiple of the baseline model, and average latency. “测速” benchmarks the selected model.
- Switching models mid-meeting is immediate: the old model writes a recap, the new session carries that recap plus the last 5 sentences and the glossary, and the switch happens after warm-up.
- Unsupported reasoning effort is clamped down and reported in the status bar.
- The overlay shows “account AIC used” and “this meeting AIC” by default. “account quota remaining %” is off by default but always turns red below 10%, without interrupting the meeting.
- With a custom endpoint, the overlay and the meeting transcript report input/output tokens; money is estimated only when prices are filled in, and the account-quota row is hidden.
- One session per meeting, rotated every 30 minutes (configurable); every request carries the session history, which is the main controllable source of spend.

### After the meeting

You are asked whether to generate minutes, action items and a cleaned bilingual transcript:

- Progress is shown in stages in the overlay; when done the recap opens automatically and a tray notification appears. Failures can be retried.
- Recap filename: `<AI-generated title>_<timestamp>.md`, e.g. `订单缺陷复盘_2026-10-14_140530.md`.
- The bilingual transcript is `<meeting start time>.md`, in the same folder.
- Tray → “整理历史会议…” re-runs the recap on an existing bilingual transcript (recap files are rejected as invalid input).

### When settings take effect

| Immediate | From the next start |
|---|---|
| translation model, reasoning effort, microphone toggle, model service & endpoint | audio devices, direction, ASR engine, ASR correction, the three splitting values, session rotation interval |

## Data location

Everything lives in `%APPDATA%\meeting-translate\` and is never uploaded:

| File | Contents |
|---|---|
| `config.json` | settings (API keys for custom endpoints are stored here, encrypted with Windows DPAPI) |
| `model_stats.json` | measured latency and past meeting usage (for cost estimates) |
| `glossary.txt` | glossary |
| `selection_translation_history.md` | selection-translation history (written only when saved) |
| `transcripts\` | temporary records during a meeting; default location when no save folder is set |
| `models\` | optionally downloaded models (not needed with the exe); `models\tts\` holds the speech models |
| `runtime\sbv2\` | speech runtime (standalone Python 3.12 environment managed by uv) |
| `pending_output_restore.json` | original default output saved during interpretation; deleted after a normal stop |
| `app.log` | log |

After a meeting only Markdown remains: `<meeting start time>.md` and `<title>_<timestamp>.md`, written to the configured save folder (asked once at the first meeting end; cancelling keeps this meeting in the default folder and asks again next time). Temporary `.json` files are deleted after the recap completes or is declined, and kept on failure so it can be retried.

## Privacy and responsible use

- **Recording requires consent.** Recording laws differ by jurisdiction; obtain consent from participants before capturing meeting audio.
- The recognized **text** is sent to the model service you pick in Settings: GitHub Copilot (GitHub / Microsoft) by default, or any OpenAI-compatible endpoint you configure (DeepSeek, Kimi, Qwen, Ollama, a corporate gateway…). Audio never leaves your machine. The tool collects no telemetry and reports nothing back to the author.
- With a third-party endpoint the meeting text goes to that provider; retention and training policies are theirs to decide. The built-in templates flag common providers — check before using it for confidential meetings.
- Logs, transcripts and the glossary stay in your local `%APPDATA%` folder.
- A microphone cannot tell speakers apart: anyone nearby will be tagged as *you*. Turn the microphone off in situations where that matters.
- The app installs no drivers by itself (you install the virtual cables); it only switches the Windows default output temporarily during voice interpretation and switches it back afterwards.

## Known limitations

- Windows only: capture relies on WASAPI loopback; no cross-platform plans.
- The default Copilot path needs a subscription and is billed in AIC / requests; a custom endpoint is billed in tokens, with the quota set by the provider.
- The default SenseVoice engine has no hotword support; switch to ReazonSpeech (slower) if you need it.
- Speaker separation only distinguishes *them* / *you*; multiple people in one room are not separated.
- The exe is ~400 MB and needs unpacking on launch (~15 s on first run).
- Pausing force-finalizes the current sentence, so pausing mid-sentence can truncate it.

## Technical notes

- **Split before recognizing** — feeding a whole recording to sherpa-onnx only yields the last sentence, so silero VAD splits first (0.6 s silence and a 10 s cap by default) and each segment is decoded separately; SenseVoice decodes one Japanese sentence in ~0.35 s on 4 CPU threads.
- **Join unfinished sentences** — silence-only splitting shreds 「〜ですが／〜ので」, so a second layer adds Japanese sentence-end detection, a 3 s wait and a 20 s merge ceiling, then translates and displays the merged text as one line.
- **One request, two jobs** — the model corrects ASR errors and translates in the same response, costing no extra requests; the corrected source replaces the raw ASR text everywhere.
- **Visible cost** — measured latency and AIC prices are folded into an “AIC/hour” estimate per model, and any model can be benchmarked in place.
- **Isolated selection session** — selection translation runs on its own pre-warmed session, so it never disturbs meeting translation.

## Development

```powershell
uv sync
uv run python scripts/fetch_models.py   # ReazonSpeech + SenseVoice + silero VAD into models/
uv run python main.py
```

## Packaging

```powershell
uv run python main.py        # run once so the SDK downloads the Copilot runtime to %LOCALAPPDATA%
uv run python -m PyInstaller meeting-translate.spec --noconfirm
# output: dist\MeetingTranslate.exe
```

## License

- This project's code: [AGPL-3.0](LICENSE).
- Third-party components and models: [licenses/THIRD-PARTY.md](licenses/THIRD-PARTY.md).
- The GitHub Copilot runtime bundled in the exe is an unmodified copy of the CLI, distributed under section 2 of [its license](licenses/Copilot-CLI-LICENSE.md).
- SenseVoice weights come from [FunAudioLLM/SenseVoiceSmall](https://huggingface.co/FunAudioLLM/SenseVoiceSmall) (Alibaba Tongyi Lab), used under the FunASR Model Open Source License v1.1: attribute the source and author, and retain the model name.
- ReazonSpeech-k2-v2 comes from [reazon-research/ReazonSpeech](https://github.com/reazon-research/ReazonSpeech) (Apache-2.0).
- The UI is built on PySide6 / Qt 6 (LGPL-3.0).

GitHub and GitHub Copilot are trademarks of GitHub, Inc. This project is not affiliated with GitHub.
