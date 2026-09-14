@echo off
rem Оновлення офіційного розкладу УЗ з цього ПК і публікація в GitHub.
rem Потрібно, бо swrailway.gov.ua не відповідає з хмарних мереж (GitHub Actions, Render).
rem Запуск: подвійний клік або "update_schedule.cmd" у консолі. Потрібні Python 3.11+ і git.
chcp 65001 >nul
cd /d "%~dp0"

echo === Розклад УЗ ===
python build_schedule.py --force --horizon 14
if errorlevel 1 (
  echo.
  echo Сайт УЗ недоступний або помилка збірки. schedule.json не змінено.
  pause
  exit /b 1
)

echo.
echo === Публікація ===
git add schedule.json trains_cache.json
git diff --staged --quiet && (
  echo Розклад не змінився, публікувати нічого.
  pause
  exit /b 0
)
git -c user.name=stanislavperec-ua -c user.email=265459095+stanislavperec-ua@users.noreply.github.com commit -q -m "Timetable update (PC) %date% %time:~0,5%"
git pull --rebase -q origin main
git push -q origin main
if errorlevel 1 (
  echo Не вдалося відправити в GitHub. Перевірте з'єднання і запустіть ще раз.
  pause
  exit /b 1
)
echo Готово: schedule.json опубліковано, застосунок оновиться за 1-2 хвилини.
pause
