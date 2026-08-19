from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
import httpx
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
    try:
        from PIL import Image
    except Exception as e:
        logger.warning("[webp] PIL not available: %s", e)
        return

    for root, _, files in os.walk(folder):
        for fn in files:
            if not fn.lower().endswith(".webp"):
                continue
            src = os.path.join(root, fn)
            dst = os.path.splitext(src)[0] + ".jpg"
            try:
                im = Image.open(src).convert("RGB")
                im.save(dst, "JPEG", quality=95)
                os.remove(src)
                logger.info("[webp] converted -> %s", dst)
            except Exception as e:
                logger.warning("[webp] convert failed %s: %s", src, e)


def _command_base(
    parsed_input: ParsedInput,
    settings: Settings,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "gallery_dl",
        "-v",
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

    return command


def _pick_all_downloaded_files(work_dir: Path) -> list[Path]:
    files: list[Path] = []
    for root, _, filenames in os.walk(work_dir):
        for fn in sorted(filenames):
            if fn.endswith(".part") or fn.endswith(".ytdl"):
                continue
            files.append(Path(root) / fn)

    if not files:
        raise RuntimeError("gallery-dl finished but no files found")

    return files


def build_gallerydl_options() -> list[DownloadOption]:
    return [
        DownloadOption(
            option_id="gallerydl_download",
            label="Download Media",
            send_type="video",
            mode="gallerydl",
        )
    ]


async def download_tiktok_api(url: str, work_dir: Path) -> list[DownloadArtifact]:
    """Fallback download TikTok foto & video tanpa terpengaruh WAF 403."""
    api_url = "https://www.tikwm.com/api/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    
    async with httpx.AsyncClient(timeout=30.0, headers=headers, follow_redirects=True) as client:
        res = await client.post(api_url, data={"url": url, "count": 12, "cursor": 0, "web": 1, "hd": 1})
        res_json = res.json()
        data = res_json.get("data", {})

    if not data:
        raise RuntimeError("TikTok API fallback returned empty data")

    artifacts: list[DownloadArtifact] = []
    caption = data.get("title", "")

    # 1. Postingan berupa Slideshow / Carousel Foto
    if "images" in data and data["images"]:
        for idx, img_url in enumerate(data["images"]):
            img_path = work_dir / f"photo_{idx+1:02d}.jpg"
            async with httpx.AsyncClient(follow_redirects=True) as client:
                r = await client.get(img_url)
                img_path.write_bytes(r.content)
            artifacts.append(
                DownloadArtifact(
                    path=img_path,
                    file_name=img_path.name,
                    send_type="photo",
                    caption=caption,
                )
            )

        # Download juga audio background-nya jika ada
        if "music" in data and data["music"]:
            audio_path = work_dir / f"audio_{data.get('id', 'tiktok')}.mp3"
            async with httpx.AsyncClient(follow_redirects=True) as client:
                r = await client.get(data["music"])
                audio_path.write_bytes(r.content)
            artifacts.append(
                DownloadArtifact(
                    path=audio_path,
                    file_name=audio_path.name,
                    send_type="audio",
                    caption=caption,
                )
            )

    # 2. Postingan berupa Video biasa / Live Photo clip
    elif "play" in data or "hdplay" in data:
        video_url = data.get("hdplay") or data.get("play")
        vid_path = work_dir / f"video_{data.get('id', 'tiktok')}.mp4"
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await client.get(video_url)
            vid_path.write_bytes(r.content)
        artifacts.append(
            DownloadArtifact(
                path=vid_path,
                file_name=vid_path.name,
                send_type="video",
                caption=caption,
            )
        )

    return artifacts


async def download_with_gallery_dl(
    parsed_input: ParsedInput,
    settings: Settings,
    work_dir: Path,
) -> list[DownloadArtifact]:
    if not settings.gallery_dl_enabled:
        raise RuntimeError("gallery-dl is disabled")

    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    link = parsed_input.source_url or ""
    if "x.com" in link:
        link = link.replace("x.com", "twitter.com", 1)

    command = _command_base(parsed_input, settings)
    command.extend([
        "-D",
        str(work_dir),
        link,
    ])

    logger.info(
        "Starting gallery-dl download | source=%s work_dir=%s",
        safe_url_label(link),
        work_dir,
    )

    try:
        await _run_command(command, cwd=work_dir)
        _convert_webp_to_jpg(work_dir)
        file_paths = _pick_all_downloaded_files(work_dir)

        artifacts: list[DownloadArtifact] = []
        for file_path in file_paths:
            ext = file_path.suffix.lower()

            if ext in {".mp4", ".mov", ".mkv", ".webm"}:
                send_type = "video"
            elif ext in {".gif"}:
                send_type = "animation"
            elif ext in {".jpg", ".jpeg", ".png"}:
                send_type = "photo"
            elif ext in {".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac"}:
                send_type = "audio"
            else:
                send_type = "document"

            artifacts.append(
                DownloadArtifact(
                    path=file_path,
                    file_name=file_path.name,
                    send_type=send_type,
                    caption=file_path.stem,
                )
            )

        logger.info("gallery-dl complete | total_files=%s", len(artifacts))
        return artifacts

    except Exception as exc:
        is_tiktok = any(h in link.lower() for h in ("tiktok.com", "vt.tiktok.com", "vm.tiktok.com"))
        
        # ⬇️ Fallback otomatis ke API jika gallery-dl gagal/kena 403 pada TikTok
        if is_tiktok:
            logger.warning(
                "gallery-dl failed for TikTok, using TikTok API fallback | url=%s error=%s",
                link,
                exc,
            )
            return await download_tiktok_api(link, work_dir)
        
        raise exc