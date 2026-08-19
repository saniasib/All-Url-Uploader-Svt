from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from config import Settings
from utils.logging_config import redact_command, safe_url_label
from utils.models import DownloadArtifact, ParsedInput

logger = logging.getLogger(__name__)


async def _run_command(
    command: list[str],
    cwd: Path | None = None,
) -> tuple[str, str]:
    logger.debug(
        "Running gallery-dl command | cwd=%s command=%s",
        cwd,
        redact_command(command),
    )

    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    stdout_text = stdout.decode(errors="replace").strip()
    stderr_text = stderr.decode(errors="replace").strip()

    if process.returncode != 0:
        error_text = stderr_text or stdout_text or "gallery-dl failed"

        logger.warning(
            "gallery-dl command failed | cwd=%s command=%s error=%s",
            cwd,
            redact_command(command),
            error_text.splitlines()[0],
        )

        raise RuntimeError(error_text)

    return stdout_text, stderr_text


def _command_base(
    parsed_input: ParsedInput,
    settings: Settings,
) -> list[str]:
    command = [
        "gallery-dl",
        "--no-input",
    ]

    if settings.http_proxy:
        command.extend([
            "--proxy",
            settings.http_proxy,
        ])

    if settings.gallery_dl_cookies:
        command.extend([
            "--cookies",
            settings.gallery_dl_cookies,
        ])

    return command


def _pick_downloaded_file(work_dir: Path) -> Path:
    ignored_extensions = {
        ".json",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".txt",
    }

    files = [
        path
        for path in work_dir.rglob("*")
        if path.is_file()
        and not path.name.endswith(".part")
        and path.suffix.lower() not in ignored_extensions
    ]

    if not files:
        raise RuntimeError("gallery-dl did not produce a media file")

    files.sort(
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )

    return files[0]


async def download_with_gallery_dl(
    parsed_input: ParsedInput,
    settings: Settings,
    work_dir: Path,
) -> DownloadArtifact:
    if not settings.gallery_dl_enabled:
        raise RuntimeError("gallery-dl is disabled")

    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    command = _command_base(parsed_input, settings)

    command.extend([
        "--directory",
        str(work_dir),
        parsed_input.source_url,
    ])

    logger.info(
        "Starting gallery-dl download | source=%s work_dir=%s",
        safe_url_label(parsed_input.source_url),
        work_dir,
    )

    await _run_command(command, cwd=work_dir)

    file_path = _pick_downloaded_file(work_dir)

    logger.info(
        "gallery-dl download complete | file=%s bytes=%s",
        file_path.name,
        file_path.stat().st_size,
    )

    return DownloadArtifact(
        path=file_path,
        file_name=file_path.name,
        send_type="video",
        caption=file_path.stem,
    )