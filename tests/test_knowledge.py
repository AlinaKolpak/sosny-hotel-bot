import copy
import unittest
from unittest.mock import Mock

import test_booking as fixtures
from hotel_bot.engine import Engine
from hotel_bot.form import FormInterpreter
from hotel_bot.hybrid import HybridInterpreter
from hotel_bot.knowledge import question_topics
from hotel_bot.storage import Store
from hotel_bot.telegram import Telegram
from hotel_bot.config import Settings


class KnowledgeTests(unittest.TestCase):
    setUp = fixtures.BookingTests.setUp
    tearDown = fixtures.BookingTests.tearDown
    send = fixtures.BookingTests.send
    prepare = fixtures.BookingTests.prepare
    count = fixtures.BookingTests.count

    def use_form(self):
        self.engine.interpreter = FormInterpreter()

    def ask_without_losing_data(self, question, expected):
        before = copy.deepcopy(self.store.state("guest"))
        reply = self.send(question)
        self.assertIn(expected, reply["text"])
        after = self.store.state("guest")
        for key in ("fields", "guest", "selected", "quote_token", "quote_expires",
                    "service_token", "extra_choices", "editing", "booking_id", "cancel_pending"):
            self.assertEqual(before.get(key), after.get(key), key)
        return reply

    def test_questions_at_each_booking_step_and_old_confirmation_still_works(self):
        self.use_form()
        self.ask_without_losing_data("Какие есть номера?", "до 4 гостей")
        self.send("01.02.2027")
        reply = self.ask_without_losing_data("Есть парковка?", "парковка")
        self.assertIn("Выберите дату выезда", reply["text"])
        self.send("03.02.2027")
        reply = self.ask_without_losing_data("Какие правила?", "предоплата 30%")
        self.assertIn("Сколько будет взрослых", reply["text"])
        self.send("2")
        self.ask_without_losing_data("Во сколько заезд?", "14:00")
        self.send("1")
        reply = self.ask_without_losing_data("Как размещаются дети?", "Возраст детей")
        self.assertIn("возраст каждого", reply["text"])
        self.send("7")
        reply = self.ask_without_losing_data("Какие номера?", "Семейный")
        choice = next(b["data"] for b in reply["buttons"] if b["data"].startswith("/choose "))
        self.send(choice)
        reply = self.ask_without_losing_data("Сколько стоит завтрак?", "600")
        self.assertIn("Добавьте услуги", reply["text"])
        token = self.store.state("guest")["service_token"]
        self.send(f"/meal {token}:yes")
        self.send(f"/extras {token}:fruit")
        reply = self.ask_without_losing_data("Есть парковка и завтрак?", "парковка")
        self.assertIn("600", reply["text"])
        self.assertTrue(any("✓" in b["text"] for b in reply["buttons"]))
        self.send(f"/extras {token}:done")
        reply = self.ask_without_losing_data("Где вы находитесь?", "вымышленный")
        self.assertIn("имя и телефон", reply["text"])
        self.send("Анна Тестовая | +79990000000")
        confirm = self.store.state("guest")["quote_token"]
        self.ask_without_losing_data("Какие правила отмены?", "отменить бесплатно в любое время")
        self.assertEqual(self.count(), 0)
        self.send("/confirm " + confirm)
        self.assertEqual(self.count(), 1)

    def test_editing_contacts_and_wishes_survives_question(self):
        self.use_form()
        self.prepare()
        self.send("/edit guest")
        self.ask_without_losing_data("Какие номера?", "Стандарт")
        self.send("Мария Проверочная | +79991111111")
        self.send("/edit wishes")
        reply = self.ask_without_losing_data("Можно с собакой?", "согласует менеджер")
        self.assertIn("Напишите пожелания", reply["text"])
        self.send("Тихий номер рядом с парковкой")
        state = self.store.state("guest")
        self.assertEqual(state["fields"]["wishes"], "Тихий номер рядом с парковкой")
        self.assertEqual(state["guest"]["name"], "Мария Проверочная")

    def test_unknown_question_offers_manager_and_preserves_step(self):
        self.use_form()
        self.send("01.02.2027")
        reply = self.ask_without_losing_data("Есть бассейн?", "Точного ответа")
        self.assertIn("Выберите дату выезда", reply["text"])
        self.assertEqual(self.store.state("guest")["mode"], "bot")
        self.assertIn({"text": "Уточнить у менеджера", "data": "/manager"}, reply["buttons"])
        self.send("/manager")
        reply = self.send("Есть парковка?")
        self.assertIsNone(reply["text"])
        self.assertIn("Есть парковка?", reply["manager"])

    def test_menu_and_answers_work_after_restart_without_schema_change(self):
        self.use_form()
        self.send("01.02.2027")
        self.store.close()
        self.store = Store(self.path, fixtures.HOTEL)
        self.engine = Engine(self.store, FormInterpreter(), fixtures.HOTEL, lambda: fixtures.NOW)
        menu = self.send("/faq")
        for topic in ("rooms", "breakfast", "parking", "rules"):
            self.assertTrue(any(b["data"] == "/faq " + topic for b in menu["buttons"]))
            reply = self.ask_without_losing_data("/faq " + topic, "Выберите дату выезда")
            self.assertIn("Справка учебного отеля", reply["text"])

    def test_question_never_confirms_cancels_or_sends_booking_notice(self):
        self.use_form()
        token = self.prepare()
        self.send("/confirm " + token)
        booking = self.store.state("guest")["booking_id"]
        self.send("/cancel " + booking)
        reply = self.ask_without_losing_data("Как отменить бронь?", "ещё не подтверждена")
        self.assertNotIn("booking_notice", reply)
        self.assertEqual(self.store.db.execute("SELECT status FROM bookings").fetchone()[0], "CONFIRMED")
        self.assertTrue(any(b["data"] == "/cancel " + booking for b in reply["buttons"]))

    def test_knowledge_does_not_call_ai_even_in_hybrid(self):
        ai = Mock()
        ai.interpret.side_effect = AssertionError("FAQ must stay local")
        hybrid = HybridInterpreter(ai)
        self.engine.interpreter = hybrid
        for question in ("Какие номера?", "ПИТАНИЕ", "парковка", "Какие правила?", "Есть бассейн?"):
            self.send(question)
            self.assertEqual(hybrid.last_metrics["api_calls"], 0)
        ai.interpret.assert_not_called()

    def test_telegram_question_and_faq_callback_return_form_buttons(self):
        self.use_form()
        sent = []
        def transport(url, body, **kwargs):
            if url.endswith("/sendMessage"):
                sent.append(body)
            return {"ok": True, "result": {}}
        bot = Telegram(Settings(telegram_token="test"), self.engine, transport)
        bot.bot_id = "123"
        for index, text in enumerate(("01.02.2027", "03.02.2027", "Есть парковка?", "/faq rooms"), 1):
            message = {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": text}
            update = {"update_id": index, "message": message}
            if text.startswith("/faq"):
                update = {"update_id": index, "callback_query": {
                    "id": str(index), "from": {"id": 42}, "data": text, "message": message}}
            bot.handle(update)
            bot.flush()
        self.assertIn("Стандарт", sent[-1]["text"])
        self.assertIn("Сколько будет взрослых", sent[-1]["text"])
        buttons = sent[-1]["reply_markup"]["inline_keyboard"]
        self.assertTrue(any(b["callback_data"] == "/adults 2" for row in buttons for b in row))

    def test_keywords_inside_statements_do_not_consume_booking_input(self):
        for text in ("01.02.2027", "2", "7,10", "Хочу номер с завтраком", "Тихий номер",
                     "Анна Паркова | +79990000000", "/guest Имя | +79990000000"):
            self.assertIsNone(question_topics(text), text)

    def test_many_topics_fit_telegram_and_preserve_confirmation(self):
        self.use_form()
        self.send("/wishes " + "Тихий номер. " * 35)
        self.prepare()
        reply = self.ask_without_losing_data(
            "Расскажите про номера, питание, парковку, правила, детей, животных и заезд?",
            "Выберите одну")
        self.assertLess(len(reply["text"].encode("utf-16-le")) // 2, 4000)
        self.assertTrue(any(b["data"] == "/faq rules" for b in reply["buttons"]))


if __name__ == "__main__":
    unittest.main()
