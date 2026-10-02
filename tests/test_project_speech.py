#!/usr/bin/env python3
"""Tests for per-project notification speech.

Run: python3 tests/test_project_speech.py

Most cases stub the synthesizer, so they are fast and run anywhere. The few
that drive the real OS voice are skipped where there is none.
"""

import importlib.util
import os
import platform
import shutil
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "project_speech", ROOT / "hooks" / "project_speech.py"
)
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


def has_voice():
    if platform.system() == "Darwin":
        return shutil.which("say") is not None
    return any(shutil.which(b) for b in ("espeak-ng", "espeak", "pico2wave"))


class Slugify(unittest.TestCase):
    def test_keeps_an_ordinary_repo_name(self):
        self.assertEqual(ps.slugify("bells-and-whistles"), "bells-and-whistles")

    def test_lowercases(self):
        self.assertEqual(ps.slugify("MyApp"), "myapp")

    def test_spaces_become_hyphens(self):
        self.assertEqual(ps.slugify("My Cool App"), "my-cool-app")

    def test_collapses_runs_of_separators(self):
        self.assertEqual(ps.slugify("a   b"), "a-b")

    def test_strips_leading_and_trailing_separators(self):
        self.assertEqual(ps.slugify("  -weird-  "), "weird")

    def test_drops_shell_metacharacters(self):
        self.assertEqual(ps.slugify("x; rm -rf /"), "x-rm-rf")

    def test_truncates_a_very_long_name(self):
        self.assertLessEqual(len(ps.slugify("a" * 200)), ps.MAX_SLUG)

    def test_a_name_with_nothing_usable_yields_none(self):
        for name in ("", None, "///", "   "):
            with self.subTest(name=name):
                self.assertIsNone(ps.slugify(name))

    def test_every_slug_is_a_legal_wire_field(self):
        """The slug travels to the listener, which validates it with LABEL_RE."""
        import re

        label_re = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
        for name in ("My Cool App", "x; rm -rf /", "a" * 200, "../../etc", "#!$%^"):
            slug = ps.slugify(name)
            if slug is not None:
                with self.subTest(name=name):
                    self.assertRegex(slug, label_re)


class ProjectFromCwd(unittest.TestCase):
    def test_uses_the_directory_name(self):
        self.assertEqual(ps.project_from_cwd("/Users/x/bells-and-whistles"),
                         "bells-and-whistles")

    def test_ignores_a_trailing_slash(self):
        self.assertEqual(ps.project_from_cwd("/Users/x/myapp/"), "myapp")

    def test_the_filesystem_root_has_no_project(self):
        self.assertIsNone(ps.project_from_cwd("/"))

    def test_no_cwd_at_all_yields_none(self):
        self.assertIsNone(ps.project_from_cwd(None))


class ProjectFromHook(unittest.TestCase):
    def test_prefers_the_payload_cwd(self):
        env = {"CLAUDE_PROJECT_DIR": "/other/elsewhere", "PWD": "/third/place"}
        self.assertEqual(
            ps.project_from_hook({"cwd": "/a/the-repo"}, env), "the-repo"
        )

    def test_falls_back_to_the_project_dir_claude_code_sets(self):
        env = {"CLAUDE_PROJECT_DIR": "/a/from-env", "PWD": "/third/place"}
        self.assertEqual(ps.project_from_hook({}, env), "from-env")

    def test_falls_back_to_pwd_last(self):
        self.assertEqual(ps.project_from_hook({}, {"PWD": "/a/from-pwd"}), "from-pwd")

    def test_a_payload_that_is_not_a_dict_does_not_raise(self):
        self.assertEqual(ps.project_from_hook("garbage", {"PWD": "/a/b"}), "b")


class Phrase(unittest.TestCase):
    def test_a_finished_turn_says_the_task_is_complete(self):
        self.assertEqual(
            ps.phrase_for("bells-and-whistles", "stop"),
            "Task complete on bells and whistles",
        )

    def test_a_blocked_turn_says_claude_is_waiting(self):
        self.assertEqual(
            ps.phrase_for("bells-and-whistles", "notification"),
            "Claude is waiting for you on bells and whistles",
        )

    def test_an_unknown_event_does_not_crash_the_chime(self):
        self.assertIn("bells and whistles", ps.phrase_for("bells-and-whistles", "???"))

    def test_underscores_and_dots_too(self):
        self.assertEqual(ps.spoken_name("my_app.v2"), "my app v2")


class Ensure(unittest.TestCase):
    """Caching behaviour, with the synthesizer stubbed out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sounds = Path(self.tmp.name)
        self.calls = []
        self.real = ps.synthesize

        def fake(text, target, accent="us", gender="male", voice=None, log=None):
            self.calls.append((text, accent, gender, voice))
            Path(target).write_bytes(b"RIFF")
            return True

        ps.synthesize = fake

    def tearDown(self):
        ps.synthesize = self.real
        self.tmp.cleanup()

    def test_synthesizes_on_first_use(self):
        found = ps.ensure(self.sounds, "us", "male", "myapp")
        self.assertTrue(found.is_file())
        self.assertEqual(len(self.calls), 1)

    def test_second_call_reuses_the_cached_file(self):
        first = ps.ensure(self.sounds, "us", "male", "myapp")
        second = ps.ensure(self.sounds, "us", "male", "myapp")
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)

    def test_caches_under_the_sounds_tree_so_the_phone_can_fetch_it(self):
        found = ps.ensure(self.sounds, "us", "male", "myapp")
        self.assertEqual(
            found.relative_to(self.sounds).as_posix(),
            "speech/projects/samantha/notification_myapp.wav",
        )

    def test_each_event_gets_its_own_cache(self):
        """Two different sentences cannot share one file."""
        a = ps.ensure(self.sounds, "us", "male", "myapp", "stop")
        b = ps.ensure(self.sounds, "us", "male", "myapp", "notification")
        self.assertNotEqual(a, b)
        self.assertIn("Task complete", self.calls[0][0])
        self.assertIn("waiting for you", self.calls[1][0])

    def test_each_voice_gets_its_own_cache(self):
        """Changing the voice must not serve back the old voice's WAV."""
        a = ps.ensure(self.sounds, "us", "male", "myapp")
        b = ps.ensure(self.sounds, "us", "male", "myapp", voice="Daniel")
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.calls), 2)

    def test_the_chosen_voice_reaches_the_synthesizer(self):
        ps.ensure(self.sounds, "us", "male", "myapp", voice="Daniel")
        self.assertEqual(self.calls[0][3], "Daniel")

    def test_speaks_the_project_name(self):
        ps.ensure(self.sounds, "us", "male", "myapp")
        self.assertIn("myapp", self.calls[0][0])

    def test_an_unusable_name_is_declined_rather_than_guessed_at(self):
        self.assertIsNone(ps.ensure(self.sounds, "us", "male", "///"))
        self.assertEqual(self.calls, [])

    def test_an_empty_cached_file_is_regenerated(self):
        target = ps.cache_path(self.sounds, "us", "male", "myapp")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"")
        self.assertTrue(ps.ensure(self.sounds, "us", "male", "myapp").stat().st_size)

    def test_failed_synthesis_returns_none_rather_than_a_broken_path(self):
        ps.synthesize = lambda *a, **k: False
        self.assertIsNone(ps.ensure(self.sounds, "us", "male", "myapp"))


class SynthesisFailure(unittest.TestCase):
    """No voice on the machine must degrade, never raise and never leave junk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sounds = Path(self.tmp.name)
        self.real = ps._command_for
        ps._command_for = lambda *a, **k: None

    def tearDown(self):
        ps._command_for = self.real
        self.tmp.cleanup()

    def test_returns_none_when_nothing_can_speak(self):
        self.assertIsNone(ps.ensure(self.sounds, "us", "male", "myapp"))

    def test_leaves_no_partial_file_behind(self):
        ps.ensure(self.sounds, "us", "male", "myapp")
        bucket = ps.cache_path(self.sounds, "us", "male", "myapp").parent
        self.assertEqual(list(bucket.iterdir()), [])

    def test_a_command_that_exits_nonzero_is_not_cached(self):
        ps._command_for = lambda *a, **k: ["false"]
        self.assertIsNone(ps.ensure(self.sounds, "us", "male", "myapp"))

    def test_a_command_that_writes_nothing_is_not_cached(self):
        ps._command_for = lambda *a, **k: ["true"]
        self.assertIsNone(ps.ensure(self.sounds, "us", "male", "myapp"))


@unittest.skipUnless(has_voice(), "no text-to-speech voice on this machine")
class RealVoice(unittest.TestCase):
    """The one test that actually drives the operating system's voice."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sounds = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_produces_a_playable_wav(self):
        for event in ("stop", "notification"):
            with self.subTest(event=event):
                found = ps.ensure(self.sounds, "us", "male",
                                  "bells-and-whistles", event)
                self.assertIsNotNone(found, "synthesis produced nothing")
                with wave.open(str(found)) as handle:
                    self.assertGreater(handle.getnframes(), 0)

    def test_the_cached_file_is_reused_not_recut(self):
        first = ps.ensure(self.sounds, "us", "male", "bells-and-whistles")
        stamp = first.stat().st_mtime_ns
        second = ps.ensure(self.sounds, "us", "male", "bells-and-whistles")
        self.assertEqual(second.stat().st_mtime_ns, stamp)


@unittest.skipUnless(platform.system() == "Darwin", "macOS voice selection")
class MacVoiceDefault(unittest.TestCase):
    """Samantha unless told otherwise, whatever the gender setting says."""

    def test_defaults_to_samantha(self):
        for gender in ("male", "female"):
            with self.subTest(gender=gender):
                self.assertEqual(ps._mac_voice("us", gender), "Samantha")

    def test_an_explicit_voice_wins(self):
        self.assertEqual(ps._mac_voice("us", "male", "Daniel"), "Daniel")

    def test_a_voice_that_is_not_installed_falls_back(self):
        """A typo must not cost you the chime."""
        fallback = ps._mac_voice("us", "male", "Nonexistent Voice")
        self.assertIsNotNone(fallback)
        self.assertNotEqual(fallback, "Nonexistent Voice")

    def test_a_bare_name_resolves_to_the_accents_locale(self):
        """"Reed" ships as both US and UK; the accent breaks the tie."""
        us = ps._mac_voice("us", "male", "Reed")
        uk = ps._mac_voice("uk", "male", "Reed")
        if us and uk and us != uk:
            self.assertIn("US", us)
            self.assertIn("UK", uk)


class VoiceKey(unittest.TestCase):
    """The cache bucket has to change when the voice does."""

    @unittest.skipUnless(platform.system() == "Darwin", "macOS buckets by voice")
    def test_buckets_by_voice_on_macos(self):
        self.assertEqual(ps.voice_key("us", "male"), "samantha")
        self.assertEqual(ps.voice_key("us", "male", "Daniel"), "daniel")

    @unittest.skipUnless(platform.system() == "Darwin", "macOS buckets by voice")
    def test_a_full_parenthesised_name_still_yields_one_segment(self):
        key = ps.voice_key("us", "male", "Reed (English (US))")
        self.assertNotIn("/", key)
        self.assertEqual(key, "reed-english-us")

    @unittest.skipIf(platform.system() == "Darwin", "other platforms ignore it")
    def test_elsewhere_the_bucket_is_accent_and_gender(self):
        self.assertEqual(ps.voice_key("uk", "female", "Samantha"), "uk-female")


@unittest.skipUnless(platform.system() == "Darwin", "macOS voice selection")
class MacVoiceSelection(unittest.TestCase):
    def test_every_resolved_voice_is_one_macos_actually_has(self):
        installed = {full for full, _ in ps.installed_mac_voices()}
        self.assertTrue(installed, "say -v '?' listed nothing")
        for accent in ("us", "uk"):
            for gender in ("male", "female"):
                for wanted in (None, "Daniel", "Nonexistent"):
                    voice = ps._mac_voice(accent, gender, wanted)
                    if voice is None:
                        continue
                    with self.subTest(accent=accent, gender=gender, wanted=wanted):
                        self.assertIn(voice, installed)

    def test_an_installed_choice_is_never_second_guessed(self):
        """Whatever you ask for, if macOS has it, that is what you get."""
        installed = {full.split(" (")[0] for full, _ in ps.installed_mac_voices()}
        asked = 0
        for name in sorted(installed)[:12]:
            resolved = ps._mac_voice("us", "male", name)
            with self.subTest(name=name):
                self.assertEqual(resolved.split(" (")[0], name)
            asked += 1
        self.assertGreater(asked, 0, "no installed voices to check")

    def test_a_voice_with_spaces_in_its_name_survives_intact(self):
        """\"Reed (English (US))\" must come back whole; \"Reed\" alone is ambiguous."""
        for wanted in ("Reed", "Rocko", "Shelley"):
            voice = ps._mac_voice("us", "male", wanted)
            if voice and "(" in voice:
                self.assertTrue(voice.endswith(")"))


class CLI(unittest.TestCase):
    def test_slug_mode_prints_the_project(self):
        import io
        import contextlib

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ps.main(["slug", "--cwd", "/a/bells-and-whistles"])
        self.assertEqual(out.getvalue().strip(), "bells-and-whistles")

    def test_slug_mode_prints_nothing_for_an_unusable_name(self):
        import io
        import contextlib

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ps.main(["slug", "--cwd", "/"])
        self.assertEqual(out.getvalue().strip(), "")

    def test_wav_mode_prints_nothing_when_synthesis_fails(self):
        import io
        import contextlib

        real = ps._command_for
        ps._command_for = lambda *a, **k: None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    ps.main(["wav", "--project", "myapp", "--sounds-dir", tmp])
                self.assertEqual(out.getvalue().strip(), "")
        finally:
            ps._command_for = real


if __name__ == "__main__":
    unittest.main(verbosity=2)
