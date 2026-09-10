"""
Raspika bot — единый бот новой системы (для bothost.ru).
Аккаунт = Telegram: профиль (вуз+группа) хранится в ядре raspika.com и един везде.

Умеет:
  /start link_<code>  — привязка профиля с сайта (кнопка «Привязать Telegram»)
  /start sub_<...>    — старый формат подписки (совместимость)
  «Сегодня» / «Завтра» / «Неделя» — расписание группы из профиля прямо в чате
  «Профиль» — показать вуз/группу, вкл/выкл уведомления
  /stop — выключить уведомления

Переменные окружения:
  BOT_TOKEN — токен бота (@raspiskaOFF_bot)
  CORE_URL  — https://raspika.com
"""
import base64
import datetime
import os
import time

import httpx

# Токен: bothost кладёт его в системные переменные под разными именами
TOKEN = (os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
         or os.environ.get("API_TOKEN") or os.environ.get("TOKEN") or "")
CORE = os.environ.get("CORE_URL", "https://raspika.com").rstrip("/")
# Мост с Claude: сообщения этого chat_id (кроме команд/кнопок) уходят в рабочую сессию
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()
# Ключ ботов: bothost может не пробрасывать имя BOT_API_KEY, поэтому
# дополнительно ищем значение с префиксом bk_ в любой переменной
# и срезаем случайно вставленное "BOT_API_KEY=" из самого значения.
def _find_bot_key():
    v = (os.environ.get("BOT_API_KEY") or "").strip().strip('"').strip("'")
    if v.startswith("BOT_API_KEY="):
        v = v.split("=", 1)[1].strip()
    if v.startswith("bk_"):
        return v
    for val in os.environ.values():
        s = (val or "").strip().strip('"').strip("'")
        if s.startswith("BOT_API_KEY="):
            s = s.split("=", 1)[1].strip()
        if s.startswith("bk_"):
            return s
    return v


BOT_KEY = _find_bot_key()


def _fetch_bot_key():
    """bothost не пробрасывает свои переменные до контейнера, поэтому ключ
    выдает ядро в обмен на токен бота (его знают только ядро и хостинг)."""
    global BOT_KEY
    if BOT_KEY.startswith("bk_"):
        return True
    try:
        r = httpx.post(f"{CORE}/api/bot/key", json={"token": TOKEN}, timeout=15)
        if r.status_code == 200:
            k = (r.json().get("key") or "").strip()
            if k.startswith("bk_"):
                BOT_KEY = k
                return True
    except Exception as e:
        print("key fetch err:", e)
    return False
API = f"https://api.telegram.org/bot{TOKEN}"
SITE = "raspika.com"

KB = {"keyboard": [[{"text": "Сегодня"}, {"text": "Завтра"}],
                   [{"text": "Неделя"}, {"text": "Профиль"}],
                   [{"text": "Поддержка"}]],
      "resize_keyboard": True}

# кто сейчас пишет обращение в поддержку: chat_id -> True
_support_wait = {}

# онбординг новичка: chat_id -> {"inst": key, "cands": [группы-кандидаты]}
_pick = {}

# слова-кнопки главного меню: в режиме поддержки они выводят из него, а не уходят тикетом
MENU_WORDS = {"сегодня", "завтра", "неделя", "профиль", "поддержка"}

RU_DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
RU_MON = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]


# ---------------- helpers ----------------

def send(chat, text, kb=KB):
    try:
        httpx.post(f"{API}/sendMessage",
                   json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                         "disable_web_page_preview": True, "reply_markup": kb},
                   timeout=15)
    except Exception as e:
        print("send err:", e)


def core_get(path, **params):
    if not BOT_KEY.startswith("bk_"):
        _fetch_bot_key()
    try:
        r = httpx.get(f"{CORE}{path}", params=params, timeout=20,
                      headers={"X-Bot-Key": BOT_KEY})
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print("core err:", e)
        return None


def core_post(path, payload):
    if not BOT_KEY.startswith("bk_"):
        _fetch_bot_key()
    try:
        r = httpx.post(f"{CORE}{path}", json=payload, timeout=20,
                       headers={"X-Bot-Key": BOT_KEY})
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print("core err:", e)
        return None


def get_profile(chat):
    d = core_get("/api/profile", chat_id=str(chat))
    return d.get("data") if d else None


def _extract_file(msg):
    """(file_id, имя) из фото или документа; ("big", None) если больше 8 МБ."""
    if msg.get("photo"):
        p = msg["photo"][-1]                      # самое большое превью
        if (p.get("file_size") or 0) > 8 * 1024 * 1024:
            return "big", None
        return p["file_id"], "photo.jpg"
    doc = msg.get("document")
    if doc:
        if (doc.get("file_size") or 0) > 8 * 1024 * 1024:
            return "big", None
        return doc["file_id"], doc.get("file_name") or "file.bin"
    return None, None


def _support_file(chat, file_id, fname, caption, contact):
    """Скачать файл у Telegram и передать в поддержку ядра."""
    try:
        r = httpx.get(f"{API}/getFile", params={"file_id": file_id}, timeout=20)
        path = r.json()["result"]["file_path"]
        data = httpx.get(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=60).content
        up = httpx.post(f"{CORE}/api/support/upload",
                        params={"uid": f"tg:{chat}", "channel": "tg",
                                "contact": contact, "caption": caption or ""},
                        files={"file": (fname, data)},
                        headers={"X-Bot-Key": BOT_KEY}, timeout=60)
        return up.status_code == 200
    except Exception as e:
        print("support file err:", e)
        return False


def start_onboarding(chat):
    """Первый вход: выбор вуза инлайн-кнопками, потом поиск группы по названию."""
    insts = (core_get("/api/institutions") or {}).get("data") or []
    live = [i for i in insts if i.get("live", True) and i.get("key")]
    if not live:
        send(chat, f"Привет! Это <b>Raspika</b>. Открой {SITE}, выбери вуз и группу.")
        return
    rows, row = [], []
    for i in live:
        row.append({"text": i.get("short") or i.get("name"), "callback_data": f"i:{i['key']}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    send(chat, "Привет! Это <b>Raspika</b>, расписание вузов Омска.\n\n"
               "Выбери свой вуз (один раз, дальше запомню):",
         kb={"inline_keyboard": rows})


def handle_callback(cb):
    chat = cb["message"]["chat"]["id"]
    data = cb.get("data") or ""
    try:  # убрать «часики» на кнопке
        httpx.post(f"{API}/answerCallbackQuery",
                   json={"callback_query_id": cb["id"]}, timeout=10)
    except Exception:
        pass
    if data.startswith("d:"):        # листание дней под расписанием
        show_schedule(chat, "date:" + data[2:])
        return
    if data == "w:":
        show_schedule(chat, "week")
        return
    if data.startswith("s:"):        # цикл подгруппы: все -> 1 -> 2 -> все
        prof = get_profile(chat)
        if prof:
            cur = prof.get("subgroup") or 0
            new = (cur + 1) % 3
            core_post("/api/profile", {"chat_id": str(chat), "subgroup": new})
            send(chat, f"Подгруппа: <b>{new or 'все'}</b>")
            show_schedule(chat, "date:" + data[2:])
        return
    if data.startswith("i:"):
        _pick[chat] = {"inst": data[2:]}
        send(chat, "Теперь напиши название своей группы, например <b>ИСТ-253</b> "
                   "или её часть:")
    elif data.startswith("g:"):
        st = _pick.get(chat) or {}
        cands = st.get("cands") or []
        try:
            g = cands[int(data[2:])]
        except (ValueError, IndexError):
            send(chat, "Не нашёл эту группу, напиши название ещё раз.")
            return
        _pick.pop(chat, None)
        r = core_post("/api/subscribe", {"chat_id": str(chat), "inst": st["inst"],
                                         "group": str(g["id"])})
        if not r:
            send(chat, "Не получилось сохранить, попробуй позже.")
            return
        core_post("/api/profile", {"chat_id": str(chat), "group_name": g.get("name") or ""})
        send(chat, f"Готово! Твоя группа: <b>{g.get('name')}</b> 🔔\n"
                   f"Запомнил. Кнопки ниже: расписание прямо здесь.\n"
                   f"Уведомления об изменениях уже включены.")
        show_schedule(chat, "today")


def handle_pick_text(chat, text):
    """Юзер в онбординге написал название группы: ищем и предлагаем кнопками."""
    st = _pick.get(chat)
    q = text.lower().replace(" ", "")
    groups = (core_get("/api/groups", inst=st["inst"]) or {}).get("data") or []
    found = [g for g in groups if q in str(g.get("name", "")).lower().replace(" ", "")]
    if not found:
        send(chat, "Не нашёл такую группу. Проверь название и напиши ещё раз "
                   "(можно только часть, например 251).")
        return
    found = found[:8]
    st["cands"] = found
    rows = [[{"text": g["name"][:60], "callback_data": f"g:{i}"}] for i, g in enumerate(found)]
    send(chat, "Нашёл! Выбери свою группу:" if len(found) > 1 else "Твоя группа:",
         kb={"inline_keyboard": rows})


def _decode_sub(arg):
    raw = arg[4:]
    raw += "=" * (-len(raw) % 4)
    try:
        s = base64.urlsafe_b64decode(raw).decode("utf-8")
        inst, group = s.split(":", 1)
        return inst, group
    except Exception:
        return None


# ---------------- расписание ----------------

def fmt_day(lessons, date, hw=None):
    d = datetime.date.fromisoformat(date)
    head = f"📅 <b>{RU_DAYS[d.weekday()]}, {d.day} {RU_MON[d.month]}</b>"
    day = [l for l in lessons if l["date"] == date]
    if not day:
        return head + "\n\nПар нет 🌤"
    hw = hw or []

    def hw_lines(subj):
        s = (subj or "").lower().strip()
        return [h for h in hw
                if h["date"] == date and (h.get("subject") or "").lower().strip() == s]

    out = [head]
    for l in sorted(day, key=lambda x: (x.get("num") or 0, x.get("start") or "")):
        out.append("")                                   # пустая строка между парами
        pair = f"{l['num']} пара" if l.get("num") else "Пара"
        time_ = f"{l.get('start','')}-{l.get('end','')}".strip("-")
        out.append(f"🔹 <b>{pair}</b> · {time_}")
        subj = l.get("subject") or "Занятие"
        tags = []
        if l.get("type"):
            tags.append(l["type"])
        if l.get("subgroup"):
            tags.append(f"п/г {l['subgroup']}")
        out.append(subj + (f" · <i>{', '.join(tags)}</i>" if tags else ""))
        info = []
        if l.get("room"):
            info.append(f"📍 {l['room']}")
        if l.get("teacher"):
            info.append(l["teacher"])
        if info:
            out.append(" · ".join(info))
        for h in hw_lines(l.get("subject")):
            out.append(f"📚 ДЗ: {h['text']}")
            for f in h.get("files") or []:
                out.append(f"📎 https://raspika.com{f['url']}")
    return "\n".join(out)


def show_schedule(chat, mode):
    prof = get_profile(chat)
    if not prof or not prof.get("inst"):
        send(chat, f"Сначала выбери группу: открой {SITE}, выбери вуз и группу и нажми "
                   f"«Привязать Telegram».")
        return
    sched = core_get("/api/schedule", inst=prof["inst"], group=prof["group"])
    if not sched:
        send(chat, "Не получилось получить расписание, попробуй позже.")
        return
    lessons = sched.get("data", [])
    sg = prof.get("subgroup") or 0
    if sg:  # своя подгруппа: чужие пары скрываем, общие оставляем
        lessons = [l for l in lessons if not l.get("subgroup") or l.get("subgroup") == sg]
    stale = " \n\n⚠️ Сайт вуза недоступен, показана последняя копия." if sched.get("stale") else ""
    hw = (core_get("/api/homework", inst=prof["inst"], group=prof["group"]) or {}).get("data", [])
    today = datetime.date.today()
    gname = prof.get("group_name") or prof.get("group")
    def day_nav(d):
        """Инлайн-кнопки листания под расписанием дня."""
        prev = (d - datetime.timedelta(days=1)).isoformat()
        nxt = (d + datetime.timedelta(days=1)).isoformat()
        sg_txt = f"Подгруппа: {sg or 'все'}"
        return {"inline_keyboard": [
            [{"text": "◀", "callback_data": f"d:{prev}"},
             {"text": "Сегодня", "callback_data": f"d:{today.isoformat()}"},
             {"text": "▶", "callback_data": f"d:{nxt}"}],
            [{"text": "Неделя", "callback_data": "w:"},
             {"text": sg_txt, "callback_data": f"s:{d.isoformat()}"}],
        ]}

    if isinstance(mode, str) and mode.startswith("date:"):
        d = datetime.date.fromisoformat(mode[5:])
        send(chat, f"<b>{gname}</b>\n" + fmt_day(lessons, d.isoformat(), hw) + stale,
             kb=day_nav(d))
    elif mode == "today":
        send(chat, f"<b>{gname}</b>\n" + fmt_day(lessons, today.isoformat(), hw) + stale,
             kb=day_nav(today))
    elif mode == "tomorrow":
        d = today + datetime.timedelta(days=1)
        send(chat, f"<b>{gname}</b>\n" + fmt_day(lessons, d.isoformat(), hw) + stale,
             kb=day_nav(d))
    else:  # week: каждый день отдельным сообщением, чтобы не было каши
        monday = today - datetime.timedelta(days=today.weekday())
        sent = 0
        for i in range(6):
            d = monday + datetime.timedelta(days=i)
            if [l for l in lessons if l["date"] == d.isoformat()]:
                send(chat, fmt_day(lessons, d.isoformat(), hw))
                sent += 1
                time.sleep(0.3)
        if not sent:
            send(chat, f"<b>{gname}</b>\nНа этой неделе пар нет 🌤" + stale)
        elif stale:
            send(chat, stale.strip())


def show_profile(chat):
    prof = get_profile(chat)
    if not prof:
        send(chat, f"Профиль не найден. Открой {SITE} и нажми «Привязать Telegram».")
        return
    notify = "включены 🔔" if prof.get("notify") else "выключены 🔕"
    sync = ""
    if prof.get("token"):
        sync = (f"\n\nОткрыть свой профиль на компе или в другом браузере:\n"
                f"https://{SITE}/?t={prof['token']}\n"
                f"(ссылка личная, не пересылай её)")
    send(chat, f"👤 <b>Профиль</b>\n"
               f"Группа: <b>{prof.get('group_name') or prof.get('group')}</b>\n"
               f"Уведомления об изменениях: {notify}\n\n"
               f"Сменить группу: на сайте {SITE} (изменится и здесь).\n"
               f"Уведомления: /stop выключить, /notify включить." + sync)


# ---------------- handlers ----------------

def handle(msg):
    chat = msg["chat"]["id"]
    text = (msg.get("text") or msg.get("caption") or "").strip()

    # Ответ владельца на тикет: реплай на сообщение «💬 #xxxx ...»
    rt = msg.get("reply_to_message")
    if rt and text:
        import re as _re
        m = _re.search(r"#([0-9a-f]{4})", rt.get("text") or "")
        if m:
            r = core_post("/api/support/reply",
                          {"ticket": m.group(1), "text": text, "admin_chat_id": str(chat)})
            if r and r.get("ok"):
                send(chat, f"✅ Ответ доставлен ({r.get('delivered_to')})")
            else:
                send(chat, "Не получилось доставить ответ (тикет не найден или нет прав).")
            return

    # Пользователь пишет обращение в поддержку
    if chat in _support_wait:
        low = text.lower()
        if low == "отмена":
            _support_wait.pop(chat, None)
            send(chat, "Ок, отменил. Кнопки ниже.")
            return
        if text.startswith("/") or low in MENU_WORDS:
            _support_wait.pop(chat, None)   # кнопка меню или команда: выходим из поддержки
        else:
            _support_wait.pop(chat, None)
            contact = "@" + (msg.get("from", {}).get("username") or str(chat))
            fid, fname = _extract_file(msg)
            if fid == "big":
                send(chat, "Файл больше 8 МБ, пришли поменьше.")
            elif fid:
                ok = _support_file(chat, fid, fname, text, contact)
                send(chat, "Отправлено! Ответ придёт сюда же." if ok
                     else "Не получилось отправить файл, попробуй ещё раз.")
            elif text:
                r = core_post("/api/support/send",
                              {"uid": f"tg:{chat}", "channel": "tg", "text": text,
                               "contact": contact})
                send(chat, "Отправлено! Ответ придёт сюда же." if r else "Ошибка, попробуй позже.")
            else:
                send(chat, "Пришли текст, фото или файл одним сообщением. Отменить: любая кнопка.")
            return

    # онбординг: ждём название группы
    if chat in _pick:
        if text.startswith("/") or text.lower() in MENU_WORDS:
            _pick.pop(chat, None)      # вышел кнопкой или командой
        elif text:
            handle_pick_text(chat, text)
            return

    if text.lower() == "поддержка":
        _support_wait[chat] = True
        send(chat, "Напиши свою проблему или идею одним сообщением. Можно приложить фото или файл.\n"
                   "Передумал: нажми любую кнопку или напиши Отмена.")
        return

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        arg = parts[1] if len(parts) > 1 else ""
        if arg.startswith("link_"):
            code = arg[5:]
            r = core_post("/api/link/complete", {"code": code, "chat_id": str(chat)})
            if r and r.get("ok"):
                p = r["profile"]
                send(chat, f"🔗 Готово! Профиль привязан.\n"
                           f"Группа: <b>{p.get('group_name') or p.get('group')}</b>\n"
                           f"Уведомления об изменениях включены 🔔\n\n"
                           f"Кнопки ниже — расписание прямо здесь.")
            else:
                send(chat, "Код привязки не найден или истёк. Открой сайт и нажми "
                           "«Привязать Telegram» ещё раз.")
        elif arg.startswith("sub_"):
            parsed = _decode_sub(arg)
            if parsed:
                inst, group = parsed
                r = core_post("/api/subscribe", {"chat_id": str(chat), "inst": inst, "group": group})
                send(chat, "🔔 Подписка оформлена!" if r else "Ошибка, попробуй позже.")
            else:
                send(chat, "Не понял ссылку. Открой сайт и попробуй ещё раз.")
        else:
            prof = get_profile(chat)
            if prof:
                send(chat, f"Привет! Твоя группа: <b>{prof.get('group_name') or prof.get('group')}</b>.\n"
                           f"Кнопки ниже — расписание.")
            else:
                start_onboarding(chat)
    elif text == "/stop":
        core_post("/api/profile", {"chat_id": str(chat), "notify": False})
        send(chat, "🔕 Уведомления выключены. Включить: /notify")
    elif text == "/notify":
        core_post("/api/profile", {"chat_id": str(chat), "notify": True})
        send(chat, "🔔 Уведомления включены.")
    elif text.lower() == "сегодня":
        show_schedule(chat, "today")
    elif text.lower() == "завтра":
        show_schedule(chat, "tomorrow")
    elif text.lower() == "неделя":
        show_schedule(chat, "week")
    elif text.lower() == "профиль":
        show_profile(chat)
    elif text.startswith("/claude"):
        # мост с рабочей сессией Claude: владельца проверяет ядро (403 чужим)
        payload = text[len("/claude"):].strip()
        if payload:
            try:
                r = httpx.post(f"{CORE}/api/bridge/in",
                               json={"chat_id": str(chat), "text": payload}, timeout=15,
                               headers={"X-Bot-Key": BOT_KEY})
                if r.status_code == 200:
                    send(chat, "📨 Передал Claude.")
                elif r.status_code != 403:
                    send(chat, "Мост недоступен, попробуй позже.")
                # 403 (не владелец): молчим
            except Exception:
                send(chat, "Мост недоступен, попробуй позже.")
        elif ADMIN_CHAT_ID and str(chat) == ADMIN_CHAT_ID:
            send(chat, "Напиши так: /claude твой текст")
    else:
        m2 = __import__("re").match(r"^(\d{1,2})[./](\d{1,2})$", text)
        if m2:
            import datetime as _dt
            dd, mm = int(m2.group(1)), int(m2.group(2))
            try:
                d = _dt.date(_dt.date.today().year, mm, dd)
                show_schedule(chat, "date:" + d.isoformat())
            except ValueError:
                send(chat, "Не понял дату. Пример: 15.09")
            return
        if not text and (msg.get("photo") or msg.get("document")):
            send(chat, "Чтобы отправить фото или файл в поддержку, сначала нажми «Поддержка».")
            return
        send(chat, "Кнопки ниже: Сегодня · Завтра · Неделя · Профиль\n"
                   "Или напиши дату, например 15.09")


def main():
    if not TOKEN:
        print("BOT_TOKEN не задан, бот не запущен.")
        return
    try:  # если платформа включила webhook, long-polling получит 409: снимаем
        httpx.get(f"{API}/deleteWebhook", timeout=10)
    except Exception:
        pass
    for _ in range(3):
        if _fetch_bot_key():
            break
        time.sleep(2)
    print("raspika_bot запущен (long-polling), bot-key:",
          "ok" if BOT_KEY.startswith("bk_") else "НЕ НАЙДЕН")
    offset = None
    while True:
        try:
            r = httpx.get(f"{API}/getUpdates", params={"timeout": 30, "offset": offset}, timeout=40)
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                if upd.get("message"):
                    handle(upd["message"])
                elif upd.get("callback_query"):
                    handle_callback(upd["callback_query"])
        except Exception as e:
            print("poll err:", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
