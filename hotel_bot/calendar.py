"""Inline-календарь: даты проверяет тот же валидатор, что и ручной ввод."""

import calendar
import uuid
from datetime import date, timedelta

from .cards import MONTHS, stay_dates
from .pms import InputError, validate_fields

MONTH_NAMES = ("", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
               "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")


def bounds(fields, today):
    if "check_in" not in fields:
        return today, today + timedelta(days=364)
    start = date.fromisoformat(fields["check_in"])
    return max(today, start + timedelta(days=1)), min(today + timedelta(days=365), start + timedelta(days=30))


def render(state, today):
    fields = state["fields"]
    stage = "check_out" if "check_in" in fields else "check_in"
    low, high = bounds(fields, today)
    cal = state.get("calendar")
    if not cal or cal["stage"] != stage:
        cal = {"stage": stage, "token": uuid.uuid4().hex[:12], "month": low.replace(day=1).isoformat()}
        state["calendar"] = cal
    month = date.fromisoformat(cal["month"])
    month = max(low.replace(day=1), min(month, high.replace(day=1)))
    cal["month"] = month.isoformat()
    token = cal["token"]
    buttons = []
    def add(text, data, row):
        buttons.append({"text": text, "data": data, "row": row})
    for name in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"):
        add(name, "/noop", 0)
    weeks = calendar.Calendar().monthdatescalendar(month.year, month.month)
    for row, week in enumerate(weeks, 1):
        for day in week:
            available = day.month == month.month and low <= day <= high
            add(str(day.day) if available else "·", f"/day {token}:{day}" if available else "/noop", row)
    nav = len(weeks) + 1
    previous = (month - timedelta(days=1)).replace(day=1)
    following = (month + timedelta(days=32)).replace(day=1)
    if previous >= low.replace(day=1):
        add("‹ Месяц", f"/month {token}:{previous}", nav)
    if following <= high.replace(day=1):
        add("Месяц ›", f"/month {token}:{following}", nav)
    add("Ввести дату текстом", f"/date_text {token}", nav + 1)
    add("Свободные даты", "/available_dates", nav + 2)
    if stage == "check_out":
        add("Изменить даты", "/edit dates", nav + 3)
    title = "Выберите дату выезда" if stage == "check_out" else "Выберите дату заезда"
    text = f"{title}\n{MONTH_NAMES[month.month]} {month.year}"
    if stage == "check_out":
        start = date.fromisoformat(fields["check_in"])
        text += f"\nЗаезд: {start.day} {MONTHS[start.month]} {start.year}"
    text += "\nПроживание от 1 до 30 ночей. Недоступные даты отмечены точкой."
    return {"text": text, "buttons": buttons, "manager": None,
            "card_key": "calendar:" + state.setdefault("calendar_card", uuid.uuid4().hex[:12])}


def apply(state, today, command, argument):
    token, _, value = argument.partition(":")
    cal = state.get("calendar")
    stage = "check_out" if "check_in" in state["fields"] else "check_in"
    if not cal or cal["token"] != token or cal["stage"] != stage or "check_out" in state["fields"]:
        raise InputError("Этот календарь устарел. Продолжите бронирование или нажмите «Изменить даты».")
    if command == "/date_text":
        reply = render(state, today)
        reply["text"] = "Напишите дату в формате ДД.ММ.ГГГГ, например " + bounds(state["fields"], today)[0].strftime("%d.%m.%Y") + ".\n\n" + reply["text"]
        return reply
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise InputError("Выберите дату кнопкой календаря.") from None
    low, high = bounds(state["fields"], today)
    if command == "/month":
        if day.day != 1 or not low.replace(day=1) <= day <= high.replace(day=1):
            raise InputError("Этот месяц недоступен для выбранной поездки.")
        cal["month"] = day.isoformat()
        return render(state, today)
    if not low <= day <= high:
        raise InputError("Эта дата недоступна. Выезд должен быть позже заезда; поездка — до 30 ночей.")
    fields = {**state["fields"], stage: day.isoformat()}
    validate_fields(fields, today)
    state["fields"] = fields
    state["quote_token"] = None
    state.pop("calendar", None)
    if stage == "check_in":
        return render(state, today)
    key = "calendar:" + state.pop("calendar_card")
    return {"text": "Даты выбраны: " + stay_dates(fields["check_in"], fields["check_out"]) +
                    f"\nКоличество ночей: {(day - date.fromisoformat(fields['check_in'])).days}",
            "buttons": [{"text": "Продолжить", "data": "/start"}, {"text": "Изменить даты", "data": "/edit dates"}],
            "manager": None, "card_key": key}
