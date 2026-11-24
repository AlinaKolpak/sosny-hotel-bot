import copy
import unittest
from datetime import timedelta

import test_booking as fixtures
from hotel_bot.form import FormInterpreter


class AvailableDatesTests(unittest.TestCase):
    setUp = fixtures.BookingTests.setUp
    tearDown = fixtures.BookingTests.tearDown
    send = fixtures.BookingTests.send
    prepare = fixtures.BookingTests.prepare
    count = fixtures.BookingTests.count

    def state(self):
        return self.store.state("guest")

    @staticmethod
    def pick(reply):
        return next(b["data"] for b in reply["buttons"] if b["data"].startswith("/available_pick "))

    def occupy(self, start, end):
        for room in self.store.db.execute("SELECT id,stock FROM rooms").fetchall():
            for n in range(room["stock"]):
                ident = f"occupied-{room['id']}-{n}"
                self.store.db.execute("INSERT INTO bookings VALUES (?,?,?,?,?,?,?,?,?)",
                                      (ident, ident, "other", room["id"], start, end, 100, "{}", "CONFIRMED"))

    def test_initial_choices_are_available_and_do_not_assume_actual_party(self):
        reply = self.send("/new")
        self.assertIn("Выберите дату заезда", reply["text"])
        self.assertNotIn("Ближайшие свободные даты", reply["text"])
        self.assertFalse(any(b["data"].startswith("/available_pick ") for b in reply["buttons"]))
        action = next(b["data"] for b in reply["buttons"] if b["text"] == "Свободные даты")
        reply = self.send(action)
        self.assertIn("Ближайшие свободные даты", reply["text"])
        self.assertIn("предварительный", reply["text"])
        self.assertIn("Взрослых: 2", reply["text"])
        self.assertEqual(len(self.state()["date_options"]["items"]), 3)
        self.assertEqual(self.state()["fields"], {})
        self.send(self.pick(reply))
        self.assertEqual(self.state()["fields"], {"check_in": "2027-01-01", "check_out": "2027-01-03"})
        self.assertEqual(self.count(), 0)
        self.assertIsNone(self.state()["quote_token"])

    def test_no_inventory_suggests_full_period_after_occupied_dates(self):
        self.occupy("2027-02-01", "2027-02-05")
        reply = self.send("/stay 2027-02-01 2027-02-04 2 7")
        self.assertIn("свободных подходящих номеров", reply["text"])
        options = self.state()["date_options"]["items"]
        self.assertEqual(options[0]["check_in"], "2027-02-05")
        self.assertEqual(options[0]["check_out"], "2027-02-08")
        self.assertEqual([o["room_id"] for o in options[0]["offers"]], ["family", "suite"])
        self.send(self.pick(reply))
        self.assertEqual(self.state()["fields"]["children_ages"], [7])
        self.assertEqual(self.state()["fields"]["check_in"], "2027-02-05")
        self.assertEqual(self.state()["offers"][0]["total"], (700000 + 40000) * 3)

    def test_rechecks_inventory_and_price_before_adopting_dates(self):
        reply = self.send("/available_dates")
        action = self.pick(reply)
        self.store.db.execute("UPDATE rooms SET nightly=nightly+100")
        updated = self.send(action)
        self.assertIn("Наличие или цены изменились", updated["text"])
        self.assertEqual(self.state()["fields"], {})
        self.assertNotEqual(action, self.pick(updated))
        self.store.db.execute("UPDATE rooms SET stock=0")
        updated = self.send(self.pick(updated))
        self.assertEqual(self.state()["date_options"]["items"], [])
        self.assertIn("не найдено", updated["text"])
        self.assertEqual(self.state()["fields"], {})

    def test_stale_foreign_repeated_and_out_of_bounds_buttons(self):
        first = self.send("/available_dates")
        action = self.pick(first)
        self.send(action, "other")
        self.assertEqual(self.store.state("other")["fields"], {})
        self.send("/adults 3")
        reply = self.send(action)
        self.assertIn("устарел", reply["text"])
        current = self.send("/available_dates")
        action = self.pick(current)
        bad = self.send(action.rsplit(":", 1)[0] + ":999")
        self.assertIn("устарел", bad["text"])
        self.send(action)
        before = copy.deepcopy(self.state()["fields"])
        self.send(action)
        self.assertEqual(self.state()["fields"], before)
        self.assertEqual(self.count(), 0)

    def test_preview_date_change_preserves_contacts_services_and_invalidates_quote(self):
        old = self.prepare()
        self.send("/edit services")
        token = self.state()["service_token"]
        self.send(f"/service {token}:breakfast")
        token = self.state()["service_token"]
        self.send(f"/service {token}:fruit")
        reply = self.send("/available_dates")
        second = [b["data"] for b in reply["buttons"] if b["data"].startswith("/available_pick ")][1]
        self.send(second)
        self.assertEqual(self.state()["fields"]["check_in"], "2027-02-02")
        self.assertTrue(self.state()["guest"])
        self.assertEqual(self.state()["selected"]["total"], 1340000)
        self.send("/confirm " + old)
        self.assertEqual(self.count(), 0)
        confirm = self.state()["quote_token"]
        self.send("/confirm " + confirm)
        self.send("/confirm " + confirm)
        self.assertEqual(self.count(), 1)

    def test_year_limit_and_impossible_capacity(self):
        end = self.engine.today() + timedelta(days=365)
        start = end - timedelta(days=2)
        self.send(f"/stay {start} {end} 2 -")
        self.send("/available_dates")
        self.assertEqual(len(self.state()["date_options"]["items"]), 1)
        reply = self.send("/stay 2027-02-01 2027-02-03 4 7,10,12")
        self.assertIn("не найдено", reply["text"])
        self.assertEqual(self.state()["date_options"]["items"], [])

    def test_unknown_child_ages_are_not_invented(self):
        self.send("/stay 2027-02-01 2027-02-03 2 -")
        self.send("/children 2")
        reply = self.send("/available_dates")
        self.assertIn("возраст каждого ребёнка", reply["text"])
        self.assertFalse(any(b["data"].startswith("/available_pick ") for b in reply["buttons"]))


if __name__ == "__main__":
    unittest.main()
