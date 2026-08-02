"""Allow `python -m video_lyrics ...` alongside the `video-lyrics` script."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
