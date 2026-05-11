"""分段版 SRT 校正 — 避開 gemini-2.5-flash 的 8192 output token 上限。

策略:
- 整段音訊上傳一次(File API 重用)
- SRT 切成 4 段 × ~100 blocks
- 每段獨立 call(input 仍夠看到完整音訊)
- 合併輸出
"""

import os, re, sys, time, difflib
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

BLOCKS_PER_CHUNK = 100   # 安全:1 block ≈ 60 chars × 100 ≈ 6000 chars output

GLOSSARY = """
Gemini CLI / Gemini API / Gemini 2.5 Flash / Google AI Studio / Gemma /
Node.js / npm / Python / pip / venv / 虛擬環境 / 第三方庫 /
ChatGPT / GPT / Claude / Anthropic /
CMD / PowerShell / VS Code / Visual Studio Code / 終端機 /
API key / 金鑰 / Token / 環境變數 / .env /
Git / GitHub
"""


def parse_srt(text: str) -> list[str]:
    """以空行切 blocks。每塊保持原樣(含尾換行符)。"""
    blocks = []
    cur = []
    for line in text.splitlines():
        if line.strip() == "":
            if cur:
                blocks.append("\n".join(cur))
                cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def make_instruction(start_idx: int, end_idx: int, chunk_text: str) -> str:
    return f"""你的任務是校正下面這段 SRT(對著音檔重聽,修正錯字與專有名詞)。

【規則 — 100% 嚴守】
1. **時間軸絕對不要改**(`hh:mm:ss,ms --> hh:mm:ss,ms` 原樣保留)
2. **行號絕對不要改**(每塊開頭的數字)
3. **每塊之間的空行保留**
4. **只修改字幕文字**(每塊第三行起的內容)
5. **塊數量不變** — 輸入 {end_idx - start_idx + 1} 個 blocks(編號 {start_idx} 到 {end_idx}),輸出也必須是 {end_idx - start_idx + 1} 個

【常見錯誤】
- 「Gameli / GEMALA / GEMALY / Gamala / Germanize / GEMICLI / GEMI」全是 Gemini CLI 的 ASR 錯誤 → 改回 **Gemini CLI**
- 「CND」應該是 **CMD**
- 「2.0 fully」/ 「2.5 fully」 → **2.0 Flash** / **2.5 Flash**
- 中文錯字、同音字、近音字也修
- 標點該斷句的地方斷,不亂加 markdown

【Glossary】
{GLOSSARY}

【輸出格式】
**只輸出校正後的 SRT,從 block {start_idx} 到 block {end_idx}**:
- 不要加任何前言、解釋、總結、markdown 標記、code fence
- 第一個字元應該是 "{start_idx}"

=== 原 SRT (blocks {start_idx}–{end_idx}) ===
{chunk_text}
"""


def upload_audio(client, path: Path):
    print(f"  [upload] {path.name} ({path.stat().st_size/1024/1024:.1f}MB)...")
    f = client.files.upload(file=str(path))
    while f.state.name == "PROCESSING":
        time.sleep(2)
        f = client.files.get(name=f.name)
    if f.state.name != "ACTIVE":
        raise RuntimeError(f"state={f.state.name}")
    return f


def correct_chunk(client, audio_file, start_idx, end_idx, chunk_text):
    prompt = make_instruction(start_idx, end_idx, chunk_text)
    t0 = time.time()
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[audio_file, prompt],
        config=types.GenerateContentConfig(
            system_instruction="你是專業字幕校對員。輸出嚴格符合格式。",
            temperature=0.1,
            max_output_tokens=8000,
        ),
    )
    elapsed = time.time() - t0
    text = (resp.text or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text, elapsed


def main():
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    srt_text = SRT_IN.read_text(encoding="utf-8")
    blocks = parse_srt(srt_text)
    n = len(blocks)
    print(f"原 SRT: {n} blocks, {len(srt_text)} 字元")

    audio_file = upload_audio(client, AUDIO)

    corrected_blocks: list[str] = []
    n_chunks = (n + BLOCKS_PER_CHUNK - 1) // BLOCKS_PER_CHUNK
    print(f"分 {n_chunks} 段 × ~{BLOCKS_PER_CHUNK} blocks")

    for chunk_i in range(n_chunks):
        s = chunk_i * BLOCKS_PER_CHUNK
        e = min(s + BLOCKS_PER_CHUNK, n)
        # blocks 是 0-indexed,SRT 編號是 1-indexed → +1
        srt_start = s + 1
        srt_end = e  # last index is e-1, SRT編號 = e-1+1 = e
        chunk_blocks = blocks[s:e]
        chunk_text = "\n\n".join(chunk_blocks)
        print(f"[chunk {chunk_i+1}/{n_chunks}] blocks {srt_start}-{srt_end}...", end=" ", flush=True)
        text, elapsed = correct_chunk(client, audio_file, srt_start, srt_end, chunk_text)
        out_blocks = parse_srt(text)
        if len(out_blocks) != (e - s):
            print(f"⚠️ 期望 {e-s} 塊,得到 {len(out_blocks)} 塊")
        else:
            print(f"OK ({elapsed:.0f}s)")
        corrected_blocks.extend(out_blocks)

    combined = "\n\n".join(corrected_blocks) + "\n"
    SRT_OUT.write_text(combined, encoding="utf-8")
    print(f"\n校正後檔: {SRT_OUT.name} ({len(combined)} 字元 / {len(corrected_blocks)} blocks)")

    # diff
    diff = list(difflib.unified_diff(
        srt_text.splitlines(), combined.splitlines(),
        fromfile="原 SRT", tofile="校正後", lineterm="", n=2,
    ))
    DIFF_OUT.write_text("\n".join(diff), encoding="utf-8")
    n_add = sum(1 for L in diff if L.startswith("+") and not L.startswith("+++"))
    n_del = sum(1 for L in diff if L.startswith("-") and not L.startswith("---"))
    print(f"Diff:    {DIFF_OUT.name} (+{n_add} / -{n_del} 行)")


if __name__ == "__main__":
    main()
