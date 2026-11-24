"""Explicit live Telegram delivery check; isolated DB, synthetic guest, no AI.

Run from project root: python tests/manual_cards.py --send with PYTHONPATH=.
"""

import argparse
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hotel_bot.config import Settings
from hotel_bot.engine import Engine
from hotel_bot.form import FormInterpreter
from hotel_bot.http import request_json
from hotel_bot.storage import Store
from hotel_bot.telegram import Telegram


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="Send test cards to the already bound manager")
    parser.add_argument("--photos", action="store_true", help="Also browse room photos and compare rooms")
    parser.add_argument("--mobile", action="store_true", help="Use calendar buttons, carousel and edit dates")
    args = parser.parse_args()
    if not args.send:
        parser.error("--send is required: this check sends actual Telegram messages")
    cfg = Settings.from_env()
    with sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True) as source:
        bot_id = source.execute("SELECT value FROM meta WHERE key='telegram_bot_id'").fetchone()[0]
        bound = source.execute("SELECT value FROM meta WHERE key=?", ("manager_id:" + bot_id,)).fetchone()
        manager = cfg.manager_id or (bound[0] if bound else "")
    if not manager:
        raise ValueError("Bind a manager first")
    cfg.manager_id = manager
    run = uuid.uuid4().hex[:10]
    cfg.db_path = str(Path("data") / f"cards-smoke-{run}.db")
    hotel = json.loads(Path("hotel_bot/hotel.json").read_text(encoding="utf-8"))
    store = Store(cfg.db_path, hotel)
    engine = Engine(store, FormInterpreter(), hotel)
    photo_deliveries = []
    def transport(url, payload, **kwargs):
        result = request_json(url, payload, **kwargs)
        if url.rsplit("/", 1)[-1] in {"sendPhoto", "editMessageMedia"} and result.get("ok") and result.get("result", {}).get("photo"):
            photo_deliveries.append(result["result"]["message_id"])
        return result
    bot = Telegram(cfg, engine, transport)
    if str(bot.call("getMe")["id"]) != bot_id:
        raise ValueError("Bot does not match the project's binding")
    bot.bot_id = bot_id
    session = f"tg:{bot_id}:{manager}:test"
    results = []

    def send(text, label):
        event = f"tg:{bot_id}:cards-smoke:{run}:{len(results)}"
        reply = engine.process(session, event, text)
        bot.queue(event, session, manager, reply)
        bot.flush()
        row = store.db.execute("SELECT status,message_id FROM outbox WHERE id=?", (event + ":guest",)).fetchone()
        if row["status"] != "sent":
            raise RuntimeError("Delivery not confirmed at " + label)
        results.append({"step": label, "message_id": row["message_id"], "card_key": reply.get("card_key"),
                        "buttons": [{"text": b["text"], "style": b.get("style")} for b in reply["buttons"]]})
        return reply

    def button(reply, text):
        return next(b["data"] for b in reply["buttons"] if b["text"] == text)

    def calendar_day(reply, day, label):
        # Follow only buttons actually displayed to the guest, including month navigation.
        while True:
            action = next((b["data"] for b in reply["buttons"] if b["data"].startswith("/day ") and b["data"].endswith(":" + day.isoformat())), None)
            if action:
                return send(action, label)
            reply = send(button(reply, "Месяц ›"), label + "_next_month")

    try:
        start = engine.today() + timedelta(days=18)
        end = start + timedelta(days=2)
        if args.mobile:
            reply = send("/new", "calendar")
            assert "Ближайшие свободные даты" not in reply["text"]
            calendar_id = results[-1]["message_id"]
            reply = calendar_day(reply, start, "check_in")
            reply = calendar_day(reply, end, "check_out")
            assert results[-1]["message_id"] == calendar_id
            reply = send(button(reply, "Продолжить"), "adults")
            reply = send(button(reply, "2"), "children")
            reply = send(button(reply, "Без детей"), "rooms")
            send(button(reply, "Свободные даты"), "available_dates")
            reply = send("/start", "back_to_rooms")
        else:
            reply = send(f"/stay {start} {end} 2 -", "rooms")
        if args.photos or args.mobile:
            reply = send(button(reply, "Смотреть фото"), "photo_standard")
            reply = send(button(reply, "Следующий"), "photo_family")
            reply = send(button(reply, "Следующий"), "photo_suite")
            reply = send(button(reply, "Сравнить"), "compare_rooms")
            assert len(photo_deliveries) == 3, "Telegram did not return three photo messages"
            assert len(set(photo_deliveries)) == 1, "Room browsing created multiple cards"
            reply = send(button(reply, "Выбрать: Стандарт"), "services")
        else:
            reply = send(button(reply, "Стандарт"), "services")
        breakfast_message_id = None
        if args.mobile:
            breakfast_reply = send(button(reply, "Фото завтрака"), "breakfast_photo")
            breakfast_message_id = results[-1]["message_id"]
            reply = send(button(breakfast_reply, "К бронированию"), "back_to_services")
        card_ids = [results[-1]["message_id"]]
        for key in ("breakfast", "transfer", "fruit", "breakfast", "transfer", "breakfast"):
            token = store.state(session)["service_token"]
            reply = send(f"/service {token}:{key}", "toggle_" + key)
            card_ids.append(results[-1]["message_id"])
        assert len(set(card_ids)) == 1, "Services were not edited in one message"
        send(button(reply, "Продолжить"), "contacts")
        if args.mobile:
            send("Есть парковка?", "faq_preserves_contacts_step")
            reply = send("Анна Тестовая", "contact_name")
            assert "Теперь напишите телефон" in reply["text"]
            reply = send("8 (999) 000-00-00", "preview")
        else:
            reply = send("Анна Тестовая | +79990000000", "preview")
        old_confirm = button(reply, "Подтвердить бронь")
        send(button(reply, "Условия и расчёт"), "conditions")
        send("/edit guest", "edit_contacts")
        reply = send("Мария Проверочная, 8 (999) 111-11-11" if args.mobile else "Мария Проверочная | +79991111111", "updated_preview")
        if args.mobile:
            old_dates_confirm = button(reply, "Подтвердить бронь")
            reply = send(button(reply, "Изменить даты"), "edit_dates")
            reply = calendar_day(reply, start + timedelta(days=4), "new_check_in")
            reply = calendar_day(reply, start + timedelta(days=7), "new_check_out")
            reply = send(button(reply, "Продолжить"), "repriced_preview")
            assert "19 600,00" in reply["text"] and "Мария Проверочная" in reply["text"]
            preview = reply
            rejected = send(old_dates_confirm, "stale_dates_confirm")
            assert "устарело" in rejected["text"]
            reply = preview
        confirm = button(reply, "Подтвердить бронь")
        reply = send(old_confirm, "stale_confirm")
        assert "устарело" in reply["text"]
        send(confirm, "confirmed")
        send(confirm, "repeat_confirm")
        assert store.db.execute("SELECT count(*) FROM bookings").fetchone()[0] == 1
        send("/bookings", "bookings")
        ident = store.state(session)["booking_id"]
        reply = send("/cancel " + ident, "cancel_preview")
        send(button(reply, "Да, отменить бронь"), "cancelled")
        booking = store.db.execute("SELECT status,total FROM bookings").fetchone()
        assert booking["status"] == "CANCELLED" and booking["total"] == (1960000 if args.mobile else 1340000)
        notices = store.db.execute("SELECT status,message_id FROM outbox WHERE id LIKE 'booking:%:notice'").fetchall()
        assert len(notices) == 2 and all(r["status"] == "sent" for r in notices)
        report = {"checked_at": datetime.now(timezone.utc).isoformat(), "database": cfg.db_path,
                  "transport": "real Telegram API; programmatic inputs, not UI clicks", "ai_calls": 0,
                  "booking": ident, "status": booking["status"], "total": booking["total"],
                  "service_message_id": card_ids[0], "service_edits": len(card_ids) - 1,
                  "photo_message_ids": photo_deliveries,
                  "calendar_message_id": calendar_id if args.mobile else None,
                  "breakfast_message_id": breakfast_message_id,
                  "manager_notice_ids": [r["message_id"] for r in notices], "steps": results}
        report_path = "docs/mobile-live-report.json" if args.mobile else "docs/rooms-live-report.json" if args.photos else "docs/cards-live-report.json"
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k != "steps"}, ensure_ascii=False))
    finally:
        # Leave no active test inventory even if a later delivery check fails.
        ident = store.state(session)["booking_id"]
        if ident:
            row = store.db.execute("SELECT status FROM bookings WHERE id=?", (ident,)).fetchone()
            if row[0] == "CONFIRMED":
                reply = engine.process(session, f"cleanup:{run}:1", "/cancel " + ident)
                cancel = button(reply, "Да, отменить бронь")
                engine.process(session, f"cleanup:{run}:2", cancel)
        store.close()


if __name__ == "__main__":
    main()
