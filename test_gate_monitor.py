"""Tests for the two-camera gate monitor logic.

Run from the repo root:
    source venv/bin/activate
    python -m unittest test_gate_monitor -v
"""

import unittest

import numpy as np
from PIL import Image

import gate_monitor as gm


def make_config(num_cameras=2, open_alert_minutes=0, reminder_cooldown_hours=1):
    cameras = [
        {
            "name": "main",
            "rtsp_url_env": "RTSP_URL",
            "roi": {"x": 0, "y": 0, "w": 100, "h": 100},
            "diff_threshold": 0.5,
            "consecutive_required": 3,
        },
    ]
    if num_cameras > 1:
        cameras.append(
            {
                "name": "second",
                "rtsp_url_env": "RTSP_URL_SECOND",
                "roi": {"x": 0, "y": 0, "w": 100, "h": 100},
                "diff_threshold": 0.5,
                "consecutive_required": 1,
            }
        )
    return {
        "cameras": cameras,
        "poll_interval_seconds": 20,
        "snapshot_timeout_seconds": 10,
        "open_alert_minutes": open_alert_minutes,
        "reminder_cooldown_hours": reminder_cooldown_hours,
    }


def cam_stub(diverged=0, matched=0, open_diverged=0):
    return {
        "consecutive_diverged_count": diverged,
        "consecutive_matched_count": matched,
        "consecutive_open_diverged_count": open_diverged,
        "baseline_updated_at": None,
    }


def fresh_state(config, diverged_main=0, diverged_second=0):
    return {
        "status": "closed",
        "open_since": None,
        "last_alert_sent_at": None,
        "last_checked_at": None,
        "cameras": {
            "main": cam_stub(diverged=diverged_main),
            "second": cam_stub(diverged=diverged_second),
        },
    }


def open_state(config, divergence_main=1, divergence_second=1,
               open_diverged_main=0, open_diverged_second=0,
               open_since="2026-09-21T10:00:00+00:00"):
    st = fresh_state(config)
    st.update(
        {
            "status": "open",
            "open_since": open_since,
            "cameras": {
                "main": cam_stub(diverged=divergence_main,
                                 open_diverged=open_diverged_main),
                "second": cam_stub(diverged=divergence_second,
                                   open_diverged=open_diverged_second),
            },
        }
    )
    return st


def observe(gate, config, main_match, second_match,
            main_open_change=None, second_open_change=None):
    return gm.update_gate_state(
        gate,
        config,
        {
            "main": {"is_match": main_match, "open_change": main_open_change},
            "second": {"is_match": second_match, "open_change": second_open_change},
        },
        "2026-09-21T10:00:00+00:00",
    )


class TestCounts(unittest.TestCase):
    def test_match_increments_matched_count_and_resets_diverged(self):
        cam = cam_stub(diverged=2)
        out = gm.apply_camera_observation(cam, is_match=True)
        self.assertEqual(out["consecutive_matched_count"], 1)
        self.assertEqual(out["consecutive_diverged_count"], 0)

    def test_diverge_increments_diverged_count_and_resets_matched(self):
        cam = cam_stub(matched=2)
        out = gm.apply_camera_observation(cam, is_match=False)
        self.assertEqual(out["consecutive_diverged_count"], 1)
        self.assertEqual(out["consecutive_matched_count"], 0)

    def test_observation_does_not_mutate_input(self):
        cam = cam_stub()
        gm.apply_camera_observation(cam, is_match=False)
        self.assertEqual(cam["consecutive_diverged_count"], 0)


class TestVotes(unittest.TestCase):
    def test_open_vote_requires_own_camera_required_count(self):
        config = make_config()
        cfg = {c["name"]: c for c in config["cameras"]}
        self.assertTrue(gm.camera_open_vote(cam_stub(diverged=3), cfg["main"]))
        self.assertFalse(gm.camera_open_vote(cam_stub(diverged=2), cfg["main"]))
        self.assertTrue(gm.camera_open_vote(cam_stub(diverged=1), cfg["second"]))
        self.assertFalse(gm.camera_open_vote(cam_stub(diverged=0), cfg["second"]))

    def test_closed_vote_requires_own_camera_required_count(self):
        config = make_config()
        cfg = {c["name"]: c for c in config["cameras"]}
        self.assertTrue(gm.camera_closed_vote(cam_stub(matched=3), cfg["main"]))
        self.assertFalse(gm.camera_closed_vote(cam_stub(matched=2), cfg["main"]))


class TestGateStaysClosedUntilAllAgree(unittest.TestCase):
    def test_gate_stays_closed_when_only_second_camera_diverges(self):
        config = make_config()
        gate, events = observe(fresh_state(config), config,
                               main_match=True, second_match=False)
        self.assertEqual(gate["status"], "closed")
        self.assertNotIn("OPENED", events)

    def test_gate_stays_closed_when_only_main_camera_diverges(self):
        config = make_config()
        # second camera required=1, so a single diverged frame is its open vote;
        # main camera required=3 only has one diverged frame -> not yet.
        gate, events = observe(fresh_state(config), config,
                               main_match=False, second_match=False)
        self.assertEqual(gate["status"], "closed")
        self.assertNotIn("OPENED", events)

    def test_gate_opens_when_every_camera_has_voted_open(self):
        config = make_config()
        # main needs 3 diverged frames, second needs 1.
        gate = fresh_state(config, diverged_main=2, diverged_second=1)
        new_gate, events = observe(gate, config,
                                   main_match=False, second_match=False)
        self.assertEqual(new_gate["status"], "open")
        self.assertEqual(new_gate["open_since"], "2026-09-21T10:00:00+00:00")
        self.assertIn("OPENED", events)

    def test_gate_stays_closed_when_second_camera_has_no_observation_this_cycle(self):
        config = make_config()
        gate = fresh_state(config, diverged_main=3, diverged_second=1)
        new_gate, events = gm.update_gate_state(
            gate,
            config,
            {"main": {"is_match": False, "open_change": None}},
            "2026-09-21T10:00:00+00:00",
        )
        self.assertEqual(new_gate["status"], "closed")
        self.assertNotIn("OPENED", events)
        # the missing camera keeps its stale counts
        self.assertEqual(new_gate["cameras"]["second"]["consecutive_diverged_count"], 1)

    def test_matching_camera_refreshes_closed_baseline_while_closed(self):
        config = make_config()
        gate, events = observe(fresh_state(config), config,
                               main_match=True, second_match=True)
        self.assertIn("REFRESH_CLOSED_BASELINE:main", events)
        self.assertEqual(gate["cameras"]["main"]["baseline_updated_at"],
                         "2026-09-21T10:00:00+00:00")


class TestGateClosesWhenAllAgree(unittest.TestCase):
    def test_gate_closes_when_all_cameras_match_closed_baseline(self):
        config = make_config()
        # main needs 3 matched, second needs 1.
        gate = open_state(config, divergence_main=1, divergence_second=1)
        gate["cameras"]["main"]["consecutive_matched_count"] = 2
        new_gate, events = observe(gate, config,
                                   main_match=True, second_match=True)
        self.assertEqual(new_gate["status"], "closed")
        self.assertIn("CLOSED", events)
        self.assertIsNone(new_gate["open_since"])
        self.assertIsNone(new_gate["last_alert_sent_at"])

    def test_gate_stays_open_when_only_one_camera_matches(self):
        config = make_config()
        gate = open_state(config)
        new_gate, events = observe(gate, config,
                                   main_match=True, second_match=False)
        self.assertEqual(new_gate["status"], "open")
        self.assertNotIn("CLOSED", events)

    def test_gate_closes_via_open_change_when_every_camera_view_changes(self):
        config = make_config()
        gate = open_state(config, open_diverged_main=2, open_diverged_second=0)
        new_gate, events = observe(gate, config,
                                   main_match=False, second_match=False,
                                   main_open_change=True, second_open_change=True)
        self.assertEqual(new_gate["status"], "closed")
        self.assertIn("CLOSED", events)
        self.assertIsNone(new_gate["open_since"])
        self.assertIsNone(new_gate["last_alert_sent_at"])

    def test_gate_stays_open_when_single_camera_view_changes(self):
        config = make_config()
        gate = open_state(config, open_diverged_main=2, open_diverged_second=0)
        new_gate, events = observe(gate, config,
                                   main_match=False, second_match=False,
                                   main_open_change=True, second_open_change=False)
        self.assertEqual(new_gate["status"], "open")
        self.assertNotIn("CLOSED", events)

    def test_open_change_not_applied_before_open_alert_minutes(self):
        config = make_config(open_alert_minutes=10)
        # open only 5 minutes before the observed cycle (now_iso is 10:00:00),
        # so the open-baseline self-calibration window has not opened yet.
        gate = open_state(config, open_since="2026-09-21T09:55:00+00:00")
        gate["cameras"]["main"]["consecutive_open_diverged_count"] = 5
        new_gate, events = observe(gate, config,
                                   main_match=False, second_match=False,
                                   main_open_change=True, second_open_change=True)
        self.assertEqual(new_gate["status"], "open")
        self.assertEqual(new_gate["cameras"]["main"]["consecutive_open_diverged_count"], 5)


class TestAlertTiming(unittest.TestCase):
    def test_alert_due_immediately_when_threshold_zero(self):
        config = make_config(open_alert_minutes=0)
        gate = open_state(config, open_since="2026-09-21T09:59:59+00:00")
        new_gate, events = observe(gate, config,
                                   main_match=False, second_match=False,
                                   main_open_change=False, second_open_change=False)
        self.assertIn("ALERT_DUE", events)

    def test_no_alert_before_open_alert_minutes(self):
        config = make_config(open_alert_minutes=5)
        gate = open_state(config, open_since="2026-09-21T09:59:59+00:00")
        new_gate, events = observe(gate, config,
                                   main_match=False, second_match=False,
                                   main_open_change=False, second_open_change=False)
        self.assertNotIn("ALERT_DUE", events)

    def test_reminder_due_after_cooldown(self):
        config = make_config(open_alert_minutes=0, reminder_cooldown_hours=1)
        gate = open_state(config, open_since="2026-09-21T08:59:00+00:00")
        gate["last_alert_sent_at"] = "2026-09-21T08:59:30+00:00"
        new_gate, events = observe(gate, config,
                                   main_match=False, second_match=False,
                                   main_open_change=False, second_open_change=False)
        self.assertIn("ALERT_DUE", events)

    def test_no_reminder_within_cooldown(self):
        config = make_config(open_alert_minutes=0, reminder_cooldown_hours=1)
        gate = open_state(config, open_since="2026-09-21T09:50:00+00:00")
        gate["last_alert_sent_at"] = "2026-09-21T09:59:30+00:00"
        new_gate, events = observe(gate, config,
                                   main_match=False, second_match=False,
                                   main_open_change=False, second_open_change=False)
        self.assertNotIn("ALERT_DUE", events)


class TestConfigValidation(unittest.TestCase):
    def test_config_cameras_list_is_required(self):
        with self.assertRaises(ValueError):
            gm.validate_config({"cameras": []})
        with self.assertRaises(ValueError):
            gm.validate_config({})

    def test_config_rejects_invalid_roi(self):
        config = make_config()
        config["cameras"][0]["roi"] = {"x": 0, "y": 0, "w": 0, "h": 100}
        with self.assertRaises(ValueError):
            gm.validate_config(config)

    def test_config_rejects_duplicate_camera_names(self):
        config = make_config()
        config["cameras"][1]["name"] = "main"
        with self.assertRaises(ValueError):
            gm.validate_config(config)

    def test_config_rejects_unsafe_camera_name(self):
        config = make_config()
        config["cameras"][1]["name"] = "cam; rm -rf"
        with self.assertRaises(ValueError):
            gm.validate_config(config)

    def test_config_requires_global_alert_keys(self):
        config = make_config()
        del config["reminder_cooldown_hours"]
        with self.assertRaises(ValueError):
            gm.validate_config(config)

    def test_env_reports_missing_camera_rtsp_url(self):
        config = make_config()
        env = {"SMTP_HOST": "x", "SMTP_PORT": "465",
               "SMTP_USER": "u", "SMTP_PASSWORD": "p",
               "ALERT_RECIPIENT": "r", "RTSP_URL": "rtsp://a"}
        missing = gm.validate_env(config, env)
        self.assertIn("RTSP_URL_SECOND", missing)
        self.assertNotIn("RTSP_URL", missing)


class TestComputeDiff(unittest.TestCase):
    def _img(self, value):
        arr = np.full((50, 50), value, dtype=np.uint8)
        return Image.fromarray(arr, mode="L")

    def test_identical_images_zero(self):
        self.assertEqual(gm.compute_diff(self._img(100), self._img(100)), 0.0)

    def test_black_vs_white_is_one(self):
        self.assertEqual(gm.compute_diff(self._img(0), self._img(255)), 1.0)

    def test_resolution_mismatch_is_maximally_diverged(self):
        arr_small = np.zeros((10, 10), dtype=np.uint8)
        arr_big = np.zeros((20, 20), dtype=np.uint8)
        self.assertEqual(
            gm.compute_diff(Image.fromarray(arr_small, mode="L"),
                            Image.fromarray(arr_big, mode="L")),
            1.0,
        )


if __name__ == "__main__":
    unittest.main()