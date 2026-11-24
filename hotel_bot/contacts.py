"""Контакты учебного гостя: свободные разделители, без ИИ и проверки владельца номера."""

import re

from .pms import InputError


PHONE = re.compile(r"(?<!\w)\+?\(?[0-9][0-9\s().\-–—]*")


def parse_contacts(text):
    text = text.strip()
    if not text:
        raise InputError("Напишите имя или телефон. Можно отправить их отдельно.")
    matches = list(PHONE.finditer(text))
    if len(matches) > 1:
        raise InputError("Напишите один телефон, чтобы я не перепутал номера.")
    result = {}
    name = text
    if matches:
        match = matches[0]
        number = re.sub(r"[^0-9]", "", match.group())
        # Учебная версия принимает и короткие тестовые номера, не проверяя их существование.
        if not 1 <= len(number) <= 20:
            raise InputError("В телефоне должно быть не больше 20 цифр. Пробелы, скобки и дефисы допустимы.")
        result["phone"] = ("+" if match.group().startswith("+") else "") + number
        name = text[:match.start()] + " " + text[match.end():]
    name = " ".join(name.strip(" ,;|\n\t").split())
    if name:
        if len(name) > 100:
            raise InputError("Имя слишком длинное. Напишите до 100 символов — фамилия не обязательна.")
        if not any(char.isalpha() for char in name):
            raise InputError("Напишите имя словами, а телефон — цифрами. Можно двумя сообщениями.")
        result["name"] = name
    if not result:
        raise InputError("Напишите имя или телефон. Можно двумя сообщениями.")
    return result


def contact_prompt(draft):
    if draft.get("name"):
        return "Имя сохранено. Теперь напишите телефон — можно с 8 или +7, с пробелами, скобками и дефисами."
    if draft.get("phone"):
        return "Телефон сохранён. Как к вам обращаться? Напишите любое имя, фамилия не обязательна."
    return ("Как к вам обращаться и по какому телефону связаться?\n"
            "Напишите имя и телефон вместе или двумя сообщениями. Например:\n"
            "Алина, 8 (999) 123-45-67\nАлина 89991234567\n\n"
            "Можно сначала имя, потом телефон — или наоборот. Для учебной брони подойдут вымышленные имя и телефон.")
