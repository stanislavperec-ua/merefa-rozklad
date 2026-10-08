# -*- coding: utf-8 -*-
"""Хук Claude Code: не пускає `git push`, поки швидкі тести не пройдуть.

Навіщо: Render розгортає кожен коміт у main автоматично, тож помилка потрапляє в живого
бота за сорок секунд, а Mini App тим часом лишається без оновлень. Дешевше зупинити push.

Як працює: на stdin приходить JSON події PreToolUse. Якщо це Bash із `git push`, женемо
tests.test_fast і tests.test_fast_api (59 тестів, близько 22 с, у мережу не ходять).
Решту команд пропускаємо миттєво.

Коди виходу: 0 дозволити, 2 заблокувати (stderr повертається в Claude). Якщо тести не
вдалося навіть запустити, push дозволяємо: хук не повинен ставати на заваді роботі.
Разово пропустити перевірку: змінна оточення SKIP_PUSH_TESTS=1.

Вмикається в `.claude/settings.json` папки, з якої відкрито сесію (див. CLAUDE.md).
"""
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAST_TESTS = ["tests.test_fast", "tests.test_fast_api"]
PUSH = re.compile(r"\bgit\s+(?:-\S+\s+)*push\b")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # тести не блимають вікнами на екрані
TAIL_LINES = 25


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return 0                      # не наша подія, мовчки пропускаємо
    if event.get("tool_name") != "Bash":
        return 0
    command = str((event.get("tool_input") or {}).get("command") or "")
    if not PUSH.search(command):
        return 0
    if os.environ.get("SKIP_PUSH_TESTS") == "1":
        print("SKIP_PUSH_TESTS=1: тести перед push пропущено")
        return 0

    try:
        run = subprocess.run([sys.executable, "-m", "unittest", *FAST_TESTS, "-q"],
                             cwd=REPO, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=300, creationflags=NO_WINDOW)
    except Exception as e:  # noqa: BLE001
        print(f"Швидкі тести не запустилися ({e}), push дозволено без перевірки")
        return 0

    if run.returncode == 0:
        print("Швидкі тести пройшли, push дозволено")
        return 0

    sys.stderr.write("Push зупинено: швидкі тести впали. Полагодь тести або постав "
                     "SKIP_PUSH_TESTS=1, якщо падіння свідоме.\n"
                     + "\n".join(important((run.stderr or "") + (run.stdout or ""))) + "\n")
    return 2


def important(output: str) -> list[str]:
    """Лише суть падіння: тести пишуть у stderr ще й свої логи, у яких воно губиться."""
    lines = output.replace("\r\n", "\n").strip().splitlines()
    first = next((i for i, s in enumerate(lines) if s.startswith("====")), 0)
    shown = lines[first:first + TAIL_LINES]
    for s in lines[-3:]:                       # підсумок «Ran N tests / FAILED»
        if s.startswith(("Ran ", "FAILED", "OK")) and s not in shown:
            shown.append(s)
    return shown


if __name__ == "__main__":
    sys.exit(main())
