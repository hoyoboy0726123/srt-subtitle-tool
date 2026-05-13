# SRT 字幕工具 V2

V1 的繼承者 — 在原本「轉錄 + 校正 + 轉檔」功能上,**新增影片編輯器**:
影片預覽、SRT 同步顯示、片段裁剪、變速輸出。

---

## V1 → V2 變化

| 模組 | V1 | V2 |
|---|---|---|
| 主程式 | `srt_corrector_gui.py` | `srt_tool_v2.py`(新) |
| 介面 | 單頁 | **兩個 Tab**: ①字幕工具(原 V1)② 影片編輯器(全新) |
| 依賴 | customtkinter / dotenv / google-genai / requests / imageio-ffmpeg | + **python-vlc**(需安裝 VLC media player) |
| 字幕同步 | 無 | ✓ 影片右邊有 SRT 滾動列表,當前播放 block 高亮 |
| 影片剪輯 | 無 | ✓ 標起點 + 標終點 → 刪除中間 → 前後接起 → SRT 同步移除 |
| 變速輸出 | 無 | ✓ 0.5x / 0.75x / 1x / 1.25x / 1.5x / 2x,輸出時也加速 |

---

## V2 新功能架構

### Tab 2:影片編輯器

```
┌───────────────────────────────────────────────────────────────┐
│  [選影片] [選 SRT (選填)]                                       │
├──────────────────────────────────────┬────────────────────────┤
│                                       │  SRT 字幕(同步)        │
│         影片播放區                     │  ─────────────────────  │
│         (python-vlc 嵌入)              │  ① 00:00:01 - 00:00:05  │
│                                       │  ▶ 當前 block 高亮      │
│                                       │  ② 00:00:05 - 00:00:09  │
│                                       │  ③ 00:00:09 - 00:00:14  │
│                                       │  ...                    │
│                                       │  (點任一 block → 跳秒)   │
├──────────────────────────────────────┴────────────────────────┤
│  ▶ ⏸  ▶▶ │ ━━━●━━━━━━━━━━ 03:24 / 24:41 │ 速度: [1.0x ▼]      │
├───────────────────────────────────────────────────────────────┤
│  剪輯片段                                                       │
│  [標起點] [標終點] [加入剪輯] [清除]                            │
│  ─────────────────────────────────                              │
│  ✂ 02:15 → 02:48 (33s)  [×]                                    │
│  ✂ 08:30 → 09:12 (42s)  [×]                                    │
├───────────────────────────────────────────────────────────────┤
│  輸出: [mp4 ▼]  ☐ 變速套用  ☐ 同步輸出新 SRT  [開始輸出 ▸]      │
└───────────────────────────────────────────────────────────────┘
```

### Tab 1:字幕工具(原 V1 沿用)

跟 V1 一樣 — 從零產生 SRT / 校正既有 SRT / 媒體轉檔 / 勘誤表。

---

## 技術選型

### 影片播放:python-vlc

為什麼不用其他:
- **OpenCV**:可顯示但音訊難搞,沒有真實 player 體驗
- **PyQt QMediaPlayer**:要全面換 GUI 框架,跟 customtkinter 不相容
- **bundled web view (CEF/PyWebView)**:依賴龐大、難 debug
- **python-vlc** ✓:成熟、Windows/Mac/Linux 都通、可嵌 tkinter `winfo_id()`

### 影片剪輯:ffmpeg concat demuxer

```bash
# 1. 切出 [0, cut_start1], [cut_end1, cut_start2], [cut_end2, end] 三段
# 2. 用 concat demuxer 接起來
ffmpeg -f concat -safe 0 -i list.txt -c copy output.mp4
```

不用 re-encode(c copy)→ 快,品質無損。

### 變速:ffmpeg filter

```bash
# 2x 加速
ffmpeg -i in.mp4 -filter:v "setpts=PTS/2" -filter:a "atempo=2.0" out.mp4
```

SRT 時間軸全部除以速度倍率即可。

### SRT 同步顯示

每 200ms 從 VLC player 取當前播放時間 → binary search SRT blocks → 設 active block 的 tag(背景藍/字體粗)→ 自動 scroll 到該 block。

---

## 安裝

```cmd
:: V2 用獨立 venv
cd V2
setup.bat
copy .env.example .env
notepad .env    :: 填 GEMINI_API_KEY + GROQ_API_KEY

:: 另外要裝 VLC media player(只要裝一次,任何位置)
:: 下載:https://www.videolan.org/vlc/

launch.bat
```

---

## 開發階段

V2 不是一次完成,分階段:

| Phase | 功能 | 狀態 |
|---|---|---|
| 0 | V1 檔案複製 + Tab 結構 + scaffold | 進行中 |
| 1 | python-vlc 嵌入播放 + 基本 transport(play/pause/seek) | TODO |
| 2 | SRT 同步顯示 + 點 block 跳秒 | TODO |
| 3 | 標記起終點 + 剪輯片段清單 | TODO |
| 4 | ffmpeg concat 輸出剪輯後影片 + SRT 同步刪除 | TODO |
| 5 | 變速 preview + ffmpeg setpts 輸出 + SRT 同步 | TODO |
| 6 | 整合測試 + edge case 處理 | TODO |
