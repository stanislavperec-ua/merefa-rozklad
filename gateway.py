# -*- coding: utf-8 -*-
"""Європейський шлюз до сайту УЗ.

Навіщо: swrailway.gov.ua приймає з'єднання лише з європейських мереж, а GitHub Actions
працює у США. Цей крихітний сервіс розгортається безкоштовно на Render у регіоні Frankfurt
і переспрямовує запити: GitHub Actions звертається сюди, сервіс ходить на сайт УЗ і віддає
сторінку як є.

Розгортання (Render → New → Web Service, регіон Frankfurt, Free):
    Build Command:  pip install -r requirements.txt
    Start Command:  gunicorn gateway:app --bind 0.0.0.0:$PORT --timeout 120
    Environment:    GATEWAY_TOKEN = довільний рядок (необов'язково, але бажано)

Використання:
    GET /fetch?url=<URL-encoded адреса сторінки УЗ>[&token=...]
    GET /         → health-check для UptimeRobot і Render
"""
from __future__ import annotations

import logging
import os

import requests
from flask import Flask, Response, abort, request

log = logging.getLogger("gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = Flask(__name__)

# Дозволені адресати: лише сайти розкладу УЗ, щоб шлюз не став відкритим проксі
ALLOWED_HOSTS = {"swrailway.gov.ua", "www.swrailway.gov.ua"}
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")
TIMEOUT = (15, 60)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 merefa-rozklad/2.0"
)


@app.route("/")
def index():
    return "OK", 200


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
