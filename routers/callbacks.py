from __future__ import annotations

import logging
import math
from services.progress import format_download_progress, format_duration
from datetime import datetime

from aiogram import Router
from aiogram.types import CallbackQuery
from typing import Callable, Awaitable
from config import Settings
from routers.commands import handle_ui_callback
from services.direct_downloads import download_direct_file
from services.request_store import RequestStore
from services.thumbnail_store import ThumbnailStore
from services.telegram_uploads import upload_artifact, upload_artifacts_batch
from services.ytdlp import download_quick_youtube, download_selected_format
from services.gallerydl import download_with_gallery_dl
from utils import text
from utils.callbacks import RequestCallback, UiCallback
from utils.logging_config import safe_url_label
from utils.models import DownloadArtifact, DownloadOption, StoredRequest

async def _update_progress(query: CallbackQuery, data: dict) -> None:
    file_name = data.get("file_name", "downloading...")
    started_at = data.get("started_at", datetime.now().timestamp())
    
    # Handle different callback formats:
    # 1. yt-dlp: sends percentage + total (as string like "1.23 MiB")
    # 2. gallery-dl: sends only percentage
    # 3. direct: sends downloaded (bytes) + total (bytes)
    
    downloaded = 0
    total = 0
    percentage = data.get("percentage")
    
    if "downloaded" in data:
        # Direct download or other: raw byte values
        downloaded = int(data["downloaded"]) if data["downloaded"] else 0
        total = int(data["total"]) if data.get("total") else 0
    elif "total" in data:
        # yt-dlp: total as string, percentage provided
        total = _parse_bytes(data["total"])
        if percentage:
            downloaded = int(total * float(percentage) / 100)
        else:
            downloaded = 0
    elif percentage is not None:
        # gallery-dl: only percentage, no size info
        percentage = float(percentage)
        total = 0
        downloaded = 0
    else:
        total = 0
        downloaded = 0
    
    await query.message.edit_text(
        format_download_progress(
            file_name=file_name,
            downloaded=downloaded,
            total=total,
            started_at=started_at,
            percentage=percentage,
        )
    )


def _parse_bytes(size_str: str) -> int:
    """Parse '50.5 MiB' style strings to bytes."""
    if not size_str:
        return 0
    size_str = size_str.strip().upper()
    units = {"B": 1, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4,
             "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}
    for unit, multiplier in units.items():
        if size_str.endswith(unit):
            try:
                return int(float(size_str[:-len(unit)].strip()) * multiplier)
            except (ValueError, IndexError):
                return 0
    # No unit suffix, assume bytes
    try:
        return int(float(size_str))
    except ValueError:
        return 0

router = Router(name="callbacks")
logger = logging.getLogger(__name__)


def _find_option(stored: StoredRequest, action: str) -> DownloadOption | None:
    for option in stored.options:
        if option.option_id == action:
            return option
    return None


@router.callback_query(UiCallback.filter())
async def ui_callback(query: CallbackQuery, callback_data: UiCallback) -> None:
    await handle_ui_callback(query, callback_data.action)


@router.callback_query(RequestCallback.filter())
async def request_callback(
    query: CallbackQuery,
    callback_data: RequestCallback,
    settings: Settings,
    request_store: RequestStore,
    thumbnail_store: ThumbnailStore,
) -> None:
    if not query.message:
        return

    # Jawab query di awal agar tidak timeout (query is too old)
    try:
        await query.answer()
    except Exception:
        pass

    stored = request_store.load(callback_data.token)
    if not stored:
        logger.warning(
            "Expired request callback | user=%s token=%s action=%s",
            query.from_user.id,
            callback_data.token,
            callback_data.action,
        )
        await query.message.edit_text(text.REQUEST_EXPIRED)
        return

    option = _find_option(stored, callback_data.action)
    if not option:
        logger.warning(
            "Unknown request option | user=%s token=%s action=%s",
            query.from_user.id,
            callback_data.token,
            callback_data.action,
        )
        await query.message.edit_text(text.REQUEST_EXPIRED)
        return

    started_at = datetime.now()
    started_ts = started_at.timestamp()
    work_dir = request_store.work_directory(stored.token)

    async def progress_cb(data: dict) -> None:
        data["started_at"] = started_ts
        if "file_name" not in data:
            data["file_name"] = file_name
        await _update_progress(query, data)

    file_name = stored.parsed_input.custom_file_name or "downloaded-file"
    await query.message.edit_text(text.download_caption(file_name))
    logger.info(
        "Starting request action | user=%s token=%s type=%s option=%s send_type=%s source=%s",
        query.from_user.id,
        stored.token,
        stored.request_type,
        option.option_id,
        option.send_type,
        safe_url_label(stored.parsed_input.source_url),
    )

    error_occurred = False
    try:
        # Menampung artefak yang akan diupload (bisa 1 file atau banyak file)
        artifacts_to_upload: list[DownloadArtifact] = []

        if stored.request_type == "gallerydl_download":
            artifacts = await download_with_gallery_dl(
                parsed_input=stored.parsed_input,
                settings=settings,
                work_dir=work_dir,
                progress_callback=progress_cb,
            )
            # Normalisasi jika gallery_dl mengembalikan list atau single object
            if isinstance(artifacts, list):
                artifacts_to_upload.extend(artifacts)
            else:
                artifacts_to_upload.append(artifacts)

        elif stored.request_type == "youtube_quick":
            artifact = await download_quick_youtube(
                parsed_input=stored.parsed_input,
                option=option,
                settings=settings,
                work_dir=work_dir,
                progress_callback=progress_cb,
            )
            artifacts_to_upload.append(artifact)

        elif stored.request_type == "direct_download":
            artifact = await download_direct_file(
                status_message=query.message,
                parsed_input=stored.parsed_input,
                option=option,
                settings=settings,
                work_dir=work_dir,
                suggested_ext=stored.info.get("ext"),
            )
            artifacts_to_upload.append(artifact)

        else:
            try:
                artifact = await download_selected_format(
                    parsed_input=stored.parsed_input,
                    option=option,
                    info=stored.info,
                    settings=settings,
                    work_dir=work_dir,
                    progress_callback=progress_cb,
                )
                artifacts_to_upload.append(artifact)
            except Exception as e:
                err_lower = str(e).lower()
                is_extractor_issue = any(
                    k in err_lower for k in (
                        "403",
                        "unexpected response",
                        "unable to extract",
                        "no video formats found",
                        "no formats",
                        "redirect to login",
                    )
                )

                if is_extractor_issue and settings.gallery_dl_enabled:
                    logger.warning(
                        "yt-dlp extractor failed, falling back to gallery-dl | url=%s error=%s",
                        stored.parsed_input.source_url,
                        e,
                    )
                    arts = await download_with_gallery_dl(
                        parsed_input=stored.parsed_input,
                        settings=settings,
                        work_dir=work_dir,
                        progress_callback=progress_cb,
                    )
                    if isinstance(arts, list):
                        artifacts_to_upload.extend(arts)
                    else:
                        artifacts_to_upload.append(arts)
                else:
                    raise e

        # Upload artifacts: batch if multiple, else single
        forwarded_ids: list[int] = []
        if len(artifacts_to_upload) > 1:
            thumb_path = thumbnail_store.get(query.from_user.id)
            thumb_list = [thumb_path] * len(artifacts_to_upload)
            forwarded_ids = await upload_artifacts_batch(
                bot=query.bot,
                status_message=query.message,
                source_message=query.message.reply_to_message or query.message,
                artifacts=artifacts_to_upload,
                thumbnail_paths=thumb_list,
                started_at=started_at,
            )
            for art in artifacts_to_upload:
                logger.info(
                    "Completed upload for file | user=%s token=%s file=%s send_type=%s",
                    query.from_user.id,
                    stored.token,
                    art.file_name,
                    art.send_type,
                )
        else:
            for art in artifacts_to_upload:
                msg_id = await upload_artifact(
                    bot=query.bot,
                    status_message=query.message,
                    source_message=query.message.reply_to_message or query.message,
                    artifact=art,
                    thumbnail_path=thumbnail_store.get(query.from_user.id),
                    started_at=started_at,
                )
                forwarded_ids.append(msg_id)
                logger.info(
                    "Completed upload for file | user=%s token=%s file=%s send_type=%s",
                    query.from_user.id,
                    stored.token,
                    art.file_name,
                    art.send_type,
                )

        # Forward all uploaded media to channel after all uploads complete
        for msg_id in forwarded_ids:
            try:
                await query.bot.forward_message(
                    chat_id=-1004395216472,
                    from_chat_id=query.message.chat.id,
                    message_id=msg_id,
                )
            except Exception:
                pass

    except Exception as exc:  # pylint: disable=broad-exception-caught
        error_occurred = True
        err_lower = str(exc).lower()
        # Map specific error patterns to user‑friendly messages
        if "tiktok api fallback returned empty data" in err_lower:
            err_msg = "TikTok blocked the request. Try again later or use another link."
        elif "403" in err_lower:
            err_msg = "TikTok returned 403 Forbidden – the link may be restricted or need cookies."
        else:
            err_msg = text.DOWNLOAD_FAILED

        logger.exception(
            "Request action failed | user=%s token=%s type=%s option=%s",
            query.from_user.id,
            stored.token,
            stored.request_type,
            option.option_id,
        )
        await query.message.edit_text(err_msg)
    finally:
        request_store.delete(stored.token)
        logger.info("Cleaned request state | token=%s", stored.token)
        # Only delete the message if no error occurred, so the user can see the error
        if not error_occurred:
            try:
                await query.message.delete()
            except Exception:
                pass