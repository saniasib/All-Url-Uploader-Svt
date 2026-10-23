from __future__ import annotations

import logging
import os
from datetime import datetime

from aiogram import Bot
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAnimation, InputMediaAudio, Message
from aiogram.utils.chat_action import ChatActionSender

from services.media import audio_duration, video_metadata, video_note_metadata
from utils import text
from utils.models import DownloadArtifact


logger = logging.getLogger(__name__)


def _thumb_file(path: str | None) -> FSInputFile | None:
    if path and os.path.isfile(path):
        return FSInputFile(path)
    return None


async def upload_artifact(
    *,
    bot: Bot,
    status_message: Message,
    source_message: Message,
    artifact: DownloadArtifact,
    thumbnail_path: str | None,
    started_at: datetime,
) -> int:
    await status_message.edit_text(text.upload_caption(artifact.file_name))
    thumb = _thumb_file(thumbnail_path)
    file_input = FSInputFile(artifact.path)
    download_seconds = int((datetime.now() - started_at).total_seconds())
    upload_started = datetime.now()
    logger.info(
        "Upload starting | chat=%s file=%s send_type=%s size=%s thumbnail=%s",
        source_message.chat.id,
        artifact.file_name,
        artifact.send_type,
        artifact.path.stat().st_size if artifact.path.exists() else 0,
        "yes" if thumb else "no",
    )

    if artifact.send_type == "video":
        width, height, duration = video_metadata(artifact.path)
        async with ChatActionSender.upload_video(bot=bot, chat_id=source_message.chat.id):
            reply_msg = await source_message.reply_video(
                video=file_input,
                caption=artifact.caption,
                duration=duration,
                width=width,
                height=height,
                supports_streaming=True,
                thumbnail=thumb,
            )
    elif artifact.send_type == "photo":
        async with ChatActionSender.upload_photo(bot=bot, chat_id=source_message.chat.id):
            reply_msg = await source_message.reply_photo(
                photo=file_input,
                caption=artifact.caption,
            )
    elif artifact.send_type == "animation":
        async with ChatActionSender.upload_video(bot=bot, chat_id=source_message.chat.id):
            reply_msg = await source_message.reply_animation(
                animation=file_input,
                caption=artifact.caption,
                thumbnail=thumb,
            )
    elif artifact.send_type == "audio":
        duration = audio_duration(artifact.path)
        async with ChatActionSender.upload_document(bot=bot, chat_id=source_message.chat.id):
            reply_msg = await source_message.reply_audio(
                audio=file_input,
                caption=artifact.caption,
                duration=duration,
                thumbnail=thumb,
                title=artifact.file_name,
            )
    elif artifact.send_type == "video_note":
        length, duration = video_note_metadata(artifact.path)
        async with ChatActionSender.upload_video_note(
            bot=bot, chat_id=source_message.chat.id
        ):
            reply_msg = await source_message.reply_video_note(
                video_note=file_input,
                duration=duration,
                length=length or 240,
                thumbnail=thumb,
            )
    else:
        async with ChatActionSender.upload_document(bot=bot, chat_id=source_message.chat.id):
            reply_msg = await source_message.reply_document(
                document=file_input,
                caption=artifact.caption,
                thumbnail=thumb,
            )

    upload_seconds = int((datetime.now() - upload_started).total_seconds())
    logger.info(
        "Upload complete | chat=%s file=%s send_type=%s download_seconds=%s upload_seconds=%s",
        source_message.chat.id,
        artifact.file_name,
        artifact.send_type,
        download_seconds,
        upload_seconds,
    )
    await status_message.edit_text(
        text.DONE.format(
            download_seconds=download_seconds,
            upload_seconds=upload_seconds,
        )
    )
    artifact.path.unlink(missing_ok=True)
    return reply_msg.message_id


async def upload_artifacts_batch(
    *,
    bot: Bot,
    status_message: Message,
    source_message: Message,
    artifacts: list[DownloadArtifact],
    thumbnail_paths: list[str | None],
    started_at: datetime,
) -> list[int]:
    """
    Batch upload artifacts using media groups when supported.
    Falls back to individual upload for unsupported combinations.
    Returns list of sent message IDs for forwarding.
    """
    if not artifacts:
        return []

    # Group artifacts by compatible send_types, preserve original thumbnail paths
    photos = []
    videos = []
    others = []

    for art, thumb_path in zip(artifacts, thumbnail_paths):
        thumb = _thumb_file(thumb_path)
        if art.send_type == "photo":
            photos.append((art, thumb, thumb_path))
        elif art.send_type == "video":
            videos.append((art, thumb, thumb_path))
        else:
            others.append((art, thumb, thumb_path))

    upload_started = datetime.now()
    total_uploaded = 0

    # Helper: send a media group batch, return message IDs
    async def send_batch(media_items, chat_action_sender, is_photo=True):
        nonlocal total_uploaded
        if not media_items:
            return []
        message_ids = []
        for i in range(0, len(media_items), 10):
            batch = media_items[i:i+10]
            media = []
            for idx, (art, thumb, _) in enumerate(batch):
                if is_photo:
                    media.append(InputMediaPhoto(
                        media=FSInputFile(art.path),
                        caption=art.caption if idx == 0 and i == 0 else "",
                    ))
                else:
                    width, height, duration = video_metadata(art.path) if art.path.exists() else (0, 0, 0)
                    media.append(InputMediaVideo(
                        media=FSInputFile(art.path),
                        caption=art.caption if idx == 0 and i == 0 else "",
                        duration=duration,
                        width=width,
                        height=height,
                        supports_streaming=True,
                        thumbnail=thumb,
                    ))
            async with chat_action_sender(bot=bot, chat_id=source_message.chat.id):
                sent_messages = await source_message.reply_media_group(media=media)
            message_ids.extend([msg.message_id for msg in sent_messages])
            # Clean up batch
            for art, _, _ in batch:
                art.path.unlink(missing_ok=True)
            total_uploaded += len(batch)
        return message_ids

    # Collect message IDs for forwarding
    message_ids: list[int] = []

    # Process photos in batches of 10 (must be ≥2 items for media group)
    if len(photos) >= 2:
        ids = await send_batch(photos, ChatActionSender.upload_photo, is_photo=True)
        message_ids.extend(ids)
        photos = []  # Mark as sent

    # Process videos in batches of 10 (must be ≥2 items for media group)
    if len(videos) >= 2:
        ids = await send_batch(videos, ChatActionSender.upload_video, is_photo=False)
        message_ids.extend(ids)
        videos = []  # Mark as sent

    # Process remaining (others + singles) individually
    for art, thumb, thumb_path in others + photos + videos:
        msg_id = await upload_artifact(
            bot=bot,
            status_message=status_message,
            source_message=source_message,
            artifact=art,
            thumbnail_path=thumb_path,
            started_at=started_at,
        )
        message_ids.append(msg_id)
        total_uploaded += 1

    # Final DONE message (matching upload_artifact behavior)
    if total_uploaded > 0:
        upload_seconds = int((datetime.now() - upload_started).total_seconds())
        download_seconds = int((datetime.now() - started_at).total_seconds()) - upload_seconds
        await status_message.edit_text(
            text.DONE.format(
                download_seconds=max(0, download_seconds),
                upload_seconds=upload_seconds,
            )
        )
    return message_ids