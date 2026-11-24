import copy
import json
import unittest

import test_booking as fixtures
from hotel_bot.cards import stay_dates
from hotel_bot.config import Settings
from hotel_bot.engine import Engine
from hotel_bot.form import FormInterpreter
from hotel_bot.http import ServiceError
from hotel_bot.storage import Store
from hotel_bot.telegram import Telegram


class CardTests(unittest.TestCase):
    setUp = fixtures.BookingTests.setUp
    tearDown = fixtures.BookingTests.tearDown

    def setup_bot(self):
        self.engine.interpreter = FormInterpreter()
        self.calls, self.messages = [], {}
        self.fail_edit = None
        self.fail_send = False
        self.event = 1000
        self.session = "tg:123:42"

        def transport(url, body, **kwargs):
            method = url.rsplit("/", 1)[-1]
            self.calls.append((method, copy.deepcopy(body)))
            if method == "editMessageText":
                if self.fail_edit:
                    raise self.fail_edit
                ident = body["message_id"]
                self.messages[ident] = copy.deepcopy(body)
                return {"ok": True, "result": {"message_id": ident}}
            if method == "sendMessage":
                if self.fail_send:
                    raise ServiceError("timeout")
                ident = len(self.messages) + 1
                self.messages[ident] = copy.deepcopy(body)
                return {"ok": True, "result": {"message_id": ident}}
            return {"ok": True, "result": True}

        self.bot = Telegram(Settings(telegram_token="test", manager_id="99"), self.engine, transport)
        self.bot.bot_id = "123"

    def send(self, text, *, callback=False, event=None):
        self.event += 1
        index = event or self.event
        message = {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": text}
        update = {"update_id": index, "message": message}
        if callback:
            update = {"update_id": index, "callback_query": {"id": str(index), "from": {"id": 42},
                                                             "message": message, "data": text}}
        self.bot.handle(update)
        self.bot.flush()
        row = self.store.db.execute("SELECT payload,message_id,status FROM outbox WHERE id=?", (f"tg:123:{index}:guest",)).fetchone()
        self.last = json.loads(row["payload"])
        self.last_id = row["message_id"]
        return row

    def button(self, text=None, suffix=None):
        return next(b["data"] for b in self.last["buttons"]
                    if (text is not None and b["text"] == text) or (suffix and b["data"].endswith(suffix)))

    def services(self):
        self.setup_bot()
        for text in ("/start", "01.02.2027", "03.02.2027", "2", "0"):
            self.send(text)
        self.send(self.button("Стандарт"), callback=True)
        self.card_id = self.last_id

    def toggle(self, key):
        command = self.button(suffix=":" + key)
        return self.send(command, callback=True)

    def test_full_button_journey_edits_services_and_keeps_receipts_separate(self):
        self.services()
        for key, total in (("breakfast", "12 400,00"), ("transfer", "13 900,00"),
                           ("fruit", "14 900,00"), ("breakfast", "12 500,00"),
                           ("transfer", "11 000,00"), ("fruit", "10 000,00"),
                           ("breakfast", "12 400,00"), ("fruit", "13 400,00")):
            self.toggle(key)
            self.assertEqual(self.last_id, self.card_id)
            self.assertIn(total, self.last["text"])
        self.assertEqual(sum(m == "editMessageText" and body.get("message_id") == self.card_id for m, body in self.calls), 8)
        self.assertEqual(sum(m == "sendMessage" and "Добавьте услуги" in b["text"] for m, b in self.calls), 1)
        self.toggle("done")
        self.send("Анна Тестовая | +79990000000")
        old = self.button("Подтвердить бронь")
        preview_id = self.last_id
        self.assertIn("13 400,00", self.last["text"])
        self.assertIn("оплата не требуется", self.last["text"])
        self.assertNotIn("Предоплата:", self.last["text"])
        self.assertNotIn("возраст:", self.last["text"])
        self.assertNotIn(old.split()[1], self.last["text"])
        self.send(self.button("Условия и расчёт"), callback=True)
        self.assertIn("Проживание: 10 000,00", self.last["text"])
        self.assertIn("Завтрак: 2 400,00", self.last["text"])
        self.assertIn("Предоплата: 4 020,00", self.last["text"])
        self.send(self.button("К итоговой карточке"), callback=True)
        self.send(self.button("Изменить данные"), callback=True)
        self.send(self.button("Имя и телефон"), callback=True)
        self.send("Мария Проверочная | +79991111111")
        confirm = self.button("Подтвердить бронь")
        self.assertNotEqual(old, confirm)
        self.send(old, callback=True)
        self.assertIn("устарело", self.last["text"])
        self.send(confirm, callback=True)
        confirmed_id = self.last_id
        self.assertNotEqual(confirmed_id, preview_id)
        self.assertEqual(self.messages[preview_id]["text"].count("Анна Тестовая"), 1)
        self.assertIn("Мария Проверочная", self.last["text"])
        self.send(confirm, callback=True)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM bookings").fetchone()[0], 1)
        self.send("/bookings")
        item = self.last["buttons"][0]
        self.assertEqual(item["text"], "Стандарт · 1–3 февраля 2027 · Активна")
        self.send(item["data"], callback=True)
        self.send(self.button("Отменить бронь"), callback=True)
        self.assertEqual(self.store.db.execute("SELECT status FROM bookings").fetchone()[0], "CONFIRMED")
        self.send(self.button("Да, отменить бронь"), callback=True)
        self.assertIn("отменена", self.last["text"])
        self.assertNotEqual(self.last_id, confirmed_id)
        self.assertIn("Бронь подтверждена", self.messages[confirmed_id]["text"])
        self.assertEqual(self.store.db.execute("SELECT status FROM bookings").fetchone()[0], "CANCELLED")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox WHERE id LIKE 'booking:%:notice' AND status='sent'").fetchone()[0], 2)
        self.send("/bookings")
        self.assertIn("Отменена", self.last["buttons"][0]["text"])
        for method, body in self.calls:
            if method not in {"sendMessage", "editMessageText"} or body["chat_id"] != "42":
                continue
            self.assertNotRegex(body["text"], r"/[a-z]+|flexible|nonrefundable|\{\"|\[\]")
            for row in body.get("reply_markup", {}).get("inline_keyboard", []):
                for b in row:
                    if b.get("style") == "danger":
                        self.assertTrue(b["callback_data"].startswith("/cancel_confirm "))
                    if b["callback_data"].startswith("/confirm "):
                        self.assertEqual(b["style"], "success")
                    if b["callback_data"].endswith(":done"):
                        self.assertEqual(b["style"], "primary")
                    self.assertLessEqual(len(b["callback_data"].encode()), 64)

    def test_uneditable_or_deleted_card_falls_back_and_next_click_edits_replacement(self):
        self.services()
        self.fail_edit = ServiceError("rejected", status=400, description="Bad Request: message to edit not found")
        self.toggle("breakfast")
        replacement = self.last_id
        self.assertNotEqual(replacement, self.card_id)
        self.assertIn("✓ Завтрак", self.last["text"])
        self.fail_edit = None
        self.toggle("fruit")
        self.assertEqual(self.last_id, replacement)
        self.assertIn("13 400,00", self.last["text"])

    def test_edit_timeout_does_not_blindly_send_duplicate_and_selection_is_kept(self):
        self.services()
        self.fail_edit = ServiceError("timeout")
        sends = sum(m == "sendMessage" for m, _ in self.calls)
        row = self.toggle("breakfast")
        self.assertEqual(row["status"], "unknown")
        self.assertTrue(self.store.state(self.session)["fields"]["breakfast"])
        self.assertEqual(sum(m == "sendMessage" for m, _ in self.calls), sends)
        self.fail_edit = None
        self.send("/start")
        self.assertEqual(self.last_id, self.card_id)
        self.assertIn("✓ Завтрак", self.last["text"])

    def test_not_modified_is_success_without_new_message(self):
        self.services()
        self.fail_edit = ServiceError("rejected", status=400, description="Bad Request: message is not modified")
        row = self.send("/start")
        self.assertEqual(row["status"], "sent")
        self.assertEqual(self.last_id, self.card_id)

    def test_fallback_send_timeout_is_not_retried(self):
        self.services()
        self.fail_edit = ServiceError("rejected", status=400, description="Bad Request: message can't be edited")
        self.fail_send = True
        row = self.toggle("breakfast")
        self.assertEqual(row["status"], "unknown")
        count = len(self.calls)
        self.bot.flush()
        self.assertEqual(len(self.calls), count)
        self.assertTrue(self.store.state(self.session)["fields"]["breakfast"])

    def test_restart_preserves_card_target_and_duplicate_toggle_does_not_toggle_back(self):
        self.services()
        command = self.button(suffix=":breakfast")
        event = 5000
        self.send(command, callback=True, event=event)
        count = len(self.messages)
        self.send(command, callback=True, event=event)
        self.assertEqual(len(self.messages), count)
        self.assertTrue(self.store.state(self.session)["fields"]["breakfast"])
        self.store.close()
        self.store = Store(self.path, fixtures.HOTEL)
        self.engine = Engine(self.store, FormInterpreter(), fixtures.HOTEL, lambda: fixtures.NOW)
        self.bot = Telegram(self.bot.settings, self.engine, self.bot.transport)
        self.bot.bot_id = "123"
        self.send("/start")
        self.toggle("fruit")
        self.assertEqual(self.last_id, self.card_id)
        self.send(command, callback=True)
        self.assertIn("устарела", self.last["text"])
        self.assertTrue(self.store.state(self.session)["fields"]["breakfast"])

    def test_reopening_services_keeps_choices_and_invalidates_quote(self):
        self.services()
        self.toggle("breakfast")
        self.toggle("fruit")
        self.toggle("done")
        self.send("Анна Тестовая | +79990000000")
        old = self.button("Подтвердить бронь")
        self.send("/edit services")
        self.assertIn("✓ Завтрак", self.last["text"])
        self.assertIn("✓ Фрукты", self.last["text"])
        self.toggle("breakfast")
        self.toggle("done")
        self.assertIn("11 000,00", self.last["text"])
        self.send(old, callback=True)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM bookings").fetchone()[0], 0)

    def test_faq_is_separate_and_does_not_replace_services_card(self):
        self.services()
        initial = copy.deepcopy(self.messages[self.card_id])
        self.send("Есть парковка?")
        self.assertNotEqual(self.last_id, self.card_id)
        self.assertEqual(self.messages[self.card_id], initial)
        self.toggle("breakfast")
        self.assertEqual(self.last_id, self.card_id)

    def test_dates_across_months_and_years(self):
        self.assertEqual(stay_dates("2026-12-10", "2026-12-12"), "10–12 декабря 2026")
        self.assertEqual(stay_dates("2027-01-30", "2027-02-02"), "30 января — 2 февраля 2027")
        self.assertEqual(stay_dates("2026-12-30", "2027-01-02"), "30 декабря 2026 — 2 января 2027")


if __name__ == "__main__":
    unittest.main()
