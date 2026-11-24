import json
import unittest

import test_booking as fixtures
from hotel_bot.config import Settings
from hotel_bot.engine import Engine
from hotel_bot.form import FormInterpreter
from hotel_bot.http import ServiceError
from hotel_bot.storage import Store
from hotel_bot.telegram import Telegram


class BookingNotificationTests(unittest.TestCase):
    setUp = fixtures.BookingTests.setUp
    tearDown = fixtures.BookingTests.tearDown
    send = fixtures.BookingTests.send
    prepare = fixtures.BookingTests.prepare
    update = staticmethod(fixtures.TelegramTests.update)
    session = "tg:123:42"

    def bot(self, fail_manager=False):
        self.deliveries = []

        def transport(url, payload, **kwargs):
            if url.endswith("/sendMessage"):
                self.deliveries.append(payload)
                if fail_manager and payload["chat_id"] == "99":
                    raise ServiceError("uncertain delivery")
            return {"ok": True, "result": {"message_id": len(self.deliveries)}}

        bot = Telegram(Settings(manager_id="99"), self.engine, transport)
        bot.bot_id = "123"
        return bot

    def notices(self):
        return self.store.db.execute("SELECT * FROM outbox WHERE id LIKE 'booking:%:notice' ORDER BY rowid").fetchall()

    def test_creation_and_cancellation_each_notify_once_with_snapshot(self):
        token = self.prepare(self.session, children="7")
        bot = self.bot()
        confirm = self.update(100, 42, "/confirm " + token)
        bot.handle(confirm)
        bot.handle(confirm)
        bot.handle(self.update(101, 42, "/confirm " + token))
        bot.handle(self.update(102, 42, "/status"))
        bot.flush()
        self.assertEqual(len(self.notices()), 1)
        notice = json.loads(self.notices()[0]["payload"])
        for text in ("НОВАЯ БРОНЬ", "2027-02-01", "2027-02-03", "Анна Тестовая", "+79990000000",
                     "Взрослых: 2; детей: 1", "[7]", "14 800,00", "Без завтрака", "Услуги:"):
            self.assertIn(text, notice["text"])
        self.assertEqual(notice["buttons"], [])
        self.assertEqual(self.store.state(self.session)["mode"], "bot")
        ident = self.store.state(self.session)["booking_id"]
        bot.handle(self.update(103, 42, "/cancel " + ident))
        self.assertEqual(len(self.notices()), 1)
        cancel = "/cancel_confirm " + self.store.state(self.session)["cancel_pending"]["token"]
        update = self.update(104, 42, cancel)
        bot.handle(update)
        bot.handle(update)
        bot.handle(self.update(105, 42, cancel))
        bot.handle(self.update(106, 42, "/cancel " + ident))
        bot.flush()
        self.assertEqual(len(self.notices()), 2)
        self.assertIn("ОТМЕНА БРОНИ", json.loads(self.notices()[1]["payload"])["text"])
        self.assertEqual(sum(p["chat_id"] == "99" for p in self.deliveries), 2)

    def test_repricing_and_rejected_confirmation_do_not_notify(self):
        token = self.prepare(self.session)
        bot = self.bot()
        self.store.db.execute("UPDATE rooms SET nightly=600000 WHERE id='standard'")
        bot.handle(self.update(100, 42, "/confirm " + token))
        bot.handle(self.update(101, 43, "/confirm " + token))
        self.assertEqual(self.notices(), [])
        current = self.store.state(self.session)["quote_token"]
        bot.handle(self.update(102, 42, "/confirm " + current))
        self.assertIn("12 000,00", json.loads(self.notices()[0]["payload"])["text"])

    def test_paid_booking_has_no_cancellation_notice(self):
        self.prepare(self.session)
        self.send("/rate nonrefundable", self.session)
        self.send("/choose 1", self.session)
        token = self.store.state(self.session)["quote_token"]
        bot = self.bot()
        bot.handle(self.update(100, 42, "/confirm " + token))
        ident = self.store.state(self.session)["booking_id"]
        snapshot = json.loads(self.store.db.execute("SELECT snapshot FROM bookings WHERE id=?", (ident,)).fetchone()[0])
        snapshot["payment_status"] = "PAID"
        self.store.db.execute("UPDATE bookings SET snapshot=? WHERE id=?", (json.dumps(snapshot), ident))
        bot.handle(self.update(101, 42, "/cancel " + ident))
        self.assertEqual(len(self.notices()), 1)
        self.assertTrue(self.store.db.execute("SELECT 1 FROM outbox WHERE id='tg:123:101:manager'").fetchone())

    def test_committed_event_recovers_notice_after_restart_without_duplicates(self):
        token = self.prepare(self.session)
        command = "/confirm " + token
        # Crash between the domain commit and Telegram.queue; polling replays the update.
        self.engine.process(self.session, "tg:123:100", command)
        self.assertEqual(self.notices(), [])
        self.store.close()
        self.store = Store(self.path, fixtures.HOTEL)
        self.engine = Engine(self.store, FormInterpreter(), fixtures.HOTEL, lambda: fixtures.NOW)
        bot = self.bot()
        bot.handle(self.update(100, 42, command))
        bot.handle(self.update(100, 42, command))
        bot.flush()
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.notices()[0]["status"], "sent")

    def test_handoff_does_not_suppress_notice_or_change_reply_routing(self):
        token = self.prepare(self.session)
        bot = self.bot()
        bot.handle(self.update(100, 42, "/confirm " + token))
        bot.handle(self.update(101, 42, "/manager"))
        bot.flush()
        row = self.notices()[0]
        self.assertEqual(row["status"], "sent")
        reply = self.update(102, 99, "не отправлять из карточки брони")
        reply["message"]["reply_to_message"] = {"message_id": row["message_id"], "from": {"id": 123}}
        bot.handle(reply)
        bot.flush()
        self.assertFalse(any(p["text"] == "Менеджер: не отправлять из карточки брони" for p in self.deliveries))

    def test_uncertain_notification_delivery_is_not_blindly_retried(self):
        token = self.prepare(self.session)
        bot = self.bot(fail_manager=True)
        update = self.update(100, 42, "/confirm " + token)
        bot.handle(update)
        bot.flush()
        bot.handle(update)
        bot.flush()
        self.assertEqual(self.notices()[0]["status"], "unknown")
        self.assertEqual(sum(p["chat_id"] == "99" for p in self.deliveries), 1)


if __name__ == "__main__":
    unittest.main()
