"""Garage gate open/closed monitor.

Polls an RTSP camera feed at a fixed interval, crops a fixed region of
interest (the gate track/motor area), and compares it against the most
recently confirmed-"closed" reference photo. If the view diverges from
that baseline for several consecutive checks, the gate is considered
open; if it stays open past a configurable threshold, an email alert
is sent (with a cooldown so it doesn't spam repeat reminders).

Run directly in a terminal:
    source venv/bin/activate
    python3 gate_monitor.py
Stop with Ctrl+C.
"""

import json
import logging
import os
import smtplib
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage

import numpy as np
from dotenv import load_dotenv
from PIL import Image

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_DIR = os.path.join(BASE_DIR, "state")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
BASELINE_PATH = os.path.join(STATE_DIR, "baseline.jpg")
LAST_SNAPSHOT_PATH = os.path.join(STATE_DIR, "last_snapshot.jpg")

log = logging.getLogger("gate_monitor")


def setup_logging() -> None:
    os.makedirs(LOGS_DIR, exist_ok=True)
    handler_file = logging.FileHandler(os.path.join(LOGS_DIR, "monitor.log"))
    handler_stream = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler_file.setFormatter(fmt)
    handler_stream.setFormatter(fmt)
    log.setLevel(logging.DEBUG)
    log.addHandler(handler_file)
    log.addHandler(handler_stream)


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    required_keys = [
        "roi", "poll_interval_seconds", "snapshot_timeout_seconds",
        "diff_threshold", "consecutive_required", "open_alert_minutes",
        "reminder_cooldown_hours",
    ]
    missing = [k for k in required_keys if k not in config]
    if missing:
        raise ValueError(f"config.json is missing required keys: {missing}")
    roi = config["roi"]
    if roi["w"] <= 0 or roi["h"] <= 0:
        raise ValueError(
            "config.json's \"roi\" is not set. Run calibrate.py first and "
            "copy the resulting x,y,w,h into config.json."
        )
    return config


def load_env() -> dict:
    load_dotenv()
    required_vars = [
        "RTSP_URL", "SMTP_HOST", "SMTP_PORT", "SMTP_USER",
        "SMTP_PASSWORD", "ALERT_RECIPIENT",
    ]
    env = {}
    missing = []
    for var in required_vars:
        value = os.environ.get(var)
        if not value:
            missing.append(var)
        env[var] = value
    if missing:
        raise ValueError(
            f"Missing required .env variables: {missing}. "
            f"Copy .env.example to .env and fill in real values."
        )
    return env


def default_state() -> dict:
    return {
        "status": "closed",
        "open_since": None,
        "consecutive_diverged_count": 0,
        "consecutive_matched_count": 0,
        "last_alert_sent_at": None,
        "baseline_updated_at": None,
        "last_checked_at": None,
    }


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return default_state()
    with open(STATE_PATH) as f:
        return json.load(f)


def save_state_atomic(state: dict) -> None:
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, STATE_PATH)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def grab_snapshot(rtsp_url: str, out_path: str, timeout_s: int) -> bool:
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
        return True
    except subprocess.TimeoutExpired:
        log.warning("snapshot failed: ffmpeg timed out")
        return False
    except subprocess.CalledProcessError as e:
        log.warning(f"snapshot failed: ffmpeg exited with code {e.returncode}")
        return False


def crop_roi(image_path: str, roi: dict) -> Image.Image:
    with Image.open(image_path) as img:
        box = (roi["x"], roi["y"], roi["x"] + roi["w"], roi["y"] + roi["h"])
        return img.crop(box).convert("RGB")


def compute_diff(current_crop: Image.Image, baseline: Image.Image) -> float:
    cur = np.asarray(current_crop.convert("L"), dtype=np.int16)
    base = np.asarray(baseline.convert("L"), dtype=np.int16)
    if cur.shape != base.shape:
        return 1.0  # treat a resolution mismatch as maximally diverged
    return float(np.abs(cur - base).mean() / 255.0)


def send_email(subject: str, body: str, env: dict) -> None:
    msg = EmailMessage()
    msg["From"] = env["SMTP_USER"]
    msg["To"] = env["ALERT_RECIPIENT"]
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL(env["SMTP_HOST"], int(env["SMTP_PORT"])) as server:
        server.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
        server.send_message(msg)
    log.info(f"Sent email: {subject}")


def tick(config: dict, env: dict, state: dict) -> dict:
    log.debug("Polling camera for a new snapshot...")
    ok = grab_snapshot(env["RTSP_URL"], LAST_SNAPSHOT_PATH, config["snapshot_timeout_seconds"])
    if not ok:
        log.debug("Snapshot skipped this cycle (grab failed) — not counted as a divergence.")
        return state  # skip this cycle entirely; never counts as a divergence

    log.debug(f"Snapshot saved to {LAST_SNAPSHOT_PATH}; cropping ROI {config['roi']}")
    crop = crop_roi(LAST_SNAPSHOT_PATH, config["roi"])
    now = now_iso()

    baseline = Image.open(BASELINE_PATH) if os.path.exists(BASELINE_PATH) else None
    roi_changed = baseline is not None and baseline.size != crop.size

    if baseline is None or roi_changed:
        crop.save(BASELINE_PATH)
        if roi_changed:
            log.warning(
                f"BOOTSTRAP: ROI size changed ({baseline.size} -> {crop.size}, likely a "
                "config.json edit) — resetting and saving current frame as the new "
                "'closed' reference. Make sure the gate is actually closed right now!"
            )
        else:
            log.warning(
                "BOOTSTRAP: no baseline found — saving current frame as the "
                "'closed' reference. Make sure the gate is actually closed right now!"
            )
        state = default_state()
        state["baseline_updated_at"] = now
        state["last_checked_at"] = now
        save_state_atomic(state)
        return state

    score = compute_diff(crop, baseline)
    is_match = score <= config["diff_threshold"]
    log.debug(
        f"Diff score {score:.4f} vs threshold {config['diff_threshold']} -> "
        f"{'MATCH (looks closed)' if is_match else 'DIVERGED (looks changed)'}"
    )

    if is_match:
        state["consecutive_matched_count"] += 1
        state["consecutive_diverged_count"] = 0
    else:
        state["consecutive_diverged_count"] += 1
        state["consecutive_matched_count"] = 0

    required = config["consecutive_required"]
    log.debug(
        f"status={state['status']} consecutive_matched={state['consecutive_matched_count']} "
        f"consecutive_diverged={state['consecutive_diverged_count']} (required={required})"
    )

    if state["status"] == "closed":
        if state["consecutive_diverged_count"] >= required:
            state["status"] = "open"
            state["open_since"] = now
            log.info(f"Gate OPENED (diff score {score:.3f})")
        elif is_match:
            crop.save(BASELINE_PATH)
            state["baseline_updated_at"] = now
            log.debug("Baseline refreshed (still matching closed reference).")

    elif state["status"] == "open":
        if state["consecutive_matched_count"] >= required:
            state["status"] = "closed"
            state["open_since"] = None
            state["last_alert_sent_at"] = None
            crop.save(BASELINE_PATH)
            state["baseline_updated_at"] = now
            log.info("Gate CLOSED")
        else:
            open_since = parse_iso(state["open_since"])
            open_minutes = (datetime.now(timezone.utc) - open_since).total_seconds() / 60.0

            due_for_first_alert = (
                state["last_alert_sent_at"] is None
                and open_minutes >= config["open_alert_minutes"]
            )
            due_for_reminder = False
            if state["last_alert_sent_at"] is not None:
                last_alert = parse_iso(state["last_alert_sent_at"])
                hours_since_alert = (datetime.now(timezone.utc) - last_alert).total_seconds() / 3600.0
                due_for_reminder = hours_since_alert >= config["reminder_cooldown_hours"]
                log.debug(
                    f"Already alerted {hours_since_alert:.2f}h ago "
                    f"(reminder cooldown {config['reminder_cooldown_hours']}h) -> "
                    f"due_for_reminder={due_for_reminder}"
                )
            else:
                log.debug(
                    f"Open for {open_minutes:.1f} min (threshold {config['open_alert_minutes']}) -> "
                    f"due_for_first_alert={due_for_first_alert}"
                )

            if due_for_first_alert or due_for_reminder:
                log.debug("Alert conditions met — attempting to send email...")
                try:
                    send_email(
                        subject="Garage gate is open",
                        body=f"The garage gate has been open for about {open_minutes:.0f} minutes "
                             f"(as of {now}).",
                        env=env,
                    )
                    state["last_alert_sent_at"] = now
                except Exception as e:
                    log.error(f"Failed to send alert email: {e}")

    state["last_checked_at"] = now
    save_state_atomic(state)
    log.debug(f"State saved: {state}")
    return state


def main() -> None:
    setup_logging()
    os.makedirs(STATE_DIR, exist_ok=True)

    try:
        config = load_config()
        env = load_env()
    except ValueError as e:
        log.error(str(e))
        sys.exit(1)

    state = load_state()
    log.info(f"Starting gate monitor. Current status: {state['status']}")

    try:
        while True:
            state = tick(config, env, state)
            time.sleep(config["poll_interval_seconds"])
    except KeyboardInterrupt:
        log.info("Stopped by user (Ctrl+C).")


if __name__ == "__main__":
    main()
