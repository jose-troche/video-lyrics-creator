"""Play a stretch of the song out loud, for `video-lyrics tune`.

ffplay cannot be paused or seeked from outside, so every transport action here is
simply a fresh ffplay started at the wanted second and killed when it is no longer
wanted. What makes that good enough is ffplay's own progress line on stderr: it
carries the real audio clock, so the playhead this module reports is the position
actually coming out of the speakers rather than a guess made from the wall clock -
which is the whole point when the job is deciding whether a lyric is 200ms late.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path

from .util import VideoLyricsError, log, which

# "   23.45 M-A:  0.000 fd=   0 aq=  208KB ..." - the leading number is the clock.
STATS = re.compile(rb"\s*(\d+\.\d+)")

STARTUP = 0.25   # ffplay's spin-up, used only until its first progress line lands
SHORTEST = 0.05  # never ask for a segment briefer than this


class Player:
    """The song, and a playhead somewhere in it."""

    def __init__(self, path: Path | str, *, duration: float):
        self.path = Path(path)
        self.duration = float(duration)
        self._program = which("ffplay")

        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._generation = 0            # so a dying reader cannot report over a new process
        self._reported: tuple[int, float] | None = None
        self._anchor = 0.0              # the second this process was told to start at
        self._started = 0.0             # monotonic clock when it was spawned
        self._until: float | None = None
        self._position = 0.0

    # ------------------------------------------------------------- playhead

    def _settle(self) -> float:
        """Refresh the cached playhead, noticing a segment that has played itself out."""
        proc = self._proc
        if proc is not None:
            with self._lock:
                reported = self._reported
            if reported and reported[0] == self._generation:
                clock = reported[1]
            else:
                clock = self._anchor + max(0.0, time.monotonic() - self._started - STARTUP)
            if proc.poll() is None:
                self._position = clock
            else:
                self._position = self._until if self._until is not None else clock
                self._proc = None
            self._position = max(0.0, min(self._position, self.duration))
        return self._position

    @property
    def position(self) -> float:
        return self._settle()

    @property
    def playing(self) -> bool:
        self._settle()
        return self._proc is not None

    @property
    def bounded(self) -> bool:
        """Is the current playback a short preview rather than the song running on?"""
        return self.playing and self._until is not None

    # ------------------------------------------------------------ transport

    def play(self, start: float | None = None, *, until: float | None = None) -> None:
        """Play from `start` (default: where the playhead is), stopping at `until`."""
        self.stop()
        start = self._position if start is None else start
        start = max(0.0, min(float(start), max(0.0, self.duration - SHORTEST)))
        if until is not None:
            until = max(start + SHORTEST, min(float(until), self.duration))

        command = [
            self._program, "-nodisp", "-autoexit", "-hide_banner", "-vn",
            "-loglevel", "error", "-stats", "-ss", f"{start:.3f}",
        ]
        if until is not None:
            command += ["-t", f"{until - start:.3f}"]
        command.append(str(self.path))

        self._generation += 1
        with self._lock:
            self._reported = None
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._anchor = self._position = start
        self._started = time.monotonic()
        self._until = until
        threading.Thread(
            target=self._follow_clock,
            args=(self._proc, self._generation),
            daemon=True,
        ).start()

    def stop(self) -> None:
        """Silence, leaving the playhead where the music got to."""
        self._settle()
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:  # pragma: no cover - ffplay always goes
            log.debug("ffplay did not exit after being killed")

    def toggle(self) -> None:
        if self.playing:
            self.stop()
        else:
            self.play()

    def seek(self, position: float) -> None:
        """Move the playhead, carrying on playing if it already was."""
        position = max(0.0, min(float(position), self.duration))
        if self.playing:
            self.play(position)
        else:
            self.stop()
            self._position = position

    def close(self) -> None:
        self.stop()

    # --------------------------------------------------------------- reader

    def _follow_clock(self, proc: subprocess.Popen, generation: int) -> None:
        """Read ffplay's progress line, one `\\r`-terminated update at a time."""
        stream = proc.stderr
        if stream is None:  # pragma: no cover - stderr is always a pipe here
            return
        line = b""
        while True:
            char = stream.read(1)
            if not char:
                break
            if char in b"\r\n":
                match = STATS.match(line)
                if match:
                    with self._lock:
                        self._reported = (generation, float(match.group(1)))
                line = b""
            elif len(line) < 32:
                line += char
        stream.close()


def available() -> bool:
    """Is there an ffplay to play through?"""
    try:
        which("ffplay")
    except VideoLyricsError:
        return False
    return True
