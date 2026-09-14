# -*- coding: utf-8 -*-
"""Європейський шлюз і збирач розкладу (Render, регіон Frankfurt).

Навіщо: swrailway.gov.ua приймає з'єднання лише з європейських мереж, а GitHub Actions
і основний бот працюють у США. Цей сервіс стоїть у Франкфурті, тому сайт УЗ для нього
доступний напряму.

Що вміє:
    GET  /                 health-check (Render, UptimeRobot)
    GET  /fetch?url=...    проксі однієї сторінки УЗ (використовує GitHub Actions)
    POST /refresh          зібрати розклад і закомітити в GitHub (кнопка в Mini App)
    GET  /status           стан останньої збірки
    GET  /schedule.json    свіжозібраний розклад просто з пам'яті, без очікування Pages

Розгортання (Render → New → Web Service, репозиторій merefa-rozklad, регіон Frankfurt, Free):
    Build Command:  pip install -r requirements.txt
    Start Command:  gunicorn gateway:app --bind 0.0.0.0:$PORT --timeout 300 --workers 1
    Environment:
        GH_TOKEN      = fine-grained token з правом Contents: Read and write на репозиторій
        GH_REPO       = stanislavperec-ua/merefa-rozklad   (необов'язково, це значення за умовчанням)
        GATEWAY_TOKEN = довільний рядок (необов'язково; захищає /fetch)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta

import requests
from flask import Flask, Response, abort, jsonify, request

import build_schedule
import uz

log = logging.getLogger("gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = Flask(__name__)

ALLOWED_HOSTS = {"swrailway.gov.ua", "www.swrailway.gov.ua"}
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO = os.environ.get("GH_REPO", "stanislavperec-ua/merefa-rozklad")
GH_API = "https://api.github.com"
HORIZON = int(os.environ.get("HORIZON_DAYS", "14"))
MIN_INTERVAL = timedelta(minutes=int(os.environ.get("MIN_REFRESH_MINUTES", "5")))
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
}
state_lock = threading.RLock()   # реентерабельний: public_state() викликається і зсередини блоків
latest_schedule: dict | None = None


def cors(resp: Response) -> Response:
    """Mini App відкривається з GitHub Pages, а в Telegram WebView origin буває null."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/")
def index():
    return cors(Response("OK", 200, content_type="text/plain; charset=utf-8"))


@app.route("/whoami")
def whoami():
    """Діагностика: звідки сервіс виходить у мережу і чи бачить сайт УЗ.

    Сайт відповідає лише європейським мережам, тому сервіс має стояти в регіоні Frankfurt.
    """
    info: dict = {"region_env": os.environ.get("RENDER_REGION", "невідомо")}
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
    return cors(jsonify(**info))


@app.route("/fetch")
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
    return {"Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


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


def do_refresh() -> None:
    """Збирає розклад і зберігає його в GitHub. Прапорець running уже виставлено у refresh()."""
    global latest_schedule
    try:
        cache, cache_sha = ({}, None)
        try:
            cache, cache_sha = gh_get_file("trains_cache.json")
            cache = cache or {}
        except Exception as e:  # noqa: BLE001
            log.warning("Кеш поїздів недоступний, збираю без нього: %s", e)

        client = uz.Client()          # сервіс у Європі: працює прямий маршрут
        schedule = build_schedule.build(client, datetime.now(KYIV).date(), HORIZON, cache, False)
        latest_schedule = schedule

        committed = False
        if GH_TOKEN:
            old, sha = gh_get_file("schedule.json")
            if old is None or strip_volatile(old) != strip_volatile(schedule):
                stamp = datetime.now(KYIV).strftime("%Y-%m-%d %H:%M")
                gh_put_file("schedule.json", schedule, sha, f"Timetable update (gateway) {stamp}")
                if cache:
                    try:
                        _, cache_sha_now = gh_get_file("trains_cache.json")
                        gh_put_file("trains_cache.json", cache, cache_sha_now or cache_sha,
                                    f"Trains cache (gateway) {stamp}")
                    except Exception as e:  # noqa: BLE001
                        log.warning("Кеш не збережено: %s", e)
                committed = True
                msg = "розклад оновлено і збережено в GitHub"
            else:
                msg = "розклад не змінився"
        else:
            msg = "розклад зібрано (GH_TOKEN не задано, у GitHub не збережено)"

        with state_lock:
            state.update(running=False, finished=datetime.now(KYIV).isoformat(timespec="seconds"),
                         ok=True, message=msg, generated=schedule.get("generated"),
                         committed=committed, requests=schedule.get("stats", {}).get("requests", 0))
        log.info("Оновлення завершено: %s", msg)
    except Exception as e:  # noqa: BLE001
        log.exception("Збірка не вдалася")
        with state_lock:
            state.update(running=False, finished=datetime.now(KYIV).isoformat(timespec="seconds"),
                         ok=False, message=f"помилка: {str(e)[:200]}")


@app.route("/refresh", methods=["POST", "GET", "OPTIONS"])
def refresh():
    if request.method == "OPTIONS":
        return cors(Response("", 204))
    with state_lock:
        if state["running"]:
            return cors(jsonify(status="running", **public_state()))
        last = state.get("finished")
        if last and not request.args.get("force"):
            try:
                since = datetime.now(KYIV) - datetime.fromisoformat(last)
                if since < MIN_INTERVAL:
                    wait = int((MIN_INTERVAL - since).total_seconds())
                    return cors(jsonify(status="too_soon", wait_seconds=wait, **public_state()))
            except ValueError:
                pass
        state.update(running=True, started=datetime.now(KYIV).isoformat(timespec="seconds"),
                     finished=None, ok=None, message="збираю розклад з сайту УЗ")
        payload = public_state()
    threading.Thread(target=do_refresh, daemon=True).start()
    return cors(jsonify(status="started", **payload))


def public_state() -> dict:
    with state_lock:
        return {k: state[k] for k in ("running", "started", "finished", "ok", "message",
                                      "generated", "committed", "requests")}


@app.route("/status")
def status():
    return cors(jsonify(**public_state()))


@app.route("/schedule.json")
def schedule_json():
    if latest_schedule is None:
        return cors(jsonify(error="ще не зібрано"), )
    return cors(Response(json.dumps(latest_schedule, ensure_ascii=False),
                         content_type="application/json; charset=utf-8"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
