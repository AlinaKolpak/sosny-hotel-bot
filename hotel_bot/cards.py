"""Гостевые тексты и оформление карточек. Цены считает PMS, не этот модуль."""

from datetime import date, timedelta

from .pms import money


MONTHS = ("", "января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря")
RATES = {"flexible": "Гибкий", "nonrefundable": "Невозвратный"}
DEMO_NOTE = "Учебная бронь, оплата не требуется"


def stay_dates(start, end):
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    if a.year != b.year:
        return f"{a.day} {MONTHS[a.month]} {a.year} — {b.day} {MONTHS[b.month]} {b.year}"
    days = f"{a.day}–{b.day} {MONTHS[b.month]}" if a.month == b.month else f"{a.day} {MONTHS[a.month]} — {b.day} {MONTHS[b.month]}"
    return f"{days} {b.year}"


def party(fields):
    text = f"Взрослых: {fields['adults']}"
    if fields.get("children_count"):
        ages = ", ".join(str(age) for age in fields.get("children_ages", []))
        text += f" · Детей: {fields['children_count']}"
        if ages:
            text += f" (возраст: {ages})"
    else:
        text += " · Без детей"
    return text


def services(offer, fields):
    names = ["Завтрак"] if fields.get("breakfast") else []
    names += [item["name"] for item in offer.get("extras", [])]
    return "\n".join("✓ " + name for name in names) or "Без завтрака и дополнительных услуг"


def cancellation(offer, fields):
    return "Неоплаченную учебную бронь можно отменить бесплатно в любое время."


def summary(offer, fields, guest):
    return (f"{offer['room_name']} · {stay_dates(fields['check_in'], fields['check_out'])}\n"
            f"{party(fields)}\n\n{services(offer, fields)}\n\n"
            f"Итого: {money(offer['total'])}\n{cancellation(offer, fields)}\n\n"
            f"Контакты: {guest['name']} · {guest['phone']}\n\n{DEMO_NOTE}")


def details(offer, fields):
    lines = ["Условия и расчёт", f"{offer['room_name']} · {stay_dates(fields['check_in'], fields['check_out'])}",
             f"Тариф: {RATES[offer['rate']]}. Ночей: {offer['nights']}", "",
             f"Проживание: {money(offer['room_total'])}", f"Завтрак: {money(offer['meal_total'])}"]
    lines += [f"{item['name']}: {money(item['price'])}" for item in offer.get("extras", [])]
    lines += [f"Итого: {money(offer['total'])}", offer["fees"], "", "Полные правила отмены:",
              cancellation(offer, fields), "Оплату, возвраты и неизвестный статус проверяет менеджер.", "",
              "Условия тарифа в учебном расчёте:",
              f"Предоплата: {money(offer['pay_now'])}; остаток: {money(offer['total'] - offer['pay_now'])}.",
              offer["payment_terms"], "Заезд с 14:00, выезд до 12:00 (Москва)."]
    if fields.get("wishes"):
        lines += ["Пожелания: " + fields["wishes"] + " (не гарантированы)."]
    return "\n".join(lines) + "\n\n" + DEMO_NOTE


def room_card(offer, fields, capacity, position, count, highlights=""):
    return (f"🛏 {offer['room_name']} · {position} из {count}\n"
            f"{stay_dates(fields['check_in'], fields['check_out'])}\n{party(fields)}\n"
            f"Вместимость: до {capacity} гостей, включая детей\n\n"
            + (highlights + "\n\n" if highlights else "") +
            f"За весь срок: {money(offer['total'])} · Ночей: {offer['nights']}\n"
            f"Тариф: {RATES[offer['rate']]}\n{services(offer, fields)}\n"
            f"{cancellation(offer, fields)}\n\n"
            "Пример интерьера — учебная иллюстрация, не фотография отеля «Сосны».\n" + DEMO_NOTE)


def compare_rooms(offers, fields, capacities):
    lines = ["Сравнить номера", stay_dates(fields['check_in'], fields['check_out']), party(fields),
             "Цены за весь срок для вашего состава гостей."]
    if len(offers) == 1:
        lines.append("На эти даты подходит один вариант.")
    for offer in offers:
        lines += ["", offer["room_name"], f"До {capacities[offer['room_id']]} гостей, включая детей",
                  f"Проживание: {money(offer['room_total'])}", f"Итого: {money(offer['total'])}",
                  services(offer, fields)]
    lines += ["", f"Тариф для всех вариантов: {RATES[offers[0]['rate']]}",
              cancellation(offers[0], fields),
              "Завтрак и услуги можно изменить после выбора номера.",
              "Площадь, кровати и оснащение пока не уточнены. Фото — примеры интерьера.", DEMO_NOTE]
    return "\n".join(lines)


def button_style(data):
    command, _, argument = data.partition(" ")
    if command == "/confirm":
        return "success"
    if command == "/cancel_confirm":
        return "danger"
    if command in {"/start", "/new", "/search", "/choose", "/preview"} or (command == "/service" and argument.endswith(":done")):
        return "primary"
    return None
