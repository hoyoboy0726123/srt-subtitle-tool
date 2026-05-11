@echo off
REM SRT 字幕工具 — 一鍵安裝(建 venv + 裝套件)
REM 第一次跑這個建立環境

cd /d "%~dp0"

echo === 1/3 建立 .venv ===
python -m venv .venv
if errorlevel 1 (
    echo 失敗:請先裝 Python 3.11+ 並加進 PATH
    pause
    exit /b 1
)

echo === 2/3 升級 pip ===
call .venv\Scripts\python.exe -m pip install --upgrade pip

echo === 3/3 安裝依賴 ===
call .venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo 失敗:requirements.txt 安裝出錯
    pause
    exit /b 1
)

echo.
echo === 完成 ===
echo 下一步:
echo   1. 確認 .env 存在且填了 GEMINI_API_KEY / GROQ_API_KEY
echo   2. 雙擊 launch.bat 啟動 GUI
echo.
pause
