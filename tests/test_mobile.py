import copy
import json
import unittest
from datetime import datetime, timedelta, timezone

import test_booking as fixtures
from hotel_bot.config import Settings
from hotel_bot.form import FormInterpreter
from hotel_bot.http import ServiceError
from hotel_bot.storage import Store
from hotel_bot.telegram import Telegram


class MobileTests(unittest.TestCase):
    tearDown = fixtures.BookingTests.tearDown
    send = fixtures.BookingTests.send
    count = fixtures.BookingTests.count

    def setUp(self):
        fixtures.BookingTests.setUp(self)
        self.engine.interpreter = FormInterpreter()

    def state(self, session="guest"):
        return self.store.state(session)

    @staticmethod
    def button(reply, text):
        return next(b["data"] for b in reply["buttons"] if b["text"] == text)

    def day(self, value, session="guest"):
        return self.send(f"/day {self.state(session)['calendar']['token']}:{value}", session)

    def prepare(self):
        self.send("/new")
        self.day("2027-02-01")
        period = self.day("2027-02-03")
        self.assertIn("Количество ночей: 2", period["text"])
        self.send(self.button(period, "Продолжить"))
        self.send("/adults 2")
        listing = self.send("/children 0")
        photo = self.send(self.button(listing, "Смотреть фото"))
        services = self.send(self.button(photo, "Выбрать номер"))
        services = self.send(self.button(services, "+ Завтрак"))
        fruit = next(b["data"] for b in services["buttons"] if b["data"].endswith(":fruit"))
        services = self.send(fruit)
        self.send(self.button(services, "Продолжить"))
        return self.send("Анна Тестовая | +79990000000")

    def test_calendar_photo_services_change_dates_confirm_cancel(self):
        preview = self.prepare()
        before = self.state()
        old = self.button(preview, "Подтвердить бронь")
        self.send(self.button(preview, "Изменить даты"))
        self.day("2027-03-01")
        dates = self.day("2027-03-04")
        new_preview = self.send(self.button(dates, "Продолжить"))
        after = self.state()
        self.assertEqual(after["guest"], before["guest"])
        self.assertEqual(after["selected"]["room_id"], "standard")
        self.assertEqual(after["selected"]["total"], 1960000)
        for key in ("adults", "children_count", "children_ages", "breakfast", "extras"):
            self.assertEqual(after["fields"][key], before["fields"][key])
        self.assertNotEqual(old, self.button(new_preview, "Подтвердить бронь"))
        self.send(old)
        self.assertEqual(self.count(), 0)
        confirm = self.button(new_preview, "Подтвердить бронь")
        created = self.send(confirm)
        repeated = self.send(confirm)
        self.assertIn("booking_notice", created)
        self.assertNotIn("booking_notice", repeated)
        self.assertEqual(self.count(), 1)
        cancellation = self.send(self.button(created, "Отменить бронь"))
        cancelled = self.send(self.button(cancellation, "Да, отменить бронь"))
        self.assertIn("booking_notice", cancelled)
        self.assertEqual(self.store.db.execute("SELECT status FROM bookings").fetchone()[0], "CANCELLED")

    def test_invalid_dates_stale_and_foreign_buttons(self):
        self.engine.clock = lambda: datetime(2027, 1, 15, tzinfo=timezone.utc)
        first = self.send("/new")
        initial = self.state()["calendar"]["token"]
        for value in ("2027-01-14", "2028-01-16", "not-a-date"):
            self.send(f"/day {initial}:{value}")
            self.assertEqual(self.state()["fields"], {})
        self.send(f"/day {initial}:2027-02-01", "other")
        self.assertEqual(self.state("other")["fields"], {})
        self.day("2027-02-01")
        self.send(f"/day {initial}:2027-02-02")
        self.assertNotIn("check_out", self.state()["fields"])
        for value in ("2027-01-31", "2027-02-01", "2027-03-04"):
            self.day(value)
            self.assertNotIn("check_out", self.state()["fields"])
        self.assertTrue(all(b["data"] == "/noop" or not b["data"].endswith("2027-01-14") for b in first["buttons"]))
        self.day("2027-03-03")  # exactly 30 nights
        self.assertEqual(self.state()["fields"]["check_out"], "2027-03-03")

    def test_month_navigation_leap_day_year_boundary_and_bounds(self):
        self.engine.clock = lambda: datetime(2027, 12, 31, tzinfo=timezone.utc)
        first = self.send("/new")
        following = self.send(self.button(first, "Месяц ›"))
        self.assertIn("Январь 2028", following["text"])
        previous = self.send(self.button(following, "‹ Месяц"))
        self.assertEqual(first["card_key"], previous["card_key"])
        token = self.state()["calendar"]["token"]
        leap = self.send(f"/month {token}:2028-02-01")
        self.assertTrue(any(b["data"].endswith(":2028-02-29") for b in leap["buttons"]))
        self.send(f"/month {token}:2027-11-01")
        self.assertEqual(self.state()["calendar"]["month"], "2028-02-01")
        self.day("2028-02-29")
        self.day("2028-03-01")
        self.assertEqual(self.state()["fields"]["check_in"], "2028-02-29")

    def test_manual_input_menu_faq_restart_preserve_calendar(self):
        menu = self.send("/menu")
        self.assertEqual([b["text"] for b in menu["buttons"]], ["Подобрать номер", "Мои брони", "Об отеле"])
        first = self.send(self.button(menu, "Подобрать номер"))
        self.send(self.button(first, "Ввести дату текстом"))
        self.send("01.02.2027")
        before = copy.deepcopy(self.state())
        faq = self.send("/faq parking")
        self.assertIn("Продолжить бронирование", [b["text"] for b in faq["buttons"]])
        self.assertEqual(self.state()["calendar"], before["calendar"])
        menu = self.send("/menu")
        self.assertIn("Продолжить бронирование", [b["text"] for b in menu["buttons"]])
        self.store.close()
        self.store = Store(self.path, fixtures.HOTEL)
        self.engine.store = self.store
        self.engine.pms.db = self.store.db
        resumed = self.send(self.button(menu, "Продолжить бронирование"))
        self.assertEqual(resumed["card_key"], "calendar:" + before["calendar_card"])
        self.send("03.02.2027")
        self.assertEqual(self.state()["fields"]["check_out"], "2027-02-03")

    def test_unavailable_previous_room_offers_alternatives_without_losing_contacts(self):
        self.prepare()
        before = self.state()
        self.send("/edit dates")
        self.store.db.execute("UPDATE rooms SET stock=0 WHERE id='standard'")
        self.day("2027-03-01")
        self.day("2027-03-03")
        reply = self.send("/start")
        self.assertIn("недоступен", reply["text"])
        self.assertIsNone(self.state()["selected"])
        self.assertEqual([o["room_id"] for o in self.state()["offers"]], ["family", "suite"])
        self.assertEqual(self.state()["guest"], before["guest"])
        preview = self.send(self.button(reply, "Семейный"))
        self.assertIn("Проверьте бронь", preview["text"])
        self.assertTrue(self.state()["fields"]["breakfast"])

    def test_calendar_row_layout_and_gallery_message_reuse(self):
        calls, messages = [], {}
        def transport(url, body, **kwargs):
            method = url.rsplit("/", 1)[-1]
            calls.append((method, body, kwargs))
            ident = body.get("message_id", len(messages) + 1)
            messages[ident] = body
            return {"ok": True, "result": {"message_id": ident}}
        bot = Telegram(Settings(telegram_token="test"), self.engine, transport)
        bot.bot_id = "123"
        session = "tg:123:42"
        def deliver(reply):
            event = f"tg:123:{self.counter}"
            bot.queue(event, session, "42", reply)
            bot.flush()
            return self.store.db.execute("SELECT message_id FROM outbox WHERE id=?", (event + ":guest",)).fetchone()[0]
        first = self.send("/new", session)
        calendar_id = deliver(first)
        self.assertEqual(len(calls[-1][1]["reply_markup"]["inline_keyboard"][0]), 7)
        second = self.day("2027-02-01", session)
        self.assertEqual(deliver(second), calendar_id)
        period = self.day("2027-02-03", session)
        self.assertEqual(deliver(period), calendar_id)
        self.send("/adults 2", session)
        listing = self.send("/children 0", session)
        photo = self.send(self.button(listing, "Смотреть фото"), session)
        photo_id = deliver(photo)
        next_photo = self.send(self.button(photo, "Следующий"), session)
        self.assertEqual(deliver(next_photo), photo_id)
        method, body, kwargs = calls[-1]
        self.assertEqual(method, "editMessageMedia")
        self.assertEqual(body["media"]["media"], "attach://photo")
        self.assertIn("Семейный", body["media"]["caption"])
        self.assertTrue(kwargs["photo_bytes"].startswith(b"\xff\xd8"))
        rows = body["reply_markup"]["inline_keyboard"]
        self.assertEqual([b["text"] for b in rows[1]], ["Предыдущий", "Следующий"])
        for row in rows:
            for b in row:
                self.assertLessEqual(len(b["callback_data"].encode()), 64)

    def test_gallery_recovery_deleted_text_fallback_and_timeout(self):
        calls = []
        errors = []
        def transport(url, body, **kwargs):
            method = url.rsplit("/", 1)[-1]
            calls.append((method, body))
            if errors:
                raise errors.pop(0)
            return {"ok": True, "result": {"message_id": body.get("message_id", len(calls))}}
        bot = Telegram(Settings(telegram_token="test"), self.engine, transport)
        body = {"chat_id": "42", "text": "Стандарт", "reply_markup": {"inline_keyboard": [[{"text": "Выбрать номер", "callback_data": "/choose a:1"}]]}}
        errors.append(ServiceError("gone", status=400, description="message to edit not found"))
        sent = bot._send_room_photo(body, "standard.jpg", "5", "photo")
        self.assertEqual([c[0] for c in calls], ["editMessageMedia", "sendPhoto"])
        self.assertEqual(sent["_card_kind"], "photo")
        calls.clear()
        bot._send_room_photo(body, "missing.jpg", "5", "text")
        self.assertEqual(calls[0][0], "editMessageText")
        calls.clear()
        bot._send_room_photo(body, "missing.jpg", "5", "photo")
        self.assertEqual(calls[0][0], "sendMessage")
        calls.clear()
        errors.append(ServiceError("timeout"))
        with self.assertRaises(ServiceError):
            bot._send_room_photo(body, "standard.jpg", "5", "photo")
        self.assertEqual([c[0] for c in calls], ["editMessageMedia"])

    def test_resume_after_arrival_date_passed_recovers_without_losing_data(self):
        preview = self.prepare()
        before = self.state()
        old_confirm = self.button(preview, "Подтвердить бронь")
        self.engine.clock = lambda: datetime(2027, 2, 2, 12, tzinfo=timezone.utc)
        self.send(old_confirm)
        self.assertEqual(self.count(), 0)
        reply = self.send("/start")
        self.assertIn("Дата заезда в сохранённой анкете уже прошла", reply["text"])
        self.assertIn("Выберите дату заезда", reply["text"])
        self.assertIsNone(self.state()["quote_token"])
        self.assertEqual(self.state()["guest"], before["guest"])
        for key in ("adults", "children_count", "breakfast", "extras"):
            self.assertEqual(self.state()["fields"][key], before["fields"][key])
        self.assertEqual(self.state()["preferred_room"], "standard")
        self.day("2027-03-01")
        self.day("2027-03-03")
        reply = self.send("/start")
        self.assertIn("Проверьте бронь", reply["text"])
        self.send(old_confirm)
        self.assertEqual(self.count(), 0)

    def test_contact_entry_after_midnight_is_kept_while_requesting_new_dates(self):
        self.send("/stay 2027-01-01 2027-01-03 2 -")
        self.send("/choose 1")
        token = self.state()["service_token"]
        self.send(f"/service {token}:fruit")
        token = self.state()["service_token"]
        self.send(f"/service {token}:done")
        self.send("Алина")
        self.engine.clock = lambda: datetime(2027, 1, 2, 12, tzinfo=timezone.utc)
        reply = self.send("8 (999) 123-45-67")
        self.assertIn("Выберите новые даты", reply["text"])
        self.assertEqual(self.state()["guest"], {"name": "Алина", "phone": "89991234567"})
        self.assertEqual(self.state()["fields"]["extras"], ["fruit"])


if __name__ == "__main__":
    unittest.main()
