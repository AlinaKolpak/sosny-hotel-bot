import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hotel_bot.__main__ import main
from hotel_bot.form import FormInterpreter
from hotel_bot.telegram import Telegram
import test_booking as fixtures


class FormTests(unittest.TestCase):
    setUp = fixtures.BookingTests.setUp
    tearDown = fixtures.BookingTests.tearDown
    send = fixtures.BookingTests.send
    count = fixtures.BookingTests.count

    def use_form(self):
        self.engine.interpreter = FormInterpreter()

    def test_booking_through_plain_answers_without_ai(self):
        self.use_form()
        for message in ["/start", "01.02.2027", "2027-02-03", "2", "2", "7,10", "1"]:
            reply = self.send(message)
        token = self.store.state("guest")["service_token"]
        self.send(f"/meal {token}:no")
        self.send(f"/extras {token}:done")
        reply = self.send("Анна Тестовая | +79990000000")
        self.assertIn("Проверьте бронь", reply["text"])
        self.assertEqual(self.store.state("guest")["fields"]["children_ages"], [7, 10])
        self.send(reply["buttons"][0]["data"])
        self.assertEqual(self.count(), 1)

    def test_bad_date_and_invalid_count_keep_current_step(self):
        self.use_form()
        self.send("31.02.2027")
        self.assertNotIn("check_in", self.store.state("guest")["fields"])
        self.send("завтра")
        self.assertNotIn("check_in", self.store.state("guest")["fields"])
        self.send("01.02.2027")
        self.send("03.02.2027")
        self.send("0")
        self.assertNotIn("adults", self.store.state("guest")["fields"])

    def test_without_children_and_faq_does_not_lose_fields(self):
        self.use_form()
        self.send("01.02.2027")
        self.send("03.02.2027")
        before = self.store.state("guest")["fields"]
        menu = self.send("/faq")
        self.send(menu["buttons"][0]["data"])
        self.assertEqual(self.store.state("guest")["fields"], before)
        self.send("2")
        self.send("0")
        self.assertTrue(self.store.state("guest")["offers"])
        self.assertEqual(self.store.state("guest")["fields"]["children_ages"], [])

    def test_yes_does_not_confirm_and_manager_stays_silent(self):
        self.use_form()
        self.send("/stay 2027-02-01 2027-02-03 2 -")
        self.send("1")
        self.send("Анна Тестовая | +79990000000")
        self.send("да")
        self.assertEqual(self.count(), 0)
        self.send("менеджер")
        self.assertIsNone(self.send("/start")["text"])

    def test_telegram_message_and_callback_journey(self):
        self.use_form()
        sent = []

        def transport(url, body, **kwargs):
            if url.endswith("/sendMessage"):
                sent.append(body)
            return {"ok": True, "result": {}}

        from hotel_bot.config import Settings
        bot = Telegram(Settings(telegram_token="test"), self.engine, transport)
        bot.bot_id = "123"
        event = 0

        def receive(text, callback=False):
            nonlocal event
            event += 1
            message = {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": text}
            update = {"update_id": event, "message": message}
            if callback:
                update = {"update_id": event, "callback_query": {
                    "id": str(event), "from": {"id": 42}, "data": text, "message": message}}
            bot.handle(update)
            bot.flush()

        for text in ["/start", "01.02.2027", "03.02.2027"]:
            receive(text)
        receive("/adults 2", True)
        receive("/children 0", True)
        receive(sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], True)
        receive(sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], True)
        receive(next(b["callback_data"] for row in sent[-1]["reply_markup"]["inline_keyboard"] for b in row if b["text"] == "Продолжить"), True)
        receive("Анна Тестовая | +79990000000")
        receive(sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], True)
        self.assertIn("DEMO-", sent[-1]["text"])
        self.assertEqual(self.count(), 1)
        receive(sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], True)
        self.assertIn("только после подтверждения", sent[-1]["text"])
        receive(sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], True)
        self.assertIn("отменена", sent[-1]["text"])
        self.assertEqual(self.store.db.execute("SELECT status FROM bookings").fetchone()[0], "CANCELLED")


class DefaultModeTests(unittest.TestCase):
    def test_telegram_default_never_constructs_ai(self):
        with tempfile.TemporaryDirectory() as temp:
            captured = []
            with patch("sys.argv", ["hotel_bot", "--db", str(Path(temp) / "test.db"), "telegram"]), \
                 patch("hotel_bot.config.load_env"), \
                 patch("hotel_bot.__main__.AIInterpreter", side_effect=AssertionError("ИИ не должен запускаться")), \
                 patch.object(Telegram, "run", lambda bot: captured.append(bot.engine.interpreter)), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            self.assertIsInstance(captured[0], FormInterpreter)


if __name__ == "__main__":
    unittest.main()
