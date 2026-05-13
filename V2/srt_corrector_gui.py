"""SRT 字幕校正 GUI — Gemini 對著音檔/影片重聽校正字幕。

特色:
- 支援影片(.mp4/.mkv/.mov/.avi/.webm/.flv/.wmv)或音檔(.mp3/.wav/.m4a/.aac/.flac/.ogg/.opus/.wma)
- 影片自動抽音軌(imageio-ffmpeg)
- 一鍵把 ASR 錯字 / 專有名詞(Gemini CLI / Node.js / 等)校回正確
- SQLite 持久追蹤每日 RPD(跨 session 累積,PT 午夜重置)
- GUI 即時顯示「今日已用 X / RPD cap」+ 彩色 log(警告黃 / 錯誤紅 / 成功綠)
- chunk 處理避開 8K output token 上限
- 結構 100% 保留(時間軸不動)

預設模型 = gemini-2.5-flash-lite(實測逐字稿密度最佳:4K RPM / Unlimited RPD / 4M TPM)。
*註*:3.1-flash-lite 雖然更新,但在長音檔轉錄上會偷懶/摘要,長視頻實測 block 密度只有 2.5 的 ~45%。
*註*:dashboard 寫的 "Unlimited RPD" 不是真無限 — 還是被 RPM (4K/min) 和 TPM (4M/min) 雙重卡住,Google 還有反濫用隱性機制。
模型清單以 Michael AI Studio dashboard 實際 quota 為準,Gemma 4 全系列不支援 audio 因此不列入。
"""

import json, os, re, sys, time, threading, queue, difflib, sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from tkinter import filedialog, messagebox

import customtkinter as ctk
from dotenv import load_dotenv

# ============================================================
# 設定
# ============================================================

VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".wmv")
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma")
MEDIA_EXTS = VIDEO_EXTS + AUDIO_EXTS

# 媒體轉檔格式 — 友善名稱 → (副檔名, ffmpeg 編碼參數)
OUTPUT_FORMATS = {
    "mp3 · 音訊 (192k)":        (".mp3",  ["-vn", "-acodec", "libmp3lame", "-ab", "192k"]),
    "wav · 音訊 (無損)":         (".wav",  ["-vn", "-acodec", "pcm_s16le"]),
    "m4a · 音訊 AAC (192k)":    (".m4a",  ["-vn", "-c:a", "aac", "-b:a", "192k"]),
    "aac · 音訊 (192k)":        (".aac",  ["-vn", "-c:a", "aac", "-b:a", "192k"]),
    "flac · 音訊 (無損壓縮)":    (".flac", ["-vn", "-acodec", "flac"]),
    "ogg · 音訊 Vorbis":         (".ogg",  ["-vn", "-acodec", "libvorbis", "-q:a", "5"]),
    "opus · 音訊 (語音用佳)":     (".opus", ["-vn", "-c:a", "libopus", "-b:a", "96k"]),
    "mp4 · 影片 H.264 (預設)":   (".mp4",  ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-c:a", "aac", "-b:a", "128k"]),
    "mp4 · 影片 H.265 (HEVC)":   (".mp4",  ["-c:v", "libx265", "-preset", "medium", "-crf", "28", "-c:a", "aac", "-b:a", "128k"]),
    "mkv · 影片 H.264":          (".mkv",  ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-c:a", "aac", "-b:a", "128k"]),
    "webm · 影片 VP9":           (".webm", ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-c:a", "libopus", "-b:a", "96k"]),
}

# Gemini directly accepts these:
GEMINI_AUDIO_MIME = {
    ".mp3": "audio/mp3", ".wav": "audio/wav",
    ".aac": "audio/aac", ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}

# 免費 tier 限制 — 2026-05-11 直接從 Michael 的 Google AI Studio dashboard 抄回。
# 來源:https://aistudio.google.com/rate-limit?timeRange=last-28-days
# Google 2025/12 起把公開 docs 的額度表下架,實際數字因帳戶而異,**只能信 dashboard**。
# 寫死的數字 = Michael 帳戶 2026-05-11 的實際 cap。換帳戶要重抄。
#
# rpd = -1 表示 Unlimited。所有列入下拉的模型都實測支援 audio input。
# Gemma 4 26B/31B 全系列實測 audio REJECTED(model card 只列 E2B/E4B 支援 audio,而那兩個沒上 AI Studio API),
# 因此不放進 SRT 模型選單。
QUOTA = {
    # ★ Groq Whisper — 純 ASR 模型,逐字轉錄不偷懶,中文品質強。free tier 25MB / 檔
    "groq-whisper-large-v3":       {"provider": "groq",   "rpm": 100,   "rpd": -1, "tpm": -1, "label": "Groq Whisper Large v3 (★ 推薦轉錄)"},
    "groq-whisper-large-v3-turbo": {"provider": "groq",   "rpm": 100,   "rpd": -1, "tpm": -1, "label": "Groq Whisper Large v3 Turbo (快 4x)"},
    # Gemini 系列 — 適合校正,長音檔轉錄中段易偷懶
    "gemini-2.5-flash-lite":       {"provider": "gemini", "rpm": 4_000, "rpd": -1,      "tpm": 4_000_000, "label": "Gemini 2.5 Flash Lite (校正用)"},
    "gemini-3.1-flash-lite":       {"provider": "gemini", "rpm": 4_000, "rpd": 150_000, "tpm": 4_000_000, "label": "Gemini 3.1 Flash Lite (新但偏摘要)"},
    "gemini-3-flash-preview":      {"provider": "gemini", "rpm": 1_000, "rpd": 10_000,  "tpm": 2_000_000, "label": "Gemini 3 Flash (preview)"},
    "gemini-2.5-flash":            {"provider": "gemini", "rpm": 1_000, "rpd": 10_000,  "tpm": 1_000_000, "label": "Gemini 2.5 Flash"},
    "gemini-2.5-pro":              {"provider": "gemini", "rpm": 150,   "rpd": 1_000,   "tpm": 2_000_000, "label": "Gemini 2.5 Pro"},
    "gemini-3.1-pro-preview":      {"provider": "gemini", "rpm": 25,    "rpd": 250,     "tpm": 2_000_000, "label": "Gemini 3.1 Pro (preview)"},
}

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL_MAP = {  # 內部 id → Groq 真實 model id
    "groq-whisper-large-v3":       "whisper-large-v3",
    "groq-whisper-large-v3-turbo": "whisper-large-v3-turbo",
}

DEFAULT_GLOSSARY = """Gemini CLI / Gemini API / Gemini 2.5 Flash / Google AI Studio / Gemma /
Node.js / npm / Python / pip / venv / 虛擬環境 / 第三方庫 /
ChatGPT / GPT / Claude / Anthropic /
CMD / PowerShell / VS Code / Visual Studio Code / 終端機 /
API key / 金鑰 / Token / 環境變數 / .env / Git / GitHub"""

USAGE_DIR = Path.home() / ".srt_corrector"
USAGE_DIR.mkdir(parents=True, exist_ok=True)
USAGE_DB = USAGE_DIR / "usage.db"
USAGE_JSON_LEGACY = USAGE_DIR / "usage.json"  # 舊版 JSON,首次啟動會遷移到 SQLite

BLOCKS_PER_CHUNK = 80
TS_RE = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$")


# ============================================================
# 輸出檔案組織 — 各種輸出分資料夾
# ============================================================
# 結構:
#   <source_dir>/output/transcribed/  ← 從零轉錄出來的 SRT
#   <source_dir>/output/corrected/    ← 校正後 SRT + diff txt
#   <source_dir>/output/converted/    ← 媒體轉檔結果
#
# source_dir 是輸入檔(SRT 或 media)所在資料夾。

def output_dir(source_path: Path, kind: str) -> Path:
    """確保 <source_dir>/output/<kind>/ 存在,回傳路徑。
    kind ∈ {'transcribed', 'corrected', 'converted'}"""
    d = source_path.parent / "output" / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============================================================
# Quota tracker — SQLite
# ============================================================
# Schema:
#   usage(id, ts_utc, pacific_date, model, count, success, note)
# 為什麼用 pacific_date 當欄位:Google 額度是 Pacific Time 午夜重置。
# 把 PT 日期算好存進去,查「今日用量」只要 WHERE pacific_date = today_pt → 換日自動歸零、跨 session 累積。
# success=1 才算進「今日已用」;429 等失敗記成 success=0 留作 diagnostic。

def pacific_today_str() -> str:
    """Google quota 是 Pacific Time 午夜重置。保守用 PST(-8)。"""
    pt = timezone(timedelta(hours=-8))
    return datetime.now(pt).strftime("%Y-%m-%d")


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(USAGE_DB), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")  # 多 GUI 同開也安全
    return conn


def _db_init():
    with _db_connect() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc       TEXT NOT NULL,
                pacific_date TEXT NOT NULL,
                model        TEXT NOT NULL,
                count        INTEGER NOT NULL DEFAULT 1,
                success      INTEGER NOT NULL DEFAULT 1,
                note         TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pd_model ON usage(pacific_date, model)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)


def _migrate_json_if_needed():
    """首次啟動且發現舊 usage.json → import 進 SQLite,然後改名留底。"""
    if not USAGE_JSON_LEGACY.exists():
        return
    with _db_connect() as c:
        already = c.execute(
            "SELECT value FROM meta WHERE key = 'json_migrated'"
        ).fetchone()
        if already:
            return
        try:
            data = json.loads(USAGE_JSON_LEGACY.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        rows = []
        for day, models in (data or {}).items():
            if not isinstance(models, dict):
                continue
            for model, count in models.items():
                try:
                    rows.append((f"{day}T00:00:00Z", day, str(model), int(count), 1, "json_migrated"))
                except (TypeError, ValueError):
                    continue
        if rows:
            c.executemany(
                "INSERT INTO usage (ts_utc, pacific_date, model, count, success, note) "
                "VALUES (?, ?, ?, ?, ?, ?)", rows
            )
        c.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('json_migrated', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
    try:
        USAGE_JSON_LEGACY.rename(USAGE_JSON_LEGACY.with_suffix(".json.migrated"))
    except Exception:
        pass


_db_init()
_migrate_json_if_needed()


def today_used(model: str = "gemini-2.5-flash") -> int:
    """查當日 PT 已成功用量(跨 session 累積)。換日自動 0。"""
    pd = pacific_today_str()
    with _db_connect() as c:
        cur = c.execute(
            "SELECT COALESCE(SUM(count), 0) FROM usage "
            "WHERE pacific_date = ? AND model = ? AND success = 1",
            (pd, model),
        )
        return int(cur.fetchone()[0])


def increment_usage(model: str = "gemini-2.5-flash", count: int = 1,
                    success: bool = True, note: str = ""):
    """每次成功 / 失敗的 API call 都打一筆。"""
    ts = datetime.now(timezone.utc).isoformat()
    pd = pacific_today_str()
    with _db_connect() as c:
        c.execute(
            "INSERT INTO usage (ts_utc, pacific_date, model, count, success, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, pd, model, count, 1 if success else 0, note or ""),
        )


def is_quota_exhausted(exc) -> bool:
    """偵測 429 RESOURCE_EXHAUSTED daily 額度錯誤。"""
    msg = str(exc)
    return "429" in msg and ("RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower())


def parse_quota_value_from_error(exc) -> int | None:
    """從 429 錯誤訊息抽出 'limit: N' 的真實 quotaValue。"""
    msg = str(exc)
    m = re.search(r"limit:\s*(\d+)", msg)
    if m:
        return int(m.group(1))
    m = re.search(r"['\"]quotaValue['\"]:\s*['\"](\d+)['\"]", msg)
    if m:
        return int(m.group(1))
    return None


def mark_quota_exhausted(model: str, quota_value: int):
    """收到 429 時補一筆 'cap_hit' marker,把今日已用拉到 quota cap。
    這樣 GUI 顯示『已用完』而不會跟 Google 那邊狀態不一致。"""
    used = today_used(model)
    deficit = max(0, quota_value - used)
    if deficit > 0:
        increment_usage(model, count=deficit, success=True, note="cap_hit")


def usage_history(days: int = 7) -> list[tuple]:
    """近 N 天 by (date, model) 的合計。"""
    with _db_connect() as c:
        cur = c.execute(
            """SELECT pacific_date, model, SUM(count) AS total
               FROM usage WHERE success = 1
               GROUP BY pacific_date, model
               ORDER BY pacific_date DESC, model ASC
               LIMIT ?""",
            (days * 10,),
        )
        return cur.fetchall()


# ============================================================
# SRT 工具
# ============================================================

def parse_srt(text: str):
    blocks = []
    cur = []
    for line in text.splitlines():
        if line.strip() == "":
            if cur:
                blocks.append(cur); cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)
    parsed = []
    for b in blocks:
        if len(b) < 3:
            continue
        try:
            idx = int(b[0].strip())
        except ValueError:
            continue
        if not TS_RE.match(b[1]):
            continue
        parsed.append((idx, b[1], b[2:]))
    return parsed


def reassemble_srt(parsed):
    out = []
    for idx, ts, lines in parsed:
        out.append(str(idx))
        out.append(ts)
        out.extend(lines)
        out.append("")
    return "\n".join(out)


# ============================================================
# 音訊處理 — 影片抽音、不支援的 audio 也轉成 mp3
# ============================================================

def get_ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise RuntimeError("缺 imageio-ffmpeg,請: pip install imageio-ffmpeg")


def _ascii_temp_audio_path(media_path: Path, ext: str = ".mp3") -> Path:
    """產生 ASCII-safe 暫存音檔路徑(避開 httpx 對中文檔名的 ASCII encoding 問題)。"""
    import tempfile, hashlib
    h = hashlib.md5(str(media_path).encode("utf-8")).hexdigest()[:8]
    return Path(tempfile.gettempdir()) / f"srt_corrector_{h}{ext}"


def prepare_audio_for_gemini(media_path: Path, log_fn) -> tuple[Path, str]:
    """確保檔案是 Gemini 可吃的格式 + ASCII 路徑。回 (path, mime_type)。"""
    ext = media_path.suffix.lower()
    # 是否已是 Gemini 友善 audio 格式
    if ext in GEMINI_AUDIO_MIME and ext in AUDIO_EXTS:
        # 檢查路徑是否 ASCII;不是的話複製到 temp
        try:
            str(media_path).encode("ascii")
            log_fn(f"音訊格式 {ext} 可直接送 Gemini")
            return media_path, GEMINI_AUDIO_MIME[ext]
        except UnicodeEncodeError:
            # 複製到 ASCII temp 路徑(因為 httpx upload 不接受非 ASCII filename)
            import shutil
            tmp = _ascii_temp_audio_path(media_path, ext)
            log_fn(f"複製到 ASCII 暫存:{tmp.name}")
            shutil.copy2(media_path, tmp)
            return tmp, GEMINI_AUDIO_MIME[ext]

    # 否則:抽音軌 / 轉碼為 mp3,直接輸出到 ASCII temp 路徑
    out_path = _ascii_temp_audio_path(media_path, ".mp3")
    ffmpeg = get_ffmpeg()
    log_fn(f"抽 / 轉碼成 mp3(mono 64kbps)...")
    import subprocess
    cmd = [
        ffmpeg, "-i", str(media_path),
        "-vn", "-acodec", "libmp3lame", "-ab", "64k", "-ac", "1",
        "-y", str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 失敗: {r.stderr[-500:]}")
    log_fn(f"  → {out_path.name} ({out_path.stat().st_size//1024} KB)")
    return out_path, "audio/mp3"


# ============================================================
# Gemini 校正核心
# ============================================================

def build_prompt(start_idx, end_idx, items, glossary):
    items_json = json.dumps(items, ensure_ascii=False, indent=2)
    return f"""你是專業字幕校對員。對著音檔重聽,把下列字幕逐句校正(錯字 / 專有名詞 / 同音字 / 標點)。

【常見 ASR 錯誤】
- 英文人名 / 產品名 / 公司名常被聽錯,看 Glossary 還原
- 「CND」應該是 **CMD**
- 中英文間建議加空格、半形逗號→全形「,」
- 不確定就保留原文,不要刪掉內容

【Glossary】
{glossary}

【輸入】index {start_idx}-{end_idx} 共 {len(items)} 個條目(JSON):
```json
{items_json}
```

【輸出 — 絕對嚴守】
**只回 JSON array**(用 application/json mime):
- length 必須 = {len(items)}
- 每個 item 的 i 跟輸入 i 一一對應
- 不合併、不拆分、不增刪
- 不要加任何前言或 markdown
"""


def extract_json_array(text: str):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _extract_english_tokens(text: str) -> list:
    """抽出英文 / 含數字 / 含 dot/dash 的 token(可能是專有名詞)。"""
    return re.findall(r"[A-Za-z][A-Za-z0-9\.\-_]*", text)


def _find_token_swaps(orig: str, new: str):
    """以 SequenceMatcher 推測 orig 跟 new 中替換的英文 token 對。"""
    from difflib import SequenceMatcher
    o_tokens = _extract_english_tokens(orig)
    n_tokens = _extract_english_tokens(new)
    o_set = set(o_tokens)
    n_set = set(n_tokens)
    removed = list(o_set - n_set)
    added = list(n_set - o_set)
    if not removed or not added:
        return []
    pairs = []
    used_new = set()
    for r in removed:
        # find best match in added by similarity
        best, best_score = None, 0.0
        for a in added:
            if a in used_new:
                continue
            score = SequenceMatcher(None, r.lower(), a.lower()).ratio()
            if score > best_score:
                best, best_score = a, score
        if best and best_score > 0.4:
            pairs.append((r, best))
            used_new.add(best)
    return pairs


def _find_char_diffs(orig: str, new: str):
    """Character-level diff,抽出局部差異片段(含 ±1 字上下文)。
    用來偵測「中英文間加空格」「同音字」「標點修正」這類沒有英文 token swap 的 typography 改動。"""
    from difflib import SequenceMatcher
    m = SequenceMatcher(None, orig, new)
    pairs = []
    for op, i1, i2, j1, j2 in m.get_opcodes():
        if op == "equal":
            continue
        # 上下文擴張 1 字幫助識別
        ctx_l = orig[max(0, i1 - 1):i1]
        ctx_r = orig[i2:min(len(orig), i2 + 1)]
        o_snip = (ctx_l + orig[i1:i2] + ctx_r)
        n_snip = (ctx_l + new[j1:j2] + ctx_r)
        # 去掉純空白前後
        if not o_snip.strip() and not n_snip.strip():
            continue
        if o_snip == n_snip:
            continue
        # 限長度,避免抓到整段
        if len(o_snip) > 24 or len(n_snip) > 24:
            continue
        pairs.append((o_snip, n_snip))
    return pairs


def extract_errata_from_files(orig_srt_path: Path, new_srt_path: Path):
    """從原 SRT vs 校正後 SRT 抽出勘誤對照表。
    回 list of (orig_term, new_term, count),依 count 由大到小。
    先試英文 token swap(專有名詞),如果都沒有,fallback 用 character-level diff
    抓 typography / 標點 / 同音字修正。"""
    from collections import Counter
    orig_parsed = parse_srt(orig_srt_path.read_text(encoding="utf-8"))
    new_parsed = parse_srt(new_srt_path.read_text(encoding="utf-8"))
    new_by_idx = {idx: "\n".join(lines) for idx, _, lines in new_parsed}

    # 第一輪:抓英文 term swap(專有名詞)
    term_pairs = []
    for idx, _, lines in orig_parsed:
        orig_text = "\n".join(lines)
        new_text = new_by_idx.get(idx, "")
        if not new_text or orig_text == new_text:
            continue
        term_pairs.extend(_find_token_swaps(orig_text, new_text))

    if term_pairs:
        counter = Counter(term_pairs)
        return [(o, n, c) for (o, n), c in counter.most_common()]

    # Fallback:character-level diff
    char_pairs = []
    for idx, _, lines in orig_parsed:
        orig_text = "\n".join(lines)
        new_text = new_by_idx.get(idx, "")
        if not new_text or orig_text == new_text:
            continue
        char_pairs.extend(_find_char_diffs(orig_text, new_text))

    counter = Counter(char_pairs)
    # 只保留出現 >= 2 次的(避免一次性 noise)
    return [(o, n, c) for (o, n), c in counter.most_common() if c >= 2]


def convert_media(media_path: Path, format_key: str, log_fn) -> Path:
    """用 ffmpeg 把輸入媒體轉成指定格式,輸出到同目錄下。

    format_key 是 OUTPUT_FORMATS 的 key。
    回傳輸出檔路徑。
    """
    if format_key not in OUTPUT_FORMATS:
        raise ValueError(f"未知格式 {format_key!r}")
    ext, args = OUTPUT_FORMATS[format_key]

    # 輸出檔名:<media_dir>/output/converted/<name>.converted.<stamp>.<ext>
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = output_dir(media_path, "converted") / f"{media_path.stem}.converted.{stamp}{ext}"

    ffmpeg = get_ffmpeg()
    log_fn(f"轉檔 → {out_path.name}...")
    import subprocess
    cmd = [ffmpeg, "-i", str(media_path), *args, "-y", str(out_path)]
    log_fn(f"  cmd: ffmpeg ... {' '.join(args)}")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 轉檔失敗:\n{r.stderr[-800:]}")
    size_mb = out_path.stat().st_size / 1024 / 1024
    log_fn(f"  ✓ {out_path.name} ({size_mb:.1f} MB)")
    return out_path


def get_media_duration_sec(media_path: Path) -> float:
    """用 ffprobe (imageio-ffmpeg 帶的) 拿到媒體秒數。"""
    import subprocess
    ffmpeg = get_ffmpeg()
    # Windows 中文路徑 → 必須強制 UTF-8 解碼 stderr,否則 cp950 遇到非 ASCII 路徑會整段空掉
    r = subprocess.run(
        [ffmpeg, "-i", str(media_path), "-hide_banner"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", r.stderr or "")
    if not m:
        return 0.0
    hh, mm, ss, cs = (int(x) for x in m.groups())
    return hh * 3600 + mm * 60 + ss + cs / 100.0


def fmt_srt_ts(t: float) -> str:
    """秒 → SRT 時間字串 hh:mm:ss,ms"""
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int((t - int(t)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _normalize_ts(s: str) -> str | None:
    """把各種怪格式的時間字串 normalize 成標準 hh:mm:ss,mmm。

    接受:
      00:00:01,600 (標準)
      00:00:01.600 (用 . 不用 ,)
      00:01:600    (Gemini 偶發,mm:ss:ms 沒 hh)
      0:01:600     (位數不足)
      01:23.456    (沒 hh)
    """
    s = s.strip()
    if not s:
        return None
    # last ':' 後面如果是 2-3 digit 看起來是 ms,改成 ','
    m = re.match(r"^(.*):(\d{2,3})$", s)
    if m and "," not in s and "." not in s:
        s = m.group(1) + "," + m.group(2)
    s = s.replace(".", ",")
    if "," not in s:
        return None
    main, ms = s.rsplit(",", 1)
    parts = main.split(":")
    if len(parts) == 2:
        parts = ["00"] + parts
    elif len(parts) != 3:
        return None
    try:
        parts = [f"{int(p):02d}" for p in parts]
    except ValueError:
        return None
    try:
        int(ms)
    except ValueError:
        return None
    ms = ms.ljust(3, "0")[:3]
    return ":".join(parts) + "," + ms


def _normalize_ts_line(line: str) -> str | None:
    """轉 'XX --> YY' 為標準格式;失敗回 None。"""
    if "-->" not in line:
        return None
    parts = re.split(r"\s*-->\s*", line, 1)
    if len(parts) != 2:
        return None
    a = _normalize_ts(parts[0])
    b = _normalize_ts(parts[1])
    if not a or not b:
        return None
    return f"{a} --> {b}"


def parse_srt_loose(text: str):
    """寬鬆 parse — 接受 Gemini 偶爾不太標準的輸出。回 list of (ts_line, text_lines)。"""
    text = text.replace("\r\n", "\n").strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    blocks = []
    cur = []
    for line in text.splitlines():
        if line.strip() == "":
            if cur:
                blocks.append(cur); cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)
    out = []
    for b in blocks:
        ts_line = None
        ts_idx = None
        for i, L in enumerate(b):
            norm = _normalize_ts_line(L.strip())
            if norm:
                ts_line = norm
                ts_idx = i
                break
        if ts_line is None or ts_idx is None:
            continue
        out.append((ts_line, b[ts_idx + 1:]))
    return out


def transcribe_with_groq(
    media_path: Path,
    glossary: str,
    model: str,
    log_fn,
    progress_fn,
):
    """用 Groq Whisper 做語音轉錄,一次處理整段(<25MB),直接拿 SRT 回來。
    純 ASR 模型,不會像 Gemini 一樣對中段「偷懶」。"""
    import requests
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError("沒設 GROQ_API_KEY — 申請 https://console.groq.com/keys 後加進 .env")
    groq_model = GROQ_MODEL_MAP.get(model, model)

    log_fn("拿媒體時長...")
    duration = get_media_duration_sec(media_path)
    if duration < 1:
        raise RuntimeError("讀不到媒體時長(ffmpeg 可能失敗)")
    log_fn(f"  時長:{int(duration//60)} 分 {int(duration%60)} 秒")

    audio_path, mime = prepare_audio_for_gemini(media_path, log_fn)
    size_mb = audio_path.stat().st_size / 1024 / 1024
    log_fn(f"  音檔 {size_mb:.1f} MB(Groq 上限 25 MB)")
    if size_mb > 25:
        raise RuntimeError(
            f"音檔 {size_mb:.1f}MB 超過 Groq 25MB 上限。\n"
            f"請先用「媒體轉檔」功能轉成 mp3 192kbps 以下,或剪短影片。"
        )

    log_fn(f"送 Groq Whisper 轉錄 ({groq_model})...")
    progress_fn(0.1)
    t0 = time.time()
    # 用 glossary 當 vocabulary hint(Groq prompt 最多 224 tokens)
    glossary_hint = (glossary or "").replace("\n", " ").strip()[:600]
    with open(audio_path, "rb") as f:
        files = {"file": (audio_path.name, f, mime)}
        data = {
            "model": groq_model,
            "response_format": "verbose_json",  # 拿 segments 以自己組 SRT(避開 Groq srt 格式邊界 case)
            "language": "zh",
            "temperature": "0",
        }
        if glossary_hint:
            data["prompt"] = "繁體中文逐字稿。專有名詞拼字請參考: " + glossary_hint
        try:
            r = requests.post(
                f"{GROQ_BASE_URL}/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files=files, data=data, timeout=600,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"Groq 連線失敗: {e}")
    elapsed = time.time() - t0
    progress_fn(0.85)

    if r.status_code != 200:
        # 嘗試解析錯誤訊息
        try:
            j = r.json()
            err = j.get("error", {}).get("message", r.text[:400])
        except Exception:
            err = r.text[:400]
        raise RuntimeError(f"Groq API {r.status_code}: {err}")

    try:
        payload = r.json()
    except Exception:
        raise RuntimeError(f"Groq 回傳非 JSON: {r.text[:400]}")

    segments = payload.get("segments") or []
    if not segments:
        # fallback:整段一塊
        full_text = (payload.get("text") or "").strip()
        if not full_text:
            raise RuntimeError("Groq 回應空 — 音檔可能無語音內容")
        log_fn("  ⚠️ 沒拿到 segments,合成單一塊")
        segments = [{"start": 0.0, "end": duration, "text": full_text}]

    log_fn(f"  ✓ {elapsed:.0f}s 取得 {len(segments)} segments")

    # 組 SRT
    out_lines = []
    for i, seg in enumerate(segments, start=1):
        st = float(seg.get("start", 0.0))
        ed = float(seg.get("end", st + 1.0))
        txt = (seg.get("text") or "").strip()
        if not txt:
            continue
        out_lines.append(str(i))
        out_lines.append(f"{fmt_srt_ts(st)} --> {fmt_srt_ts(ed)}")
        out_lines.append(txt)
        out_lines.append("")
    final_srt = "\n".join(out_lines)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_srt = output_dir(media_path, "transcribed") / f"{media_path.stem}.transcribed.{stamp}.srt"
    out_srt.write_text(final_srt, encoding="utf-8")
    log_fn(f"✓ SRT: output/transcribed/{out_srt.name} ({len(segments)} blocks / {len(final_srt)} 字元)")

    # 抓 Groq 用量(如果 header 有)— 用於 GUI 額度顯示
    increment_usage(model, 1, note="groq")
    progress_fn(1.0)
    return out_srt, len(segments)


def transcribe_to_srt(
    media_path: Path,
    glossary: str,
    model: str,
    log_fn,
    progress_fn,
):
    """路由:依 model 的 provider 走 Gemini 或 Groq。"""
    provider = QUOTA.get(model, {}).get("provider", "gemini")
    if provider == "groq":
        return transcribe_with_groq(media_path, glossary, model, log_fn, progress_fn)
    return _transcribe_with_gemini(media_path, glossary, model, log_fn, progress_fn)


def _transcribe_with_gemini(
    media_path: Path,
    glossary: str,
    model: str,
    log_fn,
    progress_fn,
):
    """從零產 SRT — 對音檔轉錄,輸出含時間軸的字幕。"""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    log_fn("拿媒體時長...")
    duration = get_media_duration_sec(media_path)
    if duration < 1:
        raise RuntimeError("讀不到媒體時長(ffmpeg 可能失敗)")
    log_fn(f"  時長:{int(duration//60)} 分 {int(duration%60)} 秒")

    audio_path, mime = prepare_audio_for_gemini(media_path, log_fn)
    log_fn("上傳音訊到 Gemini...")
    audio_file = client.files.upload(file=str(audio_path))
    while audio_file.state.name == "PROCESSING":
        time.sleep(2)
        audio_file = client.files.get(name=audio_file.name)
    if audio_file.state.name != "ACTIVE":
        raise RuntimeError(f"音訊上傳失敗 state={audio_file.state.name}")
    log_fn("  音訊就緒")

    # 切時間段:每 5 分鐘一段(避開 8K output token)。
    # **V1 策略**:整段音檔上傳一次,每個 chunk 共用同一個 audio_file,
    # 只在 prompt 寫「只轉某時間範圍」— 模型聽得到完整句子,斷句不會在邊界被切爛。
    chunk_min = 5
    chunk_sec = chunk_min * 60
    chunks: list[tuple[float, float]] = []
    t = 0.0
    while t < duration:
        e = min(t + chunk_sec, duration)
        chunks.append((t, e))
        t = e
    n_chunks = len(chunks)
    log_fn(f"分 {n_chunks} 段 × {chunk_min} 分鐘呼叫 {model}...")

    all_blocks: list[tuple[str, list[str]]] = []
    last_call = 0.0
    rpm = QUOTA[model]["rpm"]
    min_interval = 60.0 / rpm

    for ci, (st, ed) in enumerate(chunks):
        elapsed = time.time() - last_call
        if elapsed < min_interval and ci > 0:
            time.sleep(min_interval - elapsed)

        st_str = fmt_srt_ts(st).replace(",", ".")
        ed_str = fmt_srt_ts(ed).replace(",", ".")
        log_fn(f"  [{ci+1}/{n_chunks}] 轉錄 {st_str} ~ {ed_str}...")

        prompt = f"""請對附上的音檔做語音辨識,只轉錄 {st_str} 到 {ed_str} 這段時間。

【輸出格式 — 嚴格遵守】
標準 SRT 格式,單句一個 block:
```
1
HH:MM:SS,mmm --> HH:MM:SS,mmm
字幕文字

2
HH:MM:SS,mmm --> HH:MM:SS,mmm
...
```

【規則】
1. 時間軸用**絕對時間**(對應原音檔的時間軸),不要從 00:00 開始
2. 每個 block 約 5-15 秒,以自然斷句為主(語句完結、停頓處)
3. 繁體中文輸出
4. 不要加任何前言、解釋、markdown 標記、code fence
5. 第一個字元應該是 "1"
6. 編號從 1 開始(後處理會重新編號)
7. **聽不清楚的片段直接跳過**(不要輸出 [unclear]、[模糊]、[聽不清]、??? 之類的佔位字)
8. **絕對禁止重複輸出同一個 block**(同樣的文字 + 時間戳不要重複)

【Glossary — 確保這些詞拼字正確】
{glossary}

請開始轉錄。
"""
        t0 = time.time()
        try:
            resp = client.models.generate_content(
                model=model,
                contents=[audio_file, prompt],
                config=types.GenerateContentConfig(
                    system_instruction="你是專業字幕產製員,輸出絕對符合 SRT 標準格式。",
                    temperature=0.1,
                    max_output_tokens=24000,  # 拉高避免 [unclear] 重複退化吃光 token(2.5 系列已知 bug)
                ),
            )
            elapsed = time.time() - t0
            last_call = time.time()
            increment_usage(model, 1)
            # 抓 finish_reason + token usage,確認是不是 MAX_TOKENS 截斷
            try:
                fr = resp.candidates[0].finish_reason if resp.candidates else "?"
                fr = fr.name if hasattr(fr, "name") else str(fr)
                um = resp.usage_metadata
                pt = getattr(um, "prompt_token_count", "?")
                ct = getattr(um, "candidates_token_count", "?")
                tt = getattr(um, "total_token_count", "?")
                log_fn(f"     [diag] finish={fr}, tokens in={pt}/out={ct}/total={tt}")
            except Exception:
                pass
        except Exception as e:
            if is_quota_exhausted(e):
                # 429 可能來自 RPM / RPD / TPM 任一,不再蓋掉 QUOTA dict(dashboard 是 source of truth)
                real_quota = parse_quota_value_from_error(e)
                cap_rpd = QUOTA[model].get("rpd", -1)
                if real_quota is not None and cap_rpd > 0:
                    mark_quota_exhausted(model, min(real_quota, cap_rpd))
                    log_fn(f"  ❌ 命中限制(limit:{real_quota})— 停止後續 chunks。Pacific Time 午夜重置。")
                else:
                    log_fn(f"  ❌ 命中限制 — 停止後續 chunks。可能是 RPM / TPM 瞬時上限,稍候再試。")
                break
            log_fn(f"  ❌ API 錯誤: {e}")
            continue

        parsed = parse_srt_loose(resp.text or "")
        log_fn(f"     ✓ {elapsed:.0f}s, {len(parsed)} blocks")
        all_blocks.extend(parsed)
        progress_fn((ci + 1) / n_chunks)

    # 重新編號 + 拼裝
    out_lines = []
    for i, (ts, lines) in enumerate(all_blocks, start=1):
        out_lines.append(str(i))
        out_lines.append(ts)
        out_lines.extend(lines)
        out_lines.append("")
    final_srt = "\n".join(out_lines)

    # 輸出檔:<media_dir>/output/transcribed/<name>.transcribed.<stamp>.srt
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_srt = output_dir(media_path, "transcribed") / f"{media_path.stem}.transcribed.{stamp}.srt"
    out_srt.write_text(final_srt, encoding="utf-8")
    log_fn(f"✓ SRT: output/transcribed/{out_srt.name} ({len(all_blocks)} blocks / {len(final_srt)} 字元)")

    return out_srt, len(all_blocks)


def correct_srt_with_gemini(
    srt_path: Path,
    media_path: Path,
    glossary: str,
    model: str,
    log_fn,
    progress_fn,
):
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    srt_text = srt_path.read_text(encoding="utf-8")
    parsed = parse_srt(srt_text)
    if not parsed:
        raise RuntimeError("SRT 解析失敗 — 看起來不是合法 SRT 格式")
    log_fn(f"SRT: {len(parsed)} 個條目")

    audio_path, mime = prepare_audio_for_gemini(media_path, log_fn)
    log_fn(f"上傳音訊到 Gemini...")
    audio_file = client.files.upload(file=str(audio_path))
    while audio_file.state.name == "PROCESSING":
        time.sleep(2)
        audio_file = client.files.get(name=audio_file.name)
    if audio_file.state.name != "ACTIVE":
        raise RuntimeError(f"音訊上傳失敗 state={audio_file.state.name}")
    log_fn(f"  音訊就緒")

    items_all = [{"i": idx, "text": "\n".join(lines)} for idx, _, lines in parsed]
    n = len(items_all)
    n_chunks = (n + BLOCKS_PER_CHUNK - 1) // BLOCKS_PER_CHUNK
    log_fn(f"分 {n_chunks} 段呼叫 {model} (預計 ~{n_chunks*30}秒)...")

    corrected_map: dict[int, str] = {}
    last_call_ts = 0.0
    rpm = QUOTA[model]["rpm"]
    min_interval = 60.0 / rpm  # 簡單 rate limit

    for chunk_i in range(n_chunks):
        s = chunk_i * BLOCKS_PER_CHUNK
        e = min(s + BLOCKS_PER_CHUNK, n)
        chunk = items_all[s:e]
        start_idx, end_idx = chunk[0]["i"], chunk[-1]["i"]

        # 簡單 rate-limit
        elapsed = time.time() - last_call_ts
        if elapsed < min_interval and chunk_i > 0:
            wait = min_interval - elapsed
            log_fn(f"  rate-limit: 等 {wait:.1f}s")
            time.sleep(wait)

        log_fn(f"  [{chunk_i+1}/{n_chunks}] blocks {start_idx}-{end_idx}...")
        prompt = build_prompt(start_idx, end_idx, chunk, glossary)
        t0 = time.time()
        try:
            resp = client.models.generate_content(
                model=model,
                contents=[audio_file, prompt],
                config=types.GenerateContentConfig(
                    system_instruction="你是極度遵守格式的字幕校對員。",
                    temperature=0.0,
                    max_output_tokens=8000,
                    response_mime_type="application/json",
                ),
            )
            elapsed = time.time() - t0
            last_call_ts = time.time()
            increment_usage(model, 1)
        except Exception as e:
            if is_quota_exhausted(e):
                real_quota = parse_quota_value_from_error(e)
                if real_quota is not None:
                    QUOTA[model]["rpd"] = real_quota
                    mark_quota_exhausted(model, real_quota)
                    log_fn(f"  ❌ 達到每日上限({real_quota} RPD)— 停止後續 chunks。Pacific Time 午夜重置。")
                else:
                    log_fn(f"  ❌ 達到每日上限 — 停止後續 chunks。")
                # 把剩下的 chunks 都回 fallback 原文
                for item in chunk:
                    corrected_map[item["i"]] = item["text"]
                break
            log_fn(f"  ❌ API 錯誤: {e}")
            for item in chunk:
                corrected_map[item["i"]] = item["text"]
            continue

        arr = extract_json_array(resp.text or "")
        if not isinstance(arr, list):
            log_fn(f"  ⚠️ 無 JSON 回應,保留原文")
            for item in chunk:
                corrected_map[item["i"]] = item["text"]
        else:
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
            missing = len(chunk) - got
            log_fn(f"     ✓ {elapsed:.0f}s, {got}/{len(chunk)} corrected" + (f" ⚠️ {missing} fallback" if missing else ""))
            for item in chunk:
                if item["i"] not in corrected_map:
                    corrected_map[item["i"]] = item["text"]

        progress_fn((chunk_i + 1) / n_chunks)

    # 拼回 SRT
    new_parsed = []
    for idx, ts, lines in parsed:
        new_text = corrected_map.get(idx, "\n".join(lines))
        new_parsed.append((idx, ts, new_text.split("\n")))
    corrected_text = reassemble_srt(new_parsed)

    # 輸出到 <srt_dir>/output/corrected/
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst_dir = output_dir(srt_path, "corrected")
    out_srt = dst_dir / f"{srt_path.stem}.corrected.{stamp}.srt"
    out_srt.write_text(corrected_text, encoding="utf-8")
    log_fn(f"✓ 校正後檔: output/corrected/{out_srt.name}")

    diff = list(difflib.unified_diff(
        srt_text.splitlines(), corrected_text.splitlines(),
        fromfile="原 SRT", tofile="校正後", lineterm="", n=1,
    ))
    out_diff = dst_dir / f"{srt_path.stem}.diff.{stamp}.txt"
    out_diff.write_text("\n".join(diff), encoding="utf-8")
    n_changes = sum(1 for L in diff if L.startswith("+") and not L.startswith("+++"))
    log_fn(f"✓ diff:  output/corrected/{out_diff.name} ({n_changes} 行變動)")

    return out_srt, out_diff, n_changes


# ============================================================
# GUI
# ============================================================

class SRTCorrectorApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.title("SRT 字幕工具 — Gemini")
        self.geometry("820x800+100+50")
        self.minsize(780, 720)

        self.srt_path = ctk.StringVar()
        self.media_path = ctk.StringVar()
        # 預設 mode = 校正既有 SRT → 配 Gemini 3.1 Flash Lite。切到「從零產生 SRT」會自動換成 Groq Whisper。
        self.model = ctk.StringVar(value="gemini-3.1-flash-lite")
        self.mode = ctk.StringVar(value="校正既有 SRT")
        self.enable_convert = ctk.BooleanVar(value=False)
        self.convert_format = ctk.StringVar(value=list(OUTPUT_FORMATS.keys())[0])
        self.is_running = False

        # 嘗試載入 .env (找專案根目錄)
        self._load_env()

        self._build_ui()
        self._refresh_quota_display()
        # 啟動每 30 秒刷一次額度顯示
        self.after(30_000, self._tick_refresh_quota)

    def _load_env(self):
        """找 .env — 優先 script 同目錄,其次 cwd,最後 ~/.srt_corrector/。
        Side_project 已獨立,**不再 fallback 到其他專案的 .env**。"""
        candidates = [
            Path(__file__).parent / ".env",     # 跟 srt_corrector_gui.py 同目錄(推薦)
            Path.cwd() / ".env",                # 從哪 cd 啟動
            Path.home() / ".srt_corrector" / ".env",  # 全域 fallback
        ]
        for c in candidates:
            if c.exists():
                load_dotenv(c)
                return
        load_dotenv()  # 最後一招:依 python-dotenv 預設行為

    def _build_ui(self):
        # ============================================================
        # FIXED TOP — 標題 + 額度 bar
        # ============================================================
        title_box = ctk.CTkFrame(self, fg_color="transparent")
        title_box.pack(fill="x", padx=18, pady=(14, 0))
        ctk.CTkLabel(title_box, text="SRT 字幕工具",
                     font=ctk.CTkFont(size=22, weight="bold")).pack(anchor="w")
        self.subtitle_lbl = ctk.CTkLabel(
            title_box, text="校正既有 SRT  ·  從零產生 SRT",
            font=ctk.CTkFont(size=12),
            text_color=("#6b7280", "#8b949e"),
        )
        self.subtitle_lbl.pack(anchor="w")

        quota_bar = ctk.CTkFrame(
            self,
            fg_color=("#dbeafe", "#1e3a8a"),
            corner_radius=10, border_width=2,
            border_color=("#3b82f6", "#60a5fa"),
        )
        quota_bar.pack(fill="x", padx=18, pady=(8, 0))

        ctk.CTkLabel(
            quota_bar, text="今日 API 用量 (RPD)",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("#1e40af", "#bfdbfe"),
        ).pack(side="left", padx=(16, 12), pady=10)
        self.quota_main_lbl = ctk.CTkLabel(
            quota_bar, text="0 / 250",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=("#1e3a8a", "#ffffff"),
        )
        self.quota_main_lbl.pack(side="left", padx=4, pady=8)
        self.quota_remaining_lbl = ctk.CTkLabel(
            quota_bar, text="剩 250 次",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("#15803d", "#86efac"),
        )
        self.quota_remaining_lbl.pack(side="right", padx=16, pady=10)
        self.quota_model_lbl = ctk.CTkLabel(
            quota_bar, text="gemini-2.5-flash",
            font=ctk.CTkFont(family="Consolas", size=10),
            text_color=("#475569", "#cbd5e1"),
        )
        self.quota_model_lbl.pack(side="right", padx=(8, 8), pady=10)

        # ============================================================
        # FIXED BOTTOM — Start button + progress + log(先建立讓 scroll 區可知道邊界)
        # ============================================================
        # Bottom is packed AFTER scroll so it ends up below; we 把 bottom 先建立但 pack 順序在後面
        # 用 side="bottom" 也可,但 ctk + tk 混搭時要小心

        # ============================================================
        # SCROLLABLE MIDDLE — 所有 input 卡片
        # ============================================================
        main_scroll = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
        )
        main_scroll.pack(fill="both", expand=True, padx=14, pady=(8, 0))

        # ---- 卡片 1:檔案輸入 ----
        card_inputs = ctk.CTkFrame(
            main_scroll,
            fg_color=("#fbfbfd", "#0f1115"),
            border_color=("#e5e7eb", "#262b33"),
            border_width=1, corner_radius=10,
        )
        card_inputs.pack(fill="x", padx=4, pady=(4, 8))

        ctk.CTkLabel(
            card_inputs, text="① 檔案輸入",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=("#1f2328", "#e6edf3"),
            anchor="w",
        ).pack(anchor="w", padx=14, pady=(10, 6))

        # Mode
        mode_row = ctk.CTkFrame(card_inputs, fg_color="transparent")
        mode_row.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(mode_row, text="模式",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     width=80, anchor="w").pack(side="left")
        self.mode_seg = ctk.CTkSegmentedButton(
            mode_row,
            values=["校正既有 SRT", "從零產生 SRT"],
            variable=self.mode,
            command=self._on_mode_change,
        )
        self.mode_seg.pack(side="left", padx=(0, 6))

        # SRT
        self.srt_row = ctk.CTkFrame(card_inputs, fg_color="transparent")
        self.srt_row.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(self.srt_row, text="SRT 檔",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     width=80, anchor="w").pack(side="left")
        ctk.CTkEntry(self.srt_row, textvariable=self.srt_path).pack(
            side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(self.srt_row, text="選取", width=64,
                      command=self._select_srt).pack(side="left")

        # Media
        media_row = ctk.CTkFrame(card_inputs, fg_color="transparent")
        media_row.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(media_row, text="影片/錄音",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     width=80, anchor="w").pack(side="left")
        ctk.CTkEntry(media_row, textvariable=self.media_path).pack(
            side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(media_row, text="選取", width=64,
                      command=self._select_media).pack(side="left")

        ctk.CTkLabel(
            card_inputs,
            text="支援:.mp4 .mkv .mov .avi .webm .m4v .flv .wmv · .mp3 .wav .m4a .aac .flac .ogg .opus .wma",
            font=ctk.CTkFont(size=10),
            text_color=("#9aa0a6", "#6e7681"),
        ).pack(anchor="w", padx=14, pady=(4, 4))

        # Model — 依模式過濾(校正=Gemini / 轉錄=Groq)
        model_row = ctk.CTkFrame(card_inputs, fg_color="transparent")
        model_row.pack(fill="x", padx=12, pady=(4, 12))
        ctk.CTkLabel(model_row, text="模型",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     width=80, anchor="w").pack(side="left")
        self.model_optmenu = ctk.CTkOptionMenu(
            model_row, variable=self.model,
            values=self._models_for_mode(self.mode.get()),
            width=240,
            command=lambda _v: self._refresh_quota_display(),
        )
        self.model_optmenu.pack(side="left")
        self.model_hint_lbl = ctk.CTkLabel(
            model_row, text="",
            font=ctk.CTkFont(size=10),
            text_color=("#6b7280", "#8b949e"),
        )
        self.model_hint_lbl.pack(side="left", padx=10)
        self._update_model_hint()

        # ---- 卡片 2:Glossary(顯眼的獨立大區)----
        card_glossary = ctk.CTkFrame(
            main_scroll,
            fg_color=("#f0fdf4", "#0d1f17"),
            border_color=("#bbf7d0", "#14532d"),
            border_width=1, corner_radius=10,
        )
        card_glossary.pack(fill="x", padx=4, pady=8)

        glo_hdr = ctk.CTkFrame(card_glossary, fg_color="transparent")
        glo_hdr.pack(fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(
            glo_hdr, text="② 專有名詞 Glossary",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=("#14532d", "#86efac"),
        ).pack(side="left")
        ctk.CTkLabel(
            glo_hdr, text="(直接編輯;Gemini 校正/轉錄時用來確保拼字正確)",
            font=ctk.CTkFont(size=10),
            text_color=("#15803d", "#86efac"),
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            glo_hdr, text="↺ 預設值", width=80, height=24,
            font=ctk.CTkFont(size=11),
            fg_color="transparent", border_width=1,
            text_color=("#14532d", "#86efac"),
            border_color=("#bbf7d0", "#14532d"),
            hover_color=("#dcfce7", "#1c2e21"),
            command=self._reset_glossary,
        ).pack(side="right")

        self.glossary_box = ctk.CTkTextbox(
            card_glossary, height=110, wrap="word",
            font=ctk.CTkFont(size=12),
            border_width=1,
            border_color=("#bbf7d0", "#14532d"),
            fg_color=("#ffffff", "#0a1610"),
        )
        self.glossary_box.pack(fill="x", padx=14, pady=(4, 12))
        self.glossary_box.insert("1.0", DEFAULT_GLOSSARY)

        # ---- 卡片 3:媒體轉檔(選用)----
        card_convert = ctk.CTkFrame(
            main_scroll,
            fg_color=("#fef3c7", "#1f1a0d"),
            border_color=("#fde68a", "#92400e"),
            border_width=1, corner_radius=10,
        )
        card_convert.pack(fill="x", padx=4, pady=8)

        conv_top = ctk.CTkFrame(card_convert, fg_color="transparent")
        conv_top.pack(fill="x", padx=14, pady=(10, 6))
        ctk.CTkCheckBox(
            conv_top, variable=self.enable_convert,
            text="③ 同時轉檔媒體格式(選用)",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=("#7c2d12", "#fbbf24"),
            checkbox_height=20, checkbox_width=20,
        ).pack(side="left")
        ctk.CTkLabel(
            conv_top, text="(校正/轉錄完成後一併把媒體檔轉成下方格式)",
            font=ctk.CTkFont(size=10),
            text_color=("#9a3412", "#fcd34d"),
        ).pack(side="left", padx=(8, 0))

        conv_row = ctk.CTkFrame(card_convert, fg_color="transparent")
        conv_row.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkLabel(conv_row, text="輸出格式",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     width=80, anchor="w").pack(side="left")
        ctk.CTkOptionMenu(
            conv_row, variable=self.convert_format,
            values=list(OUTPUT_FORMATS.keys()),
            width=300,
        ).pack(side="left", padx=(0, 6))

        # ---- 卡片 4:勘誤表(常駐,empty state 提示)----
        self.errata_card = ctk.CTkFrame(
            main_scroll,
            fg_color=("#fff7ed", "#1c1209"),
            border_color=("#fed7aa", "#5b3a0e"),
            border_width=1, corner_radius=10,
        )
        self.errata_card.pack(fill="x", padx=4, pady=(8, 12))

        err_hdr = ctk.CTkFrame(self.errata_card, fg_color="transparent")
        err_hdr.pack(fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(
            err_hdr, text="④ 本次勘誤表",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=("#9a3412", "#fdba74"),
        ).pack(side="left")
        self.errata_hint_label = ctk.CTkLabel(
            err_hdr,
            text="(校正完成後會在這顯示 ASR 錯字對照,點 [+] 加入 Glossary)",
            font=ctk.CTkFont(size=10),
            text_color=("#9a3412", "#fdba74"),
        )
        self.errata_hint_label.pack(side="left", padx=(8, 0))

        self.errata_scroll = ctk.CTkScrollableFrame(
            self.errata_card, height=140,
            fg_color=("#ffffff", "#0f1115"),
        )
        self.errata_scroll.pack(fill="x", padx=14, pady=(4, 12))

        # empty state
        self.errata_empty_lbl = ctk.CTkLabel(
            self.errata_scroll,
            text="(目前沒有勘誤;跑「校正既有 SRT」後會出現)",
            font=ctk.CTkFont(size=11),
            text_color=("#9ca3af", "#6e7681"),
        )
        self.errata_empty_lbl.pack(padx=10, pady=24)

        # ============================================================
        # FIXED BOTTOM — Start + progress + log
        # ============================================================
        bottom_frame = ctk.CTkFrame(self, fg_color="transparent")
        bottom_frame.pack(fill="x", side="bottom", padx=18, pady=(0, 14))

        self.log_box = ctk.CTkTextbox(
            bottom_frame, wrap="word", height=110,
            font=ctk.CTkFont(family="Consolas", size=11),
            border_width=1, border_color=("#e5e7eb", "#262b33"),
            fg_color=("#fbfbfd", "#0f1115"),
        )
        self.log_box.pack(fill="x", side="bottom", pady=(6, 0))
        self._init_log_tags()

        self.log_label = ctk.CTkLabel(
            bottom_frame, text="Log",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=("#9aa0a6", "#6e7681"),
            anchor="w",
        )
        self.log_label.pack(anchor="w", side="bottom")

        self.progress = ctk.CTkProgressBar(bottom_frame)
        self.progress.set(0)
        self.progress.pack(fill="x", side="bottom", pady=(6, 4))

        self.start_btn = ctk.CTkButton(
            bottom_frame, text="開始校正 ▸",
            font=ctk.CTkFont(size=15, weight="bold"),
            height=46, fg_color="#3b82f6", hover_color="#2563eb",
            command=self._on_start,
        )
        self.start_btn.pack(fill="x", side="bottom", pady=(8, 0))

        self._check_api_key()

    def _check_api_key(self):
        if os.environ.get("GEMINI_API_KEY"):
            self.log(f"✓ Gemini API key 已載入")
        else:
            self.log("⚠️ 未設定 GEMINI_API_KEY(校正/Gemini 轉錄需要)")
            self.log("   申請: https://aistudio.google.com/apikey (免費)")
        if os.environ.get("GROQ_API_KEY"):
            self.log(f"✓ Groq API key 已載入")
        else:
            self.log("⚠️ 未設定 GROQ_API_KEY(Whisper 轉錄需要)")
            self.log("   申請: https://console.groq.com/keys (免費)")

    # ----------------- file pickers -----------------

    def _select_srt(self):
        p = filedialog.askopenfilename(
            title="選 SRT 檔案",
            filetypes=[("SRT", "*.srt"), ("All", "*.*")],
        )
        if p:
            self.srt_path.set(p)

    def _select_media(self):
        p = filedialog.askopenfilename(
            title="選影片或錄音",
            filetypes=[
                ("影片 / 音檔",
                 " ".join("*" + e for e in MEDIA_EXTS)),
                ("影片", " ".join("*" + e for e in VIDEO_EXTS)),
                ("音檔", " ".join("*" + e for e in AUDIO_EXTS)),
                ("All", "*.*"),
            ],
        )
        if p:
            self.media_path.set(p)

    # ----------------- log + progress -----------------

    def _resolve_color(self, light_dark) -> str:
        """CTk 標準是 (light, dark) tuple,tk.Text 只吃單色 → 依目前 appearance 解析。"""
        if isinstance(light_dark, (tuple, list)):
            mode = ctk.get_appearance_mode().lower()
            return light_dark[1] if mode.startswith("dark") else light_dark[0]
        return light_dark

    def _init_log_tags(self):
        """為 log 文字框註冊顏色 tag(警告黃 / 錯誤紅 / 成功綠 / muted 灰)。
        CTkTextbox 底層是 tk.Text,需用 _textbox 才拿得到原生 tag_config。"""
        tk_text = getattr(self.log_box, "_textbox", None) or self.log_box
        palette = {
            "log_error":   ("#dc2626", "#fca5a5"),
            "log_warning": ("#b45309", "#fbbf24"),
            "log_success": ("#15803d", "#86efac"),
            "log_info":    ("#1f2328", "#e6edf3"),
            "log_muted":   ("#6b7280", "#8b949e"),
            "log_ts":      ("#9aa0a6", "#6e7681"),
        }
        for name, color in palette.items():
            try:
                tk_text.tag_config(name, foreground=self._resolve_color(color))
            except Exception:
                pass
        # 錯誤 + 警告 加微微 bold
        try:
            base_font = ctk.CTkFont(family="Consolas", size=11, weight="bold")
            tk_text.tag_config("log_error",   foreground=self._resolve_color(palette["log_error"]),   font=base_font)
            tk_text.tag_config("log_warning", foreground=self._resolve_color(palette["log_warning"]), font=base_font)
        except Exception:
            pass

    def _classify_log(self, msg: str) -> str:
        """依訊息內容自動挑顏色 tag。"""
        m = msg.lower()
        if any(s in msg for s in ("❌", "錯誤", "失敗", "Traceback")) or \
           any(s in m for s in ("error", "exception", "fail")):
            return "log_error"
        if any(s in msg for s in ("⚠️", "⚠", "警告", "達到", "用完", "上限", "額度", "quota", "429")) or \
           any(s in m for s in ("warning", "warn", "skip", "retry", "fallback")):
            return "log_warning"
        if any(s in msg for s in ("✓", "✅", "完成", "成功")) or m.startswith("ok"):
            return "log_success"
        if msg.startswith("===") or msg.startswith("───") or msg.startswith("---") or msg.startswith("  "):
            return "log_muted"
        return "log_info"

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        tag = self._classify_log(msg)
        tk_text = getattr(self.log_box, "_textbox", None) or self.log_box
        try:
            # 時間戳用 muted 灰,正文用對應 tag
            self.log_box.configure(state="normal")
            tk_text.insert("end", f"[{ts}] ", "log_ts")
            tk_text.insert("end", f"{msg}\n", tag)
        except Exception:
            # fallback:純插入(萬一沒 _textbox)
            self.log_box.insert("end", f"[{ts}] {msg}\n")
        self.log_box.see("end")
        self.update_idletasks()

    def _set_progress(self, frac: float):
        self.progress.set(frac)
        self.update_idletasks()

    # ----------------- quota display -----------------

    def _refresh_quota_display(self):
        model = self.model.get()
        info = QUOTA.get(model, {})
        cap = info.get("rpd", 0)
        used = today_used(model)
        # 同步視窗標題 + subtitle 跟著目前 model 走
        friendly = info.get("label", model).split(" (")[0]  # 去掉括號裡的 RPD 提示
        try:
            self.title(f"SRT 字幕工具 — {friendly}")
        except Exception:
            pass
        if hasattr(self, "subtitle_lbl"):
            self.subtitle_lbl.configure(
                text=f"校正既有 SRT  ·  從零產生 SRT  ·  {friendly}"
            )
        if cap == -1:
            self.quota_main_lbl.configure(text=f"{used} / ∞")
            if hasattr(self, "quota_model_lbl"):
                self.quota_model_lbl.configure(text=model)
            self.quota_remaining_lbl.configure(
                text="RPD 無上限(看 TPM/RPM)",
                text_color=("#15803d", "#86efac"),
            )
            return
        remaining = max(0, cap - used)
        self.quota_main_lbl.configure(text=f"{used} / {cap}")
        if hasattr(self, "quota_model_lbl"):
            self.quota_model_lbl.configure(text=model)
        if remaining <= 0:
            self.quota_remaining_lbl.configure(
                text=f"⚠️ 今日額度用完",
                text_color=("#dc2626", "#fca5a5"),
            )
        elif remaining < max(20, cap // 10):
            self.quota_remaining_lbl.configure(
                text=f"剩 {remaining} 次(快用完)",
                text_color=("#b45309", "#fbbf24"),
            )
        else:
            self.quota_remaining_lbl.configure(
                text=f"剩 {remaining} 次",
                text_color=("#15803d", "#86efac"),
            )

    def _tick_refresh_quota(self):
        self._refresh_quota_display()
        self.after(30_000, self._tick_refresh_quota)

    # ----------------- mode toggle / model 過濾 -----------------

    # 各模式對應的 provider + 預設 model
    MODE_PROVIDER = {
        "校正既有 SRT":  ("gemini", "gemini-3.1-flash-lite"),
        "從零產生 SRT":  ("groq",   "groq-whisper-large-v3"),
    }

    def _models_for_mode(self, mode: str) -> list[str]:
        provider, _ = self.MODE_PROVIDER.get(mode, ("gemini", ""))
        return [k for k, v in QUOTA.items() if v.get("provider") == provider]

    def _update_model_hint(self):
        mode = self.mode.get()
        provider, _ = self.MODE_PROVIDER.get(mode, ("gemini", ""))
        if provider == "groq":
            self.model_hint_lbl.configure(text="(Groq Whisper · 純 ASR · 25MB 上限)")
        else:
            self.model_hint_lbl.configure(text="(Gemini · 文字校正 · 長音檔轉錄會偷懶)")

    def _on_mode_change(self, value: str):
        # 1. 切換 model 下拉清單 + 自動選該模式預設
        provider, default = self.MODE_PROVIDER.get(value, ("gemini", ""))
        models = self._models_for_mode(value)
        try:
            self.model_optmenu.configure(values=models)
        except Exception:
            pass
        if default and default in models:
            self.model.set(default)
        elif models:
            self.model.set(models[0])
        self._update_model_hint()
        self._refresh_quota_display()
        # 2. SRT 行 + 按鈕文字
        if value == "從零產生 SRT":
            try:
                self.srt_row.pack_forget()
            except Exception:
                pass
            self.start_btn.configure(text="開始轉錄 ▸")
        else:
            self._repack_srt_row()
            self.start_btn.configure(text="開始校正 ▸")

    def _repack_srt_row(self):
        """確保 srt_row 在 mode_row 跟 media_row 中間。"""
        # 找 inputs frame 的 children 順序
        try:
            self.srt_row.pack_forget()
            # 找出 media_row(它的 parent 是 inputs);用 pack(before=...)
            media_row = None
            for child in self.srt_row.master.winfo_children():
                # media_row 含 self.media_path entry
                for sub in child.winfo_children():
                    if hasattr(sub, "cget"):
                        try:
                            if sub.cget("textvariable") == str(self.media_path):
                                media_row = child
                                break
                        except Exception:
                            pass
                if media_row:
                    break
            if media_row is not None:
                self.srt_row.pack(fill="x", padx=12, pady=(6, 6),
                                   before=media_row)
            else:
                self.srt_row.pack(fill="x", padx=12, pady=(6, 6))
        except Exception:
            self.srt_row.pack(fill="x", padx=12, pady=(6, 6))

    # ----------------- start -----------------

    def _on_start(self):
        if self.is_running:
            self.log("已在執行中,請稍候。")
            return

        mode = self.mode.get()
        sel_model = self.model.get()
        sel_provider = QUOTA.get(sel_model, {}).get("provider", "gemini")
        # 模式 vs provider 的 sanity check
        if mode == "校正既有 SRT" and sel_provider == "groq":
            messagebox.showerror(
                "模型不適用",
                "Groq Whisper 只做語音轉錄,不能拿來校正既有 SRT。\n"
                "校正請選 Gemini 系列模型。"
            )
            return
        # API key 檢查依 provider 走
        if sel_provider == "groq" and not os.environ.get("GROQ_API_KEY"):
            messagebox.showerror("缺 API key",
                                  "請先設定 GROQ_API_KEY 環境變數或 .env\n"
                                  "申請:https://console.groq.com/keys")
            return
        if sel_provider == "gemini" and not os.environ.get("GEMINI_API_KEY"):
            messagebox.showerror("缺 API key",
                                  "請先設定 GEMINI_API_KEY 環境變數或 .env")
            return

        media = self.media_path.get().strip()
        if not media or not os.path.isfile(media):
            messagebox.showerror("錯誤", "請選影片或錄音檔案")
            return
        ext = Path(media).suffix.lower()
        if ext not in MEDIA_EXTS:
            messagebox.showerror("錯誤",
                                 f"不支援副檔名 {ext}\n支援:{', '.join(MEDIA_EXTS)}")
            return

        srt = self.srt_path.get().strip() if mode == "校正既有 SRT" else None
        if mode == "校正既有 SRT":
            if not srt or not os.path.isfile(srt):
                messagebox.showerror("錯誤", "請選 SRT 檔案(校正模式必須)")
                return
            if not srt.lower().endswith(".srt"):
                messagebox.showerror("錯誤", "SRT 檔副檔名要是 .srt")
                return

        model = self.model.get()
        cap = QUOTA[model]["rpd"]
        used = today_used(model)
        if cap != -1 and used >= cap:
            messagebox.showwarning(
                "今日額度已用完",
                f"{model} 今日已用 {used}/{cap}\nPacific Time 午夜重置。"
            )
            return

        glossary = self.glossary_box.get("1.0", "end").strip() or DEFAULT_GLOSSARY

        self.is_running = True
        self.start_btn.configure(state="disabled", text="處理中...")
        self.progress.set(0)
        # 清掉舊 errata
        self._clear_errata()

        if mode == "校正既有 SRT":
            threading.Thread(
                target=self._run_correction,
                args=(Path(srt), Path(media), glossary, model),
                daemon=True,
            ).start()
        else:
            threading.Thread(
                target=self._run_transcription,
                args=(Path(media), glossary, model),
                daemon=True,
            ).start()

    def _run_correction(self, srt_path, media_path, glossary, model):
        try:
            self.log(f"開始校正:")
            self.log(f"  SRT: {srt_path.name}")
            self.log(f"  Media: {media_path.name}")
            self.log(f"  Model: {model}")
            out_srt, out_diff, n_changes = correct_srt_with_gemini(
                srt_path=srt_path,
                media_path=media_path,
                glossary=glossary,
                model=model,
                log_fn=lambda m: self.after(0, lambda mm=m: self.log(mm)),
                progress_fn=lambda f: self.after(0, lambda ff=f: self._set_progress(ff)),
            )
            self.log("=" * 50)
            self.log(f"完成 — 變動 {n_changes} 行")
            self.log(f"輸出: {out_srt}")
            self.log(f"Diff:  {out_diff}")

            # 抽 errata
            try:
                errata = extract_errata_from_files(srt_path, out_srt)
                if errata:
                    self.log(f"✓ 勘誤表 {len(errata)} 項(top 6):")
                    for orig, new, count in errata[:6]:
                        self.log(f"  • {orig}  →  {new}  × {count}")
                    self.after(0, lambda er=errata: self._populate_errata(er))
                else:
                    self.log(f"  · 沒有抽到可重複利用的勘誤詞對(可能 diff 都是一次性排版/標點調整)")
                    self.after(0, lambda: self._populate_errata([]))
            except Exception as e:
                self.log(f"  (errata 抽取失敗: {e})")

            # 媒體轉檔(選用)
            if self.enable_convert.get():
                self._do_convert(media_path)

            self.after(0, lambda: messagebox.showinfo(
                "完成", f"校正完成 — {n_changes} 行變動\n\n{out_srt.name}"))
        except Exception as e:
            self.log(f"❌ 錯誤: {e}")
            import traceback
            self.log(traceback.format_exc())
            self.after(0, lambda: messagebox.showerror("錯誤", str(e)))
        finally:
            self.is_running = False
            self.after(0, lambda: self.start_btn.configure(
                state="normal",
                text="開始校正 ▸" if self.mode.get() == "校正既有 SRT" else "開始轉錄 ▸"))
            self.after(0, self._refresh_quota_display)
            self.after(0, lambda: self._set_progress(1.0))

    def _run_transcription(self, media_path, glossary, model):
        try:
            self.log(f"開始轉錄:")
            self.log(f"  Media: {media_path.name}")
            self.log(f"  Model: {model}")
            out_srt, n_blocks = transcribe_to_srt(
                media_path=media_path,
                glossary=glossary,
                model=model,
                log_fn=lambda m: self.after(0, lambda mm=m: self.log(mm)),
                progress_fn=lambda f: self.after(0, lambda ff=f: self._set_progress(ff)),
            )
            self.log("=" * 50)
            self.log(f"完成 — 產出 {n_blocks} 個字幕條目")
            self.log(f"輸出: {out_srt}")

            # 媒體轉檔(選用)
            if self.enable_convert.get():
                self._do_convert(media_path)

            self.after(0, lambda: messagebox.showinfo(
                "完成", f"轉錄完成 — {n_blocks} 條字幕\n\n{out_srt.name}"))
        except Exception as e:
            self.log(f"❌ 錯誤: {e}")
            import traceback
            self.log(traceback.format_exc())
            self.after(0, lambda: messagebox.showerror("錯誤", str(e)))
        finally:
            self.is_running = False
            self.after(0, lambda: self.start_btn.configure(
                state="normal",
                text="開始校正 ▸" if self.mode.get() == "校正既有 SRT" else "開始轉錄 ▸"))
            self.after(0, self._refresh_quota_display)
            self.after(0, lambda: self._set_progress(1.0))

    # ----------------- errata panel -----------------

    def _clear_errata(self):
        """清空 errata_scroll 內容(留下 errata_card 本身,因為常駐)。"""
        for w in list(self.errata_scroll.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        # 顯示 empty state
        self.errata_empty_lbl = ctk.CTkLabel(
            self.errata_scroll,
            text="(目前沒有勘誤;跑「校正既有 SRT」後會出現)",
            font=ctk.CTkFont(size=11),
            text_color=("#9ca3af", "#6e7681"),
        )
        self.errata_empty_lbl.pack(padx=10, pady=24)

    def _populate_errata(self, errata: list):
        # 清空(會留 empty label)
        for w in list(self.errata_scroll.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        if not errata:
            self.errata_empty_lbl = ctk.CTkLabel(
                self.errata_scroll,
                text=(
                    "(本次校正沒有抽到重複的勘誤詞對 — 可能 diff 全是一次性的"
                    "排版/標點/中英文間距修正,沒有專有名詞替換)\n\n"
                    "看 Log 上的「變動 N 行」+ 開 diff 檔可以確認實際改動。"
                ),
                font=ctk.CTkFont(size=11),
                text_color=("#9ca3af", "#6e7681"),
                wraplength=520,
                justify="left",
            )
            self.errata_empty_lbl.pack(padx=12, pady=18, anchor="w")
            return

        # 排排站(errata_card / hdr / scroll 都已常駐 packed,直接加 rows)
        for orig, new, count in errata:
            row = ctk.CTkFrame(self.errata_scroll, fg_color=("#fff7ed", "#1c1209"))
            row.pack(fill="x", padx=2, pady=2)
            ctk.CTkLabel(
                row, text=f"{orig}",
                font=ctk.CTkFont(family="Consolas", size=11),
                text_color=("#b91c1c", "#fca5a5"),
                anchor="w", width=160,
            ).pack(side="left", padx=(8, 2), pady=4)
            ctk.CTkLabel(
                row, text="→",
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color=("#9a3412", "#fdba74"),
            ).pack(side="left", padx=2)
            ctk.CTkLabel(
                row, text=f"{new}",
                font=ctk.CTkFont(family="Consolas", size=11, weight="bold"),
                text_color=("#15803d", "#86efac"),
                anchor="w", width=160,
            ).pack(side="left", padx=(2, 8), pady=4)
            ctk.CTkLabel(
                row, text=f"× {count}",
                font=ctk.CTkFont(size=10),
                text_color=("#6b7280", "#8b949e"),
            ).pack(side="left", padx=4)
            ctk.CTkButton(
                row, text="+ Glossary", width=90, height=24,
                font=ctk.CTkFont(size=10, weight="bold"),
                fg_color="#10b981", hover_color="#059669",
                command=lambda n=new: self._add_to_glossary(n),
            ).pack(side="right", padx=8)

    def _add_to_glossary(self, term: str):
        current = self.glossary_box.get("1.0", "end").strip()
        if term in current:
            self.log(f"  Glossary 已有 {term},不重複加")
            return
        new_text = current + (" / " if current else "") + term
        self.glossary_box.delete("1.0", "end")
        self.glossary_box.insert("1.0", new_text)
        self.log(f"  ✓ 加入 Glossary: {term}")

    def _reset_glossary(self):
        """還原 Glossary 為預設值。"""
        from tkinter import messagebox
        if not messagebox.askyesno("確認", "確定要還原 Glossary 為預設值?\n你目前手動加的詞會被清掉。"):
            return
        self.glossary_box.delete("1.0", "end")
        self.glossary_box.insert("1.0", DEFAULT_GLOSSARY)
        self.log("  Glossary 已還原為預設值")

    # ----------------- media conversion -----------------

    def _do_convert(self, media_path: Path):
        """跑完字幕後執行媒體轉檔(若 user 勾選)。"""
        try:
            fmt = self.convert_format.get()
            self.log("─" * 40)
            self.log(f"媒體轉檔模式啟用,格式:{fmt}")
            out = convert_media(
                media_path, fmt,
                log_fn=lambda m: self.after(0, lambda mm=m: self.log(mm)),
            )
            self.log(f"✓ 轉檔輸出:{out}")
        except Exception as e:
            self.log(f"❌ 轉檔失敗: {e}")


def main():
    app = SRTCorrectorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
