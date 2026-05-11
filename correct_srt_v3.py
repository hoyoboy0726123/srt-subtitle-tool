"""v3 — JSON 模式 SRT 校正

策略:
- 本地 parse SRT → {idx, ts, text}
- model 只看 audio + (index, text) list,回 JSON list of corrected text
- 本地拼回原 SRT 結構(時間軸 + 行號 100% 保留)
- 結構絕對不會壞;model 也不會偷刪 block
"""

import json, os, re, sys, time, difflib
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

BLOCKS_PER_CHUNK = 80

GLOSSARY = """
Gemini CLI / Gemini API / Gemini 2.5 Flash / Google AI Studio / Gemma /
Node.js / npm / Python / pip / venv / 虛擬環境 / 第三方庫 /
ChatGPT / GPT / Claude / Anthropic /
CMD / PowerShell / VS Code / Visual Studio Code / 終端機 /
API key / 金鑰 / Token / 環境變數 / .env / Git / GitHub
"""

TS_RE = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$")


def parse_srt(text: str):
    """Return list of (idx_int, timestamp_line, text_lines:list[str])."""
    blocks = []
    cur_lines = []
    for line in text.splitlines():
        if line.strip() == "":
            if cur_lines:
                blocks.append(cur_lines)
                cur_lines = []
        else:
            cur_lines.append(line)
    if cur_lines:
        blocks.append(cur_lines)

    parsed = []
    for b in blocks:
        if len(b) < 3:
            continue
        try:
            idx = int(b[0].strip())
        except ValueError:
            continue
        ts = b[1] if TS_RE.match(b[1]) else None
        if ts is None:
            continue
        text_lines = b[2:]
        parsed.append((idx, ts, text_lines))
    return parsed


def reassemble(parsed):
    out = []
    for idx, ts, lines in parsed:
        out.append(str(idx))
        out.append(ts)
        out.extend(lines)
        out.append("")
    return "\n".join(out)


def build_prompt(start_idx, end_idx, items):
    """items: list of {i, text}"""
    items_json = json.dumps(items, ensure_ascii=False, indent=2)
    return f"""你是專業字幕校對員。對著音檔重聽,把下列字幕逐句校正(錯字 / 專有名詞 / 同音字)。

【常見錯誤】
- 「Gameli / GEMALA / GEMALY / Gamala / Germanize / GEMICLI / GEMI / 加米利 / 嘉米利」等等都是 ASR 把 **Gemini CLI** 聽錯,全部改回 **Gemini CLI**
- 「CND」應該是 **CMD**(Windows 命令提示字元)
- 「2.0 fully / 2.5 fully」應該是 **2.0 Flash / 2.5 Flash**
- 中文錯字、同音字、近音字也修
- 標點該斷句的地方斷

【Glossary — 確保這些詞拼字正確】
{GLOSSARY}

【輸入】index {start_idx} 到 {end_idx} 共 {len(items)} 個字幕條目(JSON):
```json
{items_json}
```

【輸出格式 — 絕對嚴守】
**只輸出一個 JSON array,長度必須是 {len(items)}**:
```json
[
  {{"i": {start_idx}, "text": "校正後文字"}},
  {{"i": {start_idx+1}, "text": "校正後文字"}},
  ...
]
```

規則:
1. **每個 i 必須跟輸入的 i 一一對應、不能少、不能多、順序不變**
2. **不要合併或拆分條目** — 進來幾個就出去幾個
3. **不要加任何前言、解釋、markdown 標題**,只回 JSON
4. 字幕內容如果原本是 multi-line(條目有換行),回覆 `text` 用 `\\n` 表示換行
"""


def upload_audio(client, path):
    print(f"  [upload] {path.name} ({path.stat().st_size/1024/1024:.1f}MB)...")
    f = client.files.upload(file=str(path))
    while f.state.name == "PROCESSING":
        time.sleep(2)
        f = client.files.get(name=f.name)
    if f.state.name != "ACTIVE":
        raise RuntimeError(f"state={f.state.name}")
    return f


def extract_json_array(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()
    # 試直接 parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 嘗試找 [ 到最後 ] 的最長 array
    m = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def correct_chunk(client, audio_file, items, start_idx, end_idx):
    prompt = build_prompt(start_idx, end_idx, items)
    t0 = time.time()
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[audio_file, prompt],
        config=types.GenerateContentConfig(
            system_instruction="你是極度遵守格式的字幕校對員。",
            temperature=0.0,
            max_output_tokens=8000,
            response_mime_type="application/json",
        ),
    )
    elapsed = time.time() - t0
    arr = extract_json_array(resp.text or "")
    return arr, elapsed, (resp.text or "")


def main():
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    srt_text = SRT_IN.read_text(encoding="utf-8")
    parsed = parse_srt(srt_text)
    print(f"原 SRT: {len(parsed)} blocks")

    audio_file = upload_audio(client, AUDIO)

    # build a flat list of items
    items_all = [{"i": idx, "text": "\n".join(lines)} for idx, _, lines in parsed]
    n = len(items_all)
    n_chunks = (n + BLOCKS_PER_CHUNK - 1) // BLOCKS_PER_CHUNK
    print(f"分 {n_chunks} 段 × ~{BLOCKS_PER_CHUNK} blocks\n")

    corrected_map: dict[int, str] = {}

    for chunk_i in range(n_chunks):
        s = chunk_i * BLOCKS_PER_CHUNK
        e = min(s + BLOCKS_PER_CHUNK, n)
        chunk = items_all[s:e]
        start_idx, end_idx = chunk[0]["i"], chunk[-1]["i"]
        print(f"[chunk {chunk_i+1}/{n_chunks}] {start_idx}-{end_idx}...", end=" ", flush=True)

        arr, elapsed, raw = correct_chunk(client, audio_file, chunk, start_idx, end_idx)

        if not isinstance(arr, list):
            print(f"❌ 無 JSON: raw 前 100 字: {raw[:100]!r}")
            # fallback:保留原文
            for item in chunk:
                corrected_map[item["i"]] = item["text"]
            continue

        # 把 arr 塞進 corrected_map
        got = 0
        for entry in arr:
            try:
                i = int(entry.get("i"))
                t = str(entry.get("text", "")).strip()
                if t:
                    corrected_map[i] = t
                    got += 1
            except (TypeError, ValueError):
                continue

        # 對沒回的 fallback 原文
        missing = 0
        for item in chunk:
            if item["i"] not in corrected_map:
                corrected_map[item["i"]] = item["text"]
                missing += 1
        status = f"OK ({elapsed:.0f}s, {got}/{len(chunk)})"
        if missing:
            status += f" ⚠️ {missing} fallback"
        print(status)

    # 拼回 SRT
    new_parsed = []
    for idx, ts, lines in parsed:
        new_text = corrected_map.get(idx, "\n".join(lines))
        # 處理 \n 轉真換行
        new_lines = new_text.split("\n")
        new_parsed.append((idx, ts, new_lines))
    combined = reassemble(new_parsed)
    SRT_OUT.write_text(combined, encoding="utf-8")
    print(f"\n校正後檔: {SRT_OUT.name} ({len(combined)} 字元 / {len(new_parsed)} blocks)")

    # diff
    diff = list(difflib.unified_diff(
        srt_text.splitlines(), combined.splitlines(),
        fromfile="原 SRT", tofile="校正後", lineterm="", n=1,
    ))
    DIFF_OUT.write_text("\n".join(diff), encoding="utf-8")
    n_add = sum(1 for L in diff if L.startswith("+") and not L.startswith("+++"))
    n_del = sum(1 for L in diff if L.startswith("-") and not L.startswith("---"))
    print(f"Diff:    {DIFF_OUT.name} (+{n_add} / -{n_del} 行)")


if __name__ == "__main__":
    main()
