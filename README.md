# Garage Gate Monitor

A small personal prototype that watches one or two LAN camera feeds of a
garage gate and emails an alert if the gate has been open continuously for
longer than a configurable threshold (default: 10 minutes).

No AI/ML model, no cloud dependency — just a periodic snapshot compared
against a self-updating "last known closed" reference photo.

## How it works (short version)

Every `poll_interval_seconds`, the app:
1. Grabs one JPEG snapshot from each configured camera's RTSP stream via
   `ffmpeg`.
2. Crops each down to that camera's region of interest (ROI) — the small part
   of the frame where the gate visibly moves.
3. Compares each crop to that camera's most recently confirmed-"closed"
   reference photo (mean pixel difference, grayscale).
4. A camera "votes open" when its crop looks different for that camera's
   `consecutive_required` checks in a row. The gate is considered "open" only
   when **every** camera votes open (both must agree), and a stopwatch starts.
5. If it's still open after `open_alert_minutes`, an email is sent. If it
   stays open, reminder emails go out every `reminder_cooldown_hours` (not on
   every check).
6. As soon as every camera matches "closed" again for `consecutive_required`
   checks, it flips back to closed and the cycle resets.

The "closed" reference photo keeps refreshing itself while the gate is
closed (never while it's open) — that's what lets it adapt to day/night
lighting changes automatically, without maintaining separate reference
photos.

## Prerequisites

- macOS (developed/run on a Mac mini you leave powered on)
- [Homebrew](https://brew.sh)
- `ffmpeg` / `ffprobe`: `brew install ffmpeg`
- Python 3 (developed against 3.12; any recent Python 3 should work)
- A camera reachable over RTSP on your LAN (tested against a TP-Link Tapo
  camera's `stream1`)
- A Gmail account with an [app password](https://myaccount.google.com/apppasswords)
  (requires 2FA enabled) to send alert emails — or any other SMTP account

## First-time setup

```sh
cd /Users/ezzat/projects/garage-gate-monitor

# 1. Create the virtual environment and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Create your local secrets file
cp .env.example .env
chmod 600 .env
```

Now edit `.env` and fill in real values:

```sh
RTSP_URL=rtsp://USER:PASSWORD@CAMERA_IP:554/stream1
RTSP_URL_SECOND=rtsp://USER:PASSWORD@CAMERA_IP:554/stream1
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USER=youraccount@gmail.com
SMTP_PASSWORD=gmail-app-password-here
ALERT_RECIPIENT=you@example.com
```

Each camera in `config.json` names the `.env` variable holding its RTSP URL —
add a second line like `RTSP_URL_SECOND=...` pointing at the second camera's
stream.

`.env` is gitignored and permission-restricted (`chmod 600`) since it holds
real credentials — never commit it, and never put credentials anywhere else
(`config.json`, code, logs).

### Calibrating the ROI

The ROI is a fixed pixel box (`x, y, w, h`) cropped out of every frame — it is
**not** auto-detected, and it does not update itself if the camera is moved or
remounted. You determine it once with the calibration helper (one camera at a
time — `calibrate.py` reads `RTSP_URL`, so point that at the camera you're
calibrating, or run it against each camera's URL in turn):

```sh
python3 calibrate.py
```

This grabs one real frame from the camera, prints its resolution, and opens
it in Preview.app. Eyeball roughly where the gate moves, then iterate:

```sh
python3 calibrate.py --roi x,y,w,h
```

This re-grabs a fresh frame, crops exactly that box, and opens both the full
frame and the crop preview in Preview so you can check alignment. Repeat with
adjusted numbers until the crop tightly frames the part of the gate that
visibly moves when it opens, then copy the final `x,y,w,h` into that camera's
`"roi"` field in `config.json`.

**Important — pick a spot away from the hinge.** If the gate is a hinged/swing
gate (not a sliding gate), a region right next to the hinge/pillar barely
moves even when the gate is fully open, since points close to a pivot travel
very little for a given swing angle. Pick the ROI closer to the gate panel's
outer/leading edge (farther from the hinge), where the same swing produces a
much bigger, more reliable pixel change.

If you ever change a camera's ROI `x/y/w/h` in `config.json`, that camera's
existing `state/<camera>/baseline.jpg` (saved at the old crop size) becomes
stale. The app detects the size mismatch automatically and re-bootstraps a
fresh baseline for that camera — but make sure the gate is actually closed at
that moment, since whatever frame it sees during that reset becomes the new
"closed" reference.

## Running it

```sh
cd /Users/ezzat/projects/garage-gate-monitor
source venv/bin/activate
python3 gate_monitor.py
```

Stop with Ctrl+C. There's no launchd/background service by design — you
start and stop it manually. Leave the terminal tab open on the always-on Mac
mini while you want monitoring active. Logs go to both the terminal and
`logs/monitor.log` (DEBUG level — every poll, every diff score, every
decision is logged).

## Configuration reference (`config.json`)

| Key | Meaning |
|---|---|
| `cameras` | Array of camera entries, one per feed (see below) |
| `poll_interval_seconds` | How often to grab a snapshot and check |
| `snapshot_timeout_seconds` | How long to wait for `ffmpeg` before giving up on one snapshot (a timeout skips that camera that cycle, never counts as "open") |
| `open_alert_minutes` | How long the gate must be continuously open (both cameras agreed) before the first email fires |
| `reminder_cooldown_hours` | How often to re-send a reminder email while still open |

Each entry in `cameras`:

| Key | Meaning |
|---|---|
| `name` | Short label (also used for the `state/<name>/` folder). Filesystem-safe: A-Z a-z 0-9 `_ . -` |
| `rtsp_url_env` | `.env` variable name holding this camera's RTSP URL (e.g. `RTSP_URL`, `RTSP_URL_SECOND`) |
| `roi` | `{x, y, w, h}` pixel box cropped from this camera's frame (see Calibrating above) |
| `diff_threshold` | Grayscale mean-absolute-difference (0–1) above which this camera's frame is considered "changed" |
| `consecutive_required` | How many consecutive changed/matched reads this camera needs before flipping state (debounces single noisy frames) |

**Current values in `config.json` are fast test values** (`open_alert_minutes: 0`,
so the first alert fires as soon as both cameras agree the gate is open, and
`poll_interval_seconds: 20`). Before leaving this running unattended for real,
consider restoring more relaxed production values, e.g. `open_alert_minutes: 10`.

## Known limitations

- **Obstruction false positives.** The algorithm can't distinguish "gate is
  open" from "something is blocking a camera's view of the ROI" (a parked
  car, a person standing there). A brief obstruction is filtered out by the
  debounce logic. With two cameras, a sustained obstruction of **one** camera
  no longer triggers an alert on its own — both must agree — but blocking
  both cameras (or being a single-camera setup) can still cause a false
  alert. It self-corrects once the views clear.
- **Auto-closing gates and short poll windows.** If your gate auto-closes
  quickly (tens of seconds), make sure `poll_interval_seconds` is short enough
  relative to that window, or the app may simply never catch it mid-open
  during a quick test. This doesn't matter for the real use case (leaving the
  gate open for many minutes), only for manual testing.
- **No physical sensor.** This is pure vision-based diffing against a fixed
  camera — no magnetic reed switch or similar. It depends on the camera
  staying in a fixed position; if it's removed/moved/remounted, re-run
  `calibrate.py`.
- **Diff scores near the threshold are noisy.** JPEG compression jitter and
  minor lighting flicker alone can move the diff score by several hundredths.
  If real "open" scores land close to `diff_threshold`, tighten the ROI to a
  higher-signal area or lower the threshold, but expect to re-tune after
  observing real open/closed scores side by side.

## Project layout

```
gate_monitor.py       main loop (run this)
calibrate.py           one-off ROI calibration helper (one camera at a time)
config.json            non-secret tunables (see table above)
.env                    credentials (gitignored, chmod 600) — you create this
.env.example            placeholder template, safe to commit
requirements.txt        Pillow, numpy, python-dotenv
test_gate_monitor.py    unittest suite for the two-camera decision logic
state/
  state.json            persisted gate state machine (status, timers, counters)
  <camera>/baseline.jpg          that camera's "closed" reference crop (self-updating)
  <camera>/open_baseline.jpg     that camera's self-calibrating "open" reference crop
  <camera>/last_snapshot.jpg     that camera's most recent full-frame grab (debugging aid)
logs/
  monitor.log           full DEBUG-level log of every poll/decision
```
