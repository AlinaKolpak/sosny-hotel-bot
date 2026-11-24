import json
import unittest
from unittest.mock import patch

import test_booking as fixtures
from hotel_bot.config import Settings
from hotel_bot.telegram import Telegram


class ManagerTests(unittest.TestCase):
    setUp = fixtures.BookingTests.setUp
    tearDown = fixtures.BookingTests.tearDown
    update = staticmethod(fixtures.TelegramTests.update)

    def bot(self, manager=""):
        self.sent = []

        def transport(url, payload, **kwargs):
            if url.endswith("/getMe"):
                return {"ok": True, "result": {"id": 123, "username": "test_bot"}}
            if url.endswith("/sendMessage"):
                self.sent.append(payload)
            return {"ok": True, "result": {"message_id": len(self.sent)}}

        bot = Telegram(Settings(manager_id=manager), self.engine, transport)
        bot.bot_id = "123"
        return bot

    def test_pairing_is_one_time_persisted_and_not_sent_to_engine(self):
        bot = self.bot()
        link = bot.manager_link()
        command = "/start " + link.split("start=")[1]
        bot.handle(self.update(1, 99, command))
        self.assertEqual(bot.manager_id, "99")
        bot.handle(self.update(2, 42, command))
        self.assertEqual(bot.manager_id, "99")
        self.assertEqual(Telegram(Settings(), self.engine).store.meta("manager_id:123"), "99")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        self.assertEqual(self.store.meta("manager_pair:123", ""), "")
        with self.assertRaises(ValueError):
            bot.manager_link()

    def test_invalid_expired_or_group_link_does_not_bind(self):
        bot = self.bot()
        with patch("hotel_bot.telegram.time.time", return_value=100):
            command = "/start " + bot.manager_link().split("start=")[1]
        bot.handle(self.update(1, 42, "/start manager_invalid"))
        self.assertFalse(bot.manager_id)
        with patch("hotel_bot.telegram.time.time", return_value=200):
            update = self.update(2, 42, command)
            update["message"]["chat"]["type"] = "group"
            bot.handle(update)
            self.assertFalse(bot.manager_id)
        with patch("hotel_bot.telegram.time.time", return_value=1901):
            bot.handle(self.update(3, 42, command))
        self.assertFalse(bot.manager_id)

    def test_reply_targets_notification_and_duplicates_send_once(self):
        bot = self.bot("99")
        bot.handle(self.update(1, 42, "/manager"))
        bot.flush()
        notification = self.store.db.execute("SELECT message_id FROM outbox WHERE id='tg:123:1:manager'").fetchone()[0]
        # Another guest must not change the destination of a reply to the first notification.
        bot.handle(self.update(2, 43, "/manager"))
        bot.flush()
        reply = self.update(3, 99, "Здравствуйте, я помогу")
        reply["message"]["reply_to_message"] = {"message_id": notification, "from": {"id": 123}}
        bot.handle(reply)
        bot.flush()
        bot.handle(reply)
        bot.flush()
        answers = [p for p in self.sent if p["text"] == "Менеджер: Здравствуйте, я помогу"]
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["chat_id"], "42")
        self.assertTrue(any(p["chat_id"] == "99" and p["text"] == "Ответ гостю отправлен." for p in self.sent))

    def test_inbox_opens_existing_handoffs_and_resume_blocks_late_reply(self):
        bot = self.bot("99")
        bot.handle(self.update(1, 42, "/manager"))
        bot.handle(self.update(2, 99, "/inbox"))
        bot.flush()
        menu = next(p for p in self.sent if p["text"].startswith("Вы — менеджер"))
        self.assertEqual(menu["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "/dialog tg:123:42")
        bot.handle(self.update(3, 99, "/dialog tg:123:42"))
        bot.flush()
        bot.handle(self.update(4, 99, "/resume tg:123:42"))
        bot.handle(self.update(5, 99, "/reply tg:123:42 поздний ответ"))
        bot.flush()
        self.assertFalse(any(p["text"] == "Менеджер: поздний ответ" for p in self.sent))
        self.assertEqual(self.store.state("tg:123:42")["mode"], "bot")

    def test_non_manager_cannot_open_inbox_or_send_replies(self):
        bot = self.bot("99")
        bot.handle(self.update(1, 42, "/manager"))
        for i, text in enumerate(("/inbox", "/dialog tg:123:42", "/reply tg:123:42 hacked", "/resume tg:123:42"), 2):
            bot.handle(self.update(i, 43, text))
        bot.flush()
        self.assertFalse(any(p["text"] == "Менеджер: hacked" for p in self.sent))
        self.assertFalse(any(p["chat_id"] == "43" and "Обращений" in p["text"] for p in self.sent))
        self.assertEqual(self.store.state("tg:123:42")["mode"], "human")

    def test_unknown_reply_and_foreign_bot_notification_are_rejected(self):
        bot = self.bot("99")
        bot.handle(self.update(1, 42, "/manager"))
        bot.flush()
        for i, msg_id, sender in ((2, 99999, 123), (3, 2, 555)):
            update = self.update(i, 99, "не отправлять")
            update["message"]["reply_to_message"] = {"message_id": msg_id, "from": {"id": sender}}
            bot.handle(update)
        bot.flush()
        self.assertFalse(any(p["text"] == "Менеджер: не отправлять" for p in self.sent))


if __name__ == "__main__":
    unittest.main()
