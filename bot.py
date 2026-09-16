# -*- coding: utf-8 -*-
"""Telegram-бот «Розклад електричок Харків – Мерефа» (Flask + webhook, хостинг Render).

Команди:
  /start, /rozklad : попередження + кнопка Mini App
  /next [станція]  : найближчі електрички зі станції (типово Високий) в обидва боки
  /zminy           : скасування на сьогодні/завтра, зміни руху УЗ, затримки з каналу
  /help            : довідка

Дані бот читає з GitHub Pages (schedule.json, live.json), які оновлює GitHub Actions.
Змінні оточення: BOT_TOKEN, RENDER_URL, PORT, WEBHOOK_SECRET (необов'язково).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta

import requests as req
import telebot
from flask import Flask, abort, request
from telebot.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

import uz
import gateway
from gateway import gateway_bp

log = logging.getLogger("bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
RENDER_URL = os.environ.get("RENDER_URL", "https://merefa-rozklad.onrender.com").rstrip("/")
PORT = int(os.environ.get("PORT", 10000))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

APP_URL = "https://stanislavperec-ua.github.io/merefa-rozklad/"
SCHEDULE_URL = APP_URL + "schedule.json"
LIVE_URL = APP_URL + "live.json"
CHANNEL_URL = "https://t.me/UZprymisky"
UZ_TRAIN_URL = "https://swrailway.gov.ua/timetable/eltrain/?tid="

KYIV = uz.kyiv_tz()
DEFAULT_STATION_SID = 3211      # Високий
DATA_TTL = 300                  # секунд кешування schedule.json / live.json

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)
# Збирач розкладу: /refresh для кнопки в Mini App, /status, /schedule.json, /whoami
app.register_blueprint(gateway_bp)

WARNING = (
    "⚠️ <b>УВАГА</b> ⚠️\n\n"
    "❗ Цей бот НЕ є офіційним інструментом Укрзалізниці. Створений на ентузіазмі для зручності пасажирів.\n\n"
    "❗ Розклад береться з офіційного сайту swrailway.gov.ua і оновлюється кілька разів на добу, "
    "затримки і скасування беруться з каналу УЗ «Приміські поїзди». Дані можуть запізнюватись.\n\n"
    "🔍 Завжди перевіряйте наявність поїздів на офіційних ресурсах перед поїздкою!"
)

HELP = (
    "<b>Команди</b>\n"
    "/rozklad: відкрити застосунок з повним розкладом\n"
    "/next: найближчі електрички з Високого\n"
    "/next Мерефа: найближчі з іншої станції (Харків, Покотилівка, Комарівка…)\n"
    "/zminy: скасування, зміни руху та затримки\n\n"
    "Можна просто написати назву станції."
)


# ──────────────────────────────────────────────────────────────────────
# Дані з GitHub Pages
# ──────────────────────────────────────────────────────────────────────
class DataCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, dict]] = {}

    def get(self, url: str, ttl: int = DATA_TTL) -> dict | None:
        now = time.time()
        with self._lock:
            hit = self._store.get(url)
            if hit and now - hit[0] < ttl:
                return hit[1]
        try:
            r = req.get(url, params={"t": int(now // 60)}, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("Не вдалося завантажити %s: %s", url, e)
            with self._lock:
                hit = self._store.get(url)
            return hit[1] if hit else None
        with self._lock:
            self._store[url] = (now, data)
        return data


cache = DataCache()


def now_kyiv() -> datetime:
    return datetime.now(KYIV)


def day_key(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


def fmt_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso)
        return d.strftime("%d.%m %H:%M")
    except ValueError:
        return iso


def find_station(query: str) -> dict | None:
    q = (query or "").strip().lower().replace("ё", "е")
    if not q:
        return None
    aliases = {
        "харків": 2528, "харьков": 2528, "харкiв": 2528, "пас": 2528,
        "мерефа": 2538, "високий": 3211, "высокий": 3211, "висок": 3211,
        "комарівка": 2536, "комаровка": 2536, "покотилівка": 2532, "покотиловка": 2532,
        "південний": 2535, "южный": 2535, "зелений гай": 2767, "зеленый гай": 2767,
        "артемівка": 2539, "артемовка": 2539, "науковий": 2533, "научный": 2533,
        "карачівка": 2531, "карачевка": 2531, "липовий гай": 2530, "липовый гай": 2530,
        "новоселівка": 2529, "новоселовка": 2529,
    }
    sid = None
    for name, s in aliases.items():
        if q.startswith(name[:5]) or name.startswith(q[:5]):
            sid = s
            break
    if sid is None:
        return None
    return next((s for s in uz.STATIONS if s["sid"] == sid), None)


def day_trains(schedule: dict, key: str) -> tuple[list[dict], bool]:
    """Поїзди, що курсують у день key. Повертає (список, чи перевірено на сайті УЗ)."""
    trains = schedule.get("trains", {})
    day = schedule.get("days", {}).get(key)
    if day:
        return [dict(trains[t], tid=t) for t in day.get("running", []) if t in trains], True
    out = []
    for tid, t in trains.items():
        lo, hi = t.get("valid_from") or "0000-00-00", t.get("valid_to") or "9999-99-99"
        if lo <= key <= hi:
            out.append(dict(t, tid=tid))
    return out, False


def live_for(live: dict | None, num: str, key: str) -> dict | None:
    if not live:
        return None
    for it in live.get("items", []):
        if it.get("num") == num and str(it.get("time", "")).startswith(key):
            return it
    return None


def format_next(station: dict, limit: int = 5) -> str:
    schedule = cache.get(SCHEDULE_URL)
    if not schedule:
        return "😕 Розклад тимчасово недоступний. Спробуйте /rozklad."
    live = cache.get(LIVE_URL)
    now = now_kyiv()
    sid = str(station["sid"])
    lines = [f"🚉 <b>{station['name']}</b> · {now.strftime('%d.%m %H:%M')}"]

    for direction, title in (("k", "→ на Харків"), ("m", "→ на Мерефу")):
        rows = []
        for offset in (0, 1):
            d = now + timedelta(days=offset)
            key = day_key(d)
            trains, verified = day_trains(schedule, key)
            for t in trains:
                st = t.get("stops", {}).get(sid)
                if t.get("dir") != direction or not st:
                    continue
                dep = st.get("dep") or st.get("arr")
                if not dep:
                    continue
                hh, mm = map(int, dep.split(":"))
                when = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if when < now:
                    continue
                rows.append((when, dep, t, offset, verified))
        rows.sort(key=lambda r: r[0])
        rows = rows[:limit]
        lines.append(f"\n<b>{title}</b>")
        if not rows:
            lines.append("  немає рейсів найближчим часом")
        for when, dep, t, offset, verified in rows:
            dest = t.get("to") if direction == "m" else t.get("from")
            tag = " <i>(завтра)</i>" if offset else ""
            extra = ""
            it = live_for(live, t.get("num", ""), day_key(when))
            if it:
                if it.get("kind") == "delay":
                    extra = f" ⏱ +{it['minutes']} хв" if it.get("minutes") else " ⏱ затримка"
                elif it.get("kind") == "cancel":
                    extra = " ❌ скасовано (канал УЗ)"
            lines.append(f"  <b>{dep}</b>{tag} №{t.get('num')} {dest}{extra}")
    if not schedule.get("days", {}).get(day_key(now)):
        lines.append("\nℹ️ Дата поза перевіреним періодом, показано плановий розклад.")
    lines.append(f"\n<i>Розклад УЗ від {fmt_time(schedule.get('generated'))}</i>")
    return "\n".join(lines)


def format_changes() -> str:
    schedule = cache.get(SCHEDULE_URL)
    live = cache.get(LIVE_URL)
    if not schedule:
        return "😕 Розклад тимчасово недоступний. Спробуйте /rozklad."
    now = now_kyiv()
    trains = schedule.get("trains", {})
    lines = ["<b>Зміни в русі</b>"]

    any_cancel = False
    for offset, label in ((0, "сьогодні"), (1, "завтра")):
        key = day_key(now + timedelta(days=offset))
        day = schedule.get("days", {}).get(key, {})
        cancelled = day.get("cancelled", [])
        if cancelled:
            any_cancel = True
            lines.append(f"\n❌ <b>Скасовано {label} ({key[8:]}.{key[5:7]})</b>")
            for c in cancelled:
                t = trains.get(c["tid"], {})
                lines.append(f"  №{t.get('num')} {t.get('route')}\n  <i>{c.get('note', '')}</i>")
    if not any_cancel:
        lines.append("\n✅ Скасувань на сьогодні і завтра за даними УЗ немає.")

    notes = []
    seen = set()
    for tid, t in trains.items():
        for n in t.get("notes", []):
            if n["text"] in seen:
                continue
            seen.add(n["text"])
            notes.append((n.get("to") or "", n["text"]))
    if notes:
        lines.append("\n📋 <b>Повідомлення УЗ про зміни руху</b>")
        for _to, text in sorted(notes):
            lines.append(f"  • {text}")

    today = day_key(now)
    items = [i for i in (live or {}).get("items", []) if str(i.get("time", "")).startswith(today)]
    if items:
        lines.append("\n📡 <b>Сьогодні з каналу УЗ</b>")
        for it in items[:8]:
            when = it.get("time", "")[11:16]
            kind = {"delay": "⏱", "cancel": "❌", "resume": "✅", "change": "🔁"}.get(it.get("kind"), "ℹ️")
            mins = f" +{it['minutes']} хв" if it.get("minutes") else ""
            lines.append(f"  {kind} {when} №{it.get('num')}{mins}: <a href=\"{it.get('link')}\">пост</a>")
    lines.append(f"\n<i>Розклад УЗ від {fmt_time(schedule.get('generated'))}"
                 + (f", канал від {fmt_time(live.get('generated'))}" if live else "") + "</i>")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Telegram
# ──────────────────────────────────────────────────────────────────────
def keep_alive():
    while True:
        time.sleep(180)
        try:
            req.get(RENDER_URL, timeout=20)
        except Exception as e:  # noqa: BLE001
            log.warning("Self-ping failed: %s", e)


def webapp_button() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(text="🚂 Відкрити розклад", web_app=WebAppInfo(url=APP_URL)))
    kb.add(InlineKeyboardButton(text="📡 Канал УЗ «Приміські поїзди»", url=CHANNEL_URL))
    return kb


def send_rozklad(chat_id: int) -> None:
    bot.send_message(chat_id, WARNING)
    bot.send_message(chat_id, "Розклад електричок Харків – Мерефа\n\n" + HELP, reply_markup=webapp_button(),
                     disable_web_page_preview=True)


@bot.message_handler(commands=["start", "rozklad"])
def cmd_rozklad(message):
    send_rozklad(message.chat.id)


@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.send_message(message.chat.id, HELP, reply_markup=webapp_button(), disable_web_page_preview=True)


@bot.message_handler(commands=["next"])
def cmd_next(message):
    arg = message.text.split(maxsplit=1)[1] if len(message.text.split(maxsplit=1)) > 1 else ""
    station = find_station(arg) if arg else None
    if arg and not station:
        bot.send_message(message.chat.id, "Не знаю такої станції. Приклад: /next Мерефа")
        return
    station = station or next(s for s in uz.STATIONS if s["sid"] == DEFAULT_STATION_SID)
    bot.send_message(message.chat.id, format_next(station), reply_markup=webapp_button(),
                     disable_web_page_preview=True)


@bot.message_handler(commands=["zminy", "zmini", "changes"])
def cmd_changes(message):
    bot.send_message(message.chat.id, format_changes(), reply_markup=webapp_button(),
                     disable_web_page_preview=True)


@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback(message):
    station = find_station(message.text or "")
    if station:
        bot.send_message(message.chat.id, format_next(station), reply_markup=webapp_button(),
                         disable_web_page_preview=True)
    else:
        send_rozklad(message.chat.id)


# ──────────────────────────────────────────────────────────────────────
# Flask
# ──────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    # UptimeRobot стукає сюди кожні 5 хвилин, щоб сервіс не засинав. Заразом це наш
    # найнадійніший будильник: раз на чверть години він запускає читання каналу УЗ
    # (cron GitHub Actions пропускає запуски, а Cloudflare виконує свій, коли вирішить).
    try:
        gateway.maybe_collect_live()
        gateway.maybe_poke_worker()
    except Exception:  # noqa: BLE001
        log.exception("Не вдалося запустити фонове оновлення")
    return "OK", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        abort(403)
    if request.headers.get("content-type", "").startswith("application/json"):
        update = telebot.types.Update.de_json(request.get_data(as_text=True))
        bot.process_new_updates([update])
        return "", 200
    abort(403)


def setup() -> None:
    bot.remove_webhook()
    time.sleep(0.5)
    bot.set_webhook(url=f"{RENDER_URL}/webhook", secret_token=WEBHOOK_SECRET or None)
    bot.set_my_commands([
        BotCommand("rozklad", "Відкрити розклад електричок"),
        BotCommand("next", "Найближчі електрички зі станції"),
        BotCommand("zminy", "Скасування, зміни, затримки"),
        BotCommand("help", "Довідка"),
    ])
    log.info("Webhook встановлено: %s/webhook", RENDER_URL)


if __name__ == "__main__":
    threading.Thread(target=keep_alive, daemon=True).start()
    setup()
    app.run(host="0.0.0.0", port=PORT)
