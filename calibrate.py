"""One-off helper to find the gate ROI (region of interest) pixel box.

Usage:
    python3 calibrate.py
        Grabs one real frame from the camera, saves it, prints its
        resolution, and opens it in Preview so you can eyeball where
        the gate sits.

    python3 calibrate.py --roi x,y,w,h
        Grabs a fresh frame, crops exactly that box out of it, and
        opens both the full frame and the crop in Preview so you can
        check the alignment. Repeat with adjusted numbers until the
        crop tightly frames the gate track/motor area, then copy the
        final x,y,w,h into config.json's "roi" field.
"""

import argparse
import os
import subprocess
import sys

from dotenv import load_dotenv
from PIL import Image

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
FULL_FRAME_PATH = os.path.join(STATE_DIR, "calibration_frame.jpg")
CROP_PREVIEW_PATH = os.path.join(STATE_DIR, "calibration_roi_preview.jpg")


def grab_frame(rtsp_url: str, out_path: str, timeout_s: int = 15) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-frames:v", "1",
        "-update", "1",
        "-q:v", "2",
        out_path,
    ]
    try:
        subprocess.run(cmd, timeout=timeout_s, capture_output=True, check=True)
    except subprocess.TimeoutExpired:
        print(f"ERROR: timed out after {timeout_s}s waiting for the camera.", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        stderr_tail = e.stderr.decode(errors="replace")[-800:] if e.stderr else ""
        print("ERROR: ffmpeg failed to grab a frame.", file=sys.stderr)
        print(stderr_tail, file=sys.stderr)
        sys.exit(1)


def parse_roi(raw: str) -> tuple[int, int, int, int]:
    try:
        x, y, w, h = (int(v) for v in raw.split(","))
    except ValueError:
        print("ERROR: --roi must be four comma-separated integers: x,y,w,h", file=sys.stderr)
        sys.exit(1)
    return x, y, w, h


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roi", help="x,y,w,h pixel box to preview-crop")
    args = parser.parse_args()

    load_dotenv()
    rtsp_url = os.environ.get("RTSP_URL")
    if not rtsp_url:
        print("ERROR: RTSP_URL is not set. Create a .env file (see .env.example).", file=sys.stderr)
        sys.exit(1)

    os.makedirs(STATE_DIR, exist_ok=True)

    print("Grabbing a fresh frame from the camera...")
    grab_frame(rtsp_url, FULL_FRAME_PATH)

    with Image.open(FULL_FRAME_PATH) as img:
        width, height = img.size
    print(f"Saved: {FULL_FRAME_PATH}")
    print(f"Frame resolution: {width}x{height}")

    if not args.roi:
        subprocess.run(["open", "-a", "Preview", FULL_FRAME_PATH])
        print(
            "\nOpened the full frame in Preview. Eyeball roughly where the gate "
            "track/pillar sits, estimate a pixel box (x, y, w, h), then rerun:\n"
            f"  python3 calibrate.py --roi x,y,w,h"
        )
        return

    x, y, w, h = parse_roi(args.roi)
    with Image.open(FULL_FRAME_PATH) as img:
        crop = img.crop((x, y, x + w, y + h))
        crop.save(CROP_PREVIEW_PATH)

    print(f"Saved crop preview: {CROP_PREVIEW_PATH}")
    subprocess.run(["open", "-a", "Preview", FULL_FRAME_PATH, CROP_PREVIEW_PATH])
    print(
        "\nOpened the full frame and the crop preview in Preview. If the crop "
        "tightly frames the gate track/motor gap (and excludes the timestamp "
        "overlay, the 'tapo' watermark, and swaying foliage), copy this box "
        f"into config.json's \"roi\" field: {{\"x\": {x}, \"y\": {y}, \"w\": {w}, \"h\": {h}}}\n"
        "Otherwise adjust the numbers and rerun."
    )


if __name__ == "__main__":
    main()
