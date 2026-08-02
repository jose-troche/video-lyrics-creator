"""The project file (project.json): the single source of truth for a video.

Every pipeline stage reads the project, does its work, writes its results back and
saves.  That makes the whole pipeline resumable: re-running a stage reuses whatever
is already on disk unless `force` is set.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .util import VideoLyricsError, ensure_dir, expand, slugify

SCHEMA_VERSION = 1

DEFAULT_AUTHOR = "Jose Troche"
DEFAULT_VISUAL_STYLE = "cinematic photographic realism"

VIDEO_DEFAULTS: dict[str, Any] = {
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "transition": 0.75,       # cross dissolve length between images (s)
    "title_duration": 12.0,   # max title card length (s); trimmed to fit before lyric 1
    "title_fade": 0.75,       # title fade in/out (s)
    "font": "Avenir Next Demi Bold",
    "font_size": 58,
    "margin_v": 72,           # distance from bottom of frame to lyric baseline block (px)
    "zoom": 1.08,             # Ken Burns zoom factor
    "lyric_lead": 0.35,       # show a lyric this early (s)
    "lyric_fade": 0.2,        # lyric fade in/out (s)
}

IMAGE_DEFAULTS: dict[str, Any] = {
    "provider": "codex",      # codex | supplied
    "model": "gpt-image-2",
    "quality": "medium",
    "lines_per_image": 2,
    "source_dir": None,       # used when provider == "supplied"
}

ALIGN_DEFAULTS: dict[str, Any] = {
    "model": "medium.en",     # faster-whisper model id
    "language": "en",
    "vad": False,             # voice activity detection eats sung vocals; keep it off
    "prompt_hint": False,     # priming Whisper with the lyrics invites hallucination
    "min_confidence": 0.5,    # a lyric line needs this share of words heard to become a cue
    "min_matched_words": 2,   # ... and at least this many words actually heard
    "min_duration": 1.0,      # shortest cue (s)
    "max_gap_fill": 0.7,      # hold a cue over gaps shorter than this (s)
    "scene_gap": 2.5,         # a musical gap this long starts a new image
    "interlude": 12.0,        # instrumental stretches longer than this get their own image
}

RENDER_DEFAULTS: dict[str, Any] = {
    "engine": "resolve",      # resolve | ffmpeg
    "output": None,
    "format": "mp4",
    "codec": "H264",
    "audio_codec": "aac",
    "replace_existing": True,
    "motion_backend": "prerender",  # prerender | fusion
    "lyrics_mode": "overlay",       # overlay | subtitle
    "intermediate": "h264",         # h264 | prores
}


def _merge_defaults(target: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    for key, value in defaults.items():
        target.setdefault(key, value)
    return target


class Project:
    """Wrapper around project.json."""

    def __init__(self, path: Path, data: dict[str, Any]):
        self.path = Path(path)
        self.data = data
        self._apply_defaults()

    # ---------------------------------------------------------------- factory

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        audio: str,
        lyrics_source: str,
        title: str | None = None,
        author: str = DEFAULT_AUTHOR,
        visual_style: str = DEFAULT_VISUAL_STYLE,
        work_dir: str | None = None,
        output: str | None = None,
    ) -> "Project":
        audio_path = expand(audio)
        if not audio_path.is_file():
            raise VideoLyricsError(f"Audio file not found: {audio_path}")
        lyrics_path = expand(lyrics_source)
        if not lyrics_path.exists():
            raise VideoLyricsError(f"Lyrics source not found: {lyrics_path}")

        title = title or audio_path.stem
        # Absolute everywhere: Resolve imports media by path, from its own process.
        base = Path(path).expanduser().resolve().parent
        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "title": title,
            "author": author,
            "audio": str(audio_path),
            "lyrics_source": str(lyrics_path),
            "visual_style": visual_style,
            "work_dir": str(expand(work_dir) if work_dir else base / "work"),
        }
        project = cls(Path(path), data)
        project.data["render"]["output"] = str(
            expand(output) if output else base / "output" / f"{slugify(title)}.mp4"
        )
        return project

    @classmethod
    def load(cls, path: Path | str) -> "Project":
        path = Path(path)
        if not path.is_file():
            raise VideoLyricsError(
                f"No project file at {path}. Run `video-lyrics init` first."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise VideoLyricsError(
                f"{path} has schema_version {version!r}; this build expects {SCHEMA_VERSION}."
            )
        return cls(path, data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        tmp.replace(self.path)

    def _apply_defaults(self) -> None:
        self.data.setdefault("schema_version", SCHEMA_VERSION)
        self.data.setdefault("author", DEFAULT_AUTHOR)
        self.data.setdefault("visual_style", DEFAULT_VISUAL_STYLE)
        _merge_defaults(self.data.setdefault("video", {}), VIDEO_DEFAULTS)
        _merge_defaults(self.data.setdefault("image_generation", {}), IMAGE_DEFAULTS)
        _merge_defaults(self.data.setdefault("alignment", {}), ALIGN_DEFAULTS)
        _merge_defaults(self.data.setdefault("render", {}), RENDER_DEFAULTS)

    # ------------------------------------------------------------- accessors

    @property
    def title(self) -> str:
        return self.data["title"]

    @property
    def author(self) -> str:
        return self.data.get("author", DEFAULT_AUTHOR)

    @property
    def audio(self) -> Path:
        return Path(self.data["audio"])

    @property
    def lyrics_source(self) -> Path:
        return Path(self.data["lyrics_source"])

    @property
    def video(self) -> dict[str, Any]:
        return self.data["video"]

    @property
    def alignment(self) -> dict[str, Any]:
        return self.data["alignment"]

    @property
    def image_generation(self) -> dict[str, Any]:
        return self.data["image_generation"]

    @property
    def render_settings(self) -> dict[str, Any]:
        return self.data["render"]

    @property
    def fps(self) -> float:
        return float(self.video["fps"])

    @property
    def size(self) -> tuple[int, int]:
        return int(self.video["width"]), int(self.video["height"])

    @property
    def duration(self) -> float:
        duration = self.data.get("duration")
        if not duration:
            raise VideoLyricsError("Audio duration unknown. Run `video-lyrics lyrics` first.")
        return float(duration)

    @property
    def cues(self) -> list[dict[str, Any]]:
        return self.data.get("lyrics", [])

    @property
    def scenes(self) -> list[dict[str, Any]]:
        return self.data.get("scenes", [])

    @property
    def output(self) -> Path:
        return Path(self.render_settings["output"])

    # ------------------------------------------------------------ work paths

    @property
    def work_dir(self) -> Path:
        return ensure_dir(self.data["work_dir"])

    @property
    def images_dir(self) -> Path:
        return ensure_dir(self.work_dir / "images")

    @property
    def overlays_dir(self) -> Path:
        return ensure_dir(self.work_dir / "overlays")

    @property
    def clips_dir(self) -> Path:
        return ensure_dir(self.work_dir / "clips")

    @property
    def transcript_path(self) -> Path:
        return self.work_dir / "transcript.json"

    @property
    def lyrics_text_path(self) -> Path:
        return self.work_dir / "lyrics.txt"

    @property
    def srt_path(self) -> Path:
        return self.work_dir / "lyrics.srt"

    def describe(self) -> str:
        lines = [
            f"title       : {self.title}",
            f"author      : {self.author}",
            f"audio       : {self.audio}",
            f"lyrics      : {self.lyrics_source}",
            f"work dir    : {self.data['work_dir']}",
            f"output      : {self.render_settings['output']}",
            f"duration    : {self.data.get('duration', '-')}",
            f"lyric lines : {len(self.data.get('lyric_lines', []))} in source",
            f"cues        : {len(self.cues)} confirmed by audio",
            f"scenes      : {len(self.scenes)}",
        ]
        return "\n".join(lines)
