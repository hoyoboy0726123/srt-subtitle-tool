"""用 gemini-2.5-flash 把 SRT 對著音檔校正。

策略:
- 上傳音訊到 Gemini File API
- 一次 call:audio + 原 SRT + glossary → 校正後 SRT
- 時間軸 100% 保留,只動文字
- 輸出新檔 + diff 給 user 看
"""

import os
import sys
import time
import difflib
from pathlib import Path

sys.path.insert(0, r"C:/Users/GU605_PR_MZ/Report/AI-")
from dotenv import load_dotenv
load_dotenv(r"C:/Users/GU605_PR_MZ/Report/AI-/.env")

from google import genai
from google.genai import types

SIDE = Path(r"C:/Users/GU605_PR_MZ/Report/Side_project")
AUDIO = SIDE / "full_audio.mp3"
SRT_IN = SIDE / "未命名設計 2_20260510_233338.srt"
SRT_OUT = SIDE / "未命名設計 2_20260510_233338.corrected.srt"
DIFF_OUT = SIDE / "srt_diff.txt"

# Proper nouns / 術語 anchor — 大致掃過 SRT 後猜測的詞彙
GLOSSARY = """
Gemini CLI / Gemini API / Google AI Studio / Gemini 2.5 Flash / Gemma /
Node.js / npm / Python / pip / venv / 虛擬環境 /
ChatGPT / GPT / Claude / Anthropic /
CMD / PowerShell / VS Code / Visual Studio Code / 終端機 /
API key / 金鑰 / Token / 環境變數 / .env /
Git / GitHub /
Ctrl+C / Ctrl+V / Ctrl+Shift+M / Enter /
JavaScript / TypeScript /
.json / .py / .md
"""

INSTRUCTION = """你的任務是校正下面這份 SRT 字幕(對著音檔重聽,修正錯字與專有名詞)。

【規則 — 100% 嚴守】
1. **時間軸絕對不要改**(`00:00:01,279 --> 00:00:13,240` 這種 timestamp 行原樣保留)
2. **行號絕對不要改**(每塊開頭的數字 1 / 2 / 3 ...)
3. **每塊之間的空行保留**
4. **只修改字幕文字**(每塊第三行起的內容)

【常見錯誤要重點修】
- ASR 把英文專有名詞聽錯(下方有 glossary 列表)。看到任何「Gameli / GEMALA / GEMALY / Gamala / Germanize / GEMICLI / GEMI」等等都是 **Gemini CLI** 的 ASR 錯誤,全部改回 **Gemini CLI**
- 「CND」應該是 **CMD**(Windows 命令提示字元)
- 「2.0 fully」這種應該是 **2.0 Flash** / 「2.5 fully」 → **2.5 Flash**
- 中文錯字也修(同音字、近音字)
- 標點符號保持自然,該斷句的地方就斷
- 不確定的字直接保留原文,**不要刪掉內容**

【Glossary — 這些詞請確保拼字正確】
""" + GLOSSARY + """

【輸出格式】
**只輸出校正後的完整 SRT 內容**,從第一塊到最後一塊。
- 不要加任何前言、解釋、總結
- 不要包在 ```srt 或 ``` 程式碼 fence 內
- 不要加任何 markdown 標記
- 第一個字元應該是 "1"(第一塊的編號)

下面是原 SRT,請對照音檔逐字校正後重新輸出:

=== 原 SRT ===
"""


def upload_audio(client, path: Path):
    print(f"[1/4] 上傳音訊 ({path.stat().st_size / 1024 / 1024:.1f} MB)...")
    audio_file = client.files.upload(file=str(path))
    while audio_file.state.name == "PROCESSING":
        time.sleep(2)
        audio_file = client.files.get(name=audio_file.name)
    print(f"     狀態: {audio_file.state.name}")
    if audio_file.state.name != "ACTIVE":
        raise RuntimeError(f"upload state {audio_file.state.name}")
    return audio_file


def main():
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    if not AUDIO.exists():
        print(f"missing: {AUDIO}"); sys.exit(1)
    srt_text = SRT_IN.read_text(encoding="utf-8")
    print(f"原 SRT: {len(srt_text)} 字元")

    audio_file = upload_audio(client, AUDIO)

    full_prompt = INSTRUCTION + srt_text

    print("[2/4] 呼 gemini-2.5-flash 校正中...")
    t0 = time.time()
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[audio_file, full_prompt],
        config=types.GenerateContentConfig(
            system_instruction="你是專業的字幕校對員,精準度極高。",
            temperature=0.1,
            max_output_tokens=32000,
        ),
    )
    elapsed = time.time() - t0
    print(f"     完成 ({elapsed:.1f}s)")

    corrected = (resp.text or "").strip()
    # 去除可能的 fence
    if corrected.startswith("```"):
        lines = corrected.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        corrected = "\n".join(lines)

    print("[3/4] 寫入新檔...")
    SRT_OUT.write_text(corrected, encoding="utf-8")
    print(f"     {SRT_OUT.name} ({len(corrected)} 字元)")

    print("[4/4] 產 diff 報告...")
    original_lines = srt_text.splitlines()
    new_lines = corrected.splitlines()
    diff = difflib.unified_diff(
        original_lines, new_lines,
        fromfile="原 SRT", tofile="校正後",
        lineterm="", n=2,
    )
    diff_text = "\n".join(diff)
    DIFF_OUT.write_text(diff_text, encoding="utf-8")
    n_changes = sum(1 for L in diff_text.splitlines() if L.startswith("+") and not L.startswith("+++"))
    print(f"     {DIFF_OUT.name} ({n_changes} 行新增/修改)")
    print()
    print("=" * 60)
    print(f"完成。校正後檔: {SRT_OUT}")
    print(f"      Diff 報告: {DIFF_OUT}")


if __name__ == "__main__":
    main()
