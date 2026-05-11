# SRT 字幕工具

對著影片 / 錄音檔自動產生繁體中文 SRT 字幕,或校正既有 SRT 的錯字與專有名詞。

## 功能

- **從零產生 SRT**:用 **Groq Whisper Large v3** 做語音轉錄(LPU 加速,純 ASR,不會偷懶)
- **校正既有 SRT**:用 **Gemini** 對著音檔重聽,修正 ASR 錯字 + 專有名詞拼字 + 排版
- **媒體轉檔**:11 種格式可選(mp3 / wav / m4a / mp4 H.264 / H.265 / mkv / webm 等)
- **勘誤表**:校正後抽出修正詞對,可一鍵加入 Glossary
- **SQLite 額度追蹤**:跨 session 累積每日 API 用量,PT 午夜重置
- **彩色 log**:警告黃 / 錯誤紅 / 成功綠

## 安裝

```cmd
:: 1. 第一次跑(建 venv + 裝套件)
setup.bat

:: 2. 複製 .env 範本並填 key
copy .env.example .env
notepad .env   :: 填 GEMINI_API_KEY 跟 GROQ_API_KEY

:: 3. 啟動 GUI
launch.bat
```

申請 API key:
- **Gemini**(校正用):https://aistudio.google.com/apikey(免費)
- **Groq**(轉錄用):https://console.groq.com/keys(免費)

## 檔案組織

工具輸出會在 **輸入檔同目錄下** 自動建立 `output/` 子資料夾:

```
你的影片資料夾/
├── 影片.mp4               ← 你的輸入
└── output/
    ├── transcribed/      ← 從零產生的 SRT
    ├── corrected/        ← 校正後 SRT + diff txt
    └── converted/        ← 媒體轉檔
```

## 模型選擇邏輯(自動)

| 模式 | Provider | 預設 |
|---|---|---|
| 校正既有 SRT | Gemini | `gemini-3.1-flash-lite` |
| 從零產生 SRT | Groq | `groq-whisper-large-v3` |

## 額度顯示

- 跨多次 GUI 啟動累積(SQLite at `~/.srt_corrector/usage.db`)
- 換日(Pacific Time)自動歸零
- 額度數字以 [AI Studio dashboard](https://aistudio.google.com/rate-limit) 為準

## 已知限制

- **Groq Whisper**:單檔上限 25 MB(以工具預設 64kbps mono mp3 計,~25 分鐘以內)
- **Gemini Pro 系列**:免費 tier RPD 額度低(2.5-pro = 1000、3.1-pro = 250),適合手動小量校正
- **Gemma 4 不可用**:Google AI Studio 上的 26B / 31B 都不支援 audio input,因此不列入清單

## 結構

```
Side_project/
├── srt_corrector_gui.py    ← 主程式
├── requirements.txt        ← 依賴
├── .env.example            ← 環境變數範本
├── .env                    ← 你的真實 key(.gitignore 已排除)
├── .gitignore
├── setup.bat               ← 一鍵安裝
├── launch.bat              ← 啟動 GUI
└── README.md
```
