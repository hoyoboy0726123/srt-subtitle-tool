# V2 開發路線圖

> 本檔追蹤 V2 工具所有預計 / 進行中 / 已完成功能。每次新增需求 / 修 bug 都更新這份。

## 圖例
- ✅ 已完成 · 🚧 進行中 · ⏳ 待做 · 💭 暫不做

---

## Phase 0-6:V2 既有功能(已完成)

- ✅ Tk 嵌入 VLC 影片播放(direct3d9/directdraw fallback + 軟解)
- ✅ SRT 字幕同步顯示 + 點 block 跳秒 + 當前 block 高亮
- ✅ SRT 字幕**即時顯示在影片畫面**(VLC subtitle track)
- ✅ SRT 雙擊行內編輯 + 另存 `output/edited/<stamp>.srt`
- ✅ 剪輯片段:標起終點 / 加入清單 / 排序去重疊
- ✅ 剪輯預覽:套用後重新載入(ffmpeg concat),一鍵還原
- ✅ 框選裁切區域(截當前 frame → 拖曳) + **即時預覽**
- ✅ 文字疊加:字型 / 顏色 / 字級 / 位置 / 描邊(無/黑/白)
- ✅ 文字疊加時間區間(出現/消失點)+ VLC marquee 即時預覽
- ✅ 文字疊加拖曳定位(截圖上拖)
- ✅ 變速 0.5x ~ 2.0x(VLC 即時 + 輸出套用)
- ✅ ffmpeg pipeline:cuts + crop + subtitles 燒入 + drawtext + setpts
- ✅ 輸出檔分資料夾:`output/transcribed/`、`corrected/`、`converted/`、`edited/`

---

## Phase 7:本次任務(P1)

### 7.1 ✅ AI 自動字幕翻譯(Gemini)— 2026-05-12
- SRT panel 「🌐 翻譯」按鈕 + 語言選擇對話框(10 種語言)
- Gemini 2.5 Flash 分 batch (40 blocks/次) 翻譯,JSON mode 保證對應
- 輸出 `output/translated/<name>.<lang>.<stamp>.srt`,可載回主介面

### 7.2 ✅ 靜音自動偵測剪輯 — 2026-05-12
- 剪輯卡 「🤖 偵測靜音」按鈕
- 參數對話框:噪音閾值(dB,預設 -30)+ 最短持續(秒,預設 1.5)
- ffmpeg silencedetect → 解析 stderr 抓 silence_start / silence_end
- 結果列表,checkbox 勾選後一鍵加入剪輯清單(自動去重疊)

### 7.3 ✅ 項目存檔 / 開啟 — 2026-05-12
- 頂部「📂 開啟項目」「💾 存檔項目」按鈕
- 副檔名 `.editproj.json`
- 完整存:media_path / srt_path / cuts / crop / overlays / speed / audio / 輸出設定
- 開啟後自動載 media + SRT + 還原全部編輯狀態

### 7.4 ✅ 長寬比裁切預設 — 2026-05-12
- 裁切卡新增 3 個快捷按鈕:**9:16 直** / **1:1 方** / **16:9 橫**
- 自動依影片解析度算最大內接矩形 + 置中
- 對齊偶數,跟手動框選並存
- 即時 VLC 預覽

### 7.5 ✅ 音訊控制 — 2026-05-12
- 🎵 音訊卡:原音音量滑桿(0~200%)+ 靜音 checkbox
- 背景音樂(mp3/wav/m4a/...)選擇 + 音量滑桿
- ffmpeg pipeline 多 case 處理:
  - 純音量改:`-filter:a volume=X`
  - 純靜音:`-an`
  - 背景音樂混音:`-filter_complex` 多軌 amix
  - 靜音 + 背景音:bg 當唯一音軌
- 背景音用 `-stream_loop -1` 自動重複到影片結束

### 7.6 ✅ 時間軸縮圖 — 2026-05-12
- scrubber 下方 72px Canvas
- 載入影片時背景 thread:ffmpeg 抽 20 張 + Pillow 拼成長條
- 紅色直線指示當前播放位置(tick 同步)
- 時間刻度標記(每分鐘一格)
- 點任意位置直接 seek

---

## Phase 8+:未來(P2)

### 影像處理
- ⏳ 明亮度 / 對比 / 飽和度 / 色溫(ffmpeg `eq`)
- ⏳ 旋轉 90° / 180° / 水平翻轉
- ⏳ 淡入淡出(fade in/out)
- ⏳ 片段過渡 crossfade
- ⏳ 去手震 deshake
- ⏳ 倒帶播放 reverse

### 圖像疊加
- ⏳ Logo / 浮水印圖片(透明 PNG)
- ⏳ 多張圖片時間軸
- ⏳ AI 生成片頭(Pillow 畫圖)

### AI 進階
- ⏳ Gemini 自動章節 / 摘要
- ⏳ Gemini 自動裁剪建議(精彩片段)
- ⏳ Gemini 自動標題 / 標籤生成(YouTube / 社群)
- ⏳ 雙語字幕燒入(中+英並列)

### 工作流 UX
- ⏳ 拖放載入(影片 / SRT)
- ⏳ Ctrl+Z / Ctrl+Y 撤銷重做
- ⏳ 快捷鍵(Space 播停 / J K L 倒停順 / I O 起終 / Del 刪當前剪輯)
- ⏳ 當前 frame 截圖另存(PNG 海報)
- ⏳ GIF 輸出(短片預覽)
- ⏳ 批次處理(多影片套同設定)
- ⏳ Audio waveform 視覺化(時間軸下加波形)

### Codec / 品質
- ⏳ 品質預設(行動 / 網路 / 高清 / 無損)
- ⏳ GPU 加速編碼(h264_nvenc / qsv)
- ⏳ 兩階段編碼(2-pass)
- ⏳ 10-bit / HDR
- ⏳ 多音軌 / 多字幕軌
- 💭 串流推送(RTMP)

---

## 依賴(目前)
- `customtkinter`、`python-dotenv`、`google-genai`、`requests`、`imageio-ffmpeg`、`python-vlc`、`Pillow`

需新增依賴的功能會標註(目前 P1 都不用裝新東西)。

---

## 開發紀律
- 改 code 前先看這份 roadmap 確定優先順序
- 完成一個 item 把 ⏳ 改 ✅,加完成日期
- 新需求加進 P2 末端
- ffmpeg pipeline 順序固定:**cuts(stream copy)→ subtitles 燒入 → 圖像 filter(crop/eq/...)→ drawtext → setpts**
- 任何 filter 新增都要更新 `_ffmpeg_export` 的 chain 順序
