from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Iterable


def load_project_env(project_dir: str | Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if project_dir:
        candidates.append(Path(project_dir).expanduser().resolve() / ".env")
    candidates.append(Path.cwd() / ".env")
    loaded: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if load_env_file(candidate):
            loaded.append(candidate)
    return loaded


def load_env_file(path: str | Path, *, override: bool = False) -> bool:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        return False
    for name, value in parse_env_lines(source.read_text(encoding="utf-8-sig").splitlines()).items():
        if override or name not in os.environ:
            os.environ[name] = value
    return True


def parse_env_lines(lines: Iterable[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, value = line.split("=", 1)
        name = name.strip()
        if not name or not name.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[name] = value
    return values


def update_env_file(path: str | Path, updates: dict[str, str]) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = destination.read_text(encoding="utf-8-sig").splitlines() if destination.exists() else []
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        candidate = stripped[7:].lstrip() if stripped.startswith("export ") else stripped
        if "=" in candidate:
            name = candidate.split("=", 1)[0].strip()
            if name in remaining:
                output.append(f"{name}={remaining.pop(name)}")
                continue
        output.append(line)
    if output and remaining:
        output.append("")
    output.extend(f"{name}={value}" for name, value in remaining.items())

    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    try:
        temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    temporary.replace(destination)
    try:
        destination.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return destination

