#!/usr/bin/env python3
"""Збирає schedule.json з офіційного розкладу swrailway.gov.ua.

Що робить:
  1. Бере повний перелік прямих поїздів Харків ⇄ Мерефа («всі дні»).
  2. Для кожної дати горизонту (сьогодні + HORIZON_DAYS) бере перелік «на дату»:
     сайт УЗ уже виключає з нього скасовані поїзди.
  3. Для кожного поїзда завантажує сторінку з повним маршрутом (кешується
     у trains_cache.json на CACHE_TTL_DAYS) і блок «Зміни руху» (завжди свіжий,
     якщо на сайті стоїть лічильник повідомлень).
  4. Поїзд, що діє за терміном, але відсутній у переліку «на дату», позначається
     скасованим на цю дату; текст береться зі «Змін руху».

Запуск:  python build_schedule.py [--horizon 14] [--max-age-hours 4] [--force] [--refresh-cache]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta

import uz

log = logging.getLogger("build_schedule")

KYIV = uz.kyiv_tz()
SCHEDULE_FILE = "schedule.json"
CACHE_FILE = "trains_cache.json"
SCHEMA_VERSION = 2
HORIZON_DAYS = 14
CACHE_TTL_DAYS = 7
CACHE_KEEP_DAYS = 45


def now_kyiv() -> datetime:
    return datetime.now(KYIV)


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")


def page_to_entry(page: uz.TrainPage, fetched_at: datetime) -> dict:
    """Сторінка поїзда → запис для schedule.json / кешу."""
    order = {sid: i for i, sid in enumerate(uz.STATION_SIDS)}
    stops: dict[str, dict] = {}
    seq: list[int] = []
    for s in page.stops:
        if s.sid in order:
            stops[str(s.sid)] = {"arr": s.arr, "dep": s.dep}
            seq.append(s.sid)
    first, last = uz.split_route(page.route)
    # напрям: «m» → на Мерефу, «k» → на Харків (за порядком зупинок)
    direction = "m"
    if len(seq) >= 2:
        direction = "m" if order[seq[0]] < order[seq[-1]] else "k"
    elif first and not first.startswith("Харків"):
        direction = "k"
    return {
        "num": page.num,
        "route": page.route,
        "from": first,
        "to": last,
        "dir": direction,
        "days": page.days,
        "valid_from": page.valid_from,
        "valid_to": page.valid_to,
        "stops": stops,
        "notes": [{"text": n.text, "from": n.valid_from, "to": n.valid_to} for n in page.notes],
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
    }


def pick_note(notes: list[dict], day: str) -> dict | None:
    """Нотатка, чий період дії покриває дату; інакше остання нотатка."""
    for n in notes:
        lo, hi = n.get("from") or "0000-00-00", n.get("to") or "9999-99-99"
        if lo <= day <= hi:
            return n
    return notes[-1] if notes else None


def is_fresh(entry: dict | None, now: datetime) -> bool:
    if not entry or not entry.get("fetched_at") or not entry.get("stops"):
        return False
    try:
        fetched = datetime.fromisoformat(entry["fetched_at"])
    except ValueError:
        return False
    return (now - fetched) < timedelta(days=CACHE_TTL_DAYS)


def needs_train_page(row: uz.TrainRow, cache: dict, now: datetime, refresh_cache: bool = False) -> bool:
    """Чи треба тягнути сторінку поїзда, чи вистачить кешу.

    Сторінка потрібна, якщо кеш старий або порожній, або якщо в переліку стоїть червоний
    лічильник повідомлень: тоді на сторінці є свіжий блок «Зміни руху».
    """
    if refresh_cache or row.has_notes:
        return True
    return not is_fresh(cache.get(row.tid), now)


def build(client, today: date, horizon: int, cache: dict, refresh_cache: bool) -> dict:
    now = now_kyiv()
    errors: list[str] = []
    dates = [(today + timedelta(days=i)).isoformat() for i in range(horizon)]
    kh, mer = uz.KHARKIV_SID, uz.MEREFA_SID

    # 1. Повний перелік в обидва боки
    all_rows = client.pair_list(kh, mer) + client.pair_list(mer, kh)
    rows_by_tid: dict[str, uz.TrainRow] = {}
    for r in all_rows:
        rows_by_tid.setdefault(r.tid, r)
    if not rows_by_tid:
        raise uz.UZError("повний перелік поїздів порожній: сайт УЗ віддав порожню таблицю")
    log.info("Повний перелік: %d поїздів", len(rows_by_tid))

    # 2. Перелік на кожну дату.
    # Якщо хоч один напрямок не завантажився, дата вважається неперевіреною і в days не
    # потрапляє: інакше збій мережі виглядав би як скасування всіх поїздів на цю дату.
    days: dict[str, dict] = {}
    skipped: list[str] = []
    for ds in dates:
        running: list[str] = []
        failed = False
        for a, b in ((kh, mer), (mer, kh)):
            try:
                for r in client.pair_list(a, b, ds):
                    if r.tid not in running:
                        running.append(r.tid)
                    if r.tid in rows_by_tid:
                        rows_by_tid[r.tid].has_notes |= r.has_notes
                    else:
                        rows_by_tid[r.tid] = r
            except uz.UZError as e:
                failed = True
                errors.append(f"{ds} {a}->{b}: {e}")
                log.error("%s", errors[-1])
        if failed or not running:
            skipped.append(ds)
            log.warning("%s: дату пропущено (помилка завантаження), скасування не визначаються", ds)
            continue
        days[ds] = {"running": running}
        log.info("%s: курсує %d", ds, len(running))
    if not days:
        raise uz.UZError("на жодну дату не отримано переліку поїздів")
    if len(skipped) > len(dates) // 2:
        raise uz.UZError(f"завантажено лише {len(days)} дат із {len(dates)}: схоже на збій мережі")

    # 3. Сторінки поїздів (маршрут + зміни руху)
    trains: dict[str, dict] = {}
    for tid, row in rows_by_tid.items():
        cached = cache.get(tid)
        if not needs_train_page(row, cache, now, refresh_cache):
            entry = dict(cached)
            entry["notes"] = []           # лічильник повідомлень = 0
        else:
            try:
                page = client.train_page(tid)
                entry = page_to_entry(page, now)
                if not entry["num"]:
                    entry["num"] = row.num
                if not entry["route"]:
                    entry["route"] = row.route
                cache[tid] = entry
            except uz.UZError as e:
                errors.append(f"tid={tid} ({row.num}): {e}")
                log.error("%s", errors[-1])
                if cached:
                    entry = dict(cached)
                    entry["notes"] = cached.get("notes", []) if row.has_notes else []
                else:
                    continue
        entry.setdefault("num", row.num)
        entry.setdefault("route", row.route)
        entry["days"] = entry.get("days") or row.days
        # термін дії з переліку точніший за сторінку поїзда (там теж є, але хай буде єдине джерело)
        entry["valid_from"] = row.valid_from or entry.get("valid_from")
        entry["valid_to"] = row.valid_to or entry.get("valid_to")
        trains[tid] = entry

    # 4. Скасовані / нечинні на дату
    for ds, day in days.items():
        running = set(day["running"])
        cancelled: list[dict] = []
        off: list[dict] = []
        for tid, t in trains.items():
            if tid in running:
                continue
            lo, hi = t.get("valid_from") or "0000-00-00", t.get("valid_to") or "9999-99-99"
            if not (lo <= ds <= hi):
                continue
            note = pick_note(t.get("notes", []), ds)
            if (t.get("days") or "щоденно") == "щоденно":
                cancelled.append({"tid": tid, "note": note["text"] if note else "Скасовано (за даними УЗ на цю дату не курсує)"})
            else:
                off.append({"tid": tid, "note": f"Не курсує в цей день ({t['days']})"})
        day["cancelled"] = cancelled
        day["off"] = off

    # 5. Прибирання кешу
    for tid in list(cache):
        if tid in trains:
            continue
        try:
            fetched = datetime.fromisoformat(cache[tid].get("fetched_at", ""))
        except ValueError:
            fetched = now - timedelta(days=999)
        if now - fetched > timedelta(days=CACHE_KEEP_DAYS):
            del cache[tid]

    public_trains = {}
    for tid, t in trains.items():
        public_trains[tid] = {k: v for k, v in t.items() if k != "fetched_at"}

    return {
        "version": SCHEMA_VERSION,
        "generated": now.isoformat(timespec="seconds"),
        "source": uz.BASE_URL,
        "horizon": {"from": dates[0], "to": dates[-1]},
        "skipped_days": skipped,
        "stations": uz.STATIONS,
        "trains": public_trains,
        "days": days,
        "stats": {
            "requests": client.requests_made,
            "skipped_days": len(skipped),
            "busy_hits": getattr(client, "busy_hits", 0),
            "trains": len(public_trains),
            "route": getattr(client, "route", None) or "напряму",
            "errors": errors,
        },
    }


def merge_schedule(old: dict | None, fresh: dict) -> dict:
    """Кнопка збирає лише найближчі дні, тому решту днів беремо з попереднього розкладу.

    Повні 14 днів збирає GitHub Actions; тут важливо не загубити вже відомі дати.
    """
    if not old or not old.get("days"):
        return fresh
    merged = dict(fresh)
    merged["trains"] = {**old.get("trains", {}), **fresh.get("trains", {})}
    merged["days"] = {**old.get("days", {}), **fresh.get("days", {})}
    for ds in fresh.get("skipped_days", []):
        merged["days"].pop(ds, None)          # дата не зібралась: краще без неї, ніж зі старою
    horizon_to = max(old.get("horizon", {}).get("to", ""), fresh["horizon"]["to"])
    merged["horizon"] = {"from": fresh["horizon"]["from"], "to": horizon_to}
    merged["days"] = {k: v for k, v in sorted(merged["days"].items())
                      if k >= fresh["horizon"]["from"]}
    return merged


def schedule_generated(path: str) -> datetime | None:
    data = load_json(path, None)
    if not data or not data.get("generated"):
        return None
    try:
        gen = datetime.fromisoformat(data["generated"])
    except ValueError:
        return None
    return gen.replace(tzinfo=KYIV) if gen.tzinfo is None else gen


def schedule_age_hours(path: str) -> float | None:
    gen = schedule_generated(path)
    return None if gen is None else (now_kyiv() - gen).total_seconds() / 3600


def last_slot(now: datetime, hours: list[int]) -> datetime:
    """Останній слот оновлення, що вже настав (наприклад 06:00 або 12:00 за Києвом)."""
    today_slots = sorted(now.replace(hour=h, minute=0, second=0, microsecond=0) for h in hours)
    passed = [s for s in today_slots if s <= now]
    return passed[-1] if passed else today_slots[-1] - timedelta(days=1)


def due_by_slots(path: str, now: datetime, hours: list[int]) -> tuple[bool, str]:
    """Чи час оновлювати розклад: так, якщо файл старіший за останній слот.

    Стійке і до пропущених запусків cron (наздожене наступного разу), і до переходу
    на літній час, бо слоти рахуються у київському часі.
    """
    slot = last_slot(now, hours)
    gen = schedule_generated(path)
    if gen is None:
        return True, "schedule.json відсутній"
    if gen < slot:
        return True, f"останнє оновлення {gen:%d.%m %H:%M} < слот {slot:%d.%m %H:%M}"
    return False, f"слот {slot:%d.%m %H:%M} вже відпрацьовано ({gen:%d.%m %H:%M})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horizon", type=int, default=HORIZON_DAYS, help="скільки днів уперед (типово %(default)s)")
    ap.add_argument("--max-age-hours", type=float, default=None,
                    help="пропустити запуск, якщо schedule.json молодший за N годин")
    ap.add_argument("--slots", default=None,
                    help="години оновлення за київським часом через кому, напр. 6,12: "
                         "запуск лише якщо schedule.json старіший за останній слот")
    ap.add_argument("--force", action="store_true", help="ігнорувати --slots і --max-age-hours")
    ap.add_argument("--check-only", action="store_true",
                    help="нічого не збирати: код виходу 0 якщо час оновлювати, 3 якщо ні")
    ap.add_argument("--refresh-cache", action="store_true", help="перезавантажити сторінки всіх поїздів")
    ap.add_argument("--today", help="дата «сьогодні» у форматі YYYY-MM-DD (для тестів)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.check_only:
        hours = [int(h) for h in (args.slots or "6,10,13").split(",") if h.strip()]
        due, why = due_by_slots(SCHEDULE_FILE, now_kyiv(), hours)
        log.info("%s: %s", "Пора оновлювати" if due else "Ще не час", why)
        return 0 if (due or args.force) else 3

    if not args.force:
        if args.slots:
            hours = [int(h) for h in args.slots.split(",") if h.strip()]
            due, why = due_by_slots(SCHEDULE_FILE, now_kyiv(), hours)
            if not due:
                log.info("Пропускаю: %s", why)
                return 0
            log.info("Оновлюю: %s", why)
        elif args.max_age_hours is not None:
            age = schedule_age_hours(SCHEDULE_FILE)
            if age is not None and age < args.max_age_hours:
                log.info("schedule.json оновлено %.1f год тому (< %.1f): пропускаю", age, args.max_age_hours)
                return 0

    today = date.fromisoformat(args.today) if args.today else now_kyiv().date()
    cache = load_json(CACHE_FILE, {})
    client = uz.Client()
    log.info("Маршрути до сайту УЗ: %s", ", ".join(r or "напряму" for r in client.routes))
    log.info("Горизонт: %d днів", args.horizon)
    try:
        schedule = build(client, today, args.horizon, cache, args.refresh_cache)
    except uz.UZError as e:
        log.error("Збірка не вдалася, schedule.json не змінено: %s", e)
        return 2

    save_json(CACHE_FILE, cache)
    save_json(SCHEDULE_FILE, schedule)
    st = schedule["stats"]
    log.info("Готово: %d поїздів, %d дат, %d запитів, помилок: %d",
             st["trains"], len(schedule["days"]), st["requests"], len(st["errors"]))
    for ds, day in schedule["days"].items():
        if day["cancelled"]:
            log.info("  %s скасовано: %s", ds, ", ".join(schedule["trains"][c["tid"]]["num"] for c in day["cancelled"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
