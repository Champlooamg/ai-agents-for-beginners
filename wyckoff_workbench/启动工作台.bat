@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m wyckoff_cn serve --open-browser
) else (
  python -m wyckoff_cn serve --open-browser
)
pause
