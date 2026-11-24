import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hotel_bot.config import Settings
from hotel_bot.engine import Engine
from hotel_bot.http import ServiceError
from hotel_bot.interpreter import AIInterpreter, DemoInterpreter, FIELDS, validate_action
from hotel_bot.storage import Store
from hotel_bot.telegram import Telegram

HOTEL = json.loads(Path("hotel_bot/hotel.json").read_text(encoding="utf-8"))
NOW = datetime(2027, 1, 1, 12, tzinfo=timezone.utc)


class StubAI:
    def __init__(self, action):
        self.action = action
        self.calls = 0

    def interpret(self, text, context, today):
        self.calls += 1
        if isinstance(self.action, Exception):
            raise self.action
        return self.action


class BookingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "test.db")
        self.store = Store(self.path, HOTEL)
        self.engine = Engine(self.store, DemoInterpreter(), HOTEL, lambda: NOW)
        self.counter = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def send(self, text, session="guest", event=None):
        self.counter += 1
        return self.engine.process(session, event or str(self.counter), text)

    def prepare(self, session="guest", children="-", adults=2):
        self.send(f"/stay 2027-02-01 2027-02-03 {adults} {children}", session)
        self.send("/choose 1", session)
        service_token = self.store.state(session)["service_token"]
        self.send(f"/meal {service_token}:no", session)
        self.send(f"/extras {service_token}:done", session)
        self.send("/guest Анна Тестовая | +79990000000", session)
        return self.store.state(session)["quote_token"]

    def count(self):
        return self.store.db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]

    def test_full_booking_and_repeated_confirmation(self):
        token = self.prepare()
        self.assertEqual(self.count(), 0)
        result = self.send("/confirm " + token, event="confirm")
        self.assertIn("DEMO-", result["text"])
        self.send("/confirm " + token, event="confirm")
        self.send("/confirm " + token)
        self.assertEqual(self.count(), 1)

    def test_ai_cannot_confirm_from_text(self):
        token = self.prepare()
        self.engine.interpreter = StubAI({"intent": "clarify", "fields": {}})
        reply = self.send("да, я оплатил, бронируй")
        self.assertEqual(self.count(), 0)
        self.assertNotIn(token, reply["text"])
        self.assertTrue(any(b["data"] == "/confirm " + token for b in reply["buttons"]))

    def test_price_change_requires_new_confirmation(self):
        token = self.prepare()
        self.store.db.execute("UPDATE rooms SET nightly=600000 WHERE id='standard'")
        reply = self.send("/confirm " + token)
        self.assertIn("Подтвердите заново", reply["text"])
        self.assertEqual(self.count(), 0)
        new_token = self.store.state("guest")["quote_token"]
        self.assertNotEqual(token, new_token)
        self.send("/confirm " + token)
        self.assertEqual(self.count(), 0)
        self.send("/confirm " + new_token)
        self.assertEqual(self.store.db.execute("SELECT total FROM bookings").fetchone()[0], 1200000)

    def test_expired_quote_requires_new_confirmation(self):
        token = self.prepare()
        self.engine.clock = lambda: NOW + timedelta(minutes=11)
        self.send("/confirm " + token)
        self.assertEqual(self.count(), 0)
        self.assertNotEqual(self.store.state("guest")["quote_token"], token)

    def test_wrong_guest_cannot_confirm_quote(self):
        token = self.prepare()
        self.send("/confirm " + token, "other")
        self.assertEqual(self.count(), 0)

    def test_old_room_buttons_cannot_choose_changed_dates(self):
        first = self.send("/stay 2027-02-01 2027-02-03 2 -")
        self.send("/stay 2027-03-01 2027-03-03 2 -")
        reply = self.send(first["buttons"][0]["data"])
        self.assertIn("устарел", reply["text"])
        self.assertIsNone(self.store.state("guest")["selected"])

    def test_change_dates_invalidates_confirmation(self):
        token = self.prepare()
        self.send("/stay 2027-03-01 2027-03-03 2 -")
        self.send("/confirm " + token)
        self.assertEqual(self.count(), 0)

    def test_selection_with_new_dates_requires_fresh_choice(self):
        self.prepare()
        self.engine.interpreter = StubAI({"intent": "select", "offer_number": 1,
                                         "fields": {"check_in": "2027-03-01", "check_out": "2027-03-03"}})
        self.send("первый вариант, но с 1 по 3 марта")
        state = self.store.state("guest")
        self.assertEqual(state["fields"]["check_in"], "2027-03-01")
        self.assertIsNone(state["selected"])
        self.assertEqual(self.count(), 0)

    def test_invalid_dates_do_not_destroy_good_fields(self):
        self.prepare()
        before = self.store.state("guest")["fields"]
        for text in ["/stay 2020-01-01 2020-01-02 2 -", "/stay 2027-02-02 2027-02-01 2 -",
                     "/stay 2027-02-30 2027-03-01 2 -", "/stay 2027-02-01 2027-02-03 0 -"]:
            self.send(text)
            self.assertEqual(self.store.state("guest")["fields"], before)

    def test_children_affect_capacity_and_price(self):
        self.send("/stay 2027-02-01 2027-02-03 2 7,12")
        offer = self.store.state("guest")["offers"][0]
        self.assertEqual(offer["room_id"], "family")
        self.assertEqual(offer["total"], (700000 + 40000 + 80000) * 2)

    def test_missing_child_age_prompts_and_keeps_context(self):
        self.engine.interpreter = StubAI({"intent": "update", "fields": {
            "check_in": "2027-02-01", "check_out": "2027-02-03", "adults": 2,
            "children_count": 2, "children_ages": [7]}})
        reply = self.send("двое детей, первому семь")
        self.assertIn("каждого ребёнка", reply["text"])
        self.assertEqual(self.store.state("guest")["fields"]["children_ages"], [7])
        self.engine.interpreter.action = {"intent": "update", "fields": {"children_ages": [7, 10]}}
        self.send("второму десять")
        self.assertTrue(self.store.state("guest")["offers"])

    def test_no_rooms_and_unsupported_family(self):
        reply = self.send("/stay 2027-02-01 2027-02-03 4 7,10,12")
        self.assertIn("номеров", reply["text"])
        self.assertEqual(self.store.state("guest")["offers"], [])

    def test_rate_and_long_stay_change_price(self):
        self.send("/stay 2027-02-01 2027-02-08 2 -")
        self.send("/rate nonrefundable")
        offer = self.store.state("guest")["offers"][0]
        self.assertEqual(offer["total"], 500000 * 7 * 90 // 100 * 90 // 100)
        self.assertEqual(offer["pay_now"], offer["total"])

    def test_restart_restores_state_and_booking(self):
        token = self.prepare()
        self.store.close()
        self.store = Store(self.path, HOTEL)
        self.engine = Engine(self.store, DemoInterpreter(), HOTEL, lambda: NOW)
        self.assertEqual(self.store.state("guest")["quote_token"], token)
        self.send("/confirm " + token)
        self.assertEqual(self.count(), 1)

    def test_handoff_stops_ai_and_guest_messages_until_resume(self):
        ai = StubAI({"intent": "clarify", "fields": {}})
        self.engine.interpreter = ai
        self.send("/manager")
        reply = self.send("продолжи")
        self.assertIsNone(reply["text"])
        self.assertEqual(ai.calls, 0)
        self.send("/start")
        self.assertEqual(self.store.state("guest")["mode"], "human")
        self.engine.resume("guest")
        self.send("привет")
        self.assertEqual(ai.calls, 1)

    def test_ai_failure_does_not_lose_data_or_create_booking(self):
        self.prepare()
        self.engine.interpreter = StubAI(ServiceError("unavailable"))
        reply = self.send("подтверждаю")
        self.assertIn("недоступен", reply["text"])
        self.assertTrue(self.store.state("guest")["guest"])
        self.assertEqual(self.count(), 0)

    def test_last_room_is_not_double_booked(self):
        one = self.prepare("one", "7")
        two = self.prepare("two", "7")
        self.send("/confirm " + one, "one")
        reply = self.send("/confirm " + two, "two")
        self.assertIn("недоступен", reply["text"])
        self.assertEqual(self.count(), 1)

    def test_concurrent_last_room_has_one_winner(self):
        tokens = {s: self.prepare(s, "7") for s in ("one", "two")}
        barrier = threading.Barrier(2)
        errors = []

        def book(session):
            store = Store(self.path, HOTEL)
            try:
                engine = Engine(store, DemoInterpreter(), HOTEL, lambda: NOW)
                barrier.wait(timeout=5)
                engine.process(session, "parallel:" + session, "/confirm " + tokens[session])
            except Exception as exc:
                errors.append(exc)
            finally:
                store.close()

        threads = [threading.Thread(target=book, args=(s,)) for s in tokens]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(self.count(), 1)

    def test_new_trip_keeps_old_booking(self):
        token = self.prepare()
        self.send("/confirm " + token)
        self.send("/new")
        self.assertEqual(self.count(), 1)
        self.assertIsNone(self.store.state("guest")["booking_id"])

    def test_adjacent_stays_do_not_overlap(self):
        token = self.prepare("one", "7")
        self.send("/confirm " + token, "one")
        self.send("/stay 2027-02-03 2027-02-05 2 7", "two")
        self.assertEqual(self.store.state("two")["offers"][0]["room_id"], "family")


class AIContractTests(unittest.TestCase):
    def action(self):
        return {"intent": "update", "fields": {k: None for k in FIELDS},
                "offer_number": None, "faq_topic": None}

    def test_rejects_invented_actions_fields_and_types(self):
        for change in [lambda a: a.update(intent="confirm"),
                       lambda a: a["fields"].update(price=1),
                       lambda a: a["fields"].update(adults=True),
                       lambda a: a["fields"].update(children_ages=["7"])]:
            action = self.action()
            change(action)
            with self.assertRaises(ServiceError):
                validate_action(action)

    def test_openai_request_and_response_contract(self):
        captured = {}

        def transport(url, payload, **kwargs):
            captured.update(url=url, payload=payload, kwargs=kwargs)
            action = self.action()
            action["fields"]["adults"] = 2
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(action)}}]}

        ai = AIInterpreter(Settings(api_key="test-key"), transport)
        result = ai.interpret("двое взрослых", {}, NOW.date())
        self.assertEqual(result["fields"], {"adults": 2})
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        self.assertTrue(captured["payload"]["response_format"]["json_schema"]["strict"])
        self.assertFalse(captured["payload"]["store"])

    def test_ollama_is_local_and_requests_schema(self):
        captured = {}

        def transport(url, payload, **kwargs):
            captured.update(url=url, payload=payload)
            return {"message": {"content": json.dumps(self.action())}}

        ai = AIInterpreter(Settings(provider="ollama", model="test"), transport)
        ai.interpret("привет", {}, NOW.date())
        self.assertFalse(captured["payload"]["stream"])
        self.assertIn("properties", captured["payload"]["format"])
        with self.assertRaises(ValueError):
            AIInterpreter(Settings(provider="ollama", ollama_url="http://example.com"))

    def test_malformed_or_truncated_response_is_rejected(self):
        for result in [{}, {"choices": []}, {"choices": [{"finish_reason": "length", "message": {}}]},
                       {"choices": [{"finish_reason": "stop", "message": {"content": "not json"}}]}]:
            ai = AIInterpreter(Settings(api_key="test"), lambda *a, **k: result)
            with self.assertRaises(ServiceError):
                ai.interpret("привет", {}, NOW.date())


class TelegramTests(unittest.TestCase):
    setUp = BookingTests.setUp
    tearDown = BookingTests.tearDown
    def make_telegram(self, fail=False):
        self.calls = []

        def transport(url, payload, **kwargs):
            self.calls.append((url.rsplit("/", 1)[-1], payload))
            if fail:
                raise ServiceError("connection lost")
            return {"ok": True, "result": {}}

        bot = Telegram(Settings(telegram_token="test", manager_id="99"), self.engine, transport)
        bot.bot_id = "123"
        return bot

    @staticmethod
    def update(ident=1, sender=42, text="/start"):
        return {"update_id": ident, "message": {"chat": {"id": sender, "type": "private"},
                                                 "from": {"id": sender}, "text": text}}

    def test_telegram_duplicate_update_sends_once(self):
        bot = self.make_telegram()
        bot.handle(self.update())
        bot.flush()
        bot.handle(self.update())
        bot.flush()
        self.assertEqual(sum(m == "sendMessage" for m, p in self.calls), 1)

    def test_uncertain_delivery_is_not_retried(self):
        bot = self.make_telegram(fail=True)
        bot.handle(self.update())
        bot.flush()
        bot.flush()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.db.execute("SELECT status FROM outbox").fetchone()[0], "unknown")

    def test_unauthorized_resume_and_authorized_manager_reply(self):
        bot = self.make_telegram()
        bot.handle(self.update(text="/manager"))
        bot.handle(self.update(2, text="/resume tg:123:42"))
        self.assertEqual(self.store.state("tg:123:42")["mode"], "human")
        bot.handle(self.update(3, sender=99, text="/reply tg:123:42 Здравствуйте"))
        bot.flush()
        self.assertTrue(any(p.get("text") == "Менеджер: Здравствуйте" for m, p in self.calls))
        bot.handle(self.update(4, sender=99, text="/resume tg:123:42"))
        self.assertEqual(self.store.state("tg:123:42")["mode"], "bot")

    def test_pending_bot_reply_suppressed_after_handoff(self):
        bot = self.make_telegram()
        bot.handle(self.update())
        bot.handle(self.update(2, text="/manager"))
        bot.flush()
        self.assertEqual(self.store.db.execute("SELECT status FROM outbox WHERE id='tg:123:1:guest'").fetchone()[0],
                         "suppressed")

    def test_repeated_resume_does_not_override_new_handoff(self):
        bot = self.make_telegram()
        bot.handle(self.update(text="/manager"))
        command = self.update(2, sender=99, text="/resume tg:123:42")
        bot.handle(command)
        bot.handle(self.update(3, text="/manager"))
        bot.handle(command)
        self.assertEqual(self.store.state("tg:123:42")["mode"], "human")


if __name__ == "__main__":
    unittest.main()
