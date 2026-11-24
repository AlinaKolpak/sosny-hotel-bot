"""Локальное распознавание тем справки: без модели и сетевых запросов."""

import re


TOPICS = {
    "rooms": "Номера", "breakfast": "Питание", "parking": "Парковка",
    "rules": "Правила и отмена", "checkin": "Заезд и выезд",
    "pets": "Животные", "children": "Дети", "location": "Об отеле",
}
PATTERNS = {
    "rooms": r"\b(?:номер\w*|комнат\w*|люкс\w*|стандарт\w*|семейн\w*)\b",
    "breakfast": r"\b(?:завтрак\w*|питани\w*|поесть|ед[ауы]|обед\w*|ужин\w*|меню|ресторан\w*)\b",
    "parking": r"\b(?:парков\w*|стоян\w*|машин\w*|автомобил\w*)\b",
    "rules": r"\b(?:правил\w*|отмен\w*|штраф\w*|возврат\w*|оплат\w*|предоплат\w*|курени\w*|курить|документ\w*)\b",
    "checkin": r"\b(?:заезд\w*|выезд\w*|засел\w*|высел\w*)\b",
    "pets": r"\b(?:животн\w*|питом\w*|собак\w*|кошк\w*|кот\w*)\b",
    "children": r"\b(?:дет\w*|ребен\w*|ребенок|ребят\w*)\b",
    "location": r"\b(?:адрес\w*|находит\w*|располож\w*|добрать\w*)\b",
}


def question_topics(text):
    text = text.casefold().replace("ё", "е").strip()
    if not text or text.startswith("/") or "|" in text:
        return None
    # shortcut: узнаём темы и вопросительные обороты, сложный смешанный запрос
    # не извлекает параметры поездки; для этого позже понадобится отдельный разбор.
    question = "?" in text or re.search(
        r"\b(?:какие?|какая|какое|какую|сколько|когда|где|есть|можно|расскажите|подскажите|ли)\b", text)
    short_topic = text.rstrip(".! ") in {
        "номера", "питание", "завтрак", "парковка", "правила", "отмена", "заезд", "выезд",
        "животные", "дети", "адрес", "об отеле",
    }
    if not question and not short_topic:
        return None
    topics = [key for key, pattern in PATTERNS.items() if re.search(pattern, text)]
    if text.rstrip(".! ") == "об отеле":
        topics = ["location"]
    return topics  # [] означает вопрос, ответа на который в справке нет.
