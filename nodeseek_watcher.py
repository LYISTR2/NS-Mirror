#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NodeSeek 关键词监控 → Telegram 推送（RSS + TG 控制面板）。"""

from __future__ import annotations

import html
import json
import logging
import os
import random
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Sequence

import requests


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


RSS_URL = "https://rss.nodeseek.com/"
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "replace-with-your-bot-token")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "replace-with-chat-id")
POLL_INTERVAL = _env_int("POLL_INTERVAL", 5, 2)
JITTER = 0.2
FETCH_RETRIES = 2
RETRY_BACKOFF = 3

# 体验相关的可选开关（均可通过环境变量覆盖）
QUIET_HOURS = os.environ.get("QUIET_HOURS", "").strip()      # 例："23:00-08:00"，期间静音推送（仍会送达）
LINK_PREVIEW = _env_flag("LINK_PREVIEW", False)              # 通知是否展开链接预览卡片
CLEAN_CHAT = _env_flag("CLEAN_CHAT", True)                   # 添加关键词后自动删除你发的那条输入
HEALTH_ALERT_AFTER = _env_int("HEALTH_ALERT_AFTER", 30, 3)   # RSS 连续失败多少轮后提醒

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "state.json")
KW_FILE = os.path.join(DATA_DIR, "keywords.json")
BLACKLIST_FILE = os.path.join(DATA_DIR, "blacklist.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
DEFAULT_KEYWORDS = ["dmit", "vmiss", "rfc"]
MAX_RSS_BYTES = 2 * 1024 * 1024

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nodeseek-watcher")

_CHINA_TZ = timezone(timedelta(hours=8))

_KEYWORDS_LOCK = threading.RLock()
_RSS_SESSION = requests.Session()
_RSS_SESSION.trust_env = False  # RSS 明确直连，不继承 HTTP(S)_PROXY
_TG_UPDATE_SESSION = requests.Session()
_TG_SEND_SESSION = requests.Session()
_TG_SEND_LOCK = threading.Lock()  # requests.Session 本身不保证跨线程安全

PENDING_INPUT: dict[str, str] = {}            # chat_id -> 等待输入的模式
PENDING_CONTEXT: dict[str, int | None] = {}   # chat_id -> 正在被改写成“输入提示”的面板消息 id
LAST_DELETED: dict[str, tuple[int, str]] = {}  # chat_id -> (原位置, 关键词)，供「撤销」使用

STATS: dict[str, Any] = {"last_check": None, "hits": 0}
_HEALTH: dict[str, Any] = {"fails": 0, "notified": False}


# ───────────────────────── 持久化 ─────────────────────────

def _atomic_write_json(path: str, value: Any) -> None:
    """在同目录写临时文件并原子替换，尽量确保崩溃后文件仍完整。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _clean_keywords(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        keyword = item.strip().lower()
        folded = keyword.casefold()
        if keyword and folded not in seen:
            result.append(keyword)
            seen.add(folded)
    return result


def save_keywords(keywords: Iterable[str]) -> None:
    _atomic_write_json(KW_FILE, list(keywords))


def load_keywords() -> list[str]:
    try:
        with open(KW_FILE, encoding="utf-8") as file:
            raw = json.load(file)
        keywords = _clean_keywords(raw)
        if keywords is not None:  # 空数组是合法配置，不应恢复默认值
            if keywords != raw:
                save_keywords(keywords)
            return keywords
        log.warning("keywords.json 格式无效，恢复默认关键词")
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("读取 keywords.json 失败，恢复默认关键词: %s", exc)
    keywords = list(DEFAULT_KEYWORDS)
    try:
        save_keywords(keywords)
    except OSError as exc:
        log.error("保存默认关键词失败: %s", exc)
    return keywords


def load_blacklist() -> list[str]:
    try:
        with open(BLACKLIST_FILE, encoding="utf-8") as file:
            return _clean_keywords(json.load(file)) or []
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("读取 blacklist.json 失败，按空列表处理: %s", exc)
        return []


def load_settings() -> dict[str, Any]:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as file:
            raw = json.load(file)
        return {"paused": bool(raw.get("paused"))} if isinstance(raw, dict) else {"paused": False}
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("读取 settings.json 失败，使用默认设置: %s", exc)
    return {"paused": False}


KEYWORDS = load_keywords()
BLACKLIST: list[str] = load_blacklist()
SETTINGS = load_settings()


def is_paused() -> bool:
    return bool(SETTINGS.get("paused"))


def set_paused(value: bool) -> None:
    SETTINGS["paused"] = bool(value)
    try:
        _atomic_write_json(SETTINGS_FILE, SETTINGS)
    except OSError as exc:
        log.error("保存 settings.json 失败: %s", exc)


# ───────────────────────── 关键词 / 屏蔽词 ─────────────────────────

def keyword_snapshot() -> tuple[str, ...]:
    with _KEYWORDS_LOCK:
        return tuple(KEYWORDS)


def blacklist_snapshot() -> tuple[str, ...]:
    with _KEYWORDS_LOCK:
        return tuple(BLACKLIST)


def _insert_keyword(keyword: str, index: int | None = None, *,
                    icon: str = "✅", label: str = "已添加") -> tuple[bool, str]:
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return False, "关键词不能为空"
    folded = keyword.casefold()
    with _KEYWORDS_LOCK:
        if any(item.casefold() == folded for item in KEYWORDS):
            return False, f"「{html.escape(keyword)}」已存在"
        candidate = [*KEYWORDS]
        candidate.insert(len(candidate) if index is None else max(0, min(index, len(candidate))), keyword)
        try:
            save_keywords(candidate)
        except OSError as exc:
            log.error("保存关键词失败: %s", exc)
            return False, "保存关键词失败，请稍后重试"
        KEYWORDS[:] = candidate
    return True, f"{icon} {label}「{html.escape(keyword)}」"


def add_keyword(keyword: str) -> tuple[bool, str]:
    return _insert_keyword(keyword)


def del_keyword(keyword: str) -> tuple[bool, str]:
    keyword = (keyword or "").strip().lower()
    folded = keyword.casefold()
    with _KEYWORDS_LOCK:
        index = next((i for i, item in enumerate(KEYWORDS) if item.casefold() == folded), None)
        if index is None:
            return False, f"「{html.escape(keyword)}」不存在"
        return _del_keyword_at_locked(index)


def _del_keyword_at_locked(index: int) -> tuple[bool, str]:
    keyword = KEYWORDS[index]
    candidate = KEYWORDS.copy()
    candidate.pop(index)
    try:
        save_keywords(candidate)
    except OSError as exc:
        log.error("保存关键词失败: %s", exc)
        return False, "保存关键词失败，请稍后重试"
    KEYWORDS[:] = candidate
    return True, f"✅ 已删除「{html.escape(keyword)}」"


def keyword_tag(keyword: str) -> str:
    """按钮里附带的短校验码：列表在面板显示后发生变化时，避免误删别的词。"""
    return f"{zlib.crc32(keyword.casefold().encode('utf-8')) & 0xFFFF:04x}"


def del_keyword_at(index: int, tag: str | None = None) -> tuple[bool, str]:
    with _KEYWORDS_LOCK:
        if index < 0 or index >= len(KEYWORDS):
            return False, "关键词列表已变化，请刷新面板"
        if tag is not None and keyword_tag(KEYWORDS[index]) != tag:
            return False, "关键词列表已变化，请刷新面板"
        return _del_keyword_at_locked(index)


def add_blocked(word: str) -> tuple[bool, str]:
    word = (word or "").strip().lower()
    if not word:
        return False, "屏蔽词不能为空"
    with _KEYWORDS_LOCK:
        if any(item.casefold() == word.casefold() for item in BLACKLIST):
            return False, f"「{html.escape(word)}」已在屏蔽列表"
        candidate = [*BLACKLIST, word]
        try:
            _atomic_write_json(BLACKLIST_FILE, candidate)
        except OSError as exc:
            log.error("保存屏蔽词失败: %s", exc)
            return False, "保存屏蔽词失败，请稍后重试"
        BLACKLIST[:] = candidate
    return True, f"🚫 已屏蔽「{html.escape(word)}」"


def del_blocked(word: str) -> tuple[bool, str]:
    word = (word or "").strip().lower()
    with _KEYWORDS_LOCK:
        index = next((i for i, item in enumerate(BLACKLIST) if item.casefold() == word.casefold()), None)
        if index is None:
            return False, f"「{html.escape(word)}」不在屏蔽列表"
        candidate = [item for i, item in enumerate(BLACKLIST) if i != index]
        try:
            _atomic_write_json(BLACKLIST_FILE, candidate)
        except OSError as exc:
            log.error("保存屏蔽词失败: %s", exc)
            return False, "保存屏蔽词失败，请稍后重试"
        BLACKLIST[:] = candidate
    return True, f"✅ 已取消屏蔽「{html.escape(word)}」"


def list_keywords() -> str:
    keywords = keyword_snapshot()
    if not keywords:
        return "📋 当前没有关键词"
    lines = "\n".join(f"{index}. {html.escape(keyword)}" for index, keyword in enumerate(keywords, 1))
    return f"📋 当前关键词（{len(keywords)} 个）：\n{lines}"


def list_blocklist() -> str:
    blocked = blacklist_snapshot()
    if not blocked:
        return "🚫 当前没有屏蔽词\n用 /block 词 添加：标题含屏蔽词的帖子不会推送。"
    return f"🚫 屏蔽词（{len(blocked)} 个）：\n" + "、".join(html.escape(word) for word in blocked)


# ───────────────────────── 时间 / 静音 ─────────────────────────

def _parse_quiet_hours(spec: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*[-~–—]\s*(\d{1,2}):(\d{2})\s*", spec or "")
    if not match:
        return None
    h1, m1, h2, m2 = (int(part) for part in match.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        return None
    return h1 * 60 + m1, h2 * 60 + m2


_QUIET = _parse_quiet_hours(QUIET_HOURS)
if QUIET_HOURS and _QUIET is None:
    log.warning("QUIET_HOURS=%r 格式无效，应为 HH:MM-HH:MM，已忽略", QUIET_HOURS)


def in_quiet_hours(now: datetime | None = None) -> bool:
    """静音时段（北京时间）内返回 True；支持跨午夜，例如 23:00-08:00。"""
    if _QUIET is None:
        return False
    current = (now or datetime.now(_CHINA_TZ)).astimezone(_CHINA_TZ)
    minutes = current.hour * 60 + current.minute
    start, end = _QUIET
    if start == end:
        return False
    return start <= minutes < end if start < end else (minutes >= start or minutes < end)


def _clock(timestamp: float | None) -> str:
    if not timestamp:
        return "—"
    return datetime.fromtimestamp(timestamp, _CHINA_TZ).strftime("%H:%M:%S")


# ───────────────────────── 控制面板 ─────────────────────────

_RULE = "━━━━━━━━━━━━━━"
HELP_TEXT = (
    "📖 <b>使用说明</b>\n\n"
    "/panel　打开控制面板\n"
    "/add 词1 词2　添加关键词\n"
    "/del 词　删除关键词\n"
    "/list　查看关键词与屏蔽词\n"
    "/block 词　屏蔽：标题含此词的帖子不推送\n"
    "/unblock 词　取消屏蔽\n"
    "/pause　/resume　暂停 / 恢复推送\n\n"
    "关键词只匹配帖子<b>标题</b>，不区分大小写，支持中文。\n"
    "面板里点「➕ 添加关键词」后直接发文字即可，点 ❌ 按钮可删除。"
)


def _display_width(text: str) -> int:
    return sum(2 if ord(char) >= 0x2E80 else 1 for char in text)


def _shorten(text: str, max_width: int) -> str:
    if _display_width(text) <= max_width:
        return text
    out, width = "", 0
    for char in text:
        step = 2 if ord(char) >= 0x2E80 else 1
        if width + step > max_width - 1:
            break
        out += char
        width += step
    return out + "…"


def _status_line() -> str:
    if is_paused():
        return "⏸ 已暂停推送"
    if _HEALTH["fails"] >= 3:
        return f"🟠 RSS 异常（连续失败 {_HEALTH['fails']} 次）"
    return "🟢 运行中"


def control_panel(notice: str = "", undo: bool = False) -> tuple[str, dict[str, list[list[dict[str, str]]]]]:
    keywords = keyword_snapshot()
    blocked = blacklist_snapshot()
    keyword_text = "\n".join(f"{index}. {html.escape(word)}" for index, word in enumerate(keywords, 1))

    freq = f"每 {POLL_INTERVAL} 秒"
    if _QUIET is not None:
        freq += f" · 🌙 静音 {html.escape(QUIET_HOURS)}"

    lines = ["🛰 <b>NodeSeek 监控控制面板</b>"]
    if notice:
        lines += ["", notice]
    lines += [
        "", _RULE,
        f"状态　{_status_line()}",
        f"频率　{freq}",
        f"检查　{_clock(STATS['last_check'])} · 累计命中 {STATS['hits']} 条",
        _RULE, "",
        f"🏷 <b>关键词</b> · {len(keywords)}",
        keyword_text or "当前没有关键词",
    ]
    if blocked:
        lines += ["", f"🚫 <b>屏蔽词</b> · {len(blocked)}", "、".join(html.escape(w) for w in blocked)]
    lines += ["", "点击下方 ❌ 按钮删除对应关键词。" if keywords else "点击「➕ 添加关键词」开始。"]
    text = "\n".join(lines)

    rows: list[list[dict[str, str]]] = [[
        {"text": "➕ 添加关键词", "callback_data": "panel:add"},
        {"text": "🔄 刷新", "callback_data": "panel:refresh"},
    ]]
    longest = max((_display_width(word) for word in keywords), default=0)
    per_row, limit = (3, 8) if longest <= 8 else (2, 16)
    buttons = [{"text": f"❌ {_shorten(word, limit)}",
                "callback_data": f"kw:del:{index}:{keyword_tag(word)}"}
               for index, word in enumerate(keywords)]
    rows.extend(buttons[i:i + per_row] for i in range(0, len(buttons), per_row))
    if undo:
        rows.append([{"text": "↩️ 撤销删除", "callback_data": "panel:undo"}])
    rows.append([
        {"text": "▶️ 恢复推送" if is_paused() else "⏸ 暂停推送",
         "callback_data": "panel:resume" if is_paused() else "panel:pause"},
        {"text": "❓ 帮助", "callback_data": "panel:help"},
    ])
    return text, {"inline_keyboard": rows}


# ───────────────────────── RSS ─────────────────────────

def fetch_rss() -> bytes:
    last_error = "未知错误"
    for attempt in range(FETCH_RETRIES):
        try:
            response = _RSS_SESSION.get(
                RSS_URL,
                timeout=(5, 10),
                headers={"User-Agent": "NodeSeek-Watcher/1.0 (+RSS monitor)"},
            )
            response.raise_for_status()
            content = response.content
            if not content or len(content) > MAX_RSS_BYTES:
                raise ValueError(f"RSS 响应大小异常: {len(content)} bytes")
            prefix = content.lstrip()[:200].lower()
            if not (prefix.startswith(b"<?xml") or prefix.startswith(b"<rss") or prefix.startswith(b"<feed")):
                raise ValueError("RSS 响应不是 XML（可能是错误页）")
            return content
        except (requests.RequestException, ValueError) as exc:
            last_error = str(exc)[:200]
            if attempt + 1 < FETCH_RETRIES:
                time.sleep(RETRY_BACKOFF)
    raise RuntimeError(f"RSS 抓取失败: {last_error}")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_posts(xml_data: str | bytes) -> list[dict[str, Any]]:
    """解析 RSS；坏字节用替换字符保留其余有效条目。"""
    if isinstance(xml_data, bytes):
        xml_text = xml_data.decode("utf-8-sig", errors="replace")
    else:
        xml_text = xml_data.lstrip("\ufeff")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"RSS XML 解析失败: {exc}") from exc

    posts_by_id: dict[int, dict[str, Any]] = {}
    for item in (element for element in root.iter() if _local_name(element.tag) == "item"):
        fields: dict[str, str] = {}
        for child in item:
            name = _local_name(child.tag)
            if name not in fields:
                fields[name] = "".join(child.itertext()).strip()
        guid = fields.get("guid", "").strip()
        if not guid.isdecimal():
            continue
        post_id = int(guid)
        if post_id < 0:
            continue
        posts_by_id[post_id] = {
            "id": post_id,
            "title": fields.get("title", ""),
            "url": fields.get("link", "") or f"https://www.nodeseek.com/post-{post_id}-1",
            "time": fields.get("pubDate", ""),
        }
    return sorted(posts_by_id.values(), key=lambda post: post["id"], reverse=True)


def load_state() -> dict[str, int]:
    try:
        with open(STATE_FILE, encoding="utf-8") as file:
            raw = json.load(file)
        last_id = raw.get("last_max_id") if isinstance(raw, dict) else None
        if isinstance(last_id, int) and not isinstance(last_id, bool) and last_id >= 0:
            return {"last_max_id": last_id}
        if isinstance(last_id, str) and last_id.isdecimal():
            return {"last_max_id": int(last_id)}
        if last_id is not None:
            log.warning("state.json 中 last_max_id 无效，按首次运行处理")
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("读取 state.json 失败，按首次运行处理: %s", exc)
    return {}


def save_state(state: dict[str, int]) -> None:
    _atomic_write_json(STATE_FILE, state)


# ───────────────────────── Telegram 基础 ─────────────────────────

def _telegram_api_ok(response: requests.Response) -> bool:
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Telegram 返回非 JSON 响应") from exc
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API 错误: {str(payload.get('description', 'unknown'))[:200]}")
    return True


def tg_call(method: str, payload: dict[str, Any], retries: int = 3) -> dict[str, Any] | None:
    """调用 Bot API；成功返回 result（dict，可能为空），失败返回 None。

    429 按 retry_after 等待；其余 4xx（如 400/403）属于永久错误，不再白白重试。
    editMessageText 的 “message is not modified” 视为成功。
    """
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    for attempt in range(retries):
        try:
            with _TG_SEND_LOCK:
                response = _TG_SEND_SESSION.post(url, json=payload, timeout=(5, 15))
            status = response.status_code
            if status == 429:
                try:
                    wait = max(1, min(int(response.json().get("parameters", {}).get("retry_after", 5)), 60))
                except (ValueError, TypeError):
                    wait = 5
                time.sleep(wait)
                continue
            if status == 400 and "not modified" in (response.text or "").lower():
                return {}
            if isinstance(status, int) and 400 <= status < 500:
                log.error("TG %s 被拒绝(%d): %s", method, status, (response.text or "")[:200])
                return None
            _telegram_api_ok(response)
            result = response.json().get("result")
            return result if isinstance(result, dict) else {}
        except (requests.RequestException, RuntimeError) as exc:
            log.error("TG %s 失败(第%d次): %s", method, attempt + 1, exc)
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
    return None


def tg_request(method: str, payload: dict[str, Any], retries: int = 3) -> bool:
    return tg_call(method, payload, retries) is not None


_MDV2_SPECIAL_RE = re.compile(r"([_*[\]()~`>#+\-=|{}.!])")


def _mdv2_escape(text: str) -> str:
    """转义 MarkdownV2 保留字符（`_ * [ ] ( ) ~ ` > # + - = | { } . !`）。"""
    return _MDV2_SPECIAL_RE.sub(r"\\\1", text)


def _mdv2_escape_code(text: str) -> str:
    """MarkdownV2 code 实体内容转义：只有反斜杠与反引号需要转义。"""
    return text.replace("\\", "\\\\").replace("`", "\\`")


def _mdv2_escape_url(url: str) -> str:
    """MarkdownV2 链接 URL 中只需额外转义反斜杠与右括号，避免链接被截断。"""
    return url.replace("\\", "\\\\").replace(")", "\\)")


Alert = tuple[str, str, str]


def _parse_pub_date(pub_date: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(pub_date or "")
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_pub_date(pub_date: str) -> str:
    """RSS pubDate（RFC 2822）统一转换为北京时间（UTC+8），输出 `YYYY-MM-DD HH:MM`；解析失败返回原文。"""
    parsed = _parse_pub_date(pub_date)
    if parsed is None:
        return pub_date
    return parsed.astimezone(_CHINA_TZ).strftime("%Y-%m-%d %H:%M")


def _relative_time(pub_date: str, now: datetime | None = None) -> str:
    parsed = _parse_pub_date(pub_date)
    if parsed is None:
        return ""
    seconds = ((now or datetime.now(timezone.utc)) - parsed).total_seconds()
    if seconds < 90:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


def alert_body_markdownv2(post_id: int, title: str, url: str,
                          keyword: str | Sequence[str], pub_date: str) -> str:
    """新帖命中通知（MarkdownV2）。

    *🔔 NS / NEW POST*　`№ 852452`

    [标题](链接)

    > 🏷 命中　`dmit` `rfc`
    > 🕒 `2026-08-07 15:30`　·　刚刚
    """
    keywords = [keyword] if isinstance(keyword, str) else list(keyword)
    chips = " ".join(f"`{_mdv2_escape_code(item)}`" for item in keywords)
    stamp = f"`{_mdv2_escape_code(_format_pub_date(pub_date))}`"
    relative = _relative_time(pub_date)
    time_line = f"> 🕒 {stamp}" + (f"　·　{_mdv2_escape(relative)}" if relative else "")
    return "\n".join([
        f"*🔔 NS / NEW POST*　`№ {post_id}`",
        "",
        f"[{_mdv2_escape(title or '（无标题）')}]({_mdv2_escape_url(url)})",
        "",
        f"> 🏷 命中　{chips}",
        time_line,
    ])


def _send(text: str, *, reply_markup: dict[str, Any] | None = None, parse_mode: str = "HTML",
          disable_notification: bool = False, disable_preview: bool = False) -> dict[str, Any] | None:
    payload: dict[str, Any] = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }
    if disable_notification:
        payload["disable_notification"] = True
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return tg_call("sendMessage", payload)


def send_tg(text: str, *, reply_markup: dict[str, Any] | None = None, parse_mode: str = "HTML",
            disable_notification: bool = False, disable_preview: bool = False) -> bool:
    return _send(text, reply_markup=reply_markup, parse_mode=parse_mode,
                 disable_notification=disable_notification, disable_preview=disable_preview) is not None


def show_panel(notice: str = "", undo: bool = False) -> bool:
    text, markup = control_panel(notice, undo)
    return send_tg(text, reply_markup=markup, disable_preview=True)


def edit_text(message_id: int, text: str, markup: dict[str, Any] | None = None) -> bool:
    payload: dict[str, Any] = {
        "chat_id": TG_CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if markup is not None:
        payload["reply_markup"] = markup
    return tg_request("editMessageText", payload, retries=1)


def edit_panel(message_id: int, notice: str = "", undo: bool = False) -> bool:
    text, markup = control_panel(notice, undo)
    return edit_text(message_id, text, markup)


def answer_callback(callback_id: str, text: str = "", *, alert: bool = False) -> bool:
    payload: dict[str, Any] = {"callback_query_id": callback_id, "show_alert": alert}
    if text:
        payload["text"] = text[:200]
    return tg_request("answerCallbackQuery", payload, retries=1)


def register_bot_commands() -> bool:
    """为绑定私聊注册 Telegram 输入框旁的 / 命令菜单。"""
    commands = [
        {"command": "panel", "description": "打开控制面板"},
        {"command": "add", "description": "添加关键词，例如 /add dmit"},
        {"command": "del", "description": "删除关键词，例如 /del dmit"},
        {"command": "list", "description": "查看关键词与屏蔽词"},
        {"command": "block", "description": "屏蔽词：标题含此词不推送"},
        {"command": "unblock", "description": "取消屏蔽"},
        {"command": "pause", "description": "暂停推送"},
        {"command": "resume", "description": "恢复推送"},
        {"command": "help", "description": "查看使用说明"},
    ]
    scope = {"type": "chat", "chat_id": TG_CHAT_ID}
    commands_ok = tg_request("setMyCommands", {"commands": commands, "scope": scope})
    menu_ok = tg_request("setChatMenuButton", {
        "chat_id": TG_CHAT_ID,
        "menu_button": {"type": "commands"},
    })
    return commands_ok and menu_ok


# ───────────────────────── 指令 / 回调 ─────────────────────────

def _lines(results: Iterable[tuple[bool, str]]) -> str:
    return "\n".join(message if ok else f"⚠️ {message}" for ok, message in results)


def handle_command(text: str | None) -> bool:
    """保留少量快捷指令；日常管理使用内联控制面板。"""
    text = (text or "").strip()
    if not text.startswith("/"):
        return False
    parts = text.split()
    command = parts[0].split("@", 1)[0].lower()  # 兼容 /add@BotName
    args = parts[1:]

    if command in ("/start", "/panel"):
        show_panel()
        return True
    if command in ("/list", "/kw", "/keywords"):
        blocked = list_blocklist() if blacklist_snapshot() else ""
        send_tg(list_keywords() + (f"\n\n{blocked}" if blocked else ""))
        return True
    if command == "/add":
        send_tg(_lines(add_keyword(arg) for arg in args) if args else "用法：/add 关键词1 关键词2 ...")
        return True
    if command == "/del":
        send_tg(_lines(del_keyword(arg) for arg in args) if args else "用法：/del 关键词")
        return True
    if command == "/block":
        send_tg(_lines(add_blocked(arg) for arg in args) if args else "用法：/block 词1 词2 ...\n标题含屏蔽词的帖子不会推送。")
        return True
    if command == "/unblock":
        send_tg(_lines(del_blocked(arg) for arg in args) if args else "用法：/unblock 词")
        return True
    if command in ("/blocklist", "/bl"):
        send_tg(list_blocklist())
        return True
    if command == "/pause":
        set_paused(True)
        send_tg("⏸ 已暂停推送。期间的新帖会被跳过，不会在恢复后补发。\n发送 /resume 恢复。")
        return True
    if command == "/resume":
        set_paused(False)
        send_tg("▶️ 已恢复推送。")
        return True
    if command == "/help":
        send_tg(HELP_TEXT, disable_preview=True)
        return True
    return False


def _cancel_pending(chat_id: str) -> None:
    """退出“等待输入”状态，并把被改成输入提示的面板还原。"""
    PENDING_INPUT.pop(chat_id, None)
    panel_id = PENDING_CONTEXT.pop(chat_id, None)
    if panel_id is not None:
        edit_panel(panel_id)


def handle_message(text: str | None, chat_id: str, message_id: int | None = None) -> bool:
    text = (text or "").strip()
    mode = PENDING_INPUT.get(chat_id)
    if text.startswith("/"):
        _cancel_pending(chat_id)
        return handle_command(text)
    if not mode or not text:
        return handle_command(text)

    PENDING_INPUT.pop(chat_id, None)
    panel_id = PENDING_CONTEXT.pop(chat_id, None)
    if mode == "add":
        notice = _lines(add_keyword(word) for word in text.split())
        if CLEAN_CHAT and message_id is not None:
            tg_request("deleteMessage", {"chat_id": TG_CHAT_ID, "message_id": message_id}, retries=1)
        if panel_id is not None and edit_panel(panel_id, notice=notice):
            return True
        send_tg(notice)
        show_panel()
        return True
    return False


def handle_callback(data: str, callback_id: str, chat_id: str, message_id: int | None = None) -> bool:
    if data == "panel:add":
        PENDING_INPUT[chat_id] = "add"
        PENDING_CONTEXT[chat_id] = message_id
        answer_callback(callback_id)
        prompt = ("➕ <b>添加关键词</b>\n\n直接发送要监控的词，多个词用空格分隔。\n"
                  "只匹配帖子标题，不区分大小写。")
        markup = {"inline_keyboard": [[{"text": "✖️ 取消", "callback_data": "panel:cancel"}]]}
        if message_id is not None and edit_text(message_id, prompt, markup):
            return True
        PENDING_CONTEXT.pop(chat_id, None)  # 无法改写面板时，退化为新发一条提示
        return send_tg(prompt, reply_markup=markup, disable_preview=True)

    if data == "panel:cancel":
        PENDING_INPUT.pop(chat_id, None)
        PENDING_CONTEXT.pop(chat_id, None)
        answer_callback(callback_id, "已取消")
        return edit_panel(message_id) if message_id is not None else True

    if data == "panel:help":
        answer_callback(callback_id)
        return send_tg(HELP_TEXT, disable_preview=True)

    if data == "panel:refresh":
        answer_callback(callback_id, "已刷新")
        return edit_panel(message_id) if message_id is not None else show_panel()

    if data in ("panel:pause", "panel:resume"):
        set_paused(data == "panel:pause")
        answer_callback(callback_id, "⏸ 已暂停推送" if is_paused() else "▶️ 已恢复推送")
        return edit_panel(message_id) if message_id is not None else show_panel()

    if data == "panel:undo":
        saved = LAST_DELETED.pop(chat_id, None)
        if saved is None:
            answer_callback(callback_id, "没有可撤销的操作", alert=True)
            return False
        ok, message = _insert_keyword(saved[1], saved[0], icon="↩️", label="已恢复")
        answer_callback(callback_id, re.sub(r"<[^>]+>", "", message), alert=not ok)
        if message_id is not None:
            edit_panel(message_id, notice=message if ok else f"⚠️ {message}")
        return ok

    if data.startswith("kw:del:"):
        parts = data.split(":")
        try:
            index = int(parts[2])
        except (IndexError, ValueError):
            answer_callback(callback_id, "无效操作", alert=True)
            return True
        tag = parts[3] if len(parts) > 3 else None  # 兼容旧版面板上没有校验码的按钮
        snapshot = keyword_snapshot()
        removed = snapshot[index] if 0 <= index < len(snapshot) else None
        ok, message = del_keyword_at(index, tag)
        if ok and removed is not None:
            LAST_DELETED[chat_id] = (index, removed)
        answer_callback(callback_id, re.sub(r"<[^>]+>", "", message), alert=not ok)
        if message_id is not None:
            edit_panel(message_id, notice=message if ok else f"⚠️ {message}", undo=ok)
        return True

    answer_callback(callback_id, "未知操作", alert=True)
    return False


def process_update(update: dict[str, Any]) -> None:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        callback_id = str(callback.get("id", ""))
        sender_id = str((callback.get("from") or {}).get("id", ""))
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", sender_id))
        if sender_id != str(TG_CHAT_ID) or chat_id != str(TG_CHAT_ID):
            answer_callback(callback_id, "无权操作", alert=True)
            return
        handle_callback(str(callback.get("data", "")), callback_id, chat_id, message.get("message_id"))
        return

    message = update.get("message") or update.get("edited_message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if chat_id == str(TG_CHAT_ID):
        handle_message(message.get("text"), chat_id, message.get("message_id"))


def updates_loop() -> None:
    """getUpdates 长轮询线程：处理绑定用户的消息和内联按钮。"""
    offset: int | None = None
    while True:
        try:
            params: dict[str, Any] = {"timeout": 25, "allowed_updates": json.dumps(["message", "callback_query"])}
            if offset is not None:
                params["offset"] = offset
            response = _TG_UPDATE_SESSION.get(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates",
                params=params,
                timeout=(5, 35),
            )
            _telegram_api_ok(response)
            updates = response.json().get("result", [])
            if not isinstance(updates, list):
                raise RuntimeError("Telegram getUpdates result 格式异常")
            for update in updates:
                if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                    continue
                # 即使消息格式异常或不是主人消息，也确认该 update，避免永久卡住。
                offset = max(offset or 0, update["update_id"] + 1)
                try:
                    process_update(update)
                except Exception:
                    log.exception("处理 Telegram 更新失败")
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            log.warning("getUpdates 错误: %s", str(exc)[:200])
            time.sleep(3)


# ───────────────────────── 监控主流程 ─────────────────────────

def _post_id_from_url(url: str) -> int | None:
    match = re.search(r"/post-(\d+)-", url)
    return int(match.group(1)) if match else None


def _matching_alerts(posts: list[dict[str, Any]], last_id: int) -> list[Alert]:
    """返回命中的通知，按发帖先后（旧→新）排列，让聊天记录与时间线一致。"""
    keyword_values = keyword_snapshot()
    keywords = tuple(keyword.casefold() for keyword in keyword_values)
    blacklist = tuple(str(word).casefold() for word in tuple(BLACKLIST) if str(word))

    alerts: list[Alert] = []
    for post in posts:
        if post["id"] <= last_id:
            continue
        title = str(post.get("title", ""))
        folded_title = title.casefold()
        hits = [keyword_values[i] for i, keyword in enumerate(keywords) if keyword in folded_title]
        if not hits or any(word in folded_title for word in blacklist):
            continue
        # 推送链接按约定始终使用帖子 ID 构造，避免 RSS 中异常链接进入链接实体。
        url = f"https://www.nodeseek.com/post-{post['id']}-1"
        pub_date = str(post.get("time", ""))
        body = alert_body_markdownv2(post["id"], title, url, hits, pub_date)
        alerts.append((title, url, body))
        log.info("📢 命中: [%s] %s", ", ".join(hits), title)
    alerts.reverse()
    return alerts


def check_once(state: dict[str, int]) -> list[Alert]:
    """检查一轮并更新内存游标；持久化和发送由调用者负责。"""
    posts = parse_posts(fetch_rss())
    STATS["last_check"] = time.time()
    if not posts:
        log.warning("解析到 0 条帖子，跳过本轮")
        return []
    max_id = posts[0]["id"]
    last_id = state.get("last_max_id")
    if last_id is None:
        state["last_max_id"] = max_id
        log.info("首次运行，记录最新帖子 id=%d，共 %d 条，不推送历史", max_id, len(posts))
        return []
    if not isinstance(last_id, int) or isinstance(last_id, bool) or last_id < 0:
        raise ValueError("内存状态 last_max_id 无效")
    new_posts = [post for post in posts if post["id"] > last_id]
    alerts = _matching_alerts(posts, last_id)
    state["last_max_id"] = max(last_id, max_id)
    if new_posts:
        log.info("本轮新帖 %d 条，命中 %d 条", len(new_posts), len(alerts))
    return alerts


def _alert_markup(url: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "🔗 查看原帖", "url": url}]]}


def run_once(state: dict[str, int]) -> bool:
    """完成一次抓取/发送/提交。

    发送失败时只回退到“第一条未送达的帖子之前”，已送达的不会重复推送，也不会漏报。
    暂停期间照常推进游标（恢复后不会补发一堆旧帖）。
    """
    previous = dict(state)
    alerts = check_once(state)
    if alerts and is_paused():
        log.info("推送已暂停，跳过 %d 条命中通知", len(alerts))
        alerts = []

    silent = in_quiet_hours()
    for index, (_title, url, body) in enumerate(alerts):
        if not send_tg(body, parse_mode="MarkdownV2", reply_markup=_alert_markup(url),
                       disable_notification=silent, disable_preview=not LINK_PREVIEW):
            state.clear()
            state.update(previous)
            first_unsent = _post_id_from_url(url)
            if index > 0 and first_unsent is not None and "last_max_id" in previous:
                state["last_max_id"] = max(previous["last_max_id"], first_unsent - 1)
                save_state(state)  # 已送达的部分落盘，避免重试时重复推送
            log.error("第 %d/%d 条通知发送失败，游标未越过它，稍后将重试", index + 1, len(alerts))
            return False
        STATS["hits"] += 1
        time.sleep(1)
    if state != previous:
        save_state(state)
    return True


def _track_health(ok: bool, error: str = "") -> None:
    """RSS 连续失败达到阈值时提醒一次，恢复后再通知一次。"""
    if ok:
        if _HEALTH["notified"]:
            send_tg("✅ <b>NodeSeek RSS 已恢复</b>\n监控继续正常运行。")
        _HEALTH.update(fails=0, notified=False)
        return
    _HEALTH["fails"] += 1
    if _HEALTH["fails"] >= HEALTH_ALERT_AFTER and not _HEALTH["notified"]:
        text = (f"⚠️ <b>NodeSeek RSS 连续 {_HEALTH['fails']} 次抓取失败</b>\n"
                f"<code>{html.escape(error[:150])}</code>\n"
                "程序仍在自动重试，恢复后会通知你。")
        if send_tg(text):
            _HEALTH["notified"] = True


def _validate_config() -> bool:
    missing = []
    if not TG_BOT_TOKEN or TG_BOT_TOKEN == "replace-with-your-bot-token":
        missing.append("TG_BOT_TOKEN")
    if not TG_CHAT_ID or TG_CHAT_ID == "replace-with-chat-id":
        missing.append("TG_CHAT_ID")
    if missing:
        log.error("缺少环境变量: %s", ", ".join(missing))
        return False
    return True


def main() -> None:
    if not _validate_config():
        raise SystemExit(2)
    if "--test" in sys.argv:
        raise SystemExit(0 if send_tg("✅ NodeSeek 监控测试消息") else 1)
    if register_bot_commands():
        log.info("已注册 Telegram / 命令菜单")
    else:
        log.warning("Telegram 命令菜单注册失败，监控仍将继续运行")

    state = load_state()  # 常驻模式只读一次；仅游标变化时写盘
    if "--once" in sys.argv:
        try:
            ok = run_once(state)
        except Exception as exc:
            log.error("本轮失败: %s", exc)
            ok = False
        raise SystemExit(0 if ok else 1)

    log.info("启动 NodeSeek RSS 监控: 关键词=%s, 间隔 %ss±%d%%",
             keyword_snapshot(), POLL_INTERVAL, int(JITTER * 100))
    threading.Thread(target=updates_loop, name="telegram-updates", daemon=True).start()
    while True:
        try:
            if run_once(state):
                _track_health(True)
        except Exception as exc:
            log.error("本轮失败: %s", exc)
            _track_health(False, str(exc))
        time.sleep(max(2.0, POLL_INTERVAL * (1 + random.uniform(-JITTER, JITTER))))


if __name__ == "__main__":
    main()
