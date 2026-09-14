# merefa-rozklad

Telegram Mini App і бот з розкладом приміських електричок Харків ⇄ Мерефа.
Розклад береться з офіційного сайту swrailway.gov.ua, оперативні затримки і скасування
з каналу УЗ «Приміські поїзди» (t.me/UZprymisky).

* Mini App: https://stanislavperec-ua.github.io/merefa-rozklad/
* Бот: «Розклад електричок Харків – Мерефа» (Render, webhook)

## Як це працює

```
GitHub Actions (щопівгодини)
  ├─ build_schedule.py  ──► schedule.json  (офіційний розклад УЗ, горизонт 14 днів, не частіше ніж раз на 4 год)
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
| `tests/` | юніт-тести парсера на збережених сторінках УЗ |

## Важливо: сайт УЗ відповідає лише з українських адрес

swrailway.gov.ua і uz.gov.ua не приймають з'єднання з-за кордону (гео-блокування: GitHub Actions,
Render, Google, інші хмари отримують TCP timeout), але відкриваються з будь-якої мережі в Україні.
Тому крок розкладу в GitHub Actions ходить на сайт через резидентний проксі з українським IP:

1. Apify (є безкоштовний тариф з кредитом $5/міс; резидентний трафік $8/ГБ, прогін ≈ 1 МБ,
   4 прогони на добу ≈ 120 МБ/міс ≈ $1): Settings → API & Integrations → скопіювати Personal API token.
2. У репозиторії: Settings → Secrets and variables → Actions → New repository secret:
   ім'я `UZ_PROXY`, значення
   `http://groups-RESIDENTIAL,country-UA:<APIFY_TOKEN>@proxy.apify.com:8000`
3. Actions → «Diag network» → Run workflow: у логу має бути `proxy egress ip: ... (UA)` і `HTTP 200 ... eltrain=2`.
4. Actions → «Update schedule data» → Run workflow з force. Далі все автоматично: розклад не
   частіше ніж раз на 6 годин, канал щопівгодини.

Без секрету крок розкладу пропускається, а `schedule.json` можна оновити з ПК в Україні
подвійним кліком по `update_schedule.cmd` (запасний шлях).

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
