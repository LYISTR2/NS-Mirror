import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).parents[1] / "nodeseek_watcher.py"
spec = importlib.util.spec_from_file_location("watcher", MODULE_PATH)
watcher = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(watcher)

MDV2_SPECIAL = "_*[]()~`>#+-=|{}.!"


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True, "result": {}}


class MarkdownV2EscapeTests(unittest.TestCase):
    def test_escape_covers_every_reserved_character(self):
        escaped = watcher._mdv2_escape(MDV2_SPECIAL)
        for char in MDV2_SPECIAL:
            self.assertIn("\\" + char, escaped)
        self.assertEqual(escaped, "\\_\\*\\[\\]\\(\\)\\~\\`\\>\\#\\+\\-\\=\\|\\{\\}\\.\\!")

    def test_escape_keeps_plain_text_and_unicode(self):
        # `-` 是 MarkdownV2 保留字符，出现在日期中也会被正确转义
        self.assertEqual(watcher._mdv2_escape("DeepSeek V4 额度 2026-08-07"), "DeepSeek V4 额度 2026\\-08\\-07")
        self.assertEqual(watcher._mdv2_escape("日本語タイトル"), "日本語タイトル")
        self.assertEqual(watcher._mdv2_escape(""), "")

    def test_escape_url_handles_backslash_and_right_paren(self):
        self.assertEqual(
            watcher._mdv2_escape_url("https://example.com/path(a)\\b"),
            "https://example.com/path(a\\)\\\\b",
        )
        # 普通帖子 URL 不受影响
        self.assertEqual(
            watcher._mdv2_escape_url("https://www.nodeseek.com/post-852452-1"),
            "https://www.nodeseek.com/post-852452-1",
        )

    def test_escape_code_escapes_backslash_and_backtick(self):
        # code 实体内容：反斜杠 → 双反斜杠，反引号 → 前置反斜杠；其余保留字符无需转义
        self.assertEqual(watcher._mdv2_escape_code("a\\b`c"), "a\\\\b\\`c")
        self.assertEqual(watcher._mdv2_escape_code("c++ [_]"), "c++ [_]")
        self.assertEqual(watcher._mdv2_escape_code(""), "")

    def test_format_pub_date_converts_to_beijing_time(self):
        # NodeSeek RSS 的 GMT 时间转换为北京时间（UTC+8）
        self.assertEqual(
            watcher._format_pub_date("Fri, 07 Aug 2026 07:41:56 GMT"),
            "2026-08-07 15:41",
        )
        # +0800 的本地时间即北京时间，墙钟保持不变
        self.assertEqual(
            watcher._format_pub_date("Fri, 07 Aug 2026 15:30:00 +0800"),
            "2026-08-07 15:30",
        )
        # 无时区信息的时间按 UTC 处理
        self.assertEqual(
            watcher._format_pub_date("Fri, 07 Aug 2026 07:41:56"),
            "2026-08-07 15:41",
        )

    def test_format_pub_date_falls_back_to_raw_text(self):
        self.assertEqual(watcher._format_pub_date("not-a-date"), "not-a-date")
        self.assertEqual(watcher._format_pub_date(""), "")


class AlertTemplateTests(unittest.TestCase):
    def test_new_post_template_matches_required_visual(self):
        body = watcher.alert_body_markdownv2(
            852452,
            "DeepSeek V4 Flash 额度与价格调整讨论",
            "https://www.nodeseek.com/post-852452-1",
            "deepseek",
            "Fri, 07 Aug 2026 15:30:00 +0800",
        )
        self.assertEqual(body, (
            "*NS / NEW POST　№ 852452*\n"
            "\n"
            "[DeepSeek V4 Flash 额度与价格调整讨论](https://www.nodeseek.com/post-852452-1)\n"
            "\n"
            "> 命中关键词：`deepseek`\n"
            "> 发布时间：`2026-08-07 15:30`"
        ))

    def test_special_characters_in_title_keyword_and_time_are_safe(self):
        title = "C++ vs C#：100$ (折扣) [热帖] #2026 {限时} | 已结束！"
        body = watcher.alert_body_markdownv2(
            1, title, "https://x.com/p(1)", "c++", "bad-date",
        )
        # 标题中的 MarkdownV2 保留字符全部被转义，不会破坏链接实体
        self.assertIn("C\\+\\+ vs C\\#", body)
        # `$` 不是 MarkdownV2 保留字符，保持原样
        self.assertIn("100$", body)
        self.assertIn("\\[热帖\\]", body)
        self.assertIn("\\#2026", body)
        self.assertIn("\\{限时\\}", body)
        # 全角感叹号不是保留字符，保持原样
        self.assertIn("已结束！", body)
        # URL 中只需转义右括号（与反斜杠），左括号保持原样
        self.assertIn("https://x.com/p(1\\)", body)
        # 关键词与解析失败的时间放在 code 块内展示；不含反斜杠/反引号时保持字面，
        # 含反斜杠/反引号时按 _mdv2_escape_code 转义（见下方专项用例）
        self.assertIn("> 命中关键词：`c++`", body)
        self.assertIn("> 发布时间：`bad-date`", body)

    def test_code_entities_escape_backslash_and_backtick_in_keyword_and_time(self):
        body = watcher.alert_body_markdownv2(
            2, "标题", "https://x.com/2", "c\\++`x", "bad\\time`!",
        )
        # 关键词与（解析失败的）时间中的反斜杠、反引号都被正确转义，不会破坏 code 实体
        self.assertIn("> 命中关键词：`c\\\\++\\`x`", body)
        self.assertIn("> 发布时间：`bad\\\\time\\`!`", body)


class MatchingAlertsTests(unittest.TestCase):
    def setUp(self):
        watcher.KEYWORDS[:] = ["deepseek"]
        watcher.BLACKLIST[:] = []

    def test_alert_body_contains_keyword_and_beijing_time(self):
        posts = [{
            "id": 852452,
            "title": "DeepSeek V4 Flash 额度调整",
            "url": "https://www.nodeseek.com/post-852452-1",
            "time": "Fri, 07 Aug 2026 15:30:00 +0800",
        }]
        alerts = watcher._matching_alerts(posts, last_id=0)
        self.assertEqual(len(alerts), 1)
        title, url, body = alerts[0]
        self.assertEqual(title, "DeepSeek V4 Flash 额度调整")
        self.assertEqual(url, "https://www.nodeseek.com/post-852452-1")
        self.assertIn("*NS / NEW POST　№ 852452*", body)
        self.assertIn("> 命中关键词：`deepseek`", body)
        self.assertIn("> 发布时间：`2026-08-07 15:30`", body)

    def test_posts_at_or_below_cursor_produce_no_alerts(self):
        posts = [{"id": 5, "title": "deepseek 降价", "url": "", "time": ""}]
        self.assertEqual(watcher._matching_alerts(posts, last_id=5), [])
        self.assertEqual(watcher._matching_alerts(posts, last_id=9), [])

    def test_non_matching_title_produces_no_alert(self):
        posts = [{"id": 6, "title": "无关帖子", "url": "", "time": ""}]
        self.assertEqual(watcher._matching_alerts(posts, last_id=0), [])


class SendTgParseModeTests(unittest.TestCase):
    def _capture(self):
        captured = {}

        def fake_post(_url, **kwargs):
            captured.update(kwargs["json"])
            return FakeResponse()

        return captured, fake_post

    def test_default_parse_mode_is_html(self):
        captured, fake_post = self._capture()
        with patch.object(watcher._TG_SEND_SESSION, "post", side_effect=fake_post):
            self.assertTrue(watcher.send_tg("hello <b>world</b>"))
        self.assertEqual(captured["parse_mode"], "HTML")

    def test_parse_mode_can_be_overridden_to_markdownv2(self):
        captured, fake_post = self._capture()
        with patch.object(watcher._TG_SEND_SESSION, "post", side_effect=fake_post):
            self.assertTrue(watcher.send_tg("*bold*", parse_mode="MarkdownV2"))
        self.assertEqual(captured["parse_mode"], "MarkdownV2")

    def test_control_panel_still_sent_as_html(self):
        captured, fake_post = self._capture()
        with patch.object(watcher._TG_SEND_SESSION, "post", side_effect=fake_post):
            watcher.show_panel()
        self.assertEqual(captured["parse_mode"], "HTML")
        self.assertIn("<b>", captured["text"])


class RunOnceMarkdownV2Tests(unittest.TestCase):
    def setUp(self):
        watcher.KEYWORDS[:] = ["deepseek"]

    def test_run_once_sends_alert_as_markdownv2_and_advances_cursor(self):
        state = {"last_max_id": 100}
        posts = [{
            "id": 101,
            "title": "DeepSeek 新帖",
            "url": "x",
            "time": "Fri, 07 Aug 2026 15:30:00 +0800",
        }]
        sent = []
        with patch.object(watcher, "fetch_rss", return_value=b"<rss/>"), \
             patch.object(watcher, "parse_posts", return_value=posts), \
             patch.object(watcher, "send_tg",
                          side_effect=lambda text, **kwargs: sent.append((text, kwargs)) or True), \
             patch.object(watcher, "save_state", return_value=None):
            self.assertTrue(watcher.run_once(state))
        self.assertEqual(len(sent), 1)
        text, kwargs = sent[0]
        self.assertEqual(kwargs["parse_mode"], "MarkdownV2")
        self.assertIn("*NS / NEW POST　№ 101*", text)
        self.assertEqual(state["last_max_id"], 101)

    def test_run_once_rolls_back_cursor_when_alert_send_fails(self):
        state = {"last_max_id": 100}
        posts = [{"id": 101, "title": "DeepSeek 新帖", "url": "x", "time": ""}]
        with patch.object(watcher, "fetch_rss", return_value=b"<rss/>"), \
             patch.object(watcher, "parse_posts", return_value=posts), \
             patch.object(watcher, "send_tg", return_value=False):
            self.assertFalse(watcher.run_once(state))
        # 发送失败不推进磁盘游标（原有回滚逻辑保持不变）
        self.assertEqual(state, {"last_max_id": 100})


if __name__ == "__main__":
    unittest.main()
