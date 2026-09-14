"""Тести парсера swrailway.gov.ua на збережених сторінках (tests/fixtures).

Запуск:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import unittest
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import uz                      # noqa: E402
import build_schedule          # noqa: E402
import build_live              # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def read(name: str) -> str:
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


class PairListTests(unittest.TestCase):
    def test_kharkiv_merefa_on_date(self):
        rows = uz.parse_pair_list(read("pair_kh_mer_2026-09-14.html"))
        self.assertEqual(len(rows), 12)
        nums = [r.num for r in rows]
        self.assertEqual(nums[:3], ["6701", "6685", "6513"])
        r = rows[1]
        self.assertEqual(r.tid, "28152")
        self.assertEqual(r.route, "Харків-Пасажирський – Берестин")
        self.assertEqual((r.dep, r.arr), ("07:13", "07:56"))
        self.assertEqual((r.valid_from, r.valid_to), ("2025-12-14", "2026-12-12"))
        self.assertEqual(r.days, "щоденно")
        self.assertFalse(r.has_notes)

    def test_notes_counter_flag(self):
        rows = {r.num: r for r in uz.parse_pair_list(read("pair_kh_mer_all.html"))}
        self.assertTrue(rows["6527"].has_notes)
        self.assertFalse(rows["6685"].has_notes)

    def test_all_days_has_both_versions_of_6525(self):
        rows = [r for r in uz.parse_pair_list(read("pair_kh_mer_all.html")) if r.num == "6525"]
        self.assertEqual({(r.tid, r.valid_from, r.valid_to) for r in rows},
                         {("28052", "2026-08-12", "2026-09-16"), ("30665", "2026-09-17", "2026-12-12")})

    def test_reverse_direction(self):
        rows = uz.parse_pair_list(read("pair_mer_kh_2026-09-15.html"))
        self.assertEqual(len(rows), 15)
        first = rows[0]
        self.assertEqual(first.num, "6506")
        self.assertEqual((first.dep, first.arr), ("05:40", "06:25"))   # відпр. з Мерефи, приб. у Харків


class TrainPageTests(unittest.TestCase):
    def test_full_route_6685(self):
        p = uz.parse_train_page(read("train_6685_tid28152.html"), "28152")
        self.assertEqual(p.num, "6685")
        self.assertEqual(p.route, "Харків-Пасажирський – Берестин")
        self.assertEqual((p.valid_from, p.valid_to), ("2025-12-14", "2026-12-12"))
        self.assertEqual(p.days, "щоденно")
        self.assertEqual(len(p.stops), 28)
        by_sid = {s.sid: s for s in p.stops}
        self.assertEqual((by_sid[2528].arr, by_sid[2528].dep), (None, "07:13"))
        self.assertEqual((by_sid[3211].arr, by_sid[3211].dep), ("07:38", "07:39"))
        self.assertEqual((by_sid[2538].arr, by_sid[2538].dep), ("07:56", "07:58"))
        self.assertEqual(by_sid[3211].name, "Високий")
        self.assertEqual(p.notes, [])

    def test_notes_7001(self):
        p = uz.parse_train_page(read("train_7001_tid28048.html"), "28048")
        self.assertEqual(len(p.notes), 1)
        n = p.notes[0]
        self.assertIn("№7001", n.text)
        self.assertIn("ВІДМІНЕНО", n.text)
        self.assertEqual((n.valid_from, n.valid_to), ("2026-09-09", "2026-09-16"))

    def test_split_route(self):
        self.assertEqual(uz.split_route("Харків-Пасажирський – Берестин"), ("Харків-Пасажирський", "Берестин"))
        self.assertEqual(uz.split_route("Зміїв - Харків-Пасажирський"), ("Зміїв", "Харків-Пасажирський"))


class BuildScheduleTests(unittest.TestCase):
    def test_page_to_entry_direction_and_stops(self):
        p = uz.parse_train_page(read("train_6685_tid28152.html"), "28152")
        e = build_schedule.page_to_entry(p, datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(e["dir"], "m")
        self.assertEqual(e["from"], "Харків-Пасажирський")
        self.assertEqual(e["to"], "Берестин")
        self.assertEqual(set(e["stops"]), {str(s) for s in uz.STATION_SIDS})
        self.assertEqual(e["stops"]["2536"], {"arr": "07:47", "dep": "07:48"})

    def test_pick_note(self):
        notes = [{"text": "a", "from": "2026-09-09", "to": "2026-09-16"}, {"text": "b", "from": "2026-09-20", "to": None}]
        self.assertEqual(build_schedule.pick_note(notes, "2026-09-10")["text"], "a")
        self.assertEqual(build_schedule.pick_note(notes, "2026-09-25")["text"], "b")
        self.assertEqual(build_schedule.pick_note(notes, "2026-09-18")["text"], "b")   # остання за замовчуванням
        self.assertIsNone(build_schedule.pick_note([], "2026-09-18"))

    def test_build_with_fake_client(self):
        """Скасування визначається як «діє за терміном, але відсутній у переліку на дату»."""
        all_html = read("pair_kh_mer_all.html")
        date_html = read("pair_kh_mer_2026-09-14.html")
        rev_html = read("pair_mer_kh_2026-09-15.html")
        pages = {"28152": read("train_6685_tid28152.html"), "28048": read("train_7001_tid28048.html")}

        class Fake:
            requests_made = 0

            def pair_list(self, a, b, d=None):
                self.requests_made += 1
                if a == uz.KHARKIV_SID:
                    return uz.parse_pair_list(all_html if d is None else date_html)
                return uz.parse_pair_list(rev_html)

            def train_page(self, tid):
                self.requests_made += 1
                if tid in pages:
                    return uz.parse_train_page(pages[tid], tid)
                # для решти поїздів: мінімальна сторінка на основі 6685
                p = uz.parse_train_page(pages["28152"], tid)
                p.notes = []
                return p

        sched = build_schedule.build(Fake(), date(2026, 9, 14), 1, {}, False)
        day = sched["days"]["2026-09-14"]
        self.assertEqual(len(day["running"]), 27)
        # 7001 (tid 28048) діє з 17.09, тож 14.09 не скасований; 6525/30665 теж з 17.09
        cancelled = {c["tid"] for c in day["cancelled"]}
        self.assertNotIn("28048", cancelled)
        self.assertNotIn("30665", cancelled)
        self.assertEqual(sched["trains"]["28048"]["notes"][0]["to"], "2026-09-16")
        self.assertEqual(sched["trains"]["28152"]["stops"]["3211"]["dep"], "07:39")
        json.dumps(sched, ensure_ascii=False)   # серіалізується без помилок


class LiveClassifyTests(unittest.TestCase):
    TAIL = (" ❗️ У разі підвищеної небезпеки поїзд буде зупинено. Будь ласка, дотримуйтесь правил евакуації. "
            "Рух поїзда буде відновлено лише по завершенні повітряної загрози. #Харківщина")

    def test_delay_minutes(self):
        kind, minutes = build_live.classify(
            "Харківщина ❕ З безпекових міркувань затримується: 🚊 Поїзд №6691 Харків-Пасажирський – Берестин "
            "курсує зі станції Ордівка із затримкою 49 хв. Дякуємо за розуміння." + self.TAIL)
        self.assertEqual((kind, minutes), ("delay", 49))

    def test_delay_hours(self):
        kind, minutes = build_live.classify("Поїзд №6280 курсує із затримкою 1 год. 45 хв." + self.TAIL)
        self.assertEqual((kind, minutes), ("delay", 105))

    def test_cancel_and_resume(self):
        self.assertEqual(build_live.classify("Поїзд №6686 Берестин – Харків сьогодні скасовано." + self.TAIL)[0], "cancel")
        self.assertEqual(build_live.classify("Рух відновлено. Поїзд №6686 курсує за графіком.")[0], "resume")

    def test_strip_tail_and_region(self):
        self.assertEqual(build_live.strip_tail("Текст поста." + self.TAIL), "Текст поста.")
        self.assertTrue(build_live.is_our_region("Поїзд №6686 Берестин – Харків-Пасажирський"))
        self.assertFalse(build_live.is_our_region("Поїзд №6301 Черкаси – Христинівка"))

    def test_find_our_trains(self):
        nums = {"6685", "6686", "6335"}
        self.assertEqual(build_live.find_our_trains("Поїзд №6335/6336 Здолбунів", nums), ["6335"])
        self.assertEqual(build_live.find_our_trains("Поїзд №66850", nums), [])


class KyivTzTests(unittest.TestCase):
    def test_fallback_offsets(self):
        tz = uz._KyivFallback()
        summer = datetime(2026, 9, 14, 18, 0, tzinfo=timezone.utc).astimezone(tz)
        winter = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc).astimezone(tz)
        self.assertEqual(summer.isoformat(), "2026-09-14T21:00:00+03:00")
        self.assertEqual(winter.isoformat(), "2026-01-15T14:00:00+02:00")


if __name__ == "__main__":
    unittest.main()
