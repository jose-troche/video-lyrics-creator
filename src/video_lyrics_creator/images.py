from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from .envfile import load_project_env
from .errors import VideoLyricsError


def generate_scene_images(
    manifest: dict,
    *,
    provider: str | None = None,
    command_template: str | None = None,
    force: bool = False,
) -> int:
    config = manifest.setdefault("image_generation", {})
    provider = provider or config.get("provider", "codex")
    work_root = Path(manifest["work_dir"]).expanduser().resolve()
    work_dir = work_root / "images"
    work_dir.mkdir(parents=True, exist_ok=True)
    width = int(manifest["video"]["width"])
    height = int(manifest["video"]["height"])
    generated = 0
    codex_executable: str | None = None

    if provider == "openai":
        load_project_env(Path(manifest["work_dir"]).parent)

    for index, scene in enumerate(manifest["scenes"], 1):
        output = work_dir / f"scene-{index:03d}.png"
        prompt = str(scene["prompt"])
        codex_fingerprint = ""
        codex_marker = output.with_suffix(".codex.sha256")
        if provider == "codex":
            codex_fingerprint = _codex_fingerprint(prompt, config, width, height)
            existing_value = str(scene.get("image", "")).strip()
            existing = Path(existing_value).expanduser() if existing_value else None
            existing_marker = existing.with_suffix(".codex.sha256") if existing else None
            if (
                existing is not None
                and existing.is_file()
                and existing_marker is not None
                and existing_marker.is_file()
                and existing_marker.read_text(encoding="utf-8").strip() == codex_fingerprint
                and not force
            ):
                _fit_image(existing, width, height)
                scene["image"] = str(existing.resolve())
                continue
            can_reuse = output.is_file() and codex_marker.is_file() and (
                codex_marker.read_text(encoding="utf-8").strip() == codex_fingerprint
            )
        else:
            can_reuse = output.is_file()
        if can_reuse and not force:
            _fit_image(output, width, height)
            scene["image"] = str(output.resolve())
            continue
        if provider == "codex":
            if codex_executable is None:
                codex_executable = _codex_cli()
                _require_codex_chatgpt_login(codex_executable)
            print(
                f"Generating Codex image {index}/{len(manifest['scenes'])}: {output.name}",
                flush=True,
            )
            _codex_image(
                codex_executable,
                prompt,
                output,
                work_root,
                config,
                width,
                height,
            )
        elif provider == "openai":
            _openai_image(prompt, output, config, width, height)
        elif provider == "command":
            template = command_template or config.get("command")
            if not template:
                raise VideoLyricsError(
                    "The command image provider requires --image-command or image_generation.command"
                )
            _command_image(template, prompt, output, width, height)
        elif provider == "placeholder":
            _placeholder_image(prompt, output, width, height, index)
        else:
            raise VideoLyricsError(f"Unknown image provider: {provider}")
        _fit_image(output, width, height)
        if provider == "codex":
            temporary_marker = codex_marker.with_suffix(codex_marker.suffix + ".tmp")
            temporary_marker.write_text(codex_fingerprint + "\n", encoding="utf-8")
            temporary_marker.replace(codex_marker)
        scene["image"] = str(output.resolve())
        generated += 1
    return generated


def preserve_scene_images_for_replan(
    manifest: dict, previous_scenes: list[dict], scenes: list[dict]
) -> int:
    """Preserve one compatible artwork per old scene while splitting it at lyric boundaries."""
    archive = Path(manifest["work_dir"]).expanduser().resolve() / "images" / "replanned"
    reused_old_indexes: set[int] = set()
    reused = 0
    for scene in scenes:
        start = float(scene["start"])
        match = next(
            (
                (index, old)
                for index, old in enumerate(previous_scenes)
                if float(old.get("start", -1)) <= start < float(old.get("end", -1))
            ),
            None,
        )
        if not match or match[0] in reused_old_indexes:
            continue
        old_index, old = match
        source = Path(str(old.get("image", ""))).expanduser()
        if not source.is_file():
            continue
        archive.mkdir(parents=True, exist_ok=True)
        fingerprint = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:16]
        target = archive / f"scene-{old_index + 1:03d}-{fingerprint}{source.suffix.lower()}"
        shutil.copy2(source, target)
        marker = source.with_suffix(".codex.sha256")
        if marker.is_file():
            shutil.copy2(marker, target.with_suffix(".codex.sha256"))
        scene["prompt"] = str(old.get("prompt") or scene["prompt"])
        scene["image"] = str(target.resolve())
        reused_old_indexes.add(old_index)
        reused += 1
    return reused


def _codex_cli() -> str:
    executable = shutil.which("codex")
    if not executable:
        raise VideoLyricsError(
            "The Codex image provider requires Codex CLI on PATH. Install Codex, run "
            "`codex login`, then retry."
        )
    return executable


def _require_codex_chatgpt_login(executable: str) -> None:
    try:
        result = subprocess.run(
            [executable, "login", "status"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VideoLyricsError(f"Could not check Codex CLI login status: {exc}") from exc
    status = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode or "chatgpt" not in status.casefold():
        raise VideoLyricsError(
            "The Codex image provider requires ChatGPT-managed Codex authentication. Run "
            "`codex logout`, then `codex login` and choose ChatGPT sign-in."
        )


def _codex_fingerprint(prompt: str, config: dict, width: int, height: int) -> str:
    payload = json.dumps(
        {
            "prompt": prompt,
            "width": width,
            "height": height,
            "quality": str(config.get("quality", "medium")),
            "provider": "codex-built-in-imagegen-v1",
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _codex_image(
    executable: str,
    prompt: str,
    output: Path,
    work_root: Path,
    config: dict,
    width: int,
    height: int,
) -> None:
    try:
        timeout = int(config.get("codex_timeout", 900))
    except (TypeError, ValueError) as exc:
        raise VideoLyricsError("image_generation.codex_timeout must be an integer") from exc
    if timeout <= 0:
        raise VideoLyricsError("image_generation.codex_timeout must be positive")
    request = (
        "$imagegen Use the built-in image generation tool through the current ChatGPT/Codex "
        "subscription. Do not use OPENAI_API_KEY, the OpenAI API, or an API fallback script. "
        "Generate exactly one project image from the following production prompt:\n\n"
        f"{prompt}\n\n"
        f"Target frame: {width}x{height} landscape. Requested quality: "
        f"{config.get('quality', 'medium')}. No text or watermark. "
        f"After generation, copy or move the selected final image to exactly: {output.resolve()}\n"
        "Do not modify or create any other project file. Finish only after that exact image file "
        "exists."
    )
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("CODEX_API_KEY", None)
    command = [
        executable,
        "--ask-for-approval",
        "never",
        "exec",
        "--ephemeral",
        "--color",
        "never",
        "-s",
        "workspace-write",
        "-C",
        str(work_root),
        "--skip-git-repo-check",
        request,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoLyricsError(
            f"Codex image generation timed out for {output.name} after {timeout} seconds. "
            "Completed scene images are preserved; rerun without `--force-images` to resume."
        ) from exc
    except OSError as exc:
        raise VideoLyricsError(f"Could not run Codex CLI for {output.name}: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-1500:]
        suffix = f"\n{detail}" if detail else ""
        raise VideoLyricsError(
            f"Codex image generation failed for {output.name}. Completed scene images are "
            f"preserved; rerun without `--force-images` to resume.{suffix}"
        )
    if not output.is_file():
        raise VideoLyricsError(
            f"Codex completed but did not create {output}. Rerun without `--force-images` to "
            "resume after correcting the Codex output issue."
        )


def _openai_image(prompt: str, output: Path, config: dict, width: int, height: int) -> None:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise VideoLyricsError(
            "The OpenAI image provider requires the OpenAI SDK. Install with "
            "`python -m pip install -e '.[openai]'`."
        ) from exc
    client = OpenAI()
    result = client.images.generate(
        model=config.get("model", "gpt-image-2"),
        prompt=prompt,
        size=config.get("size") or _openai_size(width, height),
        quality=config.get("quality", "medium"),
    )
    payload = result.data[0].b64_json
    if not payload:
        raise VideoLyricsError("Image API returned no base64 image data")
    output.write_bytes(base64.b64decode(payload))


def _openai_size(width: int, height: int) -> str:
    """Return a valid GPT Image size that covers the requested video frame."""
    request_width = ((width + 15) // 16) * 16
    request_height = ((height + 15) // 16) * 16
    pixels = request_width * request_height
    ratio = max(request_width, request_height) / min(request_width, request_height)
    if (
        request_width <= 3840
        and request_height <= 3840
        and 655_360 <= pixels <= 8_294_400
        and ratio <= 3
    ):
        return f"{request_width}x{request_height}"
    return "1536x1024" if width >= height else "1024x1536"


def _command_image(template: str, prompt: str, output: Path, width: int, height: int) -> None:
    prompt_file = output.with_suffix(".prompt.txt")
    prompt_file.write_text(prompt + "\n", encoding="utf-8")
    values = {
        "prompt": prompt,
        "prompt_file": str(prompt_file),
        "output": str(output),
        "width": str(width),
        "height": str(height),
    }
    command = [part.format_map(values) for part in shlex.split(template)]
    try:
        subprocess.run(command, check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise VideoLyricsError(f"Image command failed for {output.name}: {exc}") from exc
    if not output.is_file():
        raise VideoLyricsError(f"Image command did not create {output}")


def _placeholder_image(prompt: str, output: Path, width: int, height: int, index: int) -> None:
    image = Image.new("RGB", (width, height), (20 + index * 9 % 80, 24, 44 + index * 13 % 90))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        amount = y / max(1, height - 1)
        color = (
            int(18 + 45 * amount + index * 7 % 35),
            int(20 + 25 * amount),
            int(48 + 80 * amount),
        )
        draw.line((0, y, width, y), fill=color)
    draw.text((40, 40), f"PLACEHOLDER SCENE {index}\n{prompt[:180]}", fill=(255, 255, 255))
    image.save(output)


def _fit_image(path: Path, width: int, height: int) -> None:
    with Image.open(path) as source:
        source = source.convert("RGB")
        scale = max(width / source.width, height / source.height)
        resized = source.resize(
            (round(source.width * scale), round(source.height * scale)), Image.Resampling.LANCZOS
        )
        left = (resized.width - width) // 2
        top = (resized.height - height) // 2
        resized.crop((left, top, left + width, top + height)).save(path, "PNG")
