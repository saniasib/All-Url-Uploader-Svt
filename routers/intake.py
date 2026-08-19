from __future__ import annotations

import logging
from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.types import Message

from config import Settings
from services.cooldown import CooldownManager
from services.parsing import extract_link_text, is_probable_youtube_url, parse_user_input
from services.request_store import RequestStore
from services.ytdlp import (
    build_direct_options,
    build_quick_youtube_options,
    build_ytdlp_options,
    probe_url,
)
from services.gallerydl import build_gallerydl_options
from utils import text
from utils.keyboards import format_keyboard
from utils.logging_config import safe_url_label
from utils.models import StoredRequest

router = Router(name="intake")
logger = logging.getLogger(__name__)


def _is_gallery_site(link: str) -> bool:
    link = (link or "").strip().lower()
    return any(h in link for h in (
        "x.com", "twitter.com", "instagram.com",
        "tiktok.com", "vt.tiktok.com", "vm.tiktok.com"
    ))


def _result_has_any_video(result: dict) -> bool:
    """Cek apakah hasil probe yt-dlp benar-benar mengandung format video."""
    if not isinstance(result, dict):
        return False

    entries = result.get("entries")
    if not entries:
        fmts = result.get("formats") or []
        for f in fmts:
            vcodec = (f.get("vcodec") or "").lower()
            if vcodec and vcodec != "none":
                return True
            if f.get("height") or f.get("video_ext") not in (None, "none"):
                return True
        return False

    for e in entries:
        if not e:
            continue
        fmts = e.get("formats") or []
        for f in fmts:
            vcodec = (f.get("vcodec") or "").lower()
            if vcodec and vcodec != "none":
                return True
        if (e.get("vcodec") or "").lower() not in ("", "none"):
            return True
    return False


@router.message(F.chat.type == "private", F.text)
async def intake_message(
    message: Message,
    settings: Settings,
    cooldown: CooldownManager,
    request_store: RequestStore,
) -> None:
    raw_text = message.text or ""
    if not extract_link_text(raw_text, message.entities):
        return

    if not message.from_user:
        return

    logger.info(
        "Incoming link | user=%s chat=%s source=%s",
        message.from_user.id,
        message.chat.id,
        safe_url_label(parse_user_input(raw_text, message.entities).source_url),
    )

    blocked_seconds = cooldown.check(message.from_user.id, settings.auth_users)
    if blocked_seconds:
        minutes = max(1, round(blocked_seconds / 60))
        logger.info(
            "Cooldown blocked | user=%s remaining=%ss",
            message.from_user.id,
            blocked_seconds,
        )
        await message.answer(text.RATE_LIMIT.format(minutes=minutes))
        return

    parsed = parse_user_input(raw_text, message.entities)
    status_message = await message.reply(text.PROCESSING)

    if is_probable_youtube_url(parsed.source_url):
        token = request_store.create_token()
        stored = StoredRequest(
            token=token,
            request_type="youtube_quick",
            parsed_input=parsed,
            options=build_quick_youtube_options(),
        )
        request_store.save(stored)
        logger.info(
            "Prepared quick YouTube request | user=%s token=%s source=%s options=%s",
            message.from_user.id,
            token,
            safe_url_label(parsed.source_url),
            len(stored.options),
        )
        await status_message.edit_text(
            text.QUICK_CHOICE,
            reply_markup=format_keyboard(token, stored.options),
        )
        return

    try:
        info = await probe_url(parsed, settings)
    except RuntimeError as exc:
        logger.warning(
            "yt-dlp probe failed | user=%s source=%s error=%s",
            message.from_user.id,
            safe_url_label(parsed.source_url),
            exc,
        )
        info = None

    token = request_store.create_token()

    # ✅ Logika penyesuaian fallback jika merupakan Carousel IG/TikTok/X tanpa video
    if info and _is_gallery_site(parsed.source_url) and not _result_has_any_video(info) and settings.gallery_dl_enabled:
        logger.info("Source is gallery/photo only, routing to gallery-dl | source=%s", parsed.source_url)
        options = build_gallerydl_options()
        request_type = "gallerydl_download"
    elif info:
        options = build_ytdlp_options(info)
        request_type = "ytdlp_selection"

        if not options:
            if settings.gallery_dl_enabled and _is_gallery_site(parsed.source_url):
                options = build_gallerydl_options()
                request_type = "gallerydl_download"
            else:
                options = build_direct_options(parsed, info=info)
                request_type = "direct_download"
    else:
        if settings.gallery_dl_enabled and _is_gallery_site(parsed.source_url):
            options = build_gallerydl_options()
            request_type = "gallerydl_download"
        else:
            options = build_direct_options(parsed, info=None)
            request_type = "direct_download"

    stored = StoredRequest(
        token=token,
        request_type=request_type,
        parsed_input=parsed,
        options=options,
        info=info or {},
    )
    request_store.save(stored)
    logger.info(
        "Prepared request | user=%s token=%s type=%s source=%s options=%s title=%s",
        message.from_user.id,
        token,
        request_type,
        safe_url_label(parsed.source_url),
        len(options),
        (info or {}).get("title", "-"),
    )
    await status_message.edit_text(
        text.FORMAT_SELECTION,
        reply_markup=format_keyboard(token, options),
    )