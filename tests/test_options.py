import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

import test_booking as fixtures
from hotel_bot.engine import Engine
from hotel_bot.form import FormInterpreter
from hotel_bot.storage import Store


class OptionsTests(unittest.TestCase):
    setUp = fixtures.BookingTests.setUp
    tearDown = fixtures.BookingTests.tearDown
    send = fixtures.BookingTests.send
    prepare = fixtures.BookingTests.prepare
    count = fixtures.BookingTests.count

    def state(self):
        return self.store.state("guest")

    def booking(self):
        return self.store.db.execute("SELECT * FROM bookings ORDER BY rowid LIMIT 1").fetchone()

    def choose_services(self, breakfast="yes", extras=("transfer",)):
        token = self.state()["service_token"]
        self.send(f"/meal {token}:{breakfast}")
        for key in extras:
            self.send(f"/extras {token}:{key}")
        return self.send(f"/extras {token}:done")

    def booked(self):
        token = self.prepare(children="7")
        self.send("/confirm " + token)
        return self.state()["booking_id"]

    def test_breakfast_age_prices_extras_and_confirmed_snapshot(self):
        self.send("/stay 2027-02-01 2027-02-03 1 5,7,12")
        self.send("/choose 1")
        self.choose_services(extras=("transfer", "fruit"))
        reply = self.send("/guest Анна Тестовая | +79990000000")
        q = self.state()["selected"]
        self.assertEqual(q["room_total"], 1640000)
        self.assertEqual(q["meal_total"], 300000)
        self.assertEqual(q["total"], 2190000)
        self.assertIn("21 900,00", reply["text"])
        self.send("/confirm " + self.state()["quote_token"])
        snapshot = json.loads(self.booking()["snapshot"])
        self.assertEqual(snapshot["offer"], q)
        self.assertEqual(snapshot["fields"]["extras"], ["fruit", "transfer"])

    def test_edit_contacts_invalidates_previous_confirmation(self):
        old = self.prepare()
        self.send("/edit guest")
        self.engine.interpreter = FormInterpreter()
        self.send("Мария Тестовая | +79991111111")
        new = self.state()["quote_token"]
        self.send("/confirm " + old)
        self.assertEqual(self.count(), 0)
        self.send("/confirm " + new)
        self.assertEqual(json.loads(self.booking()["snapshot"])["guest"]["name"], "Мария Тестовая")

    def test_edit_dates_keeps_other_fields_and_rejects_old_buttons(self):
        old = self.prepare()
        old_button = self.state()["list_token"]
        self.send("/edit dates")
        self.engine.interpreter = FormInterpreter()
        self.send("01.03.2027")
        self.send("03.03.2027")
        self.assertTrue(self.state()["guest"])
        self.assertEqual(self.state()["fields"]["adults"], 2)
        self.send(f"/choose {old_button}:1")
        self.assertEqual(self.state()["selected"]["room_id"], "standard")
        self.send("/confirm " + old)
        self.assertEqual(self.count(), 0)
        self.send("/confirm " + self.state()["quote_token"])
        self.assertEqual(self.booking()["check_in"], "2027-03-01")

    def test_edit_services_reprices_and_rejects_finished_service_buttons(self):
        old = self.prepare()
        self.send("/edit services")
        token = self.state()["service_token"]
        self.choose_services(extras=("fruit", "fruit"))  # toggle on, then off
        self.assertEqual(self.state()["selected"]["total"], 1240000)
        self.assertEqual(self.state()["fields"]["extras"], [])
        self.send(f"/extras {token}:transfer")
        self.assertEqual(self.state()["fields"]["extras"], [])
        self.send("/confirm " + old)
        self.assertEqual(self.count(), 0)

    def test_cancel_requires_confirmation_and_frees_inventory(self):
        ident = self.booked()
        self.assertFalse(any(o["room_id"] == "family" for o in self.engine.pms.offers(self.state()["fields"])))
        preview = self.send("/cancel " + ident)
        self.assertEqual(self.booking()["status"], "CONFIRMED")
        command = preview["buttons"][0]["data"]
        self.send(command, "other")
        self.assertEqual(self.booking()["status"], "CONFIRMED")
        self.send(command, event="cancel_once")
        self.send(command, event="cancel_once")
        self.send(command)
        self.assertEqual(self.booking()["status"], "CANCELLED")
        self.assertEqual(self.count(), 1)
        self.assertTrue(any(o["room_id"] == "family" for o in self.engine.pms.offers(self.state()["fields"])))
        self.assertIn("отменена", self.send("/start")["text"])

    def test_keep_booking_and_expired_cancellation(self):
        ident = self.booked()
        preview = self.send("/cancel " + ident)
        self.send("/cancel_back")
        self.send(preview["buttons"][0]["data"])
        self.assertEqual(self.booking()["status"], "CONFIRMED")
        preview = self.send("/cancel " + ident)
        self.engine.clock = lambda: fixtures.NOW + timedelta(minutes=11)
        self.send(preview["buttons"][0]["data"])
        self.assertEqual(self.booking()["status"], "CONFIRMED")

    def test_old_booking_available_after_new_trip_and_restart(self):
        ident = self.booked()
        self.send("/new")
        self.store.close()
        self.store = Store(self.path, fixtures.HOTEL)
        self.engine = Engine(self.store, FormInterpreter(), fixtures.HOTEL, lambda: fixtures.NOW)
        item = self.send("/bookings")["buttons"][0]
        self.assertEqual(item["data"], "/booking " + ident)
        self.assertNotIn(ident, item["text"])
        self.assertIn("Семейный · 1–3 февраля 2027 · Активна", item["text"])
        preview = self.send("/cancel " + ident)
        self.send(preview["buttons"][0]["data"])
        self.assertEqual(self.booking()["status"], "CANCELLED")
        self.assertIsNone(self.state()["booking_id"])

    def test_foreign_booking_is_inaccessible(self):
        ident = self.booked()
        for command in ("/booking ", "/cancel "):
            self.assertIn("не найдена", self.send(command + ident, "other")["text"])
        self.assertEqual(self.booking()["status"], "CONFIRMED")

    def test_payment_is_rechecked_before_cancellation(self):
        ident = self.booked()
        self.engine.clock = lambda: datetime(2027, 1, 30, 10, 59, tzinfo=timezone.utc)
        preview = self.send("/cancel " + ident)
        snapshot = json.loads(self.booking()["snapshot"])
        snapshot["payment_status"] = "PAID"
        self.store.db.execute("UPDATE bookings SET snapshot=? WHERE id=?", (json.dumps(snapshot), ident))
        result = self.send(preview["buttons"][0]["data"])
        self.assertTrue(result["manager"])
        self.assertEqual(self.booking()["status"], "CONFIRMED")
        self.assertEqual(self.state()["mode"], "human")

    def test_unpaid_nonrefundable_demo_can_be_cancelled(self):
        self.prepare()
        self.send("/rate nonrefundable")
        self.send("/choose 1")
        self.send("/confirm " + self.state()["quote_token"])
        result = self.send("/cancel " + self.state()["booking_id"])
        self.assertFalse(result["manager"])
        self.assertEqual(self.booking()["status"], "CONFIRMED")
        self.send(result["buttons"][0]["data"])
        self.assertEqual(self.booking()["status"], "CANCELLED")

    def test_unpaid_demo_cancellation_on_arrival_day_and_after_stay(self):
        for current in (datetime(2027, 2, 1, 12, tzinfo=timezone.utc), datetime(2027, 3, 1, 12, tzinfo=timezone.utc)):
            self.engine.clock = lambda: fixtures.NOW
            self.send("/new")
            ident = self.booked()
            self.engine.clock = lambda: current
            result = self.send("/cancel " + ident)
            self.assertFalse(result["manager"])
            row = self.store.db.execute("SELECT status FROM bookings WHERE id=?", (ident,)).fetchone()
            self.assertEqual(row["status"], "CONFIRMED")
            result = self.send(result["buttons"][0]["data"])
            self.assertIn("booking_notice", result)
            self.assertEqual(self.store.db.execute("SELECT status FROM bookings WHERE id=?", (ident,)).fetchone()[0], "CANCELLED")

    def test_unknown_payment_status_requires_manager(self):
        ident = self.booked()
        snapshot = json.loads(self.booking()["snapshot"])
        snapshot.pop("payment_status")
        self.store.db.execute("UPDATE bookings SET snapshot=? WHERE id=?", (json.dumps(snapshot), ident))
        result = self.send("/cancel " + ident)
        self.assertTrue(result["manager"])
        self.assertEqual(self.booking()["status"], "CONFIRMED")

    def test_schema_upgrade_preserves_existing_booking_and_is_repeatable(self):
        ident = self.booked()
        self.store.close()
        db = sqlite3.connect(self.path)
        db.execute("ALTER TABLE bookings DROP COLUMN status")
        db.execute("PRAGMA user_version=1")
        db.commit()
        db.close()
        self.store = Store(self.path, fixtures.HOTEL)
        self.assertEqual(self.booking()["id"], ident)
        self.assertEqual(self.booking()["status"], "CONFIRMED")
        self.store.close()
        self.store = Store(self.path, fixtures.HOTEL)
        self.assertEqual(self.count(), 1)


if __name__ == "__main__":
    unittest.main()
