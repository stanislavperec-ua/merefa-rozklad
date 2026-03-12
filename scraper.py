#!/usr/bin/env python3
import json, re
from datetime import datetime, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup

OUR_TRAINS = {
    "6506","6682","6508","6684","6702","6512",
    "6678","6852","6686","6516","6688","6690",
    "6524","6694","6856",
    "6825","6685","6513","6687","6853","6691",
    "7001","6525","6705","6693","6527","6697","6529",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MerefaRozkladBot/1.0)"}

def fetch_uz_website():
    notes, alerts = {}, []
    try:
        resp = requests.get("https://swrailway.gov.ua/timetable/eltrain/", headers=HEADERS, timeout=15)
        resp.raise_for_status()
        text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
        for m in re.finditer(r'[№#]?\s*(\d{4})\b[^.;\n]{0,80}?(?:скасов|відмін|не\s+курс)', text, re.I):
            num = m.group(1)
            if num in OUR_TRAINS and num not in notes:
                notes[num] = {"cancelled": True, "note": "❌ Скасовано"}
                alerts.append(f"скасовано №{num}")
        for m in re.finditer(r'[№#]?\s*(\d{4})\b[^.;\n]{0,100}?(?:зміне|скороч|курсує\s+до\s+(\w+))', text, re.I):
            num, dest = m.group(1), m.group(2) or "змінено"
            if num in OUR_TRAINS and num not in notes:
                notes[num] = {"cancelled": False, "note": f"⚠ До {dest.capitalize()}"}
                alerts.append(f"зміна №{num}")
        print(f"[UZ сайт] Знайдено: {len(notes)}")
    except Exception as e:
        print(f"[UZ сайт] Помилка: {e}")
    return notes, alerts

def fetch_uz_telegram():
    notes, alerts = {}, []
    try:
        resp = requests.get("https://t.me/s/UZprymisky", headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        messages = soup.select(".tgme_widget_message_text")
        for msg in messages[-10:]:
            text = msg.get_text(" ", strip=True)
            for m in re.finditer(r'[№#]?\s*(\d{4})\b[^.;\n]{0,80}?(?:скасов|відмін|не\s+курс)', text, re.I):
                num = m.group(1)
                if num in OUR_TRAINS and num not in notes:
                    notes[num] = {"cancelled": True, "note": "❌ Скасовано"}
                    alerts.append(f"скасовано №{num}")
            for m in re.finditer(r'[№#]?\s*(\d{4})\b[^.;\n]{0,100}?(?:зміне|скороч|курсує\s+до\s+(\w+))', text, re.I):
                num, dest = m.group(1), m.group(2) or "змінено"
                if num in OUR_TRAINS and num not in notes:
                    notes[num] = {"cancelled": False, "note": f"⚠ До {dest.capitalize()}"}
                    alerts.append(f"зміна №{num}")
        print(f"[Telegram] Знайдено: {len(notes)}")
    except Exception as e:
        print(f"[Telegram] Помилка: {e}")
    return notes, alerts

def main():
    notes_site, alerts_site = fetch_uz_website()
    notes_tg,   alerts_tg   = fetch_uz_telegram()
    all_notes  = {**notes_site, **notes_tg}
    all_alerts = list(dict.fromkeys(alerts_site + alerts_tg))
    alert_text = ""
    if all_alerts:
        alert_text = ", ".join(all_alerts).capitalize() + ". Деталі: t.me/UZprymisky"
    data = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "alert":   alert_text,
        "notes":   [{"num": num, **info} for num, info in all_notes.items()]
    }
    out = Path(__file__).parent / "data.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[DONE] Збережено. Змін: {len(all_notes)}")

if __name__ == "__main__":
    main()
