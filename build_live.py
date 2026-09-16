#!/usr/bin/env python3
"""Оперативні повідомлення каналу УЗ «Приміські поїзди» (t.me/UZprymisky) → live.json.

Канал пише про затримки, скасування та відновлення руху по всій Україні.
Скрипт гортає веб-версію каналу (?before=<id>) назад на LOOKBACK_HOURS годин,
відбирає пости про поїзди з нашого розкладу (номери беруться зі schedule.json)
з прив'язкою до Харківщини і класифікує їх: затримка / скасування / відновлення / зміни.

Запуск:  python build_live.py [--hours 12] [--pages 8]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

import uz

log = logging.getLogger("build_live")

KYIV = uz.kyiv_tz()
CHANNEL = "UZprymisky"
CHANNEL_URL = f"https://t.me/s/{CHANNEL}"
SCHEDULE_FILE = "schedule.json"
LIVE_FILE = "live.json"
LOOKBACK_HOURS = 12
MAX_PAGES = 8
REWRITE_AFTER_HOURS = 3       # оновлювати generated навіть без змін, щоб дата у застосунку не «старіла»
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36 merefa-rozklad/2.0"

# Запасний перелік номерів, якщо schedule.json ще немає
FALLBACK_NUMS = {
    "6701", "6685", "6513", "6687", "6853", "6691", "7001", "6525", "6705", "6693", "6527", "6697", "6529",
    "6506", "6682", "6508", "6684", "6702", "6512", "6678", "6686", "6516", "6688", "6690", "6524", "6694",
    "6704", "6856", "6852",
}
REGION_WORDS = ("харків", "#харківщина", "мереф", "берестин", "лозов", "зміїв", "златопіл", "герсеван", "біляївк", "власівк")

CANCEL_WORDS = ("скасован", "скасову", "відмінен", "не курсує", "не курсуватим", "не буде курсувати", "припинено")
RESUME_WORDS = ("рух відновлен", "відновлено рух", "відновлює рух", "рухається за графіком", "курсує за графіком",
                "затримку ліквідовано", "без затримки")
CHANGE_WORDS = ("курсуватиме до", "прямуватиме до", "скорочен", "зміна маршруту", "зі зміною", "змінен")
DELAY_WORDS = ("затрим",)

DELAY_RE = re.compile(r"затримк\w*\s*(?:(\d+)\s*год\.?)?\s*(?:(\d+)\s*хв)?", re.I)
# Шаблонний хвіст кожного поста каналу («У разі підвищеної небезпеки поїзд буде зупинено…»): прибираємо
TAIL_RE = re.compile(r"\s*(?:❗️?\s*)?У разі підвищеної небезпеки.*$", re.S)


def strip_tail(text: str) -> str:
    text = TAIL_RE.sub("", text).strip()
    return re.sub(r"\s+", " ", text)


def fetch_page(session: requests.Session, before: str | None = None) -> list[dict]:
    url = CHANNEL_URL + (f"?before={before}" if before else "")
    r = session.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    posts = []
    for msg in soup.find_all("div", class_="tgme_widget_message"):
        post_id = (msg.get("data-post") or "").split("/")[-1]
        text_div = msg.find("div", class_="tgme_widget_message_text")
        time_tag = msg.find("time")
        if not post_id or not text_div or not time_tag or not time_tag.get("datetime"):
            continue
        try:
            ts = datetime.fromisoformat(time_tag["datetime"].replace("Z", "+00:00"))
        except ValueError:
            continue
        posts.append({
            "id": int(post_id),
            "time": ts.astimezone(timezone.utc),
            "text": text_div.get_text(" ", strip=True),
            "link": f"https://t.me/{CHANNEL}/{post_id}",
        })
    return posts


def fetch_recent(session: requests.Session, since: datetime, max_pages: int) -> tuple[list[dict], int]:
    """Гортає канал назад, поки не дійде до постів старших за `since`."""
    collected: dict[int, dict] = {}
    before: str | None = None
    pages = 0
    while pages < max_pages:
        posts = fetch_page(session, before)
        pages += 1
        if not posts:
            break
        for p in posts:
            collected[p["id"]] = p
        oldest = min(posts, key=lambda p: p["id"])
        if oldest["time"] < since:
            break
        before = str(oldest["id"])
    recent = [p for p in collected.values() if p["time"] >= since]
    recent.sort(key=lambda p: p["id"])
    return recent, pages


def load_train_nums() -> tuple[set[str], dict[str, str]]:
    """Номери поїздів і маршрути зі schedule.json (або запасний перелік)."""
    try:
        with open(SCHEDULE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        nums, routes = set(), {}
        for t in data.get("trains", {}).values():
            if t.get("num"):
                nums.add(t["num"])
                routes[t["num"]] = t.get("route", "")
        if nums:
            return nums, routes
    except (OSError, ValueError):
        pass
    return set(FALLBACK_NUMS), {}


def classify(text: str) -> tuple[str, int | None]:
    """Тип повідомлення за змістовною частиною поста (без шаблонного хвоста)."""
    low = strip_tail(text).lower()
    if any(w in low for w in CANCEL_WORDS):
        return "cancel", None
    if any(w in low for w in DELAY_WORDS):
        minutes = None
        m = DELAY_RE.search(low)
        if m and (m.group(1) or m.group(2)):
            minutes = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
        return "delay", minutes
    if any(w in low for w in RESUME_WORDS):
        return "resume", None
    if any(w in low for w in CHANGE_WORDS):
        return "change", None
    return "info", None


def find_our_trains(text: str, nums: set[str]) -> list[str]:
    found = []
    for n in sorted(nums):
        if re.search(rf"(?<!\d){n}(?!\d)", text):
            found.append(n)
    return found


def is_our_region(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in REGION_WORDS)


def build_items(posts: list[dict], nums: set[str]) -> list[dict]:
    items = []
    for p in posts:
        trains = find_our_trains(p["text"], nums)
        if not trains or not is_our_region(p["text"]):
            continue
        kind, minutes = classify(p["text"])
        short = strip_tail(p["text"])
        for n in trains:
            items.append({
                "num": n,
                "kind": kind,
                "minutes": minutes,
                "text": short[:400],
                "time": p["time"].astimezone(KYIV).isoformat(timespec="minutes"),
                "link": p["link"],
            })
    items.sort(key=lambda i: i["time"], reverse=True)
    return items


def collect(hours: float = LOOKBACK_HOURS, pages: int = MAX_PAGES,
            nums: set[str] | None = None, session: requests.Session | None = None,
            now: datetime | None = None) -> dict:
    """Збирає оперативні повідомлення каналу і повертає вміст live.json.

    Без читання і запису файлів, щоб цю саму збірку міг робити бот на Render: GitHub
    Actions за розкладом запускається нерегулярно, а затримки поїздів цінні саме свіжими.
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", USER_AGENT)
    posts, scanned = fetch_recent(session, since, pages)
    if nums is None:
        nums, _routes = load_train_nums()
    items = build_items(posts, nums)
    log.info("Постів за %.0f год: %d (сторінок %d), про наші поїзди: %d",
             hours, len(posts), scanned, len(items))
    return {
        "generated": now.astimezone(KYIV).isoformat(timespec="seconds"),
        "since": since.astimezone(KYIV).isoformat(timespec="minutes"),
        "channel": f"https://t.me/{CHANNEL}",
        "items": items,
        "stats": {"posts_scanned": len(posts), "pages": scanned},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=LOOKBACK_HOURS)
    ap.add_argument("--pages", type=int, default=MAX_PAGES)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=args.hours)
    try:
        live = collect(hours=args.hours, pages=args.pages, now=now)
    except requests.RequestException as e:
        log.error("Канал недоступний, live.json не змінено: %s", e)
        return 2

    items = live["items"]
    for it in items:
        log.info("  %s %s %s: %s", it["time"], it["num"], it["kind"], it["text"][:90])

    old = {}
    try:
        with open(LIVE_FILE, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        pass
    unchanged = old.get("items") == items
    if unchanged:
        try:
            old_gen = datetime.fromisoformat(old.get("generated", ""))
            if now - old_gen < timedelta(hours=REWRITE_AFTER_HOURS):
                log.info("Змін немає, live.json не перезаписую")
                return 0
        except ValueError:
            pass

    with open(LIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(live, f, ensure_ascii=False, indent=1)
        f.write("\n")
    log.info("live.json збережено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
