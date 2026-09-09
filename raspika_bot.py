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
API = f"https://api.telegram.org/bot{TOKEN}"
SITE = "raspika.com"

KB = {"keyboard": [[{"text": "Сегодня"}, {"text": "Завтра"}],
                   [{"text": "Неделя"}, {"text": "Профиль"}]],
      "resize_keyboard": True}

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
    try:
        r = httpx.get(f"{CORE}{path}", params=params, timeout=20)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print("core err:", e)
        return None


def core_post(path, payload):
    try:
        r = httpx.post(f"{CORE}{path}", json=payload, timeout=20)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print("core err:", e)
        return None


def get_profile(chat):
    d = core_get("/api/profile", chat_id=str(chat))
    return d.get("data") if d else None


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

def fmt_day(lessons, date):
    d = datetime.date.fromisoformat(date)
    head = f"📅 <b>{RU_DAYS[d.weekday()]}, {d.day} {RU_MON[d.month]}</b>"
    day = [l for l in lessons if l["date"] == date]
    if not day:
        return head + "\n\nПар нет 🌤"
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
    stale = " \n\n⚠️ Сайт вуза недоступен, показана последняя копия." if sched.get("stale") else ""
    today = datetime.date.today()
    gname = prof.get("group_name") or prof.get("group")
    if mode == "today":
        send(chat, f"<b>{gname}</b>\n" + fmt_day(lessons, today.isoformat()) + stale)
    elif mode == "tomorrow":
        d = today + datetime.timedelta(days=1)
        send(chat, f"<b>{gname}</b>\n" + fmt_day(lessons, d.isoformat()) + stale)
    else:  # week: каждый день отдельным сообщением, чтобы не было каши
        monday = today - datetime.timedelta(days=today.weekday())
        sent = 0
        for i in range(6):
            d = monday + datetime.timedelta(days=i)
            if [l for l in lessons if l["date"] == d.isoformat()]:
                send(chat, fmt_day(lessons, d.isoformat()))
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
    send(chat, f"👤 <b>Профиль</b>\n"
               f"Группа: <b>{prof.get('group_name') or prof.get('group')}</b>\n"
               f"Уведомления об изменениях: {notify}\n\n"
               f"Сменить группу: на сайте {SITE} (изменится и здесь).\n"
               f"Уведомления: /stop — выключить, /notify — включить.")


# ---------------- handlers ----------------

def handle(msg):
    chat = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

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
                send(chat, f"Привет! Это <b>Raspika</b> — расписание вузов Омска.\n\n"
                           f"1. Открой {SITE}\n2. Выбери вуз и группу\n"
                           f"3. Нажми «Привязать Telegram»\n\n"
                           f"После этого здесь появится твоё расписание и уведомления об изменениях.")
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
                               json={"chat_id": str(chat), "text": payload}, timeout=15)
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
        send(chat, "Кнопки ниже: Сегодня · Завтра · Неделя · Профиль")


def main():
    if not TOKEN:
        print("BOT_TOKEN не задан, бот не запущен.")
        return
    try:  # если платформа включила webhook, long-polling получит 409: снимаем
        httpx.get(f"{API}/deleteWebhook", timeout=10)
    except Exception:
        pass
    print("raspika_bot запущен (long-polling)")
    offset = None
    while True:
        try:
            r = httpx.get(f"{API}/getUpdates", params={"timeout": 30, "offset": offset}, timeout=40)
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                if upd.get("message"):
                    handle(upd["message"])
        except Exception as e:
            print("poll err:", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
