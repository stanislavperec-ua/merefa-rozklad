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
| `gateway.py` | збирач розкладу: маршрути /refresh, /status, /schedule.json, /whoami (Blueprint, підключений до бота) |
| `worker.js` | Cloudflare Worker: єдиний шлях до сайту УЗ з хмар |
| `update_schedule.cmd` | запасний шлях: зібрати розклад з ПК в Україні і запушити вручну |
| `tests/` | 25 юніт-тестів: парсер, шлюзи, слоти оновлення |

## Важливо: сайт УЗ блокує великі хмари

Перевірено дослідним шляхом:

| Звідки | Мережа | Результат |
|---|---|---|
| ПК в Україні | Disavi Line | працює |
| GitHub Actions | Microsoft Azure (США) | timeout |
| Render, регіон Frankfurt | Amazon AWS (Німеччина) | timeout |
| api.cors.lol | Hetzner (Німеччина) | працює |
| Cloudflare Worker | Cloudflare | працює |

Тобто справа не в географії, а в тому, що swrailway.gov.ua відкидає діапазони Amazon,
Microsoft і Google. Тому запити йдуть через **Cloudflare Worker** (`worker.js`):
безкоштовно назавжди, 100 000 запитів на добу, розгортається за три хвилини.

### Схема

```
Cloudflare Worker /fetch  ←── єдиний шлях до swrailway.gov.ua з хмари
        ▲                      ▲
        │                      │
GitHub Actions            бот на Render
(слоти 06:00/10:00/13:00, (кнопка ↻ у Mini App: збирає і комітить сам)
 збирає і комітить)
        │
        ▼
GitHub Pages: index.html читає schedule.json + live.json
```

Збірку виконує GitHub Actions: там нормальний процесор і повний прогін триває кілька хвилин.
Бот теж уміє збирати (маршрути з `gateway.py`), але на безкоштовному Render лише 0.1 CPU,
тому кнопка ↻ покладається на кеш `trains_cache.json` і працює приблизно дві хвилини.

### Стійкість до збоїв

Сайт УЗ віддає шлюзу 522, якщо стукати надто часто, тому пауза між запитами 3 секунди
і чотири спроби. Якщо дата все одно не завантажилась, вона потрапляє в `skipped_days`
і **не** вважається днем без поїздів: інакше мережевий збій виглядав би як скасування
всього розкладу. Якщо не зібралась більшість дат, `schedule.json` не перезаписується.

### Налаштування (одноразово, безкоштовно)

1. **Cloudflare Worker.** https://dash.cloudflare.com → Compute (Workers) → Create →
   Start with Hello World → Deploy → Edit code → вставити `worker.js` → Deploy.
   Адреса вже прописана в `uz.GATEWAYS`; якщо створите свій, замініть її там.
2. **Токен для бота.** https://dashboard.render.com → merefa-rozklad → Environment →
   `GH_TOKEN` = fine-grained token GitHub з правом Contents: Read and write.
   Потрібен лише для кнопки ↻; автоматичні оновлення від нього не залежать.

Запасний шлях, якщо все впаде: `update_schedule.cmd` на ПК в Україні (прямий доступ до сайту УЗ).

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
