"""Запуск: python -m hotel_bot demo | chat | telegram | doctor."""

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from contextlib import closing

from .config import Settings
from .engine import Engine
from .form import FormInterpreter
from .http import ServiceError
from .interpreter import AIInterpreter
from .hybrid import HybridInterpreter
from .storage import Store
from .telegram import Telegram


def show(reply):
    if reply["text"]:
        print("\nБот:", reply["text"])
    if reply["manager"]:
        print("\n[Локальное уведомление менеджеру]", reply["manager"])


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Учебный отель: реальные платежи и размещение отсутствуют")
    parser.add_argument("--db", help="Путь к отдельной SQLite базе")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("demo", help="Сценарий без ИИ и сети в отдельной временной базе")
    for channel in ("chat", "telegram"):
        sub = subs.add_parser(channel)
        mode = sub.add_mutually_exclusive_group()
        mode.add_argument("--ai", action="store_true", help="Явно включить ИИ (не нужен для первого этапа)")
        mode.add_argument("--offline", action="store_true", help="Пошаговая анкета без ИИ (режим по умолчанию)")
        mode.add_argument("--hybrid", action="store_true", help="Анкета + ИИ для свободного текста; при сбое — анкета")
        mode.add_argument("--hybrid-offline", action="store_true", help="Гибридный сценарий без создания клиента ИИ и вызовов API")
    subs.add_parser("doctor", help="Проверка настроек без раскрытия ключей и без сетевых запросов")
    subs.add_parser("manager-link", help="Одноразовая ссылка привязки менеджера (30 минут)")
    state = subs.add_parser("state", help="Локальное состояние (содержит тестовые контакты)")
    state.add_argument("session", nargs="?", default="console:local")
    resume = subs.add_parser("resume", help="Локальная команда владельца компьютера")
    resume.add_argument("session", nargs="?", default="console:local")
    price = subs.add_parser("price", help="Изменить тестовую базовую цену для проверки переоценки")
    price.add_argument("room", choices=["standard", "family", "suite"])
    price.add_argument("rubles", type=int)
    args = parser.parse_args()
    store = None
    try:
        cfg = Settings.from_env()
        if args.db:
            cfg.db_path = args.db
        if args.command == "doctor":
            print("Python:", sys.version.split()[0])
            print("База:", str(Path(cfg.db_path).resolve()))
            print("Основной режим: Telegram, пошаговая анкета БЕЗ ИИ")
            print("Необязательный ИИ (--ai):", cfg.provider, "| модель:", cfg.model)
            print("Ключ внешнего ИИ:", "настроен" if cfg.api_key else "не настроен")
            print("Telegram:", "токен настроен" if cfg.telegram_token else "токен не настроен")
            manager_configured = bool(cfg.manager_id)
            if not manager_configured and Path(cfg.db_path).is_file():
                with closing(sqlite3.connect(Path(cfg.db_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
                    bound = db.execute("SELECT value FROM meta WHERE key='telegram_bot_id'").fetchone()
                    if bound:
                        manager = db.execute("SELECT value FROM meta WHERE key=?", ("manager_id:" + bound[0],)).fetchone()
                        manager_configured = bool(manager and manager[0])
            print("Менеджер:", "настроен" if manager_configured else "не настроен")
            print("Доступ к API и Telegram этой командой не проверяется.")
            return 0
        hotel = json.loads(Path(__file__).with_name("hotel.json").read_text(encoding="utf-8"))
        use_ai = args.command in {"chat", "telegram"} and args.ai
        interpreter = AIInterpreter(cfg) if use_ai else FormInterpreter()
        hybrid = args.command in {"chat", "telegram"} and (args.hybrid or args.hybrid_offline)
        if hybrid:
            if args.hybrid:
                cfg.timeout = min(cfg.timeout, 15)
                interpreter = HybridInterpreter(AIInterpreter(cfg))
            else:
                interpreter = HybridInterpreter()
        store = Store(":memory:" if args.command == "demo" else cfg.db_path, hotel)
        engine = Engine(store, interpreter, hotel)
        if args.command == "demo":
            print("ДЕМОНСТРАЦИЯ БЕЗ ИИ. Все данные тестовые; сеть не используется.")
            start = engine.today() + timedelta(days=30)
            steps = ["/start", f"/stay {start} {start + timedelta(days=2)} 2 7", "/choose 1",
                     "/meal {services}:no", "/extras {services}:done",
                     "/guest Анна Тестовая | +79990000000"]
            for i, text in enumerate(steps):
                text = text.replace("{services}", store.state("demo").get("service_token", ""))
                print("\nГость:", text)
                show(engine.process("demo", f"demo:{i}", text))
            token = store.state("demo")["quote_token"]
            show(engine.process("demo", "demo:confirm", "/confirm " + token))
            show(engine.process("demo", "demo:confirm-again", "/confirm " + token))
            print("\nБроней в БД:", store.db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0])
        elif args.command == "chat":
            print("Локальный чат:", "гибрид" if hybrid else "ИИ через " + cfg.provider if use_ai else "БЕЗ ИИ, пошаговая анкета")
            print("Вводите только вымышленные данные. /help — помощь; /quit — выход.")
            if use_ai or (hybrid and args.hybrid):
                print("В режиме API текст сообщений отправляется настроенному провайдеру ИИ.")
            while True:
                text = input("\nВы: ").strip()
                if text == "/quit":
                    break
                if text:
                    show(engine.process("console:local", "cli:" + uuid.uuid4().hex, text))
        elif args.command == "telegram":
            print("Режим:", ("ГИБРИД с API" if args.hybrid else "ГИБРИД БЕЗ API") if hybrid else
                  "ИИ через " + cfg.provider if use_ai else "БЕЗ ИИ — ключ модели не используется")
            Telegram(cfg, engine).run()
        elif args.command == "manager-link":
            print(Telegram(cfg, engine).manager_link())
        elif args.command == "state":
            print(json.dumps(store.state(args.session), ensure_ascii=False, indent=2))
        elif args.command == "resume":
            engine.resume(args.session)
            print("Бот возвращён в диалог:", args.session)
        elif args.command == "price":
            if not 1 <= args.rubles <= 1_000_000:
                raise ValueError("Цена должна быть от 1 до 1000000 рублей")
            store.db.execute("UPDATE rooms SET nightly=? WHERE id=?", (args.rubles * 100, args.room))
            print("Изменена только тестовая цена; опубликованные предложения перепроверятся при подтверждении.")
        return 0
    except (ValueError, ServiceError) as exc:
        print("Ошибка:", str(exc), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nОстановлено. Сохранённые данные остаются в SQLite.")
        return 0
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
