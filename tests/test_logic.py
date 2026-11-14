import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.logic import (  # noqa: E402
    BotState,
    mark_notified,
    on_failure,
    on_reported_offline,
    on_success,
    parse_online,
    render_template,
    unwrap_payload,
)


class LogicTests(unittest.TestCase):
    def test_threshold_not_reached(self):
        state = BotState()
        state, notify = on_failure(state, "timeout", 3, now=100)
        self.assertFalse(notify)
        self.assertTrue(state.online)
        self.assertEqual(state.fail_count, 1)

    def test_threshold_reached_notifies_once(self):
        state = BotState()
        for _ in range(2):
            state, notify = on_failure(state, "timeout", 3, now=100)
            self.assertFalse(notify)
        state, notify = on_failure(state, "timeout", 3, now=100)
        self.assertTrue(notify)
        self.assertFalse(state.online)

        state = mark_notified(state, 100)
        state, notify = on_failure(state, "timeout", 3, now=101)
        self.assertFalse(notify)
        self.assertFalse(state.online)

    def test_success_resets_notified(self):
        state = BotState(online=False, notified=True, fail_count=4)
        state = on_success(state, 200, nickname="bot", bot_id="123")
        self.assertTrue(state.online)
        self.assertFalse(state.notified)
        self.assertEqual(state.fail_count, 0)
        self.assertEqual(state.nickname, "bot")
        self.assertEqual(state.bot_id, "123")
        self.assertEqual(state.last_success, 200)

    def test_reported_offline_immediate(self):
        state = BotState()
        state, notify = on_reported_offline(state, "online=false", now=300)
        self.assertTrue(notify)
        self.assertFalse(state.online)
        state = mark_notified(state, 300)
        state, notify = on_reported_offline(state, "online=false", now=301)
        self.assertFalse(notify)

    def test_state_roundtrip(self):
        state = BotState(online=False, notified=True, fail_count=3, nickname="n")
        restored = BotState.from_dict(state.to_dict())
        self.assertEqual(restored, state)

    def test_render_template(self):
        text = render_template("{{a}} / {{b}}", {"a": "x", "b": "y"})
        self.assertEqual(text, "x / y")

    def test_render_missing_key_stays(self):
        self.assertEqual(render_template("{{a}} {{c}}", {"a": "x"}), "x {{c}}")

    def test_parse_online_plain(self):
        self.assertTrue(parse_online({"online": True}))
        self.assertFalse(parse_online({"online": False}))
        self.assertTrue(parse_online({"online": "true"}))
        self.assertFalse(parse_online({"online": "false"}))

    def test_parse_online_wrapped(self):
        payload = {"status": "ok", "retcode": 0, "data": {"online": True}}
        self.assertTrue(parse_online(payload))
        self.assertEqual(unwrap_payload(payload), {"online": True})

    def test_parse_online_bad_payload(self):
        self.assertFalse(parse_online(None))
        self.assertFalse(parse_online("oops"))


if __name__ == "__main__":
    unittest.main()
