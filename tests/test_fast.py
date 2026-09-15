"""Тести швидкого оновлення: сторінки завантажує телефон, сервер їх лише розбирає.

Запуск:  python -m unittest discover -s tests -v
"""
import hashlib
import hmac
import os
import re
import sys
import time
import unittest
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import uz                      # noqa: E402
import build_schedule          # noqa: E402
import fastbuild               # noqa: E402

FIX = os.path.join(HERE, "fixtures")
TODAY = date(2026, 9, 14)
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=build_schedule.KYIV)


def read(name: str) -> str:
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def fill_one(session: fastbuild.Session, page_id: str) -> None:
    """Підсовує сесії ту фікстуру, яка відповідає виду сторінки."""
    if ":2528:2538:" not in page_id:
        session.submit(page_id, read("pair_mer_kh_2026-09-15.html"))
    elif page_id.endswith(":"):
        session.submit(page_id, read("pair_kh_mer_all.html"))
    else:
        session.submit(page_id, read("pair_kh_mer_2026-09-14.html"))


def fill_lists(session: fastbuild.Session, skip: str | None = None) -> None:
    """Подає сесії всі переліки поїздів, як це зробив би телефон."""
    for task in list(session.state()["tasks"]):
        if task["id"] != skip:
            fill_one(session, task["id"])


def fill_trains(session: fastbuild.Session) -> None:
    """Подає сторінки поїздів: для двох є справжні, решті вистачить будь-якої справжньої."""
    pages = {"28152": read("train_6685_tid28152.html"), "28048": read("train_7001_tid28048.html")}
    for task in list(session.state()["tasks"]):
        tid = task["id"].split(":")[1]
        session.submit(task["id"], pages.get(tid, pages["28152"]))


class TaskPlanTests(unittest.TestCase):
    def test_list_tasks_count_and_urls(self):
        tasks = fastbuild.list_tasks(TODAY, 3, gateway="https://gw.test/fetch?url={url}")
        self.assertEqual(len(tasks), 2 + 2 * 3)          # два загальні переліки плюс два на дату
        ids = [t["id"] for t in tasks]
        self.assertEqual(ids[:2], ["pair:2528:2538:", "pair:2538:2528:"])
        self.assertIn("pair:2528:2538:2026-09-16", ids)
        url = [t["url"] for t in tasks if t["id"].endswith("2026-09-16")][0]
        self.assertTrue(url.startswith("https://gw.test/fetch?url="))
        self.assertIn("eventdate%3D2026-09-16", url)
        self.assertIn("sid1%3D2528", url)

    def test_default_gateway_is_the_cloudflare_worker(self):
        task = fastbuild.pair_task(uz.KHARKIV_SID, uz.MEREFA_SID)
        self.assertTrue(task["url"].startswith(uz.default_gateways()[0].split("?")[0]))

    def test_train_task_url(self):
        task = fastbuild.train_task("28152", gateway="https://gw.test/fetch?url={url}")
        self.assertEqual(task["id"], "train:28152")
        self.assertIn("tid%3D28152", task["url"])

    def test_rows_from_pairs_keeps_notes_flag(self):
        quiet = uz.TrainRow("1", "6685", "r", "щоденно", "", "", None, None, has_notes=False)
        loud = uz.TrainRow("1", "6685", "r", "щоденно", "", "", None, None, has_notes=True)
        rows = fastbuild.rows_from_pairs({"a": [quiet], "b": [loud]})
        self.assertTrue(rows["1"].has_notes, "червоний лічильник з будь-якого переліку має лишитись")

    def test_train_tasks_respect_cache(self):
        rows = {"1": uz.TrainRow("1", "6685", "r", "щоденно", "", "", None, None, False),
                "2": uz.TrainRow("2", "6701", "r", "щоденно", "", "", None, None, True)}
        cache = {"1": {"stops": {"2528": {}}, "fetched_at": (NOW - timedelta(days=1)).isoformat()},
                 "2": {"stops": {"2528": {}}, "fetched_at": (NOW - timedelta(days=1)).isoformat()}}
        ids = [t["id"] for t in fastbuild.train_tasks(rows, cache, NOW)]
        self.assertEqual(ids, ["train:2"], "свіжий кеш без повідомлень качати не треба")

        old = {"1": {"stops": {"2528": {}}, "fetched_at": (NOW - timedelta(days=30)).isoformat()}}
        ids = sorted(t["id"] for t in fastbuild.train_tasks(rows, old, NOW))
        self.assertEqual(ids, ["train:1", "train:2"])

    def test_needs_train_page_matches_build(self):
        row = uz.TrainRow("1", "6685", "r", "щоденно", "", "", None, None, False)
        fresh = {"1": {"stops": {"2528": {}}, "fetched_at": NOW.isoformat()}}
        self.assertFalse(build_schedule.needs_train_page(row, fresh, NOW))
        self.assertTrue(build_schedule.needs_train_page(row, fresh, NOW, refresh_cache=True))
        self.assertTrue(build_schedule.needs_train_page(row, {}, NOW))


class OfflineClientTests(unittest.TestCase):
    def test_missing_page_raises_uz_error(self):
        client = fastbuild.OfflineClient()
        with self.assertRaises(uz.UZError):
            client.pair_list(uz.KHARKIV_SID, uz.MEREFA_SID, "2026-09-14")
        with self.assertRaises(uz.UZError):
            client.train_page("28152")

    def test_parsed_once_served_many_times(self):
        client = fastbuild.OfflineClient()
        self.assertEqual(client.add_pair(uz.KHARKIV_SID, uz.MEREFA_SID, "2026-09-14",
                                         read("pair_kh_mer_2026-09-14.html")), 12)
        rows = client.pair_list(uz.KHARKIV_SID, uz.MEREFA_SID, "2026-09-14")
        self.assertEqual(rows[1].tid, "28152")
        self.assertIs(rows, client.pair_list(uz.KHARKIV_SID, uz.MEREFA_SID, "2026-09-14"))
        self.assertEqual(client.requests_made, 2)


class SessionTests(unittest.TestCase):
    def new_session(self, horizon=1, cache=None):
        return fastbuild.Session(today=TODAY, horizon=horizon, cache=cache or {}, now=NOW)

    def test_full_cycle_builds_schedule(self):
        session = self.new_session(horizon=1)
        self.assertEqual(session.phase, fastbuild.PHASE_LISTS)
        self.assertEqual(len(session.state()["tasks"]), 4)

        fill_lists(session)
        tasks = session.advance()
        self.assertEqual(session.phase, fastbuild.PHASE_TRAINS)
        self.assertTrue(tasks and all(t["id"].startswith("train:") for t in tasks))

        fill_trains(session)
        self.assertEqual(session.advance(), [])
        self.assertEqual(session.phase, fastbuild.PHASE_READY)

        schedule = session.build()
        day = schedule["days"]["2026-09-14"]
        self.assertEqual(len(day["running"]), 27)
        self.assertEqual(schedule["skipped_days"], [])
        self.assertEqual(schedule["stats"]["source"], "mini app")
        self.assertIn("28152", schedule["trains"])

    def test_cached_trains_are_not_requested_again(self):
        session = self.new_session(horizon=1)
        fill_lists(session)
        without_cache = len(session.advance())

        entry = {"stops": {"2528": {"arr": None, "dep": "07:13"}}, "notes": [],
                 "fetched_at": (NOW - timedelta(days=1)).isoformat()}
        rows = fastbuild.rows_from_pairs(session.client.pairs)
        cache = {tid: dict(entry) for tid in rows}
        cached = self.new_session(horizon=1, cache=cache)
        fill_lists(cached)
        with_cache = len(cached.advance())
        self.assertLess(with_cache, without_cache)
        # лишаються тільки поїзди з червоним лічильником повідомлень
        self.assertTrue(all(rows[t["id"].split(":")[1]].has_notes for t in cached.state()["tasks"]))

    def test_lost_page_becomes_skipped_day_not_mass_cancellation(self):
        """Якщо телефон не зміг завантажити дату, вона пропускається, а не «скасовується»."""
        session = self.new_session(horizon=2)
        lost = f"pair:2528:2538:{(TODAY + timedelta(days=1)).isoformat()}"
        fill_lists(session, skip=lost)
        session.give_up([lost])
        session.advance()
        fill_trains(session)
        schedule = session.build()
        self.assertEqual(schedule["skipped_days"], ["2026-09-15"])
        self.assertNotIn("2026-09-15", schedule["days"])
        self.assertLessEqual(len(schedule["days"]["2026-09-14"]["cancelled"]), 5)

    def test_bad_pages_are_rejected(self):
        session = self.new_session(horizon=1)
        with self.assertRaises(fastbuild.FastError):
            session.submit("pair:9999:2538:", "<html>ElTrain</html>")     # чужа станція
        with self.assertRaises(fastbuild.FastError):
            session.submit("whatever", "<html></html>")                  # невідомий вид сторінки
        with self.assertRaises(fastbuild.FastError):
            session.submit("pair:2528:2538:", None)                      # не рядок
        with self.assertRaises(fastbuild.FastError):
            session.submit("pair:2528:2538:", "x" * (fastbuild.MAX_PAGE_CHARS + 1))
        with self.assertRaises(fastbuild.FastError):
            session.submit("pair:2528:2538:", "<html>gateway error</html>")   # сторінка не з сайту УЗ
        with self.assertRaises(uz.UZError):
            session.submit("train:28152", "<html>ElTrain, але без розкладу</html>")
        self.assertEqual(session.done, 0)
        self.assertEqual(len(session.state()["tasks"]), 4, "жодну сторінку не зараховано")

    def test_state_shows_only_pending_tasks(self):
        session = self.new_session(horizon=1)
        fill_one(session, "pair:2528:2538:")
        ids = [t["id"] for t in session.state()["tasks"]]
        self.assertNotIn("pair:2528:2538:", ids)
        self.assertEqual(len(ids), 3)
        self.assertEqual(session.state()["received"], 1)

    def test_horizon_is_capped(self):
        self.assertEqual(fastbuild.Session(today=TODAY, horizon=999, now=NOW).horizon,
                         fastbuild.MAX_HORIZON)
        self.assertEqual(fastbuild.Session(today=TODAY, horizon=0, now=NOW).horizon, 1)


class SessionStoreTests(unittest.TestCase):
    def test_get_missing_session(self):
        store = fastbuild.SessionStore()
        with self.assertRaises(fastbuild.FastError):
            store.get("нема такої")

    def test_limit_pushes_out_the_oldest(self):
        store = fastbuild.SessionStore(limit=2)
        first = store.add(fastbuild.Session(today=TODAY, horizon=1, now=NOW))
        time.sleep(0.01)
        second = store.add(fastbuild.Session(today=TODAY, horizon=1, now=NOW))
        time.sleep(0.01)
        third = store.add(fastbuild.Session(today=TODAY, horizon=1, now=NOW))
        self.assertEqual(len(store), 2)
        with self.assertRaises(fastbuild.FastError):
            store.get(first.id)
        self.assertIs(store.get(second.id), second)
        self.assertIs(store.get(third.id), third)

    def test_expired_session_is_swept(self):
        store = fastbuild.SessionStore(ttl=0)
        session = store.add(fastbuild.Session(today=TODAY, horizon=1, now=NOW))
        time.sleep(0.01)
        with self.assertRaises(fastbuild.FastError):
            store.get(session.id)

    def test_drop(self):
        store = fastbuild.SessionStore()
        session = store.add(fastbuild.Session(today=TODAY, horizon=1, now=NOW))
        store.drop(session.id)
        self.assertEqual(len(store), 0)


class InitDataTests(unittest.TestCase):
    TOKEN = "123456:AAHtestTokenForUnitTestsOnly"

    def make(self, token=TOKEN, auth_date=None, extra=None):
        fields = {"auth_date": str(int(auth_date if auth_date is not None else time.time())),
                  "query_id": "AAHdM3", "user": '{"id":42,"first_name":"Тест"}'}
        fields.update(extra or {})
        check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urlencode(fields)

    def test_valid(self):
        data = fastbuild.check_init_data(self.make(), self.TOKEN)
        self.assertIsNotNone(data)
        self.assertEqual(data["user"], '{"id":42,"first_name":"Тест"}')

    def test_wrong_token(self):
        self.assertIsNone(fastbuild.check_init_data(self.make(), "999:other"))

    def test_tampered_field(self):
        init = self.make()
        self.assertIsNone(fastbuild.check_init_data(init.replace("query_id=AAHdM3", "query_id=AAHdM4"),
                                                    self.TOKEN))

    def test_expired(self):
        old = time.time() - 2 * 86400
        self.assertIsNone(fastbuild.check_init_data(self.make(auth_date=old), self.TOKEN))
        self.assertIsNotNone(fastbuild.check_init_data(self.make(auth_date=old), self.TOKEN, max_age=0))

    def test_empty_and_garbage(self):
        self.assertIsNone(fastbuild.check_init_data("", self.TOKEN))
        self.assertIsNone(fastbuild.check_init_data(self.make(), ""))
        self.assertIsNone(fastbuild.check_init_data("hash=abc", self.TOKEN))
        self.assertIsNone(fastbuild.check_init_data("не схоже на query string", self.TOKEN))


class TrimHtmlTests(unittest.TestCase):
    """Телефон перед відправкою прибирає зі сторінки скрипти і стилі: перевіряємо, що розбір не змінюється.

    Регулярки тут дзеркалять функцію trimHtml() з index.html.
    """

    @staticmethod
    def trim(html: str) -> str:
        html = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
        html = re.sub(r"<style[\s\S]*?</style>", "", html, flags=re.IGNORECASE)
        html = re.sub(r"<!--[\s\S]*?-->", "", html)
        return re.sub(r"[ \t]{2,}", " ", html)

    def test_pair_list_survives_trim(self):
        raw = read("pair_kh_mer_2026-09-14.html")
        trimmed = self.trim(raw)
        self.assertLess(len(trimmed), len(raw) * 0.8, "чистка має відчутно зменшувати обсяг")
        before = uz.parse_pair_list(raw)
        after = uz.parse_pair_list(trimmed)
        self.assertEqual([(r.tid, r.num, r.dep, r.arr, r.days, r.valid_from, r.valid_to, r.has_notes)
                          for r in before],
                         [(r.tid, r.num, r.dep, r.arr, r.days, r.valid_from, r.valid_to, r.has_notes)
                          for r in after])

    def test_train_page_survives_trim(self):
        raw = read("train_7001_tid28048.html")
        before = uz.parse_train_page(raw, "28048")
        after = uz.parse_train_page(self.trim(raw), "28048")
        self.assertEqual([(s.sid, s.arr, s.dep) for s in before.stops],
                         [(s.sid, s.arr, s.dep) for s in after.stops])
        self.assertEqual([(n.text, n.valid_from, n.valid_to) for n in before.notes],
                         [(n.text, n.valid_from, n.valid_to) for n in after.notes])
        self.assertEqual((before.num, before.route, before.days), (after.num, after.route, after.days))


if __name__ == "__main__":
    unittest.main()
