#!/usr/bin/env python3
"""Tests for the claw-bell chime listener.

Run: python3 tests/test_chime_listener.py

Uses a stub player (/usr/bin/true) so nothing is actually audible.
"""

import importlib.util
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("chime_listener", ROOT / "hooks" / "chime-listener.py")
chime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chime)


def build_sounds(base):
    """A miniature sounds/ tree mirroring the real layout."""
    base = Path(base)
    for theme in ("dnd", "classical", "videogame"):
        (base / theme).mkdir(parents=True)
        (base / theme / f"{theme}_one.wav").write_bytes(b"RIFF")
        (base / theme / f"{theme}_two.wav").write_bytes(b"RIFF")
    speech = base / "speech" / "us" / "male"
    speech.mkdir(parents=True)
    for name in ("stop_0.wav", "notification_0.wav", "number_3.wav",
                 "notification_window_2_0.wav"):
        (speech / name).write_bytes(b"RIFF")
    return base


class ParseMessage(unittest.TestCase):
    THEMES = {"dnd", "classical", "videogame"}

    def test_accepts_a_well_formed_line(self):
        self.assertEqual(
            chime.parse_message("stop|dnd|s1\n", self.THEMES),
            ("stop", "dnd", "s1", None),
        )

    def test_accepts_notification_event(self):
        self.assertEqual(
            chime.parse_message("notification|classical|s2\n", self.THEMES),
            ("notification", "classical", "s2", None),
        )

    def test_accepts_an_optional_project_field(self):
        self.assertEqual(
            chime.parse_message("notification|dnd|s1|bells-and-whistles\n", self.THEMES),
            ("notification", "dnd", "s1", "bells-and-whistles"),
        )

    def test_rejects_shell_metacharacters_in_project(self):
        with self.assertRaises(chime.ChimeError):
            chime.parse_message("stop|dnd|s1|x; rm -rf /\n", self.THEMES)

    def test_rejects_path_traversal_in_project(self):
        with self.assertRaises(chime.ChimeError):
            chime.parse_message("stop|dnd|s1|../../etc/passwd\n", self.THEMES)

    def test_rejects_an_empty_project_field(self):
        with self.assertRaises(chime.ChimeError):
            chime.parse_message("stop|dnd|s1|\n", self.THEMES)

    def test_rejects_path_traversal_in_theme(self):
        with self.assertRaises(chime.ChimeError):
            chime.parse_message("stop|../../etc|s1\n", self.THEMES)

    def test_rejects_absolute_path_in_theme(self):
        with self.assertRaises(chime.ChimeError):
            chime.parse_message("stop|/etc/passwd|s1\n", self.THEMES)

    def test_rejects_theme_that_does_not_exist(self):
        with self.assertRaises(chime.ChimeError):
            chime.parse_message("stop|jazz|s1\n", self.THEMES)

    def test_rejects_unknown_event(self):
        with self.assertRaises(chime.ChimeError):
            chime.parse_message("rm -rf|dnd|s1\n", self.THEMES)

    def test_rejects_shell_metacharacters_in_label(self):
        with self.assertRaises(chime.ChimeError):
            chime.parse_message("stop|dnd|s1; rm -rf /\n", self.THEMES)

    def test_rejects_wrong_field_count(self):
        for line in ("stop|dnd\n", "stop|dnd|s1|a|b\n", "stop\n"):
            with self.subTest(line=line), self.assertRaises(chime.ChimeError):
                chime.parse_message(line, self.THEMES)

    def test_rejects_empty_line(self):
        with self.assertRaises(chime.ChimeError):
            chime.parse_message("\n", self.THEMES)

    def test_rejects_overlong_line(self):
        with self.assertRaises(chime.ChimeError):
            chime.parse_message("stop|dnd|" + "a" * 300 + "\n", self.THEMES)


class ListThemes(unittest.TestCase):
    def test_excludes_the_speech_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            sounds = build_sounds(tmp)
            self.assertEqual(
                chime.list_themes(sounds), {"dnd", "classical", "videogame"}
            )

    def test_missing_directory_yields_no_themes(self):
        self.assertEqual(chime.list_themes("/nonexistent/sounds"), set())

    def test_real_repo_themes_include_the_per_host_ones(self):
        themes = chime.list_themes(ROOT / "sounds")
        self.assertTrue({"dnd", "classical", "videogame"} <= themes)


class Resolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sounds = build_sounds(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_plays_melody_then_speech_by_default(self):
        tracks = chime.resolve(self.sounds, "stop", "dnd", {})
        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].parent.name, "dnd")
        self.assertEqual(tracks[1].name, "stop_0.wav")

    def test_melody_comes_from_the_requested_theme(self):
        tracks = chime.resolve(self.sounds, "stop", "classical", {})
        self.assertEqual(tracks[0].parent.name, "classical")

    def test_sound_only_mode_skips_speech(self):
        tracks = chime.resolve(self.sounds, "stop", "dnd", {"mode": "sound_only"})
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].parent.name, "dnd")

    def test_voice_only_mode_skips_melody(self):
        tracks = chime.resolve(self.sounds, "stop", "dnd", {"mode": "voice_only"})
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].name, "stop_0.wav")

    def test_speech_matches_the_event(self):
        tracks = chime.resolve(self.sounds, "notification", "dnd", {})
        self.assertEqual(tracks[1].name, "notification_0.wav")

    def test_speech_glob_excludes_window_and_number_variants(self):
        speech = chime.pick_speech(self.sounds, "notification", "us", "male")
        self.assertEqual(speech.name, "notification_0.wav")

    def test_missing_speech_voice_degrades_to_melody_only(self):
        tracks = chime.resolve(
            self.sounds, "stop", "dnd", {"accent": "uk", "gender": "female"}
        )
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].parent.name, "dnd")


class ProjectSpeech(unittest.TestCase):
    """A notification that knows its project says the project's name."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sounds = build_sounds(self.tmp.name)
        self.asked = []

        cache = Path(self.sounds, "speech", "projects", "us", "male")
        cache.mkdir(parents=True)

        def fake_ensure(sounds_dir, accent, gender, slug, log=None):
            """Stands in for the OS voice: records the ask, hands back a file."""
            self.asked.append((accent, gender, slug))
            if slug == "nosynth":
                return None
            wav = Path(sounds_dir, "speech", "projects", accent, gender,
                       f"notification_{slug}.wav")
            wav.parent.mkdir(parents=True, exist_ok=True)
            wav.write_bytes(b"RIFF")
            return wav

        self.real_ensure = chime.project_speech.ensure
        chime.project_speech.ensure = fake_ensure

    def tearDown(self):
        chime.project_speech.ensure = self.real_ensure
        self.tmp.cleanup()

    def test_notification_speaks_the_project(self):
        tracks = chime.resolve(self.sounds, "notification", "dnd", {},
                               project="bells-and-whistles")
        self.assertEqual(tracks[1].name, "notification_bells-and-whistles.wav")

    def test_project_wav_lives_under_the_sounds_tree(self):
        """chime_web.py only serves sounds/, so the phone needs it in there."""
        tracks = chime.resolve(self.sounds, "notification", "dnd", {},
                               project="bells-and-whistles")
        self.assertTrue(tracks[1].is_relative_to(Path(self.sounds)))

    def test_stop_keeps_the_shipped_phrase(self):
        tracks = chime.resolve(self.sounds, "stop", "dnd", {}, project="whatever")
        self.assertEqual(tracks[1].name, "stop_0.wav")
        self.assertEqual(self.asked, [])

    def test_no_project_keeps_the_shipped_phrase(self):
        tracks = chime.resolve(self.sounds, "notification", "dnd", {})
        self.assertEqual(tracks[1].name, "notification_0.wav")
        self.assertEqual(self.asked, [])

    def test_failed_synthesis_falls_back_rather_than_going_silent(self):
        tracks = chime.resolve(self.sounds, "notification", "dnd", {},
                               project="nosynth")
        self.assertEqual(tracks[1].name, "notification_0.wav")

    def test_honours_the_configured_voice(self):
        chime.resolve(self.sounds, "notification", "dnd",
                      {"accent": "uk", "gender": "female"}, project="proj")
        self.assertEqual(self.asked, [("uk", "female", "proj")])

    def test_project_announce_false_opts_out(self):
        tracks = chime.resolve(self.sounds, "notification", "dnd",
                               {"project_announce": False}, project="proj")
        self.assertEqual(tracks[1].name, "notification_0.wav")
        self.assertEqual(self.asked, [])

    def test_sound_only_mode_still_skips_speech_entirely(self):
        tracks = chime.resolve(self.sounds, "notification", "dnd",
                               {"mode": "sound_only"}, project="proj")
        self.assertEqual(len(tracks), 1)
        self.assertEqual(self.asked, [])


class LoadConfig(unittest.TestCase):
    def test_override_file_wins_over_plugin_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(
                json.dumps({"theme": "videogame", "gender": "male"})
            )
            override = root / "claw-bell.json"
            override.write_text(json.dumps({"theme": "dnd"}))

            config = chime.load_config(root, override)
            self.assertEqual(config["theme"], "dnd")
            self.assertEqual(config["gender"], "male")

    def test_absent_override_leaves_plugin_config_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(json.dumps({"theme": "videogame"}))
            config = chime.load_config(root, root / "nope.json")
            self.assertEqual(config["theme"], "videogame")

    def test_malformed_json_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text("{not json")
            self.assertEqual(chime.load_config(root, None), {})


class EndToEnd(unittest.TestCase):
    """Drive the real socket server the way the remote hook does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sounds = build_sounds(self.tmp.name)
        self.logs = []
        self.player = chime.Player(command="/usr/bin/true", log=self.logs.append)

        handler = chime.make_handler(self.sounds, {}, self.player, self.logs.append)

        class Server(chime.socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def send(self, line):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(line.encode())
        time.sleep(0.3)

    def test_bound_to_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_valid_message_plays_two_tracks(self):
        self.send("stop|dnd|s1\n")
        self.assertEqual(len(self.player.played), 2)
        self.assertEqual(self.player.played[0].parent.name, "dnd")

    def test_each_host_gets_its_own_theme(self):
        self.send("stop|dnd|s1\n")
        self.send("notification|classical|s2\n")
        themes = [track.parent.name for track in self.player.played]
        self.assertIn("dnd", themes)
        self.assertIn("classical", themes)

    def test_hostile_message_plays_nothing(self):
        self.send("stop|../../../etc|s1\n")
        self.assertEqual(self.player.played, [])
        self.assertTrue(any("rejected" in line for line in self.logs))

    def test_four_field_message_is_accepted(self):
        """Protocol acceptance only — a stub voice keeps the suite off `say`."""
        real = chime.project_speech.ensure
        chime.project_speech.ensure = lambda *a, **k: None
        try:
            self.send("notification|dnd|s1|bells-and-whistles\n")
        finally:
            chime.project_speech.ensure = real
        self.assertEqual(len(self.player.played), 2)

    def test_garbage_does_not_kill_the_server(self):
        self.send("total garbage\n")
        self.send("stop|dnd|s1\n")
        self.assertEqual(len(self.player.played), 2)

    def exchange(self, line):
        """Send a line and return the listener's reply."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(line.encode())
            return sock.recv(16)

    def test_acks_an_accepted_message(self):
        self.assertEqual(self.exchange("stop|dnd|s1\n"), b"ok\n")

    def test_acks_a_rejected_message_rather_than_stalling(self):
        self.assertEqual(self.exchange("stop|../../etc|s1\n"), b"err\n")

    def test_acks_garbage_rather_than_stalling(self):
        self.assertEqual(self.exchange("nonsense\n"), b"err\n")

    def test_ping_answers_pong(self):
        self.assertEqual(self.exchange("ping\n"), b"pong\n")

    def test_ping_plays_nothing(self):
        self.exchange("ping\n")
        time.sleep(0.2)
        self.assertEqual(self.player.played, [])

    def test_ping_is_not_logged_as_a_rejection(self):
        self.exchange("ping\n")
        self.assertFalse(any("rejected" in line for line in self.logs))


class BuildEvent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sounds = build_sounds(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_maps_tracks_to_sound_urls(self):
        tracks = chime.resolve(self.sounds, "stop", "dnd", {})
        ev = chime.build_event("stop", "dnd", "s1", tracks, self.sounds, True, 7,
                               "2026-09-17T14:00:00Z")
        # resolve() picks randomly among the theme's WAVs, so assert the shape.
        self.assertRegex(ev["melody"], r"^/sounds/dnd/dnd_\w+\.wav$")
        self.assertEqual(ev["speech"], "/sounds/speech/us/male/stop_0.wav")

    def test_carries_identity_and_presence(self):
        tracks = chime.resolve(self.sounds, "stop", "dnd", {})
        ev = chime.build_event("stop", "dnd", "s1", tracks, self.sounds, False, 7,
                               "2026-09-17T14:00:00Z")
        self.assertEqual(ev["label"], "s1")
        self.assertEqual(ev["event"], "stop")
        self.assertEqual(ev["theme"], "dnd")
        self.assertEqual(ev["mac_active"], False)
        self.assertEqual(ev["id"], 7)
        self.assertEqual(ev["ts"], "2026-09-17T14:00:00Z")

    def test_carries_the_project_to_the_browser(self):
        tracks = chime.resolve(self.sounds, "stop", "dnd", {})
        ev = chime.build_event("notification", "dnd", "s1", tracks, self.sounds,
                               True, 1, "t", "bells-and-whistles")
        self.assertEqual(ev["project"], "bells-and-whistles")

    def test_project_is_none_when_the_sender_did_not_send_one(self):
        tracks = chime.resolve(self.sounds, "stop", "dnd", {})
        ev = chime.build_event("stop", "dnd", "s1", tracks, self.sounds, True, 1, "t")
        self.assertIsNone(ev["project"])

    def test_speech_is_none_when_only_a_melody_played(self):
        tracks = chime.resolve(self.sounds, "stop", "dnd", {"mode": "sound_only"})
        ev = chime.build_event("stop", "dnd", "s1", tracks, self.sounds, True, 1, "t")
        self.assertIsNone(ev["speech"])


class PresenceRouting(unittest.TestCase):
    """The Mac plays only when someone is at it; the event always goes out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sounds = build_sounds(self.tmp.name)
        self.logs = []
        self.player = chime.Player(command="/usr/bin/true", log=self.logs.append)
        self.published = []
        self.server = None

        class FakeBroadcaster:
            def __init__(self, sink, listeners=1):
                self.sink = sink
                self.listeners = listeners

            def publish(self, payload):
                self.sink.append(payload)
                return self.listeners

            def subscriber_count(self):
                return self.listeners

        self.broadcaster = FakeBroadcaster(self.published)

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.tmp.cleanup()

    def serve_with(self, present, broadcaster=True, presence_obj=True):
        class FakePresence:
            def active(self_inner):
                return present

        handler = chime.make_handler(
            self.sounds, {}, self.player, self.logs.append,
            self.broadcaster if broadcaster else None,
            FakePresence() if presence_obj else None,
        )

        class Server(chime.socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server.server_address[1]

    def send(self, port, line):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(line.encode())
            s.recv(16)
        time.sleep(0.3)

    def test_plays_locally_when_present(self):
        self.send(self.serve_with(True), "stop|dnd|s1\n")
        self.assertEqual(len(self.player.played), 2)

    def test_stays_silent_locally_when_absent_and_someone_is_listening(self):
        self.send(self.serve_with(False), "stop|dnd|s1\n")
        self.assertEqual(self.player.played, [])

    def test_plays_locally_when_absent_but_nobody_is_listening(self):
        """Handing a chime to nobody loses it, which is worse than playing it
        to an empty room."""
        self.broadcaster.listeners = 0
        self.send(self.serve_with(False), "stop|dnd|s1\n")
        self.assertEqual(len(self.player.played), 2)

    def test_logs_why_it_played_while_away(self):
        self.broadcaster.listeners = 0
        self.send(self.serve_with(False), "stop|dnd|s1\n")
        self.assertTrue(any("nobody listening" in m for m in self.logs))

    def test_plays_locally_when_absent_and_there_is_no_broadcaster_at_all(self):
        self.send(self.serve_with(False, broadcaster=False), "stop|dnd|s1\n")
        self.assertEqual(len(self.player.played), 2)

    def test_broadcasts_regardless_of_presence(self):
        self.send(self.serve_with(False), "stop|dnd|s1\n")
        self.assertEqual(len(self.published), 1)
        self.assertEqual(self.published[0]["label"], "s1")
        self.assertEqual(self.published[0]["mac_active"], False)

    def test_rejected_chime_is_not_broadcast(self):
        self.send(self.serve_with(True), "stop|../../etc|s1\n")
        self.assertEqual(self.published, [])

    def test_event_ids_increment(self):
        port = self.serve_with(True)
        self.send(port, "stop|dnd|s1\n")
        self.send(port, "notification|classical|s2\n")
        self.assertEqual([e["id"] for e in self.published], [1, 2])

    def test_without_presence_it_always_plays(self):
        """Backwards compatibility: no presence object means today's behaviour."""
        self.send(self.serve_with(False, broadcaster=False, presence_obj=False),
                  "stop|dnd|s1\n")
        self.assertEqual(len(self.player.played), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
