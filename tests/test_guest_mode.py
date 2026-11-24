import unittest

import test_booking as fixtures
import test_manager as manager_fixtures
from hotel_bot.form import FormInterpreter
from hotel_bot.telegram import Telegram


class GuestModeTests(unittest.TestCase):
    setUp = fixtures.BookingTests.setUp
    tearDown = fixtures.BookingTests.tearDown
    bot = manager_fixtures.ManagerTests.bot
    update = staticmethod(fixtures.TelegramTests.update)

    def test_manager_can_book_edit_cancel_as_separate_test_guest(self):
        self.engine.interpreter = FormInterpreter()
        bot = self.bot("99")
        counter = 0

        def send(text):
            nonlocal counter
            counter += 1
            bot.handle(self.update(counter, 99, text))
            bot.flush()

        session = "tg:123:99:test"
        send("/guest_mode")
        for text in ["01.02.2027", "03.02.2027", "2", "1", "7", "1"]:
            send(text)
        token = self.store.state(session)["service_token"]
        send(f"/meal {token}:yes")
        send(f"/extras {token}:fruit")
        send(f"/extras {token}:done")
        send("Анна Тестовая | +79990000000")
        old_token = self.store.state(session)["quote_token"]
        send("/edit guest")
        send("Мария Тестовая | +79991111111")
        send("/confirm " + old_token)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM bookings").fetchone()[0], 0)
        send("/confirm " + self.store.state(session)["quote_token"])
        ident = self.store.state(session)["booking_id"]
        send("/cancel " + ident)
        send("/cancel_confirm " + self.store.state(session)["cancel_pending"]["token"])
        self.assertEqual(self.store.db.execute("SELECT status FROM bookings").fetchone()[0], "CANCELLED")
        self.assertTrue(all(p["chat_id"] == "99" for p in self.sent))
        self.assertFalse(self.store.state("tg:123:99")["fields"])
        send("/manager_mode")
        self.assertIn("Вы — менеджер", self.sent[-1]["text"])
        self.assertTrue(any("НОВАЯ БРОНЬ" in p["text"] for p in self.sent))
        self.assertTrue(any("ОТМЕНА БРОНИ" in p["text"] for p in self.sent))

    def test_guest_mode_is_manager_only_and_duplicate_does_not_switch_back(self):
        bot = self.bot("99")
        bot.handle(self.update(1, 42, "/guest_mode"))
        self.assertEqual(self.store.meta("guest_mode:123:42", "0"), "0")
        enter = self.update(2, 99, "/guest_mode")
        bot.handle(enter)
        bot.handle(self.update(3, 99, "/manager_mode"))
        bot.handle(enter)
        self.assertEqual(self.store.meta("guest_mode:123:99"), "0")

    def test_test_guest_handoff_reply_and_resume(self):
        bot = self.bot("99")
        bot.handle(self.update(1, 99, "/guest_mode"))
        bot.handle(self.update(2, 99, "/manager"))
        bot.flush()
        self.assertEqual(self.store.state("tg:123:99:test")["mode"], "human")
        bot.handle(self.update(3, 99, "/reply tg:123:99:test Проверка ответа"))
        bot.flush()
        self.assertTrue(any("Менеджер: Проверка ответа" in p["text"] and p["chat_id"] == "99" for p in self.sent))
        bot.handle(self.update(4, 99, "/resume tg:123:99:test"))
        self.assertEqual(self.store.state("tg:123:99:test")["mode"], "bot")
        restarted = Telegram(bot.settings, self.engine, bot.transport)
        restarted.bot_id = "123"
        restarted.handle(self.update(5, 99, "/start"))
        self.assertEqual(self.store.meta("guest_mode:123:99"), "1")


if __name__ == "__main__":
    unittest.main()
