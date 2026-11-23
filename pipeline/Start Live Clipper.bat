@echo off
REM Double-click to watch one Twitch channel. Clips go to paths.clips_dir in config.toml (default: the clips folder here).
cd /d "%~dp0"
set /p CHANNEL=Twitch channel name (e.g. xqc), then press Enter:
if not exist ".venv\Scripts\python.exe" (
  echo Set up first: see the README.
  pause
  exit /b 1
)
if "%CHANNEL%"=="" (
  echo No channel entered.
  pause
  exit /b 1
)
REM "base" captions are fast on a CPU. Change to "small" for better accuracy (slower, +300 MB download).
REM Clips: 18 s before the moment, 8 s after. If chat stays hyped, the clip keeps going (up to 25 s more).
REM Add --realism to also clip moments chat calls realistic (graphics, physics, details) as full-screen gameplay.
REM keepalive.py restarts the clipper after connection drops while the channel is still live (up to 12 hours).
".venv\Scripts\python.exe" keepalive.py %CHANNEL% --hours 12 -- --model base --before 18 --after 8 --max-extend 25 --cooldown 180
echo.
echo Finished. Your clips are in the folder set by paths.clips_dir in config.toml (default: clips).
pause
