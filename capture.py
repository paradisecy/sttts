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


def load_tts(voice: str, speed: float):
    """Load Kokoro TTS pipeline on CPU; return callable speak(text)."""
    import torch
    from kokoro import KPipeline

    print(f"Loading TTS (voice={voice}, speed={speed}, device=cpu)…")
    with torch.device("cpu"):
        pipeline = KPipeline(lang_code="a", device="cpu")

    def speak(text: str) -> None:
        if not text.strip():
            return
        others = _get_sink_inputs()
        _duck(others)
        try:
            for _, _, audio in pipeline(text, voice=voice, speed=speed):
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
    # True after TTS finishes speaking — so next idle frame triggers a page turn
    waiting_for_next_page = False

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
                        # Reset so we OCR the new page fresh
                        prev_pixels = None
                        waiting_for_next_page = False
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
                        speak(speakable)
                        # TTS just finished — next idle frame should turn the page
                        if next_btn_region:
                            waiting_for_next_page = True

            deadline = time.monotonic() + interval
            while not stop and time.monotonic() < deadline:
                time.sleep(0.05)

    print(f"\nStopped. {count} snapshot(s) taken.")


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
    )


if __name__ == "__main__":
    main()
