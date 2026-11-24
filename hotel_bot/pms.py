"""Тестовая PMS: правила вымышленного отеля и атомарный учёт остатков."""

from datetime import date, timedelta


class InputError(ValueError):
    pass


def validate_fields(fields, today):
    for name in ("check_in", "check_out"):
        if name in fields:
            value = fields[name]
            if not isinstance(value, str):
                raise InputError("Даты нужны в формате ГГГГ-ММ-ДД.")
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                raise InputError("Проверьте дату: нужен формат ГГГГ-ММ-ДД.") from None
            if parsed.isoformat() != value or parsed < today:
                raise InputError("Дата должна быть в формате ГГГГ-ММ-ДД и не в прошлом.")
            if parsed > today + timedelta(days=365):
                raise InputError("Учебный поиск доступен на год вперёд.")
    if "check_in" in fields and "check_out" in fields:
        nights = (date.fromisoformat(fields["check_out"]) - date.fromisoformat(fields["check_in"])).days
        if not 1 <= nights <= 30:
            raise InputError("Выезд должен быть после заезда; длительность — от 1 до 30 ночей.")
    for name, low, high in [("adults", 1, 4), ("children_count", 0, 3)]:
        if name in fields and (type(fields[name]) is not int or not low <= fields[name] <= high):
            raise InputError("Для демо допустимы 1–4 взрослых и 0–3 ребёнка. Другие варианты — через менеджера.")
    ages = fields.get("children_ages", [])
    if not isinstance(ages, list) or len(ages) > 3 or any(type(a) is not int or not 0 <= a <= 17 for a in ages):
        raise InputError("Укажите возраст каждого ребёнка: целое число от 0 до 17 на дату заезда.")
    if "children_count" in fields and len(ages) > fields["children_count"]:
        raise InputError("Количество возрастов больше числа детей. Уточните состав гостей.")
    if "wishes" in fields and (not isinstance(fields["wishes"], str) or len(fields["wishes"]) > 500):
        raise InputError("Пожелания должны быть текстом до 500 символов.")
    if fields.get("rate", "flexible") not in {"flexible", "nonrefundable"}:
        raise InputError("Доступны гибкий и невозвратный тарифы.")


def missing_question(fields):
    for key, question in [
        ("check_in", "На какую дату планируете заезд? Формат: ДД.ММ.ГГГГ или ГГГГ-ММ-ДД."),
        ("check_out", "На какую дату планируете выезд? Формат: ДД.ММ.ГГГГ или ГГГГ-ММ-ДД."),
        ("adults", "Сколько будет взрослых?"),
        ("children_count", "Сколько будет детей? Если без детей — укажите 0."),
    ]:
        if key not in fields:
            return question
    if len(fields.get("children_ages", [])) != fields["children_count"]:
        return "Укажите возраст каждого ребёнка на дату заезда (полный список через запятую, например 7,10)."
    return None


class DemoPMS:
    def __init__(self, db, services):
        self.db = db
        self.services = services

    def available_dates(self, fields, start, nights, today, limit=3):
        """Ближайшие полные периоды, рассчитанные тем же способом, что и бронь."""
        result = []
        day = max(start, today)
        last = today + timedelta(days=365 - nights)
        while day <= last and len(result) < limit:
            candidate = {**fields, "check_in": day.isoformat(), "check_out": (day + timedelta(days=nights)).isoformat()}
            validate_fields(candidate, today)
            offers = self.offers(candidate)
            if offers:
                result.append({"check_in": candidate["check_in"], "check_out": candidate["check_out"], "offers": offers})
            day += timedelta(days=1)
        return result

    def offers(self, fields):
        result = []
        start, end = fields["check_in"], fields["check_out"]
        people = fields["adults"] + fields["children_count"]
        nights = (date.fromisoformat(end) - date.fromisoformat(start)).days
        for room in self.db.execute("SELECT * FROM rooms ORDER BY nightly,id"):
            occupied = self.db.execute(
                "SELECT COUNT(*) FROM bookings WHERE status='CONFIRMED' AND room_id=? AND check_in<? AND check_out>?",
                (room["id"], end, start)).fetchone()[0]
            if people > room["capacity"] or occupied >= room["stock"]:
                continue
            extra = max(0, fields["adults"] - 2) * 80000
            extra += sum(0 if age < 6 else 40000 if age < 12 else 80000
                         for age in fields.get("children_ages", []))
            total = (room["nightly"] + extra) * nights
            rate = fields.get("rate", "flexible")
            if rate == "nonrefundable":
                total = total * 90 // 100
            if nights >= 7:
                total = total * 90 // 100
            room_total = total
            breakfast = self.services["breakfast"]
            meal_total = (fields["adults"] * breakfast["adult"] + sum(
                0 if age < 6 else breakfast["child"] if age < 12 else breakfast["adult"]
                for age in fields.get("children_ages", []))) * nights if fields.get("breakfast") else 0
            extras = [{"id": key, **self.services["extras"][key]} for key in fields.get("extras", [])]
            total += meal_total + sum(item["price"] for item in extras)
            result.append({
                "room_id": room["id"], "room_name": room["name"], "nights": nights,
                "total": total, "currency": "RUB", "rate": rate,
                "pay_now": total if rate == "nonrefundable" else total * 30 // 100,
                "payment_terms": "Внести предоплату в течение 2 часов после создания брони.",
                "cancellation": "Неоплаченную учебную бронь можно отменить бесплатно в любое время. Оплату и возвраты проверяет менеджер.",
                "room_total": room_total, "meal_total": meal_total, "extras": extras,
                "meal": "Завтрак для всех гостей: " + money(meal_total) if fields.get("breakfast") else "Без завтрака",
                "fees": "Все выбранные услуги включены в итог.",
            })
        return result


def money(cents):
    return f"{cents // 100:,}".replace(",", " ") + f",{cents % 100:02d} ₽"
