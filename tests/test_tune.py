"""The editing rules behind `video-lyrics tune`.

Everything here is `tune.Session`: the cue list plus the moves that can be made on
it. Nothing draws and nothing plays, which is the point of keeping that class free
of curses and ffplay.
"""

from __future__ import annotations

import io
import sys
import threading

import pytest

from video_lyrics import tune
from video_lyrics.config import Project


@pytest.fixture
def project(tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"RIFF")
    words = tmp_path / "lyrics.txt"
    words.write_text("one\ntwo\nthree\nnever sung\n", encoding="utf-8")
    project = Project.create(
        tmp_path / "project.yaml",
        audio=str(audio), lyrics_source=str(words), title="Test Song",
        work_dir=str(tmp_path / "work"),
    )
    project.data["duration"] = 60.0
    project.data["lyric_lines"] = ["one", "two", "three", "never sung"]
    project.data["lyrics"] = [
        {"start": 10.0, "end": 14.0, "text": "one", "line_index": 0,
         "alignment_confidence": 1.0},
        {"start": 14.0, "end": 18.0, "text": "two", "line_index": 1,
         "alignment_confidence": 1.0},
        {"start": 25.0, "end": 30.0, "text": "three", "line_index": 2,
         "alignment_confidence": 0.8},
    ]
    project.save()
    return project


def spans(session):
    return [(cue["start"], cue["end"]) for cue in session.cues]


# ------------------------------------------------------------------ nudging


def test_nudging_an_edge_moves_only_that_edge(project):
    session = tune.Session(project)
    assert session.nudge(2, "start", -0.5)
    assert spans(session)[2] == (24.5, 30.0)
    assert session.nudge(2, "end", 0.25)
    assert spans(session)[2] == (24.5, 30.25)


def test_nudging_a_whole_line_keeps_its_length(project):
    session = tune.Session(project)
    assert session.nudge(2, "line", -1.5)
    assert spans(session)[2] == (23.5, 28.5)


def test_an_edited_cue_is_marked_as_tuned(project):
    session = tune.Session(project)
    session.nudge(0, "end", -0.1)
    assert session.cues[0]["tuned"] is True
    assert "tuned" not in session.cues[2]


# ---------------------------------------------------------------- neighbours


def test_linking_is_off_by_default(project):
    assert tune.Session(project).link is False


def test_by_default_a_line_never_moves_its_neighbour(project):
    """Cue 0 ends exactly where cue 1 begins, but pulling its edge back leaves a gap
    rather than dragging cue 1's start along with it."""
    session = tune.Session(project)
    assert session.nudge(0, "end", -0.5)
    assert spans(session)[:2] == [(10.0, 13.5), (14.0, 18.0)]


def test_by_default_a_line_stops_dead_at_its_neighbour_rather_than_overlap(project):
    session = tune.Session(project)
    assert not session.nudge(0, "end", 0.5)   # cue 1 already begins at 14.0
    assert spans(session)[:2] == [(10.0, 14.0), (14.0, 18.0)]


def test_a_line_never_overruns_a_neighbour_it_is_not_touching(project):
    session = tune.Session(project)
    session.nudge(2, "start", -30.0)          # cue 2 starts at 25, cue 1 ends at 18
    assert session.cues[2]["start"] == 18.0
    assert session.cues[1]["end"] == 18.0


def test_turning_link_on_pushes_a_touching_neighbour_along(project):
    """Cue 0 ends exactly where cue 1 begins, so with linking on one edit fixes both."""
    session = tune.Session(project)
    session.link = True
    assert session.nudge(0, "end", 0.5)
    assert spans(session)[:2] == [(10.0, 14.5), (14.5, 18.0)]


def test_a_pushed_neighbour_is_never_swallowed(project):
    session = tune.Session(project)
    session.link = True
    session.nudge(1, "start", -30.0)          # would run back over cue 0 entirely
    assert session.cues[0]["start"] == 10.0
    assert session.cues[0]["end"] - session.cues[0]["start"] == pytest.approx(tune.MIN_CUE)
    assert session.cues[1]["start"] == session.cues[0]["end"]


def test_a_line_stays_inside_the_song(project):
    session = tune.Session(project)
    session.nudge(0, "start", -60.0)
    assert session.cues[0]["start"] == 0.0
    session.nudge(2, "end", 60.0)
    assert session.cues[2]["end"] == 60.0


def test_a_line_is_never_squeezed_below_the_minimum(project):
    session = tune.Session(project)
    session.nudge(2, "end", -30.0)
    assert session.cues[2]["end"] - session.cues[2]["start"] == pytest.approx(tune.MIN_CUE)


# -------------------------------------------------------- setting from audio


def test_an_edge_can_be_pinned_to_the_playhead(project):
    session = tune.Session(project)
    assert session.set_edge(2, "start", 26.4)
    assert spans(session)[2] == (26.4, 30.0)
    assert session.set_edge(2, "end", 28.9)
    assert spans(session)[2] == (26.4, 28.9)


def test_an_impossible_edit_reports_that_it_did_nothing(project):
    session = tune.Session(project)
    assert session.set_edge(0, "start", 0.0)          # room to move
    assert not session.set_edge(0, "start", -5.0)     # already hard against zero


# --------------------------------------------------- adding and removing cues


def test_a_line_the_aligner_dropped_can_be_given_a_cue(project):
    session = tune.Session(project)
    assert session.unconfirmed() == [3]
    where = session.insert(3, 20.0)
    assert where == 2
    assert session.cues[2]["text"] == "never sung"
    assert spans(session)[2] == (20.0, 23.0)
    assert session.unconfirmed() == []


def test_a_new_cue_is_kept_inside_the_gap_it_was_dropped_into(project):
    session = tune.Session(project)
    session.insert(3, 23.5)                     # only 1.5s free before cue "three"
    assert spans(session)[2] == (23.5, 25.0)


def test_there_has_to_be_room_for_a_new_cue(project):
    session = tune.Session(project)
    assert session.insert(3, 12.0) is None      # mid-way through "one"
    assert len(session.cues) == 3


def test_deleting_a_cue_frees_its_line_again(project):
    session = tune.Session(project)
    assert session.delete(1)
    assert [cue["text"] for cue in session.cues] == ["one", "three"]
    assert session.unconfirmed() == [1, 3]


def test_a_line_that_was_never_in_the_lyrics_can_be_added_too(project):
    """Ad-libs, spoken asides - anything the source text never had."""
    session = tune.Session(project)
    where = session.insert_text("Yeah, come on!", 20.0)
    assert where == 2
    assert session.cues[2]["text"] == "Yeah, come on!"
    assert session.cues[2]["line_index"] is None
    assert session.cues[2]["tuned"] is True
    # It isn't a reference line, so it can't show up as one still needing a cue.
    assert session.unconfirmed() == [3]


def test_a_freely_typed_line_still_respects_the_gap_it_is_dropped_into(project):
    session = tune.Session(project)
    assert session.insert_text("Yeah!", 23.5) is not None
    assert spans(session)[2] == (23.5, 25.0)
    assert session.insert_text("no room here", 12.0) is None


def test_blank_or_whitespace_only_text_is_rejected(project):
    session = tune.Session(project)
    assert session.insert_text("   ", 20.0) is None
    assert session.insert_text("", 20.0) is None
    assert len(session.cues) == 3


def test_a_cues_text_can_be_edited(project):
    session = tune.Session(project)
    assert session.set_text(0, "One (edited)")
    assert session.cues[0]["text"] == "One (edited)"
    assert session.cues[0]["tuned"] is True
    # Its slot in the reference lyrics is unaffected - the original line "one"
    # still counts as confirmed, just displayed differently now.
    assert session.unconfirmed() == [3]


def test_editing_a_freely_typed_lines_text_leaves_it_unconfirmed_to_none(project):
    session = tune.Session(project)
    session.insert_text("Yeah!", 20.0)
    assert session.set_text(2, "Yeah, come on!")
    assert session.cues[2]["line_index"] is None


def test_editing_to_the_same_or_blank_text_is_a_no_op(project):
    session = tune.Session(project)
    assert not session.set_text(0, "one")
    assert not session.set_text(0, "   ")
    assert "tuned" not in session.cues[0]


# ------------------------------------------------------- undo, redo and saving


def test_every_edit_can_be_undone_and_redone(project):
    session = tune.Session(project)
    session.nudge(2, "start", -0.5)
    session.nudge(2, "end", 0.5)
    assert spans(session)[2] == (24.5, 30.5)
    assert session.undo() and spans(session)[2] == (24.5, 30.0)
    assert session.undo() and spans(session)[2] == (25.0, 30.0)
    assert not session.undo()
    assert session.redo() and spans(session)[2] == (24.5, 30.0)


def test_an_edit_after_an_undo_drops_the_redo_trail(project):
    session = tune.Session(project)
    session.nudge(2, "start", -0.5)
    session.undo()
    session.nudge(2, "end", 1.0)
    assert not session.redo()


def test_adding_and_editing_text_can_be_undone(project):
    session = tune.Session(project)
    session.insert_text("Yeah!", 20.0)
    session.set_text(0, "One (edited)")
    assert session.undo() and session.cues[0]["text"] == "one"
    assert session.undo() and len(session.cues) == 3


def test_saving_writes_the_cues_back_into_the_project(project):
    session = tune.Session(project)
    assert not session.dirty
    session.nudge(2, "start", -0.5)
    assert session.dirty and not session.persisted

    session.save()
    assert not session.dirty and session.persisted
    assert Project.load(project.path).cues[2]["start"] == 24.5
    assert "1 line retimed" in session.summary()


def test_leaving_without_saving_leaves_the_file_untouched(project):
    session = tune.Session(project)
    session.nudge(2, "start", -0.5)
    session.delete(0)
    assert not session.persisted
    assert Project.load(project.path).cues[2]["start"] == 25.0


def test_the_summary_counts_lines_added_and_removed(project):
    session = tune.Session(project)
    session.insert(3, 20.0)
    session.delete(0)
    session.save()
    assert session.summary() == "0 lines retimed, 1 added, 1 removed"


# --------------------------------------------------------------- the pipeline


def test_realigning_keeps_hand_tuned_cues_unless_forced(project, caplog):
    from video_lyrics import pipeline

    session = tune.Session(project)
    session.nudge(2, "start", -0.5)
    session.save()

    pipeline.stage_align(project)               # no transcript on disk at all
    assert project.cues[2]["start"] == 24.5
    assert "adjusted by hand" in caplog.text


def test_tune_sits_between_align_and_plan_in_the_pipeline():
    from video_lyrics import pipeline

    assert (
        pipeline.STAGES.index("align")
        < pipeline.STAGES.index("tune")
        < pipeline.STAGES.index("plan")
    )


def test_stage_tune_skips_without_asking_unless_asked_for(project, monkeypatch, caplog):
    from video_lyrics import pipeline

    caplog.set_level("INFO")
    asked = []
    monkeypatch.setattr("builtins.input", lambda *a: asked.append(1) or "y")
    pipeline.stage_tune(project)
    assert not asked
    assert "Skipping the fine-tuning prompt" in caplog.text


def test_stage_tune_skips_when_there_are_no_cues_yet(project, monkeypatch):
    from video_lyrics import pipeline

    project.data["lyrics"] = []
    asked = []
    monkeypatch.setattr("builtins.input", lambda *a: asked.append(1) or "y")
    pipeline.stage_tune(project, tune=True)
    assert not asked


def test_stage_tune_skips_the_prompt_outside_a_terminal(project, monkeypatch):
    from video_lyrics import pipeline

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    asked = []
    monkeypatch.setattr("builtins.input", lambda *a: asked.append(1) or "y")
    pipeline.stage_tune(project, tune=True)
    assert not asked


def test_stage_tune_declining_does_not_open_the_editor(project, monkeypatch):
    from video_lyrics import pipeline
    from video_lyrics import tune as tune_mod

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    opened = []
    monkeypatch.setattr(tune_mod, "tune", lambda proj: opened.append(proj))
    pipeline.stage_tune(project, tune=True)
    assert not opened


def test_stage_tune_accepting_opens_the_editor(project, monkeypatch, caplog):
    from video_lyrics import pipeline
    from video_lyrics import tune as tune_mod

    caplog.set_level("INFO")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    opened = []
    monkeypatch.setattr(
        tune_mod, "tune", lambda proj: opened.append(proj) or "1 line retimed"
    )
    pipeline.stage_tune(project, tune=True)
    assert opened == [project]
    assert "Tuning saved: 1 line retimed" in caplog.text


# ------------------------------------------------------------- the transport


def test_the_playhead_is_read_from_ffplays_own_progress_line():
    """ffplay reports the clock coming out of the speakers; that beats guessing."""
    from video_lyrics.player import Player

    player = Player.__new__(Player)          # no ffplay needed to test the parsing
    player._lock = threading.Lock()
    player._reported = None

    class Fake:
        stderr = io.BytesIO(
            b"    nan M-A:    nan fd=   0 aq=    0KB \r"
            b"   12.34 M-A:  0.000 fd=   0 aq=  208KB \r"
            b"   12.37 M-A: -0.000 fd=   0 aq=  208KB \r"
        )

    player._follow_clock(Fake(), generation=7)
    assert player._reported == (7, 12.37)


def test_a_reader_left_over_from_an_earlier_seek_cannot_report_over_a_new_one():
    from video_lyrics.player import Player

    player = Player.__new__(Player)
    player._lock = threading.Lock()
    player._generation = 3
    player._reported = (2, 99.0)             # a stale thread got there first
    player._proc = None
    player._position = 5.0
    assert player.position == 5.0


@pytest.mark.slow
def test_the_waveform_follows_the_shape_of_the_song(tmp_path):
    """A tone that fades in should read as a rising envelope."""
    from video_lyrics import audio as audio_mod
    from video_lyrics.util import run, which

    song = tmp_path / "fade.wav"
    run([which("ffmpeg"), "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=4",
         "-af", "afade=t=in:st=0:d=4", str(song)])

    peaks = audio_mod.envelope(song, resolution=10)
    assert len(peaks) == pytest.approx(40, abs=2)
    assert max(peaks) == 1.0
    assert peaks[2] < peaks[20] < peaks[-2]
