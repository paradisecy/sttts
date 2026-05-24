# 🔊 sttts

> **Your screen, read aloud. Entirely local. No cloud. No keys. Just your GPU and your voice.**

**sttts** has four modes — screen capture, browser reader, MP3 export, and summarise — all powered by local AI models on your own hardware.

---

## ✨ Modes

### 1 — Screen capture (original)

```
🖥️  screen region  →  🔍 pixel diff  →  🧠 OCR  →  ✨ clean text  →  🗣️ TTS  →  🔊 speaker
                         (skip if                      (HTML/tables
                          idle)                         stripped)
```

Draw a rectangle on any part of your screen. sttts OCRs it every N seconds and speaks what changed.

### 2 — Browser reader (`--browser-url`)

```
🌐 URL  →  🎭 Playwright  →  📄 DOM text  →  🗣️ TTS  →  🔊 speaker
                                               ↕
                                        🖱️ live scroll + word highlight
```

Open a URL in a visible browser, extract clean text directly from the DOM, speak it paragraph by paragraph with live word highlighting and auto-scroll. Floating control bar for play/pause and navigation.

### 3 — MP3 export (`--save-mp3`)

```
🌐 URL  →  🎭 Playwright (headless)  →  📄 DOM text  →  🗣️ TTS  →  💾 MP3 file
```

Batch convert any web page to an MP3 file. No playback — just a file you can copy to your phone or MP3 player.

### 4 — Summarise & read (`--summarize-url`)

```
🌐 URL  →  🎭 Playwright (headless)  →  📄 DOM text  →  🦙 Ollama LLM  →  📝 summary
                                                                              ↓
                                                                🎭 visible browser + 🗣️ TTS
                                                                ⏸ play/pause/next/prev
                                                                💾 Download MP3 button
```

Condense any web page with a local Ollama model, then read the summary aloud with the full browser reader UI and a one-click MP3 download button.

---

## 📖 Auto page-turn (screen capture mode)

Point it at Kindle, an epub reader, or any paginated app. Draw a second rectangle over the **"next page"** button. After TTS finishes and the screen goes idle, sttts clicks the button and reads the next page — completely hands-free.

```bash
uv run python capture.py --next-btn -i 2
```

### 🌐 Web scroll mode (`--web`)

Same screen-capture pipeline but instead of clicking a button, sttts presses `Page Down` after each spoken portion. If OCR finds too much overlap with the previous text it nudges with `Arrow Down` until fresh content appears.

```bash
uv run python capture.py --web
```

---

## 🎭 Browser reader (`--browser-url`)

Speak and follow along any web page with a live in-browser UI.

```bash
uv run python capture.py --browser-url https://example.com/article
```

**What happens:**
1. Opens the page in a visible Chromium window
2. Tags every readable paragraph in the DOM with an index
3. For each paragraph: scrolls to it, highlights it, speaks it with Kokoro
4. Every Kokoro grapheme segment is highlighted word-by-word in real time

**Floating control bar** (injected at the top of the page):

```
⏮ Prev   ⏸ Pause   Next ⏭        paragraph 3 / 47        sttts
```

| Button | While playing | While paused |
|---|---|---|
| **⏮ Prev** | Jump to previous paragraph, keep playing | Jump to previous paragraph, resume playing |
| **⏸ Pause** | Stop audio within ~200 ms | — |
| **▶ Resume** | — | Resume from topmost visible paragraph (scroll first, then resume) |
| **Next ⏭** | Jump to next paragraph, keep playing | Jump to next paragraph, resume playing |

**Seek on resume:** while paused, scroll anywhere in the page, then click Resume — playback continues from the paragraph at the top of your viewport.

```bash
# Hidden browser (audio only)
uv run python capture.py --browser-url https://example.com --browser-headless

# Slower chunks, different voice
uv run python capture.py --browser-url https://example.com --voice am_adam --tts-speed 1.1
```

---

## 💾 MP3 export (`--save-mp3`)

Convert an entire web page to an MP3 file for offline listening on a phone or MP3 player.

```bash
# Filename derived from page title automatically
uv run python capture.py --save-mp3 https://example.com/article

# Custom output path
uv run python capture.py --save-mp3 https://example.com/article --mp3-out my-article.mp3

# Faster voice
uv run python capture.py --save-mp3 https://example.com/article --voice am_adam --tts-speed 1.15
```

**What happens:**
1. Fetches the page headlessly (no visible window)
2. Extracts readable paragraphs from the DOM (skips nav, header, footer, sidebars)
3. Synthesises each paragraph with Kokoro, writing audio **incrementally** to a temp WAV (no memory spike for long articles)
4. Converts WAV → MP3 via `ffmpeg -qscale:a 2` (high-quality VBR), deletes the temp WAV
5. Falls back to WAV if `ffmpeg` is not installed

Terminal output:
```
Fetching https://example.com/article …
Loading TTS (voice=af_heart, speed=1.0)…
Synthesising 47 paragraphs → My_Article.mp3

[   1/47] It was the best of times, it was the worst of times…
[   2/47] …
Duration: 01h 12m 34s
Converting to MP3…
Saved: My_Article.mp3  (98,432 KB)
```

---

## 🤖 Summarise & read (`--summarize-url`)

```
🌐 URL  →  🎭 Playwright (headless)  →  📄 DOM text  →  🦙 Ollama LLM  →  📝 summary HTML
                                                                               ↓
                                                                  🎭 visible browser
                                                                  🗣️ TTS + word highlight
                                                                  ⏸ play/pause/next/prev
                                                                  💾 Download MP3 button
```

Fetch any web page, condense it to a readable summary using a **local Ollama model**, then open the summary in a visible browser with the full reader experience — word highlighting, play/pause/next/prev control bar, and a one-click Download MP3 button.

```bash
# Default model: llama3.2
uv run python capture.py --summarize-url https://example.com/article

# Different model
uv run python capture.py --summarize-url https://example.com/article --summarize-model mistral

# Combine with voice/speed options
uv run python capture.py --summarize-url https://example.com/article --voice am_adam --tts-speed 1.1
```

**Requirements:** [Ollama](https://ollama.com) must be running locally (`ollama serve`) with the chosen model pulled (`ollama pull llama3.2`).

**What happens:**
1. Fetches the page headlessly and extracts readable text
2. Streams the summary from Ollama token-by-token (visible in the terminal)
3. Opens the formatted summary in a Chromium window
4. Speaks it paragraph by paragraph with live word highlighting
5. **Download MP3** button (green, in the control bar) — click once to start background synthesis; button updates as each paragraph is processed; shows `✅ filename.mp3` when done

---

## 🧠 Models

| Component | Model | Runs on |
|---|---|---|
| 🔍 OCR | [LightOnOCR-2-1B](https://huggingface.co/lightonai/LightOnOCR-2-1B) | AMD GPU (ROCm) / CPU |
| 🗣️ TTS | [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) | CPU |

Both models download automatically on first run via HuggingFace. **No API keys required.**

---

## 🛠️ System requirements

### 🐍 Python 3.13

```bash
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt-get update
sudo apt-get install -y python3.13 python3.13-dev python3.13-venv
```

### 📦 uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 🔧 apt packages

```bash
sudo apt-get install -y \
    slop \
    xdotool \
    libportaudio2 \
    libsndfile1 \
    libasound2t64 \
    ffmpeg
```

| Package | Purpose |
|---|---|
| `slop` | 🖱️ Mouse region selection — draw the capture rectangle |
| `xdotool` | 🤖 Simulate mouse clicks / key presses for auto scroll |
| `libportaudio2` | 🔊 Audio backend for TTS playback |
| `libsndfile1` | 🎵 Audio file I/O |
| `libasound2t64` | 🔈 ALSA sound library |
| `ffmpeg` | 🎞️ WAV → MP3 conversion for `--save-mp3` |

### 🔴 AMD GPU / ROCm (optional but recommended for screen-capture mode)

Tested on **RX 7900 XTX**. Without ROCm, the OCR model runs on CPU (slower). Follow the [official ROCm install guide](https://rocm.docs.amd.com/en/latest/deploy/linux/index.html), then verify with `rocm-smi`.

---

## 🚀 Install

```bash
git clone <repo>
cd sttts
uv sync

# Install Chromium for --browser-url and --save-mp3
uv run playwright install chromium
```

`uv sync` installs all Python dependencies:

| Package | Purpose |
|---|---|
| `torch` 2.8.0+rocm6.3 | 🔥 Deep learning runtime (ROCm) |
| `transformers` | 🤗 LightOn OCR model loader |
| `kokoro` | 🗣️ Kokoro-82M TTS |
| `playwright` | 🎭 Browser automation for `--browser-url` / `--save-mp3` |
| `trafilatura` | 📰 Article text extraction (fallback) |
| `mss` | 📸 Fast screen capture |
| `sounddevice` | 🔊 Audio playback |
| `soundfile` | 💾 WAV writing for `--save-mp3` |
| `numpy` / `pillow` | 🖼️ Image processing |

---

## 🎮 Usage

```bash
# ── Screen capture (OCR + TTS) ──────────────────────────────────────────────

# Basic: draw a region, capture + OCR + speak every 3s
uv run python capture.py

# Auto page-turn (Kindle, epub readers)
uv run python capture.py --next-btn -i 2

# Web page scroll mode (Page Down after each spoken section)
uv run python capture.py --web

# Faster interval, different voice
uv run python capture.py -i 1.5 --voice am_adam

# OCR only (no speech), or capture only (no OCR)
uv run python capture.py --no-tts
uv run python capture.py --no-ocr

# Fixed region (skip mouse selection)
uv run python capture.py --region 100,200,800,600

# ── Browser reader ───────────────────────────────────────────────────────────

# Visible browser with play/pause/next/prev UI and word highlighting
uv run python capture.py --browser-url https://example.com/article

# Headless (audio only, no window)
uv run python capture.py --browser-url https://example.com/article --browser-headless

# ── MP3 export ───────────────────────────────────────────────────────────────

# Save page as MP3 (filename from page title)
uv run python capture.py --save-mp3 https://example.com/article

# Custom output path
uv run python capture.py --save-mp3 https://example.com/article --mp3-out chapter1.mp3

# ── Summarise & read ─────────────────────────────────────────────────────────

# Summarise page with llama3.2, open in browser, speak aloud
uv run python capture.py --summarize-url https://example.com/article

# Different Ollama model
uv run python capture.py --summarize-url https://example.com/article --summarize-model mistral
```

---

## ⚙️ All options

### Screen capture

| Flag | Default | Description |
|---|---|---|
| `-i, --interval` | `3.0` | ⏱️ Seconds between captures |
| `-o, --output-dir` | `snapshots/` | 📁 Directory for snapshots |
| `-m, --monitor` | `1` | 🖥️ Monitor index |
| `--select` / `--no-select` | on | 🖱️ Draw region / use full monitor |
| `--region X,Y,W,H` | — | 📐 Fixed capture region |
| `--no-ocr` | off | 🚫 Capture images only |
| `--model` | `lightonai/LightOnOCR-2-1B` | 🧠 HuggingFace OCR model |
| `--prompt TEXT` | — | 💬 Prompt passed to the OCR model |
| `--max-tokens` | `1024` | 🔢 Max tokens for OCR generation |
| `--diff-threshold` | `1.0` | 🔍 Min % of changed pixels to trigger OCR |
| `--no-tts` | off | 🔇 Disable TTS |
| `--next-btn` | off | 📖 Draw next-page button; auto-clicked when idle after TTS |
| `--next-btn-region X,Y,W,H` | — | 📐 Fixed next-page button region |
| `--web` | off | 🌐 Page Down after TTS; Arrow Down to find new content |
| `--overlap-threshold` | `0.6` | 🔁 Max word overlap before nudging down (web mode) |
| `--list-monitors` | — | 🖥️ Print available monitors and exit |

### Voice (all modes)

| Flag | Default | Description |
|---|---|---|
| `--voice` | `af_heart` | 🎙️ Kokoro voice (`af_heart`, `am_adam`, `bf_emma` …) |
| `--tts-speed` | `1.0` | 🐇 Speech speed multiplier |

### Browser reader (`--browser-url`)

| Flag | Default | Description |
|---|---|---|
| `--browser-url URL` | — | 🌐 URL to open and read aloud |
| `--browser-headless` | off | 🙈 Hide the browser window |
| `--chunk-words` | `150` | 📏 Words per TTS chunk (unused in element-based mode) |

### MP3 export (`--save-mp3`)

| Flag | Default | Description |
|---|---|---|
| `--save-mp3 URL` | — | 💾 URL to convert to audio file |
| `--mp3-out FILE` | page title | 📂 Output path (`.mp3` or `.wav`) |

### Summarise & read (`--summarize-url`)

| Flag | Default | Description |
|---|---|---|
| `--summarize-url URL` | — | 🤖 Summarise URL with Ollama, then speak in browser reader |
| `--summarize-model MODEL` | `llama3.2` | 🦙 Ollama model name |

---

## ⚙️ How it works (screen capture)

```
┌─────────────────────────────────────────────────────────────────┐
│                        🔄 capture loop                          │
│                                                                 │
│  📸 screenshot ──► 🔍 pixel diff ──► changed?                   │
│                                          │                      │
│                               yes ◄──────┤──────► no            │
│                                ▼                   ▼            │
│                           🧠 OCR run         📖 next-btn / web  │
│                                ▼              yes ▼             │
│                          ✨ clean text   🖱️ click or Page Down  │
│                                ▼                                │
│                          🗣️ Kokoro TTS ──► 🔊 speaker           │
└─────────────────────────────────────────────────────────────────┘
```

- 🔒 Only **one instance** runs at a time — a new launch kills the previous automatically
- 🗑️ Only the **latest snapshot** is kept on disk
- 🔊 Audio is **released cleanly** on exit — no device lock left behind
