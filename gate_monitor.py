"""Garage gate open/closed monitor (two-camera).

Polls one RTSP camera feed per configured camera at a fixed interval, crops a
fixed region of interest (the gate track/motor area) from each, and compares
each crop against that camera's most recently confirmed-"closed" reference
photo. A camera "votes open" when its view diverges from its closed baseline
for several consecutive checks; the gate is only considered open when *every*
configured camera votes open (both must agree). If it stays open past a
configurable threshold, an email alert is sent (with a cooldown so it doesn't
spam repeat reminders).

Run directly in a terminal:
    source venv/bin/activate
    python3 gate_monitor.py
Stop with Ctrl+C.
"""

import copy
import json
import logging
import os
import re
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

log = logging.getLogger("gate_monitor")


class ColorFormatter(logging.Formatter):
    """Highlights diff scores and open/closed status for terminal readability.

    Only used on the console handler — logs/monitor.log stays plain text.
    """

    _RED = "\033[31m"
    _GREEN = "\033[32m"
    _YELLOW = "\033[33m"
    _CYAN = "\033[36m"
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        msg = re.sub(r"Diff score [\d.]+", lambda m: f"{self._CYAN}{m.group(0)}{self._RESET}", msg)
        msg = re.sub(r"\bMATCH\b", f"{self._GREEN}MATCH{self._RESET}", msg)
        msg = re.sub(r"\bDIVERGED\b", f"{self._YELLOW}DIVERGED{self._RESET}", msg)
        msg = re.sub(r"status=open\b", f"status={self._RED}open{self._RESET}", msg)
        msg = re.sub(r"status=closed\b", f"status={self._GREEN}closed{self._RESET}", msg)
        msg = re.sub(r"Gate OPENED", f"{self._RED}Gate OPENED{self._RESET}", msg)
        msg = re.sub(r"Gate CLOSED", f"{self._GREEN}Gate CLOSED{self._RESET}", msg)
        return msg


def setup_logging() -> None:
    os.makedirs(LOGS_DIR, exist_ok=True)
    handler_file = logging.FileHandler(os.path.join(LOGS_DIR, "monitor.log"))
    handler_stream = logging.StreamHandler(sys.stdout)
    fmt_str = "%(asctime)s %(levelname)s %(message)s"
    handler_file.setFormatter(logging.Formatter(fmt_str))
    handler_stream.setFormatter(ColorFormatter(fmt_str))
    log.setLevel(logging.DEBUG)
    log.addHandler(handler_file)
    log.addHandler(handler_stream)


def sanitize_camera_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def validate_config(config: dict) -> None:
    missing = [
        k for k in ("poll_interval_seconds", "snapshot_timeout_seconds",
                    "open_alert_minutes", "reminder_cooldown_hours")
        if k not in config
    ]
    if missing:
        raise ValueError(f"config.json is missing required keys: {missing}")
    if not isinstance(config["cameras"], list) or not config["cameras"]:
        raise ValueError('config.json "cameras" must be a non-empty list.')
    if config["poll_interval_seconds"] <= 0:
        raise ValueError('config.json "poll_interval_seconds" must be > 0.')
    if config["snapshot_timeout_seconds"] <= 0:
        raise ValueError('config.json "snapshot_timeout_seconds" must be > 0.')
    if config["open_alert_minutes"] < 0:
        raise ValueError('config.json "open_alert_minutes" must be >= 0.')
    if config["reminder_cooldown_hours"] <= 0:
        raise ValueError('config.json "reminder_cooldown_hours" must be > 0.')

    seen_names = set()
    for cam in config["cameras"]:
        name = cam.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError('Each camera needs a "name".')
        if sanitize_camera_name(name) != name:
            raise ValueError(
                f'Camera name {name!r} is not filesystem-safe; use only '
                "A-Z a-z 0-9 _ . -"
            )
        if name in seen_names:
            raise ValueError(f"Duplicate camera name: {name}")
        seen_names.add(name)
        if not cam.get("rtsp_url_env"):
            raise ValueError(f'Camera "{name}" needs an "rtsp_url_env".')
        roi = cam.get("roi")
        if not isinstance(roi, dict):
            raise ValueError(f'Camera "{name}" needs a "roi" {x, y, w, h} box.')
        if roi["w"] <= 0 or roi["h"] <= 0:
            raise ValueError(
                f'Camera "{name}" ROI is not set or invalid. Run calibrate.py '
                "first and copy the resulting x,y,w,h into config.json."
            )
        if not isinstance(cam.get("diff_threshold"), (int, float)):
            raise ValueError(f'Camera "{name}" needs a numeric "diff_threshold".')
        if cam["diff_threshold"] < 0 or cam["diff_threshold"] > 1:
            raise ValueError(f'Camera "{name}" "diff_threshold" must be in [0, 1].')
        if not isinstance(cam.get("consecutive_required"), int) or cam["consecutive_required"] < 1:
            raise ValueError(f'Camera "{name}" needs "consecutive_required" >= 1.')


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    validate_config(config)
    return config


def validate_env(config: dict, env: dict) -> list:
    missing = []
    secrets = [
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "ALERT_RECIPIENT",
    ]
    for var in secrets:
        if not env.get(var):
            missing.append(var)
    for cam in config["cameras"]:
        var = cam["rtsp_url_env"]
        if not env.get(var):
            missing.append(var)
    return missing


def load_env(config: dict) -> dict:
    load_dotenv()
    env = {var: os.environ.get(var) for var in (
        "RTSP_URL", "RTSP_URL_SECOND", "SMTP_HOST", "SMTP_PORT", "SMTP_USER",
        "SMTP_PASSWORD", "ALERT_RECIPIENT",
    )}
    missing = validate_env(config, env)
    if missing:
        raise ValueError(
            f"Missing required .env variables: {missing}. "
            f"Copy .env.example to .env and fill in real values."
        )
    return env


def default_state(config: dict) -> dict:
    cameras = {}
    for cam in config["cameras"]:
        cameras[cam["name"]] = {
            "consecutive_diverged_count": 0,
            "consecutive_matched_count": 0,
            "consecutive_open_diverged_count": 0,
            "baseline_updated_at": None,
        }
    return {
        "status": "closed",
        "open_since": None,
        "last_alert_sent_at": None,
        "last_checked_at": None,
        "cameras": cameras,
    }


def clear_open_baseline(name: str) -> None:
    path = camera_open_baseline_path(name)
    if os.path.exists(path):
        os.remove(path)


def save_state_atomic(state: dict) -> None:
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, STATE_PATH)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def camera_dir(name: str) -> str:
    return os.path.join(STATE_DIR, sanitize_camera_name(name))


def camera_baseline_path(name: str) -> str:
    return os.path.join(camera_dir(name), "baseline.jpg")


def camera_open_baseline_path(name: str) -> str:
    return os.path.join(camera_dir(name), "open_baseline.jpg")


def camera_snapshot_path(name: str) -> str:
    return os.path.join(camera_dir(name), "last_snapshot.jpg")


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


def apply_camera_observation(cam_state: dict, is_match: bool) -> dict:
    out = copy.deepcopy(cam_state)
    if is_match:
        out["consecutive_matched_count"] += 1
        out["consecutive_diverged_count"] = 0
    else:
        out["consecutive_diverged_count"] += 1
        out["consecutive_matched_count"] = 0
    return out


def camera_open_vote(cam_state: dict, cam_cfg: dict) -> bool:
    return cam_state["consecutive_diverged_count"] >= cam_cfg["consecutive_required"]


def camera_closed_vote(cam_state: dict, cam_cfg: dict) -> bool:
    return cam_state["consecutive_matched_count"] >= cam_cfg["consecutive_required"]


def camera_open_change_vote(cam_state: dict, cam_cfg: dict) -> bool:
    return cam_state["consecutive_open_diverged_count"] >= cam_cfg["consecutive_required"]


def update_gate_state(gate: dict, config: dict, results: dict, now_iso: str) -> tuple:
    """Combine per-camera observations into the next gate state.

    `results` maps a camera name to {"is_match": bool, "open_change": bool|None}.
    A camera may be entirely absent when it produced no usable snapshot this
    cycle — it keeps its stale counts and can never vote toward a transition.
    Returns (new_state, events) where events are:
      "OPENED", "CLOSED",
      "REFRESH_CLOSED_BASELINE:<name>",
      "ESTABLISH_OPEN_BASELINE:<name>", "REFRESH_OPEN_BASELINE:<name>",
      "ALERT_DUE".
    """
    new_gate = copy.deepcopy(gate)
    cam_cfg = {c["name"]: c for c in config["cameras"]}
    events = []

    for name, r in results.items():
        if name in new_gate["cameras"]:
            new_gate["cameras"][name] = apply_camera_observation(
                new_gate["cameras"][name], r["is_match"]
            )

    present = set(results)

    def all_cameras_vote(pred) -> bool:
        return all(
            name in present and pred(new_gate["cameras"][name], cam_cfg[name])
            for name in cam_cfg
        )

    now_dt = parse_iso(now_iso)

    if new_gate["status"] == "closed":
        if all_cameras_vote(camera_open_vote):
            new_gate["status"] = "open"
            new_gate["open_since"] = now_iso
            for name in cam_cfg:
                new_gate["cameras"][name]["consecutive_open_diverged_count"] = 0
            events.append("OPENED")
        for name, r in results.items():
            if r["is_match"]:
                new_gate["cameras"][name]["baseline_updated_at"] = now_iso
                events.append(f"REFRESH_CLOSED_BASELINE:{name}")
    else:  # status == "open"
        open_minutes = (now_dt - parse_iso(new_gate["open_since"])).total_seconds() / 60.0

        if all_cameras_vote(camera_closed_vote):
            new_gate["status"] = "closed"
            new_gate["open_since"] = None
            new_gate["last_alert_sent_at"] = None
            for name, r in results.items():
                new_gate["cameras"][name]["consecutive_open_diverged_count"] = 0
                if r["is_match"]:
                    new_gate["cameras"][name]["baseline_updated_at"] = now_iso
                    events.append(f"REFRESH_CLOSED_BASELINE:{name}")
            events.append("CLOSED")
        else:
            # Once past the alert threshold, the original pre-open baseline can
            # be stale (lighting may have drifted during a long open episode)
            # and might never match again. Track a second, self-calibrating
            # "open" baseline per camera in parallel — if every camera's view
            # diverges from *its* open baseline, the gate has almost certainly
            # closed, so treat it as closed and start a fresh slate.
            if open_minutes >= config["open_alert_minutes"]:
                for name, r in results.items():
                    if r["open_change"] is None:
                        events.append(f"ESTABLISH_OPEN_BASELINE:{name}")
                    elif r["open_change"] is False:
                        new_gate["cameras"][name]["consecutive_open_diverged_count"] = 0
                        events.append(f"REFRESH_OPEN_BASELINE:{name}")
                    else:
                        new_gate["cameras"][name]["consecutive_open_diverged_count"] += 1

                if all_cameras_vote(camera_open_change_vote):
                    log.info(
                        "Every camera view diverged from its open-state baseline — "
                        "assuming the gate has closed and starting a fresh slate."
                    )
                    new_gate["status"] = "closed"
                    new_gate["open_since"] = None
                    new_gate["last_alert_sent_at"] = None
                    for name in cam_cfg:
                        cam = new_gate["cameras"][name]
                        cam["consecutive_matched_count"] = 0
                        cam["consecutive_diverged_count"] = 0
                        cam["consecutive_open_diverged_count"] = 0
                        cam["baseline_updated_at"] = now_iso
                        events.append(f"REFRESH_CLOSED_BASELINE:{name}")
                    events.append("CLOSED")

            if new_gate["status"] == "open":
                due_for_first_alert = (
                    new_gate["last_alert_sent_at"] is None
                    and open_minutes >= config["open_alert_minutes"]
                )
                due_for_reminder = False
                if new_gate["last_alert_sent_at"] is not None:
                    hours_since_alert = (
                        now_dt - parse_iso(new_gate["last_alert_sent_at"])
                    ).total_seconds() / 3600.0
                    due_for_reminder = hours_since_alert >= config["reminder_cooldown_hours"]
                if due_for_first_alert or due_for_reminder:
                    events.append("ALERT_DUE")

    new_gate["last_checked_at"] = now_iso
    return new_gate, events


def observe_cameras(config: dict, env: dict, state: dict) -> dict:
    """Grab and eval each camera's snapshot; returns {name: observation|None}.

    Observation keys: is_match, open_change, diff_score, crop, cam_cfg, name.
    None means no usable snapshot this cycle (grab failed or bootstrap).
    """
    observations = {}
    for cam in config["cameras"]:
        name = cam["name"]
        cam_state = state["cameras"][name]
        os.makedirs(camera_dir(name), exist_ok=True)
        snap_path = camera_snapshot_path(name)

        ok = grab_snapshot(env[cam["rtsp_url_env"]], snap_path, config["snapshot_timeout_seconds"])
        if not ok:
            log.debug(
                f"Snapshot skipped this cycle for camera '{name}' "
                "(grab failed) — not counted as a divergence."
            )
            continue

        crop = crop_roi(snap_path, cam["roi"])
        baseline_path = camera_baseline_path(name)
        baseline = Image.open(baseline_path) if os.path.exists(baseline_path) else None
        roi_changed = baseline is not None and baseline.size != crop.size

        if baseline is None or roi_changed:
            crop.save(baseline_path)
            cam_state["baseline_updated_at"] = now_iso()
            log.warning(
                f"BOOTSTRAP for camera '{name}': no usable baseline — saving "
                "current frame as the 'closed' reference. Make sure the gate is "
                "actually closed right now!"
            )
            continue

        is_match = compute_diff(crop, baseline) <= cam["diff_threshold"]
        open_change = None
        open_baseline_path = camera_open_baseline_path(name)
        if os.path.exists(open_baseline_path):
            open_score = compute_diff(crop, Image.open(open_baseline_path))
            open_change = not (open_score <= cam["diff_threshold"])
        log.debug(
            f"Camera '{name}': diff score {compute_diff(crop, baseline):.4f} "
            f"vs threshold {cam['diff_threshold']} -> "
            f"{'MATCH (looks closed)' if is_match else 'DIVERGED (looks changed)'}"
        )
        observations[name] = {
            "is_match": is_match,
            "open_change": open_change,
            "diff_score": compute_diff(crop, baseline),
            "crop": crop,
            "cam_cfg": cam,
        }
    return observations


def apply_events(events: list, observations: dict, config: dict, env: dict,
                 gate: dict, now: str) -> None:
    for ev in events:
        if ev == "OPENED":
            log.info("Gate OPENED (every camera agrees the view diverged)")
            for cam in config["cameras"]:
                clear_open_baseline(cam["name"])
        elif ev == "CLOSED":
            log.info("Gate CLOSED (matched closed baseline / views changed back)")
            for cam in config["cameras"]:
                clear_open_baseline(cam["name"])
        elif ev.startswith("REFRESH_CLOSED_BASELINE:"):
            name = ev.split(":", 1)[1]
            if name in observations:
                observations[name]["crop"].save(camera_baseline_path(name))
                log.debug(f"Baseline refreshed for camera '{name}' (still matching closed reference).")
        elif ev.startswith("ESTABLISH_OPEN_BASELINE:"):
            name = ev.split(":", 1)[1]
            if name in observations:
                observations[name]["crop"].save(camera_open_baseline_path(name))
                log.debug(f"Open-state baseline established for camera '{name}'.")
        elif ev.startswith("REFRESH_OPEN_BASELINE:"):
            name = ev.split(":", 1)[1]
            if name in observations:
                observations[name]["crop"].save(camera_open_baseline_path(name))
                log.debug(f"Open-state baseline refreshed for camera '{name}'.")
        elif ev == "ALERT_DUE":
            open_minutes = (parse_iso(now) - parse_iso(gate["open_since"])).total_seconds() / 60.0
            camera_lines = "\n".join(
                f"  {name}: diff score {obs['diff_score']:.3f}"
                for name, obs in observations.items()
            )
            body = (
                f"The garage gate has been open for about {open_minutes:.0f} minutes "
                f"(as of {now}). Both cameras agree:\n{camera_lines}"
            )
            try:
                send_email(subject="Garage gate is open", body=body, env=env)
                gate["last_alert_sent_at"] = now
            except Exception as e:
                log.error(f"Failed to send alert email: {e}")


def tick(config: dict, env: dict, state: dict) -> dict:
    observations = observe_cameras(config, env, state)
    if not observations:
        log.debug("No camera produced a usable snapshot this cycle — skipping.")
        return state

    now = now_iso()
    results = {
        name: {"is_match": obs["is_match"], "open_change": obs["open_change"]}
        for name, obs in observations.items()
    }
    gate, events = update_gate_state(state, config, results, now)
    apply_events(events, observations, config, env, gate, now)
    save_state_atomic(gate)
    log.debug(f"State saved: status={gate['status']}")
    return gate


def main() -> None:
    setup_logging()
    os.makedirs(STATE_DIR, exist_ok=True)

    try:
        config = load_config()
        env = load_env(config)
    except ValueError as e:
        log.error(str(e))
        sys.exit(1)

    # Every start assumes the gate is closed right now, regardless of any
    # previous run's state or baseline — you only start this manually, so
    # that's the reasonable assumption. Removing each camera's baseline forces
    # observe_cameras()'s bootstrap path to fire immediately on the first poll,
    # capturing fresh "closed" references straight away.
    state = default_state(config)
    for cam in config["cameras"]:
        os.makedirs(camera_dir(cam["name"]), exist_ok=True)
        baseline = camera_baseline_path(cam["name"])
        open_baseline = camera_open_baseline_path(cam["name"])
        if os.path.exists(baseline):
            os.remove(baseline)
        if os.path.exists(open_baseline):
            os.remove(open_baseline)
    log.info(
        "Starting gate monitor with %d camera(s): %s. Assuming gate is closed — "
        "will capture a fresh baseline on the first poll.",
        len(config["cameras"]),
        [c["name"] for c in config["cameras"]],
    )

    try:
        while True:
            state = tick(config, env, state)
            time.sleep(config["poll_interval_seconds"])
    except KeyboardInterrupt:
        log.info("Stopped by user (Ctrl+C).")


if __name__ == "__main__":
    main()