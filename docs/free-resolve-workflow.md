# DaVinci Resolve 21 Free workflow

## Why two processes are required

Resolve Free does not expose the Studio preference that allows an external Python process to connect to Resolve. It does support scripts launched internally through `Workspace > Scripts`. Video Lyrics Creator uses the external terminal only for media preparation and the internal Resolve script only for timeline and render operations.

On macOS, internal Python scripts also require a Python framework Resolve can embed. A working
`python3` command in Terminal is not proof of that: Apple's `/usr/bin/python3`, Homebrew Python,
and a virtual environment can run the CLI while remaining invisible to Resolve. Install a
universal macOS Python from [python.org](https://www.python.org/downloads/macos/) and confirm this
kind of path exists before installing the launcher:

```bash
ls /Library/Frameworks/Python.framework/Versions/*/Python
```

## Installed files

`video-lyrics install-resolve` uses Resolve’s documented per-user script locations:

| Platform | Scripts root |
| --- | --- |
| macOS | `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts` |
| Windows | `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts` |
| Linux | `~/.local/share/DaVinciResolve/Fusion/Scripts` |

The visible launcher is installed under `Utility`. Its supporting modules are placed under `Modules`, which keeps implementation files out of the Workspace menu.

## Handoff structure

The default macOS layout is:

```text
~/Movies/Video Lyrics Creator/
├── latest-job.json
├── resolve-result.json
├── Jobs/
│   └── song-title/
│       ├── resolve-job.json
│       └── Media/
│           ├── audio.wav
│           ├── Scenes/
│           └── Overlays/
└── Output/
    └── song-title.mp4
```

Windows and Linux use `~/Videos/Video Lyrics Creator` by default. Override the root with `--handoff-dir` if necessary.

`latest-job.json` contains absolute paths to the copied assets. The internal script does not need access to the source repository, virtual environment, API keys, or original media locations.

## Reinstalling after code updates

Run the installer again and restart Resolve:

```bash
video-lyrics install-resolve
```

The installer updates only the Video Lyrics Creator launcher and module files.

## Troubleshooting

### The Workspace script is missing

1. On macOS, run `ls /Library/Frameworks/Python.framework/Versions/*/Python`. If it reports no
   match, install a universal macOS Python from python.org. The system `/usr/bin/python3` does not
   replace this framework.
2. In Resolve, open **Workspace > Console**, select **Py3**, and enter `print(resolve)`. If Resolve
   reports that Python is unavailable, fix the framework installation before continuing.
3. Run `video-lyrics install-resolve --dry-run` and check the reported target.
4. Run `video-lyrics install-resolve` without `--dry-run`.
5. Fully quit Resolve with **DaVinci Resolve > Quit DaVinci Resolve**, then reopen it; closing only
   the project window is insufficient because scripts are scanned at application startup.
6. Look both directly under **Workspace > Scripts** and under its **Utility** category.

Resolve's installed scripting documentation lists the same per-user script directory used by
the installer. If a `.py` file exists there but no Python menu items appear, the host Python
runtime—not the script path—is the usual cause on macOS.

### The latest job has already completed

This is intentional replay protection. Stage a fresh job in Terminal:

```bash
video-lyrics build project.json --render --replace-timeline
```

### Resolve reports that a timeline already exists

The process does not delete a timeline without explicit authorization. Restage with `--replace-timeline`, then run the Workspace script again.

### Media cannot be imported

Use the default Movies/Videos handoff folder. It exists specifically to give Resolve Free a predictable, local media location. Confirm that every path inside `latest-job.json` exists.

### The render failed

Inspect `resolve-result.json` and Resolve’s Console. The result includes the exception and traceback. Correct the issue, restage the job, and launch the Workspace script again. Run `video-lyrics verify project.json` only after Resolve reports completion.
