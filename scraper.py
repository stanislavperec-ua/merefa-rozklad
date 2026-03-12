import requests
from bs4 import BeautifulSoup
import json
import re
from datetime import datetime, timedelta

# Номери наших поїздів
OUR_TRAINS = [
    "6825", "6685", "6513", "6687", "6853", "6691",
    "7001", "6525", "6705", "6693", "6527", "6697", "6529",
    "6506", "6682", "6508", "6684", "6702", "6512", "6678",
    "6852", "6686", "6516", "6688", "6690", "6524", "6694", "6856"
]

# Слова що означають скасування або зміну
CANCEL_WORDS = [
    "скасован", "скасовано", "скасовується", "відмінен", "відмінено",
    "не курсує", "не буде курсувати", "не курсуватим", "тимчасово не",
    "без зупинки", "змін"
]

def fetch_channel_posts():
    url = "https://t.me/s/UZprymisky"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"Помилка завантаження каналу: {e}")
        return None

def parse_posts(html):
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for msg in soup.find_all("div", class_="tgme_widget_message"):
        text_div = msg.find("div", class_="tgme_widget_message_text")
        date_tag = msg.find("time")
        if text_div:
            text = text_div.get_text(separator=" ").strip()
            date_str = date_tag["datetime"][:10] if date_tag and date_tag.get("datetime") else ""
            posts.append({"text": text, "date": date_str})
    return posts

def find_relevant_posts(posts):
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    relevant = []
    for post in posts:
        text_lower = post["text"].lower()
        # Перевіряємо чи є номер нашого поїзда
        found_trains = [t for t in OUR_TRAINS if t in post["text"]]
        if not found_trains:
            continue
        # Перевіряємо чи є слова про скасування/зміну
        found_cancel = [w for w in CANCEL_WORDS if w in text_lower]
        if not found_cancel:
            continue
        relevant.append({
            "trains": found_trains,
            "text": post["text"][:300],
            "date": post["date"],
            "type": "cancel" if any(w in text_lower for w in [
                "скасован", "відмінен", "не курсує", "не курсуватим", "тимчасово не"
            ]) else "change"
        })
    return relevant

def build_notes(relevant_posts):
    notes = {}
    for post in relevant_posts:
        for train in post["trains"]:
            if post["type"] == "cancel":
                notes[train] = {"status": "cancelled", "note": "Скасовано", "source": post["text"][:100]}
            else:
                notes[train] = {"status": "changed", "note": "Зміни в розкладі", "source": post["text"][:100]}
    return notes

def load_existing_data():
    try:
        with open("data.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"cancellations": {}, "changes": {}, "notes": [], "updated": ""}

def save_data(data):
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("data.json збережено")

def main():
    print(f"Запуск скрапера: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    html = fetch_channel_posts()
    if not html:
        print("Не вдалося завантажити канал — залишаємо старий data.json")
        return

    posts = parse_posts(html)
    print(f"Знайдено постів: {len(posts)}")

    relevant = find_relevant_posts(posts)
    print(f"Релевантних постів про наші поїзди: {len(relevant)}")

    for r in relevant:
        print(f"  Поїзди: {r['trains']} | Тип: {r['type']} | {r['text'][:80]}")

    notes = build_notes(relevant)

    data = load_existing_data()
    data["cancellations"] = {k: v for k, v in notes.items() if v["status"] == "cancelled"}
    data["changes"] = {k: v for k, v in notes.items() if v["status"] == "changed"}
    data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    data["notes"] = [r["text"] for r in relevant]

    save_data(data)
    print(f"Готово. Скасовано: {list(data['cancellations'].keys())}")
    print(f"Зміни: {list(data['changes'].keys())}")

if __name__ == "__main__":
    main()

