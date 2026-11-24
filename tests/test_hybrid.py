import contextlib
import io
import json
import unittest
from unittest.mock import patch

import test_booking as fixtures
from hotel_bot.__main__ import main
from hotel_bot.config import Settings
from hotel_bot.hybrid import HybridInterpreter
from hotel_bot.http import ServiceError
from hotel_bot.interpreter import AIInterpreter, FIELDS
from hotel_bot.evaluate import evaluate


class HybridTests(unittest.TestCase):
    setUp = fixtures.BookingTests.setUp
    tearDown = fixtures.BookingTests.tearDown
    send = fixtures.BookingTests.send
    prepare = fixtures.BookingTests.prepare

    def ai(self, changes=None, fail=False, malformed=False):
        self.calls = []

        def transport(url, payload, **kwargs):
            self.calls.append((payload, kwargs))
            if fail:
                raise ServiceError("timeout")
            action = {"intent": "update", "fields": {key: None for key in FIELDS},
                      "offer_number": None, "faq_topic": None}
            action["fields"].update(changes or {})
            return {"choices": [{"finish_reason": "stop", "message": {
                "content": "invalid json" if malformed else json.dumps(action)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                          "prompt_tokens_details": {"cached_tokens": 30}}}

        return AIInterpreter(Settings(api_key="test", timeout=15), transport)

    def test_simple_answers_and_invalid_calendar_date_do_not_call_ai(self):
        hybrid = HybridInterpreter(self.ai())
        self.engine.interpreter = hybrid
        self.send("31.02.2027")
        self.assertEqual(self.store.state("guest")["fields"], {})
        self.send("01.02.2027")
        self.send("03.02.2027")
        self.send("2")
        self.send("0")
        self.assertEqual(self.calls, [])
        self.assertEqual(hybrid.last_metrics["api_calls"], 0)

    def test_free_text_updates_fields_and_usage_is_measured(self):
        ai = self.ai({"check_in": "2027-02-01", "check_out": "2027-02-03", "adults": 2})
        self.engine.interpreter = HybridInterpreter(ai)
        reply = self.send("Двое взрослых, с 1 по 3 февраля 2027")
        self.assertIn("детей", reply["text"])
        self.assertEqual(self.store.state("guest")["fields"]["adults"], 2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(ai.last_metrics["input_tokens"], 100)
        self.assertEqual(ai.last_metrics["cached_input_tokens"], 30)
        self.assertEqual(ai.last_metrics["output_tokens"], 20)
        self.assertTrue(ai.last_metrics["ok"])
        self.assertEqual(self.calls[0][1]["timeout"], 15)

    def test_timeout_falls_back_with_next_question_and_keeps_data(self):
        self.send("/stay 2027-02-01 2027-02-03 2 -")
        before = self.store.state("guest")["fields"]
        self.engine.interpreter = HybridInterpreter(self.ai(fail=True))
        reply = self.send("Давайте на неделю позже")
        self.assertIn("по анкете", reply["text"])
        self.assertTrue(reply["buttons"])
        self.assertEqual(self.store.state("guest")["fields"], before)
        self.assertEqual(self.engine.interpreter.last_metrics["api_calls"], 1)
        self.assertIsNone(self.engine.interpreter.last_metrics["input_tokens"])
        self.send("/edit dates")
        self.send("08.02.2027")
        self.send("10.02.2027")
        self.assertEqual(self.store.state("guest")["fields"]["check_in"], "2027-02-08")
        self.assertEqual(len(self.calls), 1)

    def test_malformed_result_falls_back_but_retains_usage(self):
        hybrid = HybridInterpreter(self.ai(malformed=True))
        action = hybrid.interpret("двое взрослых", {"fields": {}, "offers": []}, fixtures.NOW.date())
        self.assertTrue(action["fallback"])
        self.assertEqual(hybrid.last_metrics["output_tokens"], 20)
        self.assertFalse(hybrid.last_metrics["ok"])

    def test_contacts_confirmations_and_handoff_bypass_ai(self):
        self.prepare()
        self.engine.interpreter = HybridInterpreter(self.ai())
        self.send("Новая Гостья | +79991111111")
        self.send("да")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM bookings").fetchone()[0], 0)
        self.send("/manager")
        self.send("двое взрослых")
        self.assertEqual(self.calls, [])

    def test_offline_hybrid_never_constructs_api_client(self):
        captured = []
        with patch("sys.argv", ["hotel_bot", "--db", ":memory:", "telegram", "--hybrid-offline"]), \
             patch("hotel_bot.config.load_env"), \
             patch("hotel_bot.__main__.AIInterpreter", side_effect=AssertionError("No API client")), \
             patch("hotel_bot.__main__.Telegram.run", lambda bot: captured.append(bot.engine.interpreter)), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        self.assertIsInstance(captured[0], HybridInterpreter)
        self.assertIsNone(captured[0].ai)

    def test_offline_corpus_is_not_reported_as_ai_quality(self):
        with patch("hotel_bot.interpreter.request_json", side_effect=AssertionError("No network")):
            report = evaluate()
        self.assertEqual(report["cases"], 26)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["api_calls"], 0)
        self.assertEqual(report["ai_quality"], "NOT_EVALUATED")
        self.assertIsNone(report["api_median_ms"])


if __name__ == "__main__":
    unittest.main()
