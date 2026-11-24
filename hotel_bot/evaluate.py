"""Offline by default. --live is the only switch allowing model API calls."""

import argparse
import copy
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .engine import Engine
from .evaluation_cases import CASES
from .hybrid import HybridInterpreter
from .interpreter import AIInterpreter
from .storage import Store, new_state


def evaluate(*, ai=None):
    hotel = json.loads(Path(__file__).with_name("hotel.json").read_text(encoding="utf-8"))
    rows = []
    for index, case in enumerate(CASES, 1):
        store = Store(":memory:", hotel)
        try:
            hybrid = HybridInterpreter(ai)
            engine = Engine(store, hybrid, hotel, lambda: datetime(2027, 1, 1, 12, tzinfo=timezone.utc))
            state = new_state()
            state["fields"] = copy.deepcopy(case["initial"])
            store.save("evaluation", state)
            started = time.perf_counter()
            reply = engine.process("evaluation", str(index), case["text"])
            elapsed = round((time.perf_counter() - started) * 1000, 2)
            actual = store.state("evaluation")
            no_booking = store.db.execute("SELECT count(*) FROM bookings").fetchone()[0] == 0
            if ai is None and case["needs_ai"]:
                passed = (actual["fields"] == case["initial"] and actual["mode"] == "bot" and no_booking
                          and hybrid.last_route == "fallback" and "по анкете" in reply["text"])
                status = "FALLBACK_OK_AI_NOT_TESTED" if passed else "FAIL"
            else:
                passed = (actual["fields"] == case["expected_fields"] and actual["mode"] == case["expected_mode"]
                          and no_booking and (not case["contains"] or case["contains"].casefold() in reply["text"].casefold()))
                status = "PASS" if passed else "FAIL"
            rows.append({"case": index, "message": case["text"], "initial": case["initial"],
                         "expected_fields": case["expected_fields"], "actual_fields": actual["fields"],
                         "expected_mode": case["expected_mode"], "actual_mode": actual["mode"],
                         "reply": reply["text"], "status": status, "route": hybrid.last_route,
                         "local_total_ms": elapsed, "metrics": hybrid.last_metrics})
        finally:
            store.close()
    calls = sum(r["metrics"]["api_calls"] for r in rows)
    api_rows = [r for r in rows if r["metrics"]["api_calls"]]
    measured_usage = all(r["metrics"].get("input_tokens") is not None and
                         r["metrics"].get("output_tokens") is not None for r in api_rows)
    return {"mode": "live" if ai is not None else "offline_no_api", "cases": len(rows),
            "clock": "2027-01-01T12:00:00Z", "failed": sum(r["status"] == "FAIL" for r in rows),
            "ai_quality": "evaluated_on_synthetic_cases" if ai is not None else "NOT_EVALUATED",
            "api_calls": calls, "usage_complete": measured_usage,
            "input_tokens": sum(r["metrics"].get("input_tokens") or 0 for r in rows) if measured_usage else None,
            "output_tokens": sum(r["metrics"].get("output_tokens") or 0 for r in rows) if measured_usage else None,
            "cached_input_tokens": sum(r["metrics"].get("cached_input_tokens") or 0 for r in rows) if measured_usage else None,
            "local_median_ms": round(statistics.median(r["local_total_ms"] for r in rows), 2),
            "api_median_ms": round(statistics.median(r["metrics"]["elapsed_ms"] for r in api_rows), 2) if api_rows else None,
            "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly allow paid model calls on synthetic cases")
    parser.add_argument("--output", default="docs/evaluation-offline.json")
    for name in ("input", "output", "cached"):
        parser.add_argument("--" + name + "-price", type=float, help="Verified USD per million tokens (optional)")
    args = parser.parse_args()
    ai = None
    if args.live:
        cfg = Settings.from_env()
        cfg.timeout = min(cfg.timeout, 15)
        ai = AIInterpreter(cfg)
    report = evaluate(ai=ai)
    prices = {name: getattr(args, name + "_price") for name in ("input", "output", "cached")}
    report["usd_estimate"] = 0 if not report["api_calls"] else None
    if report["api_calls"] and report["usage_complete"] and all(v is not None and v >= 0 for v in prices.values()):
        cached = min(report["cached_input_tokens"], report["input_tokens"])
        report["usd_estimate"] = ((report["input_tokens"] - cached) * prices["input"] + cached * prices["cached"]
                                  + report["output_tokens"] * prices["output"]) / 1_000_000
    report["price_per_million_usd"] = prices
    report["model"] = ai.settings.model if ai is not None else None
    output = Path(args.output)
    if args.live and args.output == "docs/evaluation-offline.json":
        output = Path("docs/evaluation-live.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Проверка гибридного режима", "", f"Режим: `{report['mode']}`. Сообщений: {report['cases']}. Ошибок проверок: {report['failed']}.",
             "", f"Вызовов API: {report['api_calls']}. Качество ИИ: `{report['ai_quality']}`.",
             f"Медиана локального времени: {report['local_median_ms']} мс. Медиана API: {report['api_median_ms']}.",
             "", "В offline-режиме проверяется сохранение данных и возврат к анкете, а не понимание свободного текста ИИ.",
             "Стоимость live-прогона рассчитывается только при переданных тарифах; это оценка, не счёт провайдера.",
             "", "| № | Сообщение | Маршрут | Результат | мс |", "|---|---|---|---|---|"]
    for row in report["rows"]:
        lines.append(f"| {row['case']} | {row['message'].replace('|', '/')} | {row['route']} | {row['status']} | {row['local_total_ms']} |")
    output.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False, indent=2))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
