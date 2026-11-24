"""Пошаговая анкета без модели и без сетевых запросов."""

import re
from datetime import datetime

from .knowledge import question_topics


class FormInterpreter:
    def interpret(self, text, context, today):
        text = text.strip()
        fields = context["fields"]
        if text.casefold() in {"менеджер", "оператор"}:
            return {"intent": "handoff", "fields": {}}
        topics = question_topics(text)
        if topics is not None:
            return {"intent": "faq", "fields": {}, "faq_topics": topics}
        changes = {}
        for key in ("check_in", "check_out"):
            if key not in fields:
                for pattern, fmt in [(r"\d{2}\.\d{2}\.\d{4}", "%d.%m.%Y"),
                                     (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d")]:
                    if re.fullmatch(pattern, text):
                        try:
                            changes[key] = datetime.strptime(text, fmt).date().isoformat()
                        except ValueError:
                            pass
                return {"intent": "update" if changes else "form_retry", "fields": changes}
        for key in ("adults", "children_count"):
            if key not in fields:
                if re.fullmatch(r"\d{1,2}", text):
                    changes[key] = int(text)
                return {"intent": "update" if changes else "form_retry", "fields": changes}
        if len(fields.get("children_ages", [])) != fields["children_count"]:
            if re.fullmatch(r"\d{1,2}(?:\s*[, ]\s*\d{1,2}){0,2}", text):
                changes["children_ages"] = [int(n) for n in re.split(r"[ ,]+", text)]
            return {"intent": "update" if changes else "form_retry", "fields": changes}
        if re.fullmatch(r"\d{1,2}", text) and context["offers"]:
            return {"intent": "select", "fields": {}, "offer_number": int(text)}
        return {"intent": "form_retry", "fields": {}}
