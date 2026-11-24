"""Один локальный polling-процесс; сообщения сохраняются до отправки."""

import json
import hashlib
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

from .http import request_json, ServiceError
from .engine import response


@contextmanager
def worker_lock(db_path):
    # shortcut: one polling process per database; add a shared worker lease before scaling.
    lock_path = Path(str(db_path) + ".worker.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            import msvcrt
        except ImportError:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("Другой Telegram-процесс уже работает с этой БД") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        else:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("Другой Telegram-процесс уже работает с этой БД") from None
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class Telegram:
    def __init__(self, settings, engine, transport=request_json):
        self.settings = settings
        self.engine = engine
        self.store = engine.store
        self.transport = transport
        self.bot_id = None

    @property
    def manager_id(self):
        return self.settings.manager_id or self.store.meta(f"manager_id:{self.bot_id}", "")

    def manager_link(self):
        me = self.call("getMe")
        self.bot_id = str(me["id"])
        if self.manager_id:
            raise ValueError("Менеджер уже настроен. Откройте /inbox в его Telegram.")
        token = secrets.token_urlsafe(24)
        self.store.set_meta(f"manager_pair:{self.bot_id}", json.dumps({
            "hash": hashlib.sha256(token.encode()).hexdigest(), "expires": time.time() + 1800}))
        return f"https://t.me/{me['username']}?start=manager_{token}"

    def _pair_manager(self, event_id, session, recipient, token):
        db = self.store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            pending = json.loads(self.store.meta(f"manager_pair:{self.bot_id}", "{}"))
            valid = (not self.manager_id and pending.get("expires", 0) > time.time()
                     and secrets.compare_digest(pending.get("hash", ""), hashlib.sha256(token.encode()).hexdigest()))
            if valid:
                self.store.set_meta(f"manager_id:{self.bot_id}", recipient)
                db.execute("DELETE FROM meta WHERE key=?", (f"manager_pair:{self.bot_id}",))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        if valid or recipient == self.manager_id:
            self._inbox(event_id, session, recipient)
        else:
            self.queue(event_id, session, recipient, response("Ссылка привязки недействительна или истекла."), manual=True)

    def _inbox(self, event_id, session, recipient):
        rows = self.store.db.execute("SELECT id,state FROM sessions WHERE id LIKE ?", (f"tg:{self.bot_id}:%",)).fetchall()
        waiting = [r for r in rows if json.loads(r["state"])["mode"] == "human"]
        buttons = [{"text": "Тестовый гость" if r["id"].endswith(":test") else "Открыть диалог " + r["id"].rsplit(":", 1)[-1], "data": "/dialog " + r["id"]}
                   for r in waiting[:30]]
        buttons.append({"text": "Тестовый гость", "data": "/guest_mode"})
        self.queue(event_id, session, recipient, response(
            f"Вы — менеджер. Обращений в ручном режиме: {len(waiting)}.\n"
            "Чтобы ответить гостю, используйте «Ответить» на его уведомление и напишите текст (до 2000 символов).\n"
            "«Вернуть бота» завершает ручной режим. /inbox — список обращений.\n"
            "«Тестовый гость» — отдельная анкета на вашем аккаунте. /manager_mode — вернуться к менеджеру.", buttons), manual=True)

    def _guest_mode(self, event_id, recipient, enabled):
        db = self.store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            key = "guest-mode-event:" + event_id
            if not self.store.meta(key, ""):
                self.store.set_meta(f"guest_mode:{self.bot_id}:{recipient}", "1" if enabled else "0")
                self.store.set_meta(key, "done")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def call(self, method, payload=None, **kwargs):
        result = self.transport("https://api.telegram.org/bot" + self.settings.telegram_token + "/" + method,
                                payload or {}, timeout=35, **kwargs)
        if not isinstance(result, dict) or not result.get("ok"):
            raise ServiceError("Telegram отклонил запрос", status=result.get("error_code") if isinstance(result, dict) else None,
                               description=str(result.get("description", "")) if isinstance(result, dict) else "")
        return result["result"]

    def queue(self, event_id, session, recipient, reply, *, manual=False):
        db = self.store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            if reply["text"]:
                buttons = list(reply["buttons"])
                test_guest = session.endswith(":test") and str(recipient) == self.manager_id
                if test_guest:
                    buttons.append({"text": "К менеджеру", "data": "/manager_mode"})
                payload = {"text": ("[Тестовый гость]\n" if test_guest else "") + reply["text"], "buttons": buttons,
                           "manual": manual or bool(reply["manager"]), "notify_delivery": reply.get("notify_delivery")}
                if reply.get("card_key") and not payload["manual"]:
                    payload["card_key"] = reply["card_key"]
                if reply.get("photo") and not payload["manual"]:
                    payload["photo"] = reply["photo"]
                db.execute("INSERT OR IGNORE INTO outbox(id,session_id,recipient,payload) VALUES (?,?,?,?)",
                           (event_id + ":guest", session, str(recipient), json.dumps(payload, ensure_ascii=False)))
            if reply["manager"]:
                if self.manager_id:
                    payload = {"text": ("[Обращение тестового гостя]\n" if session.endswith(":test") else "") + reply["manager"] + "\n\nИспользуйте «Ответить» на это сообщение, чтобы написать гостю.",
                               "buttons": [{"text": "Вернуть бота", "data": "/resume " + session}], "manual": True}
                    db.execute("INSERT OR IGNORE INTO outbox(id,session_id,recipient,payload) VALUES (?,?,?,?)",
                               (event_id + ":manager", session, self.manager_id,
                                json.dumps(payload, ensure_ascii=False)))
                else:
                    print("Передача сохранена; MANAGER_ID не настроен. Просмотр: команда state.")
            notice = reply.get("booking_notice")
            if notice and self.manager_id:
                # Booking notices do not transfer ownership or enable replies to guests.
                payload = {"text": ("[Тестовый гость]\n" if session.endswith(":test") else "") + notice["text"], "buttons": [], "manual": True}
                db.execute("INSERT OR IGNORE INTO outbox(id,session_id,recipient,payload) VALUES (?,?,?,?)",
                           (notice["id"], session, self.manager_id, json.dumps(payload, ensure_ascii=False)))
            db.execute("UPDATE events SET delivery='queued' WHERE id=?", (event_id,))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def flush(self):
        db = self.store.db
        for row in db.execute("SELECT * FROM outbox WHERE status='pending' AND session_id LIKE ? ORDER BY rowid",
                              (f"tg:{self.bot_id}:%",)).fetchall():
            payload = json.loads(row["payload"])
            if not payload["manual"] and self.store.state(row["session_id"])["mode"] != "bot":
                db.execute("UPDATE outbox SET status='suppressed' WHERE id=?", (row["id"],))
                continue
            db.execute("UPDATE outbox SET status='sending' WHERE id=?", (row["id"],))
            body = {"chat_id": row["recipient"], "text": payload["text"]}
            if payload["buttons"]:
                keyboard = []
                last_row = None
                for button in payload["buttons"]:
                    item = {"text": button["text"], "callback_data": button["data"],
                            **({"style": button["style"]} if button.get("style") else {})}
                    if "row" in button and keyboard and last_row == button["row"]:
                        keyboard[-1].append(item)
                    else:
                        keyboard.append([item])
                    last_row = button.get("row")
                body["reply_markup"] = {"inline_keyboard": keyboard}
            try:
                card_key = f"service_card:{row['session_id']}:{payload['card_key']}" if payload.get("card_key") else None
                target = self.store.meta(card_key, "") if card_key else ""
                kind = self.store.meta(card_key + ":kind", "text") if card_key else "text"
                if payload.get("photo"):
                    sent = self._send_room_photo(body, payload["photo"], target, kind)
                elif target:
                    try:
                        sent = self.call("editMessageText", {**body, "message_id": int(target)})
                    except ServiceError as exc:
                        description = exc.description.casefold()
                        if exc.status == 400 and "message is not modified" in description:
                            sent = {"message_id": int(target)}
                        elif exc.status == 400 and any(reason in description for reason in (
                                "message can't be edited", "message to edit not found", "message can not be edited")):
                            sent = self.call("sendMessage", body)
                        else:
                            raise
                else:
                    sent = self.call("sendMessage", body)
            except ServiceError:
                db.execute("UPDATE outbox SET status='unknown' WHERE id=?", (row["id"],))
                print("Доставка требует ручной проверки; автоматического повтора нет:", row["id"])
                delivery_text = "Доставка ответа гостю не подтверждена. Проверьте связь; автоматически повторять сообщение не буду."
            else:
                db.execute("BEGIN IMMEDIATE")
                try:
                    db.execute("UPDATE outbox SET status='sent',message_id=? WHERE id=?", (sent.get("message_id"), row["id"]))
                    if card_key and sent.get("message_id"):
                        self.store.set_meta(card_key, sent["message_id"])
                        self.store.set_meta(card_key + ":kind", sent.get("_card_kind", "text"))
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                delivery_text = "Ответ гостю отправлен."
            if payload.get("notify_delivery"):
                self.queue(row["id"] + ":receipt", row["session_id"], payload["notify_delivery"],
                           response(delivery_text, [{"text": "Вернуть бота", "data": "/resume " + row["session_id"]}]), manual=True)

    def _text_card(self, body, target=""):
        if target:
            try:
                return self.call("editMessageText", {**body, "message_id": int(target)})
            except ServiceError as exc:
                if exc.status == 400 and "message is not modified" in exc.description.casefold():
                    return {"message_id": int(target)}
                if not self._missing_card(exc):
                    raise
        return self.call("sendMessage", body)

    @staticmethod
    def _missing_card(exc):
        return exc.status == 400 and any(reason in exc.description.casefold() for reason in (
            "message can't be edited", "message to edit not found", "message can not be edited"))

    def _send_room_photo(self, body, filename, target="", kind="text"):
        def fallback():
            # A photo cannot become plain text in place; the replacement becomes the new card.
            return self._text_card({**body, "text": "Фото сейчас недоступно.\n\n" + body["text"]},
                                   target if kind == "text" else "")
        root = Path(__file__).with_name("assets") / "rooms"
        path = (root / filename).resolve()
        try:
            # Only packaged room JPEGs, never a path supplied by a callback or arbitrary local files.
            if path.parent != root.resolve() or path.suffix.lower() not in {".jpg", ".jpeg"}:
                raise ValueError("Invalid photo path")
            with path.open("rb") as source:
                data = source.read(10_000_001)
            if not data.startswith(b"\xff\xd8") or len(data) > 10_000_000:
                raise ValueError("Invalid JPEG")
            if len(body["text"].encode("utf-16-le")) // 2 > 1024:
                raise ValueError("Caption too long")
        except (OSError, ValueError):
            return fallback()
        photo_body = {key: value for key, value in body.items() if key != "text"}
        photo_body["caption"] = body["text"]
        try:
            if target:
                edit_body = {key: value for key, value in photo_body.items() if key != "caption"}
                edit_body.update(message_id=int(target), media={"type": "photo", "media": "attach://photo", "caption": body["text"]})
                try:
                    sent = self.call("editMessageMedia", edit_body, photo_bytes=data)
                except ServiceError as exc:
                    if exc.status == 400 and "message is not modified" in exc.description.casefold():
                        sent = {"message_id": int(target)}
                    elif self._missing_card(exc):
                        sent = self.call("sendPhoto", photo_body, photo_bytes=data)
                    else:
                        raise
            else:
                sent = self.call("sendPhoto", photo_body, photo_bytes=data)
            return {**sent, "_card_kind": "photo"}
        except ServiceError as exc:
            if exc.status == 400 and any(reason in exc.description.casefold() for reason in (
                    "photo_invalid", "image_process_failed", "photo must be", "photo should be")):
                return fallback()
            raise

    def handle(self, update):
        event_id = f"tg:{self.bot_id}:{update['update_id']}"
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        sender = callback.get("from", {}) if callback else message.get("from", {})
        if message.get("chat", {}).get("type") != "private" or sender.get("is_bot"):
            return
        recipient = str(message["chat"]["id"])
        if str(sender.get("id")) != recipient:
            return
        text = callback.get("data", "") if callback else message.get("text", "")
        if not text:
            text = "/unsupported"
        if callback:
            try:
                self.call("answerCallbackQuery", {"callback_query_id": callback["id"]})
            except ServiceError:
                pass
        session = f"tg:{self.bot_id}:{recipient}"
        if text.startswith("/start manager_"):
            self._pair_manager(event_id, session, recipient, text[len("/start manager_"):])
            return
        if recipient == self.manager_id:
            if text in {"/guest_mode", "/manager_mode", "/inbox"}:
                self._guest_mode(event_id, recipient, text == "/guest_mode")
                if text != "/guest_mode":
                    self._inbox(event_id, session, recipient)
                    return
                session += ":test"
                reply = self.engine.process(session, event_id, "/start")
                self.queue(event_id, session, recipient, reply)
                return
            if not callback and message.get("reply_to_message"):
                replied = message["reply_to_message"]
                target = self.store.db.execute(
                    "SELECT session_id FROM outbox WHERE recipient=? AND message_id=? AND id LIKE '%:manager' AND status='sent'",
                    (recipient, replied.get("message_id"))).fetchone()
                if target and str(replied.get("from", {}).get("id")) == self.bot_id and message.get("text"):
                    self._manager(event_id, session, recipient, "/reply " + target["session_id"] + " " + text)
                else:
                    self.queue(event_id, session, recipient, response("Ответьте текстом на уведомление гостя. /inbox — обращения."), manual=True)
                return
            elif text.startswith(("/resume ", "/reply ", "/dialog ")):
                self._manager(event_id, session, recipient, text)
                return
            elif self.store.meta(f"guest_mode:{self.bot_id}:{recipient}", "0") != "1":
                self._inbox(event_id, session, recipient)
                return
            session += ":test"
        reply = self.engine.process(session, event_id, text)
        self.queue(event_id, session, recipient, reply)

    def _manager(self, event_id, manager_session, recipient, text):
        command, target, *rest = text.split(" ", 2)
        prefix = f"tg:{self.bot_id}:"
        test_target = target == f"{prefix}{recipient}:test"
        if not test_target and (not target.startswith(prefix) or not target[len(prefix):].isdecimal()):
            self.queue(event_id, manager_session, recipient, response("Нужен ID диалога из уведомления."), manual=True)
            return
        exists = self.store.db.execute("SELECT 1 FROM sessions WHERE id=?", (target,)).fetchone()
        if not exists:
            self.queue(event_id, manager_session, recipient, response("Диалог не найден."), manual=True)
        elif command == "/dialog":
            state = self.store.state(target)
            if state["mode"] != "human":
                self.queue(event_id, manager_session, recipient, response("Этот диалог уже ведёт бот."), manual=True)
            else:
                last = self.store.db.execute("SELECT reply FROM events WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (target,)).fetchone()
                detail = json.loads(last[0]).get("manager") if last else None
                self.queue(event_id, target, recipient, response(manager=detail or f"Обращение {target}. Бронь: {state['booking_id'] or 'нет'}"), manual=True)
        elif command == "/resume":
            self.engine.resume(target, event_id)
            self.queue(event_id, manager_session, recipient, response("Бот возвращён в диалог."), manual=True)
        elif not rest or not rest[0].strip() or len(rest[0]) > 2000 or self.store.state(target)["mode"] != "human":
            self.queue(event_id, manager_session, recipient,
                       response("Ответ до 2000 символов доступен только после передачи диалога."), manual=True)
        else:
            reply = response("Менеджер: " + rest[0])
            reply["notify_delivery"] = recipient
            guest_recipient = recipient if test_target else target[len(prefix):]
            self.queue(event_id, target, guest_recipient, reply, manual=True)

    def run(self):
        if not self.settings.telegram_token:
            raise ValueError("Заполните TELEGRAM_BOT_TOKEN в локальном .env")
        with worker_lock(self.settings.db_path):
            me = self.call("getMe")
            self.bot_id = str(me["id"])
            bound_bot = self.store.meta("telegram_bot_id", "")
            if bound_bot and bound_bot != self.bot_id:
                raise ValueError("Эта БД связана с другим Telegram-ботом. Укажите отдельный DB_PATH.")
            self.store.set_meta("telegram_bot_id", self.bot_id)
            if self.call("getWebhookInfo").get("url"):
                raise ValueError("У этого бота уже есть webhook. Используйте отдельного учебного бота.")
            self.store.db.execute("UPDATE outbox SET status='unknown' WHERE status='sending'")
            print(f"Telegram @{me['username']} запущен локально. Остановка: Ctrl+C.")
            self.flush()
            offset_key = "telegram_offset:" + self.bot_id
            while True:
                try:
                    updates = self.call("getUpdates", {"offset": int(self.store.meta(offset_key)),
                                                        "timeout": 25,
                                                        "allowed_updates": ["message", "callback_query"]})
                    for update in updates:
                        self.handle(update)
                        self.store.set_meta(offset_key, update["update_id"] + 1)
                    self.flush()
                except ServiceError:
                    print("Telegram временно недоступен. Повтор чтения через 5 секунд.")
                    time.sleep(5)
