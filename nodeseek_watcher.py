#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NodeSeek 关键词监控 → Telegram 推送（RSS + TG 指令管理）。"""

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
from typing import Any, Iterable

import requests

RSS_URL = "https://rss.nodeseek.com/"
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "replace-with-your-bot-token")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "replace-with-chat-id")
POLL_INTERVAL = 5
JITTER = 0.2
FETCH_RETRIES = 2
RETRY_BACKOFF = 3
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "state.json")
KW_FILE = os.path.join(DATA_DIR, "keywords.json")
DEFAULT_KEYWORDS = ["dmit", "vmiss", "rfc"]
BLACKLIST: list[str] = []
MAX_RSS_BYTES = 2 * 1024 * 1024

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nodeseek-watcher")

_KEYWORDS_LOCK = threading.RLock()
_RSS_SESSION = requests.Session()
_RSS_SESSION.trust_env = False  # RSS 明确直连，不继承 HTTP(S)_PROXY
_TG_UPDATE_SESSION = requests.Session()
_TG_SEND_SESSION = requests.Session()
_TG_SEND_LOCK = threading.Lock()  # requests.Session 本身不保证跨线程安全
PENDING_INPUT: dict[str, str] = {}


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


KEYWORDS = load_keywords()


def keyword_snapshot() -> tuple[str, ...]:
    with _KEYWORDS_LOCK:
        return tuple(KEYWORDS)


def add_keyword(keyword: str) -> tuple[bool, str]:
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return False, "关键词不能为空"
    folded = keyword.casefold()
    with _KEYWORDS_LOCK:
        if any(item.casefold() == folded for item in KEYWORDS):
            return False, f"「{html.escape(keyword)}」已存在"
        candidate = [*KEYWORDS, keyword]
        try:
            save_keywords(candidate)
        except OSError as exc:
            log.error("保存关键词失败: %s", exc)
            return False, "保存关键词失败，请稍后重试"
        KEYWORDS[:] = candidate
    return True, f"✅ 已添加「{html.escape(keyword)}」"


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


def del_keyword_at(index: int) -> tuple[bool, str]:
    with _KEYWORDS_LOCK:
        if index < 0 or index >= len(KEYWORDS):
            return False, "关键词列表已变化，请刷新面板"
        return _del_keyword_at_locked(index)


def list_keywords() -> str:
    keywords = keyword_snapshot()
    if not keywords:
        return "📋 当前没有关键词"
    lines = "\n".join(f"{index}. {html.escape(keyword)}" for index, keyword in enumerate(keywords, 1))
    return f"📋 当前关键词（{len(keywords)} 个）：\n{lines}"


def control_panel() -> tuple[str, dict[str, list[list[dict[str, str]]]]]:
    keywords = keyword_snapshot()
    keyword_text = "\n".join(f"{index}. {html.escape(word)}" for index, word in enumerate(keywords, 1))
    text = ("🛰 <b>NodeSeek 监控控制面板</b>\n\n"
            f"状态：✅ 运行中\n检查间隔：{POLL_INTERVAL} 秒\n"
            f"关键词：{len(keywords)} 个\n\n{keyword_text or '当前没有关键词'}\n\n"
            "点击关键词按钮可直接删除。")
    rows = [[{"text": "➕ 添加关键词", "callback_data": "panel:add"},
             {"text": "🔄 刷新", "callback_data": "panel:refresh"}]]
    buttons = [{"text": f"❌ {word[:10]}", "callback_data": f"kw:del:{index}"}
               for index, word in enumerate(keywords)]
    rows.extend(buttons[index:index + 4] for index in range(0, len(buttons), 4))
    rows.append([{"text": "❓ 帮助", "callback_data": "panel:help"}])
    return text, {"inline_keyboard": rows}


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


def _telegram_api_ok(response: requests.Response) -> bool:
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Telegram 返回非 JSON 响应") from exc
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API 错误: {str(payload.get('description', 'unknown'))[:200]}")
    return True


def tg_request(method: str, payload: dict[str, Any], retries: int = 3) -> bool:
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    for attempt in range(retries):
        try:
            with _TG_SEND_LOCK:
                response = _TG_SEND_SESSION.post(
                    url,
                    json=payload,
                    timeout=(5, 15),
                )
            if response.status_code == 429:
                try:
                    wait = max(1, min(int(response.json().get("parameters", {}).get("retry_after", 5)), 60))
                except (ValueError, TypeError):
                    wait = 5
                time.sleep(wait)
                continue
            _telegram_api_ok(response)
            return True
        except (requests.RequestException, RuntimeError) as exc:
            log.error("TG %s 失败(第%d次): %s", method, attempt + 1, exc)
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    return False


def send_tg(text: str, *, reply_markup: dict[str, Any] | None = None) -> bool:
    payload: dict[str, Any] = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return tg_request("sendMessage", payload)


def show_panel() -> bool:
    text, markup = control_panel()
    return send_tg(text, reply_markup=markup)


def edit_panel(message_id: int) -> bool:
    text, markup = control_panel()
    return tg_request("editMessageText", {
        "chat_id": TG_CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": markup,
    }, retries=1)


def answer_callback(callback_id: str, text: str = "", *, alert: bool = False) -> bool:
    payload: dict[str, Any] = {"callback_query_id": callback_id, "show_alert": alert}
    if text:
        payload["text"] = text[:200]
    return tg_request("answerCallbackQuery", payload, retries=1)


def register_bot_commands() -> bool:
    """为绑定私聊注册 Telegram 输入框旁的 / 命令菜单。"""
    commands = [
        {"command": "panel", "description": "打开关键词控制面板"},
        {"command": "add", "description": "添加关键词，例如 /add dmit"},
        {"command": "del", "description": "删除关键词，例如 /del dmit"},
        {"command": "list", "description": "查看当前关键词"},
        {"command": "help", "description": "查看使用说明"},
    ]
    scope = {"type": "chat", "chat_id": TG_CHAT_ID}
    commands_ok = tg_request("setMyCommands", {"commands": commands, "scope": scope})
    menu_ok = tg_request("setChatMenuButton", {
        "chat_id": TG_CHAT_ID,
        "menu_button": {"type": "commands"},
    })
    return commands_ok and menu_ok


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
        send_tg(list_keywords())
        return True
    if command == "/add":
        send_tg("\n".join(add_keyword(arg)[1] for arg in args) if args else "用法：/add 关键词1 关键词2 ...")
        return True
    if command == "/del":
        send_tg("\n".join(del_keyword(arg)[1] for arg in args) if args else "用法：/del 关键词")
        return True
    if command == "/help":
        send_tg("📖 <b>使用说明</b>\n\n发送 /panel 打开控制面板。\n"
                "点击“添加关键词”后直接发送关键词；点击关键词按钮可删除。\n\n"
                "兼容指令：/add、/del、/list")
        return True
    return False


def handle_message(text: str | None, chat_id: str) -> bool:
    text = (text or "").strip()
    mode = PENDING_INPUT.get(chat_id)
    if text.startswith("/"):
        PENDING_INPUT.pop(chat_id, None)
        return handle_command(text)
    if not mode or not text:
        return handle_command(text)
    PENDING_INPUT.pop(chat_id, None)
    words = text.split()
    if mode == "add":
        send_tg("\n".join(add_keyword(word)[1] for word in words))
        show_panel()
        return True
    return False


def handle_callback(data: str, callback_id: str, chat_id: str, message_id: int | None = None) -> bool:
    if data == "panel:add":
        PENDING_INPUT[chat_id] = "add"
        answer_callback(callback_id, "请发送关键词")
        send_tg("请输入要添加的关键词；多个关键词请用空格分隔。",
                reply_markup={"inline_keyboard": [[
                    {"text": "取消", "callback_data": "panel:cancel"}
                ]]})
        return True
    if data == "panel:cancel":
        PENDING_INPUT.pop(chat_id, None)
        answer_callback(callback_id, "已取消")
        return True
    if data == "panel:help":
        answer_callback(callback_id)
        send_tg("点击“添加关键词”后发送文字即可添加；点击带 ❌ 的关键词即可删除。")
        return True
    if data == "panel:refresh":
        answer_callback(callback_id, "已刷新")
        return edit_panel(message_id) if message_id is not None else show_panel()
    if data.startswith("kw:del:"):
        try:
            index = int(data.rsplit(":", 1)[1])
        except ValueError:
            answer_callback(callback_id, "无效操作", alert=True)
            return True
        ok, message = del_keyword_at(index)
        answer_callback(callback_id, re.sub(r"<[^>]+>", "", message), alert=not ok)
        if message_id is not None:
            edit_panel(message_id)
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
        handle_message(message.get("text"), chat_id)


def updates_loop() -> None:
    """getUpdates 长轮询线程：处理绑定用户的消息和内联按钮。"""
    offset: int | None = None
    while True:
        try:
            params: dict[str, int] = {"timeout": 25}
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


def _matching_alerts(posts: list[dict[str, Any]], last_id: int) -> list[tuple[str, str, str]]:
    keyword_values = keyword_snapshot()
    keywords = tuple(keyword.casefold() for keyword in keyword_values)
    blacklist = tuple(str(word).casefold() for word in BLACKLIST if str(word))
    alerts: list[tuple[str, str, str]] = []
    for post in posts:
        if post["id"] <= last_id:
            continue
        title = str(post.get("title", ""))
        folded_title = title.casefold()
        hit_index = next((i for i, keyword in enumerate(keywords) if keyword in folded_title), None)
        if hit_index is None or any(word in folded_title for word in blacklist):
            continue
        # 推送链接按约定始终使用帖子 ID 构造，避免 RSS 中异常链接进入 HTML 属性。
        url = f"https://www.nodeseek.com/post-{post['id']}-1"
        body = f"📌 标题：{html.escape(title)}\n🔗 <a href='{html.escape(url, quote=True)}'>查看帖子</a>"
        alerts.append((title, url, body))
        log.info("📢 命中: [%s] %s", keyword_values[hit_index], title)
    return alerts


def check_once(state: dict[str, int]) -> list[tuple[str, str, str]]:
    """检查一轮并更新内存游标；持久化和发送由调用者负责。"""
    posts = parse_posts(fetch_rss())
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


def run_once(state: dict[str, int]) -> bool:
    """完成一次抓取/发送/提交。发送失败时不推进磁盘游标，避免漏报。"""
    previous = dict(state)
    alerts = check_once(state)
    for _title, _url, body in alerts:
        if not send_tg(body):
            state.clear()
            state.update(previous)
            log.error("本轮存在未发送通知，游标未推进，稍后将重试")
            return False
        time.sleep(1)
    if state != previous:
        save_state(state)
    return True


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
            run_once(state)
        except Exception as exc:
            log.error("本轮失败: %s", exc)
        time.sleep(max(2.0, POLL_INTERVAL * (1 + random.uniform(-JITTER, JITTER))))


if __name__ == "__main__":
    main()
