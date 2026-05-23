#!/usr/bin/env python3
"""Screen capture CLI — takes snapshots at a regular interval and runs OCR on each."""

import argparse
import atexit
import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import numpy as np
import sounddevice as sd
import mss
import mss.tools

os.environ["TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL"] = "1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture screen snapshots at a fixed interval and OCR each one.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-i", "--interval",
        type=float,
        default=3.0,
        metavar="SECONDS",
        help="Seconds between snapshots",
    )
    parser.add_argument(
        "-o", "--output-dir",
        type=Path,
        default=Path("snapshots"),
        metavar="DIR",
        help="Directory to save the current snapshot",
    )
    parser.add_argument(
        "-m", "--monitor",
        type=int,
        default=1,
        metavar="N",
        help="Monitor index to capture (1 = primary); ignored when region is selected",
    )
    parser.add_argument(
        "--prefix",
        default="snap",
        help="Filename prefix for snapshots",
    )
    parser.add_argument(
        "--list-monitors",
        action="store_true",
        help="List available monitors and exit",
    )
    parser.add_argument(
        "--region",
        metavar="X,Y,W,H",
        help="Capture region as x,y,width,height (skips slop selection)",
    )
    parser.add_argument(
        "--select",
        action="store_true",
        default=True,
        help="Draw a rectangle with the mouse to select the capture region (default)",
    )
    parser.add_argument(
        "--no-select",
        dest="select",
        action="store_false",
        help="Capture the full monitor instead of drawing a region",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Disable OCR; just capture images",
    )
    parser.add_argument(
        "--model",
        default="lightonai/LightOnOCR-2-1B",
        help="HuggingFace model ID for OCR",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        metavar="TEXT",
        help="Optional prompt to pass to the OCR model",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=1024,
        help="Max new tokens for OCR generation",
    )
    parser.add_argument(
        "--diff-threshold",
        type=float,
        default=1.0,
        metavar="PERCENT",
        help="Minimum %% of changed pixels to trigger OCR (0 = always run OCR)",
    )
    parser.add_argument(
        "--no-tts",
        action="store_true",
        help="Disable text-to-speech; only print OCR text",
    )
    parser.add_argument(
        "--voice",
        default="af_heart",
        help="Kokoro voice name (e.g. af_heart, am_adam, bf_emma)",
    )
    parser.add_argument(
        "--tts-speed",
        type=float,
        default=1.0,
        help="TTS speech speed multiplier",
    )
    parser.add_argument(
        "--next-btn",
        action="store_true",
        help="Draw a rectangle over the 'next page' button; clicked when content is idle after TTS",
    )
    parser.add_argument(
        "--next-btn-region",
        metavar="X,Y,W,H",
        help="'Next page' button region as x,y,width,height (skips slop selection)",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Web scroll mode: after TTS, Page_Down to the next portion; Arrow Down to nudge when old text is still visible",
    )
    parser.add_argument(
        "--overlap-threshold",
        type=float,
        default=0.6,
        metavar="RATIO",
        help="Word overlap ratio above which the screen is considered to still show old text (web mode, 0–1)",
    )
    parser.add_argument(
        "--save-mp3",
        metavar="URL",
        help="Fetch URL, synthesise all text with Kokoro, save as MP3 (no playback)",
    )
    parser.add_argument(
        "--mp3-out",
        type=Path,
        metavar="FILE",
        help="Output file for --save-mp3 (default: derived from page title, e.g. My_Article.mp3)",
    )
    parser.add_argument(
        "--browser-url",
        metavar="URL",
        help="Browser-controlled mode: open URL in a browser, extract text from DOM, speak and auto-scroll (no OCR)",
    )
    parser.add_argument(
        "--browser-headless",
        action="store_true",
        help="Hide the browser window in --browser-url mode (default: visible)",
    )
    parser.add_argument(
        "--chunk-words",
        type=int,
        default=150,
        metavar="N",
        help="Words per spoken chunk in --browser-url mode",
    )
    return parser.parse_args()


def list_monitors() -> None:
    with mss.mss() as sct:
        for i, mon in enumerate(sct.monitors):
            tag = " (all)" if i == 0 else ""
            print(f"  [{i}]{tag} {mon['width']}x{mon['height']} at ({mon['left']},{mon['top']})")


def click_center(region: dict) -> None:
    """Click the center of the given region using xdotool."""
    if not shutil.which("xdotool"):
        print("Warning: xdotool not found, cannot click next-page button.", file=sys.stderr)
        return
    x = region["left"] + region["width"] // 2
    y = region["top"] + region["height"] // 2
    subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "1"], capture_output=True)


def press_key_in_region(region: dict, key: str) -> None:
    """Move the mouse into the region so the browser is under the cursor, then send a key."""
    if not shutil.which("xdotool"):
        print("Warning: xdotool not found, cannot send key.", file=sys.stderr)
        return
    x = region["left"] + region["width"] // 2
    y = region["top"] + region["height"] // 2
    subprocess.run(["xdotool", "mousemove", str(x), str(y)], capture_output=True)
    time.sleep(0.05)
    subprocess.run(["xdotool", "key", "--clearmodifiers", key], capture_output=True)


def text_overlap_ratio(old_text: str, new_text: str) -> float:
    """Fraction of new_text words that also appear in old_text."""
    old_words = set(re.findall(r'\w+', old_text.lower()))
    new_words = re.findall(r'\w+', new_text.lower())
    if not new_words or not old_words:
        return 0.0
    return sum(1 for w in new_words if w in old_words) / len(new_words)


def select_region(label: str = "capture") -> dict:
    """Launch slop so the user can drag a rectangle; return mss-style region dict."""
    if not shutil.which("slop"):
        print(
            "Error: 'slop' is not installed. Install it with:\n"
            "  sudo apt-get install slop\n"
            "Or pass --no-select to capture the full monitor.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Draw a rectangle to select the {label} region…")
    try:
        result = subprocess.run(
            ["slop", "--format", "%x %y %w %h"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        print("Region selection cancelled.", file=sys.stderr)
        sys.exit(1)

    x, y, w, h = map(int, result.stdout.strip().split())
    if w == 0 or h == 0:
        print("Error: selected region has zero size.", file=sys.stderr)
        sys.exit(1)

    print(f"Region: {w}x{h} at ({x},{y})\n")
    return {"left": x, "top": y, "width": w, "height": h}


class _TableParser(HTMLParser):
    """Extract rows from an HTML table as lists of cell strings."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = ""

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(self._cell.strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell += data


def to_speakable(text: str) -> str:
    """Convert OCR output (possibly HTML) to clean speakable text."""
    text = text.strip()
    if not text:
        return ""

    # If it contains HTML tags, parse and flatten
    if re.search(r"<[a-zA-Z]", text):
        parser = _TableParser()
        parser.feed(text)
        if parser.rows:
            # Convert each data row to "SYMBOL: VALUE, CHANGE, PERCENT"
            lines = []
            for row in parser.rows:
                # Skip header rows (empty or all-blank cells)
                parts = [c for c in row if c]
                if not parts:
                    continue
                lines.append(", ".join(parts))
            speakable = ". ".join(lines)
        else:
            # Generic HTML — strip all tags
            speakable = re.sub(r"<[^>]+>", " ", text)

        # Clean up whitespace
        speakable = re.sub(r"\s+", " ", speakable).strip()
        # Make percent sign speakable
        speakable = speakable.replace("%", " percent")
        return speakable

    # Plain text — just normalise whitespace
    return re.sub(r"\s+", " ", text).strip()


def load_ocr_model(model_id: str):
    """Load LightOn OCR model and processor; return (model, processor, device)."""
    import torch
    from transformers import LightOnOcrForConditionalGeneration, LightOnOcrProcessor

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16
    print(f"Loading OCR model '{model_id}' on {device}…")

    model = LightOnOcrForConditionalGeneration.from_pretrained(
        model_id,
        torch_dtype=dtype,
        trust_remote_code=True,
    ).to(device)
    processor = LightOnOcrProcessor.from_pretrained(model_id)

    print("OCR model ready.\n")
    return model, processor, device


def run_ocr(image_path: Path, model, processor, device: str, prompt: str | None, max_tokens: int) -> str:
    """Run OCR on a single image file; return extracted text."""
    import torch

    dtype = torch.bfloat16

    content = [{"type": "image", "url": str(image_path)}]
    if prompt:
        content.append({"type": "text", "text": prompt})

    conversation = [{"role": "user", "content": content}]

    inputs = processor.apply_chat_template(
        conversation,
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
    )
    inputs = {
        k: v.to(device=device, dtype=dtype) if v.is_floating_point() else v.to(device)
        for k, v in inputs.items()
    }

    output_ids = model.generate(**inputs, max_new_tokens=max_tokens)
    generated_ids = output_ids[0, inputs["input_ids"].shape[1]:]
    return processor.decode(generated_ids, skip_special_tokens=True)


def _get_sink_inputs() -> list[tuple[str, str]]:
    """Return list of (sink_input_id, volume) for all active pipewire sink inputs except ours."""
    try:
        out = subprocess.check_output(["pactl", "list", "sink-inputs"], text=True)
    except Exception:
        return []
    inputs = []
    current_id = None
    current_vol = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Sink Input #"):
            if current_id and current_vol:
                inputs.append((current_id, current_vol))
            current_id = line.split("#")[1]
            current_vol = None
        elif line.startswith("Volume:") and current_id:
            # grab the first percentage, e.g. "50%"
            m = re.search(r"(\d+)%", line)
            if m:
                current_vol = m.group(1)
    if current_id and current_vol:
        inputs.append((current_id, current_vol))
    return inputs


def _duck(inputs: list[tuple[str, str]], duck_vol: int = 20) -> None:
    for sid, _ in inputs:
        subprocess.run(["pactl", "set-sink-input-volume", sid, f"{duck_vol}%"],
                       capture_output=True)


def _unduck(inputs: list[tuple[str, str]]) -> None:
    for sid, vol in inputs:
        subprocess.run(["pactl", "set-sink-input-volume", sid, f"{vol}%"],
                       capture_output=True)


class _Seek(Exception):
    """Raised from on_grapheme to jump to a specific element index."""
    def __init__(self, idx: int):
        self.idx = idx


class _BrowserState:
    """Thread-safe state shared between the main thread and Playwright's
    background thread (where expose_function callbacks run)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._paused = False
        self._seek = -1          # -1 = no pending seek
        self._resume = threading.Event()

    # --- called from Playwright background thread ---

    def on_pause(self):
        with self._lock:
            self._paused = True
            self._resume.clear()
        sd.stop()

    def on_resume(self, seek_idx):
        with self._lock:
            self._paused = False
            self._seek = int(seek_idx) if seek_idx is not None else -1
        self._resume.set()

    def on_navigate(self, seek_idx):
        """Next / Previous — interrupt current audio and jump, keep playing."""
        with self._lock:
            self._seek = int(seek_idx)
            self._paused = False   # ensure we stay playing after the jump
        self._resume.set()         # unblock _check_interrupted if it was paused
        sd.stop()                  # cut current audio chunk immediately

    # --- called from main thread ---

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def has_seek(self) -> bool:
        with self._lock:
            return self._seek >= 0

    def is_interrupted(self) -> bool:
        """True when the chunk loop should stop (paused OR navigation pending)."""
        with self._lock:
            return self._paused or self._seek >= 0

    def wait_resume(self, timeout: float = 0.5) -> bool:
        return self._resume.wait(timeout=timeout)

    def consume_seek(self) -> int:
        with self._lock:
            s = self._seek
            self._seek = -1
            return s


def _inject_browser_ui(page) -> None:
    """Inject CSS highlight rules + a fixed play/pause control bar."""
    page.evaluate("""() => {
        if (document.getElementById('sttts-style')) return;

        // --- CSS ---
        const style = document.createElement('style');
        style.id = 'sttts-style';
        style.textContent = `
            [data-sttts-active] {
                background: rgba(255, 250, 180, 0.55) !important;
                outline: 2px solid #FFA500;
                border-radius: 4px;
            }
            span.sttts-word {
                background: #FFD700;
                color: #000 !important;
                border-radius: 3px;
                padding: 0 2px;
            }
            #sttts-bar * { box-sizing: border-box; }
        `;
        document.head.appendChild(style);

        // --- Control bar ---
        const bar = document.createElement('div');
        bar.id = 'sttts-bar';
        bar.style.cssText = [
            'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:2147483647',
            'background:rgba(18,18,18,0.92)', 'color:#fff',
            'padding:10px 20px', 'display:flex', 'align-items:center', 'gap:14px',
            'font-family:-apple-system,BlinkMacSystemFont,sans-serif', 'font-size:14px',
            'backdrop-filter:blur(10px)', 'box-shadow:0 2px 16px rgba(0,0,0,0.5)',
        ].join(';');
        const btnStyle = `
            border:none; border-radius:20px; padding:7px 16px; cursor:pointer;
            font-size:15px; font-weight:700; white-space:nowrap;
            transition:background 0.2s, color 0.2s;
        `;
        bar.innerHTML = `
            <button id="sttts-prev" style="${btnStyle} background:#444; color:#fff;">⏮ Prev</button>
            <button id="sttts-btn"  style="${btnStyle} background:#FFD700; color:#000;">⏸ Pause</button>
            <button id="sttts-next" style="${btnStyle} background:#444; color:#fff;">Next ⏭</button>
            <span id="sttts-pos" style="opacity:0.65; white-space:nowrap; margin-left:8px;">Starting…</span>
            <span style="margin-left:auto; opacity:0.35; font-size:11px; letter-spacing:2px; text-transform:uppercase;">sttts</span>
        `;
        document.body.prepend(bar);
        document.body.style.marginTop = (bar.offsetHeight + 8) + 'px';

        // --- Helpers ---
        window._sttts_paused = false;

        function setPlaying() {
            window._sttts_paused = false;
            const btn = document.getElementById('sttts-btn');
            btn.textContent = '⏸ Pause';
            btn.style.background = '#FFD700';
            btn.style.color = '#000';
        }
        function setPaused() {
            window._sttts_paused = true;
            const btn = document.getElementById('sttts-btn');
            btn.textContent = '▶ Resume';
            btn.style.background = '#4CAF50';
            btn.style.color = '#fff';
        }
        function currentIdx() {
            const el = document.querySelector('[data-sttts-active]');
            return el ? parseInt(el.getAttribute('data-sttts')) : 0;
        }
        function topVisibleIdx() {
            let idx = -1;
            document.querySelectorAll('[data-sttts]').forEach(el => {
                if (idx >= 0) return;
                const r = el.getBoundingClientRect();
                if (r.bottom > 0 && r.top < window.innerHeight) {
                    idx = parseInt(el.getAttribute('data-sttts'));
                }
            });
            return idx;
        }

        // --- Pause / Resume ---
        document.getElementById('sttts-btn').addEventListener('click', () => {
            if (!window._sttts_paused) {
                setPaused();
                window.sttts_pause();
            } else {
                setPlaying();
                window.sttts_resume(topVisibleIdx());
            }
        });

        // --- Previous ---
        document.getElementById('sttts-prev').addEventListener('click', () => {
            setPlaying();
            window.sttts_navigate(Math.max(0, currentIdx() - 1));
        });

        // --- Next ---
        document.getElementById('sttts-next').addEventListener('click', () => {
            setPlaying();
            window.sttts_navigate(currentIdx() + 1);
        });
    }""")


def _update_bar_status(page, text: str) -> None:
    page.evaluate(
        "t => { const el = document.getElementById('sttts-pos'); if (el) el.textContent = t; }",
        text,
    )


def _check_interrupted(state: _BrowserState, is_stopped) -> int | None:
    """Handle pause, resume-with-seek, and next/prev navigation.
    Returns a seek element idx, or None to continue at the current position."""
    if not state.is_paused() and not state.has_seek():
        return None
    if state.is_paused():
        print("[paused]", flush=True)
        while not is_stopped():
            if state.wait_resume(timeout=0.5):
                break
        if is_stopped():
            return None
    seek = state.consume_seek()
    if seek >= 0:
        print(f"[seek → {seek}]", flush=True)
    return seek if seek >= 0 else None


def _activate_element(page, idx: int) -> None:
    """Mark element idx as the currently reading element and scroll to it."""
    page.evaluate("""idx => {
        document.querySelectorAll('[data-sttts-active]').forEach(el => {
            el.removeAttribute('data-sttts-active');
        });
        document.querySelectorAll('span.sttts-word').forEach(sp => {
            sp.replaceWith(document.createTextNode(sp.textContent));
        });
        const el = document.querySelector(`[data-sttts="${idx}"]`);
        if (!el) return;
        el.setAttribute('data-sttts-active', '1');
        el.scrollIntoView({behavior: 'smooth', block: 'center'});
    }""", idx)


def _highlight_word(page, idx: int, phrase: str) -> None:
    """Highlight a specific phrase within element idx (scoped search — always matches)."""
    page.evaluate("""([idx, phrase]) => {
        const container = document.querySelector(`[data-sttts="${idx}"]`);
        if (!container || !phrase) return;

        // Remove previous word highlight within this element only
        container.querySelectorAll('span.sttts-word').forEach(sp => {
            sp.replaceWith(document.createTextNode(sp.textContent));
        });
        container.normalize();

        // Walk text nodes inside this element
        const walk = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
        let n;
        while ((n = walk.nextNode())) {
            const i = n.textContent.indexOf(phrase);
            if (i < 0) continue;
            const end = Math.min(i + phrase.length, n.textContent.length);
            try {
                const r = document.createRange();
                r.setStart(n, i);
                r.setEnd(n, end);
                const sp = document.createElement('span');
                sp.className = 'sttts-word';
                r.surroundContents(sp);
            } catch(e) {}
            return;
        }
    }""", [idx, phrase])


def _clear_browser_highlights(page) -> None:
    page.evaluate("""() => {
        document.querySelectorAll('[data-sttts-active]').forEach(el => {
            el.removeAttribute('data-sttts-active');
        });
        document.querySelectorAll('span.sttts-word').forEach(sp => {
            sp.replaceWith(document.createTextNode(sp.textContent));
        });
        document.body.normalize();
    }""")


def load_tts(voice: str, speed: float):
    """Load Kokoro TTS pipeline on CPU; return callable speak(text, ...)."""
    import torch
    from kokoro import KPipeline

    print(f"Loading TTS (voice={voice}, speed={speed}, device=cpu)…")
    with torch.device("cpu"):
        pipeline = KPipeline(lang_code="a", device="cpu")

    def speak(text: str, on_grapheme=None, pump=None, pause_state=None) -> None:
        """
        on_grapheme: called with each grapheme string before its audio plays.
        pump:        called between 200ms audio chunks — pumps Playwright's event
                     queue so expose_function callbacks (pause/resume) are delivered.
        pause_state: _BrowserState instance; if set, chunk playback stops when paused.
        """
        if not text.strip():
            return
        others = _get_sink_inputs()
        _duck(others)
        try:
            CHUNK = int(24000 * 0.2)  # 200 ms per chunk
            for gs, _, audio in pipeline(text, voice=voice, speed=speed):
                if on_grapheme is not None and gs and gs.strip():
                    on_grapheme(gs.strip())
                if pause_state is not None:
                    i = 0
                    while i < len(audio):
                        if pause_state.is_interrupted():
                            break
                        sd.play(audio[i:i + CHUNK], samplerate=24000)
                        sd.wait()
                        if pump is not None:
                            pump()   # lets Playwright deliver queued callbacks
                        i += CHUNK
                else:
                    sd.play(audio, samplerate=24000)
                    sd.wait()
        finally:
            _unduck(others)

    print("TTS ready.\n")
    return speak


def pixel_diff_percent(prev: np.ndarray, curr: np.ndarray) -> float:
    """Return the percentage of pixels that changed between two RGB arrays."""
    diff = np.abs(prev.astype(np.int16) - curr.astype(np.int16))
    changed = np.any(diff > 10, axis=-1)  # per-pixel; ignore tiny noise
    return changed.mean() * 100.0


def capture_loop(
    interval: float,
    output_dir: Path,
    region: dict,
    prefix: str,
    ocr_model,
    ocr_processor,
    ocr_device: str,
    ocr_prompt: str | None,
    max_tokens: int,
    diff_threshold: float,
    speak,
    next_btn_region: dict | None,
    web_scroll: bool = False,
    overlap_threshold: float = 0.6,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    stop = False

    def _handle_signal(sig, frame):
        nonlocal stop
        stop = True
        sd.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    print(
        f"Capturing {region['width']}x{region['height']} at "
        f"({region['left']},{region['top']}) every {interval}s → {output_dir}/"
    )
    if next_btn_region:
        print(
            f"Next-page button: {next_btn_region['width']}x{next_btn_region['height']} "
            f"at ({next_btn_region['left']},{next_btn_region['top']})"
        )
    if ocr_model is not None and diff_threshold > 0:
        print(f"OCR triggers when >{diff_threshold:.1f}% of pixels change.")
    print("Press Ctrl+C to stop.\n")

    count = 0
    last_file: Path | None = None
    prev_pixels: np.ndarray | None = None
    waiting_for_next_page = False
    last_spoken: str = ""
    nudge_count: int = 0

    with mss.mss() as sct:
        while not stop:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            filename = output_dir / f"{prefix}_{ts}.png"

            screenshot = sct.grab(region)
            curr_pixels = np.frombuffer(screenshot.rgb, dtype=np.uint8).reshape(
                screenshot.height, screenshot.width, 3
            )

            mss.tools.to_png(screenshot.rgb, screenshot.size, output=str(filename))

            if last_file is not None and last_file.exists():
                last_file.unlink()
            last_file = filename

            count += 1

            # Diff check
            if prev_pixels is not None and diff_threshold > 0:
                changed_pct = pixel_diff_percent(prev_pixels, curr_pixels)
                if changed_pct < diff_threshold:
                    # Content is idle
                    if waiting_for_next_page and next_btn_region:
                        print(f"[{count:>4}] idle after TTS — clicking next-page button")
                        click_center(next_btn_region)
                        prev_pixels = None
                        waiting_for_next_page = False
                        nudge_count = 0
                    elif waiting_for_next_page and web_scroll:
                        print(f"[{count:>4}] idle after TTS — pressing Page_Down")
                        press_key_in_region(region, "Page_Down")
                        prev_pixels = None
                        waiting_for_next_page = False
                        nudge_count = 0
                    else:
                        print(f"[{count:>4}] {changed_pct:.2f}% changed — idle, skipping OCR")
                        prev_pixels = curr_pixels
                    deadline = time.monotonic() + interval
                    while not stop and time.monotonic() < deadline:
                        time.sleep(0.05)
                    continue
                else:
                    print(f"\n[{count:>4}] {filename}  ({changed_pct:.2f}% changed)")
                    waiting_for_next_page = False
            else:
                print(f"\n[{count:>4}] {filename}")

            prev_pixels = curr_pixels

            if ocr_model is not None:
                t0 = time.monotonic()
                text = run_ocr(filename, ocr_model, ocr_processor, ocr_device, ocr_prompt, max_tokens)
                elapsed = time.monotonic() - t0
                print(f"--- OCR ({elapsed:.1f}s) ---")
                print(text if text.strip() else "(no text detected)")
                print("---")
                if speak is not None and text.strip():
                    speakable = to_speakable(text)
                    if speakable:
                        # Web mode: nudge down if old text is still largely visible
                        if web_scroll and last_spoken and nudge_count < 20:
                            overlap = text_overlap_ratio(last_spoken, speakable)
                            if overlap > overlap_threshold:
                                print(f"[web] {overlap:.0%} overlap with last spoken — nudging Arrow Down")
                                for _ in range(3):
                                    press_key_in_region(region, "Down")
                                    time.sleep(0.1)
                                prev_pixels = None
                                nudge_count += 1
                                deadline = time.monotonic() + interval
                                while not stop and time.monotonic() < deadline:
                                    time.sleep(0.05)
                                continue

                        speak(speakable)
                        last_spoken = speakable
                        nudge_count = 0
                        if next_btn_region or web_scroll:
                            waiting_for_next_page = True

            deadline = time.monotonic() + interval
            while not stop and time.monotonic() < deadline:
                time.sleep(0.05)

    print(f"\nStopped. {count} snapshot(s) taken.")


def _group_chunks(paragraphs: list[str], max_words: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_words = 0
    for para in paragraphs:
        words = len(para.split())
        if current and current_words + words > max_words:
            chunks.append(" ".join(current))
            current, current_words = [para], words
        else:
            current.append(para)
            current_words += words
    if current:
        chunks.append(" ".join(current))
    return chunks


def browser_reader_loop(url: str, speak, chunk_words: int, headless: bool) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "playwright not installed. Run:\n"
            "  uv add playwright && uv run playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    stop = False

    def _handle_signal(sig, frame):
        nonlocal stop
        stop = True
        sd.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        # Expose Python callbacks so browser button can drive them directly.
        # These run in Playwright's background thread — _BrowserState is thread-safe.
        bs = _BrowserState()
        page.expose_function("sttts_pause",    bs.on_pause)
        page.expose_function("sttts_resume",   bs.on_resume)
        page.expose_function("sttts_navigate", bs.on_navigate)

        print(f"Opening {url} …")
        try:
            page.goto(url, wait_until="networkidle", timeout=30_000)
        except Exception as e:
            print(f"Error loading page: {e}", file=sys.stderr)
            browser.close()
            return

        # Tag every readable block element with a data-sttts index and
        # return its text directly from the DOM — no external extractor needed.
        elements = page.evaluate("""() => {
            const SKIP = el => !!el.closest(
                'nav, header, footer, aside, [role=navigation], [role=banner], [role=complementary]'
            );
            const results = [];
            let idx = 0;
            const sel = 'p, h1, h2, h3, h4, h5, h6, li, blockquote';
            document.querySelectorAll(sel).forEach(el => {
                if (SKIP(el)) return;
                const text = (el.innerText || '').trim();
                if (text.length < 20) return;
                el.setAttribute('data-sttts', idx);
                results.push({idx: idx++, text});
            });
            return results;
        }""")

        if not elements:
            print("No readable content found on this page.", file=sys.stderr)
            browser.close()
            return

        print(f"Found {len(elements)} readable elements.")
        print("Press Ctrl+C to stop.\n")

        _inject_browser_ui(page)

        def pump():
            """Trivial Playwright call that flushes any queued expose_function callbacks."""
            try:
                page.evaluate("() => null")
            except Exception:
                pass

        cursor = 0  # index into elements list
        while not stop and cursor < len(elements):
            el   = elements[cursor]
            idx  = el["idx"]
            text = el["text"]
            preview = text[:80] + ("…" if len(text) > 80 else "")
            print(f"\n[{idx + 1}/{len(elements)}] {preview}")

            _activate_element(page, idx)
            _update_bar_status(page, f"{idx + 1} / {len(elements)}")

            if speak is None:
                cursor += 1
                continue

            try:
                def on_grapheme(phrase, _idx=idx):
                    _highlight_word(page, _idx, phrase)
                    seek = _check_interrupted(bs, lambda: stop)
                    if seek is not None and seek != _idx:
                        raise _Seek(seek)

                speak(text, on_grapheme=on_grapheme, pump=pump, pause_state=bs)

                # Catch interrupts on the last audio chunk of this element
                seek = _check_interrupted(bs, lambda: stop)
                if seek is not None and seek != idx:
                    raise _Seek(seek)

                cursor += 1

            except _Seek as s:
                target = next(
                    (i for i, e in enumerate(elements) if e["idx"] >= s.idx),
                    len(elements),
                )
                print(f"[seek] → element {s.idx} (list pos {target})", flush=True)
                cursor = target

        _clear_browser_highlights(page)
        _update_bar_status(page, "Done")
        if not stop:
            print("\nFinished reading the page.")
        browser.close()


def save_mp3_loop(url: str, out: Path | None, voice: str, speed: float) -> None:
    """Fetch a web page, synthesise all readable text with Kokoro, save as MP3."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed. Run: uv add playwright && uv run playwright install chromium", file=sys.stderr)
        sys.exit(1)

    import soundfile as sf
    import torch
    from kokoro import KPipeline

    # ── 1. Extract text from page ─────────────────────────────────────────────
    print(f"Fetching {url} …")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=30_000)
        except Exception as e:
            print(f"Error loading page: {e}", file=sys.stderr)
            browser.close()
            return

        title = page.title() or "audio"
        elements = page.evaluate("""() => {
            const SKIP = el => !!el.closest(
                'nav, header, footer, aside, [role=navigation], [role=banner], [role=complementary]'
            );
            const results = [];
            document.querySelectorAll('p, h1, h2, h3, h4, h5, h6, li, blockquote').forEach(el => {
                if (SKIP(el)) return;
                const text = (el.innerText || '').trim();
                if (text.length >= 20) results.push(text);
            });
            return results;
        }""")
        browser.close()

    if not elements:
        print("No readable text found on this page.", file=sys.stderr)
        return

    # ── 2. Resolve output path ────────────────────────────────────────────────
    if out is None:
        safe = re.sub(r'[^\w\s-]', '', title).strip()
        safe = re.sub(r'\s+', '_', safe)[:60] or "audio"
        out = Path(f"{safe}.mp3")

    wav_path = out.with_suffix('.wav')

    # ── 3. Load TTS ───────────────────────────────────────────────────────────
    print(f"Loading TTS (voice={voice}, speed={speed})…")
    with torch.device("cpu"):
        pipeline = KPipeline(lang_code="a", device="cpu")

    # ── 4. Synthesise and write WAV incrementally ─────────────────────────────
    total = len(elements)
    total_samples = 0
    print(f"Synthesising {total} paragraphs → {out}\n")

    try:
        with sf.SoundFile(str(wav_path), mode='w', samplerate=24000, channels=1, subtype='PCM_16') as wav:
            for i, text in enumerate(elements):
                preview = text[:70] + ('…' if len(text) > 70 else '')
                print(f"[{i + 1:>4}/{total}] {preview}", flush=True)
                for _, _, audio in pipeline(text, voice=voice, speed=speed):
                    wav.write(audio)
                    total_samples += len(audio)
    except KeyboardInterrupt:
        print("\nInterrupted — partial WAV saved.", file=sys.stderr)

    duration = total_samples / 24_000
    h, rem = divmod(int(duration), 3600)
    m, s   = divmod(rem, 60)
    dur_str = (f"{h}h " if h else "") + f"{m:02d}m {s:02d}s"
    print(f"\nDuration: {dur_str}")

    # ── 5. Convert WAV → MP3 via ffmpeg ───────────────────────────────────────
    if out.suffix.lower() == '.mp3':
        if shutil.which('ffmpeg'):
            print("Converting to MP3…")
            res = subprocess.run(
                ['ffmpeg', '-y', '-i', str(wav_path),
                 '-codec:a', 'libmp3lame', '-qscale:a', '2', str(out)],
                capture_output=True,
            )
            if res.returncode == 0:
                wav_path.unlink()
                size_kb = out.stat().st_size // 1024
                print(f"Saved: {out}  ({size_kb:,} KB)")
                return
            else:
                print("ffmpeg conversion failed; keeping WAV.", file=sys.stderr)
                print(res.stderr.decode(errors='replace'), file=sys.stderr)
        else:
            print("ffmpeg not found — saved as WAV instead. Install ffmpeg for MP3 output.")

    size_kb = wav_path.stat().st_size // 1024
    print(f"Saved: {wav_path}  ({size_kb:,} KB)")


LOCKFILE = Path("/tmp/sttts_capture.lock")


def acquire_single_instance() -> None:
    """Kill any previous instance and acquire an exclusive lock."""
    # If a PID file exists, kill the old process tree
    pid_file = Path("/tmp/sttts_capture.pid")
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            # Kill the entire process group so child threads die too
            os.killpg(os.getpgid(old_pid), signal.SIGKILL)
            print(f"Killed previous instance (pid {old_pid})")
        except (ProcessLookupError, ValueError, PermissionError):
            pass
        pid_file.unlink(missing_ok=True)

    # Write our own PID
    pid_file.write_text(str(os.getpid()))

    # Also hold an exclusive flock so a second instance can detect us
    lock_fd = open(LOCKFILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another instance is already running.", file=sys.stderr)
        sys.exit(1)

    # Cleanup on any exit — including SIGKILL survivors via atexit
    def _cleanup():
        sd.stop()
        pid_file.unlink(missing_ok=True)
        LOCKFILE.unlink(missing_ok=True)
        try:
            lock_fd.close()
        except Exception:
            pass

    atexit.register(_cleanup)


def main() -> None:
    args = parse_args()

    if not args.list_monitors:
        acquire_single_instance()

    if args.list_monitors:
        list_monitors()
        return

    # Save-to-MP3 mode — headless, no playback
    if args.save_mp3:
        save_mp3_loop(
            url=args.save_mp3,
            out=args.mp3_out,
            voice=args.voice,
            speed=args.tts_speed,
        )
        return

    # Browser-controlled mode — no screen capture, no OCR
    if args.browser_url:
        speak = None
        if not args.no_tts:
            speak = load_tts(voice=args.voice, speed=args.tts_speed)
        browser_reader_loop(
            url=args.browser_url,
            speak=speak,
            chunk_words=args.chunk_words,
            headless=args.browser_headless,
        )
        return

    # Determine capture region
    if args.region:
        try:
            x, y, w, h = map(int, args.region.split(","))
        except ValueError:
            print("Error: --region must be x,y,width,height (e.g. 100,200,800,600)", file=sys.stderr)
            sys.exit(1)
        region = {"left": x, "top": y, "width": w, "height": h}
    elif args.select:
        region = select_region()
    else:
        with mss.mss() as sct:
            monitors = sct.monitors
            if args.monitor >= len(monitors):
                print(
                    f"Error: monitor {args.monitor} does not exist "
                    f"(available: 0-{len(monitors)-1})",
                    file=sys.stderr,
                )
                sys.exit(1)
            mon = monitors[args.monitor]
            region = {"left": mon["left"], "top": mon["top"], "width": mon["width"], "height": mon["height"]}

    # Determine next-page button region
    next_btn_region = None
    if args.next_btn_region:
        try:
            x, y, w, h = map(int, args.next_btn_region.split(","))
        except ValueError:
            print("Error: --next-btn-region must be x,y,width,height", file=sys.stderr)
            sys.exit(1)
        next_btn_region = {"left": x, "top": y, "width": w, "height": h}
    elif args.next_btn:
        next_btn_region = select_region(label="next-page button")

    # Load OCR model unless disabled
    ocr_model = ocr_processor = ocr_device = None
    if not args.no_ocr:
        ocr_model, ocr_processor, ocr_device = load_ocr_model(args.model)

    # Load TTS unless disabled
    speak = None
    if not args.no_tts and not args.no_ocr:
        speak = load_tts(voice=args.voice, speed=args.tts_speed)

    capture_loop(
        interval=args.interval,
        output_dir=args.output_dir,
        region=region,
        prefix=args.prefix,
        ocr_model=ocr_model,
        ocr_processor=ocr_processor,
        ocr_device=ocr_device,
        ocr_prompt=args.prompt,
        max_tokens=args.max_tokens,
        diff_threshold=args.diff_threshold,
        speak=speak,
        next_btn_region=next_btn_region,
        web_scroll=args.web,
        overlap_threshold=args.overlap_threshold,
    )


if __name__ == "__main__":
    main()
