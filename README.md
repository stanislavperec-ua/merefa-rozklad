# merefa-rozklad

Telegram Mini App і бот з розкладом приміських електричок Харків ⇄ Мерефа.
Розклад береться з офіційного сайту swrailway.gov.ua, оперативні затримки і скасування
з каналу УЗ «Приміські поїзди» (t.me/UZprymisky).

* Mini App: https://stanislavperec-ua.github.io/merefa-rozklad/
* Бот: «Розклад електричок Харків – Мерефа» (Render, webhook)

## Як це працює

```
GitHub Actions (щопівгодини)
  ├─ build_schedule.py  ──► schedule.json  (офіційний розклад УЗ на 14 днів; збирається о 06:00, 10:00 і 13:00 за Києвом)
  └─ build_live.py      ──► live.json      (канал УЗ: затримки / скасування за останні 12 год)
                                 │ commit у main
                                 ▼
                    GitHub Pages: index.html читає schedule.json + live.json
                                 ▲
Telegram ─► webhook ─► Render (bot.py): кнопка Mini App, /next, /zminy
```

| Файл | Призначення |
|---|---|
| `uz.py` | клієнт і парсер swrailway.gov.ua (перелік між станціями, сторінка поїзда, «Зміни руху»), перелік станцій з кодами `sid`, часова зона Києва |
| `build_schedule.py` | збирає `schedule.json`: поїзди, точні зупинки, скасування на кожну дату |
| `build_live.py` | збирає `live.json` з каналу УЗ |
| `trains_cache.json` | кеш сторінок поїздів (маршрут по зупинках), оновлюється раз на 7 днів |
| `index.html` | Mini App (весь UI в одному файлі) |
| `bot.py` | Telegram-бот (Flask + pyTelegramBotAPI) |
| `gateway.py` | резервний шлюз до сайту УЗ (розгортається на Render у регіоні Frankfurt) |
| `tests/` | 25 юніт-тестів: парсер, шлюзи, слоти оновлення |

## Важливо: сайт УЗ відповідає лише європейським мережам

Перевірка з 25 вузлів світу (check-host.net) показала: swrailway.gov.ua приймає з'єднання
з Європи (Нідерланди, Фінляндія, Австрія, Британія, Молдова, Україна), але мовчить для США,
Канади, Азії та РФ. GitHub Actions і Render у США отримують TCP timeout. Публічні CORS-шлюзи
теж не рятують: єдиний робочий (api.cors.lol) блокує вже після десятка запитів.

Тому розклад збирає **gateway.py на Render у регіоні Frankfurt**: для нього сайт УЗ доступний
напряму. Сервіс уміє:

| Маршрут | Призначення |
|---|---|
| `GET /` | health-check (Render, UptimeRobot) |
| `GET /fetch?url=...` | проксі однієї сторінки УЗ (запасний режим для Actions) |
| `POST /refresh` | зібрати розклад і закомітити `schedule.json` через GitHub API |
| `GET /status` | стан останньої збірки |
| `GET /schedule.json` | свіжозібраний розклад просто з пам'яті, без очікування GitHub Pages |

### Налаштування сервісу (одноразово, безкоштовно)

1. **Токен GitHub:** https://github.com/settings/personal-access-tokens/new → Repository access:
   `merefa-rozklad` → Permissions → Repository permissions → **Contents: Read and write** → Generate.
2. **Сервіс:** https://dashboard.render.com → New → Web Service → репозиторій `merefa-rozklad` →
   **Region: Frankfurt (EU Central)**, Instance Type: **Free**,
   Build Command `pip install -r requirements.txt`,
   Start Command `gunicorn gateway:app --bind 0.0.0.0:$PORT --timeout 300 --workers 1`,
   Environment: `GH_TOKEN` = токен з кроку 1.
3. **Секрет репозиторію:** Settings → Secrets and variables → Actions → New repository secret:
   ім'я `GATEWAY_URL`, значення `https://<ім'я-сервісу>.onrender.com`.
4. **Кнопка в Mini App:** у `index.html` вписати ту саму адресу в константу `GATEWAY_URL`.

Після цього оновлення повністю автономне: GitHub Actions щопівгодини перевіряє, чи настав слот
(06:00, 10:00, 13:00 за Києвом), і якщо так, просить сервіс зібрати розклад. Кнопка ↻ у Mini App
запускає збірку вручну і одразу показує свіжі дані, не чекаючи деплою GitHub Pages.
Безкоштовний Render засинає без трафіку, тому перший запит прокидає сервіс до хвилини.

## Локальний запуск

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v     # тести парсера
python build_schedule.py --horizon 14       # ~60 запитів до сайту УЗ, ~1 хв
python build_live.py --hours 12
python -m http.server 8765                  # відкрити http://localhost:8765/
```

Бот потребує змінних оточення `BOT_TOKEN`, `RENDER_URL`, `PORT`, опційно `WEBHOOK_SECRET`.

## Формат schedule.json (version 2)

```json
{
  "generated": "2026-09-14T21:24:20+03:00",
  "horizon": {"from": "2026-09-14", "to": "2026-09-27"},
  "stations": [{"sid": 2528, "name": "Харків-Пас.", "full": "Харків-Пасажирський"}, "..."],
  "trains": {
    "28152": {
      "num": "6685", "route": "Харків-Пасажирський – Берестин", "from": "...", "to": "...",
      "dir": "m", "days": "щоденно", "valid_from": "2025-12-14", "valid_to": "2026-12-12",
      "stops": {"2528": {"arr": null, "dep": "07:13"}, "3211": {"arr": "07:38", "dep": "07:39"}, "...": {}},
      "notes": [{"text": "З 17 вересня ... ВІДМІНЕНО", "from": "2026-09-08", "to": "2026-09-18"}]
    }
  },
  "days": {
    "2026-09-14": {"running": ["28152", "..."], "cancelled": [{"tid": "...", "note": "..."}], "off": []}
  }
}
```

`tid` є ідентифікатором поїзда на сайті УЗ (`?tid=28152`); один номер може мати кілька `tid`
з різними термінами дії. `dir`: `m` означає «на Мерефу», `k` означає «на Харків».
