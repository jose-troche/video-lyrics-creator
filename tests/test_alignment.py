import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from video_lyrics_creator.alignment import (
    TimedWord,
    align_lines,
    apply_canonical_lines,
    parse_timing_file,
    transcribe_words,
)
from video_lyrics_creator.errors import VideoLyricsError


class AlignmentTests(unittest.TestCase):
    def test_alignment_preserves_canonical_lines(self):
        lines = ["Grace has found me", "Love will lead me home"]
        words = [
            TimedWord("grace", 1.0, 1.4),
            TimedWord("has", 1.5, 1.7),
            TimedWord("found", 1.8, 2.1),
            TimedWord("me", 2.2, 2.5),
            TimedWord("love", 4.0, 4.3),
            TimedWord("will", 4.4, 4.6),
            TimedWord("lead", 4.7, 5.0),
            TimedWord("me", 5.1, 5.3),
            TimedWord("home", 5.4, 5.8),
        ]
        cues = align_lines(lines, words, duration=8.0)
        self.assertEqual([cue["text"] for cue in cues], lines)
        self.assertEqual(cues[0]["start"], 1.0)
        self.assertEqual(cues[1]["end"], 5.8)
        self.assertEqual(cues[0]["alignment_confidence"], 1.0)

    def test_audio_selects_lines_and_unmatched_reference_text_is_omitted(self):
        lines = [
            "Main",
            "Song title",
            "Grace has found me",
            "Production notes",
        ]
        words = [
            TimedWord("grace", 2.0, 2.3),
            TimedWord("has", 2.4, 2.6),
            TimedWord("found", 2.7, 3.0),
            TimedWord("me", 3.1, 3.3),
        ]
        cues = align_lines(lines, words, duration=5.0)
        self.assertEqual([cue["text"] for cue in cues], ["Grace has found me"])
        self.assertEqual(cues[0]["start"], 2.0)
        self.assertEqual(cues[0]["end"], 3.3)

    def test_alignment_fails_when_audio_confirms_no_reference_lines(self):
        with self.assertRaisesRegex(VideoLyricsError, "did not confirm any lines"):
            align_lines(
                ["Document title", "Unrelated notes"],
                [TimedWord("singing", 1.0, 1.4), TimedWord("here", 1.5, 1.8)],
                duration=3.0,
            )

    def test_same_timestamp_audio_lines_merge_without_overlap(self):
        cues = align_lines(
            ["Grace", "Love"],
            [TimedWord("grace", 1.0, 1.3), TimedWord("love", 1.0, 1.4)],
            duration=3.0,
        )
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["text"], "Grace / Love")
        self.assertEqual(cues[0]["start"], 1.0)
        self.assertEqual(cues[0]["end"], 1.4)

    def test_song_transcription_uses_cpu_int8_without_speech_vad(self):
        calls = {}

        class FakeWhisperModel:
            def __init__(self, model, *, device, compute_type):
                calls["init"] = (model, device, compute_type)

            def transcribe(self, audio, **options):
                calls["transcribe"] = (audio, options)
                segment = SimpleNamespace(
                    words=[SimpleNamespace(word=" grace ", start=1.0, end=1.4)]
                )
                return [segment], SimpleNamespace(language="en")

        fake_ctranslate2 = SimpleNamespace(get_cuda_device_count=lambda: 0)
        fake_faster_whisper = SimpleNamespace(WhisperModel=FakeWhisperModel)
        with patch.dict(
            sys.modules,
            {"ctranslate2": fake_ctranslate2, "faster_whisper": fake_faster_whisper},
        ):
            words = transcribe_words("song.wav", model="small", device="auto")

        self.assertEqual(calls["init"], ("small", "cpu", "int8"))
        self.assertFalse(calls["transcribe"][1]["vad_filter"])
        self.assertTrue(calls["transcribe"][1]["word_timestamps"])
        self.assertEqual(words, [TimedWord("grace", 1.0, 1.4)])

    def test_srt_uses_canonical_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timings.srt"
            path.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nwrong words\n\n"
                "2\n00:00:03,000 --> 00:00:04,000\nignored\n",
                encoding="utf-8",
            )
            parsed = parse_timing_file(path)
            cues = apply_canonical_lines(parsed, ["Right one", "Right two"], 5.0)
        self.assertEqual([cue["text"] for cue in cues], ["Right one", "Right two"])


if __name__ == "__main__":
    unittest.main()
