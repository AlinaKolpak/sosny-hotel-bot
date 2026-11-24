import copy
import json
import unittest
from unittest.mock import patch

import test_booking as fixtures
from hotel_bot.config import Settings
from hotel_bot.form import FormInterpreter
from hotel_bot.http import ServiceError, request_json
from hotel_bot.telegram import Telegram


class RoomTests(unittest.TestCase):
    setUp = fixtures.BookingTests.setUp
    tearDown = fixtures.BookingTests.tearDown
    send = fixtures.BookingTests.send
    count = fixtures.BookingTests.count

    @staticmethod
    def button(reply, label):
        return next(b["data"] for b in reply["buttons"] if b["text"] == label)

    def rooms(self, children="-", session="guest"):
        self.engine.interpreter = FormInterpreter()
        return self.send(f"/stay 2027-02-01 2027-02-03 2 {children}", session)

    def test_browse_compare_choose_services_edit_confirm_cancel(self):
        listing = self.rooms()
        before = copy.deepcopy(self.store.state("guest"))
        photo = self.send(self.button(listing, "Смотреть фото"))
        self.assertEqual(photo["photo"], "standard.jpg")
        self.assertIn("Пример интерьера", photo["text"])
        family = self.send(self.button(photo, "Следующий"))
        self.assertEqual(family["photo"], "family.jpg")
        suite = self.send(self.button(family, "Следующий"))
        self.assertEqual(suite["photo"], "suite.jpg")
        self.assertNotIn("Следующий", [b["text"] for b in suite["buttons"]])
        self.send(self.button(suite, "Предыдущий"))
        comparison = self.send(self.button(suite, "Сравнить"))
        for expected in ("Стандарт", "Семейный", "Люкс", "10 000,00", "14 000,00", "20 000,00", "До 2 гостей", "До 4 гостей"):
            self.assertIn(expected, comparison["text"])
        self.assertNotIn("photo", comparison)
        after = self.store.state("guest")
        for key in ("fields", "selected", "guest", "offers", "list_token", "quote_token"):
            self.assertEqual(before[key], after[key])
        self.send(self.button(comparison, "Выбрать: Стандарт"))
        token = self.store.state("guest")["service_token"]
        self.send(f"/service {token}:breakfast")
        token = self.store.state("guest")["service_token"]
        self.send(f"/service {token}:fruit")
        token = self.store.state("guest")["service_token"]
        self.send(f"/service {token}:done")
        preview = self.send("Анна Тестовая | +79990000000")
        old = self.button(preview, "Подтвердить бронь")
        self.send("/edit guest")
        preview = self.send("Мария Тестовая | +79991111111")
        self.send(old)
        self.assertEqual(self.count(), 0)
        confirm = self.button(preview, "Подтвердить бронь")
        result = self.send(confirm)
        self.send(confirm)
        self.assertEqual(self.count(), 1)
        self.assertIn("13 400,00", result["text"])
        cancel = self.send(self.button(result, "Отменить бронь"))
        self.send(self.button(cancel, "Да, отменить бронь"))
        self.assertEqual(self.store.db.execute("SELECT status FROM bookings").fetchone()[0], "CANCELLED")

    def test_photo_choose_and_return_to_list_use_same_generation(self):
        listing = self.rooms()
        photo = self.send(self.button(listing, "Смотреть фото"))
        returned = self.send(self.button(photo, "К списку номеров"))
        self.assertEqual(self.button(listing, "Стандарт"), self.button(returned, "Стандарт"))
        self.send(self.button(photo, "Выбрать номер"))
        self.assertEqual(self.store.state("guest")["selected"]["room_id"], "standard")

    def test_children_only_see_suitable_rooms_and_correct_photo(self):
        listing = self.rooms("7")
        photo = self.send(self.button(listing, "Смотреть фото"))
        self.assertEqual(photo["photo"], "family.jpg")
        self.assertIn("возраст: 7", photo["text"])
        comparison = self.send(self.button(photo, "Сравнить"))
        self.assertNotIn("Стандарт", comparison["text"])
        self.assertIn("14 800,00", comparison["text"])

    def test_repricing_refreshes_catalog_and_rejects_old_choice(self):
        listing = self.rooms()
        old = self.button(listing, "Стандарт")
        self.store.db.execute("UPDATE rooms SET nightly=600000 WHERE id='standard'")
        comparison = self.send(self.button(listing, "Сравнить номера"))
        self.assertIn("изменились", comparison["text"])
        self.assertIn("12 000,00", comparison["text"])
        self.send(old)
        self.assertIsNone(self.store.state("guest")["selected"])

    def test_new_dates_foreign_session_and_selected_room_reject_old_browse(self):
        listing = self.rooms()
        action = self.button(listing, "Смотреть фото")
        self.assertIn("устарел", self.send(action, "other")["text"])
        self.send("/edit dates")
        self.assertIn("устарел", self.send(action)["text"])
        listing = self.rooms()
        self.send(self.button(listing, "Стандарт"))
        before = self.store.state("guest")
        reply = self.send(self.button(listing, "Сравнить номера"))
        self.assertIn("устарел", reply["text"])
        self.assertEqual(self.store.state("guest")["selected"], before["selected"])
        self.assertEqual(self.store.state("guest")["service_token"], before["service_token"])

    def test_empty_and_single_catalog(self):
        self.store.db.execute("UPDATE rooms SET stock=0 WHERE id != 'suite'")
        listing = self.rooms()
        comparison = self.send(self.button(listing, "Сравнить номера"))
        self.assertIn("один вариант", comparison["text"])
        self.store.db.execute("UPDATE rooms SET stock=0")
        reply = self.send(self.button(listing, "Смотреть фото"))
        self.assertIn("номеров", reply["text"])
        self.assertNotIn("photo", reply)

    def test_photo_caption_limit_and_callback_lengths(self):
        self.send("/wishes " + "Тихий номер " * 40)
        listing = self.rooms("7,10")
        photo = self.send(self.button(listing, "Смотреть фото"))
        self.assertLess(len(("[Тестовый гость]\n" + photo["text"]).encode("utf-16-le")) // 2, 1024)
        for b in photo["buttons"]:
            self.assertLessEqual(len(b["data"].encode()), 64)

    def transport_bot(self, photo_error=None):
        self.calls = []
        def transport(url, payload, **kwargs):
            method = url.rsplit("/", 1)[-1]
            self.calls.append((method, payload, kwargs))
            if method == "sendPhoto" and photo_error:
                raise photo_error
            return {"ok": True, "result": {"message_id": len(self.calls)}}
        bot = Telegram(Settings(telegram_token="test"), self.engine, transport)
        bot.bot_id = "123"
        return bot

    def photo_reply(self):
        session = "tg:123:42"
        listing = self.rooms(session=session)
        return self.send(self.button(listing, "Смотреть фото"), session)

    def test_photo_delivery_is_queued_once_with_buttons_and_jpeg(self):
        bot = self.transport_bot()
        reply = self.photo_reply()
        for _ in range(2):
            bot.queue("tg:123:photo", "tg:123:42", "42", reply)
            bot.flush()
        self.assertEqual(len(self.calls), 1)
        method, payload, kwargs = self.calls[0]
        self.assertEqual(method, "sendPhoto")
        self.assertIn("Стандарт", payload["caption"])
        self.assertNotIn("text", payload)
        self.assertTrue(kwargs["photo_bytes"].startswith(b"\xff\xd8"))
        self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0]["style"], "primary")

    def test_missing_or_unsafe_asset_falls_back_with_choice_buttons(self):
        for i, filename in enumerate(("missing.jpg", "../../.env")):
            with self.subTest(filename=filename):
                bot = self.transport_bot()
                reply = self.photo_reply()
                reply["photo"] = filename
                bot.queue("tg:123:" + filename, "tg:123:42", "42", reply)
                bot.flush()
                self.assertEqual([c[0] for c in self.calls], ["sendMessage" if i == 0 else "editMessageText"])
                self.assertIn("Фото сейчас недоступно", self.calls[0][1]["text"])
                self.assertTrue(self.calls[0][1]["reply_markup"])

    def test_rejected_photo_falls_back_but_timeout_never_duplicates(self):
        reply = self.photo_reply()
        for i, error in enumerate((ServiceError("bad image", status=400, description="PHOTO_INVALID_DIMENSIONS"),
                                   ServiceError("timeout"))):
            bot = self.transport_bot(error)
            reply = self.photo_reply()
            reply["card_key"] += f":failure-{i}"
            event = f"tg:123:photo-failure-{i}"
            bot.queue(event, "tg:123:42", "42", reply)
            bot.flush()
            bot.flush()
            methods = [c[0] for c in self.calls]
            self.assertEqual(methods, ["sendPhoto", "sendMessage"] if i == 0 else ["sendPhoto"])
            status = self.store.db.execute("SELECT status FROM outbox WHERE id=?", (event + ":guest",)).fetchone()[0]
            self.assertEqual(status, "sent" if i == 0 else "unknown")

    def test_manual_mode_suppresses_pending_photo(self):
        bot = self.transport_bot()
        reply = self.photo_reply()
        bot.queue("tg:123:photo-human", "tg:123:42", "42", reply)
        self.send("/manager", "tg:123:42")
        bot.flush()
        self.assertEqual(self.calls, [])

    def test_breakfast_photo_preserves_services_and_returns_to_same_step(self):
        self.rooms()
        self.send("/choose 1")
        token = self.store.state("guest")["service_token"]
        self.send(f"/service {token}:fruit")
        before = copy.deepcopy(self.store.state("guest"))
        reply = self.send("/breakfast_photo")
        self.assertEqual(reply["photo"], "breakfast.jpg")
        self.assertIn("Учебная иллюстрация", reply["text"])
        self.assertIn("не обещание конкретного меню", reply["text"])
        after = self.store.state("guest")
        for key in ("fields", "selected", "guest", "quote_token", "extra_choices", "service_token", "service_card_id"):
            self.assertEqual(before.get(key), after.get(key))
        resumed = self.send(self.button(reply, "К бронированию"))
        self.assertIn("Добавьте услуги", resumed["text"])
        self.assertIn("fruit", self.store.state("guest")["extra_choices"])

    def test_breakfast_photo_delivery_and_faq_button(self):
        bot = self.transport_bot()
        reply = self.send("/faq breakfast", "tg:123:42")
        self.assertIn("Фото завтрака", [b["text"] for b in reply["buttons"]])
        reply = self.send(self.button(reply, "Фото завтрака"), "tg:123:42")
        bot.queue("tg:123:breakfast-photo", "tg:123:42", "42", reply)
        bot.flush()
        self.assertEqual(self.calls[0][0], "sendPhoto")
        self.assertTrue(self.calls[0][2]["photo_bytes"].startswith(b"\xff\xd8"))


class MultipartTests(unittest.TestCase):
    def test_jpeg_and_unicode_caption_are_multipart_with_json_keyboard(self):
        captured = []
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, count):
                return b'{"ok":true,"result":{"message_id":1}}'
        class Opener:
            def open(self, request, **kwargs):
                captured.append(request)
                return Response()
        payload = {"chat_id": "42", "caption": "Пример интерьера", "reply_markup": {"inline_keyboard": [[{"text": "Выбрать", "callback_data": "/choose x:1"}]]}}
        photo = b"\xff\xd8test\xff\xd9"
        with patch("hotel_bot.http.build_opener", return_value=Opener()):
            request_json("https://example.test/sendPhoto", payload, photo_bytes=photo)
        request = captured[0]
        boundary = request.get_header("Content-type").split("boundary=")[1]
        self.assertIn(photo, request.data)
        self.assertIn("Пример интерьера".encode(), request.data)
        self.assertIn(json.dumps(payload["reply_markup"], ensure_ascii=False).encode(), request.data)
        self.assertTrue(request.data.endswith(("--" + boundary + "--\r\n").encode()))


if __name__ == "__main__":
    unittest.main()
