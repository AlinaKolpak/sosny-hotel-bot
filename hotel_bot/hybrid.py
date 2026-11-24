"""Local form first; optional AI only for text the form cannot interpret."""

import time
import re

from .form import FormInterpreter
from .http import ServiceError


class HybridInterpreter:
    def __init__(self, ai=None):
        self.ai = ai
        self.form = FormInterpreter()
        self.last_route = None
        self.last_metrics = {}

    def interpret(self, text, context, today):
        started = time.perf_counter()
        self.last_metrics = {"api_calls": 0, "input_tokens": 0, "output_tokens": 0}
        action = self.form.interpret(text, context, today)
        self.last_route = "form"
        if action["intent"] == "form_retry":
            self.last_route = "fallback"
            # Contacts and confirmations remain explicit form steps.
            if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2}", text.strip()):
                self.last_route = "form"
            elif text.casefold().strip() in {"да", "подтверждаю", "оплатил", "оплатила"} or "|" in text:
                action = {"intent": "clarify", "fields": {}}
                self.last_route = "local_guard"
            elif self.ai is not None:
                try:
                    action = self.ai.interpret(text, context, today)
                    self.last_route = "ai"
                except ServiceError:
                    action = {"intent": "form_retry", "fields": {}, "fallback": True}
                self.last_metrics = dict(self.ai.last_metrics)
            else:
                action = {"intent": "form_retry", "fields": {}, "fallback": True}
        self.last_metrics["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return action
