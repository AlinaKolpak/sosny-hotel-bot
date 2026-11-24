import copy
import unittest
from unittest.mock import Mock

import test_booking as fixtures
from hotel_bot.contacts import parse_contacts
from hotel_bot.form import FormInterpreter
from hotel_bot.storage import Store


class ContactTests(unittest.TestCase):
    tearDown = fixtures.BookingTests.tearDown
    send = fixtures.BookingTests.send
    count = fixtures.BookingTests.count

    def setUp(self):
        fixtures.BookingTests.setUp(self)
        self.engine.interpreter = FormInterpreter()

    def ready(self):
        self.send("/new")
        self.send("/stay 2027-02-01 2027-02-03 2 -")
        self.send("/choose 1")
        token = self.store.state("guest")["service_token"]
        return self.send(f"/service {token}:done")

    def test_combined_formats_names_and_phone_prefixes(self):
        for text, name, phone in [
            ("Алина, 8 (999) 123-45-67", "Алина", "89991234567"),
            ("Алина 89991234567", "Алина", "89991234567"),
            ("Анна Мария +7 999 123 45 67", "Анна Мария", "+79991234567"),
            ("李 | +86 (138) 0013-8000", "李", "+8613800138000"),
            ("Ли, 89991234567", "Ли", "89991234567"),
            ("А, 12345", "А", "12345"),
            ("Anne-Marie O'Neill\n+44 7700 900123", "Anne-Marie O'Neill", "+447700900123"),
            ("8 (999) 123-45-67, Алина", "Алина", "89991234567"),
        ]:
            with self.subTest(text=text):
                self.ready()
                reply = self.send(text)
                self.assertEqual(self.store.state("guest")["guest"], {"name": name, "phone": phone})
                self.assertIn("Подтвердить бронь", [b["text"] for b in reply["buttons"]])

    def test_separate_messages_both_orders_never_call_interpreter(self):
        for values in (("Алина", "8 999 123-45-67"), ("89991234567", "Алина"), ("89991234567", "Ли")):
            self.ready()
            interpreter = self.engine.interpreter
            self.engine.interpreter = Mock()
            self.engine.interpreter.interpret.side_effect = AssertionError("Contacts must stay local")
            self.send(values[0])
            self.assertIsNone(self.store.state("guest")["guest"])
            self.assertIsNone(self.store.state("guest")["quote_token"])
            self.send(values[1])
            self.assertEqual(self.store.state("guest")["guest"]["phone"], "89991234567")
            self.engine.interpreter.interpret.assert_not_called()
            self.engine.interpreter = interpreter

    def test_partial_contacts_survive_faq_menu_restart_and_dates(self):
        self.ready()
        self.send("Алина")
        draft = copy.deepcopy(self.store.state("guest")["guest_draft"])
        self.send("Завтрак до 12?")
        self.send("/menu")
        self.assertEqual(self.store.state("guest")["guest_draft"], draft)
        self.send("/edit dates")
        self.send("01.03.2027")
        self.send("03.03.2027")
        self.store.close()
        self.store = Store(self.path, fixtures.HOTEL)
        self.engine.store = self.store
        self.engine.pms.db = self.store.db
        self.send("89991234567")
        self.assertEqual(self.store.state("guest")["guest"]["name"], "Алина")
        self.assertEqual(self.store.state("guest")["fields"]["check_in"], "2027-03-01")

    def test_edit_invalidates_confirmation_and_does_not_mix_old_contacts(self):
        self.ready()
        self.send("Алина 89991234567")
        old = self.store.state("guest")["quote_token"]
        self.send("/edit guest")
        self.send("Мария")
        self.send("/confirm " + old)
        self.assertEqual(self.count(), 0)
        self.send("8 999 000 00 00")
        new = self.store.state("guest")["quote_token"]
        self.send("/confirm " + old)
        self.assertEqual(self.count(), 0)
        self.send("/confirm " + new)
        self.send("/confirm " + new)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.store.state("guest")["guest"], {"name": "Мария", "phone": "89990000000"})

    def test_invalid_input_keeps_draft_and_edit_clears_it(self):
        self.ready()
        self.send("Алина")
        self.send("9" * 21)
        self.assertEqual(self.store.state("guest")["guest_draft"], {"name": "Алина"})
        self.send("89991234567, 89997654321")
        self.assertIsNone(self.store.state("guest")["guest"])
        self.send("/edit guest")
        self.send("89997654321")
        self.assertEqual(self.store.state("guest")["guest_draft"], {"phone": "89997654321"})
        self.send("/manager")
        self.send("Мария")
        self.assertIsNone(self.store.state("guest")["guest"])


if __name__ == "__main__":
    unittest.main()
