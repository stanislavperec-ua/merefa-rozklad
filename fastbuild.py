# -*- coding: utf-8 -*-
"""Швидке оновлення: сторінки УЗ завантажує телефон користувача, сервер їх лише розбирає.

Навіщо. Сайт swrailway.gov.ua обмежує частоту запитів за адресою відправника, і мережа
Render потрапляє під це обмеження: серія запитів отримує 522, збірка розтягується на
десять і більше хвилин. З телефона користувача (український оператор або домашній
провайдер) ті самі сторінки віддаються за частки секунди.

Тому кнопка «оновити» працює так:

    Mini App                          бот на Render
       │  POST /fast/start  ─────────────►  створює сесію, віддає перелік адрес
       │  ◄───────────────────────────────  (2 загальні переліки + 2 на кожну дату)
       │  завантажує їх через Cloudflare Worker, паралельно
       │  POST /fast/pages  ─────────────►  розбирає сторінки одразу, тримає лише результат
       │  ◄───────────────────────────────  коли перелік зібрано: адреси сторінок поїздів
       │  POST /fast/pages  ─────────────►  (тільки ті, яких немає у свіжому кеші)
       │  POST /fast/finish ─────────────►  збирає schedule.json і комітить у GitHub

Розбір і збірка тут ті самі, що й у звичайному шляху: `OfflineClient` підставляється
замість `uz.Client` у `build_schedule.build`, тож логіка скасувань, кешу і захисту від
хибних скасувань лишається одна на обидва шляхи.

Модуль навмисно без Flask: так його видно тестам у середовищі, де стоять тільки
requests і beautifulsoup4.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import parse_qsl

import build_schedule
import uz

log = logging.getLogger("fastbuild")

SESSION_TTL = 900          # сесія живе 15 хвилин
SESSION_LIMIT = 3          # більше одночасних збірок пам'яті сервісу не треба
MAX_HORIZON = 31          # сайт УЗ віддає розклад щонайменше на два місяці вперед
MAX_PAGE_CHARS = 600_000   # сторінка УЗ важить до 40 тис. символів, із запасом
MAX_PAGES_PER_BATCH = 16

PHASE_LISTS = 1            # переліки поїздів між станціями
PHASE_TRAINS = 2           # сторінки окремих поїздів
PHASE_READY = "ready"      # усе зібрано, можна будувати розклад


class FastError(Exception):
    """Помилка протоколу швидкого оновлення."""


# ──────────────────────────────────────────────────────────────────────
# Адреси сторінок
# ──────────────────────────────────────────────────────────────────────
def pair_id(sid1: int, sid2: int, day: str | None = None) -> str:
    return f"pair:{sid1}:{sid2}:{day or ''}"


def train_id(tid: str) -> str:
    return f"train:{tid}"


def _task(page_id: str, params: dict, gateway: str | None = None) -> dict:
    return {"id": page_id, "url": uz.via_gateway(uz.page_url(params), gateway)}


def pair_task(sid1: int, sid2: int, day: str | None = None, gateway: str | None = None) -> dict:
    return _task(pair_id(sid1, sid2, day), uz.pair_params(sid1, sid2, day), gateway)


def train_task(tid: str, gateway: str | None = None) -> dict:
    return _task(train_id(tid), uz.train_params(tid), gateway)


def horizon_dates(today: date, horizon: int) -> list[str]:
    return [(today + timedelta(days=i)).isoformat() for i in range(horizon)]


def list_tasks(today: date, horizon: int, gateway: str | None = None) -> list[dict]:
    """Перша фаза: повний перелік в обидва боки плюс перелік на кожну дату горизонту."""
    kh, mer = uz.KHARKIV_SID, uz.MEREFA_SID
    tasks = [pair_task(kh, mer, None, gateway), pair_task(mer, kh, None, gateway)]
    for day in horizon_dates(today, horizon):
        tasks.append(pair_task(kh, mer, day, gateway))
        tasks.append(pair_task(mer, kh, day, gateway))
    return tasks


def rows_from_pairs(pairs: dict[str, list[uz.TrainRow]]) -> dict[str, uz.TrainRow]:
    """Усі поїзди з усіх переліків; червоний лічильник повідомлень збирається з усіх згадок."""
    rows: dict[str, uz.TrainRow] = {}
    for rows_of_page in pairs.values():
        for row in rows_of_page:
            known = rows.get(row.tid)
            if known is None:
                rows[row.tid] = row
            elif row.has_notes:
                known.has_notes = True
    return rows


def train_tasks(rows: dict[str, uz.TrainRow], cache: dict, now: datetime,
                gateway: str | None = None) -> list[dict]:
    """Друга фаза: лише ті поїзди, яким не вистачає свіжого кешу."""
    return [train_task(tid, gateway) for tid, row in rows.items()
            if build_schedule.needs_train_page(row, cache, now)]


# ──────────────────────────────────────────────────────────────────────
# Клієнт, що не ходить у мережу
# ──────────────────────────────────────────────────────────────────────
class OfflineClient:
    """Підміна `uz.Client`: сторінки вже завантажив телефон, лишилось віддати розібране.

    Інтерфейс збігається з `uz.Client` у тій частині, яку використовує
    `build_schedule.build`, тому збірка не відрізняє один шлях від іншого.
    """

    route = "Mini App (телефон користувача)"

    def __init__(self) -> None:
        self.pairs: dict[str, list[uz.TrainRow]] = {}
        self.trains: dict[str, uz.TrainPage] = {}
        self.requests_made = 0
        self.busy_hits = 0

    def add_pair(self, sid1: int, sid2: int, day: str | None, html: str) -> int:
        rows = uz.parse_pair_list(html)
        self.pairs[pair_id(sid1, sid2, day)] = rows
        return len(rows)

    def add_train(self, tid: str, html: str) -> uz.TrainPage:
        page = uz.parse_train_page(html, tid)
        self.trains[tid] = page
        return page

    def pair_list(self, sid1: int, sid2: int, date: str | None = None) -> list[uz.TrainRow]:
        key = pair_id(sid1, sid2, date)
        if key not in self.pairs:
            raise uz.UZError(f"сторінку {key} телефон не передав")
        self.requests_made += 1
        return self.pairs[key]

    def train_page(self, tid: str) -> uz.TrainPage:
        page = self.trains.get(tid)
        if page is None:
            raise uz.UZError(f"сторінку поїзда {tid} телефон не передав")
        self.requests_made += 1
        return page


# ──────────────────────────────────────────────────────────────────────
# Сесія збірки
# ──────────────────────────────────────────────────────────────────────
class Session:
    """Одне натискання кнопки: перелік потрібних сторінок і те, що вже прийшло."""

    def __init__(self, today: date, horizon: int, cache: dict | None = None,
                 trusted: bool = False, gateway: str | None = None,
                 now: datetime | None = None, sid: str | None = None):
        self.id = sid or secrets.token_urlsafe(9)
        self.today = today
        self.horizon = max(1, min(int(horizon), MAX_HORIZON))
        self.cache = cache if cache is not None else {}
        self.trusted = trusted
        self.gateway = gateway
        self.now = now or datetime.now(build_schedule.KYIV)
        self.created = time.time()
        self.touched = self.created
        self.client = OfflineClient()
        # результат контрольної звірки, яку сервіс робить сам, поки телефон качає сторінки:
        # (дата, перелік tid або None, текст помилки або None)
        self.check: tuple[str, set[str] | None, str | None] | None = None
        self.check_ready = threading.Event()
        self.phase: int | str = PHASE_LISTS
        self.tasks: list[dict] = list_tasks(self.today, self.horizon, gateway)
        self.pending: set[str] = {t["id"] for t in self.tasks}
        self.failed: set[str] = set()
        self.done = 0
        self.lock = threading.Lock()

    # ── приймання сторінок ────────────────────────────────
    def submit(self, page_id: str, html: str) -> None:
        """Розбирає сторінку одразу: HTML не зберігається, лише готові рядки розкладу."""
        if not isinstance(page_id, str) or not isinstance(html, str):
            raise FastError("очікую рядки id і html")
        if len(html) > MAX_PAGE_CHARS:
            raise FastError(f"сторінка завелика ({len(html)} символів)")
        if uz.GATEWAY_MARKER not in html:
            # шлюз міг віддати свою сторінку помилки; прийняти її означало б вирішити,
            # що на дату немає жодного поїзда, тому такі сторінки не зараховуються
            raise FastError("сторінка не схожа на розклад УЗ")
        parts = page_id.split(":")
        if parts[0] == "pair" and len(parts) == 4:
            sid1, sid2 = int(parts[1]), int(parts[2])
            if sid1 not in uz.STATION_SIDS or sid2 not in uz.STATION_SIDS:
                raise FastError(f"невідома станція в {page_id}")
            self.client.add_pair(sid1, sid2, parts[3] or None, html)
        elif parts[0] == "train" and len(parts) == 2:
            if not parts[1].isdigit():
                raise FastError(f"невірний номер поїзда в {page_id}")
            self.client.add_train(parts[1], html)
        else:
            raise FastError(f"невідомий ідентифікатор сторінки {page_id}")
        with self.lock:
            if page_id in self.pending:
                self.pending.discard(page_id)
                self.done += 1
            self.failed.discard(page_id)
            self.touched = time.time()

    def give_up(self, page_ids: list[str]) -> None:
        """Сторінки, які телефон так і не зміг завантажити: без них дата буде пропущена."""
        with self.lock:
            for page_id in page_ids:
                if page_id in self.pending:
                    self.pending.discard(page_id)
                    self.failed.add(page_id)
            self.touched = time.time()

    # ── перехід між фазами ────────────────────────────────
    def advance(self) -> list[dict]:
        """Коли фаза зібрана, повертає завдання наступної фази (або порожній перелік)."""
        with self.lock:
            if self.pending or self.phase == PHASE_READY:
                return []
            if self.phase == PHASE_TRAINS:
                self.phase = PHASE_READY
                return []
        rows = rows_from_pairs(self.client.pairs)
        if not rows:
            raise FastError("у переліках немає жодного поїзда")
        tasks = train_tasks(rows, self.cache, self.now, self.gateway)
        with self.lock:
            self.phase = PHASE_TRAINS if tasks else PHASE_READY
            self.tasks = tasks
            self.pending = {t["id"] for t in tasks}
            self.touched = time.time()
        log.info("Сесія %s: переліки зібрано (%d поїздів), сторінок треба %d",
                 self.id, len(rows), len(tasks))
        return tasks

    # ── збірка ────────────────────────────────────────────
    def build(self) -> dict:
        """Той самий build_schedule.build, лише клієнт не ходить у мережу."""
        schedule = build_schedule.build(self.client, self.today, self.horizon, self.cache, False)
        schedule.setdefault("stats", {})["source"] = "mini app"
        return schedule

    def state(self) -> dict:
        """Те, що бачить телефон: скільки лишилось і які сторінки ще треба завантажити."""
        with self.lock:
            return {"session": self.id, "phase": self.phase, "pending": len(self.pending),
                    "received": self.done, "failed": sorted(self.failed),
                    "tasks": [t for t in self.tasks if t["id"] in self.pending]}

    def expired(self, ttl: int = SESSION_TTL) -> bool:
        return (time.time() - self.touched) > ttl


class SessionStore:
    """Сесії живуть у пам'яті процесу: збірка триває секунди, переживати рестарт нема потреби."""

    def __init__(self, ttl: int = SESSION_TTL, limit: int = SESSION_LIMIT):
        self.ttl = ttl
        self.limit = limit
        self._items: dict[str, Session] = {}
        self._lock = threading.Lock()

    def sweep(self) -> None:
        with self._lock:
            for sid in [s for s, sess in self._items.items() if sess.expired(self.ttl)]:
                del self._items[sid]

    def add(self, session: Session) -> Session:
        self.sweep()
        with self._lock:
            if len(self._items) >= self.limit:
                oldest = min(self._items.values(), key=lambda s: s.touched)
                del self._items[oldest.id]
            self._items[session.id] = session
        return session

    def get(self, sid: str) -> Session:
        self.sweep()
        with self._lock:
            session = self._items.get(sid)
        if session is None:
            raise FastError("сесія застаріла або втрачена, почніть оновлення спочатку")
        return session

    def drop(self, sid: str) -> None:
        with self._lock:
            self._items.pop(sid, None)

    def __len__(self) -> int:
        return len(self._items)


# ──────────────────────────────────────────────────────────────────────
# Підпис Telegram
# ──────────────────────────────────────────────────────────────────────
def check_init_data(init_data: str, bot_token: str, max_age: int = 86400,
                    now: float | None = None) -> dict | None:
    """Перевіряє підпис `Telegram.WebApp.initData`. Повертає поля або None.

    Потрібно, щоб сторінки приймались від справжнього користувача бота, а не від
    будь-кого, хто знає адресу сервісу: інакше можна було б підсунути вигаданий розклад.
    Опис перевірки: core.telegram.org/bots/webapps (Validating data received via Mini App).
    """
    if not init_data or not bot_token:
        return None
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    given = fields.pop("hash", "")
    if not given:
        return None
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, given):
        return None
    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError:
        return None
    if max_age and abs((now or time.time()) - auth_date) > max_age:
        return None
    return fields


def day_tids(schedule: dict, day: str) -> set[str]:
    """Перелік поїздів, що курсують у вказану дату, з готового розкладу."""
    return set(schedule.get("days", {}).get(day, {}).get("running", []))
