# -*- coding: utf-8 -*-
"""Збирач розкладу: маршрути, які підключаються до бота (bot.py) або працюють окремо.

Сайт swrailway.gov.ua відкидає з'єднання з великих хмар (Amazon, Microsoft, Google),
тому Render і GitHub Actions до нього не дістають навіть із Франкфурта. Запити йдуть
через Cloudflare Worker (worker.js), адреса якого задається змінною UZ_GATEWAYS.

Маршрути:
    GET  /whoami           діагностика: мережа сервісу і чи бачить він сайт УЗ
    GET  /fetch?url=...    проксі однієї сторінки УЗ (запасний шлях для GitHub Actions)
    POST /refresh          зібрати розклад самотужки і закомітити в GitHub (повільний шлях)
    POST /fast/start       швидкий шлях: видати перелік сторінок для завантаження
    POST /fast/pages       прийняти пачку сторінок у JSON (так робить Mini App)
    POST /fast/page        прийняти одну сторінку сирим тілом (так робить Cloudflare Worker)
    GET  /fast/state       чого сесії бракує і завдання наступної фази
    POST /fast/finish      зібрати розклад із прийнятих сторінок і закомітити
    GET  /due              чи час оновлювати розклад (слоти 06:00 і 13:00 за Києвом)
    POST /live             прочитати канал УЗ і оновити live.json (затримки, скасування)
    GET  /status           стан останньої збірки
    GET  /schedule.json    свіжозібраний розклад з пам'яті, без очікування GitHub Pages

Швидкий шлях існує тому, що сайт УЗ обмежує частоту запитів за адресою відправника:
з мережі Render серія запитів отримує 522 і збірка триває більше десяти хвилин, а з
телефона користувача ті самі сторінки віддаються за частки секунди (див. fastbuild.py).

Змінні оточення:
    GH_TOKEN      токен GitHub з правом Contents: Read and write (інакше збірка не комітиться)
    GH_REPO       stanislavperec-ua/merefa-rozklad (значення за умовчанням)
    UZ_GATEWAYS   https://<worker>.workers.dev/fetch?url={url}
    GATEWAY_TOKEN довільний рядок, захищає /fetch (необов'язково)
"""
from __future__ import annotations

import base64
import gzip
import hmac
import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timedelta

import requests
from urllib.parse import quote
from flask import Blueprint, Flask, Response, abort, jsonify, request

import build_live
import build_schedule
import fastbuild
import uz

log = logging.getLogger("gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Маршрути оформлені як Blueprint, щоб їх міг підключити і бот (bot.py), і окремий сервіс
gateway_bp = Blueprint("gateway", __name__)

ALLOWED_HOSTS = {"swrailway.gov.ua", "www.swrailway.gov.ua"}
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO = os.environ.get("GH_REPO", "stanislavperec-ua/merefa-rozklad")
GH_API = "https://api.github.com"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")          # ним перевіряється підпис Telegram
FAST_TOKEN = os.environ.get("FAST_TOKEN", "")        # спільний секрет із Cloudflare Worker
SLOT_HOURS = [int(h) for h in os.environ.get("SLOTS", "6,13").split(",") if h.strip()]
HORIZON = int(os.environ.get("HORIZON_DAYS", "4"))   # повільний шлях бере найближчі дні:
# сайт УЗ з мережі Render відповідає неохоче, тому там важлива швидкість відповіді
FAST_HORIZON = int(os.environ.get("FAST_HORIZON_DAYS", "14"))  # кнопка: найближчі два тижні
# Автоматика (Cloudflare Worker за розкладом) бере місяць: УЗ оголошує скасування і зміни
# графіка заздалегідь, і чим далі видно, тим більше таких змін застосунок покаже одразу.
CRON_HORIZON = int(os.environ.get("CRON_HORIZON_DAYS", "30"))
MIN_INTERVAL = timedelta(minutes=int(os.environ.get("MIN_REFRESH_MINUTES", "5")))
MAX_BODY = 12 * 1024 * 1024                          # більше сторінки розкладу не важать
TIMEOUT = (15, 60)
USER_AGENT = uz.USER_AGENT

KYIV = uz.kyiv_tz()

# Стан останньої збірки (у пам'яті процесу)
state: dict = {
    "running": False,
    "started": None,
    "finished": None,
    "ok": None,
    "message": "ще не запускалась",
    "generated": None,
    "committed": False,
    "requests": 0,
    "horizon": None,
}
state_lock = threading.RLock()   # реентерабельний: public_state() викликається і зсередини блоків
latest_schedule: dict | None = None
fast_sessions = fastbuild.SessionStore()


def cors(resp: Response) -> Response:
    """Mini App відкривається з GitHub Pages, а в Telegram WebView origin буває null."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Gzip"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def body_json() -> dict:
    """Тіло запиту, за потреби розпаковане: телефон стискає сторінки, щоб не гнати зайве."""
    if request.content_length and request.content_length > MAX_BODY:
        abort(413)
    raw = request.get_data(cache=False)
    if request.headers.get("X-Gzip") == "1":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError) as e:
            raise fastbuild.FastError(f"не вдалося розпакувати тіло запиту: {e}") from e
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise fastbuild.FastError(f"тіло запиту не є JSON: {e}") from e
    if not isinstance(data, dict):
        raise fastbuild.FastError("очікую об'єкт JSON")
    return data


@gateway_bp.route("/whoami")
def whoami():
    """Діагностика: звідки сервіс виходить у мережу і чи бачить сайт УЗ.

    Сайт відповідає лише європейським мережам, тому сервіс має стояти в регіоні Frankfurt.
    """
    info: dict = {"region_env": os.environ.get("RENDER_REGION", "невідомо"),
                  "telegram_check": bool(BOT_TOKEN),   # чи зможемо перевірити підпис Mini App
                  "github_token": bool(GH_TOKEN),
                  "worker_token": bool(FAST_TOKEN),    # чи впізнаємо Cloudflare Worker
                  "slots": SLOT_HOURS,
                  "fast_sessions": len(fast_sessions),
                  "live": {k: live_state.get(k) for k in ("finished", "ok", "items", "message")},
                  "poke": poke_state}
    try:
        r = requests.get("https://ipinfo.io/json", timeout=(10, 20))
        data = r.json()
        info["ip"] = data.get("ip")
        info["country"] = data.get("country")
        info["city"] = data.get("city")
        info["org"] = data.get("org")
    except Exception as e:  # noqa: BLE001
        info["ip_error"] = str(e)[:160]
    started = time.time()
    try:
        r = requests.get(uz.BASE_URL, params={"sid1": uz.KHARKIV_SID, "sid2": uz.MEREFA_SID, "dateR": 0},
                         headers={"User-Agent": USER_AGENT}, timeout=(15, 40))
        info["uz_status"] = r.status_code
        info["uz_trains"] = len(uz.parse_pair_list(r.text))
    except Exception as e:  # noqa: BLE001
        info["uz_error"] = str(e)[:200]
    info["uz_seconds"] = round(time.time() - started, 1)

    # Через Cloudflare Worker: важливо знати, в якому дата-центрі він виконується,
    # бо сайт УЗ пускає лише частину з них (заголовок cf-ray закінчується кодом колокації).
    for gw in uz.default_gateways()[:1]:
        target = uz.BASE_URL + "?sid1=2528&sid2=2538&dateR=0"
        url = gw.replace("{url}", quote(target, safe=""))
        t0 = time.time()
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=(15, 40))
            info["worker_status"] = r.status_code
            info["worker_colo"] = (r.headers.get("cf-ray") or "").split("-")[-1]
            info["worker_trains"] = len(uz.parse_pair_list(r.text)) if r.ok else 0
        except Exception as e:  # noqa: BLE001
            info["worker_error"] = str(e)[:160]
        info["worker_seconds"] = round(time.time() - t0, 1)
    return cors(jsonify(**info))


@gateway_bp.route("/fetch")
def fetch():
    if GATEWAY_TOKEN and request.args.get("token") != GATEWAY_TOKEN:
        abort(403)
    url = request.args.get("url", "")
    if not url.startswith("https://"):
        abort(400)
    host = url.split("/")[2].split(":")[0].lower()
    if host not in ALLOWED_HOSTS:
        log.warning("Відхилено хост %s", host)
        abort(403)
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    except requests.RequestException as e:
        log.error("Помилка запиту до %s: %s", host, e)
        return f"gateway error: {e}", 502
    return Response(r.content, status=r.status_code,
                    content_type=r.headers.get("Content-Type", "text/html; charset=utf-8"))


# ──────────────────────────────────────────────────────────────────────
# GitHub
# ──────────────────────────────────────────────────────────────────────
def gh_headers() -> dict:
    """Без токена читання публічного репозиторію теж працює, тому заголовок лише за наявності."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"
    return headers


def gh_get_file(path: str) -> tuple[dict | None, str | None]:
    """Повертає (вміст як dict, sha) або (None, None), якщо файла немає."""
    r = requests.get(f"{GH_API}/repos/{GH_REPO}/contents/{path}", headers=gh_headers(), timeout=TIMEOUT)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    raw = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(raw), data["sha"]


def gh_put_file(path: str, payload: dict, sha: str | None, message: str) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    req = {"message": message,
           "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
           "committer": {"name": "merefa-gateway", "email": "noreply@users.noreply.github.com"}}
    if sha:
        req["sha"] = sha
    r = requests.put(f"{GH_API}/repos/{GH_REPO}/contents/{path}", headers=gh_headers(), json=req, timeout=TIMEOUT)
    r.raise_for_status()


def strip_volatile(schedule: dict) -> str:
    """Порівнюємо без полів, що змінюються щоразу (час генерації, лічильник запитів)."""
    copy = {k: v for k, v in schedule.items() if k not in ("generated", "stats")}
    return json.dumps(copy, ensure_ascii=False, sort_keys=True)


def load_cache() -> tuple[dict, str | None]:
    """Кеш сторінок поїздів із GitHub; без нього збірка теж працює, лише довша."""
    try:
        cache, sha = gh_get_file("trains_cache.json")
        return cache or {}, sha
    except Exception as e:  # noqa: BLE001
        log.warning("Кеш поїздів недоступний, збираю без нього: %s", e)
        return {}, None


def fetch_schedule() -> tuple[dict | None, str | None]:
    """Розклад, що зараз лежить у репозиторії (для злиття і для звірки)."""
    try:
        return gh_get_file("schedule.json")
    except Exception as e:  # noqa: BLE001
        log.warning("Попередній розклад недоступний: %s", e)
        return None, None


def older_than_slot(schedule: dict | None, now: datetime | None = None) -> bool:
    """Чи зібраний розклад старіший за останній слот оновлення (06:00 / 13:00 за Києвом)."""
    now = now or datetime.now(KYIV)
    slot = build_schedule.last_slot(now, SLOT_HOURS)
    if not schedule or not schedule.get("generated"):
        return True
    try:
        generated = datetime.fromisoformat(schedule["generated"])
    except ValueError:
        return True
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=KYIV)
    return generated < slot


def store_schedule(schedule: dict, cache: dict, cache_sha: str | None,
                   commit: bool = True, previous: tuple | None = None) -> tuple[dict, bool, str]:
    """Зливає з попереднім розкладом, кладе в пам'ять і, якщо дозволено, комітить у GitHub."""
    global latest_schedule
    old, sha = previous if previous is not None else (None, None)
    if previous is None and GH_TOKEN:
        old, sha = gh_get_file("schedule.json")
    schedule = build_schedule.merge_schedule(old, schedule)
    latest_schedule = schedule

    if not GH_TOKEN:
        return schedule, False, "розклад зібрано (GH_TOKEN не задано, у GitHub не збережено)"
    if not commit:
        return schedule, False, "розклад зібрано, але не збережено в GitHub"
    # Позначку часу треба оновити навіть тоді, коли розклад не змінився: за нею і воркер,
    # і GitHub Actions розуміють, що слот відпрацьовано. Інакше вони збирали б знову і знову.
    same = old is not None and strip_volatile(old) == strip_volatile(schedule)
    if same and not older_than_slot(old):
        return schedule, False, "розклад не змінився"

    stamp = datetime.now(KYIV).strftime("%Y-%m-%d %H:%M")
    if same:
        gh_put_file("schedule.json", schedule, sha, f"Timetable check (gateway) {stamp}")
        return schedule, True, "розклад не змінився, оновлено позначку часу"
    gh_put_file("schedule.json", schedule, sha, f"Timetable update (gateway) {stamp}")
    if cache:
        try:
            _, cache_sha_now = gh_get_file("trains_cache.json")
            gh_put_file("trains_cache.json", cache, cache_sha_now or cache_sha,
                        f"Trains cache (gateway) {stamp}")
        except Exception as e:  # noqa: BLE001
            log.warning("Кеш не збережено: %s", e)
    return schedule, True, "розклад оновлено і збережено в GitHub"


def finish_state(ok: bool, message: str, schedule: dict | None = None, committed: bool = False) -> None:
    with state_lock:
        state.update(running=False, finished=datetime.now(KYIV).isoformat(timespec="seconds"),
                     ok=ok, message=message, committed=committed,
                     generated=(schedule or {}).get("generated") or state.get("generated"),
                     requests=(schedule or {}).get("stats", {}).get("requests", 0))


def do_refresh() -> None:
    """Збирає розклад і зберігає його в GitHub. Прапорець running уже виставлено у refresh()."""
    try:
        cache, cache_sha = load_cache()
        # Сервіс працює в хмарі, де прямий маршрут завжди впирається в таймаут 15 с
        # на кожному запиті, тому лишаємо тільки Cloudflare Worker.
        client = uz.Client(direct=False)
        horizon = state.get("horizon") or HORIZON
        schedule = build_schedule.build(client, datetime.now(KYIV).date(), horizon, cache, False)
        schedule, committed, msg = store_schedule(schedule, cache, cache_sha)
        finish_state(True, msg, schedule, committed)
        log.info("Оновлення завершено: %s", msg)
    except Exception as e:  # noqa: BLE001
        log.exception("Збірка не вдалася")
        finish_state(False, f"помилка: {str(e)[:200]}")


@gateway_bp.route("/refresh", methods=["POST", "GET", "OPTIONS"])
def refresh():
    if request.method == "OPTIONS":
        return cors(Response("", 204))
    with state_lock:
        if state["running"]:
            return cors(jsonify(status="running", **public_state()))
        wait = 0 if request.args.get("force") else seconds_to_wait()
        if wait:
            return cors(jsonify(status="too_soon", wait_seconds=wait, **public_state()))
        try:
            days = int(request.args.get("days", HORIZON))
        except ValueError:
            days = HORIZON
        state.update(running=True, started=datetime.now(KYIV).isoformat(timespec="seconds"),
                     finished=None, ok=None, horizon=max(1, min(days, fastbuild.MAX_HORIZON)),
                     message=f"збираю розклад з сайту УЗ ({max(1, min(days, fastbuild.MAX_HORIZON))} дн.)")
        payload = public_state()
    threading.Thread(target=do_refresh, daemon=True).start()
    return cors(jsonify(status="started", **payload))


def public_state() -> dict:
    with state_lock:
        return {k: state[k] for k in ("running", "started", "finished", "ok", "message",
                                      "generated", "committed", "requests")}


@gateway_bp.route("/status")
def status():
    return cors(jsonify(**public_state()))


# ──────────────────────────────────────────────────────────────────────
# Оперативні повідомлення каналу УЗ (затримки, скасування)
# ──────────────────────────────────────────────────────────────────────
WORKER_URL = os.environ.get("WORKER_URL", "https://merefa-uz-gateway.stanislav-perec.workers.dev")
POKE_INTERVAL = timedelta(minutes=int(os.environ.get("POKE_WORKER_MINUTES", "30")))
poke_state: dict = {"at": None, "result": None}
LIVE_MIN_INTERVAL = timedelta(minutes=int(os.environ.get("MIN_LIVE_MINUTES", "10")))
LIVE_AUTO_INTERVAL = timedelta(minutes=int(os.environ.get("LIVE_AUTO_MINUTES", "25")))
LIVE_HOURS = float(os.environ.get("LIVE_HOURS", "12"))
LIVE_DAY_FROM, LIVE_DAY_TO = 5, 0        # читаємо канал з 05:00 до 00:59 за Києвом
live_state: dict = {"running": False, "finished": None, "ok": None,
                    "message": "ще не запускалась", "items": 0, "committed": False}
live_lock = threading.Lock()
_nums_cache: dict = {"nums": None, "at": None}


def train_nums() -> set[str]:
    """Номери наших поїздів: потрібні, щоб відібрати з каналу пости саме про них."""
    with live_lock:
        cached, at = _nums_cache["nums"], _nums_cache["at"]
    if cached and at and (datetime.now(KYIV) - at) < timedelta(hours=6):
        return cached
    schedule = latest_schedule or fetch_schedule()[0]
    nums = {t.get("num") for t in (schedule or {}).get("trains", {}).values() if t.get("num")}
    if not nums:
        return set(build_live.FALLBACK_NUMS)
    with live_lock:
        _nums_cache.update(nums=nums, at=datetime.now(KYIV))
    return nums


def do_live() -> dict:
    """Читає канал і за потреби комітить live.json. Повертає стан для відповіді."""
    try:
        fresh = build_live.collect(hours=LIVE_HOURS, nums=train_nums())
    except Exception as e:  # noqa: BLE001
        log.warning("Канал УЗ недоступний: %s", e)
        with live_lock:
            live_state.update(running=False, finished=datetime.now(KYIV).isoformat(timespec="seconds"),
                              ok=False, message=f"канал недоступний: {str(e)[:120]}")
            return dict(live_state)

    committed, message = False, "канал прочитано"
    if GH_TOKEN:
        try:
            old, sha = gh_get_file("live.json")
        except Exception as e:  # noqa: BLE001
            log.warning("live.json з GitHub недоступний: %s", e)
            old, sha = None, None
        changed = not old or old.get("items") != fresh["items"]
        # навіть без змін раз на кілька годин оновлюємо позначку часу, щоб у застосунку
        # не здавалося, що дані застигли
        stale = True
        if old and old.get("generated"):
            try:
                gen = datetime.fromisoformat(old["generated"])
                stale = (datetime.now(KYIV) - gen) > timedelta(hours=build_live.REWRITE_AFTER_HOURS)
            except ValueError:
                stale = True
        if changed or stale:
            stamp = datetime.now(KYIV).strftime("%Y-%m-%d %H:%M")
            gh_put_file("live.json", fresh, sha, f"Live update (gateway) {stamp}")
            committed = True
            message = "оновлено" if changed else "змін немає, оновлено позначку часу"
        else:
            message = "змін немає"
    with live_lock:
        live_state.update(running=False, finished=datetime.now(KYIV).isoformat(timespec="seconds"),
                          ok=True, message=message, items=len(fresh["items"]), committed=committed)
        state_copy = dict(live_state)
    log.info("Канал УЗ: %s, повідомлень про наші поїзди %d", message, len(fresh["items"]))
    return state_copy


def live_due(interval: timedelta, now: datetime | None = None) -> bool:
    """Чи час читати канал: не частіше заданого інтервалу і не серед ночі."""
    now = now or datetime.now(KYIV)
    if not (LIVE_DAY_FROM <= now.hour or now.hour <= LIVE_DAY_TO):
        return False
    with live_lock:
        if live_state.get("running"):
            return False
        last = live_state.get("finished")
    if not last:
        return True
    try:
        return (now - datetime.fromisoformat(last)) >= interval
    except ValueError:
        return True


def maybe_collect_live() -> None:
    """Будильник із health-check: UptimeRobot стукає в бота кожні 5 хвилин.

    Це найнадійніший годинник, який у нас є: cron GitHub Actions пропускає запуски, а
    Cloudflare Cron Trigger виконується коли і де вирішить сам. Пінг же приходить завжди,
    тож раз на LIVE_AUTO_MINUTES він і запускає читання каналу у фоні.
    """
    if not live_due(LIVE_AUTO_INTERVAL):
        return
    with live_lock:
        if live_state.get("running"):
            return
        live_state["running"] = True
    threading.Thread(target=do_live, daemon=True).start()


def poke_worker() -> None:
    """Просить Cloudflare Worker перевірити слот розкладу і, якщо час, зібрати його.

    Воркер має власний Cron Trigger, але покладатися лише на нього не можна: після зміни
    розкладу він мовчав (перевірено 16.09.2026 о 06:30 і 07:00). Пінг UptimeRobot приходить
    завжди, тож бот сам нагадує воркеру. `via=do` обов'язковий: інакше воркер виконався б
    поруч із ботом, у США, звідки сайт УЗ майже не відповідає.
    """
    try:
        r = requests.post(f"{WORKER_URL}/run?via=do", headers={"X-Fast-Token": FAST_TOKEN},
                          timeout=(15, 600))
        result = r.json() if r.ok else {"http": r.status_code}
    except Exception as e:  # noqa: BLE001
        result = {"error": str(e)[:160]}
    poke_state.update(at=datetime.now(KYIV).isoformat(timespec="seconds"), result=result)
    log.info("Нагадав воркеру про розклад: %s", str(result)[:200])
    if not result.get("ok"):
        fallback_refresh()


def fallback_refresh() -> None:
    """Воркер не впорався: збираємо найближчі дні самотужки.

    Повільно (сайт УЗ обмежує мережу Render), тому беремо лише HORIZON днів, а решту
    дат зберігає merge_schedule. Це остання лінія оборони, коли не працює ні Cloudflare
    Cron, ні GitHub Actions.
    """
    with state_lock:
        if state["running"]:
            return
    old, _ = fetch_schedule()
    if not older_than_slot(old):
        return
    log.warning("Воркер не зібрав розклад, запускаю давній шлях на %d дн.", HORIZON)
    with state_lock:
        state.update(running=True, started=datetime.now(KYIV).isoformat(timespec="seconds"),
                     finished=None, ok=None, horizon=HORIZON,
                     message=f"запасна збірка своїми силами ({HORIZON} дн.)")
    threading.Thread(target=do_refresh, daemon=True).start()


def maybe_poke_worker() -> None:
    """Раз на POKE_INTERVAL нагадуємо воркеру; сам воркер вирішує, чи настав слот."""
    if not FAST_TOKEN:
        return
    now = datetime.now(KYIV)
    if not (LIVE_DAY_FROM <= now.hour or now.hour <= LIVE_DAY_TO):
        return
    last = poke_state.get("at")
    if last:
        try:
            if (now - datetime.fromisoformat(last)) < POKE_INTERVAL:
                return
        except ValueError:
            pass
    poke_state["at"] = now.isoformat(timespec="seconds")   # позначаємо одразу, щоб не задвоїти
    threading.Thread(target=poke_worker, daemon=True).start()


@gateway_bp.route("/live", methods=["POST", "GET", "OPTIONS"])
def live():
    """Збирає канал УЗ і оновлює live.json.

    Раніше це робив GitHub Actions щопівгодини, але його cron на безкоштовному тарифі
    пропускає запуски (16.09.2026 не спрацював сім годин поспіль), а затримки поїздів
    цінні саме свіжими. Тепер бота будить Cloudflare Worker, а канал бот читає сам:
    Telegram, на відміну від сайту УЗ, доступний з мережі Render.
    """
    if request.method == "OPTIONS":
        return cors(Response("", 204))
    force = bool(request.args.get("force"))
    with live_lock:
        last = live_state.get("finished")
        busy = live_state.get("running")
    if busy:
        return cors(jsonify(status="running", **live_state))
    if last and not force:
        try:
            since = datetime.now(KYIV) - datetime.fromisoformat(last)
            if since < LIVE_MIN_INTERVAL:
                return cors(jsonify(status="too_soon",
                                    wait_seconds=int((LIVE_MIN_INTERVAL - since).total_seconds()),
                                    **live_state))
        except ValueError:
            pass
    with live_lock:
        live_state["running"] = True
    result = do_live()
    return cors(jsonify(status="ok" if result.get("ok") else "error", **result))


# ──────────────────────────────────────────────────────────────────────
# Швидкий шлях: сторінки завантажує телефон користувача
# ──────────────────────────────────────────────────────────────────────
def fail(message: str, code: int = 400, **extra) -> Response:
    resp = jsonify(error=message, **extra)
    resp.status_code = code
    return cors(resp)


@gateway_bp.errorhandler(fastbuild.FastError)
def on_fast_error(e: fastbuild.FastError):
    return fail(str(e), 400)


def seconds_to_wait() -> int:
    """Скільки лишилось до наступного дозволеного оновлення (0, якщо вже можна)."""
    last = state.get("finished")
    if not last:
        return 0
    try:
        since = datetime.now(KYIV) - datetime.fromisoformat(last)
    except ValueError:
        return 0
    return max(0, int((MIN_INTERVAL - since).total_seconds()))


SPOT_WAIT = 25          # скільки чекати на власну звірку, якщо вона ще не встигла


def start_spot_check(session: fastbuild.Session) -> None:
    """Тягне одну дату з сайту УЗ, поки телефон завантажує сторінки.

    Потрібно клієнту без підпису Telegram (звичайний браузер): сторінки міг би прислати
    будь-хто, хто знає адресу сервісу, тому результат треба звірити з джерелом. Запит
    іде одразу після /fast/start, щоб до кінця збірки відповідь уже була.
    """
    days = fastbuild.horizon_dates(session.today, session.horizon)
    day = random.choice(days[:7])       # найближчий тиждень: саме він цікавить користувача

    def work() -> None:
        try:
            rows = uz.Client(direct=False, retries=2).pair_list(uz.KHARKIV_SID, uz.MEREFA_SID, day)
            session.check = (day, {r.tid for r in rows}, None)
        except Exception as e:  # noqa: BLE001
            session.check = (day, None, str(e)[:120])
        finally:
            session.check_ready.set()

    threading.Thread(target=work, daemon=True).start()


def spot_check(session: fastbuild.Session, schedule: dict) -> tuple[bool | None, str]:
    """Звіряє зібраний розклад із тим, що сервіс бачить на сайті сам.

    Сайт із мережі Render відповідає не завжди, тож невдала звірка не означає підробку:
    у такому разі рішення ухвалює sanity_check.
    """
    session.check_ready.wait(SPOT_WAIT)
    if not session.check:
        return None, "звірка не встигла"
    day, theirs, error = session.check
    if error:
        return None, f"сайт УЗ не відповів сервісу ({error})"
    if not theirs:
        return None, f"{day}: сайт віддав порожній перелік"
    if day not in (schedule.get("days") or {}):
        return None, f"{day}: цієї дати немає в зібраному розкладі"
    missing = theirs - fastbuild.day_tids(schedule, day)
    if missing:
        return False, f"{day}: серед присланих даних немає поїздів {sorted(missing)[:5]}"
    return True, f"{day}: збіглося, {len(theirs)} поїздів"


def sanity_check(old: dict | None, fresh: dict) -> tuple[bool, str]:
    """Груба перевірка правдоподібності, коли звірка з сайтом не вдалася.

    Розклад не може раптово втратити більшість поїздів: якщо втратив, це або підробка,
    або зіпсовані дані, і зберігати таке в GitHub не варто.
    """
    if not old or not old.get("trains"):
        return True, "порівнювати нема з чим"
    was, now = len(old["trains"]), len(fresh.get("trains") or {})
    if now < was * 0.7:
        return False, f"поїздів стало {now} замість {was}"
    for day, info in (fresh.get("days") or {}).items():
        before = (old.get("days") or {}).get(day)
        if not before:
            continue
        if len(info.get("running") or []) < len(before.get("running") or []) * 0.7:
            return False, f"{day}: курсує {len(info.get('running') or [])} замість {len(before['running'])}"
    return True, "зміни в межах звичайного"


def from_worker() -> bool:
    """Запит від Cloudflare Worker: він сам качає сторінки з сайту, тож дані достовірні."""
    given = request.headers.get("X-Fast-Token", "")
    return bool(FAST_TOKEN) and hmac.compare_digest(given.encode("utf-8"), FAST_TOKEN.encode("utf-8"))


@gateway_bp.route("/due")
def due():
    """Чи час оновлювати розклад. Питає Cloudflare Worker, який ходить за розкладом сам.

    Рішення ухвалюється за київським часом і за тим, що зараз лежить у репозиторії, тому
    воно не залежить ні від годинника воркера, ні від того, чи бот перезапускався.
    """
    now = datetime.now(KYIV)
    slot = build_schedule.last_slot(now, SLOT_HOURS)
    old, _ = fetch_schedule()
    need = older_than_slot(old, now)
    return cors(jsonify(due=need, slot=slot.isoformat(timespec="minutes"),
                        generated=(old or {}).get("generated"),
                        reason=("останнє оновлення старіше за слот" if need else "слот уже відпрацьовано")))


@gateway_bp.route("/fast/page", methods=["POST", "OPTIONS"])
def fast_page():
    """Одна сторінка сирим тілом: так її може переслати воркер, не читаючи в пам'ять.

    Безкоштовний план Cloudflare дає воркеру лише 10 мс процесорного часу на виклик,
    тож усе, що можна, він робить потоком: качає сторінку і одразу ллє її сюди.
    """
    if request.method == "OPTIONS":
        return cors(Response("", 204))
    session = fast_sessions.get(request.args.get("session", ""))
    page_id = request.args.get("id", "")
    if request.content_length and request.content_length > MAX_BODY:
        abort(413)
    raw = request.get_data(cache=False)
    if request.headers.get("X-Gzip") == "1":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError) as e:
            return fail(f"не вдалося розпакувати сторінку: {e}")
    try:
        session.submit(page_id, raw.decode("utf-8", "replace"))
    except (fastbuild.FastError, uz.UZError, ValueError, TypeError) as e:
        log.warning("Сесія %s: сторінку %s відхилено: %s", session.id, page_id, str(e)[:160])
        return fail(str(e)[:160])
    session.advance()
    with session.lock:
        return cors(jsonify(status="ok", pending=len(session.pending), phase=session.phase))


@gateway_bp.route("/fast/state")
def fast_state():
    """Що сесії ще бракує і які завдання наступної фази."""
    session = fast_sessions.get(request.args.get("session", ""))
    session.advance()
    return cors(jsonify(status="ok", **session.state()))


@gateway_bp.route("/fast/start", methods=["POST", "OPTIONS"])
def fast_start():
    if request.method == "OPTIONS":
        return cors(Response("", 204))
    data = body_json()
    with state_lock:
        if state["running"]:
            return cors(jsonify(status="running", **public_state()))
    wait = 0 if data.get("force") else seconds_to_wait()
    if wait:
        return cors(jsonify(status="too_soon", wait_seconds=wait, **public_state()))
    worker = from_worker()
    try:
        days = int(data.get("days") or FAST_HORIZON)
    except (TypeError, ValueError):
        days = FAST_HORIZON
    if worker:
        days = max(days, CRON_HORIZON)     # кнопка лишається швидкою, автоматика бере місяць
    telegram = bool(fastbuild.check_init_data(str(data.get("initData") or ""), BOT_TOKEN))
    trusted = worker or telegram          # ці двоє качають сторінки самі, звірка їм не потрібна
    cache, cache_sha = load_cache()
    now = datetime.now(KYIV)
    session = fastbuild.Session(today=now.date(), horizon=days, cache=cache,
                                trusted=trusted, now=now)
    session.cache_sha = cache_sha
    fast_sessions.add(session)
    if not trusted:
        start_spot_check(session)      # звірка йде паралельно, щоб не чекати на неї в кінці
    source = "Cloudflare Worker" if worker else ("Mini App у Telegram" if telegram else "браузер")
    if worker and data.get("from"):
        source += f" ({data['from']})"      # дата-центр воркера: від нього залежить, чи пустить сайт
    log.info("Швидке оновлення %s: %d днів, %d сторінок, джерело: %s",
             session.id, session.horizon, len(session.tasks), source)
    return cors(jsonify(status="started", trusted=trusted, horizon=session.horizon,
                        **session.state()))


@gateway_bp.route("/fast/pages", methods=["POST", "OPTIONS"])
def fast_pages():
    if request.method == "OPTIONS":
        return cors(Response("", 204))
    data = body_json()
    session = fast_sessions.get(str(data.get("session") or ""))
    pages = data.get("pages") or []
    if not isinstance(pages, list) or len(pages) > fastbuild.MAX_PAGES_PER_BATCH:
        return fail(f"за раз приймаю не більше {fastbuild.MAX_PAGES_PER_BATCH} сторінок")
    rejected = []
    for page in pages:
        page_id = str((page or {}).get("id", ""))
        try:
            session.submit(page_id, (page or {}).get("html"))
        except (fastbuild.FastError, uz.UZError, ValueError, TypeError) as e:
            log.warning("Сесія %s: сторінку %s відхилено: %s", session.id, page_id, str(e)[:160])
            rejected.append({"id": page_id, "error": str(e)[:160]})
    lost = [str(i) for i in (data.get("failed") or []) if isinstance(i, str)]
    if lost:
        session.give_up(lost)
    session.advance()
    return cors(jsonify(status="ok", rejected=rejected, **session.state()))


def do_fast_finish(session: fastbuild.Session) -> None:
    """Збирає розклад із уже розібраних сторінок. Прапорець running виставлено у fast_finish()."""
    try:
        schedule = session.build()
        previous = fetch_schedule() if GH_TOKEN else (None, None)
        commit, note = True, ""
        if not session.trusted:
            verdict, why = spot_check(session, schedule)
            if verdict is False:
                raise fastbuild.FastError(f"дані не збіглися з сайтом УЗ ({why})")
            if verdict is None:
                # сайт не відповів сервісу: лишається перевірити дані на правдоподібність
                commit, reason = sanity_check(previous[0], schedule)
                note = f"; звірка з сайтом не вийшла ({why}), перевірка даних: {reason}"
                log.warning("Сесія %s: звірка неможлива (%s), правдоподібність: %s", session.id, why, reason)
            else:
                log.info("Сесія %s: контрольна звірка пройдена, %s", session.id, why)
        schedule, committed, msg = store_schedule(schedule, session.cache,
                                                  getattr(session, "cache_sha", None), commit, previous)
        finish_state(True, msg + note, schedule, committed)
        log.info("Швидке оновлення %s завершено: %s", session.id, msg)
    except Exception as e:  # noqa: BLE001
        log.exception("Швидка збірка не вдалася")
        finish_state(False, f"помилка: {str(e)[:200]}")
    finally:
        fast_sessions.drop(session.id)


@gateway_bp.route("/fast/finish", methods=["POST", "OPTIONS"])
def fast_finish():
    if request.method == "OPTIONS":
        return cors(Response("", 204))
    data = body_json()
    session = fast_sessions.get(str(data.get("session") or ""))
    with state_lock:
        if state["running"]:
            return cors(jsonify(status="running", **public_state()))
        state.update(running=True, started=datetime.now(KYIV).isoformat(timespec="seconds"),
                     finished=None, ok=None, horizon=session.horizon,
                     message=f"збираю розклад зі сторінок, завантажених телефоном "
                             f"({session.done} стор., {session.horizon} дн.)")
        payload = public_state()
    threading.Thread(target=do_fast_finish, args=(session,), daemon=True).start()
    return cors(jsonify(status="started", **payload))


@gateway_bp.route("/schedule.json")
def schedule_json():
    if latest_schedule is None:
        return cors(jsonify(error="ще не зібрано"), )
    return cors(Response(json.dumps(latest_schedule, ensure_ascii=False),
                         content_type="application/json; charset=utf-8"))


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(gateway_bp)
    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
