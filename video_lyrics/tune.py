"""`video-lyrics tune`: hear the song and move the lyric lines onto it.

The aligner gets most lines right and some of them slightly wrong, and "slightly
wrong" is exactly the kind of thing that is obvious in a second of listening and
invisible in a column of numbers. So this is a small terminal editor built around
one loop: pick a line, hear it, nudge its edges, hear it again.

`Session` is the cue list and every edit that can be made to it - no curses, no
audio, so the rules about what a cue may do to its neighbours can be tested
directly. Everything below it draws that state and turns keystrokes into calls on
it.
"""

from __future__ import annotations

import copy
import curses
from typing import Any

from .audio import ENVELOPE_RESOLUTION, envelope
from .config import Project
from .player import Player, available
from .util import VideoLyricsError, human_time, log

MIN_CUE = 0.2           # a cue may never be squeezed shorter than this
NEW_CUE = 3.0           # how long a hand-added cue starts out
JOINED = 0.002          # two cues this close count as sharing one boundary
UNDO_DEPTH = 200

STEPS = (0.01, 0.02, 0.05, 0.1, 0.25, 0.5)
TARGETS = ("start", "end", "line")
ZOOMS = (3.0, 6.0, 12.0, 30.0, 60.0)

PREVIEW_LEAD = 0.8      # start a preview this far ahead of the line
PREVIEW_TAIL = 0.4      # ... and let it run this far past the end
EDGE_WINDOW = 1.5       # a preview of just one edge reaches this far either side
SEEK_STEP = 2.0
SEEK_JUMP = 10.0

FRAME = 60              # ms between redraws, so the playhead visibly moves

# Keys that move the playhead. The waveform follows whichever of the playhead and the
# selected line was touched last, so scrubbing scrolls it and picking a line jumps it.
MOVES_PLAYHEAD = frozenset({
    ord(" "), ord("g"), ord("\\"), 10, 13, curses.KEY_ENTER,
    curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_SLEFT, curses.KEY_SRIGHT,
    curses.KEY_HOME,
})
BLOCKS = " ▁▂▃▄▅▆▇█"
HINTS = "?keys  ␣play  ⏎hear  ,.nudge  []set  w save  q quit"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


class Session:
    """The cue list being edited. Every change goes through here, so undo is free."""

    def __init__(self, project: Project):
        self.project = project
        self.duration = float(project.duration)
        self.lines: list[str] = list(project.data.get("lyric_lines") or [])
        self.cues: list[dict[str, Any]] = copy.deepcopy(project.cues)
        self.link = True
        self.step_index = STEPS.index(0.05)
        self._opened = copy.deepcopy(self.cues)   # how the aligner left it
        self._saved = copy.deepcopy(self.cues)    # what is on disk right now
        self._undo: list[list[dict[str, Any]]] = []
        self._redo: list[list[dict[str, Any]]] = []

    # ----------------------------------------------------------------- state

    @property
    def step(self) -> float:
        return STEPS[self.step_index]

    @property
    def dirty(self) -> bool:
        """Are there edits the project file has not been told about?"""
        return self.cues != self._saved

    @property
    def persisted(self) -> bool:
        """Did any edit make it as far as the project file?"""
        return self._saved != self._opened

    def cue_at(self, position: float) -> int | None:
        """Which cue is on screen at `position`, if any."""
        for index, cue in enumerate(self.cues):
            if cue["start"] <= position < cue["end"]:
                return index
        return None

    def unconfirmed(self) -> list[int]:
        """Reference lines the aligner never turned into a cue."""
        used = {cue["line_index"] for cue in self.cues}
        return [index for index in range(len(self.lines)) if index not in used]

    # ----------------------------------------------------------------- edits

    def _joined(self, index: int) -> bool:
        """Does cue `index` end exactly where cue `index + 1` begins?"""
        if not self.link or index < 0 or index + 1 >= len(self.cues):
            return False
        return abs(self.cues[index]["end"] - self.cues[index + 1]["start"]) < JOINED

    def bounds(self, index: int) -> tuple[float, float]:
        """How far cue `index` may reach either way before it would eat a neighbour.

        A neighbour it shares a boundary with is pushed rather than bumped into, so
        the room available then runs all the way to that neighbour's far edge.
        """
        previous = self.cues[index - 1] if index > 0 else None
        following = self.cues[index + 1] if index + 1 < len(self.cues) else None
        low = 0.0
        if previous is not None:
            low = previous["start"] + MIN_CUE if self._joined(index - 1) else previous["end"]
        high = self.duration
        if following is not None:
            high = following["end"] - MIN_CUE if self._joined(index) else following["start"]
        return low, high

    def set_span(self, index: int, start: float, end: float) -> bool:
        """Put cue `index` at [start, end], clamped to the room it has.

        Where the two fight, the start wins and the end yields. Any neighbour sharing
        a boundary with this cue follows it, so joined lines stay joined.
        """
        cue = self.cues[index]
        low, high = self.bounds(index)
        pushes_previous, pushes_following = self._joined(index - 1), self._joined(index)

        start = round(clamp(start, low, high - MIN_CUE), 3)
        end = round(clamp(end, start + MIN_CUE, high), 3)
        if (start, end) == (cue["start"], cue["end"]):
            return False

        self._checkpoint()
        cue["start"], cue["end"], cue["tuned"] = start, end, True
        if pushes_previous:
            self.cues[index - 1]["end"] = start
            self.cues[index - 1]["tuned"] = True
        if pushes_following:
            self.cues[index + 1]["start"] = end
            self.cues[index + 1]["tuned"] = True
        return True

    def nudge(self, index: int, target: str, delta: float) -> bool:
        """Shift a cue's start, its end, or the whole line."""
        cue = self.cues[index]
        if target == "start":
            return self.set_span(index, cue["start"] + delta, cue["end"])
        if target == "end":
            return self.set_span(index, cue["start"], cue["end"] + delta)
        low, high = self.bounds(index)
        delta = clamp(delta, low - cue["start"], high - cue["end"])
        return self.set_span(index, cue["start"] + delta, cue["end"] + delta)

    def set_edge(self, index: int, edge: str, when: float) -> bool:
        """Pin one edge of a cue to a moment in the song."""
        cue = self.cues[index]
        if edge == "end":
            return self.set_span(index, cue["start"], when)
        return self.set_span(index, when, cue["end"])

    def delete(self, index: int) -> bool:
        """Drop a cue - the line stops being shown at all."""
        if not 0 <= index < len(self.cues):
            return False
        self._checkpoint()
        self.cues.pop(index)
        return True

    def insert(self, line_index: int, at: float) -> int | None:
        """Give an unconfirmed reference line a cue, in the free space around `at`."""
        position = len([cue for cue in self.cues if cue["start"] <= at])
        low = self.cues[position - 1]["end"] if position else 0.0
        high = self.cues[position]["start"] if position < len(self.cues) else self.duration
        start = clamp(at, low, high)
        end = min(high, start + NEW_CUE)
        if end - start < MIN_CUE:
            return None
        self._checkpoint()
        self.cues.insert(position, {
            "start": round(start, 3),
            "end": round(end, 3),
            "text": self.lines[line_index],
            "line_index": line_index,
            "alignment_confidence": 0.0,
            "tuned": True,
        })
        return position

    # ------------------------------------------------------- undo and saving

    def _checkpoint(self) -> None:
        self._undo.append(copy.deepcopy(self.cues))
        del self._undo[:-UNDO_DEPTH]
        self._redo.clear()

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(copy.deepcopy(self.cues))
        self.cues = self._undo.pop()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(copy.deepcopy(self.cues))
        self.cues = self._redo.pop()
        return True

    def save(self) -> None:
        self.project.data["lyrics"] = copy.deepcopy(self.cues)
        self.project.save()
        self._saved = copy.deepcopy(self.cues)

    def summary(self) -> str:
        """What was written, in a sentence - printed once the screen is handed back."""
        before = {(cue["line_index"], cue["text"]): (cue["start"], cue["end"])
                  for cue in self._opened}
        after = {(cue["line_index"], cue["text"]): (cue["start"], cue["end"])
                 for cue in self._saved}
        retimed = sum(1 for key, span in after.items()
                      if key in before and before[key] != span)
        parts = [f"{retimed} line{'' if retimed == 1 else 's'} retimed"]
        added = len(set(after) - set(before))
        removed = len(set(before) - set(after))
        if added:
            parts.append(f"{added} added")
        if removed:
            parts.append(f"{removed} removed")
        return ", ".join(parts)


# --------------------------------------------------------------------- screen


class Tuner:
    """Draws the session and turns keystrokes into edits."""

    def __init__(self, session: Session, player: Player, peaks: list[float]):
        self.session = session
        self.player = player
        self.peaks = peaks
        self.selected = 0
        self.scroll = 0
        self.target = 0
        self.zoom = ZOOMS.index(12.0)
        self.follow = True
        self.centred_on_playhead = False
        self.message = "? for the keys"

    # -------------------------------------------------------------- the loop

    def run(self, screen: "curses._CursesWindow") -> None:
        curses.curs_set(0)
        # A timeout rather than nodelay: getch has to be allowed to block briefly, or
        # ncurses cannot gather the several bytes an arrow key arrives as. The wait is
        # also what paces the redraws that keep the playhead moving.
        screen.timeout(FRAME)
        curses.set_escdelay(25)
        _init_colours()
        while True:
            self._track()
            self._draw(screen)
            key = screen.getch()
            if key == -1:
                continue
            if key == curses.KEY_RESIZE:
                screen.clear()
                continue
            if not self._handle(key, screen):
                return

    def _track(self) -> None:
        """While the song runs on, keep the selection on the line being sung."""
        if not (self.follow and self.player.playing) or self.player.bounded:
            return
        index = self.session.cue_at(self.player.position)
        if index is not None:
            self.selected = index

    @property
    def cue(self) -> dict[str, Any] | None:
        cues = self.session.cues
        if not cues:
            return None
        self.selected = max(0, min(self.selected, len(cues) - 1))
        return cues[self.selected]

    # ------------------------------------------------------------------ keys

    def _handle(self, key: int, screen: "curses._CursesWindow") -> bool:
        session, player = self.session, self.player
        cue = self.cue
        self.centred_on_playhead = key in MOVES_PLAYHEAD

        # transport
        if key == ord(" "):
            player.toggle()
        elif key == curses.KEY_LEFT:
            player.seek(player.position - SEEK_STEP)
        elif key == curses.KEY_RIGHT:
            player.seek(player.position + SEEK_STEP)
        elif key == curses.KEY_SLEFT:
            player.seek(player.position - SEEK_JUMP)
        elif key == curses.KEY_SRIGHT:
            player.seek(player.position + SEEK_JUMP)
        elif key == curses.KEY_HOME:
            player.seek(0.0)
        elif key in (curses.KEY_ENTER, 10, 13) and cue:
            player.play(cue["start"] - PREVIEW_LEAD, until=cue["end"] + PREVIEW_TAIL)
        elif key == ord("\\") and cue:
            edge = cue["end"] if TARGETS[self.target] == "end" else cue["start"]
            player.play(edge - EDGE_WINDOW, until=edge + EDGE_WINDOW)
        elif key == ord("g") and cue:
            player.seek(cue["start"])

        # selection
        elif key == curses.KEY_UP:
            self.selected, self.follow = max(0, self.selected - 1), False
        elif key == curses.KEY_DOWN:
            self.selected = min(len(session.cues) - 1, self.selected + 1)
            self.follow = False
        elif key == curses.KEY_PPAGE:
            self.selected, self.follow = max(0, self.selected - 10), False
        elif key == curses.KEY_NPAGE:
            self.selected = min(len(session.cues) - 1, self.selected + 10)
            self.follow = False
        elif key == ord("f"):
            self.follow = not self.follow
            self.message = f"follow {'on' if self.follow else 'off'}"

        # editing
        elif key == ord("\t"):
            self.target = (self.target + 1) % len(TARGETS)
        elif key in (ord(","), ord("."), ord("<"), ord(">")) and cue:
            size = session.step * (5 if key in (ord("<"), ord(">")) else 1)
            delta = -size if key in (ord(","), ord("<")) else size
            self._edited(session.nudge(self.selected, TARGETS[self.target], delta))
        elif key == ord("[") and cue:
            self._edited(session.set_edge(self.selected, "start", player.position))
        elif key == ord("]") and cue:
            self._edited(session.set_edge(self.selected, "end", player.position))
        elif key in (ord("-"), ord("_")):
            session.step_index = max(0, session.step_index - 1)
        elif key in (ord("="), ord("+")):
            session.step_index = min(len(STEPS) - 1, session.step_index + 1)
        elif key == ord("l"):
            session.link = not session.link
            self.message = f"joined edges move together: {'on' if session.link else 'off'}"
        elif key == ord("d") and cue:
            text = cue["text"]
            if session.delete(self.selected):
                self.message = f"removed {text!r} - u to undo"
        elif key == ord("a"):
            self._add(screen)
        elif key == ord("u"):
            self.message = "undone" if session.undo() else "nothing to undo"
        elif key in (ord("y"), 18):  # ^R
            self.message = "redone" if session.redo() else "nothing to redo"

        # view
        elif key == ord("z"):
            self.zoom = min(len(ZOOMS) - 1, self.zoom + 1)
        elif key == ord("Z"):
            self.zoom = max(0, self.zoom - 1)

        # session
        elif key == ord("w"):
            session.save()
            self.message = "saved"
        elif key == ord("?"):
            self._help(screen)
        elif key == ord("q"):
            return self._quit(screen)
        return True

    def _edited(self, changed: bool) -> None:
        self.message = "" if changed else "no room to move that edge"

    def _quit(self, screen: "curses._CursesWindow") -> bool:
        """False closes the editor; True stays in it."""
        if not self.session.dirty:
            return False
        answer = self._ask(screen, " Save before leaving?   y / n / esc ", "yn\x1b")
        if answer == "y":
            self.session.save()
        return answer == "\x1b"

    # ------------------------------------------------------------- modal bits

    def _ask(self, screen: "curses._CursesWindow", prompt: str, accepts: str) -> str:
        height, width = screen.getmaxyx()
        _put(screen, height - 1, 0, prompt.ljust(width - 1), _colour("warn") | curses.A_BOLD)
        screen.refresh()
        screen.timeout(-1)
        try:
            while True:
                key = screen.getch()
                if 0 <= key < 256 and chr(key).lower() in accepts:
                    return chr(key).lower()
        finally:
            screen.timeout(FRAME)

    def _help(self, screen: "curses._CursesWindow") -> None:
        rows = [
            ("space", "play / pause"),
            ("← →", f"seek {SEEK_STEP:g}s     ⇧← ⇧→  seek {SEEK_JUMP:g}s"),
            ("⏎", "play the selected line, with a run-up"),
            ("\\", "play just the edge being edited"),
            ("g", "put the playhead at the line's start"),
            ("↑ ↓", "pick a line          f  follow the song"),
            ("", ""),
            ("tab", "edit the start / the end / the whole line"),
            (", .", "nudge it by one step   < >  by five"),
            ("- =", "smaller / bigger step"),
            ("[ ]", "set start / end to where the playhead is"),
            ("l", "joined edges move together, on or off"),
            ("a d", "add a line the audio never confirmed / remove one"),
            ("u y", "undo / redo"),
            ("", ""),
            ("z Z", "zoom the waveform out / in"),
            ("w", "save into the project file"),
            ("q", "leave"),
        ]
        screen_height, screen_width = screen.getmaxyx()
        window = curses.newwin(
            min(len(rows) + 4, screen_height - 1), min(62, screen_width - 2), 1, 1
        )
        window.bkgd(" ", _colour("panel"))
        window.box()
        _put(window, 1, 3, "keys", _colour("accent") | curses.A_BOLD)
        for row, (keys, what) in enumerate(rows, start=2):
            _put(window, row, 3, f"{keys:>6}", _colour("accent") | curses.A_BOLD)
            _put(window, row, 11, what)
        _put(window, window.getmaxyx()[0] - 2, 3, "any key to go back", curses.A_DIM)
        window.refresh()
        screen.timeout(-1)
        try:
            screen.getch()
        finally:
            screen.timeout(FRAME)
        screen.clear()

    def _add(self, screen: "curses._CursesWindow") -> None:
        """Pick one of the lines the aligner dropped and give it a cue."""
        pending = self.session.unconfirmed()
        if not pending:
            self.message = "every reference line already has a cue"
            return

        choice, top = 0, 0
        height, width = screen.getmaxyx()
        rows = min(len(pending), max(3, height - 10))
        window = curses.newwin(rows + 4, min(width - 4, 78), 2, 2)
        window.bkgd(" ", _colour("panel"))
        screen.timeout(-1)
        try:
            while True:
                window.erase()
                window.box()
                _put(window, 1, 3, f"add a line at {human_time(self.player.position)}",
                     _colour("accent") | curses.A_BOLD)
                top = min(max(top, choice - rows + 1), choice)
                for row in range(rows):
                    if top + row >= len(pending):
                        break
                    line = self.session.lines[pending[top + row]]
                    chosen = top + row == choice
                    _put(window, row + 2, 3, ("▸ " if chosen else "  ") + line,
                         curses.A_REVERSE if chosen else 0, window.getmaxyx()[1] - 4)
                _put(window, rows + 2, 3, "↑↓ pick   ⏎ add   esc cancel", curses.A_DIM)
                window.refresh()
                key = screen.getch()
                if key == curses.KEY_UP:
                    choice = max(0, choice - 1)
                elif key == curses.KEY_DOWN:
                    choice = min(len(pending) - 1, choice + 1)
                elif key in (curses.KEY_ENTER, 10, 13):
                    where = self.session.insert(pending[choice], self.player.position)
                    if where is None:
                        self.message = "no free room at the playhead for another line"
                    else:
                        self.selected, self.follow = where, False
                        self.message = "added - now set its edges"
                    return
                elif key in (27, ord("q")):
                    return
        finally:
            screen.timeout(FRAME)
            screen.clear()

    # ----------------------------------------------------------------- drawing

    def _draw(self, screen: "curses._CursesWindow") -> None:
        height, width = screen.getmaxyx()
        screen.erase()
        if height < 12 or width < 60:
            _put(screen, 0, 0, "The window is too small - make it at least 60x12.")
            screen.refresh()
            return

        wave_height = max(3, min(7, (height - 12) // 2))
        row = self._draw_header(screen, width)
        row = self._draw_wave(screen, row, wave_height, width)
        self._draw_list(screen, row + 1, height - row - 2, width)

        _put(screen, height - 1, 0, " " * (width - 1), _colour("panel"))
        _put(screen, height - 1, max(0, width - len(HINTS) - 2), HINTS, curses.A_DIM)
        _put(screen, height - 1, 1, self.message, _colour("accent") | curses.A_BOLD,
             width - len(HINTS) - 4)
        screen.refresh()

    def _draw_header(self, screen: "curses._CursesWindow", width: int) -> int:
        session, player = self.session, self.player
        title = f" {session.project.title} — {len(session.cues)} lines"
        state = "unsaved changes" if session.dirty else "saved"
        _put(screen, 0, 0, title.ljust(width - 1), _colour("accent") | curses.A_BOLD)
        _put(screen, 0, max(0, width - len(state) - 2), state,
             (_colour("warn") if session.dirty else curses.A_DIM) | curses.A_BOLD)

        transport = "▶ playing" if player.playing else "❚❚ paused"
        bar = (
            f" {human_time(player.position)} / {human_time(session.duration)}  {transport}"
            f"   editing: {TARGETS[self.target]}   step {session.step:g}s"
            f"   joined {'on' if session.link else 'off'}"
            f"   follow {'on' if self.follow else 'off'}"
        )
        _put(screen, 1, 0, bar.ljust(width - 1), curses.A_DIM)
        return 2

    def _window(self) -> tuple[float, float]:
        """The stretch of song the waveform covers, and where it starts."""
        span = ZOOMS[self.zoom]
        cue = self.cue
        if cue is None or self.centred_on_playhead or self.player.playing:
            centre = self.player.position
        else:
            centre = (cue["start"] + cue["end"]) / 2
        return clamp(centre - span / 2, 0.0, max(0.0, self.session.duration - span)), span

    def _draw_wave(
        self, screen: "curses._CursesWindow", top: int, height: int, width: int
    ) -> int:
        left, span = self._window()
        seconds = span / width
        cue = self.cue

        # A bar per column, in eighths of a row, so a whole song's worth of loudness
        # fits in a few lines and a syllable is still a visible bump.
        levels = []
        for column in range(width):
            at = left + column * seconds
            low = int(at * ENVELOPE_RESOLUTION)
            high = max(low + 1, int((at + seconds) * ENVELOPE_RESOLUTION))
            heard = self.peaks[low:high]
            levels.append((max(heard) if heard else 0.0) * height * 8)

        first = last = 0
        if cue is not None:
            first = int(clamp((cue["start"] - left) / seconds, 0, width))
            last = int(clamp((cue["end"] - left) / seconds, 0, width))

        for row in range(height):
            floor = (height - 1 - row) * 8
            bars = "".join(BLOCKS[int(clamp(round(level - floor), 0, 8))] for level in levels)
            for start, stop, attr in (
                (0, first, _colour("wave")),
                (first, last, _colour("selected")),
                (last, width, _colour("wave")),
            ):
                if stop > start:
                    _put(screen, top + row, start, bars[start:stop], attr)

        self._draw_ribbon(screen, top + height, width, left, seconds)
        self._draw_ruler(screen, top + height + 1, width, left, span)
        self._mark(screen, top, height + 2, width, left, seconds)
        return top + height + 2

    def _draw_ribbon(
        self, screen: "curses._CursesWindow", row: int, width: int,
        left: float, seconds: float,
    ) -> None:
        """One bar per cue under the waveform, so gaps and overlaps show up."""
        for index, cue in enumerate(self.session.cues):
            first = int((cue["start"] - left) / seconds)
            last = int((cue["end"] - left) / seconds)
            if last < 0 or first >= width:
                continue
            chosen = index == self.selected
            attr = _colour("selected") | curses.A_BOLD if chosen else _colour("wave")
            for column in range(max(0, first), min(width, max(last, first + 1))):
                _put(screen, row, column, "━", attr)
            if chosen:
                _put(screen, row, first, "▐", _colour("edge") | curses.A_BOLD)
                _put(screen, row, max(first, last - 1), "▌", _colour("edge") | curses.A_BOLD)

    def _draw_ruler(
        self, screen: "curses._CursesWindow", row: int, width: int,
        left: float, span: float,
    ) -> None:
        ticks, step = _ticks(left, span)
        for tick in ticks:
            column = int((tick - left) / span * width)
            label = human_time(tick) if step < 1 else f"{int(tick) // 60}:{int(tick) % 60:02d}"
            if 0 <= column < width - len(label) - 1:
                _put(screen, row, column, f"╵{label}", curses.A_DIM)

    def _mark(
        self, screen: "curses._CursesWindow", top: int, height: int, width: int,
        left: float, seconds: float,
    ) -> None:
        column = int((self.player.position - left) / seconds)
        if not 0 <= column < width:
            return
        for row in range(top, top + height):
            try:
                screen.chgat(row, column, 1, _colour("head") | curses.A_BOLD)
            except curses.error:  # the very last cell of the window
                pass

    def _draw_list(
        self, screen: "curses._CursesWindow", top: int, height: int, width: int
    ) -> None:
        cues = self.session.cues
        rows = max(1, height - 1)
        margin = min(2, rows // 3)          # keep a little context above and below
        self.scroll = min(self.scroll, self.selected - margin)
        self.scroll = max(self.scroll, self.selected - rows + 1 + margin)
        self.scroll = max(0, min(self.scroll, max(0, len(cues) - rows)))

        header = f"{'':6}{'start':>9}{'end':>10}{'dur':>7}{'conf':>7}  line"
        _put(screen, top, 0, header.ljust(width - 1), curses.A_UNDERLINE | curses.A_DIM)

        playing = self.session.cue_at(self.player.position)
        for row in range(rows):
            index = self.scroll + row
            if index >= len(cues):
                break
            cue = cues[index]
            # A cue that starts before the one above it ended will show two lines at
            # once; a plain gap is only worth noticing, not warning about.
            previous = cues[index - 1]["end"] if index else None
            flag = " " if previous is None else (
                "!" if cue["start"] < previous - JOINED else
                "·" if cue["start"] > previous + 0.05 else " ")
            confidence = cue.get("alignment_confidence")
            text = (
                f"{'▸' if index == self.selected else ' '}{flag}{index + 1:>3} "
                f"{'*' if cue.get('tuned') else ' '}{human_time(cue['start']):>8}"
                f"{human_time(cue['end']):>10}"
                f"{cue['end'] - cue['start']:>7.2f}"
                f"{(f'{confidence:.2f}' if confidence else '—'):>7}  {cue['text']}"
            )
            attr = 0
            if index == self.selected:
                attr = curses.A_REVERSE | curses.A_BOLD
            elif index == playing:
                attr = _colour("playing") | curses.A_BOLD
            _put(screen, top + 1 + row, 0, text.ljust(width - 1), attr, width - 1)


# ------------------------------------------------------------------ curses bits

_PAIRS: dict[str, int] = {}


def _init_colours() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    for index, (name, colour) in enumerate((
        ("accent", curses.COLOR_CYAN),
        ("selected", curses.COLOR_YELLOW),
        ("playing", curses.COLOR_GREEN),
        ("wave", curses.COLOR_BLUE),
        ("edge", curses.COLOR_MAGENTA),
        ("head", curses.COLOR_RED),
        ("warn", curses.COLOR_RED),
        ("panel", curses.COLOR_WHITE),
    ), start=1):
        curses.init_pair(index, colour, -1)
        _PAIRS[name] = index


def _colour(name: str) -> int:
    return curses.color_pair(_PAIRS[name]) if name in _PAIRS else 0


def _put(window, row: int, column: int, text: str, attr: int = 0, limit: int | None = None) -> None:
    """addstr that simply gives up rather than raising at the edges of the window."""
    height, width = window.getmaxyx()
    if not 0 <= row < height or column < 0:
        return
    room = width - column - 1
    if limit is not None:
        room = min(room, limit)
    if room <= 0:
        return
    try:
        window.addstr(row, column, text[:room], attr)
    except curses.error:  # pragma: no cover - a resize mid-draw
        pass


def _ticks(left: float, span: float) -> tuple[list[float], float]:
    """Round times to label the ruler with, spaced so the labels cannot collide."""
    step = 120.0
    for candidate in (0.5, 1, 2, 5, 10, 15, 30, 60, 120):
        if span / candidate <= 8:
            step = float(candidate)
            break
    first = int(left / step) * step
    return [first + index * step for index in range(int(span / step) + 2)], step


# ------------------------------------------------------------------ entry point


def tune(project: Project) -> str | None:
    """Run the editor. Returns a note about what changed, or None if nothing did."""
    if not project.cues:
        raise VideoLyricsError("No lyric cues yet. Run `video-lyrics align` first.")
    if not project.audio.is_file():
        raise VideoLyricsError(f"Audio file not found: {project.audio}")
    if not available():
        raise VideoLyricsError(
            "ffplay is needed to hear the song while tuning; it ships with ffmpeg "
            "(brew install ffmpeg)."
        )

    log.info("Reading the waveform…")
    peaks = envelope(project.audio)
    session = Session(project)
    player = Player(project.audio, duration=session.duration)
    try:
        curses.wrapper(Tuner(session, player, peaks).run)
    finally:
        player.close()
    return session.summary() if session.persisted else None
