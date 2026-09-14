@echo off
rem Update the official UZ timetable from this PC and publish it to GitHub.
rem Needed because swrailway.gov.ua does not answer cloud networks (GitHub Actions, Render).
rem Run: double-click, or "update_schedule.cmd" in a console. Requires Python 3.11+ and git.
rem (ASCII only: cmd reads batch files in the OEM code page, Cyrillic here breaks the parser.)
chcp 65001 >nul
cd /d "%~dp0"

echo === UZ timetable (swrailway.gov.ua) ===
python build_schedule.py --force --horizon 14
if errorlevel 1 (
  echo.
  echo FAILED: UZ site unavailable or build error. schedule.json left unchanged.
  pause
  exit /b 1
)

echo.
echo === Publish ===
git add schedule.json trains_cache.json
git diff --staged --quiet
if not errorlevel 1 (
  echo Timetable unchanged, nothing to publish.
  pause
  exit /b 0
)
git -c user.name=stanislavperec-ua -c user.email=265459095+stanislavperec-ua@users.noreply.github.com commit -q -m "Timetable update (PC) %date% %time:~0,5%"
git pull --rebase -q origin main
git push -q origin main
if errorlevel 1 (
  echo FAILED: could not push to GitHub. Check the connection and run again.
  pause
  exit /b 1
)
echo DONE: schedule.json published, the app picks it up in 1-2 minutes.
pause
