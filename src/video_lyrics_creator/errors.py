class VideoLyricsError(RuntimeError):
    """Expected, user-actionable pipeline error."""


class ManifestError(VideoLyricsError):
    """The project manifest is invalid or incomplete."""


class ResolveError(VideoLyricsError):
    """DaVinci Resolve could not perform a requested operation."""

