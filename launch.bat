@echo off
REM SRT 字幕工具 — 啟動 GUI
cd /d "%~dp0"

if not exist .venv (
    echo 找不到 .venv,請先跑 setup.bat
    pause
    exit /b 1
)

if not exist .env (
    echo 找不到 .env,請複製 .env.example 為 .env 並填 API key
    pause
    exit /b 1
)

start "" .venv\Scripts\pythonw.exe srt_corrector_gui.py
