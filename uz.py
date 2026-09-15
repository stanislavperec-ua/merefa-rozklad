"""Клієнт і парсер офіційного розкладу приміських поїздів swrailway.gov.ua (ElTrain v4.1).

Сайт віддає:
  * ?sid1=A&sid2=B&dateR=1&eventdate=YYYY-MM-DD  : прямі поїзди між станціями на дату
    (вже без скасованих на цю дату);
  * ?sid1=A&sid2=B&dateR=0                        : усі поїзди між станціями з терміном дії;
  * ?tid=N                                        : повний маршрут поїзда по зупинках
    і блок «Зміни руху» (скасування / зміни з датами дії).
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from urllib.parse import quote, urlencode

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("uz")


# ──────────────────────────────────────────────────────────────────────
# Часова зона Києва (з запасним варіантом для систем без бази tzdata)
# ──────────────────────────────────────────────────────────────────────
class _KyivFallback(tzinfo):
    """UTC+2 взимку, UTC+3 влітку; перехід в останню неділю березня/жовтня о 01:00 UTC.

    Реалізовано за зразком прикладу з документації datetime: usta зміщення
    рахуються від «настінного» (наївного) часу, без звернень до astimezone.
    """

    @staticmethod
    def _last_sunday(year: int, month: int) -> datetime:
        d = datetime(year, month, 31)          # березень і жовтень мають 31 день
        return d - timedelta(days=(d.weekday() + 1) % 7)

    def _dst_wall(self, dt: datetime) -> bool:
        """dt: наївний місцевий час."""
        start = self._last_sunday(dt.year, 3).replace(hour=3)    # 03:00 EET  → 04:00 EEST
        end = self._last_sunday(dt.year, 10).replace(hour=4)     # 04:00 EEST → 03:00 EET
        return start <= dt < end

    def utcoffset(self, dt):
        if dt is None:
            return timedelta(hours=2)
        return timedelta(hours=3 if self._dst_wall(dt.replace(tzinfo=None)) else 2)

    def dst(self, dt):
        if dt is None:
            return timedelta(0)
        return timedelta(hours=1 if self._dst_wall(dt.replace(tzinfo=None)) else 0)

    def tzname(self, dt):
        if dt is None:
            return "EET"
        return "EEST" if self._dst_wall(dt.replace(tzinfo=None)) else "EET"

    def fromutc(self, dt):
        naive_utc = dt.replace(tzinfo=None)
        start = self._last_sunday(naive_utc.year, 3).replace(hour=1)
        end = self._last_sunday(naive_utc.year, 10).replace(hour=1)
        hours = 3 if start <= naive_utc < end else 2
        return (naive_utc + timedelta(hours=hours)).replace(tzinfo=self)


def kyiv_tz() -> tzinfo:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Kyiv")
    except Exception:  # noqa: BLE001  (немає tzdata, наприклад Windows без пакета)
        return _KyivFallback()

BASE_URL = "https://swrailway.gov.ua/timetable/eltrain/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 merefa-rozklad/2.0"
)
REQUEST_PAUSE = 0.4          # пауза між запитами, щоб не навантажувати сайт
GATEWAY_PAUSE = 1.0          # пауза між запитами через шлюз
REQUEST_TIMEOUT = 60
CONNECT_TIMEOUT = 15
RETRIES = 6

# Сайт УЗ відкидає з'єднання з діапазонів великих хмар (Amazon, Microsoft, Google):
# перевірено, що навіть Render у регіоні Frankfurt (AWS) отримує timeout, тоді як Hetzner
# і Cloudflare проходять. Тому запити з хмари йдуть через Cloudflare Worker (worker.js).
# {url} підставляється URL-encoded.
GATEWAYS = [
    # Власний Cloudflare Worker (worker.js): основний і надійний шлях, без лімітів на нашу потребу
    "https://merefa-uz-gateway.stanislav-perec.workers.dev/fetch?url={url}",
    # Запасні публічні шлюзи на випадок, якщо воркер недоступний
    "https://api.cors.lol/?url={url}",
    "https://api.codetabs.com/v1/proxy?quest={url}",
]
GATEWAY_MARKER = "ElTrain"   # ознака справжньої сторінки: шлюз міг повернути свою помилку
# 520-524 від Cloudflare означають, що сайт УЗ не відповів воркеру. Причина не в частоті:
# Cloudflare виконує воркер у різних дата-центрах, і частину з них (LAX, IAD) сайт блокує,
# а інші (SEA, VIE) пускає. Тому після відмови не чекаємо довго, а швидко пробуємо ще раз:
# наступна спроба з високою ймовірністю потрапить у дозволений дата-центр.
BUSY_STATUSES = {520, 521, 522, 523, 524, 429, 503}
BUSY_PAUSE = 2.0

# Станції в порядку від Харкова. sid: код станції на swrailway.gov.ua.
STATIONS = [
    {"sid": 2528, "name": "Харків-Пас.",  "full": "Харків-Пасажирський"},
    {"sid": 2529, "name": "Новоселівка",  "full": "Новоселівка"},
    {"sid": 2530, "name": "Липовий Гай",  "full": "Липовий гай"},
    {"sid": 2531, "name": "Карачівка",    "full": "Карачівка"},
    {"sid": 2532, "name": "Покотилівка",  "full": "Покотилівка"},
    {"sid": 2533, "name": "Науковий",     "full": "Науковий"},
    {"sid": 3211, "name": "Високий",      "full": "Високий"},
    {"sid": 2767, "name": "Зелений Гай",  "full": "Зелений гай"},
    {"sid": 2535, "name": "Південний",    "full": "Південний"},
    {"sid": 2536, "name": "Комарівка",    "full": "Комарівка"},
    {"sid": 2539, "name": "Артемівка",    "full": "Артемівка"},
    {"sid": 2538, "name": "Мерефа",       "full": "Мерефа"},
]
STATION_SIDS = [s["sid"] for s in STATIONS]
KHARKIV_SID = 2528
MEREFA_SID = 2538

TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
TID_RE = re.compile(r"tid=(\d+)")
SID_RE = re.compile(r"sid=(\d+)")


class UZError(Exception):
    """Помилка завантаження або розбору сторінки УЗ."""


@dataclass
class TrainRow:
    """Рядок зі списку «Прямі потяги» між двома станціями."""
    tid: str
    num: str
    route: str
    days: str
    valid_from: str
    valid_to: str
    dep: str | None          # відправлення з першої станції
    arr: str | None          # прибуття на другу станцію
    has_notes: bool = False  # червоний лічильник «є актуальні повідомлення»


@dataclass
class Stop:
    sid: int
    name: str
    arr: str | None
    dep: str | None


@dataclass
class Note:
    text: str
    valid_from: str | None
    valid_to: str | None


@dataclass
class TrainPage:
    tid: str
    num: str
    route: str
    days: str
    valid_from: str | None
    valid_to: str | None
    stops: list[Stop] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────
# HTTP
# ──────────────────────────────────────────────────────────────────────
def default_gateways() -> list[str]:
    """Шлюзи зі змінної оточення UZ_GATEWAYS (через кому) або вбудований перелік."""
    env = os.environ.get("UZ_GATEWAYS", "").strip()
    if env:
        return [g.strip() for g in env.split(",") if g.strip()]
    return list(GATEWAYS)


def direct_allowed() -> bool:
    """У хмарі прямий маршрут завжди впирається в таймаут, тож його можна вимкнути.

    UZ_DIRECT=0 прибирає прямі спроби і економить 15 секунд на першому запиті.
    """
    return os.environ.get("UZ_DIRECT", "1").strip() not in ("0", "false", "no")


class Client:
    """Завантажує сторінки УЗ напряму або через європейський шлюз.

    Маршрути пробуються по черзі: прямий, потім кожен шлюз. Перший, що віддав справжню
    сторінку, запам'ятовується і далі використовується першим, щоб не витрачати час на
    свідомо недоступні шляхи.
    """

    def __init__(self, session: requests.Session | None = None, pause: float = REQUEST_PAUSE,
                 gateways: list[str] | None = None, direct: bool | None = None):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.pause = pause
        self.requests_made = 0
        if direct is None:
            direct = direct_allowed()
        self.routes: list[str | None] = ([None] if direct else []) + list(
            gateways if gateways is not None else default_gateways())
        if not self.routes:
            raise UZError("не задано жодного маршруту до сайту УЗ")
        self.route: str | None = self.routes[0]
        self.route_log: list[str] = []
        self.busy_hits = 0          # скільки разів сайт відповів «зайнято»

    @staticmethod
    def _url(route: str | None, params: dict) -> str:
        target = BASE_URL + "?" + urlencode(params)
        if route is None:
            return target
        return route.replace("{url}", quote(target, safe=""))

    def _pause_for(self, route: str | None) -> float:
        return self.pause if route is None else max(self.pause, GATEWAY_PAUSE)

    def _fetch(self, route: str | None, params: dict) -> str:
        r = self.session.get(self._url(route, params), timeout=(CONNECT_TIMEOUT, REQUEST_TIMEOUT))
        self.requests_made += 1
        if r.status_code in BUSY_STATUSES:
            self.busy_hits += 1
            raise UZError(f"сайт УЗ зайнятий (HTTP {r.status_code})")
        r.raise_for_status()
        r.encoding = "utf-8"
        text = r.text
        if GATEWAY_MARKER not in text:
            raise UZError(f"відповідь не схожа на сторінку ElTrain ({len(text)} символів)")
        return text

    def get(self, **params) -> str:
        last_err: Exception | None = None
        # спочатку маршрут, який уже спрацював, потім решта
        order = [self.route] + [r for r in self.routes if r != self.route]
        for attempt in range(1, RETRIES + 1):
            for route in order:
                try:
                    if self.requests_made:
                        # 522 від шлюзу означає, що сайт УЗ відмовив у з'єднанні:
                        # після невдачі чекаємо довше, щоб не довбати його поспіль
                        time.sleep(self._pause_for(route) * (2 if last_err else 1))
                    text = self._fetch(route, params)
                    if route != self.route:
                        name = route or "напряму"
                        log.info("UZ: перемикаюсь на маршрут %s", name)
                        self.route_log.append(name)
                        self.route = route
                    return text
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    log.warning("UZ %s через %s: спроба %d/%d невдала: %s",
                                params, route or "напряму", attempt, RETRIES, str(e)[:160])
                    if "зайнятий" in str(e):
                        time.sleep(BUSY_PAUSE)
            time.sleep(2 * attempt)
        raise UZError(f"не вдалося завантажити {params}: {last_err}")


    def pair_list(self, sid1: int, sid2: int, date: str | None = None) -> list[TrainRow]:
        if date:
            html = self.get(sid1=sid1, sid2=sid2, dateR=1, eventdate=date)
        else:
            html = self.get(sid1=sid1, sid2=sid2, dateR=0)
        return parse_pair_list(html)

    def train_page(self, tid: str) -> TrainPage:
        html = self.get(tid=tid, dateR=0)
        return parse_train_page(html, tid)


# ──────────────────────────────────────────────────────────────────────
# Парсинг
# ──────────────────────────────────────────────────────────────────────
def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _time_or_none(text: str) -> str | None:
    t = _clean(text)
    if TIME_RE.match(t):
        h, m = t.split(":")
        return f"{int(h):02d}:{m}"
    return None


def parse_pair_list(html: str) -> list[TrainRow]:
    """Розбирає таблицю «Прямі потяги» сторінки «між станціями»."""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[TrainRow] = []
    for tr in soup.find_all("tr"):
        a = tr.find("a", class_="et", href=TID_RE)
        if not a or not a.find_parent("td"):
            continue
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 10:
            continue
        tid = TID_RE.search(a["href"]).group(1)
        num = _clean(a.get_text())
        if not num.isdigit():
            continue
        has_notes = bool(tds[0].find("font", color="red")) or bool(tds[0].find("sup"))
        days = _clean(tds[2].get_text())
        route = _clean(tds[3].get_text()).rstrip("–- ").strip()
        # Після колонки маршруту йдуть [приб.1, назва1, відпр.1, приб.2, назва2, відпр.2]
        times = [_clean(td.get_text()) for td in tds[4:10]]
        dep = _time_or_none(times[2]) or _time_or_none(times[0])
        arr = _time_or_none(times[3]) or _time_or_none(times[5])
        dates = [_clean(td.get_text()) for td in tds[-2:]]
        valid_from = dates[0] if DATE_RE.fullmatch(dates[0]) else ""
        valid_to = dates[1] if DATE_RE.fullmatch(dates[1]) else ""
        rows.append(TrainRow(tid, num, route, days, valid_from, valid_to, dep, arr, has_notes))
    return rows


def parse_notes(soup: BeautifulSoup) -> list[Note]:
    """Блок «Зміни руху» (div#tabs-notes): текст + дати дії."""
    notes: list[Note] = []
    box = soup.find("div", id="tabs-notes")
    if not box:
        return notes
    for li in box.find_all("li"):
        note_div = li.find("div", class_="note")
        if not note_div:
            continue
        text = _clean(note_div.get_text(" "))
        text = re.sub(r"\s*-\s*ВІДМІНЕНО", ". ВІДМІНЕНО", text)
        descr = li.find("div", class_="n_descr")
        dates = DATE_RE.findall(descr.get_text(" ")) if descr else []
        valid_from = dates[0] if len(dates) > 0 else None
        valid_to = dates[1] if len(dates) > 1 else None
        notes.append(Note(text, valid_from, valid_to))
    return notes


def parse_train_page(html: str, tid: str) -> TrainPage:
    """Сторінка поїзда: маршрут, термін дії, зупинки, зміни руху."""
    soup = BeautifulSoup(html, "html.parser")
    box = soup.find("div", id="tabs-train")
    if not box:
        raise UZError(f"tid={tid}: на сторінці немає блоку розкладу")

    head = box.find("tr", class_="onx")
    route, valid_from, valid_to, num, days = "", None, None, "", ""
    if head:
        cells = head.find_all("td", recursive=False)
        if len(cells) >= 2:
            b = cells[1].find("b")
            route = _clean(b.get_text()) if b else ""
            dates = DATE_RE.findall(cells[1].get_text(" "))
            if len(dates) >= 2:
                valid_from, valid_to = dates[0], dates[1]
        inner = head.find("table")
        if inner:
            b = inner.find("b")
            num = _clean(b.get_text()) if b else ""
            txt = _clean(inner.get_text(" "))
            days = txt.replace(num, "", 1).strip() if num else txt

    stops: list[Stop] = []
    for tr in box.find_all("tr"):
        a = tr.find("a", class_="et", href=SID_RE)
        if not a:
            continue
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 4:
            continue
        sid = int(SID_RE.search(a["href"]).group(1))
        name = _clean(a.get_text())
        name = re.sub(r"^з\.п\.\s*", "", name)
        stops.append(Stop(sid, name, _time_or_none(tds[2].get_text()), _time_or_none(tds[3].get_text())))

    if not stops:
        raise UZError(f"tid={tid}: не знайдено зупинок")

    return TrainPage(tid, num, route, days, valid_from, valid_to, stops, parse_notes(soup))


def split_route(route: str) -> tuple[str, str]:
    """Маршрут виду «Харків-Пасажирський – Берестин» ділить на початкову і кінцеву станції."""
    parts = re.split(r"\s+[–—-]\s+", route, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return route.strip(), ""
