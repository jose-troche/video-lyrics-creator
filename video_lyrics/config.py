"""The project file: the single source of truth for a video.

Two files, so that starting a new song never overwrites the last one:

  * ``project.yaml`` (or ``.json``) at the top level - a small pointer holding just
    enough to find the rest: schema version, title, and the work directory root.
  * ``<work>/<slugified title>/project.yaml`` - everything else: every setting and
    every stage's results.  All of that song's working files (images, overlays,
    clips, the transcript) live in this same per-song folder.

`Project.data` always holds the full merged contents (so the rest of the codebase
never has to care about the split); `save()` is what writes the two files.  YAML by
default, JSON accepted as well - the suffix decides.  Every pipeline stage reads
the project, does its work, writes its results back and saves, which makes the
whole pipeline resumable: re-running a stage reuses whatever is already on disk
unless `force` is set.

Loading an older, single-file project (before this split existed) migrates it in
place: the existing flat work directory is moved into its own
``<slug>/`` subfolder and the pointer/data files are written out going forward.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .util import VideoLyricsError, ensure_dir, expand, log, slugify

SCHEMA_VERSION = 1
DATA_FILE_STEM = "project"

YAML_SUFFIXES = (".yaml", ".yml")
JSON_SUFFIXES = (".json",)
DEFAULT_PROJECT_NAMES = ("project.yaml", "project.yml", "project.json")
DEFAULT_PROJECT_NAME = DEFAULT_PROJECT_NAMES[0]

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
    "zoom": 1.20,             # Ken Burns zoom factor at a 6s scene; scaled by duration
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


def _yaml():
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise VideoLyricsError(
            "PyYAML is needed for .yaml project files (pip install pyyaml), "
            "or use a project.json file instead."
        ) from exc
    return yaml


def serialize(data: dict[str, Any], suffix: str) -> str:
    if suffix.lower() in YAML_SUFFIXES:
        return _yaml().safe_dump(
            data,
            sort_keys=False,      # keep the readable order the pipeline writes in
            allow_unicode=True,   # curly apostrophes stay curly
            default_flow_style=False,
            width=4096,           # never wrap a prompt across lines
        )
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def deserialize(text: str, suffix: str) -> dict[str, Any]:
    if suffix.lower() in YAML_SUFFIXES:
        data = _yaml().safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise VideoLyricsError("A project file must contain a mapping of settings.")
    return data


def find_project(explicit: str | Path | None = None, base: Path | None = None) -> Path:
    """Resolve the project file to use: the given one, or the first default present."""
    if explicit:
        return Path(explicit)
    base = Path(base) if base else Path.cwd()
    for name in DEFAULT_PROJECT_NAMES:
        candidate = base / name
        if candidate.is_file():
            return candidate
    return base / DEFAULT_PROJECT_NAME


def _merge_defaults(target: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    for key, value in defaults.items():
        target.setdefault(key, value)
    return target


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _check_version(data: dict[str, Any], path: Path) -> None:
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise VideoLyricsError(
            f"{path} has schema_version {version!r}; this build expects {SCHEMA_VERSION}."
        )


def _is_legacy(data: dict[str, Any]) -> bool:
    """A pointer never has an `audio` key; a pre-split project file always does."""
    return "audio" in data


def _relocate_paths(value: Any, old_base: str, new_base: str) -> Any:
    """Rewrite every recorded path under `old_base` to sit under `new_base` instead.

    Earlier stages bake absolute paths into the data (scene images, overlay
    clips, the bed, the transcript) *before* migration moves the files that
    those paths point at, so the strings go stale unless they're moved too.
    """
    if isinstance(value, dict):
        return {key: _relocate_paths(item, old_base, new_base) for key, item in value.items()}
    if isinstance(value, list):
        return [_relocate_paths(item, old_base, new_base) for item in value]
    if isinstance(value, str) and value.startswith(old_base + "/") and not value.startswith(new_base + "/"):
        return new_base + value[len(old_base):]
    return value


def _migrate_legacy(pointer_path: Path, legacy_data: dict[str, Any]) -> Path:
    """Move a pre-split project into <work>/<slug>/ and return the new data path.

    `legacy_data` (the old single file's full contents) is mutated in place and
    is what the caller should treat as the project's data from here on; nothing
    is written yet - the caller's own `save()` does that once, atomically.
    """
    title = legacy_data.get("title") or pointer_path.stem
    slug = slugify(title)
    base = expand(legacy_data.get("work_dir") or (pointer_path.parent / "work"))
    song_dir = base / slug

    if base.is_dir() and base.resolve() != song_dir.resolve():
        ensure_dir(song_dir)
        for entry in sorted(base.iterdir()):
            if entry.resolve() == song_dir.resolve():
                continue
            if entry.is_dir() and (entry / f"{DATA_FILE_STEM}.yaml").is_file():
                continue  # already a migrated song folder - leave other songs alone
            if entry.is_dir() and (entry / f"{DATA_FILE_STEM}.json").is_file():
                continue
            target = song_dir / entry.name
            if target.exists():
                log.warning("Not moving %s - %s already exists.", entry, target)
                continue
            shutil.move(str(entry), str(target))
            log.info("Moved work/%s into %s/", entry.name, song_dir.name)

    # The files just moved, so every path recorded before now (scene images,
    # overlay clips, the bed, the transcript) points at where they used to be.
    old_base, new_base = str(base), str(song_dir)
    for key in list(legacy_data):
        legacy_data[key] = _relocate_paths(legacy_data[key], old_base, new_base)
    legacy_data["work_dir"] = str(base)
    data_path = song_dir / f"{DATA_FILE_STEM}{pointer_path.suffix}"
    log.info(
        "Migrated %s to the new layout: a pointer at %s, and this song's data and "
        "working files under %s.",
        pointer_path, pointer_path, song_dir,
    )
    return data_path


class Project:
    """Wrapper around the project file (project.yaml or project.json)."""

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
        loaded = deserialize(path.read_text(encoding="utf-8"), path.suffix)
        _check_version(loaded, path)

        if _is_legacy(loaded):
            data_path = _migrate_legacy(path, loaded)
            if data_path.resolve() == path.resolve():
                # `path` was itself already sitting at the per-song data location
                # (e.g. opened directly with -p) - nothing to split, just use it.
                return cls(path, loaded)
            project = cls(path, loaded)
            project.save()  # write the new pointer + data files immediately
            return project

        title = loaded.get("title")
        if not title:
            raise VideoLyricsError(f"{path} has no 'title' - is this a project pointer file?")
        base = expand(loaded.get("work_dir") or (path.parent / "work"))
        data_path = base / slugify(title) / f"{DATA_FILE_STEM}{path.suffix}"
        if not data_path.is_file():
            raise VideoLyricsError(
                f"{path} points at {data_path}, which does not exist. If you moved "
                "the project, move its work/<song> folder along with it."
            )
        data = deserialize(data_path.read_text(encoding="utf-8"), data_path.suffix)
        _check_version(data, data_path)
        return cls(path, data)

    def save(self, path: Path | str | None = None) -> Path:
        """Write the project out.

        Saving to the project's own canonical location splits it: a minimal
        pointer at `self.path`, and everything else at `self.data_path` (inside
        its own `work/<slug>/` folder). Saving to any other, explicit path
        instead writes one self-contained file there - used by `convert` and by
        the Resolve handoff job, which both want a single portable snapshot.
        """
        target = Path(path) if path else self.path
        if path is not None and target.resolve() != self.path.resolve():
            _atomic_write(target, serialize(self.data, target.suffix))
            return target

        _atomic_write(self.data_path, serialize(self.data, self.data_path.suffix))
        pointer = {
            "schema_version": SCHEMA_VERSION,
            "title": self.title,
            "work_dir": self.data["work_dir"],
        }
        _atomic_write(self.path, serialize(pointer, self.path.suffix))
        return self.path

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
    def slug(self) -> str:
        return slugify(self.title)

    @property
    def data_path(self) -> Path:
        """Where this song's full project data lives, inside its own work folder."""
        return Path(self.data["work_dir"]) / self.slug / f"{DATA_FILE_STEM}{self.path.suffix}"

    @property
    def work_dir(self) -> Path:
        """This song's own working folder - every intermediate file lives under it."""
        return ensure_dir(Path(self.data["work_dir"]) / self.slug)

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
            f"project file: {self.path}",
            f"song folder : {self.work_dir}",
            f"output      : {self.render_settings['output']}",
            f"duration    : {self.data.get('duration', '-')}",
            f"lyric lines : {len(self.data.get('lyric_lines', []))} in source",
            f"cues        : {len(self.cues)} confirmed by audio",
            f"scenes      : {len(self.scenes)}",
        ]
        return "\n".join(lines)
