"""Тести маршрутів швидкого оновлення (/fast/start, /fast/pages, /fast/finish).

Мережа не потрібна: сторінки беруться з фікстур, GitHub і звірка з сайтом підмінені.
Якщо Flask не встановлено (раннер GitHub ставить лише requests і beautifulsoup4),
тести свідомо пропускаються.

Запуск:  python -m unittest discover -s tests -v
"""
import gzip
import hashlib
import hmac
import json
import os
import sys
import time
import unittest
from urllib.parse import urlencode

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import fastbuild               # noqa: E402

try:
    import gateway             # noqa: E402
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

FIX = os.path.join(HERE, "fixtures")
BOT_TOKEN = "123456:AAHtestTokenForUnitTestsOnly"


def read(name: str) -> str:
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def page_for(page_id: str) -> str:
    if page_id.startswith("train:"):
        return read("train_6685_tid28152.html")
    if ":2528:2538:" not in page_id:
        return read("pair_mer_kh_2026-09-15.html")
    return read("pair_kh_mer_all.html" if page_id.endswith(":") else "pair_kh_mer_2026-09-14.html")


def telegram_init_data(token: str = BOT_TOKEN) -> str:
    fields = {"auth_date": str(int(time.time())), "query_id": "AAHdM3",
              "user": '{"id":42,"first_name":"Тест"}'}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@unittest.skipUnless(HAS_FLASK, "Flask не встановлено")
class FastApiTests(unittest.TestCase):
    def setUp(self):
        self.client = gateway.create_app().test_client()
        self.checked = []
        gateway.GH_TOKEN = ""            # без GitHub: розклад лишається в пам'яті сервісу
        gateway.BOT_TOKEN = BOT_TOKEN
        gateway.fast_sessions = fastbuild.SessionStore()
        gateway.latest_schedule = None
        gateway.state.update(running=False, started=None, finished=None, ok=None,
                             message="тест", generated=None, committed=False, requests=0)
        # у мережу тести не ходять: звірка з сайтом і GitHub підмінені
        self._saved = (gateway.spot_check, gateway.start_spot_check,
                       gateway.gh_get_file, gateway.gh_put_file)
        self.real_spot_check = gateway.spot_check
        gateway.spot_check = self.fake_spot_check
        gateway.start_spot_check = lambda session: None
        gateway.gh_get_file = lambda path: (None, None)
        gateway.gh_put_file = lambda *a, **kw: None

    def tearDown(self):
        (gateway.spot_check, gateway.start_spot_check,
         gateway.gh_get_file, gateway.gh_put_file) = self._saved

    def fake_spot_check(self, *args, **kwargs):
        """Підміна звірки з сайтом УЗ: у тестах у мережу не ходимо."""
        self.checked.append(args)
        return self.verdict

    verdict = (True, "тест")

    # ── допоміжне ─────────────────────────────────────────
    def post(self, path, payload, gzip_body=False, token=None):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if gzip_body:
            raw, headers["X-Gzip"] = gzip.compress(raw), "1"
        if token is not None:
            headers["X-Fast-Token"] = token
        return self.client.post(path, data=raw, headers=headers)

    def send_pages(self, session, tasks, gzip_body=False, skip=()):
        answer = None
        for i in range(0, len(tasks), 4):
            chunk = [t for t in tasks[i:i + 4] if t["id"] not in skip]
            lost = [t["id"] for t in tasks[i:i + 4] if t["id"] in skip]
            pages = [{"id": t["id"], "html": page_for(t["id"])} for t in chunk]
            r = self.post("/fast/pages", {"session": session, "pages": pages, "failed": lost}, gzip_body)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            answer = r.get_json()
            self.assertEqual(answer["rejected"], [])
        return answer

    def wait_build(self, seconds=15):
        for _ in range(seconds * 10):
            st = self.client.get("/status").get_json()
            if not st["running"] and st["ok"] is not None:
                return st
            time.sleep(0.1)
        self.fail("збірка не завершилась")

    # ── сценарії ──────────────────────────────────────────
    def test_full_cycle_with_telegram_signature(self):
        started = self.post("/fast/start", {"days": 1, "force": True,
                                            "initData": telegram_init_data()}).get_json()
        self.assertEqual(started["status"], "started")
        self.assertTrue(started["trusted"], "підпис Telegram має прийматись")
        self.assertEqual(len(started["tasks"]), 4)

        lists = self.send_pages(started["session"], started["tasks"])
        self.assertEqual(lists["phase"], fastbuild.PHASE_TRAINS)
        self.assertTrue(lists["tasks"])

        trains = self.send_pages(started["session"], lists["tasks"], gzip_body=True)
        self.assertEqual(trains["phase"], fastbuild.PHASE_READY)
        self.assertEqual(trains["pending"], 0)

        r = self.post("/fast/finish", {"session": started["session"]})
        self.assertEqual(r.get_json()["status"], "started")
        st = self.wait_build()
        self.assertTrue(st["ok"], st["message"])
        self.assertEqual(self.checked, [], "клієнту з підписом Telegram звірка не потрібна")

        schedule = self.client.get("/schedule.json").get_json()
        self.assertEqual(len(schedule["days"]), 1)                 # горизонт один день
        running = list(schedule["days"].values())[0]["running"]
        self.assertEqual(len(running), 27)
        self.assertEqual(schedule["stats"]["source"], "mini app")

    def test_without_signature_result_is_verified(self):
        started = self.post("/fast/start", {"days": 1, "force": True}).get_json()
        self.assertFalse(started["trusted"])
        lists = self.send_pages(started["session"], started["tasks"])
        self.send_pages(started["session"], lists["tasks"])
        self.post("/fast/finish", {"session": started["session"]})
        st = self.wait_build()
        self.assertTrue(st["ok"], st["message"])
        self.assertEqual(len(self.checked), 1, "без підпису Telegram дані треба звірити з сайтом")

    def test_forged_pages_are_rejected(self):
        self.verdict = (False, "2026-09-14: серед присланих даних немає поїздів ['28152']")
        started = self.post("/fast/start", {"days": 1, "force": True}).get_json()
        lists = self.send_pages(started["session"], started["tasks"])
        self.send_pages(started["session"], lists["tasks"])
        self.post("/fast/finish", {"session": started["session"]})
        st = self.wait_build()
        self.assertFalse(st["ok"])
        self.assertIn("не збіглися", st["message"])
        self.verdict = (True, "тест")

    def run_cycle(self):
        started = self.post("/fast/start", {"days": 1, "force": True}).get_json()
        lists = self.send_pages(started["session"], started["tasks"])
        self.send_pages(started["session"], lists["tasks"])
        self.post("/fast/finish", {"session": started["session"]})
        return self.wait_build()

    def test_unverifiable_but_plausible_result_is_committed(self):
        """Сайт не відповів сервісу: дані приймаємо, якщо вони не суперечать попереднім."""
        self.verdict = (None, "сайт УЗ не відповів сервісу")
        gateway.GH_TOKEN = "fake"
        saved = []
        gateway.gh_get_file = lambda path: ({"trains": {str(i): {} for i in range(29)},
                                             "days": {}}, "sha1") if path.endswith("schedule.json") else (None, None)
        gateway.gh_put_file = lambda path, *a, **kw: saved.append(path)
        try:
            st = self.run_cycle()
            self.assertTrue(st["ok"], st["message"])
            self.assertTrue(st["committed"])
            self.assertIn("schedule.json", saved)
        finally:
            gateway.GH_TOKEN = ""

    def test_unverifiable_and_implausible_result_is_not_committed(self):
        """Якщо розклад раптом схуд удвічі, а звірити з сайтом не вийшло, не зберігаємо."""
        self.verdict = (None, "сайт УЗ не відповів сервісу")
        gateway.GH_TOKEN = "fake"
        gateway.gh_get_file = lambda path: ({"trains": {str(i): {} for i in range(100)},
                                             "days": {}}, "sha1") if path.endswith("schedule.json") else (None, None)
        gateway.gh_put_file = lambda *a, **kw: self.fail("сумнівні дані комітити не можна")
        try:
            st = self.run_cycle()
            self.assertTrue(st["ok"], st["message"])
            self.assertFalse(st["committed"])
            self.assertIn("перевірка даних", st["message"])
        finally:
            gateway.GH_TOKEN = ""

    def test_spot_check_compares_with_what_service_sees(self):
        session = fastbuild.Session(today=gateway.datetime.now(gateway.KYIV).date(), horizon=1)
        day = list(session.state()["tasks"])[2]["id"].split(":")[-1]
        schedule = {"days": {day: {"running": ["1", "2", "3"]}}}
        session.check_ready.set()

        session.check = (day, {"1", "2"}, None)
        self.assertTrue(self.real_spot_check(session, schedule)[0], "усі поїзди сайту є в даних")

        session.check = (day, {"1", "9"}, None)
        self.assertFalse(self.real_spot_check(session, schedule)[0], "поїзда 9 бракує: підозріло")

        session.check = (day, None, "сайт УЗ зайнятий (HTTP 522)")
        self.assertIsNone(self.real_spot_check(session, schedule)[0], "збій мережі не є доказом підробки")

        session.check = ("1999-01-01", {"1"}, None)
        self.assertIsNone(self.real_spot_check(session, schedule)[0], "дати немає в розкладі")

    def test_sanity_check(self):
        old = {"trains": {str(i): {} for i in range(29)},
               "days": {"2026-09-15": {"running": [str(i) for i in range(27)]}}}
        same = {"trains": {str(i): {} for i in range(29)},
                "days": {"2026-09-15": {"running": [str(i) for i in range(26)]}}}
        self.assertTrue(gateway.sanity_check(old, same)[0])
        self.assertTrue(gateway.sanity_check(None, same)[0])
        few_trains = {"trains": {str(i): {} for i in range(10)}, "days": {}}
        self.assertFalse(gateway.sanity_check(old, few_trains)[0])
        empty_day = {"trains": {str(i): {} for i in range(29)},
                     "days": {"2026-09-15": {"running": []}}}
        self.assertFalse(gateway.sanity_check(old, empty_day)[0])

    def test_lost_pages_are_accepted_and_day_is_skipped(self):
        started = self.post("/fast/start", {"days": 2, "force": True,
                                            "initData": telegram_init_data()}).get_json()
        lost = {"pair:2528:2538:" + started["tasks"][-1]["id"].split(":")[-1]}
        lists = self.send_pages(started["session"], started["tasks"], skip=lost)
        self.assertEqual(lists["failed"], sorted(lost))
        self.send_pages(started["session"], lists["tasks"])
        self.post("/fast/finish", {"session": started["session"]})
        st = self.wait_build()
        self.assertTrue(st["ok"], st["message"])
        schedule = self.client.get("/schedule.json").get_json()
        self.assertEqual(len(schedule["skipped_days"]), 1)

    def test_worker_token_is_trusted(self):
        """Cloudflare Worker качає сторінки сам, тому його даним можна вірити без звірки."""
        gateway.FAST_TOKEN = "секрет-воркера"
        try:
            ours = self.post("/fast/start", {"days": 1, "force": True}, token="секрет-воркера").get_json()
            self.assertTrue(ours["trusted"])
            gateway.fast_sessions = fastbuild.SessionStore()
            gateway.state.update(finished=None)
            stranger = self.post("/fast/start", {"days": 1, "force": True}, token="інший").get_json()
            self.assertFalse(stranger["trusted"])
        finally:
            gateway.FAST_TOKEN = ""

    def test_page_route_takes_raw_body(self):
        """Воркер ллє сторінку сирим тілом, бо на розбір у нього немає процесорного часу."""
        started = self.post("/fast/start", {"days": 1, "force": True}).get_json()
        session = started["session"]
        page = page_for("pair:2528:2538:").encode("utf-8")
        r = self.client.post(f"/fast/page?session={session}&id=pair:2528:2538:",
                             data=page, headers={"Content-Type": "text/html; charset=utf-8"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["pending"], 3)

        packed = gzip.compress(page_for("pair:2538:2528:").encode("utf-8"))
        r = self.client.post(f"/fast/page?session={session}&id=pair:2538:2528:",
                             data=packed, headers={"Content-Type": "text/html", "X-Gzip": "1"})
        self.assertEqual(r.get_json()["pending"], 2)

        bad = self.client.post(f"/fast/page?session={session}&id=pair:2528:2538:2026-09-14",
                               data=b"<html>gateway error</html>", headers={"Content-Type": "text/html"})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("розклад УЗ", bad.get_json()["error"])

    def test_state_route_reports_remaining_work(self):
        started = self.post("/fast/start", {"days": 1, "force": True}).get_json()
        session = started["session"]
        for task in started["tasks"]:
            self.client.post(f"/fast/page?session={session}&id={task['id']}",
                             data=page_for(task["id"]).encode("utf-8"),
                             headers={"Content-Type": "text/html"})
        state = self.client.get(f"/fast/state?session={session}").get_json()
        self.assertEqual(state["phase"], fastbuild.PHASE_TRAINS)
        self.assertTrue(all(t["id"].startswith("train:") for t in state["tasks"]))
        self.assertEqual(self.client.get("/fast/state?session=нема").status_code, 400)

    def test_due_follows_slots(self):
        def with_generated(value):
            gateway.gh_get_file = lambda path: (({"generated": value}, "sha")
                                                if path.endswith("schedule.json") else (None, None))

        with_generated("2020-01-01T06:00:00+02:00")
        old = self.client.get("/due").get_json()
        self.assertTrue(old["due"], old)

        with_generated(gateway.datetime.now(gateway.KYIV).isoformat(timespec="seconds"))
        fresh = self.client.get("/due").get_json()
        self.assertFalse(fresh["due"], fresh)

        gateway.gh_get_file = lambda path: (None, None)
        empty = self.client.get("/due").get_json()
        self.assertTrue(empty["due"])

    def test_unknown_session(self):
        r = self.post("/fast/pages", {"session": "нема", "pages": []})
        self.assertEqual(r.status_code, 400)
        self.assertIn("сесія", r.get_json()["error"])

    def test_too_many_pages_in_one_batch(self):
        started = self.post("/fast/start", {"days": 1, "force": True}).get_json()
        pages = [{"id": "pair:2528:2538:", "html": "<html></html>"}] * (fastbuild.MAX_PAGES_PER_BATCH + 1)
        r = self.post("/fast/pages", {"session": started["session"], "pages": pages})
        self.assertEqual(r.status_code, 400)

    def test_page_without_uz_marker_is_rejected(self):
        """Сторінка помилки шлюзу не повинна зараховуватись: інакше вийде «поїздів немає»."""
        started = self.post("/fast/start", {"days": 1, "force": True}).get_json()
        r = self.post("/fast/pages", {"session": started["session"],
                                      "pages": [{"id": "pair:2528:2538:", "html": "<html>gateway error</html>"},
                                                {"id": "pair:2538:2528:", "html": page_for("pair:2538:2528:")}]})
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["received"], 1)
        self.assertEqual(len(body["rejected"]), 1)
        self.assertIn("pair:2528:2538:", [t["id"] for t in body["tasks"]])

    def test_broken_json_body(self):
        r = self.client.post("/fast/start", data=b"{broken", headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 400)

    def test_too_soon_without_force(self):
        gateway.state["finished"] = gateway.datetime.now(gateway.KYIV).isoformat(timespec="seconds")
        body = self.post("/fast/start", {"days": 1}).get_json()
        self.assertEqual(body["status"], "too_soon")
        self.assertGreater(body["wait_seconds"], 0)

    def test_cors_headers_allow_gzip_marker(self):
        r = self.client.open("/fast/pages", method="OPTIONS")
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], "*")
        self.assertIn("X-Gzip", r.headers["Access-Control-Allow-Headers"])


if __name__ == "__main__":
    unittest.main()
