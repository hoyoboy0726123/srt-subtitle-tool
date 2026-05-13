# 多音軌 + 麥克風錄音 — 實作計畫

> 給「影片 + 背景音樂 + 旁白(預錄 / 即錄)」的後製場景。
> 寫完先給 Michael 過,確認方向再動工。

---

## 1. 釐清需求

| 場景 | 你要的 |
|---|---|
| 拿到原片 + 找了首 bg music | 原音 + bg → 混成一條輸出(✓ 目前已支援) |
| 上面再加事先錄好的旁白 mp3 | 原音 + bg + 旁白 → 混成一條(目前可以,只是 UI 都叫「背景音」) |
| **想對著影片即時錄旁白** | 邊播邊用麥克風錄,**這條是新功能** |
| 想保留多軌不混音(後續另外處理) | mp4 容器保留多條 audio stream(進階,可選) |

**結論**:核心是「擴充音訊來源類型 + 加 mic 錄音」,**混音邏輯不用大改**(現有 amix 已能吃 N 條)。

---

## 2. 資料模型重構

### 現在
```python
self.bg_music_clips: list[dict] = []
# 每筆 {path, start_sec, end_sec, volume}
```

### 改成 — 給每軌加 `type` 標記
```python
self.audio_tracks: list[dict] = []
# 每筆 {
#   type: "music" | "voiceover" | "mic",
#   path: Path,
#   start_sec, end_sec, volume,
#   loop: bool,   # 只有 music 需要(背景 loop 到結束)
# }
```

UI / timeline / pipeline 全部讀 `audio_tracks` 不分種類,只是顯示 icon / 顏色不同。

### Backward compat
`_open_project()` 載入舊 `.editproj.json` 時把 `bg_clips` 轉換:
```python
for c in old_bg_clips:
    audio_tracks.append({**c, "type": "music", "loop": True})
```

---

## 3. UI 改動

### 音訊卡(現有區塊改造)

```
┌─ 🎵 音訊控制 ──────────────────────────┐
│ 原音音量 [━━━●━━] 100%  ☐ 靜音        │
│                                         │
│ 加音樂  [選 mp3] [起 mm:ss] ~ [結]      │
│         音量 [━━━●━━] 30%  [+ 音樂]    │
│ 加旁白  [選 mp3] [起 mm:ss] ~ [結]      │
│         音量 [━━━●━━] 100% [+ 旁白]    │
│ 即錄    [🎤 麥克風 ▼ device]            │
│         [⚫ 開始錄音] (錄時:00:23 ⏹)   │
│                                         │
│ ─────                                   │
│ 🎵 bgm.mp3 · 0:00~end · 30%        [×]  │
│ 🎤 voice_record_1.wav · 0:15~1:30 · 100% [×]│
│ 🎙 narration.mp3 · 2:00~end · 80%   [×]  │
└─────────────────────────────────────────┘
```

- 「加音樂」「加旁白」**共用底下同一個清單**,只是 type 標記不同
- 「即錄」用麥克風直接錄,完成後 auto-add 到清單
- 每筆 row 用 emoji 表示類型:🎵 音樂 / 🎙 旁白 / 🎤 麥克風

### Timeline track(現有音訊 track)
保留 1 條 audio track,但 bar 顏色依 type:
- 🎵 music = 粉色(#db2777)
- 🎙 voiceover = 紫色(#7c3aed)
- 🎤 mic = 紅色(#dc2626)

---

## 4. 麥克風錄音實作

### 新依賴
```
sounddevice>=0.4.6   # PortAudio binding,Windows 開箱即用
soundfile>=0.12.0    # 寫 wav
```

無需另外安裝 dll(sounddevice wheel 帶 portaudio binary)。

### 錄音流程

```python
import sounddevice as sd
import soundfile as sf

# 列出可用裝置
devices = sd.query_devices()
# 用戶從下拉選 input device

# 開始錄音(callback 模式 → 邊錄邊寫 wav,不一次全進 RAM)
def callback(indata, frames, time, status):
    sf_file.write(indata)

sf_file = sf.SoundFile(tmp_path, mode='w', samplerate=48000, channels=1)
stream = sd.InputStream(samplerate=48000, channels=1, callback=callback,
                       device=selected_device_idx)
stream.start()

# 停止
stream.stop(); stream.close(); sf_file.close()
```

### UI 互動

```
[🎤 麥克風 ▼]  ← 預設選系統 default device
[⚫ 開始錄音]
   ↓ 按下後變
[⏹ 停止 · 錄音中 00:23]
   ↓ 停止後 auto-add:
   🎤 mic_recording_20260513_174523.wav · 全片 · 100%
```

### 邊播邊錄(killer feature)

```
□ 邊播邊錄(同步影片時間軸)
```
勾起來時,按「開始錄音」會:
1. VLC 立刻 `play()` 從當前位置
2. 同時開 mic stream
3. 停止時:
   - 算實際錄音 duration
   - 把音檔 start_sec 設為**錄音開始時 VLC 的時間軸位置**
   - end_sec 設為 start + duration
   - 自動加進 audio_tracks(用戶可後續調整或拖 timeline track)

### 邊角

- **延遲**:PortAudio 預設 ~10-30ms 延遲。對 podcast 旁白 OK,要嚴格對嘴可加 `--latency=low`
- **格式**:錄成 wav(無損),用戶之後可在輸出時轉 mp3
- **大小**:60min 單聲道 48kHz = ~340 MB wav。錄完可選自動壓 mp3 並刪 wav
- **儲存路徑**:`output/recordings/<media>_voice_<stamp>.wav`
- **失敗處理**:macbook / no mic → 錯誤對話框「找不到輸入裝置」

---

## 5. ffmpeg pipeline(基本不變)

現在的 amix 路徑已支援 N 個音訊輸入,只要把 audio_tracks 全部送進去就好:

```python
# 原本
for clip in bg_clips:
    cmd += ["-stream_loop", "-1" if loop else "0", "-i", str(clip["path"])]

# 改成
for tr in audio_tracks:
    loop_arg = ["-stream_loop", "-1"] if tr.get("loop") and tr["type"] == "music" else []
    cmd += [*loop_arg, "-i", str(tr["path"])]
```

filter_complex 每個 input 算 adelay + atrim + volume → amix。

旁白 / 麥克風 **不 loop**(只播一次,結束就沒了),只有音樂 loop。

---

## 6. 進階:真.多音軌輸出(可選 phase 2)

如果用戶要 mp4 內保留多條獨立 audio stream(後製到 DaVinci 才分軌混):

```python
# 不 amix,各軌獨立 -map 進 container
cmd += ["-map", "0:v"]
cmd += ["-map", "0:a"]                # 原音
cmd += ["-map", f"{i+1}:a"]            # 每條 audio track
cmd += ["-c:a", "aac"]                 # 各自編碼
```

UI 加 checkbox「□ 保留多軌(不混音)」。預設 OFF。

但要注意:
- mp4 多 audio stream **大部分播放器只播第一條**(YouTube/IG/手機)
- 主要對「導入 DaVinci/Premiere 後製」場景有意義
- 大多數使用者不會用 → **預設不做,有需要再加**

---

## 7. 實作分階段

| 階段 | 內容 | 大概難度 |
|---|---|---|
| **Phase A** | audio_tracks 重構 + type 標記 + UI 區分加音樂/旁白 | 小 |
| **Phase B** | 麥克風錄音(裝置選擇 + 錄音 + auto-add) | 中(新依賴 + thread 管理) |
| **Phase C** | 邊播邊錄(同步影片時間軸) | 小(在 B 完成基礎上加) |
| **Phase D** | Timeline track 多色顯示 | 小 |
| **Phase E** | (可選)真.多軌輸出 | 中(輸出 UI + pipeline 分支) |

---

## 8. 給 Michael 的問題

1. **真.多軌輸出(Phase E)** 要做嗎?
   - 你日常出片直接給 YouTube/IG → **不需要**,Phase A-D 就夠
   - 你後製還會再用 DaVinci/Premiere → **需要**,加 checkbox 保留多軌
   - 我的猜:不需要,Phase A-D 即可

2. **邊播邊錄(Phase C)** 是必須嗎?
   - 對著影片即時旁白 → **必須**
   - 一律先錄好再導入 → 跳過 C
   - 我的猜:必須,這是後製旁白核心場景

3. **舊 `bg_music_clips` 命名**:重構成 `audio_tracks` OK 嗎?還是維持 `bg_music_clips` 但加 type 欄位?
   - 重構乾淨但 .editproj.json 要 migration
   - 我的傾:重構,反正還早

4. **錄完自動壓縮 mp3** 還是保留 wav?
   - wav 大但無損
   - mp3 小但有壓縮
   - 折衷:錄成 wav,輸出時 ffmpeg 自動轉 mp3

---

## 9. 預期最終體驗(以 Michael 講教學影片為例)

```
1. 載入 24min 教學影片
2. 看到中段 5:00 ~ 7:00 想加旁白補充
3. seek 到 5:00,勾「邊播邊錄」,按「⚫ 開始錄音」
4. 影片開始播放,你對著 mic 講話補充
5. 講到 7:00 按「⏹ 停止」
6. 工具自動把錄到的 wav 加進清單:
   🎤 voice_record_20260513_180012.wav · 5:00~7:00 · 100%
7. 在 timeline track 上看到一條紅色 bar 對齊 5:00~7:00
8. 想精修就拖 bar 邊緣調整起終
9. 設原音音量 60%(略降)+ bg music 20%
10. 輸出 → ffmpeg amix 3 條混成一條輸出 mp3 track
```

---

## 10. 進度更新

- [ ] Phase A 收到綠燈
- [ ] Phase B 收到綠燈
- [ ] Phase C 收到綠燈
- [ ] Phase D 收到綠燈
- [ ] Phase E(可選)
