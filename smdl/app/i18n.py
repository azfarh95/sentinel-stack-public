"""Lightweight i18n for SMDL.

Two-language design (en, ru). String catalog lives inline — small enough
that a separate .po toolchain isn't worth the dependency. Per-chat
preference persisted to /data/lang.json.

Usage:
    from .i18n import t, get_lang, set_lang, SUPPORTED_LANGS
    msg = t("download_failed", get_lang(chat_id), error=str(e))

Missing-key fallback: returns the key itself (so untranslated strings
are visible in logs/UI rather than crashing).
Missing-language fallback: falls back to English.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

LANG_FILE = Path(os.environ.get("LANG_FILE", "/data/lang.json"))
TZ_FILE   = Path(os.environ.get("TZ_FILE",   "/data/tz.json"))
TRANSCODE_FILE = Path(os.environ.get("TRANSCODE_FILE", "/data/transcode.json"))
VIDEO_QUALITY_FILE = Path(os.environ.get("VIDEO_QUALITY_FILE", "/data/video_quality.json"))
SUPPORTED_LANGS = ("en", "ru")
DEFAULT_LANG = "en"
DEFAULT_TZ_OFFSET = 0.0  # UTC. Range: -12 to +14 (real-world UTC offsets).

LANG_LABELS = {
    "en": "English",
    "ru": "Русский",
}

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        # Generic
        "owner_only":           "Owner only.",
        "error_generic":        "Error: {error}",

        # /language
        "lang_picker":          "Choose your language:",
        "lang_set_en":          "Language set to English.",
        "lang_set_ru":          "Language set to Russian.",
        "lang_unknown":         "Unknown language: {lang}. Supported: {supported}",

        # URL identification
        "identifying":          "Identifying {platform} post...",
        "identify_failed":      "Failed to identify post: {error}",
        "private_account":      "Private account — cannot download.",
        "could_not_identify":   "Could not identify post: {error}",

        # Live recording — manual
        "live_disabled":        "{platform} · @{uploader} · 🔴 LIVE\nLive recording is disabled in config (live_enabled=false).",
        "live_site_unsupported": "{platform} · 🔴 LIVE\n⚠ Site not supported / not configured yet (yt-dlp can't extract a live stream from this URL after {budget} attempts).\n\nIf you think this should work, the site may need a yt-dlp extractor update or cookies.",
        "live_started":         "{platform} · @{uploader} · 🔴 LIVE\nRecording started — heartbeats every 5 min. Will auto-stop on stream end or session failure.",
        "live_progress":        "🔴 Recording · @{uploader}\n⏱ {duration} · 💾 {mb:.1f} MB",
        "live_ended_natural":   "✓ Recording ended naturally · {mins} min · {mb:.0f} MB",
        "live_user_stopped":    "⏹ Stopped by /stop_livestream · {mins} min · {mb:.0f} MB saved",
        "live_session_fail":    "⚠ Session/auth failed at {mins} min · {mb:.0f} MB saved\nCookie likely expired — refresh cookies and retry.",
        "live_no_extractor_final": "⚠ Site not supported / not configured yet.\nyt-dlp couldn't extract a live stream from this URL after {attempts} attempts.",
        "live_no_extractor_retry": "⚠ yt-dlp couldn't extract this URL ({attempts}/{budget} attempts)\nTry again — {remaining} more attempts before site is marked as not configured.",
        "live_platform_not_allowed": "⚠ {detail}",
        "live_disk_low":        "⚠ {detail}",
        "live_other_abort":     "⚠ Stopped: {reason} · {mins} min · {mb:.0f} MB · {detail}",
        "live_mouflon_blocked": "🛡 Stripchat anti-recording (Mouflon) blocked this stream. Only a short ad was served. The room is gated against bot recordings.",

        # /stop_livestream + /live_status
        "no_active_live":       "No active livestream recording in this chat.",
        "no_active_live_short": "No active livestream recording.",
        "stop_requested":       "⏹ Stop requested for {platform} · @{uploader} ({duration} in). Finalizing the file…",
        "live_status_active":   "🔴 Recording · {platform} · @{uploader}\n⏱ {duration} · use /stop_livestream to halt",

        # Normal download
        "downloading":          "{platform} · @{uploader} · {media_label}\nDownloading...",
        "download_failed":      "Download failed: {error}",
        "sending_files":        "{prefix}ending {count} files...",
        "sending_one":          "{prefix}ending {platform} {media_label}...",
        "sent_short":           "Sent ({detail})",
        "uploading_telethon":   "📤 Uploading {size_mb} MB via user account…",
        "uploaded_telethon":    "✓ Uploaded ({size_mb} MB)",
        "send_failed":          "Send failed: {error}",

        # Delivery links
        "file_ready":           "📁 File ready · {size_mb:.0f} MB",
        "tailnet_link":         "🔒 Tailnet (you, on mesh):\n{url}",
        "share_link":           "🌍 Share link (anyone, expires in {hours}h):\n{url}",
        "no_delivery":          "⚠ No delivery method configured. File is at /downloads/{rel}",

        # /watch /unwatch /watchlist
        "watch_usage":          "Usage: /watch <url>\nExample: /watch https://twitch.tv/some_streamer",
        "unwatch_usage":        "Usage: /unwatch <url>",
        "watch_already":        "Already watching {url}",
        "watch_added":          "Now watching {url}",
        "watch_not_found":      "Not in watchlist: {url}",
        "watch_removed":        "Removed {url}",
        "watchlist_empty":      "Watchlist is empty.\nAdd one with: /watch <url>",
        "watchlist_header":     "📺 Watchlist ({count})",

        # Stream monitor live notification — one-liner pattern so the
        # snooze/skip/rec follow-ups can replace the whole bubble instead of
        # appending to a verbose prompt.
        "monitor_live_prompt":  "🔴 {platform} — {uploader} is online. Record stream?",
        "btn_yes_record":       "✅ Yes — Record",
        "btn_skip":             "❌ Skip",
        "btn_lang_en":          "English",
        "btn_lang_ru":          "Русский",
        "monitor_skipped":      "{uploader} — ⏭ Skipped",
        "monitor_starting":     "{uploader} — 🎬 Recording starting…",
        "monitor_record_starting": "🔴 Recording · @{uploader}\nStarting…",
        "monitor_recording_crashed": "⚠ Recording crashed: {error}",
        "btn_snooze_1h":        "💤 1h",
        "btn_snooze_8h":        "😴 8h",
        "monitor_snoozed":      "{uploader} — 💤 Snoozed for {duration} (until {until})",

        # /start handshake — pending-code expiry edit
        "access_code_expired": "⏱ One-time access code expired.\nSend /regenerate\\_token for a fresh one.",

        # /timezone
        "tz_current":           "Current timezone: {tz}\n\nChange with: /timezone <offset>",
        "tz_set":               "Timezone set to {tz}.",
        "tz_invalid":           "Invalid offset: {value}. Range: -12 to +14.",
        "tz_usage":             "Usage: /timezone <offset>\n\nExamples:\n  /timezone 8     → UTC+8 (Singapore)\n  /timezone -5    → UTC-5 (New York)\n  /timezone 5.5   → UTC+5:30 (India)",

        # /transcode
        "transcode_picker":     "Pick a post-recording transcode option:\n\nCurrent: {current}",
        "transcode_set":        "Transcode set: {summary}",
        "transcode_off":        "Off — no transcode",
        "transcode_replace":    "{height}p (replaces original)",
        "transcode_keep":       "{height}p (keeps original as archive)",
        "btn_transcode_off":    "❌ Off",
        "btn_transcode_480_r":  "🔻 480p · replace",
        "btn_transcode_240_r":  "🔻 240p · replace",
        "btn_transcode_480_k":  "📦 480p + keep archive",
        "btn_transcode_240_k":  "📦 240p + keep archive",

        # /default_video_size — quality for non-live downloads (TikTok, IG, etc.)
        "vq_picker":            "Pick default video quality for non-live downloads:\n\nCurrent: {current}",
        "vq_set":               "Default video size set to {value}.",
        "vq_label_best":        "Best available (1080p+ if present)",
        "vq_label_height":      "{height}p",
        "btn_vq_best":          "🥇 Best",
        "btn_vq_1080":          "📺 1080p",
        "btn_vq_720":           "🖥 720p",
        "btn_vq_360":           "📱 360p",

        # /storage_stats + /clear_cache
        "storage_stats":        "📊 Storage\n\nFree on /downloads: {free_gb:.1f} GB / {total_gb:.0f} GB total\nDownloads: {downloads_count} files, {downloads_size}\nLive recordings: {live_count} files, {live_size}\n\n💾 URL cache: {cache_count} entries\nOldest: {cache_oldest}\nNewest: {cache_newest}",
        "cache_cleared":        "🧹 Cache cleared. Removed {count} entr{plural}.",
        "cache_clear_usage":    "Usage: /clear_cache       — clear everything\n       /clear_cache <url> — clear one URL",
        "cache_url_not_found":  "ℹ URL not in cache: {url}",
    },
    "ru": {
        # Generic
        "owner_only":           "Только для владельца.",
        "error_generic":        "Ошибка: {error}",

        # /language
        "lang_picker":          "Выберите язык:",
        "lang_set_en":          "Язык изменён на английский.",
        "lang_set_ru":          "Язык изменён на русский.",
        "lang_unknown":         "Неизвестный язык: {lang}. Поддерживаются: {supported}",

        # URL identification
        "identifying":          "Анализирую публикацию {platform}...",
        "identify_failed":      "Не удалось проанализировать публикацию: {error}",
        "private_account":      "Закрытый аккаунт — скачивание невозможно.",
        "could_not_identify":   "Не удалось распознать публикацию: {error}",

        # Live recording — manual
        "live_disabled":        "{platform} · @{uploader} · 🔴 ЭФИР\nЗапись эфиров отключена в настройках (live_enabled=false).",
        "live_site_unsupported": "{platform} · 🔴 ЭФИР\n⚠ Сайт не поддерживается / ещё не настроен (yt-dlp не смог извлечь поток после {budget} попыток).\n\nЕсли это должно работать — возможно, нужно обновить yt-dlp или cookies.",
        "live_started":         "{platform} · @{uploader} · 🔴 ЭФИР\nЗапись началась — обновления каждые 5 минут. Автоматически остановится при завершении эфира или ошибке сессии.",
        "live_progress":        "🔴 Запись · @{uploader}\n⏱ {duration} · 💾 {mb:.1f} МБ",
        "live_ended_natural":   "✓ Запись завершилась естественно · {mins} мин · {mb:.0f} МБ",
        "live_user_stopped":    "⏹ Остановлено через /stop_livestream · {mins} мин · {mb:.0f} МБ сохранено",
        "live_session_fail":    "⚠ Ошибка сессии/авторизации на {mins} мин · {mb:.0f} МБ сохранено\nВероятно, истёк срок cookie — обновите cookie и повторите.",
        "live_no_extractor_final": "⚠ Сайт не поддерживается / ещё не настроен.\nyt-dlp не смог извлечь эфир после {attempts} попыток.",
        "live_no_extractor_retry": "⚠ yt-dlp не смог извлечь эту ссылку ({attempts}/{budget} попыток)\nПопробуйте снова — осталось {remaining} попыток.",
        "live_platform_not_allowed": "⚠ {detail}",
        "live_disk_low":        "⚠ {detail}",
        "live_other_abort":     "⚠ Остановлено: {reason} · {mins} мин · {mb:.0f} МБ · {detail}",
        "live_mouflon_blocked": "🛡 Защита Stripchat (Mouflon) заблокировала стрим. Сервер выдал короткую рекламу вместо эфира. Эта комната закрыта для бот-записей.",

        # /stop_livestream + /live_status
        "no_active_live":       "В этом чате нет активной записи эфира.",
        "no_active_live_short": "Нет активной записи эфира.",
        "stop_requested":       "⏹ Запрошена остановка {platform} · @{uploader} ({duration} в эфире). Завершаю файл…",
        "live_status_active":   "🔴 Запись · {platform} · @{uploader}\n⏱ {duration} · /stop_livestream чтобы остановить",

        # Normal download
        "downloading":          "{platform} · @{uploader} · {media_label}\nСкачиваю...",
        "download_failed":      "Ошибка скачивания: {error}",
        "sending_files":        "{prefix}тправляю {count} файлов...",
        "sending_one":          "{prefix}тправляю {platform} {media_label}...",
        "sent_short":           "Отправлено ({detail})",
        "uploading_telethon":   "📤 Загружаю {size_mb} МБ через пользовательский аккаунт…",
        "uploaded_telethon":    "✓ Загружено ({size_mb} МБ)",
        "send_failed":          "Ошибка отправки: {error}",

        # Delivery links
        "file_ready":           "📁 Файл готов · {size_mb:.0f} МБ",
        "tailnet_link":         "🔒 Tailnet (вы, в сети):\n{url}",
        "share_link":           "🌍 Общая ссылка (для всех, истекает через {hours}ч):\n{url}",
        "no_delivery":          "⚠ Способ доставки не настроен. Файл на /downloads/{rel}",

        # /watch /unwatch /watchlist
        "watch_usage":          "Использование: /watch <url>\nПример: /watch https://twitch.tv/some_streamer",
        "unwatch_usage":        "Использование: /unwatch <url>",
        "watch_already":        "Уже отслеживается: {url}",
        "watch_added":          "Теперь отслеживается: {url}",
        "watch_not_found":      "Нет в списке отслеживания: {url}",
        "watch_removed":        "Удалено: {url}",
        "watchlist_empty":      "Список отслеживания пуст.\nДобавить: /watch <url>",
        "watchlist_header":     "📺 Список отслеживания ({count})",

        # Stream monitor live notification — see English block for rationale.
        "monitor_live_prompt":  "🔴 {platform} — {uploader} в эфире. Записать?",
        "btn_yes_record":       "✅ Да — записать",
        "btn_skip":             "❌ Пропустить",
        "btn_lang_en":          "English",
        "btn_lang_ru":          "Русский",
        "monitor_skipped":      "{uploader} — ⏭ Пропущено",
        "monitor_starting":     "{uploader} — 🎬 Запись начинается…",
        "monitor_record_starting": "🔴 Запись · @{uploader}\nЗапуск…",
        "monitor_recording_crashed": "⚠ Запись прервалась с ошибкой: {error}",
        "btn_snooze_1h":        "💤 1ч",
        "btn_snooze_8h":        "😴 8ч",
        "monitor_snoozed":      "{uploader} — 💤 Тишина на {duration} (до {until})",

        # /start handshake — pending-code expiry edit
        "access_code_expired": "⏱ Одноразовый код доступа истёк.\nОтправьте /regenerate\\_token для нового.",

        # /timezone
        "tz_current":           "Текущий часовой пояс: {tz}\n\nИзменить: /timezone <смещение>",
        "tz_set":               "Часовой пояс установлен: {tz}.",
        "tz_invalid":           "Неверное смещение: {value}. Диапазон: от -12 до +14.",
        "tz_usage":             "Использование: /timezone <смещение>\n\nПримеры:\n  /timezone 8     → UTC+8 (Сингапур)\n  /timezone -5    → UTC-5 (Нью-Йорк)\n  /timezone 5.5   → UTC+5:30 (Индия)",

        # /transcode
        "transcode_picker":     "Выберите режим перекодировки после записи:\n\nСейчас: {current}",
        "transcode_set":        "Перекодировка установлена: {summary}",
        "transcode_off":        "Выкл — без перекодировки",
        "transcode_replace":    "{height}p (заменяет оригинал)",
        "transcode_keep":       "{height}p (сохраняет оригинал как архив)",
        "btn_transcode_off":    "❌ Выкл",
        "btn_transcode_480_r":  "🔻 480p · заменить",
        "btn_transcode_240_r":  "🔻 240p · заменить",
        "btn_transcode_480_k":  "📦 480p + архив",
        "btn_transcode_240_k":  "📦 240p + архив",

        # /default_video_size — quality for non-live downloads
        "vq_picker":            "Качество видео по умолчанию для обычных загрузок:\n\nСейчас: {current}",
        "vq_set":               "Качество видео установлено: {value}.",
        "vq_label_best":        "Лучшее доступное (1080p+ если есть)",
        "vq_label_height":      "{height}p",
        "btn_vq_best":          "🥇 Лучшее",
        "btn_vq_1080":          "📺 1080p",
        "btn_vq_720":           "🖥 720p",
        "btn_vq_360":           "📱 360p",

        # /storage_stats + /clear_cache
        "storage_stats":        "📊 Хранилище\n\nСвободно на /downloads: {free_gb:.1f} ГБ из {total_gb:.0f} ГБ\nЗагрузки: {downloads_count} файлов, {downloads_size}\nЭфиры: {live_count} файлов, {live_size}\n\n💾 Кэш URL: {cache_count} записей\nСтарейшая: {cache_oldest}\nНовейшая: {cache_newest}",
        "cache_cleared":        "🧹 Кэш очищен. Удалено {count} запис{plural}.",
        "cache_clear_usage":    "Использование: /clear_cache       — очистить всё\n              /clear_cache <url> — очистить одну ссылку",
        "cache_url_not_found":  "ℹ URL не в кэше: {url}",
    },
}


_lang_cache: dict[int, str] = {}
_loaded_from_disk = False


def _load_from_disk_once() -> None:
    global _loaded_from_disk
    if _loaded_from_disk:
        return
    _loaded_from_disk = True
    if not LANG_FILE.exists():
        return
    try:
        with open(LANG_FILE) as f:
            data = json.load(f)
        for k, v in data.items():
            try:
                _lang_cache[int(k)] = str(v) if str(v) in SUPPORTED_LANGS else DEFAULT_LANG
            except (ValueError, TypeError):
                continue
    except Exception as e:
        logger.warning("i18n: failed to read %s: %s", LANG_FILE, e)


def get_lang(chat_id: int) -> str:
    _load_from_disk_once()
    return _lang_cache.get(int(chat_id), DEFAULT_LANG)


def set_lang(chat_id: int, lang: str) -> bool:
    """Returns True if the lang was set, False if not supported."""
    if lang not in SUPPORTED_LANGS:
        return False
    _load_from_disk_once()
    _lang_cache[int(chat_id)] = lang
    try:
        LANG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LANG_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in _lang_cache.items()}, f, indent=2, ensure_ascii=False)
        tmp.replace(LANG_FILE)
    except Exception as e:
        logger.error("i18n: failed to persist %s: %s", LANG_FILE, e)
    return True


_tz_cache: dict[int, float] = {}
_tz_loaded_from_disk = False


def _load_tz_from_disk_once() -> None:
    global _tz_loaded_from_disk
    if _tz_loaded_from_disk:
        return
    _tz_loaded_from_disk = True
    if not TZ_FILE.exists():
        return
    try:
        with open(TZ_FILE) as f:
            data = json.load(f)
        for k, v in data.items():
            try:
                offset = float(v)
                if -12 <= offset <= 14:
                    _tz_cache[int(k)] = offset
            except (ValueError, TypeError):
                continue
    except Exception as e:
        logger.warning("i18n: failed to read %s: %s", TZ_FILE, e)


def get_tz_offset(chat_id: int) -> float:
    _load_tz_from_disk_once()
    return _tz_cache.get(int(chat_id), DEFAULT_TZ_OFFSET)


def set_tz_offset(chat_id: int, offset_hours: float) -> bool:
    """Returns True if set, False if out of range."""
    if offset_hours < -12 or offset_hours > 14:
        return False
    _load_tz_from_disk_once()
    _tz_cache[int(chat_id)] = float(offset_hours)
    try:
        TZ_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TZ_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in _tz_cache.items()}, f, indent=2, ensure_ascii=False)
        tmp.replace(TZ_FILE)
    except Exception as e:
        logger.error("i18n: failed to persist %s: %s", TZ_FILE, e)
    return True


def format_tz_offset(offset: float) -> str:
    """0 → 'UTC', 8 → 'UTC+8', -5 → 'UTC-5', 5.5 → 'UTC+5:30'."""
    if offset == 0:
        return "UTC"
    sign = "+" if offset >= 0 else "-"
    abs_offset = abs(offset)
    hours = int(abs_offset)
    mins = int(round((abs_offset - hours) * 60))
    if mins == 0:
        return f"UTC{sign}{hours}"
    return f"UTC{sign}{hours}:{mins:02d}"


def format_local_time(epoch_seconds: float, chat_id: int, fmt: str = "%H:%M") -> str:
    """Format a UTC epoch as wall-clock time in the chat's configured timezone."""
    from datetime import datetime, timedelta, timezone
    offset = get_tz_offset(chat_id)
    tz = timezone(timedelta(hours=offset))
    return datetime.fromtimestamp(epoch_seconds, tz=tz).strftime(fmt)


_transcode_cache: dict[int, tuple[int, bool]] = {}
_transcode_loaded = False
ALLOWED_TRANSCODE_HEIGHTS = (0, 240, 480)


def _load_transcode_once() -> None:
    global _transcode_loaded
    if _transcode_loaded:
        return
    _transcode_loaded = True
    if not TRANSCODE_FILE.exists():
        return
    try:
        with open(TRANSCODE_FILE) as f:
            data = json.load(f)
        for k, v in data.items():
            try:
                # v is [height, keep_original]
                if isinstance(v, (list, tuple)) and len(v) == 2:
                    h, keep = int(v[0]), bool(v[1])
                    if h in ALLOWED_TRANSCODE_HEIGHTS:
                        _transcode_cache[int(k)] = (h, keep)
            except (ValueError, TypeError):
                continue
    except Exception as e:
        logger.warning("i18n: failed to read %s: %s", TRANSCODE_FILE, e)


def get_transcode_pref(chat_id: int) -> tuple[int, bool]:
    """Return (height, keep_original). (0, False) = off."""
    _load_transcode_once()
    return _transcode_cache.get(int(chat_id), (0, False))


def set_transcode_pref(chat_id: int, height: int, keep_original: bool) -> bool:
    if height not in ALLOWED_TRANSCODE_HEIGHTS:
        return False
    _load_transcode_once()
    _transcode_cache[int(chat_id)] = (int(height), bool(keep_original))
    try:
        TRANSCODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TRANSCODE_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {str(k): list(v) for k, v in _transcode_cache.items()},
                f, indent=2, ensure_ascii=False,
            )
        tmp.replace(TRANSCODE_FILE)
    except Exception as e:
        logger.error("i18n: failed to persist %s: %s", TRANSCODE_FILE, e)
    return True


def format_transcode_summary(height: int, keep_original: bool, lang: str = DEFAULT_LANG) -> str:
    if height == 0:
        return t("transcode_off", lang)
    if keep_original:
        return t("transcode_keep", lang, height=height)
    return t("transcode_replace", lang, height=height)


_video_quality_cache: dict[int, str] = {}
_video_quality_loaded = False
# Supported values. "best" means the bot picks whatever yt-dlp considers
# best (typically the source resolution); the rest cap to that height.
ALLOWED_VIDEO_QUALITIES = ("best", "1080p", "720p", "360p")
DEFAULT_VIDEO_QUALITY = "best"


def _load_video_quality_once() -> None:
    global _video_quality_loaded
    if _video_quality_loaded:
        return
    _video_quality_loaded = True
    if not VIDEO_QUALITY_FILE.exists():
        return
    try:
        with open(VIDEO_QUALITY_FILE) as f:
            data = json.load(f)
        for k, v in data.items():
            try:
                if str(v) in ALLOWED_VIDEO_QUALITIES:
                    _video_quality_cache[int(k)] = str(v)
            except (ValueError, TypeError):
                continue
    except Exception as e:
        logger.warning("i18n: failed to read %s: %s", VIDEO_QUALITY_FILE, e)


def get_video_quality(chat_id: int) -> str:
    """Return the chat's video-quality preference ('best' / '1080p' / '720p' / '360p')."""
    _load_video_quality_once()
    return _video_quality_cache.get(int(chat_id), DEFAULT_VIDEO_QUALITY)


def set_video_quality(chat_id: int, quality: str) -> bool:
    if quality not in ALLOWED_VIDEO_QUALITIES:
        return False
    _load_video_quality_once()
    _video_quality_cache[int(chat_id)] = quality
    try:
        VIDEO_QUALITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = VIDEO_QUALITY_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {str(k): v for k, v in _video_quality_cache.items()},
                f, indent=2, ensure_ascii=False,
            )
        tmp.replace(VIDEO_QUALITY_FILE)
    except Exception as e:
        logger.error("i18n: failed to persist %s: %s", VIDEO_QUALITY_FILE, e)
    return True


def format_video_quality_summary(quality: str, lang: str = DEFAULT_LANG) -> str:
    if quality == "best":
        return t("vq_label_best", lang)
    return t("vq_label_height", lang, height=quality.rstrip("p"))


def t(key: str, lang: str = DEFAULT_LANG, **kwargs) -> str:
    """Translate a key, falling back to English then to the key itself."""
    table = STRINGS.get(lang) or STRINGS[DEFAULT_LANG]
    template = table.get(key) or STRINGS[DEFAULT_LANG].get(key) or key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError) as e:
        logger.warning("i18n: format failed for key=%s lang=%s: %s", key, lang, e)
        return template


def format_duration(seconds: int | float) -> str:
    """Format a duration as H:MM:SS (or MM:SS for under an hour).

    Examples: 7 → '0:07', 332 → '5:32', 3932 → '1:05:32'.
    """
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"
