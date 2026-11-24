"""Сценарий бронирования, общий для консоли и Telegram."""

import copy
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone

from .http import ServiceError
from . import calendar as date_calendar
from .contacts import PHONE, contact_prompt, parse_contacts
from .cards import DEMO_NOTE, RATES, button_style, compare_rooms, details, party, room_card, stay_dates, summary
from .knowledge import TOPICS, question_topics
from .pms import DemoPMS, InputError, money, missing_question, validate_fields
from .storage import new_state

MOSCOW = timezone(timedelta(hours=3))
HELP = """🌲 Добро пожаловать в учебный отель «Сосны».
Выберите даты и номер, добавьте услуги и проверьте итоговую карточку.
Изменить данные можно до подтверждения. Созданные брони доступны в «Моих бронях».
Об отеле можно спросить обычным сообщением.
Учебная бронь, оплата не требуется."""


def response(text=None, buttons=None, manager=None):
    return {"text": text, "buttons": buttons or [], "manager": manager}


class Engine:
    def __init__(self, store, interpreter, hotel, clock=None):
        self.store = store
        self.interpreter = interpreter
        self.hotel = hotel
        self.pms = DemoPMS(store.db, hotel["services"])
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def today(self):
        return self.clock().astimezone(MOSCOW).date()

    def process(self, session, event_id, text):
        cached = self.store.event(event_id)
        if cached is not None:
            return cached
        before = self.store.state(session)
        if before.get("editing") == "wishes" and not text.startswith("/") and text.casefold() not in {"менеджер", "оператор"} and question_topics(text) is None:
            text = "/wishes " + text
        if before["mode"] == "bot" and before["selected"] and not text.startswith("/"):
            awaiting_contacts = (not before["guest"] and not before.get("service_token")
                                 and "breakfast" in before["fields"] and "extras" in before["fields"])
            topics = question_topics(text)
            simple_name = len(text.split()) == 1 and "?" not in text and not topics
            if "|" in text or (awaiting_contacts and text.casefold().strip() not in {"менеджер", "оператор"}
                               and (topics is None or simple_name or (PHONE.search(text) and not topics and "?" not in text))):
                text = "/guest " + text
        action = None
        if before["mode"] == "bot" and not text.startswith("/") and len(text) <= 2000:
            # Contact details and confirmation tokens are never included in model context.
            context = {"fields": before["fields"], "last_prompt": before.get("last_prompt", ""),
                       "offers": [{"number": i + 1, "name": o["room_name"]}
                                  for i, o in enumerate(before["offers"]) if not before["selected"]]}
            try:
                action = self.interpreter.interpret(text, context, self.today())
            except ServiceError:
                action = {"intent": "unavailable", "fields": {}}
        db = self.store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            cached = self.store.event(event_id)
            if cached is not None:
                db.execute("COMMIT")
                return cached
            state = self.store.state(session)
            if state["revision"] != before["revision"]:
                reply = response("Параметры диалога уже изменились. Повторите сообщение.")
            else:
                original = copy.deepcopy(state)
                db.execute("SAVEPOINT action")
                try:
                    reply = self._apply(session, state, text.strip(), action)
                except InputError as exc:
                    db.execute("ROLLBACK TO action")
                    state = original
                    reply = response(str(exc), [{"text": "Продолжить", "data": "/start"}])
                db.execute("RELEASE action")
            if reply["text"]:
                if state["mode"] == "bot":
                    is_menu = text.strip() in {"/menu", "/help"}
                    if not is_menu and not state["booking_id"] and not any(b["data"] == "/edit" for b in reply["buttons"]):
                        reply["buttons"].append({"text": "Изменить данные", "data": "/edit"})
                    if not is_menu and not any(b["data"] == "/menu" for b in reply["buttons"]):
                        reply["buttons"].append({"text": "Меню", "data": "/menu"})
                    for button in reply["buttons"]:
                        style = button_style(button["data"])
                        if style:
                            button["style"] = style
                state["last_prompt"] = reply["text"] if not state["guest"] else "Контакты собраны; подтвердите карточку или исправьте параметры."
            state["revision"] += 1
            self.store.save(session, state)
            db.execute("INSERT INTO events(id,session_id,reply) VALUES (?,?,?)",
                       (event_id, session, json.dumps(reply, ensure_ascii=False)))
            db.execute("COMMIT")
            return reply
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def _apply(self, session, state, text, action):
        if state["mode"] != "bot":
            return response(manager=f"Сообщение гостя {session}: {text[:2000]}")
        if len(text) > 2000:
            raise InputError("Сообщение слишком длинное: максимум 2000 символов.")
        command, _, argument = text.partition(" ")
        if command == "/noop":
            return response()
        if command in {"/help", "/menu"}:
            buttons = [{"text": "Подобрать номер", "data": "/new"},
                       {"text": "Мои брони", "data": "/bookings"}, {"text": "Об отеле", "data": "/faq"}]
            if not state["booking_id"] and (state["fields"] or state.get("calendar")):
                buttons.insert(0, {"text": "Продолжить бронирование", "data": "/start"})
            return response(HELP, buttons)
        if command == "/start":
            if state["booking_id"]:
                return self._booking(session, state["booking_id"])
            reply = self._advance(state)
            reply["text"] = "Добро пожаловать в учебный отель «Сосны»! Все брони тестовые.\n\n" + reply["text"]
            return reply
        if command == "/unsupported":
            return response("В первой версии поддерживаются текстовые сообщения. Напишите запрос текстом.")
        if command == "/manager" or (action and action["intent"] == "handoff"):
            return self._handoff(session, state, text)
        if command == "/new":
            revision = state["revision"]
            state.clear()
            state.update(new_state())
            state["revision"] = revision
            return self._advance(state)
        if command == "/status":
            if state["booking_id"]:
                return self._booking(session, state["booking_id"])
            return self._advance(state)
        if command == "/confirm":
            return self._confirm(session, state, argument)
        if command == "/bookings":
            rows = self.store.db.execute("SELECT id,status,check_in,check_out,snapshot FROM bookings WHERE session_id=? ORDER BY rowid DESC", (session,)).fetchall()
            return response("Ваши учебные брони:" if rows else "У вас пока нет броней.",
                            [{"text": json.loads(r["snapshot"])["offer"]["room_name"] + " · " + stay_dates(r["check_in"], r["check_out"]) + (" · Отменена" if r["status"] == "CANCELLED" else " · Активна"),
                              "data": "/booking " + r["id"]} for r in rows[:30]] +
                            [{"text": "Продолжить анкету", "data": "/start"}, {"text": "Новая поездка", "data": "/new"}])
        if command in {"/conditions", "/preview"}:
            if not argument or argument != state.get("quote_token") or not state["selected"] or not state["guest"]:
                raise InputError("Эта карточка устарела. Вернитесь к бронированию.")
            if state["booking_id"]:
                return self._booking(session, state["booking_id"])
            if command == "/preview":
                return self._advance(state)
            return response(details(state["selected"], state["fields"]),
                            [{"text": "К итоговой карточке", "data": "/preview " + argument}])
        if command == "/booking_conditions":
            row = self._owned_booking(session, argument)
            snap = json.loads(row["snapshot"])
            return response(details(snap["offer"], snap["fields"]),
                            [{"text": "К брони", "data": "/booking " + argument}])
        if command == "/booking":
            return self._booking(session, argument)
        if command == "/breakfast_photo":
            prices = self.hotel["services"]["breakfast"]
            reply = response("☕ Завтрак\n\n"
                             f"От 12 лет — {money(prices['adult'])}, 6–11 лет — {money(prices['child'])}, до 6 лет — бесплатно за день.\n"
                             "Завтрак добавляется на всех гостей на весь срок проживания.\n\n"
                             "Учебная иллюстрация. Это не фото завтрака отеля «Сосны» и не обещание конкретного меню.",
                             [{"text": "К бронированию", "data": "/start"}, {"text": "Об отеле", "data": "/faq"}])
            reply["photo"] = self.hotel.get("breakfast_photo", "missing.jpg")
            reply["card_key"] = "breakfast-photo"
            return reply
        if command in {"/cancel", "/cancel_confirm", "/cancel_back"}:
            return self._cancel(session, state, command, argument)
        if command == "/faq" or (action and action["intent"] == "faq"):
            topics = [argument] if command == "/faq" else action.get("faq_topics", [action.get("faq_topic")])
            answers = [self.hotel["faq"][topic] for topic in topics if topic in self.hotel["faq"]]
            if 1 <= len(answers) <= 2 and len("\n\n".join(answers)) <= 1500:
                reply = self._faq_continue(session, state)
                reply.pop("card_key", None)
                reply.pop("photo", None)
                reply["text"] = "Справка учебного отеля:\n" + "\n\n".join(answers) + "\n\nПродолжим с сохранённого шага.\n" + reply["text"]
                reply["buttons"].append({"text": "Уточнить у менеджера", "data": "/manager"})
                reply["buttons"].append({"text": "Продолжить бронирование", "data": "/start"})
                if "breakfast" in topics:
                    reply["buttons"].append({"text": "Фото завтрака", "data": "/breakfast_photo"})
                return reply
            menu = [{"text": label, "data": "/faq " + key} for key, label in TOPICS.items()]
            reply = self._faq_continue(session, state)
            reply.pop("card_key", None)
            reply.pop("photo", None)
            prefix = ("Выберите тему справки." if command == "/faq" and not argument else
                      "Вопрос затрагивает несколько тем. Выберите одну, чтобы прочитать полный ответ." if answers else
                      "Точного ответа в базе знаний нет. Выберите тему или уточните у менеджера.")
            reply["text"] = prefix + " Данные сохранены.\n\n" + reply["text"]
            reply["buttons"] = menu + [{"text": "Продолжить бронирование", "data": "/start"}, {"text": "Уточнить у менеджера", "data": "/manager"}]
            return reply
        if state["booking_id"]:
            return self._booking(session, state["booking_id"])
        if command == "/available_dates":
            return self._date_options(state)
        if command == "/available_pick":
            return self._pick_dates(state, argument)
        if command in {"/day", "/month", "/date_text"}:
            return date_calendar.apply(state, self.today(), command, argument)
        if command in {"/room", "/compare", "/rooms"}:
            return self._browse_rooms(state, command, argument)
        if command == "/edit":
            return self._edit(state, argument)
        if command in {"/meal", "/extras", "/service"}:
            return self._services(state, command, argument)
        if command == "/guest":
            draft = {**state.get("guest_draft", {}), **parse_contacts(argument)}
            state["guest"] = draft if "name" in draft and "phone" in draft else None
            if state["guest"]:
                state.pop("guest_draft", None)
            else:
                state["guest_draft"] = draft
            state["quote_token"] = None
            return self._advance(state)
        if command == "/choose" or (action and action["intent"] == "select" and not action.get("fields")):
            value = argument if command == "/choose" else str(action.get("offer_number", ""))
            if ":" in value:
                generation, value = value.split(":", 1)
                if generation != state.get("list_token"):
                    raise InputError("Этот список устарел. Откройте варианты заново.")
            if not value.isdecimal() or not 1 <= int(value) <= len(state["offers"]):
                raise InputError("Выберите номер кнопкой из предложенных вариантов.")
            state["selected"] = state["offers"][int(value) - 1]
            state["quote_token"] = None
            state.pop("service_token", None)
            state.pop("service_card_id", None)
            return self._advance(state)
        changes = {}
        if command == "/stay":
            parts = argument.split()
            if len(parts) != 4:
                raise InputError("Укажите даты и состав гостей через анкету.")
            try:
                ages = [] if parts[3] == "-" else [int(a) for a in parts[3].split(",")]
                changes = {"check_in": parts[0], "check_out": parts[1], "adults": int(parts[2]),
                           "children_count": len(ages), "children_ages": ages}
            except ValueError:
                raise InputError("Количество взрослых и возраст детей должны быть целыми числами.") from None
        elif command == "/rate":
            changes = {"rate": argument}
        elif command == "/wishes":
            changes = {"wishes": argument}
            state.pop("editing", None)
        elif command in {"/adults", "/children"}:
            if not re.fullmatch(r"\d{1,2}", argument):
                raise InputError("Укажите количество целым числом.")
            changes = {"adults" if command == "/adults" else "children_count": int(argument)}
        elif command == "/search":
            state["selected"] = None
            state["quote_token"] = None
        elif command.startswith("/"):
            return response("Не удалось распознать действие. Откройте меню или продолжите анкету.")
        elif action:
            if action["intent"] == "form_retry":
                reply = self._advance(state)
                prefix = ("Разбор свободного текста сейчас недоступен. Продолжим по анкете — данные сохранены."
                          if action.get("fallback") else "Пожалуйста, используйте указанный формат или кнопку.")
                reply["text"] = prefix + "\n\n" + reply["text"]
                return reply
            if action["intent"] == "unavailable":
                return response("Разбор сообщения сейчас недоступен. Параметры сохранены. Продолжите анкету или свяжитесь с менеджером через меню.")
            if action["intent"] == "demo_help":
                return response("Режим без ИИ: используйте кнопки.\n" + HELP)
            if action["intent"] == "clarify":
                if state["selected"] and state["guest"]:
                    return self._advance(state)
                return response("Уточните сообщение: укажите даты с годом, состав гостей или номер варианта. Подтверждение брони — отдельной кнопкой.")
            changes = action.get("fields", {})
        if changes:
            new_fields = {**state["fields"], **changes}
            if changes.get("children_count") == 0:
                new_fields["children_ages"] = []
            elif "children_count" in changes and "children_ages" not in changes and changes["children_count"] != state["fields"].get("children_count"):
                new_fields["children_ages"] = []
            validate_fields(new_fields, self.today())
            if new_fields != state["fields"]:
                if "check_in" in changes or "check_out" in changes:
                    state.pop("calendar", None)
                    if "check_out" in new_fields:
                        state.pop("calendar_card", None)
                state["fields"] = new_fields
                state["offers"] = []
                state["selected"] = None
                state["quote_token"] = None
                state.pop("service_token", None)
        return self._advance(state)

    def _faq_continue(self, session, state):
        pending = state.get("cancel_pending")
        if pending:
            return response(f"Отмена брони {pending['id']} ещё не подтверждена. Для проверки условий откройте отмену заново.",
                            [{"text": "Проверить отмену", "data": "/cancel " + pending["id"]},
                             {"text": "Сохранить бронь", "data": "/cancel_back"}])
        if state["booking_id"]:
            return self._booking(session, state["booking_id"])
        return self._advance(state)

    def _edit(self, state, section):
        sections = {"dates": "Даты", "party": "Состав гостей", "room": "Номер",
                    "services": "Завтрак и услуги", "guest": "Имя и телефон", "wishes": "Пожелания"}
        if not section:
            return response("Что изменить? После изменения проверьте новую итоговую карточку.",
                            [{"text": label, "data": "/edit " + key} for key, label in sections.items()] +
                            [{"text": "Продолжить", "data": "/start"}])
        if section not in sections:
            raise InputError("Нажмите «Изменить данные» и выберите раздел.")
        state["quote_token"] = None
        if section == "dates":
            if "check_in" in state["fields"] and "check_out" in state["fields"]:
                state["suggested_nights"] = (date.fromisoformat(state["fields"]["check_out"]) - date.fromisoformat(state["fields"]["check_in"])).days
            if state["selected"]:
                state["preferred_room"] = state["selected"]["room_id"]
            if "extra_choices" in state:
                state["fields"]["extras"] = sorted(state.pop("extra_choices"))
            state.pop("calendar", None)
            state.pop("calendar_card", None)
        elif section in {"room", "party"}:
            state.pop("preferred_room", None)
        state.pop("service_token", None)
        state.pop("editing", None)
        keys = {"dates": ["check_in", "check_out"], "party": ["adults", "children_count", "children_ages"],
                }.get(section, [])
        for key in keys:
            state["fields"].pop(key, None)
        if section in {"dates", "party", "room"}:
            state["selected"] = None
            state["offers"] = []
            state.pop("list_token", None)
        if section == "services":
            state["extra_choices"] = list(state["fields"].get("extras", []))
            state["service_token"] = uuid.uuid4().hex[:12]
            state["service_card_id"] = uuid.uuid4().hex[:12]
        if section == "guest":
            state["guest"] = None
            state.pop("guest_draft", None)
        if section == "wishes":
            state["editing"] = "wishes"
        return self._advance(state)

    def _service_question(self, state):
        token = state.setdefault("service_token", uuid.uuid4().hex[:12])
        group = state.setdefault("service_card_id", uuid.uuid4().hex[:12])
        choices = state.setdefault("extra_choices", list(state["fields"].get("extras", [])))
        fields = {**state["fields"], "extras": sorted(choices)}
        current = next((o for o in self.pms.offers(fields) if o["room_id"] == state["selected"]["room_id"]), None)
        if current is None:
            state["selected"] = None
            state["quote_token"] = None
            state.pop("service_token", None)
            state.pop("service_card_id", None)
            return self._advance(state)
        with_breakfast = next(o for o in self.pms.offers({**fields, "breakfast": True}) if o["room_id"] == current["room_id"])
        breakfast = fields.get("breakfast", False)
        lines = ["☕ Добавьте услуги", f"{current['room_name']} · {stay_dates(fields['check_in'], fields['check_out'])}",
                 party(fields), "", f"Проживание: {money(current['room_total'])}",
                 ("✓ " if breakfast else "") + f"Завтрак: {money(with_breakfast['meal_total'])} за весь срок"]
        buttons = [{"text": ("✓ " if breakfast else "+ ") + "Завтрак", "data": f"/service {token}:breakfast"}]
        for key, item in self.hotel["services"]["extras"].items():
            mark = "✓ " if key in choices else "+ "
            lines.append(mark + item["name"] + ": " + money(item["price"]))
            buttons.append({"text": mark + item["name"], "data": f"/service {token}:{key}"})
        lines += ["", f"Итого с выбранными услугами: {money(current['total'])}",
                  "Нажмите на услугу, чтобы добавить или убрать.", DEMO_NOTE]
        buttons.append({"text": "Продолжить", "data": f"/service {token}:done"})
        buttons.append({"text": "Фото завтрака", "data": "/breakfast_photo"})
        reply = response("\n".join(lines), buttons)
        reply["card_key"] = group
        return reply

    def _services(self, state, command, argument):
        token, _, value = argument.partition(":")
        if not state["selected"] or not token or token != state.get("service_token"):
            raise InputError("Эта кнопка устарела. Нажмите «Продолжить», чтобы открыть текущую карточку.")
        fields = state["fields"]
        choices = state.setdefault("extra_choices", list(fields.get("extras", [])))
        if command == "/meal":  # Compatibility with buttons already in chat history.
            if value not in {"yes", "no"}:
                raise InputError("Выберите завтрак кнопкой.")
            fields["breakfast"] = value == "yes"
        elif value == "done":
            fields.setdefault("breakfast", False)
            fields["extras"] = sorted(choices)
            state.pop("service_token", None)
            state.pop("service_card_id", None)
            state.pop("extra_choices", None)
        elif command == "/service" and value == "breakfast":
            fields["breakfast"] = not fields.get("breakfast", False)
        elif value in self.hotel["services"]["extras"]:
            choices.remove(value) if value in choices else choices.append(value)
        else:
            raise InputError("Выберите услугу кнопкой.")
        state["quote_token"] = None
        if command == "/service" and value != "done":
            # Old versions cannot toggle twice if callbacks arrive out of order.
            state["service_token"] = uuid.uuid4().hex[:12]
        return self._advance(state)

    @staticmethod
    def _extra_text(offer):
        return "; ".join(item["name"] + " — " + money(item["price"]) for item in offer.get("extras", [])) or "не выбраны"

    def _owned_booking(self, session, booking_id):
        row = self.store.db.execute("SELECT * FROM bookings WHERE id=? AND session_id=?", (booking_id, session)).fetchone()
        if row is None:
            raise InputError("Бронь не найдена. Выберите свою бронь в «Моих бронях».")
        return row

    def _booking(self, session, booking_id):
        row = self._owned_booking(session, booking_id)
        snap = json.loads(row["snapshot"])
        q, g = snap["offer"], snap["guest"]
        text = ("Бронь отменена" if row["status"] == "CANCELLED" else "✅ Бронь подтверждена")
        text += "\n\n" + summary(q, snap["fields"], g) + "\nНомер брони: " + booking_id
        buttons = []
        if row["status"] == "CONFIRMED":
            buttons = [{"text": "Отменить бронь", "data": "/cancel " + booking_id},
                       {"text": "Изменить созданную бронь — менеджер", "data": "/manager"}]
        return response(text, buttons + [{"text": "Условия и расчёт", "data": "/booking_conditions " + booking_id}, {"text": "Новая поездка", "data": "/new"}])

    def _booking_changed(self, session, booking_id):
        """Attach an immutable notice only to an actual booking transition."""
        row = self._owned_booking(session, booking_id)
        snap = json.loads(row["snapshot"])
        fields, offer, guest = snap["fields"], snap["offer"], snap["guest"]
        cancelled = row["status"] == "CANCELLED"
        text = (f"{'ОТМЕНА БРОНИ' if cancelled else 'НОВАЯ БРОНЬ'} · {booking_id}\n"
                f"{self.hotel['name']} · {offer['room_name']}\n"
                f"{row['check_in']} → {row['check_out']}, ночей: {offer['nights']}\n"
                f"Гость: {guest['name']}, {guest['phone']}\n"
                f"Взрослых: {fields['adults']}; детей: {fields['children_count']}; "
                f"возраст: {fields.get('children_ages', [])}\n"
                f"Тариф: {offer['rate']}. {offer['meal']}\nУслуги: {self._extra_text(offer)}\n"
                f"{'Сумма отменённой брони' if cancelled else 'Итого'}: {money(row['total'])}\n"
                f"Пожелания: {fields.get('wishes', 'нет')}\n"
                + ("Отмена подтверждена. Номер освобождён.\n" if cancelled else "Бронь подтверждена.\n")
                + "Учебная бронь; реальная оплата не проводилась.")
        reply = self._booking(session, booking_id)
        reply["booking_notice"] = {"id": f"booking:{booking_id}:{row['status']}:notice", "text": text}
        return reply

    def _free_cancellation(self, row):
        snap = json.loads(row["snapshot"])
        # Only local, explicitly unpaid demo bookings may bypass simulated tariff deadlines.
        return row["id"].startswith("DEMO-") and snap.get("payment_status") == "NOT_REQUESTED"

    def _cancel(self, session, state, command, argument):
        if command == "/cancel_back":
            pending = state.pop("cancel_pending", None)
            return self._booking(session, pending["id"]) if pending else response("Отмена не выполнялась. Бронь доступна в «Моих бронях».")
        if command == "/cancel":
            row = self._owned_booking(session, argument or state["booking_id"])
            if row["status"] == "CANCELLED":
                return self._booking(session, row["id"])
            if not self._free_cancellation(row):
                reply = self._handoff(session, state, "Отмена брони " + row["id"] + ": требуется проверка оплаты или возврата.")
                reply["text"] = "Не удалось подтвердить, что это неоплаченная учебная бронь. Отмену и возможный возврат проверит менеджер.\n\n" + reply["text"]
                return reply
            token = uuid.uuid4().hex[:24]
            state["cancel_pending"] = {"id": row["id"], "token": token,
                                       "expires": (self.clock() + timedelta(minutes=10)).isoformat()}
            return response(f"Отменить бронь?\n{json.loads(row['snapshot'])['offer']['room_name']} · {stay_dates(row['check_in'], row['check_out'])}\n"
                            "Учебную бронь без оплаты можно отменить бесплатно в любое время. Возвращать деньги не нужно.\n"
                            "Номер освободится только после подтверждения.",
                            [{"text": "Да, отменить бронь", "data": "/cancel_confirm " + token},
                             {"text": "Сохранить бронь", "data": "/cancel_back"}])
        pending = state.get("cancel_pending")
        if not pending or not argument or argument != pending["token"] or self.clock() >= datetime.fromisoformat(pending["expires"]):
            raise InputError("Подтверждение отмены устарело. Откройте «Мои брони» и выберите отмену заново.")
        row = self._owned_booking(session, pending["id"])
        if row["status"] == "CANCELLED":
            return self._booking(session, row["id"])
        if not self._free_cancellation(row):
            state.pop("cancel_pending", None)
            return self._handoff(session, state, "Условия отмены брони " + row["id"] + " изменились; нужна проверка менеджера.")
        snapshot = json.loads(row["snapshot"])
        snapshot["cancelled_at"] = self.clock().isoformat()
        self.store.db.execute("UPDATE bookings SET status='CANCELLED',snapshot=? WHERE id=? AND session_id=? AND status='CONFIRMED'",
                              (json.dumps(snapshot, ensure_ascii=False), row["id"], session))
        state.pop("cancel_pending", None)
        return self._booking_changed(session, row["id"])

    def _advance(self, state):
        fields = state["fields"]
        if "check_in" in fields and date.fromisoformat(fields["check_in"]) < self.today():
            reply = self._edit(state, "dates")
            reply["text"] = "Дата заезда в сохранённой анкете уже прошла. Выберите новые даты — остальные данные сохранены.\n\n" + reply["text"]
            return reply
        if state.get("editing") == "wishes":
            return response("Напишите пожелания к размещению или «нет». Пожелания согласует отель, они не гарантированы.")
        validate_fields(fields, self.today())
        question = missing_question(fields)
        if question:
            if "check_in" not in fields or "check_out" not in fields:
                return date_calendar.render(state, self.today())
            buttons = []
            if "check_in" in fields and "check_out" in fields:
                if "adults" not in fields:
                    buttons = [{"text": str(n), "data": f"/adults {n}"} for n in range(1, 5)]
                elif "children_count" not in fields:
                    buttons = [{"text": "Без детей" if n == 0 else str(n), "data": f"/children {n}"}
                               for n in range(4)]
            nights = (datetime.fromisoformat(fields["check_out"]) - datetime.fromisoformat(fields["check_in"])).days
            return response(f"{stay_dates(fields['check_in'], fields['check_out'])} · Ночей: {nights}\n\n" + question, buttons)
        preferred = state.pop("preferred_room", None)
        if preferred:
            state["selected"] = next((o for o in self.pms.offers(fields) if o["room_id"] == preferred), None)
            if not state["selected"]:
                reply = self._advance(state)
                reply["text"] = "Выбранный ранее номер на новые даты недоступен. Выберите другой вариант.\n\n" + reply["text"]
                return reply
        if state["selected"]:
            if state.get("service_token") or "breakfast" not in fields or "extras" not in fields:
                return self._service_question(state)
            current = next((o for o in self.pms.offers(fields) if o["room_id"] == state["selected"]["room_id"]), None)
            if current is None:
                state["selected"] = None
                state["quote_token"] = None
                return self._advance(state)
            if current != state["selected"]:
                state["selected"] = current
                state["quote_token"] = None
            if not state["guest"]:
                return response(contact_prompt(state.get("guest_draft", {})))
            return self._preview(state)
        offers = self.pms.offers(fields)
        if offers != state["offers"] or not state.get("list_token"):
            state["list_token"] = uuid.uuid4().hex[:12]
        state["offers"] = offers
        if not offers:
            reply = self._date_options(state)
            reply["text"] = "На этот состав и даты свободных подходящих номеров в учебном отеле нет.\n\n" + reply["text"]
            return reply
        return self._room_list(state)

    def _date_options(self, state):
        original = state["fields"]
        fields = {"adults": 2, "children_count": 0, "children_ages": [], **original}
        if "extra_choices" in state:
            fields["extras"] = sorted(state["extra_choices"])
        if len(fields.get("children_ages", [])) != fields["children_count"]:
            state.pop("date_options", None)
            return response("Для подбора свободных дат нужен возраст каждого ребёнка.",
                            [{"text": "Уточнить состав гостей", "data": "/edit party"}])
        nights = state.get("suggested_nights", 2)
        if "check_in" in original and "check_out" in original:
            nights = (date.fromisoformat(original["check_out"]) - date.fromisoformat(original["check_in"])).days
        start = max(self.today(), date.fromisoformat(original.get("check_in", self.today().isoformat())))
        options = self.pms.available_dates(fields, start, nights, self.today())
        token = uuid.uuid4().hex[:12]
        state["date_options"] = {"token": token, "fields": copy.deepcopy(original), "extras": copy.deepcopy(state.get("extra_choices")), "basis": fields, "items": options}
        lines = ["Ближайшие свободные даты", f"{party(fields)} · Ночей: {nights}"]
        if "adults" not in original or "children_count" not in original:
            lines.append("Это предварительный подбор: состав гостей ещё не полностью указан. После уточнения проверим варианты заново.")
        lines.append("Цена за весь период с текущими услугами. Номер пока не забронирован.")
        buttons = []
        for i, item in enumerate(options, 1):
            period = stay_dates(item["check_in"], item["check_out"])
            minimum = min(o["total"] for o in item["offers"])
            names = ", ".join(o["room_name"] for o in item["offers"])
            lines.append(f"\n{period} — от {money(minimum)}\n{names}")
            buttons.append({"text": period, "data": f"/available_pick {token}:{i}"})
        if not options:
            lines.append(f"Свободных периодов на {nights} ночей с {start.strftime('%d.%m.%Y')} до {(self.today() + timedelta(days=365)).strftime('%d.%m.%Y')} для этого состава не найдено. Можно изменить длительность или обратиться к менеджеру.")
        buttons += [{"text": "Изменить даты", "data": "/edit dates"}, {"text": "Уточнить у менеджера", "data": "/manager"}]
        return response("\n".join(lines), buttons)

    def _pick_dates(self, state, argument):
        token, _, index = argument.partition(":")
        saved = state.get("date_options")
        if not saved or token != saved["token"] or saved["fields"] != state["fields"] or saved["extras"] != state.get("extra_choices") or not index.isdecimal() or not 1 <= int(index) <= len(saved["items"]):
            raise InputError("Этот подбор устарел. Нажмите «Свободные даты», чтобы проверить варианты заново.")
        item = saved["items"][int(index) - 1]
        dates = {"check_in": item["check_in"], "check_out": item["check_out"]}
        fields = {**saved["basis"], **dates}
        validate_fields(fields, self.today())
        current = self.pms.offers(fields)
        if current != item["offers"]:
            reply = self._date_options(state)
            reply["text"] = "Наличие или цены изменились. Выберите актуальное предложение.\n\n" + reply["text"]
            return reply
        if state["selected"]:
            state["preferred_room"] = state["selected"]["room_id"]
        state["fields"].update(dates)
        if "extra_choices" in state:
            state["fields"]["extras"] = sorted(state.pop("extra_choices"))
        state["selected"] = None
        state["offers"] = []
        state["quote_token"] = None
        for key in ("list_token", "calendar", "calendar_card", "date_options", "service_token", "service_card_id"):
            state.pop(key, None)
        return self._advance(state)

    def _room_list(self, state):
        fields, offers = state["fields"], state["offers"]
        lines = ["Выберите номер", stay_dates(fields['check_in'], fields['check_out']), party(fields), ""]
        buttons = []
        for i, offer in enumerate(offers, 1):
            lines.append(f"{offer['room_name']} — {money(offer['total'])} за {offer['nights']} ночей")
            buttons.append({"text": offer['room_name'], "data": f"/choose {state['list_token']}:{i}"})
        lines += ["", "Тариф: " + RATES[fields.get("rate", "flexible")],
                  "После выбора номера можно добавить завтрак и услуги.", DEMO_NOTE]
        buttons += [{"text": "Смотреть фото", "data": f"/room {state['list_token']}:1"},
                    {"text": "Сравнить номера", "data": "/compare " + state["list_token"]},
                    {"text": "Свободные даты", "data": "/available_dates"},
                    {"text": "Изменить даты", "data": "/edit dates"}]
        return response("\n".join(lines), buttons)

    def _browse_rooms(self, state, command, argument):
        generation, _, index = argument.partition(":")
        if state["selected"] or not state["offers"] or not generation or generation != state.get("list_token"):
            raise InputError("Этот список устарел. Продолжите текущую анкету или измените номер через «Изменить данные».")
        validate_fields(state["fields"], self.today())
        if self.pms.offers(state["fields"]) != state["offers"]:
            reply = self._advance(state)
            reply["text"] = "Наличие или цены изменились. Проверьте обновлённые варианты.\n\n" + reply["text"]
            return reply
        if command == "/rooms":
            return self._room_list(state)
        capacities = {row["id"]: row["capacity"] for row in self.store.db.execute("SELECT id,capacity FROM rooms")}
        if command == "/compare":
            buttons = [{"text": "Выбрать: " + offer["room_name"], "data": f"/choose {generation}:{i}"}
                       for i, offer in enumerate(state["offers"], 1)]
            buttons += [{"text": "Смотреть фото", "data": f"/room {generation}:1"},
                        {"text": "К списку номеров", "data": "/rooms " + generation}]
            return response(compare_rooms(state["offers"], state["fields"], capacities), buttons)
        if not index.isdecimal() or not 1 <= int(index) <= len(state["offers"]):
            raise InputError("Откройте фотографию кнопкой в списке номеров.")
        position = int(index)
        offer = state["offers"][position - 1]
        buttons = [{"text": "Выбрать номер", "data": f"/choose {generation}:{position}"}]
        if position > 1:
            buttons.append({"text": "Предыдущий", "data": f"/room {generation}:{position - 1}", "row": 1})
        if position < len(state["offers"]):
            buttons.append({"text": "Следующий", "data": f"/room {generation}:{position + 1}", "row": 1})
        buttons += [{"text": "Сравнить", "data": "/compare " + generation},
                    {"text": "К списку номеров", "data": "/rooms " + generation}]
        metadata = next((room for room in self.hotel["rooms"] if room["id"] == offer["room_id"]), {})
        reply = response(room_card(offer, state["fields"], capacities[offer["room_id"]], position, len(state["offers"]), metadata.get("highlights", "")), buttons)
        reply["card_key"] = "rooms:" + generation
        reply["photo"] = metadata.get("photo") or "missing.jpg"
        return reply

    def _preview(self, state, prefix=""):
        if not state["quote_token"]:
            state["quote_token"] = uuid.uuid4().hex[:24]
            state["quote_expires"] = (self.clock() + timedelta(minutes=10)).isoformat()
        q, f, g = state["selected"], state["fields"], state["guest"]
        token = state["quote_token"]
        text = prefix + "🧾 Проверьте бронь\n\n" + summary(q, f, g)
        return response(text, [{"text": "Подтвердить бронь", "data": "/confirm " + token},
                               {"text": "Изменить данные", "data": "/edit"},
                               {"text": "Изменить даты", "data": "/edit dates"},
                               {"text": "Условия и расчёт", "data": "/conditions " + token}])

    def _confirm(self, session, state, token):
        if state["booking_id"]:
            return self._booking(session, state["booking_id"])
        if not token or token != state["quote_token"] or not state["selected"] or not state["guest"]:
            raise InputError("Подтверждение устарело или не относится к этой карточке. Нажмите «Продолжить» и проверьте актуальные данные.")
        if "breakfast" not in state["fields"] or "extras" not in state["fields"]:
            state["quote_token"] = None
            return self._advance(state)
        validate_fields(state["fields"], self.today())
        current = next((o for o in self.pms.offers(state["fields"])
                        if o["room_id"] == state["selected"]["room_id"]), None)
        if current is None:
            state["selected"] = None
            state["quote_token"] = None
            result = self._advance(state)
            result["text"] = "Выбранный номер уже недоступен.\n" + result["text"]
            return result
        if current != state["selected"] or self.clock() >= datetime.fromisoformat(state["quote_expires"]):
            state["selected"] = current
            state["quote_token"] = None
            return self._preview(state, "Условия изменились или истёк срок предложения. Подтвердите заново.\n")
        booking_id = "DEMO-" + uuid.uuid4().hex[:12].upper()
        f = state["fields"]
        snapshot = {"offer": current, "fields": f, "guest": state["guest"],
                    "confirmed_at": self.clock().isoformat(), "payment_status": "NOT_REQUESTED"}
        # Test PMS shares this transaction: availability, booking and event commit together.
        self.store.db.execute("INSERT INTO bookings(id,intent,session_id,room_id,check_in,check_out,total,snapshot) VALUES (?,?,?,?,?,?,?,?)",
                              (booking_id, token, session, current["room_id"], f["check_in"], f["check_out"],
                               current["total"], json.dumps(snapshot, ensure_ascii=False)))
        state["booking_id"] = booking_id
        return self._booking_changed(session, booking_id)

    @staticmethod
    def _handoff(session, state, text):
        state["mode"] = "human"
        return response("Запрос сохранён для менеджера. Автоматические ответы остановлены; время ответа не обещаю.",
                        manager=f"Передача диалога {session}. Причина: {text[:1000]}. "
                                f"Параметры: {json.dumps(state['fields'], ensure_ascii=False)}. "
                                f"Бронь: {state['booking_id'] or 'нет'}")

    def resume(self, session, operation_key=None):
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            if operation_key and self.store.meta(operation_key, "") == "done":
                self.store.db.execute("COMMIT")
                return
            state = self.store.state(session)
            state["mode"] = "bot"
            state["revision"] += 1
            self.store.save(session, state)
            if operation_key:
                self.store.set_meta(operation_key, "done")
            self.store.db.execute("COMMIT")
        except BaseException:
            self.store.db.execute("ROLLBACK")
            raise
