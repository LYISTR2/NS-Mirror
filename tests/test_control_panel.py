import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "nodeseek_watcher.py"
spec = importlib.util.spec_from_file_location("watcher", MODULE_PATH)
watcher = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(watcher)


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True, "result": {}}


class ControlPanelTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        watcher.KW_FILE = str(Path(self.tempdir.name) / "keywords.json")
        watcher.KEYWORDS[:] = ["dmit", "vmiss"]
        watcher.PENDING_INPUT.clear()

    def test_panel_contains_inline_controls_and_keyword_buttons(self):
        text, markup = watcher.control_panel()
        self.assertIn("NodeSeek 监控控制面板", text)
        self.assertIn("dmit", text)
        rows = markup["inline_keyboard"]
        callbacks = [button.get("callback_data") for row in rows for button in row]
        self.assertIn("panel:add", callbacks)
        self.assertIn("panel:refresh", callbacks)
        self.assertIn("kw:del:0", callbacks)

    def test_add_flow_prompts_then_consumes_plain_text(self):
        sent = []
        with patch.object(watcher, "answer_callback", return_value=True), patch.object(
            watcher, "send_tg", side_effect=lambda text, **kwargs: sent.append((text, kwargs)) or True
        ):
            self.assertTrue(watcher.handle_callback("panel:add", "cb-add", "123"))
            self.assertEqual(watcher.PENDING_INPUT["123"], "add")
            self.assertTrue(watcher.handle_message("Alpha beta", "123"))
        self.assertEqual(watcher.keyword_snapshot(), ("dmit", "vmiss", "alpha", "beta"))
        self.assertNotIn("123", watcher.PENDING_INPUT)
        self.assertTrue(any("已添加" in text for text, _ in sent))

    def test_delete_callback_removes_keyword_by_index(self):
        with patch.object(watcher, "answer_callback", return_value=True), patch.object(
            watcher, "edit_panel", return_value=True
        ):
            self.assertTrue(watcher.handle_callback("kw:del:0", "cb-del", "123", message_id=9))
        self.assertEqual(watcher.keyword_snapshot(), ("vmiss",))

    def test_cancel_clears_pending_input(self):
        watcher.PENDING_INPUT["123"] = "add"
        with patch.object(watcher, "answer_callback", return_value=True):
            self.assertTrue(watcher.handle_callback("panel:cancel", "cb-cancel", "123"))
        self.assertNotIn("123", watcher.PENDING_INPUT)

    def test_command_cancels_pending_input_and_opens_panel(self):
        watcher.PENDING_INPUT["123"] = "add"
        with patch.object(watcher, "show_panel", return_value=True) as show:
            self.assertTrue(watcher.handle_message("/panel", "123"))
        self.assertNotIn("123", watcher.PENDING_INPUT)
        show.assert_called_once_with()

    def test_unauthorized_callback_is_acknowledged_without_mutation(self):
        watcher.TG_CHAT_ID = "123"
        with patch.object(watcher, "answer_callback", return_value=True) as answer:
            watcher.process_update(
                {
                    "update_id": 7,
                    "callback_query": {
                        "id": "bad",
                        "from": {"id": 999},
                        "data": "kw:del:0",
                        "message": {"chat": {"id": 123}, "message_id": 5},
                    },
                }
            )
        self.assertEqual(watcher.keyword_snapshot(), ("dmit", "vmiss"))
        answer.assert_called_once_with("bad", "无权操作", alert=True)

    def test_send_tg_passes_inline_keyboard(self):
        captured = {}

        def fake_post(_url, **kwargs):
            captured.update(kwargs["json"])
            return FakeResponse()

        with patch.object(watcher._TG_SEND_SESSION, "post", side_effect=fake_post):
            self.assertTrue(watcher.send_tg("panel", reply_markup={"inline_keyboard": []}))
        self.assertEqual(captured["reply_markup"], {"inline_keyboard": []})


if __name__ == "__main__":
    unittest.main()
