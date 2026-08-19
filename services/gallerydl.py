from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from PIL import Image

from config import Settings
from utils.logging_config import redact_command, safe_url_label
from utils.models import DownloadArtifact, DownloadOption, ParsedInput

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
            error_text.splitlines()[0] if error_text else error_text,
        )

        raise RuntimeError(error_text)

    return stdout_text, stderr_text


def _pick_cookie_file() -> str | None:
    candidates = [
        Path("cookies.txt"),
        Path("/root/All-Url-Uploader/cookies/tiktok.txt"),
    ]

    for cookie in candidates:
        if cookie.exists():
            return str(cookie)

    return None


def _convert_webp_to_jpg(folder: Path) -> None:
    """Mengonversi format WebP ke JPG untuk menghindari error Telegram PHOTO_EXT_INVALID."""
    for root, _, files in os.walk(folder):
        for fn in files:
            if fn.lower().endswith(".webp"):
                src = Path(root) / fn
                dst = src.with_suffix(".jpg")
                try:
                    with Image.open(src) as im:
                        im.convert("RGB").save(dst, "JPEG", quality=95)
                    src.unlink()
                    logger.info("Converted webp to jpg: %s", dst.name)
                except Exception as e:
                    logger.warning("Failed to convert webp %s: %s", src.name, e)


def _command_base(
    parsed_input: ParsedInput,
    settings: Settings,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "gallery_dl",
    ]

    if settings.http_proxy:
        command.extend([
            "--proxy",
            settings.http_proxy,
        ])

    cookie = _pick_cookie_file()

    if cookie:
        command.extend([
            "--cookies",
            cookie,
        ])

    command.extend([
        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "-v",
    ])

    return command


def _pick_downloaded_file(work_dir: Path) -> Path:
    # Hanya abaikan file metadata/text, jangan abaikan gambar (.jpg, .png, dll.)
    files = [
        path
        for path in work_dir.rglob("*")
        if path.is_file()
        and not path.name.endswith(".part")
        and path.suffix.lower() not in {".json", ".txt"}
    ]

    if not files:
        discovered = sorted(
            str(path.relative_to(work_dir))
            for path in work_dir.rglob("*")
            if path.is_file()
        )

        logger.warning(
            "No gallery-dl media file found | work_dir=%s files=%s",
            work_dir,
            discovered,
        )

        raise RuntimeError(
            "gallery-dl finished but no media file was found"
        )

    files.sort(
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )

    return files[0]


def build_gallerydl_options() -> list[DownloadOption]:
    return [
        DownloadOption(
            option_id="gallerydl_download",
            label="Download Media",
            send_type="video",
            mode="gallerydl",
        )
    ]


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

    target_url = parsed_input.source_url
    if target_url and "x.com" in target_url:
        target_url = target_url.replace("x.com", "twitter.com", 1)

    command.extend([
        "-D",
        str(work_dir),
        target_url,
    ])

    logger.info(
        "Starting gallery-dl download | source=%s work_dir=%s",
        safe_url_label(target_url),
        work_dir,
    )

    await _run_command(command, cwd=work_dir)

    # Konversi file .webp ke .jpg jika ada
    _convert_webp_to_jpg(work_dir)

    file_path = _pick_downloaded_file(work_dir)

    # Tentukan tipe pengiriman Telegram secara dinamis
    is_photo = file_path.suffix.lower() in {".jpg", ".jpeg", ".png"}
    send_type = "photo" if is_photo else "video"

    logger.info(
        "gallery-dl download complete | file=%s bytes=%s send_type=%s",
        file_path.name,
        file_path.stat().st_size,
        send_type,
    )

    return DownloadArtifact(
        path=file_path,
        file_name=file_path.name,
        send_type=send_type,
        caption=file_path.stem,
    )