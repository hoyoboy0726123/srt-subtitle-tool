"""SRT 字幕工具 V2 — 影片編輯器

新功能(相對 V1):
- 影片播放(python-vlc 嵌入 tkinter)
- SRT 字幕同步顯示在右側面板,當前播放 block 高亮
- 點任一字幕 block 跳到該秒
- 剪輯片段:標起點 / 標終點 / 加入剪輯清單 → 一鍵 ffmpeg concat 輸出
- 變速:0.5x ~ 2x preview + 輸出時套用

V1 的轉錄 / 校正 / 轉檔功能保留在父資料夾的 srt_corrector_gui.py。
V2 主要目的是給「拿到 SRT 後想剪片 + 對字幕」的場景。

依賴 VLC media player:https://www.videolan.org/vlc/
"""

import os, sys, re, json, time, threading, subprocess, tempfile, shutil, atexit, hashlib
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

# 圖片疊加即時預覽:縮放後圖檔的暫存目錄(啟動時清空、結束時刪除)
IMG_CACHE_DIR = Path(tempfile.gettempdir()) / "srt_v2_imgcache"

OUTPUT_FORMATS = {
    "mp4 · H.264 (預設)":   (".mp4", ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-c:a", "aac", "-b:a", "128k"]),
    "mp4 · H.265 (HEVC)":   (".mp4", ["-c:v", "libx265", "-preset", "medium", "-crf", "28", "-c:a", "aac", "-b:a", "128k"]),
    "mkv · H.264":          (".mkv", ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-c:a", "aac", "-b:a", "128k"]),
    "webm · VP9":           (".webm", ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-c:a", "libopus", "-b:a", "96k"]),
    "mp3 · 音訊 (192k)":    (".mp3", ["-vn", "-acodec", "libmp3lame", "-ab", "192k"]),
    "wav · 音訊":           (".wav", ["-vn", "-acodec", "pcm_s16le"]),
}

SPEED_OPTIONS = ["0.5x", "0.75x", "1.0x", "1.25x", "1.5x", "1.75x", "2.0x"]

TS_RE = re.compile(r"^\d{2}:\d{2}:\d{2}[,\.]\d{3} --> \d{2}:\d{2}:\d{2}[,\.]\d{3}$")

# ---------- 文字疊加 預設選項 ----------
# Windows 字型清單。若 ttc 不存在,啟動時會自動 fallback 到第一個可用的
_WINFONTS = Path(r"C:/Windows/Fonts")
TEXT_FONTS = {
    "微軟正黑體":       _WINFONTS / "msjh.ttc",
    "微軟正黑體 粗體":   _WINFONTS / "msjhbd.ttc",
    "微軟雅黑體":       _WINFONTS / "msyh.ttc",
    "標楷體":          _WINFONTS / "kaiu.ttf",
    "新細明體":         _WINFONTS / "mingliub.ttc",
    "Arial":           _WINFONTS / "arial.ttf",
    "Arial 粗體":      _WINFONTS / "arialbd.ttf",
    "Consolas":        _WINFONTS / "consola.ttf",
    "Times New Roman": _WINFONTS / "times.ttf",
}
TEXT_COLORS = {
    "白色":  "white",
    "黑色":  "black",
    "紅色":  "#ef4444",
    "黃色":  "#fde047",
    "藍色":  "#3b82f6",
    "綠色":  "#22c55e",
    "粉紅":  "#ec4899",
    "橘色":  "#f97316",
}
TEXT_SIZES = {
    "小 (24)":    24,
    "中 (36)":    36,
    "大 (56)":    56,
    "特大 (80)":  80,
}
# 描邊樣式 → (border_enabled, border_color)
TEXT_BORDERS = {
    "無":     (False, None),
    "黑邊":   (True,  "black"),
    "白邊":   (True,  "white"),
}
# 位置 → (x_expr, y_expr) ffmpeg expression(text_w, text_h, w, h 是 drawtext 內建變數)
TEXT_POSITIONS = {
    "頂部中央":   ("(w-text_w)/2", "30"),
    "頂部左側":   ("30",           "30"),
    "頂部右側":   ("w-text_w-30",  "30"),
    "畫面中央":   ("(w-text_w)/2", "(h-text_h)/2"),
    "底部中央":   ("(w-text_w)/2", "h-text_h-30"),
    "底部左側":   ("30",           "h-text_h-30"),
    "底部右側":   ("w-text_w-30",  "h-text_h-30"),
}

# tk Font family — 用於 drag dialog 顯示文字 preview
TEXT_TK_FAMILY = {
    "微軟正黑體":       "Microsoft JhengHei",
    "微軟正黑體 粗體":   "Microsoft JhengHei",
    "微軟雅黑體":       "Microsoft YaHei",
    "標楷體":          "DFKai-SB",
    "新細明體":         "PMingLiU",
    "Arial":           "Arial",
    "Arial 粗體":      "Arial",
    "Consolas":        "Consolas",
    "Times New Roman": "Times New Roman",
}


def parse_time_str(s: str) -> float | None:
    """空白 → None;'1:30' → 90.0;'90' → 90.0;'1:23:45' → 5025.0"""
    s = (s or "").strip()
    if not s:
        return None
    try:
        if ":" in s:
            parts = s.split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        return float(s)
    except (ValueError, TypeError):
        return None


def sec_to_short_ts(t: float | None) -> str:
    if t is None:
        return "—"
    m = int(t // 60); s = t - m * 60
    return f"{m}:{s:05.2f}" if m or s >= 10 else f"0:{s:05.2f}"


# ============================================================
# 工具函數
# ============================================================

def get_ffmpeg():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


# Windows 上每個 subprocess 都會跳一個 console 黑窗 → monkey-patch 強制隱藏。
# 影響:本程式所有 subprocess.run / Popen 都不再跳窗(包含 ffmpeg/python launcher)。
if sys.platform == "win32":
    _NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _orig_run = subprocess.run
    _orig_popen = subprocess.Popen
    def _silent_run(*args, **kwargs):
        kwargs.setdefault("creationflags", _NO_WIN)
        return _orig_run(*args, **kwargs)
    class _SilentPopen(_orig_popen):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("creationflags", _NO_WIN)
            super().__init__(*args, **kwargs)
    subprocess.run = _silent_run
    subprocess.Popen = _SilentPopen


# ASS subtitle 字幕燒入樣式 — PrimaryColour / OutlineColour 是 ASS 的 &HAABBGGRR(BGR 順序)
SUBTITLE_STYLES = {
    "白字黑邊(預設)":  {"primary": "&H00FFFFFF", "outline": "&H00000000", "border": True},
    "黑字白邊":         {"primary": "&H00000000", "outline": "&H00FFFFFF", "border": True},
    "黃字黑邊":         {"primary": "&H0000FFFF", "outline": "&H00000000", "border": True},
    "白字無邊":         {"primary": "&H00FFFFFF", "outline": "&H00000000", "border": False},
    "黑字無邊":         {"primary": "&H00000000", "outline": "&H00FFFFFF", "border": False},
}

# 主字幕字級 — 第二字幕自動取一半
SUBTITLE_SIZES = {
    "小": 17,   # 大的 70%(原本的小變成大)
    "大": 24,   # 原本的小
}

# 第二字幕相對主字幕的比例
SUBTITLE_SECONDARY_SCALES = {
    "小": 0.5,    # 主的一半(預設)
    "大": 0.75,   # 主的 75%(= 小的 1.5 倍)
}


def output_dir(source_path: Path, kind: str) -> Path:
    d = source_path.parent / "output" / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def srt_ts_to_sec(ts: str) -> float:
    """'00:01:23,456' → 83.456"""
    ts = ts.replace(",", ".")
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def sec_to_srt_ts(t: float) -> str:
    """83.456 → '00:01:23,456'"""
    h = int(t // 3600); t -= h * 3600
    m = int(t // 60); t -= m * 60
    s = int(t)
    ms = int(round((t - s) * 1000))
    if ms == 1000:
        s += 1; ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt(text: str):
    """回 list of (idx:int, start:float, end:float, text_lines:list[str])"""
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
        if len(b) < 3:
            continue
        try:
            idx = int(b[0].strip())
        except ValueError:
            continue
        if not TS_RE.match(b[1].replace(".", ",")):
            continue
        try:
            ts_parts = b[1].split(" --> ")
            st = srt_ts_to_sec(ts_parts[0].strip())
            ed = srt_ts_to_sec(ts_parts[1].strip())
        except Exception:
            continue
        out.append((idx, st, ed, b[2:]))
    return out


def serialize_srt(parsed) -> str:
    """parsed → SRT 字串"""
    out = []
    for i, (_, st, ed, lines) in enumerate(parsed, start=1):
        out.append(str(i))
        out.append(f"{sec_to_srt_ts(st)} --> {sec_to_srt_ts(ed)}")
        out.extend(lines)
        out.append("")
    return "\n".join(out)


def get_media_duration(media_path: Path) -> float:
    """用 ffmpeg 拿時長,中文路徑 UTF-8 安全。"""
    r = subprocess.run(
        [get_ffmpeg(), "-i", str(media_path), "-hide_banner"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", r.stderr or "")
    if not m:
        return 0.0
    hh, mm, ss, cs = (int(x) for x in m.groups())
    return hh * 3600 + mm * 60 + ss + cs / 100.0


def get_video_dimensions(media_path: Path) -> tuple[int, int]:
    """用 ffmpeg 拿視訊解析度 (w, h)。回 (0,0) 表示失敗。"""
    r = subprocess.run(
        [get_ffmpeg(), "-i", str(media_path), "-hide_banner"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    # Stream #0:0(...): Video: ... , 1920x1080 ...
    m = re.search(r"Video:.+?,\s*(\d+)x(\d+)", r.stderr or "")
    if not m:
        return 0, 0
    return int(m.group(1)), int(m.group(2))


# ============================================================
# VLC player wrapper
# ============================================================

class VLCPlayer:
    """python-vlc 嵌入 tk frame 的封裝。"""
    def __init__(self, host_frame, log_fn=None):
        import vlc
        self.vlc = vlc
        self.log_fn = log_fn or (lambda m: print(m, flush=True))
        # 允許環境變數覆寫 vout(SRT_VOUT=directdraw / direct3d9 / direct3d11 / opengl)
        # 預設用 directdraw — 嵌入 tk hwnd 時 d3d11/d3d9 都會狂吐 get_buffer fail,
        # directdraw 是最古老但最兼容的 windows 視訊輸出
        vout = os.environ.get("SRT_VOUT", "directdraw" if sys.platform == "win32" else "")
        # --no-sub-autodetect-file:禁止 VLC 自動撈同目錄 .srt(會撈到舊 SRT 蓋掉預期內容)
        args = ["--no-xlib", "--quiet", "--avcodec-hw=none", "--no-sub-autodetect-file"]
        if vout:
            args.append(f"--vout={vout}")
        self.log_fn(f"[VLC] instance args: {args}")
        self.instance = vlc.Instance(*args)
        if self.instance is None:
            raise RuntimeError("vlc.Instance() 回傳 None — libvlc 初始化失敗,可能 VLC 安裝壞了或 args 不合法")
        self.player = self.instance.media_player_new()
        self.host = host_frame
        # 強制 frame realize + 取 hwnd
        self.host.update()
        hwnd = self.host.winfo_id()
        self.log_fn(f"[VLC] host hwnd = {hwnd}  size = {self.host.winfo_width()}x{self.host.winfo_height()}")
        if not hwnd or hwnd == 0:
            raise RuntimeError("video_host hwnd is 0 — frame 還沒 realize")
        if sys.platform == "win32":
            self.player.set_hwnd(hwnd)
        elif sys.platform == "darwin":
            self.player.set_nsobject(hwnd)
        else:
            self.player.set_xwindow(hwnd)
        self.duration = 0.0
        self.media_path: Path | None = None

    def load(self, path: Path):
        self.media_path = path
        media = self.instance.media_new(str(path))
        self.player.set_media(media)
        # 解析 metadata 取時長
        media.parse_with_options(self.vlc.MediaParseFlag.local, 5000)
        # vlc 的 duration 在 play 後才會更新到準確值,先用 ffmpeg 取
        self.duration = get_media_duration(path)

    def play(self):
        self.player.play()

    def pause(self):
        self.player.pause()

    def stop(self):
        self.player.stop()

    def is_playing(self) -> bool:
        return bool(self.player.is_playing())

    def is_ended(self) -> bool:
        """影片是否已播畢(VLC Ended 狀態;此時 play() 無法直接重播)。"""
        try:
            return self.player.get_state() == self.vlc.State.Ended
        except Exception:
            return False

    def replay(self):
        """從頭重新播放。Ended 狀態下 play() 無效,需先 stop 重置再播。"""
        self.player.stop()
        self.player.play()

    def get_time_sec(self) -> float:
        ms = self.player.get_time()
        return ms / 1000.0 if ms >= 0 else 0.0

    def set_time_sec(self, sec: float):
        self.player.set_time(int(sec * 1000))

    def set_rate(self, rate: float):
        self.player.set_rate(rate)

    def get_volume(self) -> int:
        return self.player.audio_get_volume()

    def set_volume(self, vol: int):
        self.player.audio_set_volume(max(0, min(100, vol)))


# ============================================================
# 框選裁切 Dialog
# ============================================================

class CropDialog(ctk.CTkToplevel):
    """暫停 → 截圖 → 用滑鼠拖曳在截圖上框選保留區域 → 套到輸出 ffmpeg crop filter。"""
    def __init__(self, parent, frame_path: Path, on_confirm):
        super().__init__(parent)
        self.title("框選裁切區域")
        self.resizable(False, False)
        self.on_confirm = on_confirm
        self.attributes("-topmost", True)

        from PIL import Image, ImageTk
        self.pil_img = Image.open(frame_path)
        self.orig_w, self.orig_h = self.pil_img.size

        # 縮放到 fit 1080x680(留給 UI 元件空間)
        max_w, max_h = 1080, 640
        scale = min(max_w / self.orig_w, max_h / self.orig_h, 1.0)
        disp_w = int(self.orig_w * scale)
        disp_h = int(self.orig_h * scale)
        self.scale = scale
        display_img = self.pil_img.resize((disp_w, disp_h), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(display_img)

        # 視窗大小
        self.geometry(f"{max(disp_w + 40, 520)}x{disp_h + 140}")

        # 提示
        ctk.CTkLabel(
            self, text=f"原始解析度 {self.orig_w}×{self.orig_h}  ·  用滑鼠拖曳框選要保留的區域",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(pady=(8, 4))

        # Canvas
        import tkinter as tk
        self.canvas = tk.Canvas(self, width=disp_w, height=disp_h,
                                 bg="black", highlightthickness=1,
                                 highlightbackground="#3b82f6")
        self.canvas.pack(padx=10, pady=4)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")

        # 互動狀態
        self.rect_id = None
        self.dim_label_id = None
        self.start_x = self.start_y = None
        self.end_x = self.end_y = None
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

        # 狀態顯示
        self.info_lbl = ctk.CTkLabel(
            self, text="尚未選取(拖動滑鼠開始框選)",
            font=ctk.CTkFont(family="Consolas", size=11),
            text_color=("#6b7280", "#9ca3af"),
        )
        self.info_lbl.pack(pady=(4, 0))

        # 按鈕列
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(pady=8)
        ctk.CTkButton(btn_row, text="取消", width=80,
                      command=self.destroy).pack(side="left", padx=4)
        ctk.CTkButton(btn_row, text="重畫", width=80,
                      command=self._reset).pack(side="left", padx=4)
        self.confirm_btn = ctk.CTkButton(
            btn_row, text="✓ 確認套用", width=120, state="disabled",
            fg_color="#16a34a", hover_color="#15803d",
            font=ctk.CTkFont(weight="bold"),
            command=self._confirm,
        )
        self.confirm_btn.pack(side="left", padx=4)

    def _on_press(self, event):
        self.start_x, self.start_y = event.x, event.y
        self._reset_rect()
        self.rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="#ef4444", width=2,
        )

    def _on_drag(self, event):
        if self.start_x is None:
            return
        self.end_x, self.end_y = event.x, event.y
        self.canvas.coords(self.rect_id, self.start_x, self.start_y, event.x, event.y)
        # 原始座標
        x, y, w, h = self._current_orig_coords()
        self.info_lbl.configure(
            text=f"原始視訊座標:  {w}×{h}  @  ({x}, {y})",
            text_color=("#1f2328", "#e6edf3"),
        )
        # 在 rect 角落顯示尺寸
        if self.dim_label_id:
            self.canvas.delete(self.dim_label_id)
        self.dim_label_id = self.canvas.create_text(
            min(self.start_x, event.x) + 4, min(self.start_y, event.y) - 8,
            text=f"{w}×{h}", fill="#fef08a", anchor="w",
            font=("Consolas", 10, "bold"),
        )

    def _on_release(self, event):
        if self.start_x is None or self.end_x is None:
            return
        if abs(self.end_x - self.start_x) < 10 or abs(self.end_y - self.start_y) < 10:
            self.info_lbl.configure(text="選取範圍太小(< 10 px)",
                                     text_color=("#dc2626", "#fca5a5"))
            return
        self.confirm_btn.configure(state="normal")

    def _reset_rect(self):
        if self.rect_id:
            self.canvas.delete(self.rect_id); self.rect_id = None
        if self.dim_label_id:
            self.canvas.delete(self.dim_label_id); self.dim_label_id = None

    def _reset(self):
        self._reset_rect()
        self.start_x = self.start_y = None
        self.end_x = self.end_y = None
        self.info_lbl.configure(text="尚未選取(拖動滑鼠開始框選)",
                                 text_color=("#6b7280", "#9ca3af"))
        self.confirm_btn.configure(state="disabled")

    def _current_orig_coords(self) -> tuple[int, int, int, int]:
        """回 (x, y, w, h),clamp 到影片範圍內,並對 2 取整(ffmpeg crop 要 even)。"""
        x = min(self.start_x, self.end_x)
        y = min(self.start_y, self.end_y)
        w = abs(self.end_x - self.start_x)
        h = abs(self.end_y - self.start_y)
        ox = int(x / self.scale)
        oy = int(y / self.scale)
        ow = int(w / self.scale)
        oh = int(h / self.scale)
        # clamp + even
        ox = max(0, min(ox, self.orig_w - 2))
        oy = max(0, min(oy, self.orig_h - 2))
        ow = min(ow, self.orig_w - ox)
        oh = min(oh, self.orig_h - oy)
        if ow % 2: ow -= 1
        if oh % 2: oh -= 1
        return ox, oy, ow, oh

    def _confirm(self):
        x, y, w, h = self._current_orig_coords()
        if w < 2 or h < 2:
            self.info_lbl.configure(text="選取無效", text_color=("#dc2626", "#fca5a5"))
            return
        self.on_confirm((x, y, w, h, self.orig_w, self.orig_h))
        self.destroy()


# ============================================================
# 進度遮罩 Dialog
# ============================================================

class ProgressOverlay(ctk.CTkToplevel):
    """處理中遮罩 — 跑長 ffmpeg / AI 操作時擋住主視窗,顯示忙碌指示器。"""
    def __init__(self, parent, title: str = "處理中", message: str = "請稍候..."):
        super().__init__(parent)
        self.title(title)
        # 居中於 parent
        parent.update_idletasks()
        w, h = 460, 180
        px = parent.winfo_x() + parent.winfo_width() // 2 - w // 2
        py = parent.winfo_y() + parent.winfo_height() // 2 - h // 2
        self.geometry(f"{w}x{h}+{px}+{py}")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.transient(parent)
        try:
            self.grab_set()
        except Exception:
            pass
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # 不可關閉
        self.parent = parent

        ctk.CTkLabel(self, text=message,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(pady=(20, 6))
        self.progress = ctk.CTkProgressBar(self, mode="indeterminate", height=14)
        self.progress.pack(fill="x", padx=22, pady=8)
        self.progress.start()
        self.detail_lbl = ctk.CTkLabel(
            self, text="",
            font=ctk.CTkFont(size=11),
            text_color=("#6b7280", "#9ca3af"),
            wraplength=420,
        )
        self.detail_lbl.pack(pady=(4, 8), padx=14)
        ctk.CTkLabel(
            self, text="⚠️ 請等候完成,期間請勿關閉視窗",
            font=ctk.CTkFont(size=10),
            text_color=("#dc2626", "#fca5a5"),
        ).pack(pady=(0, 8))

    def set_detail(self, text: str):
        try:
            self.detail_lbl.configure(text=text)
        except Exception:
            pass

    def set_progress(self, frac: float):
        """切換為 determinate + 設百分比(0~1)。"""
        try:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress.set(max(0.0, min(1.0, frac)))
        except Exception:
            pass

    def done(self):
        try:
            self.progress.stop()
            self.grab_release()
            self.destroy()
        except Exception:
            pass


# ============================================================
# 文字拖曳定位 Dialog
# ============================================================

class TextDragDialog(ctk.CTkToplevel):
    """顯示當前 frame + 在上面渲染 overlay 文字,用滑鼠拖曳調整位置。
    輸出絕對像素 (x, y) 座標(原始視訊解析度)。"""
    def __init__(self, parent, frame_path: Path, overlay: dict, on_confirm):
        super().__init__(parent)
        self.title(f"拖曳定位:{overlay['text'][:20]}")
        self.resizable(False, False)
        self.on_confirm = on_confirm
        self.attributes("-topmost", True)
        self.overlay = overlay

        from PIL import Image, ImageTk
        self.pil_img = Image.open(frame_path)
        self.orig_w, self.orig_h = self.pil_img.size
        max_w, max_h = 1080, 640
        scale = min(max_w / self.orig_w, max_h / self.orig_h, 1.0)
        self.scale = scale
        disp_w = int(self.orig_w * scale)
        disp_h = int(self.orig_h * scale)
        display_img = self.pil_img.resize((disp_w, disp_h), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(display_img)
        self.geometry(f"{max(disp_w + 40, 540)}x{disp_h + 140}")

        ctk.CTkLabel(
            self, text=f"拖曳調整文字位置  ·  原始 {self.orig_w}×{self.orig_h}  ·  字級 {overlay['size']}",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(pady=(8, 4))

        import tkinter as tk
        self.canvas = tk.Canvas(self, width=disp_w, height=disp_h,
                                 bg="black", highlightthickness=1,
                                 highlightbackground="#0891b2")
        self.canvas.pack(padx=10, pady=4)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")

        # 初始位置:沿用 overlay 的設定。若是 preset → 從 ffmpeg expression 估算初始 px。
        tk_family = TEXT_TK_FAMILY.get(overlay["font_name"], "Arial")
        tk_weight = "bold" if "粗體" in overlay["font_name"] or "Bold" in overlay["font_name"] else "normal"
        scaled_size = max(6, int(overlay["size"] * scale))
        self.tk_font = (tk_family, scaled_size, tk_weight)

        # 計算文字尺寸(用 tk metrics)
        # 用一個臨時 text item 量
        tmp_id = self.canvas.create_text(0, 0, text=overlay["text"], anchor="nw",
                                          font=self.tk_font, fill="white")
        x0, y0, x1, y1 = self.canvas.bbox(tmp_id)
        self.canvas.delete(tmp_id)
        self.text_w_disp = x1 - x0
        self.text_h_disp = y1 - y0

        # 估算初始 px 在原始座標:用 preset 表 map 到大概位置
        ix, iy = self._initial_position()
        self.cur_px = ix
        self.cur_py = iy

        # 主 text item — 用 anchor=nw,座標 = 左上角
        # 描邊用兩個 text 疊(下層黑色 stroke,上層彩色 fill)
        self.text_outline = None
        if overlay.get("border"):
            self.text_outline = self.canvas.create_text(
                0, 0, text=overlay["text"], anchor="nw",
                font=self.tk_font, fill="black",
            )
        color = overlay.get("color", "white")
        # tk 不認 8 位 hex,轉成 6 位
        if color.startswith("#") and len(color) == 9:
            color = color[:7]
        self.text_id = self.canvas.create_text(
            0, 0, text=overlay["text"], anchor="nw",
            font=self.tk_font, fill=color,
        )
        self._move_text_to_orig(self.cur_px, self.cur_py)

        # 滑鼠拖曳
        self.canvas.tag_bind(self.text_id, "<ButtonPress-1>", self._on_press)
        self.canvas.tag_bind(self.text_id, "<B1-Motion>", self._on_drag)
        # 點 canvas 任意處 → 把文字移到滑鼠位置
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_click, add="+")
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag, add="+")

        # 座標 label
        self.coord_lbl = ctk.CTkLabel(
            self, text=self._coord_text(),
            font=ctk.CTkFont(family="Consolas", size=11),
        )
        self.coord_lbl.pack(pady=(4, 0))

        # 按鈕
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(pady=8)
        ctk.CTkButton(btn_row, text="取消", width=80,
                      command=self.destroy).pack(side="left", padx=4)
        ctk.CTkButton(
            btn_row, text="✓ 確認位置", width=120,
            fg_color="#0891b2", hover_color="#0e7490",
            font=ctk.CTkFont(weight="bold"),
            command=self._confirm,
        ).pack(side="left", padx=4)

    def _initial_position(self) -> tuple[int, int]:
        """從現有 overlay 取初始位置 (原始 px)。"""
        if self.overlay.get("pos_mode") == "absolute":
            return self.overlay.get("x_px", 50), self.overlay.get("y_px", 50)
        # preset → 大概對應位置(用顯示文字尺寸 / scale)
        tw = self.text_w_disp / self.scale
        th = self.text_h_disp / self.scale
        name = self.overlay.get("pos_name", "底部中央")
        if "中央" in name and "底部" not in name and "頂部" not in name:
            return int((self.orig_w - tw) / 2), int((self.orig_h - th) / 2)
        x = (self.orig_w - tw) / 2  # default center
        if "左" in name: x = 30
        elif "右" in name: x = self.orig_w - tw - 30
        y = self.orig_h - th - 30  # default bottom
        if "頂部" in name: y = 30
        elif "中央" in name: y = (self.orig_h - th) / 2
        return int(max(0, x)), int(max(0, y))

    def _move_text_to_orig(self, px: float, py: float):
        """text item 的位置 = scale 過的 (px, py)。"""
        dx = px * self.scale
        dy = py * self.scale
        if self.text_outline:
            self.canvas.coords(self.text_outline, dx + 1, dy + 1)
        self.canvas.coords(self.text_id, dx, dy)
        self.cur_px = int(px)
        self.cur_py = int(py)

    def _on_press(self, event):
        self._drag_start_dx = event.x - (self.cur_px * self.scale)
        self._drag_start_dy = event.y - (self.cur_py * self.scale)

    def _on_drag(self, event):
        nx = (event.x - self._drag_start_dx) / self.scale
        ny = (event.y - self._drag_start_dy) / self.scale
        # clamp
        nx = max(0, min(nx, self.orig_w - self.text_w_disp / self.scale))
        ny = max(0, min(ny, self.orig_h - self.text_h_disp / self.scale))
        self._move_text_to_orig(nx, ny)
        self.coord_lbl.configure(text=self._coord_text())

    def _on_canvas_click(self, event):
        # 點空白處 → 把文字中心移到滑鼠位置
        items = self.canvas.find_overlapping(event.x, event.y, event.x, event.y)
        if self.text_id in items:
            return  # 已被 text 的 binding 處理
        # 把文字左上角放在 (event.x, event.y) - half size
        nx = (event.x - self.text_w_disp / 2) / self.scale
        ny = (event.y - self.text_h_disp / 2) / self.scale
        nx = max(0, min(nx, self.orig_w - self.text_w_disp / self.scale))
        ny = max(0, min(ny, self.orig_h - self.text_h_disp / self.scale))
        self._move_text_to_orig(nx, ny)
        self.coord_lbl.configure(text=self._coord_text())
        # 設定 drag 起點為當前位置 → 可繼續拖
        self._drag_start_dx = event.x - (self.cur_px * self.scale)
        self._drag_start_dy = event.y - (self.cur_py * self.scale)

    def _on_canvas_drag(self, event):
        # 同樣的計算
        if hasattr(self, "_drag_start_dx"):
            self._on_drag(event)

    def _coord_text(self) -> str:
        return f"位置 (px,py) = ({self.cur_px}, {self.cur_py})  原始 {self.orig_w}×{self.orig_h}"

    def _confirm(self):
        self.on_confirm((self.cur_px, self.cur_py))
        self.destroy()


# ============================================================
# GUI
# ============================================================

class SRTToolV2(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.title("SRT 字幕工具 V2 — 影片編輯器")
        self.geometry("1520x840+60+30")
        self.minsize(1300, 760)

        self._load_env()

        self.media_path: Path | None = None
        self.srt_path: Path | None = None
        self.srt_blocks: list = []  # parsed SRT
        self.cuts: list[tuple[float, float]] = []  # 剪輯起終 (sec, sec)
        self.cut_in: float | None = None  # 當前標記中的起點
        self.cut_out: float | None = None
        # 框選裁切:(x, y, w, h, orig_w, orig_h) in 原始影片座標
        self.crop_region: tuple[int, int, int, int, int, int] | None = None
        # 裁切若來自長寬比預設,記下比例 (rw, rh) — 預覽改用 VLC 比例裁切
        self._crop_aspect: tuple[int, int] | None = None
        # 文字疊加清單:每筆 dict(text/font_name/font_path/size/color/color_name/position/border)
        self.text_overlays: list[dict] = []
        # 剪輯預覽 state — preview 模式:VLC 載入暫存的剪輯後 mp4 + SRT 重新對應
        self.cut_preview_active = False
        self.original_media_path: Path | None = None
        self.original_srt_blocks: list = []
        # 音訊控制 — 多個背景音 clip,每個 {path, start_sec, end_sec, volume}
        self.bg_music_clips: list[dict] = []
        # 圖片疊加 — 多筆 {path, scale, opacity, pos_mode, pos_name, x_px, y_px, start_sec, end_sec}
        self.image_overlays: list[dict] = []
        # 第二字幕(雙語)
        self.secondary_srt_path: Path | None = None
        self.player: VLCPlayer | None = None
        self.current_active_block_idx: int | None = None

        # 圖片疊加即時預覽(VLC logo filter)— 縮放後暫存 PNG 的快取
        self._img_logo_cache: dict = {}   # {(path, scale): 縮放後 png 路徑}
        self._img_logo_sig = None         # 目前顯示中的 logo 簽章,變了才重設
        try:
            if IMG_CACHE_DIR.exists():
                shutil.rmtree(IMG_CACHE_DIR, ignore_errors=True)
            IMG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        atexit.register(lambda: shutil.rmtree(IMG_CACHE_DIR, ignore_errors=True))

        self._build_ui()
        # tick loop 200ms 更新時間 / 同步字幕高亮
        self.after(200, self._tick)

    def _load_env(self):
        for c in (Path(__file__).parent / ".env", Path.cwd() / ".env"):
            if c.exists():
                load_dotenv(c)
                return
        load_dotenv()

    # ----------------- UI -----------------

    def _toggle_left_panel(self):
        """收折 / 展開左側 SRT 同步面板,讓出空間給中間影片區。"""
        if self._left_panel_collapsed:
            self.srt_col.pack(side="left", fill="y",
                              before=self.left_collapse_btn)
            self.left_collapse_btn.configure(text="◀")
            self._left_panel_collapsed = False
        else:
            self.srt_col.pack_forget()
            self.left_collapse_btn.configure(text="▶")
            self._left_panel_collapsed = True

    def _toggle_right_panel(self):
        """收折 / 展開右側編輯控制面板,讓出空間給中間影片區。"""
        if self._right_panel_collapsed:
            self.edit_col.pack(side="right", fill="y",
                               before=self.right_collapse_btn)
            self.right_collapse_btn.configure(text="▶")
            self._right_panel_collapsed = False
        else:
            self.edit_col.pack_forget()
            self.right_collapse_btn.configure(text="◀")
            self._right_panel_collapsed = True

    def _build_ui(self):
        # 頂部:檔案選擇
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(12, 6))

        title_row = ctk.CTkFrame(top, fg_color="transparent")
        title_row.pack(fill="x")
        title_l = ctk.CTkFrame(title_row, fg_color="transparent")
        title_l.pack(side="left")
        ctk.CTkLabel(title_l, text="SRT 字幕工具 V2 · 影片編輯器",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(title_l, text="影片預覽 · 字幕同步 · 片段裁剪 · 變速輸出",
                     font=ctk.CTkFont(size=11),
                     text_color=("#6b7280", "#8b949e")).pack(anchor="w")
        # 模式切換 + 項目存檔
        proj_row = ctk.CTkFrame(title_row, fg_color="transparent")
        proj_row.pack(side="right", padx=4)
        ctk.CTkButton(proj_row, text="📝 SRT 字幕工具", width=130,
                      fg_color="#7c3aed", hover_color="#6d28d9",
                      font=ctk.CTkFont(weight="bold"),
                      command=self._launch_v1_srt_tool).pack(side="left", padx=2)
        ctk.CTkButton(proj_row, text="📂 開啟項目", width=100,
                      command=self._open_project).pack(side="left", padx=2)
        ctk.CTkButton(proj_row, text="💾 存檔項目", width=100,
                      fg_color="#16a34a", hover_color="#15803d",
                      command=self._save_project).pack(side="left", padx=2)

        file_row = ctk.CTkFrame(self, fg_color="transparent")
        file_row.pack(fill="x", padx=14, pady=4)

        ctk.CTkLabel(file_row, text="影片", width=50, anchor="w",
                     font=ctk.CTkFont(weight="bold")).pack(side="left")
        self.media_entry = ctk.CTkEntry(file_row, placeholder_text="選一個影片檔...")
        self.media_entry.pack(side="left", fill="x", expand=True, padx=4)
        ctk.CTkButton(file_row, text="選影片", width=70,
                      command=self._pick_media).pack(side="left", padx=2)

        srt_row = ctk.CTkFrame(self, fg_color="transparent")
        srt_row.pack(fill="x", padx=14, pady=4)
        ctk.CTkLabel(srt_row, text="SRT", width=50, anchor="w",
                     font=ctk.CTkFont(weight="bold")).pack(side="left")
        self.srt_entry = ctk.CTkEntry(srt_row, placeholder_text="(可選)同名 .srt 會自動載入...")
        self.srt_entry.pack(side="left", fill="x", expand=True, padx=4)
        ctk.CTkButton(srt_row, text="選 SRT", width=70,
                      command=self._pick_srt).pack(side="left", padx=2)

        # 三欄佈局:① SRT 同步(左) ② 影片預覽(中) ③ 編輯控制(右)
        # 左右兩側可「收折」讓出空間給中間影片區;不收折時即原始比例。
        main_row = ctk.CTkFrame(self, fg_color="transparent")
        main_row.pack(fill="both", expand=True, padx=14, pady=8)

        # ① 左:SRT 同步面板(寬 340,可收折)
        srt_col = ctk.CTkFrame(main_row, fg_color=("#f8fafc", "#0d1117"),
                                border_color=("#e2e8f0", "#262b33"),
                                border_width=1, corner_radius=8, width=340)
        srt_col.pack(side="left", fill="y")
        srt_col.pack_propagate(False)
        self.srt_col = srt_col

        # 左收折鈕(細長條,永遠可見)
        self.left_collapse_btn = ctk.CTkButton(
            main_row, text="◀", width=22, corner_radius=0,
            fg_color=("#e2e8f0", "#262b33"),
            hover_color=("#cbd5e1", "#374151"),
            text_color=("#475569", "#9ca3af"),
            command=self._toggle_left_panel,
        )
        self.left_collapse_btn.pack(side="left", fill="y", padx=(2, 6))

        # ③ 右:編輯控制(剪輯/裁切/文字/輸出),寬 460,可收折
        edit_col = ctk.CTkScrollableFrame(main_row, fg_color="transparent",
                                           width=460)
        edit_col.pack(side="right", fill="y")
        self.edit_col = edit_col

        # 右收折鈕(細長條,永遠可見)
        self.right_collapse_btn = ctk.CTkButton(
            main_row, text="▶", width=22, corner_radius=0,
            fg_color=("#e2e8f0", "#262b33"),
            hover_color=("#cbd5e1", "#374151"),
            text_color=("#475569", "#9ca3af"),
            command=self._toggle_right_panel,
        )
        self.right_collapse_btn.pack(side="right", fill="y", padx=(6, 2))

        # ② 中:影片預覽 + transport(占用中間 flex 空間)
        left_col = ctk.CTkFrame(main_row, fg_color="transparent")
        left_col.pack(side="left", fill="both", expand=True)

        self._left_panel_collapsed = False
        self._right_panel_collapsed = False

        # video host — 用原生 tk.Frame,不能用 CTkFrame!
        # CTkFrame 底層會塞一個 Canvas 蓋住 VLC 渲染區(造成有聲無畫面的 bug)。
        # 用 tk.Frame 才能讓 VLC 透過 set_hwnd 真正繪製在這個 hwnd 上。
        import tkinter as _tk
        self.video_host = _tk.Frame(left_col, bg="#000000", height=400, bd=0,
                                     highlightthickness=0)
        self.video_host.pack(fill="both", expand=True)
        # placeholder 用原生 tk.Label,載入影片時會 destroy
        self.video_placeholder = _tk.Label(
            self.video_host, text="影片預覽\n\n選一個影片檔載入...",
            bg="#000000", fg="#6b7280",
            font=("Microsoft JhengHei", 14),
        )
        self.video_placeholder.pack(expand=True, fill="both")

        # 播放控制
        ctrl_row = ctk.CTkFrame(left_col, fg_color="transparent")
        ctrl_row.pack(fill="x", pady=(6, 2))

        self.play_btn = ctk.CTkButton(ctrl_row, text="▶ 播放", width=80,
                                       command=self._toggle_play, state="disabled")
        self.play_btn.pack(side="left")
        ctk.CTkButton(ctrl_row, text="⏮ -5s", width=60,
                      command=lambda: self._seek_rel(-5)).pack(side="left", padx=2)
        ctk.CTkButton(ctrl_row, text="⏭ +5s", width=60,
                      command=lambda: self._seek_rel(5)).pack(side="left", padx=2)
        self.time_lbl = ctk.CTkLabel(ctrl_row, text="00:00 / 00:00",
                                      font=ctk.CTkFont(family="Consolas", size=12))
        self.time_lbl.pack(side="left", padx=10)

        # 速度下拉
        ctk.CTkLabel(ctrl_row, text="速度",
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(20, 4))
        self.speed_var = ctk.StringVar(value="1.0x")
        ctk.CTkOptionMenu(ctrl_row, variable=self.speed_var,
                          values=SPEED_OPTIONS, width=80,
                          command=self._on_speed_change).pack(side="left")

        # 音量
        ctk.CTkLabel(ctrl_row, text="音量",
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(20, 4))
        self.volume_slider = ctk.CTkSlider(ctrl_row, from_=0, to=100, width=120,
                                            command=self._on_volume_change)
        self.volume_slider.set(80)
        self.volume_slider.pack(side="left")
        # 截圖 + GIF 按鈕
        ctk.CTkButton(ctrl_row, text="📷 截圖", width=70,
                      command=self._save_frame_png).pack(side="left", padx=(20, 2))
        ctk.CTkButton(ctrl_row, text="🎞 GIF", width=70,
                      fg_color="#ea580c", hover_color="#c2410c",
                      command=self._export_gif).pack(side="left", padx=2)

        # timeline scrubber
        self.scrubber = ctk.CTkSlider(left_col, from_=0, to=100,
                                       command=self._on_scrub)
        self.scrubber.set(0)
        self.scrubber.pack(fill="x", pady=(4, 2))
        self._scrub_dragging = False
        self.scrubber.bind("<Button-1>", lambda e: setattr(self, "_scrub_dragging", True))
        self.scrubber.bind("<ButtonRelease-1>", self._on_scrub_release)

        # 時間軸縮圖(載入影片後抽 ~20 張顯示)
        import tkinter as _tk
        self.timeline_canvas = _tk.Canvas(left_col, height=72, bg="#0f1115",
                                           highlightthickness=1,
                                           highlightbackground="#262b33")
        self.timeline_canvas.pack(fill="x", pady=(2, 0))
        self.timeline_canvas.bind("<Button-1>", self._on_timeline_click)
        self._thumb_photo = None
        self._thumb_indicator_id = None

        # 文字疊加 track(青色)
        self.overlay_track_canvas = _tk.Canvas(
            left_col, height=24, bg="#0f1115",
            highlightthickness=1, highlightbackground="#155e75",
            cursor="hand2",
        )
        self.overlay_track_canvas.pack(fill="x", pady=(1, 0))
        self.overlay_track_canvas.bind(
            "<Button-1>", lambda e: self._track_click(e, "overlay"))
        self.overlay_track_canvas.bind(
            "<B1-Motion>", lambda e: self._track_drag(e, "overlay"))
        self.overlay_track_canvas.bind(
            "<ButtonRelease-1>", lambda e: self._track_release(e, "overlay"))
        self.overlay_track_canvas.bind(
            "<Motion>", lambda e: self._track_motion(e, "overlay"))

        # 音訊 clip track(粉色)
        self.audio_track_canvas = _tk.Canvas(
            left_col, height=24, bg="#0f1115",
            highlightthickness=1, highlightbackground="#9d174d",
            cursor="hand2",
        )
        self.audio_track_canvas.pack(fill="x", pady=(1, 0))
        self.audio_track_canvas.bind(
            "<Button-1>", lambda e: self._track_click(e, "audio"))
        self.audio_track_canvas.bind(
            "<B1-Motion>", lambda e: self._track_drag(e, "audio"))
        self.audio_track_canvas.bind(
            "<ButtonRelease-1>", lambda e: self._track_release(e, "audio"))
        self.audio_track_canvas.bind(
            "<Motion>", lambda e: self._track_motion(e, "audio"))

        # 圖片疊加 track(橘色)
        self.image_track_canvas = _tk.Canvas(
            left_col, height=24, bg="#0f1115",
            highlightthickness=1, highlightbackground="#9a3412",
            cursor="hand2",
        )
        self.image_track_canvas.pack(fill="x", pady=(1, 0))
        self.image_track_canvas.bind(
            "<Button-1>", lambda e: self._track_click(e, "image"))
        self.image_track_canvas.bind(
            "<B1-Motion>", lambda e: self._track_drag(e, "image"))
        self.image_track_canvas.bind(
            "<ButtonRelease-1>", lambda e: self._track_release(e, "image"))
        self.image_track_canvas.bind(
            "<Motion>", lambda e: self._track_motion(e, "image"))

        self._track_drag_state = None
        self._setup_shortcuts()

        # 視窗 resize → 重畫縮圖 + tracks(用戶要求:寬度跟著影片區同步)
        self.timeline_canvas.bind("<Configure>", lambda e: self._on_timeline_resize())
        self.overlay_track_canvas.bind("<Configure>", lambda e: self._render_overlay_track())
        self.audio_track_canvas.bind("<Configure>", lambda e: self._render_audio_track())
        self.image_track_canvas.bind("<Configure>", lambda e: self._render_image_track())
        self._resize_debounce_id = None

        # 剪輯片段卡片
        cut_card = ctk.CTkFrame(edit_col, fg_color=("#fef3c7", "#1a1208"),
                                border_color=("#fcd34d", "#92400e"), border_width=1,
                                corner_radius=10)
        cut_card.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(cut_card, text="✂ 剪輯片段",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=("#92400e", "#fcd34d")).pack(anchor="w", padx=12, pady=(8, 2))

        cut_btn_row = ctk.CTkFrame(cut_card, fg_color="transparent")
        cut_btn_row.pack(fill="x", padx=10, pady=4)
        ctk.CTkButton(cut_btn_row, text="標起點", width=80,
                      command=self._mark_cut_in).pack(side="left", padx=2)
        ctk.CTkButton(cut_btn_row, text="標終點", width=80,
                      command=self._mark_cut_out).pack(side="left", padx=2)
        ctk.CTkButton(cut_btn_row, text="加入剪輯",
                      fg_color="#dc2626", hover_color="#b91c1c", width=90,
                      command=self._add_cut).pack(side="left", padx=2)
        ctk.CTkButton(cut_btn_row, text="清空", width=60,
                      command=self._clear_cuts).pack(side="left", padx=2)
        self.cut_mark_lbl = ctk.CTkLabel(cut_btn_row, text="起 — / 終 —",
                                          font=ctk.CTkFont(family="Consolas", size=10),
                                          text_color=("#6b7280", "#9ca3af"))
        self.cut_mark_lbl.pack(side="left", padx=10)

        self.cuts_scroll = ctk.CTkScrollableFrame(cut_card, fg_color="transparent",
                                                   height=80)
        self.cuts_scroll.pack(fill="x", padx=8, pady=(0, 4))

        # 套用剪輯預覽 + 還原原始 + 靜音偵測
        preview_row = ctk.CTkFrame(cut_card, fg_color="transparent")
        preview_row.pack(fill="x", padx=10, pady=(0, 4))
        self.cut_apply_btn = ctk.CTkButton(
            preview_row, text="📺 套用剪輯預覽", width=130,
            fg_color="#dc2626", hover_color="#b91c1c",
            font=ctk.CTkFont(weight="bold"),
            command=self._apply_cuts_preview,
        )
        self.cut_apply_btn.pack(side="left", padx=2)
        ctk.CTkButton(
            preview_row, text="🤖 偵測靜音", width=100,
            fg_color="#7c3aed", hover_color="#6d28d9",
            command=self._detect_silence,
        ).pack(side="left", padx=2)
        self.cut_restore_btn = ctk.CTkButton(
            preview_row, text="↺ 還原原始", width=100,
            fg_color="#6b7280", hover_color="#4b5563",
            command=self._restore_from_cut_preview,
        )
        # 預設不顯示 — 進入 preview mode 才 pack
        self.cut_preview_status_lbl = ctk.CTkLabel(
            preview_row, text="",
            font=ctk.CTkFont(family="Consolas", size=10),
            text_color=("#dc2626", "#fca5a5"),
        )
        self.cut_preview_status_lbl.pack(side="left", padx=8)

        # 框選裁切區域 + 長寬比預設
        crop_row = ctk.CTkFrame(cut_card, fg_color="transparent")
        crop_row.pack(fill="x", padx=10, pady=(0, 2))
        ctk.CTkButton(
            crop_row, text="🔲 框選裁切", width=100,
            fg_color="#7c3aed", hover_color="#6d28d9",
            command=self._open_crop_dialog,
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            crop_row, text="9:16 直", width=70,
            fg_color="#0891b2", hover_color="#0e7490",
            command=lambda: self._apply_aspect_preset(9, 16),
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            crop_row, text="1:1 方", width=70,
            fg_color="#0891b2", hover_color="#0e7490",
            command=lambda: self._apply_aspect_preset(1, 1),
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            crop_row, text="16:9 橫", width=70,
            fg_color="#0891b2", hover_color="#0e7490",
            command=lambda: self._apply_aspect_preset(16, 9),
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            crop_row, text="清空", width=60,
            command=self._clear_crop,
        ).pack(side="left", padx=2)
        self.crop_status_lbl = ctk.CTkLabel(
            cut_card, text="裁切:無(輸出時保留完整畫面)",
            font=ctk.CTkFont(family="Consolas", size=10),
            text_color=("#6b7280", "#9ca3af"),
        )
        self.crop_status_lbl.pack(anchor="w", padx=14, pady=(0, 8))

        # 文字疊加卡
        txt_card = ctk.CTkFrame(edit_col, fg_color=("#ecfeff", "#06121a"),
                                 border_color=("#67e8f9", "#155e75"), border_width=1,
                                 corner_radius=10)
        txt_card.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(txt_card, text="✨ 文字疊加(浮水印 / 標題)",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=("#155e75", "#67e8f9")).pack(anchor="w", padx=12, pady=(8, 4))

        # 第一列:文字輸入
        txt_row1 = ctk.CTkFrame(txt_card, fg_color="transparent")
        txt_row1.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(txt_row1, text="文字", width=40, anchor="w").pack(side="left")
        self.overlay_text_var = ctk.StringVar(value="")
        ctk.CTkEntry(txt_row1, textvariable=self.overlay_text_var,
                     placeholder_text="輸入要疊在影片上的文字...").pack(
                     side="left", fill="x", expand=True, padx=4)

        # 第二列:字型 + 字級
        txt_row2 = ctk.CTkFrame(txt_card, fg_color="transparent")
        txt_row2.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(txt_row2, text="字型", width=40, anchor="w").pack(side="left")
        self.overlay_font_var = ctk.StringVar(value=self._first_available_font())
        ctk.CTkOptionMenu(txt_row2, variable=self.overlay_font_var,
                          values=list(TEXT_FONTS.keys()),
                          width=150).pack(side="left", padx=4)
        ctk.CTkLabel(txt_row2, text="字級", width=40, anchor="e").pack(side="left", padx=(8, 0))
        self.overlay_size_var = ctk.StringVar(value="中 (36)")
        ctk.CTkOptionMenu(txt_row2, variable=self.overlay_size_var,
                          values=list(TEXT_SIZES.keys()),
                          width=90).pack(side="left", padx=4)

        # 第三列:顏色 + 位置 + 描邊
        txt_row3 = ctk.CTkFrame(txt_card, fg_color="transparent")
        txt_row3.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(txt_row3, text="顏色", width=40, anchor="w").pack(side="left")
        self.overlay_color_var = ctk.StringVar(value="白色")
        ctk.CTkOptionMenu(txt_row3, variable=self.overlay_color_var,
                          values=list(TEXT_COLORS.keys()),
                          width=80).pack(side="left", padx=4)
        ctk.CTkLabel(txt_row3, text="位置", width=40, anchor="e").pack(side="left", padx=(8, 0))
        self.overlay_pos_var = ctk.StringVar(value="底部中央")
        ctk.CTkOptionMenu(txt_row3, variable=self.overlay_pos_var,
                          values=list(TEXT_POSITIONS.keys()),
                          width=110).pack(side="left", padx=4)
        ctk.CTkLabel(txt_row3, text="描邊", width=40, anchor="e").pack(side="left", padx=(8, 0))
        self.overlay_border_var = ctk.StringVar(value="黑邊")
        ctk.CTkOptionMenu(txt_row3, variable=self.overlay_border_var,
                          values=list(TEXT_BORDERS.keys()),
                          width=70).pack(side="left", padx=4)

        # 第四列:時間(起 / 結)+ 加入按鈕
        txt_row4 = ctk.CTkFrame(txt_card, fg_color="transparent")
        txt_row4.pack(fill="x", padx=10, pady=(2, 6))
        ctk.CTkLabel(txt_row4, text="時間", width=40, anchor="w").pack(side="left")
        self.overlay_start_var = ctk.StringVar(value="")
        ctk.CTkEntry(txt_row4, textvariable=self.overlay_start_var,
                     placeholder_text="起 mm:ss",
                     width=80).pack(side="left", padx=2)
        ctk.CTkLabel(txt_row4, text="~").pack(side="left", padx=2)
        self.overlay_end_var = ctk.StringVar(value="")
        ctk.CTkEntry(txt_row4, textvariable=self.overlay_end_var,
                     placeholder_text="結 mm:ss",
                     width=80).pack(side="left", padx=2)
        ctk.CTkLabel(
            txt_row4, text="(空白=全片)",
            font=ctk.CTkFont(size=10),
            text_color=("#9ca3af", "#6e7681"),
        ).pack(side="left", padx=2)

        ctk.CTkButton(txt_row4, text="+ 加入", width=70,
                      fg_color="#0891b2", hover_color="#0e7490",
                      command=self._add_text_overlay).pack(side="right", padx=2)
        ctk.CTkButton(txt_row4, text="清空", width=60,
                      command=self._clear_text_overlays).pack(side="right", padx=2)

        # 文字清單
        self.overlays_scroll = ctk.CTkScrollableFrame(txt_card, fg_color="transparent",
                                                       height=80)
        self.overlays_scroll.pack(fill="x", padx=8, pady=(0, 8))
        self._render_text_overlay_list()

        # 圖片疊加卡
        img_card = ctk.CTkFrame(edit_col, fg_color=("#fff7ed", "#1c0d04"),
                                 border_color=("#fdba74", "#9a3412"), border_width=1,
                                 corner_radius=10)
        img_card.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(img_card, text="🖼 圖片疊加(Logo / 浮水印)",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=("#9a3412", "#fdba74")).pack(anchor="w", padx=12, pady=(8, 4))

        img_row1 = ctk.CTkFrame(img_card, fg_color="transparent")
        img_row1.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(img_row1, text="圖片", width=40, anchor="w").pack(side="left")
        self.image_path_entry = ctk.CTkEntry(
            img_row1, placeholder_text="選 png / jpg(透明 png 最佳)")
        self.image_path_entry.pack(side="left", fill="x", expand=True, padx=4)
        ctk.CTkButton(img_row1, text="選", width=40,
                      command=self._pick_image_overlay).pack(side="left", padx=2)

        img_row2 = ctk.CTkFrame(img_card, fg_color="transparent")
        img_row2.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(img_row2, text="位置", width=40, anchor="w").pack(side="left")
        self.image_pos_var = ctk.StringVar(value="底部右側")
        ctk.CTkOptionMenu(img_row2, variable=self.image_pos_var,
                          values=list(TEXT_POSITIONS.keys()),
                          width=120).pack(side="left", padx=4)
        ctk.CTkLabel(img_row2, text="大小", width=40, anchor="e").pack(side="left", padx=(8, 0))
        self.image_scale_var = ctk.DoubleVar(value=0.2)
        ctk.CTkSlider(img_row2, from_=0.05, to=1.0, number_of_steps=19,
                      variable=self.image_scale_var, width=100,
                      command=lambda v: self.image_scale_lbl.configure(
                          text=f"{int(self.image_scale_var.get()*100)}%")).pack(side="left", padx=2)
        self.image_scale_lbl = ctk.CTkLabel(img_row2, text="20%", width=40,
                                             font=ctk.CTkFont(family="Consolas", size=10))
        self.image_scale_lbl.pack(side="left")

        img_row3 = ctk.CTkFrame(img_card, fg_color="transparent")
        img_row3.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(img_row3, text="透明", width=40, anchor="w").pack(side="left")
        self.image_opacity_var = ctk.DoubleVar(value=1.0)
        ctk.CTkSlider(img_row3, from_=0.1, to=1.0, number_of_steps=18,
                      variable=self.image_opacity_var, width=140,
                      command=lambda v: self.image_opacity_lbl.configure(
                          text=f"{int(self.image_opacity_var.get()*100)}%")).pack(side="left", padx=4)
        self.image_opacity_lbl = ctk.CTkLabel(img_row3, text="100%", width=40,
                                               font=ctk.CTkFont(family="Consolas", size=10))
        self.image_opacity_lbl.pack(side="left")

        img_row4 = ctk.CTkFrame(img_card, fg_color="transparent")
        img_row4.pack(fill="x", padx=10, pady=(2, 4))
        ctk.CTkLabel(img_row4, text="時間", width=40, anchor="w").pack(side="left")
        self.image_start_var = ctk.StringVar(value="")
        ctk.CTkEntry(img_row4, textvariable=self.image_start_var,
                     placeholder_text="起 mm:ss", width=80).pack(side="left", padx=2)
        ctk.CTkLabel(img_row4, text="~").pack(side="left", padx=2)
        self.image_end_var = ctk.StringVar(value="")
        ctk.CTkEntry(img_row4, textvariable=self.image_end_var,
                     placeholder_text="結 mm:ss", width=80).pack(side="left", padx=2)
        ctk.CTkLabel(img_row4, text="(空=全片)",
                     font=ctk.CTkFont(size=10),
                     text_color=("#9ca3af", "#6e7681")).pack(side="left", padx=2)
        ctk.CTkButton(img_row4, text="+ 加入", width=70,
                      fg_color="#ea580c", hover_color="#c2410c",
                      command=self._add_image_overlay).pack(side="right", padx=2)
        ctk.CTkButton(img_row4, text="清空", width=60,
                      command=self._clear_image_overlays).pack(side="right", padx=2)

        self.image_overlays_scroll = ctk.CTkScrollableFrame(img_card, fg_color="transparent",
                                                             height=60)
        self.image_overlays_scroll.pack(fill="x", padx=8, pady=(0, 8))
        self._render_image_overlay_list()

        # 音訊卡
        au_card = ctk.CTkFrame(edit_col, fg_color=("#fdf2f8", "#1a0a18"),
                                border_color=("#f9a8d4", "#9d174d"), border_width=1,
                                corner_radius=10)
        au_card.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(au_card, text="🎵 音訊控制",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=("#9d174d", "#f9a8d4")).pack(anchor="w", padx=12, pady=(8, 4))

        # 原音音量
        au_row1 = ctk.CTkFrame(au_card, fg_color="transparent")
        au_row1.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(au_row1, text="原音音量", width=70, anchor="w").pack(side="left")
        self.audio_vol_var = ctk.DoubleVar(value=1.0)
        self.audio_vol_slider = ctk.CTkSlider(
            au_row1, from_=0, to=2.0, number_of_steps=40,
            variable=self.audio_vol_var,
            command=lambda v: self._update_vol_lbl(),
        )
        self.audio_vol_slider.pack(side="left", fill="x", expand=True, padx=4)
        self.audio_vol_lbl = ctk.CTkLabel(au_row1, text="100%", width=50,
                                           font=ctk.CTkFont(family="Consolas", size=10))
        self.audio_vol_lbl.pack(side="left", padx=4)
        self.mute_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(au_row1, text="靜音",
                        variable=self.mute_var,
                        command=self._update_vol_lbl).pack(side="left", padx=8)

        # 背景音 — 像文字疊加一樣多筆 clip(各自起終時間 + 音量)
        bg_row = ctk.CTkFrame(au_card, fg_color="transparent")
        bg_row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(bg_row, text="背景音", width=70, anchor="w").pack(side="left")
        self.bg_music_entry = ctk.CTkEntry(bg_row, placeholder_text="選 mp3 / wav...")
        self.bg_music_entry.pack(side="left", fill="x", expand=True, padx=4)
        ctk.CTkButton(bg_row, text="選", width=40,
                      command=self._pick_bg_music).pack(side="left", padx=2)

        # 時間 + 音量 + 加入
        bg_time_row = ctk.CTkFrame(au_card, fg_color="transparent")
        bg_time_row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(bg_time_row, text="時間", width=70, anchor="w").pack(side="left")
        self.bg_start_var = ctk.StringVar(value="")
        ctk.CTkEntry(bg_time_row, textvariable=self.bg_start_var,
                     placeholder_text="起 mm:ss", width=80).pack(side="left", padx=2)
        ctk.CTkLabel(bg_time_row, text="~").pack(side="left", padx=2)
        self.bg_end_var = ctk.StringVar(value="")
        ctk.CTkEntry(bg_time_row, textvariable=self.bg_end_var,
                     placeholder_text="結 mm:ss", width=80).pack(side="left", padx=2)
        ctk.CTkLabel(bg_time_row, text="(空白=全片)",
                     font=ctk.CTkFont(size=10),
                     text_color=("#9ca3af", "#6e7681")).pack(side="left", padx=2)

        bg_vol_row = ctk.CTkFrame(au_card, fg_color="transparent")
        bg_vol_row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(bg_vol_row, text="音量", width=70, anchor="w").pack(side="left")
        self.bg_vol_var = ctk.DoubleVar(value=0.3)
        self.bg_vol_slider = ctk.CTkSlider(
            bg_vol_row, from_=0, to=1.5, number_of_steps=30,
            variable=self.bg_vol_var,
            command=lambda v: self._update_vol_lbl(),
        )
        self.bg_vol_slider.pack(side="left", fill="x", expand=True, padx=4)
        self.bg_vol_lbl = ctk.CTkLabel(bg_vol_row, text="30%", width=50,
                                        font=ctk.CTkFont(family="Consolas", size=10))
        self.bg_vol_lbl.pack(side="left", padx=4)
        ctk.CTkButton(bg_vol_row, text="+ 加入", width=70,
                      fg_color="#db2777", hover_color="#be185d",
                      command=self._add_bg_clip).pack(side="left", padx=2)
        ctk.CTkButton(bg_vol_row, text="清空", width=60,
                      command=self._clear_bg_clips).pack(side="left", padx=2)

        # bg clip 清單
        self.bg_clips_scroll = ctk.CTkScrollableFrame(au_card, fg_color="transparent",
                                                       height=60)
        self.bg_clips_scroll.pack(fill="x", padx=8, pady=(0, 8))
        self._render_bg_clips_list()

        # 輸出卡(放在 edit_col 最底)
        out_card = ctk.CTkFrame(edit_col, fg_color=("#f0fdf4", "#0d1f17"),
                                 border_color=("#86efac", "#14532d"), border_width=1,
                                 corner_radius=10)
        out_card.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(out_card, text="🚀 輸出",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=("#14532d", "#86efac")).pack(anchor="w", padx=12, pady=(8, 4))

        fmt_row = ctk.CTkFrame(out_card, fg_color="transparent")
        fmt_row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(fmt_row, text="格式", width=40, anchor="w").pack(side="left")
        self.out_format = ctk.StringVar(value=list(OUTPUT_FORMATS.keys())[0])
        ctk.CTkOptionMenu(fmt_row, variable=self.out_format,
                          values=list(OUTPUT_FORMATS.keys()),
                          width=220).pack(side="left", padx=4)

        opt_row1 = ctk.CTkFrame(out_card, fg_color="transparent")
        opt_row1.pack(fill="x", padx=10, pady=(4, 0))
        self.apply_speed_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(opt_row1, text="套用速度",
                        variable=self.apply_speed_var).pack(side="left", padx=2)
        self.export_srt_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(opt_row1, text="輸出獨立 SRT",
                        variable=self.export_srt_var).pack(side="left", padx=10)

        # 淡入淡出
        fade_row = ctk.CTkFrame(out_card, fg_color="transparent")
        fade_row.pack(fill="x", padx=10, pady=(2, 0))
        self.fade_in_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(fade_row, text="淡入(0~1.5s)",
                        variable=self.fade_in_var).pack(side="left", padx=2)
        self.fade_out_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(fade_row, text="淡出(尾段 1.5s)",
                        variable=self.fade_out_var).pack(side="left", padx=10)

        opt_row2 = ctk.CTkFrame(out_card, fg_color="transparent")
        opt_row2.pack(fill="x", padx=10, pady=(0, 0))
        self.burn_srt_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(opt_row2, text="字幕燒入畫面",
                        variable=self.burn_srt_var).pack(side="left", padx=2)

        opt_row3 = ctk.CTkFrame(out_card, fg_color="transparent")
        opt_row3.pack(fill="x", padx=10, pady=(0, 0))
        ctk.CTkLabel(opt_row3, text="字幕樣式", width=70,
                     anchor="w").pack(side="left")
        self.burn_style_var = ctk.StringVar(value="白字黑邊(預設)")
        ctk.CTkOptionMenu(opt_row3, variable=self.burn_style_var,
                          values=list(SUBTITLE_STYLES.keys()),
                          width=140).pack(side="left", padx=4)
        ctk.CTkLabel(opt_row3, text="大小", width=40, anchor="e").pack(side="left", padx=(4, 0))
        self.burn_size_var = ctk.StringVar(value="小")
        ctk.CTkOptionMenu(opt_row3, variable=self.burn_size_var,
                          values=list(SUBTITLE_SIZES.keys()),
                          width=60).pack(side="left", padx=4)

        opt_row4 = ctk.CTkFrame(out_card, fg_color="transparent")
        opt_row4.pack(fill="x", padx=10, pady=(2, 0))
        ctk.CTkLabel(opt_row4, text="第二字幕", width=70,
                     anchor="w").pack(side="left")
        self.secondary_srt_entry = ctk.CTkEntry(
            opt_row4, placeholder_text="(可選)雙語下方小字 — 通常選英文翻譯")
        self.secondary_srt_entry.pack(side="left", fill="x", expand=True, padx=4)
        ctk.CTkButton(opt_row4, text="選", width=40,
                      command=self._pick_secondary_srt).pack(side="left", padx=2)
        ctk.CTkButton(opt_row4, text="×", width=30,
                      fg_color="#6b7280", hover_color="#4b5563",
                      command=self._clear_secondary_srt).pack(side="left", padx=2)

        opt_row4b = ctk.CTkFrame(out_card, fg_color="transparent")
        opt_row4b.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkLabel(opt_row4b, text="二字大小", width=70,
                     anchor="w").pack(side="left")
        self.burn_size_secondary_var = ctk.StringVar(value="小")
        ctk.CTkOptionMenu(opt_row4b, variable=self.burn_size_secondary_var,
                          values=list(SUBTITLE_SECONDARY_SCALES.keys()),
                          width=60).pack(side="left", padx=4)
        ctk.CTkLabel(
            opt_row4b, text="(相對主字幕 — 小=½, 大=¾)",
            font=ctk.CTkFont(size=10),
            text_color=("#6b7280", "#9ca3af"),
        ).pack(side="left", padx=6)

        self.export_btn = ctk.CTkButton(
            out_card, text="開始輸出 ▸",
            fg_color="#16a34a", hover_color="#15803d",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=40,
            command=self._on_export, state="disabled",
        )
        self.export_btn.pack(fill="x", padx=10, pady=(4, 10))

        # ─── SRT 同步面板 widgets(parent = 已 pack 的 srt_col)───
        srt_hdr = ctk.CTkFrame(srt_col, fg_color="transparent")
        srt_hdr.pack(fill="x", padx=12, pady=(10, 0))
        ctk.CTkLabel(srt_hdr, text="📜 SRT 字幕(同步 · 雙擊編輯)",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        self.srt_save_btn = ctk.CTkButton(
            srt_hdr, text="💾 存 SRT", width=80, height=24,
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color="#16a34a", hover_color="#15803d",
            command=self._save_srt_inplace, state="disabled",
        )
        self.srt_save_btn.pack(side="right", padx=2)
        ctk.CTkButton(
            srt_hdr, text="🌐 翻譯", width=70, height=24,
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color="#0891b2", hover_color="#0e7490",
            command=self._translate_srt,
        ).pack(side="right", padx=2)
        self.srt_status_lbl = ctk.CTkLabel(
            srt_col, text="(尚未載入 SRT)",
            font=ctk.CTkFont(size=10),
            text_color=("#6b7280", "#8b949e"),
        )
        self.srt_status_lbl.pack(anchor="w", padx=12)

        self.srt_scroll = ctk.CTkScrollableFrame(srt_col, fg_color="transparent")
        self.srt_scroll.pack(fill="both", expand=True, padx=8, pady=8)

        # 底部 status bar
        self.status_lbl = ctk.CTkLabel(
            self, text="準備好。選一個影片開始。",
            font=ctk.CTkFont(size=10),
            text_color=("#6b7280", "#8b949e"), anchor="w",
        )
        self.status_lbl.pack(fill="x", padx=14, pady=(0, 8))

    # ----------------- 檔案選擇 -----------------

    def _pick_media(self):
        p = filedialog.askopenfilename(
            title="選影片",
            filetypes=[("影片", " ".join("*" + e for e in VIDEO_EXTS)),
                       ("音檔", " ".join("*" + e for e in AUDIO_EXTS)),
                       ("All", "*.*")],
        )
        if not p:
            return
        self._load_media(Path(p))

    def _pick_srt(self):
        p = filedialog.askopenfilename(
            title="選 SRT",
            filetypes=[("SRT", "*.srt"), ("All", "*.*")],
        )
        if not p:
            return
        self._load_srt(Path(p))

    def _reset_edit_state(self):
        """載入新影片前,清空上一支影片的所有編輯紀錄,回到乾淨狀態。

        包含:剪輯片段、裁切、文字 / 圖片疊加、背景音、變速、還原 / 重做堆疊,
        以及剪輯預覽模式(隱藏「還原原始」按鈕)。
        """
        # 1) 若還停在剪輯預覽模式 → 直接退出預覽 UI(不重載舊片)
        if getattr(self, "cut_preview_active", False):
            self.cut_preview_active = False
            try:
                self.cut_restore_btn.pack_forget()
                self.cut_apply_btn.pack(side="left", padx=2)
                self.cut_apply_btn.configure(state="normal", text="📺 套用剪輯預覽")
                self.cut_preview_status_lbl.configure(text="")
            except Exception:
                pass
        self.original_media_path = None
        self.original_srt_blocks = []

        # 2) 清空各類編輯紀錄(沿用既有 clear 函式,會一併刷新 UI)
        self._clear_cuts()
        self._clear_crop()
        self._clear_text_overlays()
        self._clear_image_overlays()
        self._clear_bg_clips()

        # 3) 變速回 1.0x
        try:
            self.speed_var.set("1.0x")
        except Exception:
            pass

        # 4) 還原 / 重做 堆疊清空
        self._undo_stack = []
        self._redo_stack = []

        # 5) 其它一次性狀態
        self._srt_dirty = False
        self.secondary_srt_path = None

    def _load_media(self, path: Path):
        # 換片 → 先清空上一支影片的所有編輯紀錄(剪輯 / 裁切 / 疊加 / 音訊 /
        # 變速 / 剪輯預覽「還原原始」按鈕),避免狀態錯亂。
        self._reset_edit_state()
        self.media_path = path
        self.media_entry.delete(0, "end")
        self.media_entry.insert(0, str(path))
        self.status_lbl.configure(text=f"載入影片 {path.name}...")
        self.update_idletasks()

        # 初始化或重用 VLC player
        try:
            if self.player is None:
                # placeholder destroy(不只是 forget,徹底拿掉避免蓋住 VLC 輸出)
                try:
                    self.video_placeholder.destroy()
                    self.video_placeholder = None
                except Exception:
                    pass
                # 等 frame 真正畫出來才取 winfo_id(否則拿到無效 hwnd)
                self.video_host.update()
                self.player = VLCPlayer(self.video_host)
            self.player.load(path)
            try:
                self.player.set_rate(1.0)  # 換片 → 播放速度回 1.0x
            except Exception:
                pass
            # crop 已於 _reset_edit_state 清空,這裡套用即還原成無裁切
            self._apply_crop_preview()
            self._apply_srt_subtitle_preview()
            self._build_timeline_thumbnails()
        except Exception as e:
            messagebox.showerror("影片載入失敗",
                                  f"無法載入 {path.name}\n\n{e}\n\n"
                                  "請確認已安裝 VLC media player:\n"
                                  "https://www.videolan.org/vlc/")
            self.status_lbl.configure(text="❌ VLC 載入失敗")
            return

        # 設 scrubber 範圍
        self.scrubber.configure(from_=0, to=max(1.0, self.player.duration))
        self.scrubber.set(0)

        self.play_btn.configure(state="normal", text="▶ 播放")
        self.export_btn.configure(state="normal")
        self.status_lbl.configure(text=f"✓ 載入 {path.name}({int(self.player.duration//60)}:{int(self.player.duration%60):02d})")

        # 同目錄找同名 SRT 自動載入
        candidates = [
            path.with_suffix(".srt"),
            path.parent / "output" / "transcribed" / f"{path.stem}.transcribed.srt",
            path.parent / "output" / "corrected" / f"{path.stem}.corrected.srt",
        ]
        for c in candidates:
            if c.exists():
                self._load_srt(c)
                break

    def _load_srt(self, path: Path):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        parsed = parse_srt(text)
        if not parsed:
            messagebox.showwarning("SRT 解析失敗", "找不到合法的 SRT block")
            return
        self.srt_path = path
        self.srt_blocks = parsed
        self.srt_entry.delete(0, "end")
        self.srt_entry.insert(0, str(path))
        self.srt_status_lbl.configure(text=f"{len(parsed)} blocks · {path.name}")
        self._populate_srt_panel()
        # 把這個 SRT 顯示在 VLC 影片畫面上(real-time preview,不影響輸出)
        self._apply_srt_subtitle_preview()

    def _apply_srt_subtitle_preview(self):
        """把 self.srt_path 設給 VLC 當 subtitle track,並立即顯示。"""
        if self.player is None or self.player.player is None:
            return
        if not self.srt_path or not Path(self.srt_path).exists():
            return
        self._attach_subtitle_to_player(Path(self.srt_path))

    def _write_srt_temp_and_reload(self):
        """SRT 改過(編輯 / 剪輯預覽 remap)後,寫到 temp file 重新給 VLC 預覽。"""
        import tempfile
        tmp = Path(tempfile.gettempdir()) / "srt_v2_live.srt"
        try:
            tmp.write_text(serialize_srt(self.srt_blocks), encoding="utf-8")
        except Exception:
            return
        self._attach_subtitle_to_player(tmp)

    def _attach_subtitle_to_player(self, srt_file: Path):
        """用 add_slave(modern API)+ select=True,VLC 即時切換到此字幕。"""
        if self.player is None or self.player.player is None:
            return
        p = self.player.player
        try:
            import vlc
            uri = srt_file.as_uri()  # 'file:///C:/...'
            slave_type = getattr(vlc.MediaSlaveType, "subtitle", 0)
            # select=True 表示加入後直接選用 → 立即顯示
            p.add_slave(slave_type, uri, True)
        except Exception:
            # fallback 舊 API
            try:
                p.video_set_subtitle_file(str(srt_file))
            except Exception:
                pass
        # 不管哪個路徑,延遲 600ms 確認 SPU 有被啟用(避免 VLC 還在 parse media 時設不上)
        self.after(600, self._ensure_subtitle_enabled)

    def _ensure_subtitle_enabled(self):
        if self.player is None or self.player.player is None:
            return
        p = self.player.player
        try:
            count = p.video_get_spu_count()
            current = p.video_get_spu()
            # 沒選任何 sub 但有可用的 → 強制啟第一個
            if count > 0 and current < 0:
                # 取得所有 SPU descriptions 找第一個非 disabled
                try:
                    descs = p.video_get_spu_description()
                    for d in descs:
                        sid = d[0] if isinstance(d, tuple) else getattr(d, "id", -1)
                        if sid > 0:  # 跳過 disable=-1 / system=0
                            p.video_set_spu(sid)
                            return
                except Exception:
                    pass
                p.video_set_spu(0)
        except Exception:
            pass

    def _populate_srt_panel(self):
        # 清空
        for w in list(self.srt_scroll.winfo_children()):
            try: w.destroy()
            except Exception: pass

        self.srt_block_widgets = []  # 用於高亮 / 即時編輯
        for i, (orig_idx, st, ed, lines) in enumerate(self.srt_blocks):
            row = ctk.CTkFrame(self.srt_scroll, fg_color=("#ffffff", "#161b22"),
                                corner_radius=6, border_width=1,
                                border_color=("#e2e8f0", "#262b33"))
            row.pack(fill="x", padx=2, pady=2)

            header_row = ctk.CTkFrame(row, fg_color="transparent")
            header_row.pack(fill="x", padx=8, pady=(4, 0))
            ts_lbl = ctk.CTkLabel(
                header_row,
                text=f"{sec_to_srt_ts(st)[:-4]} → {sec_to_srt_ts(ed)[:-4]}",
                font=ctk.CTkFont(family="Consolas", size=10),
                text_color=("#6b7280", "#8b949e"),
                anchor="w",
            )
            ts_lbl.pack(side="left")
            edit_btn = ctk.CTkButton(
                header_row, text="✎", width=22, height=18,
                font=ctk.CTkFont(size=10),
                fg_color="#3b82f6", hover_color="#2563eb",
                command=lambda idx=i: self._edit_srt_block(idx),
            )
            edit_btn.pack(side="right")

            text_str = "\n".join(lines)
            text_lbl = ctk.CTkLabel(
                row, text=text_str,
                font=ctk.CTkFont(size=11),
                anchor="w", justify="left", wraplength=280,
            )
            text_lbl.pack(anchor="w", padx=8, pady=(0, 4), fill="x")

            # 點 row → seek;雙擊 → 編輯
            for w in (row, ts_lbl, text_lbl):
                w.bind("<Button-1>", lambda e, t=st: self._seek_to(t))
                w.bind("<Double-Button-1>", lambda e, idx=i: self._edit_srt_block(idx))

            self.srt_block_widgets.append({"frame": row, "ts": ts_lbl, "text": text_lbl,
                                            "start": st, "end": ed})

    def _edit_srt_block(self, idx: int):
        """彈窗即時編輯 SRT block 文字。儲存 → 更新 srt_blocks + 重新 render 該行。"""
        if not (0 <= idx < len(self.srt_blocks)):
            return
        orig_idx, st, ed, lines = self.srt_blocks[idx]
        cur_text = "\n".join(lines)

        dlg = ctk.CTkToplevel(self)
        dlg.title("編輯字幕")
        dlg.geometry("520x300+200+200")
        dlg.attributes("-topmost", True)

        ctk.CTkLabel(
            dlg, text=f"{sec_to_srt_ts(st)[:-4]} → {sec_to_srt_ts(ed)[:-4]}",
            font=ctk.CTkFont(family="Consolas", size=11, weight="bold"),
        ).pack(anchor="w", padx=14, pady=(12, 4))

        box = ctk.CTkTextbox(dlg, height=160, wrap="word",
                              font=ctk.CTkFont(size=13))
        box.pack(fill="both", expand=True, padx=14, pady=4)
        box.insert("1.0", cur_text)
        box.focus_set()

        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(fill="x", padx=14, pady=10)

        def save():
            new_text = box.get("1.0", "end").rstrip("\n")
            new_lines = new_text.split("\n")
            self.srt_blocks[idx] = (orig_idx, st, ed, new_lines)
            # 同步更新 panel 顯示
            w = self.srt_block_widgets[idx]
            try:
                w["text"].configure(text=new_text)
            except Exception:
                pass
            self.status_lbl.configure(text=f"✓ SRT 第 {idx+1} 行已更新(尚未存檔,按「💾 存 SRT」)")
            self._srt_dirty = True
            self._update_srt_save_button()
            # 同步更新 VLC 預覽字幕
            self._write_srt_temp_and_reload()
            dlg.destroy()

        ctk.CTkButton(btn_row, text="取消", width=80,
                      command=dlg.destroy).pack(side="right", padx=4)
        ctk.CTkButton(btn_row, text="✓ 儲存", width=100,
                      fg_color="#16a34a", hover_color="#15803d",
                      font=ctk.CTkFont(weight="bold"),
                      command=save).pack(side="right", padx=4)

    def _update_srt_save_button(self):
        """SRT 改過後顯示「💾 存 SRT」按鈕。"""
        if not hasattr(self, "srt_save_btn"):
            return
        if getattr(self, "_srt_dirty", False):
            self.srt_save_btn.configure(state="normal", text="💾 存 SRT*")
        else:
            self.srt_save_btn.configure(state="disabled", text="💾 存 SRT")

    # ----------------- V1 SRT 字幕工具 launcher -----------------

    def _launch_v1_srt_tool(self):
        """開啟 V1 GUI(轉字幕 / 校正 / Glossary)在獨立視窗。V2 不動。"""
        v1_dir = Path(__file__).parent.parent  # Side_project/
        v1_script = v1_dir / "srt_corrector_gui.py"
        if not v1_script.exists():
            messagebox.showerror(
                "找不到 V1",
                f"找不到 {v1_script}\n\nV1 字幕工具是 Side_project 父資料夾下的 srt_corrector_gui.py。"
            )
            return
        # 優先用 V1 自己的 venv,否則用 V2 的 venv(只要套件相容)
        v1_pythonw = v1_dir / ".venv" / "Scripts" / "pythonw.exe"
        v2_pythonw = Path(__file__).parent / ".venv" / "Scripts" / "pythonw.exe"
        python_exe = v1_pythonw if v1_pythonw.exists() else v2_pythonw
        if not python_exe.exists():
            messagebox.showerror("找不到 Python", f"venv 找不到:\n{v1_pythonw}\n{v2_pythonw}")
            return
        try:
            subprocess.Popen([str(python_exe), str(v1_script)],
                              cwd=str(v1_dir),
                              creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            self.status_lbl.configure(text="✓ V1 SRT 字幕工具已開啟(獨立視窗)")
        except Exception as e:
            messagebox.showerror("啟動失敗", str(e))

    # ----------------- 項目存檔 / 開啟 -----------------

    PROJECT_EXT = ".editproj.json"

    def _save_project(self):
        if not self.media_path:
            messagebox.showwarning("存檔項目", "請先載入影片再存檔")
            return
        p = filedialog.asksaveasfilename(
            title="存檔項目",
            defaultextension=self.PROJECT_EXT,
            filetypes=[("Edit Project", f"*{self.PROJECT_EXT}"), ("All", "*.*")],
            initialfile=self.media_path.stem + self.PROJECT_EXT,
        )
        if not p:
            return
        state = {
            "version": 1,
            "saved_at": datetime.now().isoformat(),
            "media_path": str(self.media_path),
            "srt_path": str(self.srt_path) if self.srt_path else None,
            "cuts": list(self.cuts),
            "crop_region": list(self.crop_region) if self.crop_region else None,
            "text_overlays": [
                {**ov, "font_path": str(ov.get("font_path", ""))}
                for ov in self.text_overlays
            ],
            "speed": float(self.speed_var.get().replace("x", "")) if self.speed_var.get() else 1.0,
            "apply_speed": bool(self.apply_speed_var.get()),
            "export_srt": bool(self.export_srt_var.get()),
            "burn_srt": bool(self.burn_srt_var.get()),
            "audio": {
                "mute": bool(self.mute_var.get()),
                "main_volume": float(self.audio_vol_var.get()),
                "bg_clips": [
                    {**c, "path": str(c["path"])}
                    for c in self.bg_music_clips
                ],
            },
            "out_format": self.out_format.get(),
        }
        import json as _json
        Path(p).write_text(_json.dumps(state, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        self.status_lbl.configure(text=f"✓ 已存項目 → {Path(p).name}")
        messagebox.showinfo("存檔完成", f"項目已存\n\n{p}")

    def _open_project(self):
        p = filedialog.askopenfilename(
            title="開啟項目",
            filetypes=[("Edit Project", f"*{self.PROJECT_EXT}"), ("JSON", "*.json"),
                       ("All", "*.*")],
        )
        if not p:
            return
        try:
            import json as _json
            state = _json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception as e:
            messagebox.showerror("開啟失敗", str(e))
            return
        # 還原 — 順序很重要(media 先,其他靠著它)
        media = state.get("media_path")
        if media and Path(media).exists():
            self._load_media(Path(media))
        srt = state.get("srt_path")
        if srt and Path(srt).exists():
            self._load_srt(Path(srt))
        # cuts
        self.cuts = [tuple(c) for c in state.get("cuts", [])]
        self._render_cut_list()
        # crop
        cr = state.get("crop_region")
        if cr:
            self.crop_region = tuple(cr)
            x, y, w, h, ow, oh = self.crop_region
            self.crop_status_lbl.configure(
                text=f"裁切: {w}×{h} @ ({x}, {y})  原 {ow}×{oh}",
                text_color=("#7c3aed", "#c4b5fd"),
            )
            self._apply_crop_preview()
        # overlays
        self.text_overlays = []
        for ov in state.get("text_overlays", []):
            ov = dict(ov)
            ov["font_path"] = Path(ov["font_path"]) if ov.get("font_path") else None
            self.text_overlays.append(ov)
        self._render_text_overlay_list()
        # speed
        sp = state.get("speed", 1.0)
        self.speed_var.set(f"{sp}x")
        self.apply_speed_var.set(state.get("apply_speed", False))
        self.export_srt_var.set(state.get("export_srt", True))
        self.burn_srt_var.set(state.get("burn_srt", False))
        # audio
        au = state.get("audio", {})
        self.mute_var.set(au.get("mute", False))
        self.audio_vol_var.set(au.get("main_volume", 1.0))
        # 還原 bg clips(新格式)
        self.bg_music_clips = []
        for c in au.get("bg_clips", []):
            p = Path(c.get("path", ""))
            if p.exists():
                self.bg_music_clips.append({
                    "path": p,
                    "start_sec": c.get("start_sec"),
                    "end_sec": c.get("end_sec"),
                    "volume": float(c.get("volume", 0.3)),
                })
        self._render_bg_clips_list()
        self._update_vol_lbl()
        # format
        if state.get("out_format") in OUTPUT_FORMATS:
            self.out_format.set(state["out_format"])
        self.status_lbl.configure(text=f"✓ 已開啟項目 {Path(p).name}")

    # ----------------- AI 翻譯字幕(Gemini)-----------------

    LANGUAGES = {
        "English (英文)":   "English",
        "日本語 (日文)":    "Japanese",
        "한국어 (韓文)":    "Korean",
        "Español (西班牙文)": "Spanish",
        "Français (法文)":  "French",
        "Deutsch (德文)":   "German",
        "Русский (俄文)":   "Russian",
        "العربية (阿拉伯文)": "Arabic",
        "繁體中文":         "Traditional Chinese",
        "简体中文":         "Simplified Chinese",
    }

    def _translate_srt(self):
        if not self.srt_blocks:
            messagebox.showwarning("翻譯", "請先載入 SRT")
            return
        if not os.environ.get("GEMINI_API_KEY"):
            messagebox.showerror("翻譯", "需要 GEMINI_API_KEY 才能翻譯,請在 .env 設定")
            return
        dlg = ctk.CTkToplevel(self)
        dlg.title("翻譯字幕")
        dlg.geometry("420x200+200+200")
        dlg.attributes("-topmost", True)
        ctk.CTkLabel(dlg, text=f"翻譯這個 SRT ({len(self.srt_blocks)} blocks)",
                     font=ctk.CTkFont(weight="bold")).pack(pady=(14, 6))
        row = ctk.CTkFrame(dlg, fg_color="transparent")
        row.pack(pady=4)
        ctk.CTkLabel(row, text="目標語言", width=80).pack(side="left", padx=4)
        target_var = ctk.StringVar(value="English (英文)")
        ctk.CTkOptionMenu(row, variable=target_var,
                          values=list(self.LANGUAGES.keys()),
                          width=200).pack(side="left", padx=4)
        ctk.CTkLabel(dlg,
                     text="會用 Gemini API 翻譯,結果存到 output/translated/ 並可同步載入",
                     font=ctk.CTkFont(size=10),
                     text_color=("#6b7280", "#9ca3af"),
                     wraplength=380).pack(pady=6)
        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(pady=10)
        def run():
            lang_label = target_var.get()
            lang = self.LANGUAGES[lang_label]
            dlg.destroy()
            threading.Thread(target=self._do_translate_srt,
                              args=(lang, lang_label), daemon=True).start()
        ctk.CTkButton(btn_row, text="取消", width=80,
                      command=dlg.destroy).pack(side="left", padx=4)
        ctk.CTkButton(btn_row, text="🌐 開始翻譯", width=120,
                      fg_color="#0891b2", hover_color="#0e7490",
                      font=ctk.CTkFont(weight="bold"),
                      command=run).pack(side="left", padx=4)

    def _do_translate_srt(self, target_lang: str, lang_label: str):
        overlay_ref = {"ov": None}
        self.after(0, lambda: overlay_ref.update(
            ov=ProgressOverlay(self, "AI 翻譯字幕", f"Gemini 翻譯成 {lang_label} 中...")))
        def set_detail(msg):
            self.after(0, lambda m=msg:
                       overlay_ref["ov"].set_detail(m) if overlay_ref["ov"] else None)
        def set_progress(frac):
            self.after(0, lambda f=frac:
                       overlay_ref["ov"].set_progress(f) if overlay_ref["ov"] else None)
        try:
            from google import genai
            from google.genai import types
            import json as _json
            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

            self.after(0, lambda: self.status_lbl.configure(
                text=f"翻譯中:目標 {lang_label}..."))

            # 分 batch — 每 batch 40 個 block
            batch_size = 40
            translated_blocks = []
            n = len(self.srt_blocks)
            for batch_start in range(0, n, batch_size):
                batch_end = min(batch_start + batch_size, n)
                batch = self.srt_blocks[batch_start:batch_end]
                # 組 JSON input
                items = [{"i": i, "text": "\n".join(lines)}
                          for i, (_, _, _, lines) in enumerate(batch, start=batch_start)]
                items_json = _json.dumps(items, ensure_ascii=False, indent=2)
                prompt = f"""把下列字幕逐句翻成 {target_lang}。回傳 JSON array,每筆 {{i, text}} 對應原始 i。
不要加任何前言、解釋、markdown。每個 i 一對一,順序不變。

輸入({batch_end - batch_start} 個):
```json
{items_json}
```"""
                resp = client.models.generate_content(
                    # 跟 V1 SRT 校正預設一致 — flash-lite 系列文字任務夠用、額度高
                    model="gemini-3.1-flash-lite",
                    contents=[prompt],
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        max_output_tokens=8000,
                        response_mime_type="application/json",
                    ),
                )
                # parse
                txt = (resp.text or "").strip()
                if txt.startswith("```"):
                    txt = re.sub(r"^```\w*\n?", "", txt)
                    txt = re.sub(r"\n?```\s*$", "", txt)
                try:
                    arr = _json.loads(txt)
                except _json.JSONDecodeError:
                    arr = []
                # 對應回 block
                idx_map = {entry.get("i"): str(entry.get("text", "")).strip()
                            for entry in arr if isinstance(entry, dict)}
                for i, (orig_idx, st, ed, lines) in enumerate(batch, start=batch_start):
                    new_text = idx_map.get(i, "\n".join(lines))
                    translated_blocks.append((orig_idx, st, ed, new_text.split("\n")))
                self.after(0, lambda done=batch_end, total=n:
                            self.status_lbl.configure(
                                text=f"翻譯中: {done}/{total} blocks..."))
                set_detail(f"已翻譯 {batch_end}/{n} blocks(batch={batch_size})")
                set_progress(batch_end / n)

            # 存檔
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_stem = self.srt_path.stem if self.srt_path else "translated"
            out_dir = output_dir(self.srt_path or self.media_path, "translated")
            out_path = out_dir / f"{base_stem}.{target_lang.replace(' ', '_')}.{stamp}.srt"
            out_path.write_text(serialize_srt(translated_blocks), encoding="utf-8")

            self.after(0, lambda p=out_path, n=len(translated_blocks):
                       messagebox.showinfo("翻譯完成",
                                            f"翻譯 {n} 個字幕完成\n\n{p}\n\n"
                                            "可在「選 SRT」載入這個翻譯版"))
            self.after(0, lambda p=out_path: self.status_lbl.configure(
                text=f"✓ 翻譯完成 → output/translated/{p.name}"))
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            err_msg = str(e)
            self.after(0, lambda: messagebox.showerror(
                "翻譯失敗", f"{err_msg}\n\n{tb[-300:]}"))
            self.after(0, lambda: self.status_lbl.configure(text=f"❌ {err_msg}"))
        finally:
            self.after(0, lambda: overlay_ref["ov"].done() if overlay_ref["ov"] else None)

    def _save_srt_inplace(self):
        """儲存編輯後的 SRT 到原路徑(會問是否覆蓋,或另存)。"""
        if not self.srt_path:
            messagebox.showwarning("存檔", "沒有 SRT 來源檔")
            return
        if not getattr(self, "_srt_dirty", False):
            return
        # 另存到 output/corrected/ 加 timestamp
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = output_dir(self.srt_path, "edited") / f"{self.srt_path.stem}.edited.{stamp}.srt"
        text = serialize_srt(self.srt_blocks)
        dst.write_text(text, encoding="utf-8")
        self._srt_dirty = False
        self._update_srt_save_button()
        self.status_lbl.configure(text=f"✓ 已存 SRT → output/edited/{dst.name}")
        # 把目前載入的 SRT 路徑指到新檔(方便輸出時用新版)
        self.srt_path = dst
        self.srt_entry.delete(0, "end")
        self.srt_entry.insert(0, str(dst))

    # ----------------- 播放控制 -----------------

    def _toggle_play(self):
        if self.player is None:
            return
        if self.player.is_playing():
            self.player.pause()
            self.play_btn.configure(text="▶ 播放")
        else:
            # 影片播畢(Ended)後 VLC 不會從 play() 直接重播 → 需先重置
            if self.player.is_ended():
                self.player.replay()
            else:
                self.player.play()
            self.play_btn.configure(text="⏸ 暫停")

    def _seek_rel(self, delta: float):
        if self.player is None:
            return
        cur = self.player.get_time_sec()
        self._seek_to(max(0.0, min(self.player.duration, cur + delta)))

    def _seek_to(self, sec: float):
        if self.player is None:
            return
        self.player.set_time_sec(sec)

    def _on_scrub(self, value):
        # 拖動時不立刻 seek(等放開),只更新顯示
        if self._scrub_dragging:
            self._update_time_display(float(value))

    def _on_scrub_release(self, _event):
        if self.player is None:
            self._scrub_dragging = False
            return
        v = float(self.scrubber.get())
        self.player.set_time_sec(v)
        self._scrub_dragging = False

    def _on_speed_change(self, value: str):
        if self.player is None:
            return
        rate = float(value.replace("x", ""))
        self.player.set_rate(rate)

    def _on_volume_change(self, value):
        if self.player is None:
            return
        self.player.set_volume(int(float(value)))

    # ----------------- 剪輯 -----------------

    def _current_sec(self) -> float:
        return self.player.get_time_sec() if self.player else 0.0

    def _mark_cut_in(self):
        if self.cut_preview_active:
            messagebox.showinfo("剪輯", "預覽模式中無法再標記。先按「↺ 還原原始」。")
            return
        self.cut_in = self._current_sec()
        self._refresh_cut_mark_lbl()

    def _mark_cut_out(self):
        if self.cut_preview_active:
            messagebox.showinfo("剪輯", "預覽模式中無法再標記。先按「↺ 還原原始」。")
            return
        self.cut_out = self._current_sec()
        self._refresh_cut_mark_lbl()

    def _refresh_cut_mark_lbl(self):
        i = sec_to_srt_ts(self.cut_in)[:-4] if self.cut_in is not None else "—"
        o = sec_to_srt_ts(self.cut_out)[:-4] if self.cut_out is not None else "—"
        self.cut_mark_lbl.configure(text=f"起 {i}  /  終 {o}")

    def _add_cut(self):
        if self.cut_in is None or self.cut_out is None:
            messagebox.showwarning("剪輯", "請先標記起點和終點")
            return
        st, ed = sorted([self.cut_in, self.cut_out])
        if ed - st < 0.1:
            messagebox.showwarning("剪輯", "片段太短(< 0.1 秒)")
            return
        # 檢查重疊
        for cs, ce in self.cuts:
            if not (ed <= cs or st >= ce):
                messagebox.showwarning("剪輯", "與已有片段重疊")
                return
        self.cuts.append((st, ed))
        self.cuts.sort()
        self.cut_in = self.cut_out = None
        self._refresh_cut_mark_lbl()
        self._render_cut_list()

    def _clear_cuts(self):
        self.cuts.clear()
        self.cut_in = self.cut_out = None
        self._refresh_cut_mark_lbl()
        self._render_cut_list()

    def _render_cut_list(self):
        for w in list(self.cuts_scroll.winfo_children()):
            try: w.destroy()
            except Exception: pass
        if not self.cuts:
            ctk.CTkLabel(
                self.cuts_scroll, text="(尚無剪輯片段 — 標起終點後加入)",
                font=ctk.CTkFont(size=10),
                text_color=("#9ca3af", "#6e7681"),
            ).pack(pady=8)
            return
        for i, (st, ed) in enumerate(self.cuts):
            row = ctk.CTkFrame(self.cuts_scroll, fg_color=("#fff7ed", "#1c1209"),
                                corner_radius=6)
            row.pack(fill="x", padx=2, pady=1)
            ctk.CTkLabel(
                row,
                text=f"✂ {sec_to_srt_ts(st)[:-4]} → {sec_to_srt_ts(ed)[:-4]}  ({ed-st:.1f}s)",
                font=ctk.CTkFont(family="Consolas", size=10),
                anchor="w",
            ).pack(side="left", padx=8, pady=3)
            ctk.CTkButton(
                row, text="×", width=24, height=22,
                fg_color="#dc2626", hover_color="#b91c1c",
                command=lambda idx=i: self._remove_cut(idx),
            ).pack(side="right", padx=4)

    def _remove_cut(self, idx: int):
        if 0 <= idx < len(self.cuts):
            self.cuts.pop(idx)
            self._render_cut_list()

    # ----------------- 剪輯預覽 -----------------

    def _apply_cuts_preview(self):
        """把當前 cuts 套用後產出暫存 mp4 + 載回 VLC,SRT 同步重新對應。"""
        if self.cut_preview_active:
            messagebox.showinfo("剪輯預覽", "已在預覽模式中。先按「還原原始」再來。")
            return
        if not self.cuts:
            messagebox.showinfo("剪輯預覽", "尚無剪輯片段")
            return
        if not self.media_path or not self.player:
            messagebox.showwarning("剪輯預覽", "請先載入影片")
            return
        cut_count = len(self.cuts)
        if not messagebox.askyesno(
            "套用剪輯預覽",
            f"將套用 {cut_count} 個剪輯片段並重新載入影片預覽。\n\n"
            "處理時間視影片長度而定(通常數秒~數十秒)。\n"
            "預覽模式中無法再加片段,需先「還原原始」。\n\n"
            "繼續?",
        ):
            return
        self.cut_apply_btn.configure(state="disabled", text="處理中...")
        self.status_lbl.configure(text="ffmpeg 套用剪輯中,稍候...")
        threading.Thread(target=self._build_cut_preview, daemon=True).start()

    def _ffmpeg_cut_join(self, in_path, keeps, out_path, preset="medium", crf=18):
        """用 select / aselect 濾鏡一次過保留 keeps 內的多個片段 → out_path。

        為什麼不用 stream-copy 切片 + concat:stream-copy 只能在關鍵影格切,
        切點不準會回退到前一個 keyframe;concat demuxer 在接點對這些帶非零 /
        負時間戳的片段處理不一致,會吃掉內容 —— 這正是「未剪輯片段消失一部分」
        的根因。select 濾鏡逐幀篩選 + setpts 重新編號,frame-accurate,
        且整段只跑一次編碼。

        keeps:[(start_sec, end_sec), ...](原始時間軸)。失敗丟 RuntimeError。
        """
        ffmpeg = get_ffmpeg()
        expr = "+".join(f"between(t,{st:.3f},{ed:.3f})" for st, ed in keeps)
        vf = f"select='{expr}',setpts=N/FRAME_RATE/TB"
        af = f"aselect='{expr}',asetpts=N/SR/TB"

        def _run(with_audio):
            cmd = [ffmpeg, "-y", "-i", str(in_path), "-vf", vf]
            if with_audio:
                cmd += ["-af", af, "-c:a", "aac", "-b:a", "192k"]
            else:
                cmd += ["-an"]
            cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                    str(out_path)]
            return subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")

        r = _run(with_audio=True)
        if r.returncode != 0:
            # 來源可能無音軌 → 退回純影像再試一次
            r2 = _run(with_audio=False)
            if r2.returncode != 0:
                raise RuntimeError(f"剪輯重新編碼失敗: {r.stderr[-300:]}")

    def _build_cut_preview(self):
        overlay_ref = {"ov": None}
        self.after(0, lambda: overlay_ref.update(
            ov=ProgressOverlay(self, "套用剪輯", "切割 + concat 影片中...")))
        def set_detail(msg):
            self.after(0, lambda m=msg:
                       overlay_ref["ov"].set_detail(m) if overlay_ref["ov"] else None)
        try:
            import tempfile, hashlib
            total_dur = self.player.duration
            # 算保留片段
            keeps = []
            cursor = 0.0
            for cs, ce in self.cuts:
                if cs > cursor + 0.05:
                    keeps.append((cursor, cs))
                cursor = max(cursor, ce)
            if cursor < total_dur - 0.05:
                keeps.append((cursor, total_dur))
            if not keeps:
                raise RuntimeError("剪完無剩餘片段")

            sig = hashlib.md5(f"{self.media_path}_{self.cuts}".encode()).hexdigest()[:10]
            tmp_dir = Path(tempfile.gettempdir()) / f"srt_v2_cutpv_{sig}"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            out_path = tmp_dir / "cut_preview.mp4"

            # 用 select 濾鏡一次過保留多個片段(frame-accurate)。取代原本的
            # 「stream-copy 切片 + concat」—— 後者在非關鍵影格切點會掉幀、
            # concat 接點會吃掉未剪輯內容(就是這次回報的 bug)。
            set_detail(f"重新編碼保留片段({len(keeps)} 段,請稍候)...")
            self._ffmpeg_cut_join(self.media_path, keeps, out_path,
                                  preset="ultrafast", crf=26)

            new_dur = sum(ed - st for st, ed in keeps)
            self.after(0, lambda: self._enter_cut_preview_mode(out_path, new_dur))
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            err_msg = str(e)
            self.after(0, lambda: messagebox.showerror(
                "剪輯預覽失敗", f"{err_msg}\n\n{tb[-400:]}"))
            self.after(0, lambda: self.status_lbl.configure(text=f"❌ {err_msg}"))
            self.after(0, lambda: self.cut_apply_btn.configure(
                state="normal", text="📺 套用剪輯預覽"))
        finally:
            self.after(0, lambda: overlay_ref["ov"].done() if overlay_ref["ov"] else None)

    def _enter_cut_preview_mode(self, preview_path: Path, new_dur: float):
        """切換 VLC media 到 cut-applied 暫存檔 + 重 map SRT。"""
        # 保留原始狀態
        self.original_media_path = self.media_path
        self.original_srt_blocks = list(self.srt_blocks)

        # 1) 載入新 media
        try:
            self.player.load(preview_path)
        except Exception as e:
            messagebox.showerror("載入失敗", str(e))
            self.cut_apply_btn.configure(state="normal", text="📺 套用剪輯預覽")
            return
        # scrubber 範圍 → 新 duration
        self.scrubber.configure(from_=0, to=max(1.0, self.player.duration))
        self.scrubber.set(0)

        # 2) SRT 重新對應(只套 cuts,變速 = 1.0)
        if self.srt_blocks:
            self.srt_blocks = self._apply_edits_to_srt(
                self.original_srt_blocks, self.cuts, speed=1.0,
            )
            self._populate_srt_panel()
            self.srt_status_lbl.configure(
                text=f"{len(self.srt_blocks)} blocks · (剪輯後)")
            # 用 remap 後的 SRT 給 VLC
            self._write_srt_temp_and_reload()

        # 3) UI 進預覽模式
        self.cut_preview_active = True
        self.cut_apply_btn.pack_forget()
        self.cut_restore_btn.pack(side="left", padx=2)
        old_min = int(self.original_media_path and self.original_media_path.exists()
                       and self.original_media_path.stat().st_size or 0)  # placeholder
        orig_min = int(self.scrubber.cget("to") // 60) if False else None  # not useful
        self.cut_preview_status_lbl.configure(
            text=f"📺 預覽中:{int(new_dur//60)}:{int(new_dur%60):02d}"
        )
        self._apply_crop_preview()  # 重新套裁切
        self.status_lbl.configure(text=f"✓ 剪輯預覽已套用({len(self.cuts)} 個片段刪除)")

    def _restore_from_cut_preview(self):
        if not self.cut_preview_active:
            return
        if not self.original_media_path:
            return
        # 載回原始
        try:
            self.player.load(self.original_media_path)
        except Exception as e:
            messagebox.showerror("還原失敗", str(e))
            return
        self.scrubber.configure(from_=0, to=max(1.0, self.player.duration))
        self.scrubber.set(0)
        # SRT 恢復
        if self.original_srt_blocks:
            self.srt_blocks = self.original_srt_blocks
            self._populate_srt_panel()
            self.srt_status_lbl.configure(
                text=f"{len(self.srt_blocks)} blocks · "
                     f"{(self.srt_path.name if self.srt_path else '原始')}")
            self._apply_srt_subtitle_preview()
        # UI 還原
        self.cut_preview_active = False
        self.cut_restore_btn.pack_forget()
        self.cut_apply_btn.pack(side="left", padx=2)
        self.cut_apply_btn.configure(state="normal", text="📺 套用剪輯預覽")
        self.cut_preview_status_lbl.configure(text="")
        self._apply_crop_preview()
        self.status_lbl.configure(text="✓ 已還原原始影片")

    # ----------------- 框選裁切 -----------------

    def _capture_current_frame(self) -> Path:
        """用 ffmpeg 抓當前播放秒數的單張 frame → JPG temp 檔。"""
        import tempfile, hashlib
        if not self.media_path or not self.player:
            raise RuntimeError("沒載入影片")
        t = max(0.0, self.player.get_time_sec())
        h = hashlib.md5(f"{self.media_path}_{int(t*1000)}".encode()).hexdigest()[:10]
        out = Path(tempfile.gettempdir()) / f"srt_v2_frame_{h}.jpg"
        cmd = [
            get_ffmpeg(), "-y",
            "-ss", str(t),
            "-i", str(self.media_path),
            "-vframes", "1",
            "-q:v", "2",
            str(out),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"截圖失敗: {r.stderr[-300:]}")
        return out

    def _open_crop_dialog(self):
        if self.player is None or self.media_path is None:
            messagebox.showwarning("框選", "請先載入影片")
            return
        # 暫停影片
        try:
            if self.player.is_playing():
                self.player.pause()
                self.play_btn.configure(text="▶ 播放")
        except Exception:
            pass
        self.status_lbl.configure(text="截取當前畫面...")
        self.update_idletasks()
        try:
            frame_path = self._capture_current_frame()
        except Exception as e:
            messagebox.showerror("截圖失敗", str(e))
            return
        # 開 dialog
        CropDialog(self, frame_path, on_confirm=self._on_crop_confirm)
        self.status_lbl.configure(text="在彈出視窗框選裁切區域...")

    def _on_crop_confirm(self, region: tuple[int, int, int, int, int, int]):
        self.crop_region = region
        self._crop_aspect = None  # 框選裁切是任意視窗,非比例
        x, y, w, h, ow, oh = region
        self.crop_status_lbl.configure(
            text=f"裁切: {w}×{h} @ ({x}, {y})  原 {ow}×{oh}",
            text_color=("#7c3aed", "#c4b5fd"),
        )
        self.status_lbl.configure(text=f"✓ 已設定裁切區域 {w}×{h}(預覽即時更新)")
        # 即時預覽 — VLC 的 crop_geometry
        self._apply_crop_preview()

    def _clear_crop(self):
        self.crop_region = None
        self._crop_aspect = None
        self.crop_status_lbl.configure(
            text="裁切:無(輸出時保留完整畫面)",
            text_color=("#6b7280", "#9ca3af"),
        )
        self._apply_crop_preview()

    # ----------------- 靜音偵測 -----------------

    def _detect_silence(self):
        if not self.media_path or not self.player:
            messagebox.showwarning("偵測靜音", "請先載入影片")
            return
        # 簡易參數對話框
        dlg = ctk.CTkToplevel(self)
        dlg.title("靜音偵測參數")
        dlg.geometry("400x230+200+200")
        dlg.attributes("-topmost", True)
        ctk.CTkLabel(dlg, text="參數設定",
                     font=ctk.CTkFont(weight="bold")).pack(pady=(10, 4))
        # 噪音閾值
        row1 = ctk.CTkFrame(dlg, fg_color="transparent")
        row1.pack(fill="x", padx=20, pady=4)
        ctk.CTkLabel(row1, text="噪音閾值(dB)", width=140, anchor="w").pack(side="left")
        nv = ctk.StringVar(value="-30")
        ctk.CTkEntry(row1, textvariable=nv, width=80).pack(side="left")
        ctk.CTkLabel(row1, text="(越接近 0 越嚴格)",
                     font=ctk.CTkFont(size=10),
                     text_color=("#6b7280", "#9ca3af")).pack(side="left", padx=6)
        # 最短持續秒數
        row2 = ctk.CTkFrame(dlg, fg_color="transparent")
        row2.pack(fill="x", padx=20, pady=4)
        ctk.CTkLabel(row2, text="最短持續(秒)", width=140, anchor="w").pack(side="left")
        dv = ctk.StringVar(value="1.5")
        ctk.CTkEntry(row2, textvariable=dv, width=80).pack(side="left")

        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(pady=14)
        ctk.CTkButton(btn_row, text="取消", width=80,
                      command=dlg.destroy).pack(side="left", padx=4)
        def run():
            try:
                noise_db = float(nv.get())
                min_dur = float(dv.get())
            except ValueError:
                messagebox.showerror("參數錯誤", "閾值/秒數要是數字")
                return
            dlg.destroy()
            threading.Thread(target=self._run_silence_detect,
                              args=(noise_db, min_dur), daemon=True).start()
        ctk.CTkButton(btn_row, text="開始偵測", width=120,
                      fg_color="#7c3aed", hover_color="#6d28d9",
                      font=ctk.CTkFont(weight="bold"),
                      command=run).pack(side="left", padx=4)

    def _run_silence_detect(self, noise_db: float, min_dur: float):
        overlay_ref = {"ov": None}
        self.after(0, lambda: overlay_ref.update(
            ov=ProgressOverlay(self, "偵測靜音",
                                f"ffmpeg silencedetect 跑全片中(noise={noise_db}dB / ≥{min_dur}s)...")))
        try:
            self.after(0, lambda: self.status_lbl.configure(
                text=f"偵測靜音中(noise={noise_db}dB / 最短={min_dur}s)..."))
            ffmpeg = get_ffmpeg()
            cmd = [
                ffmpeg, "-hide_banner", "-nostats",
                "-i", str(self.media_path),
                "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
                "-f", "null", "-",
            ]
            r = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
            # 解析 stderr 抓 silence_start / silence_end
            silences = []  # list of (start, end, duration)
            start = None
            for line in (r.stderr or "").splitlines():
                m1 = re.search(r"silence_start:\s*(-?[\d.]+)", line)
                m2 = re.search(r"silence_end:\s*([\d.]+)\s+\|\s+silence_duration:\s*([\d.]+)", line)
                if m1:
                    start = max(0.0, float(m1.group(1)))
                elif m2 and start is not None:
                    end = float(m2.group(1))
                    dur = float(m2.group(2))
                    silences.append((start, end, dur))
                    start = None

            if not silences:
                self.after(0, lambda: messagebox.showinfo(
                    "偵測完成", f"沒找到符合條件的靜音段。\n\n"
                                f"條件:< {noise_db}dB 且持續 ≥ {min_dur}s"))
                self.after(0, lambda: self.status_lbl.configure(
                    text="✓ 靜音偵測:找不到符合條件的段"))
                return

            self.after(0, lambda s=silences: self._show_silence_results(s))
        except Exception as e:
            err_msg = str(e)
            self.after(0, lambda: messagebox.showerror("偵測失敗", err_msg))
            self.after(0, lambda: self.status_lbl.configure(text=f"❌ {err_msg}"))
        finally:
            self.after(0, lambda: overlay_ref["ov"].done() if overlay_ref["ov"] else None)

    def _show_silence_results(self, silences: list):
        """顯示偵測到的靜音段,使用者勾選後加入剪輯清單。"""
        dlg = ctk.CTkToplevel(self)
        dlg.title(f"偵測到 {len(silences)} 個靜音段")
        dlg.geometry("520x600+250+150")
        dlg.attributes("-topmost", True)
        ctk.CTkLabel(
            dlg, text=f"找到 {len(silences)} 個靜音段 — 勾選要加入剪輯的:",
            font=ctk.CTkFont(weight="bold")).pack(pady=(10, 4))
        # 全選/全不選
        top_row = ctk.CTkFrame(dlg, fg_color="transparent")
        top_row.pack(fill="x", padx=14, pady=4)
        vars_list = [ctk.BooleanVar(value=True) for _ in silences]
        def select_all(v):
            for var in vars_list:
                var.set(v)
        ctk.CTkButton(top_row, text="全選", width=70,
                      command=lambda: select_all(True)).pack(side="left", padx=2)
        ctk.CTkButton(top_row, text="全不選", width=70,
                      command=lambda: select_all(False)).pack(side="left", padx=2)
        ctk.CTkLabel(top_row,
                     text=f"總靜音 {sum(d for _,_,d in silences):.1f}s",
                     font=ctk.CTkFont(family="Consolas", size=11)
                     ).pack(side="right", padx=6)

        # 清單
        scroll = ctk.CTkScrollableFrame(dlg, fg_color=("#f8fafc", "#0d1117"))
        scroll.pack(fill="both", expand=True, padx=14, pady=8)
        for i, (st, ed, dur) in enumerate(silences):
            row = ctk.CTkFrame(scroll, fg_color=("#ffffff", "#161b22"),
                                corner_radius=4)
            row.pack(fill="x", padx=2, pady=1)
            ctk.CTkCheckBox(row, text="", variable=vars_list[i],
                            width=20).pack(side="left", padx=4)
            ctk.CTkLabel(
                row,
                text=f"{sec_to_srt_ts(st)[:-4]} → {sec_to_srt_ts(ed)[:-4]}  ({dur:.1f}s)",
                font=ctk.CTkFont(family="Consolas", size=11),
                anchor="w",
            ).pack(side="left", padx=8, pady=4, fill="x", expand=True)

        # 動作
        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(pady=10)
        def add_selected():
            added = 0
            for (st, ed, dur), v in zip(silences, vars_list):
                if not v.get():
                    continue
                # 跳過重疊
                ok = True
                for cs, ce in self.cuts:
                    if not (ed <= cs or st >= ce):
                        ok = False; break
                if ok:
                    self.cuts.append((st, ed))
                    added += 1
            self.cuts.sort()
            self._render_cut_list()
            dlg.destroy()
            self.status_lbl.configure(
                text=f"✓ 加入 {added} 個靜音剪輯片段(總計 {len(self.cuts)})")
        ctk.CTkButton(btn_row, text="取消", width=80,
                      command=dlg.destroy).pack(side="left", padx=4)
        ctk.CTkButton(btn_row, text="✓ 加入剪輯",
                      fg_color="#16a34a", hover_color="#15803d",
                      font=ctk.CTkFont(weight="bold"),
                      width=140,
                      command=add_selected).pack(side="left", padx=4)

    # ----------------- 時間軸縮圖 -----------------

    def _build_timeline_thumbnails(self):
        """背景 thread 用 ffmpeg 抽 ~20 張縮圖,拼成一條 image 顯示在 timeline canvas。"""
        if not self.media_path or not self.player:
            return
        threading.Thread(target=self._do_build_thumbnails, daemon=True).start()

    def _do_build_thumbnails(self):
        try:
            import tempfile, hashlib
            from PIL import Image, ImageTk
            duration = self.player.duration
            if duration < 1:
                return
            n_thumbs = 20
            canvas_w = max(800, self.timeline_canvas.winfo_width())
            canvas_h = 60  # 縮圖高度,留 12px 給時間刻度
            thumb_w = canvas_w // n_thumbs
            thumb_h = canvas_h

            ffmpeg = get_ffmpeg()
            sig = hashlib.md5(f"{self.media_path}_{duration}".encode()).hexdigest()[:8]
            tmp_dir = Path(tempfile.gettempdir()) / f"srt_v2_thumbs_{sig}"
            tmp_dir.mkdir(parents=True, exist_ok=True)

            # 一次性產出 20 張縮圖
            interval = duration / n_thumbs
            thumb_paths = []
            for i in range(n_thumbs):
                t = i * interval + interval / 2  # 取每段中點
                out = tmp_dir / f"t_{i:02d}.jpg"
                if not out.exists():
                    cmd = [
                        ffmpeg, "-y", "-ss", str(t),
                        "-i", str(self.media_path),
                        "-vframes", "1", "-vf", f"scale={thumb_w}:{thumb_h}",
                        "-q:v", "5", str(out),
                    ]
                    subprocess.run(cmd, capture_output=True,
                                   encoding="utf-8", errors="replace")
                if out.exists():
                    thumb_paths.append(out)

            if not thumb_paths:
                return

            # 拼成一張長條圖
            strip = Image.new("RGB", (thumb_w * len(thumb_paths), thumb_h), "#000000")
            for i, p in enumerate(thumb_paths):
                try:
                    img = Image.open(p).resize((thumb_w, thumb_h), Image.LANCZOS)
                    strip.paste(img, (i * thumb_w, 0))
                except Exception:
                    pass

            self.after(0, lambda: self._set_thumbnail_strip(strip, duration))
        except Exception as e:
            print(f"[thumbnails] err: {e}", flush=True)

    def _set_thumbnail_strip(self, pil_img, duration: float):
        # 保留原始 PIL strip,resize 時再 scale
        self._thumb_pil_strip = pil_img
        self._thumb_duration = duration
        self._redraw_timeline_canvas()
        # 縮圖好了 → tracks 也跟著渲
        self.after(50, self._render_tracks)

    def _redraw_timeline_canvas(self):
        """依當前 canvas 寬度重畫 thumbnail strip + 時間刻度 + 指示器。"""
        if not hasattr(self, "_thumb_pil_strip") or self._thumb_pil_strip is None:
            return
        from PIL import ImageTk
        canvas_w = self.timeline_canvas.winfo_width()
        if canvas_w < 4:
            return
        # scale PIL strip to canvas width(保持高度 60)
        target_h = 60
        scaled = self._thumb_pil_strip.resize((canvas_w, target_h))
        self._thumb_photo = ImageTk.PhotoImage(scaled)
        c = self.timeline_canvas
        c.delete("all")
        c.create_image(0, 0, image=self._thumb_photo, anchor="nw")
        duration = self._thumb_duration
        for tick in range(0, int(duration), max(60, int(duration // 8))):
            x = (tick / duration) * canvas_w
            c.create_line(x, 60, x, 72, fill="#9aa0a6")
            m, s = divmod(int(tick), 60)
            c.create_text(x + 2, 62, anchor="nw",
                           text=f"{m:02d}:{s:02d}",
                           fill="#9aa0a6", font=("Consolas", 8))
        self._thumb_indicator_id = c.create_line(0, 0, 0, 60, fill="#ef4444", width=2)

    def _on_timeline_resize(self):
        """視窗 resize 時 debounce 重畫(避免拖動時瘋狂呼叫)。"""
        if self._resize_debounce_id is not None:
            try: self.after_cancel(self._resize_debounce_id)
            except Exception: pass
        self._resize_debounce_id = self.after(100, self._do_timeline_resize)

    def _do_timeline_resize(self):
        self._resize_debounce_id = None
        self._redraw_timeline_canvas()
        self._render_tracks()

    def _on_timeline_click(self, event):
        if not getattr(self, "_thumb_duration", 0):
            return
        canvas_w = self.timeline_canvas.winfo_width()
        if canvas_w <= 0:
            return
        ratio = event.x / canvas_w
        target = max(0.0, min(self._thumb_duration, ratio * self._thumb_duration))
        self._seek_to(target)

    # --- timeline tracks(文字疊加 + 音訊 clip)---

    def _render_tracks(self):
        """重畫所有 timeline tracks。"""
        self._render_overlay_track()
        self._render_audio_track()
        self._render_image_track()

    def _render_overlay_track(self):
        c = getattr(self, "overlay_track_canvas", None)
        if c is None:
            return
        c.delete("all")
        dur = getattr(self, "_thumb_duration", 0)
        w = c.winfo_width()
        if dur <= 0 or w < 4:
            c.create_text(8, 12, anchor="w", text="(載入影片後此處顯示文字疊加區段)",
                          fill="#6b7280", font=("Microsoft JhengHei", 9))
            return
        c.create_text(4, 12, anchor="w", text="✨", fill="#67e8f9",
                       font=("Microsoft JhengHei", 10))
        for i, ov in enumerate(getattr(self, "text_overlays", [])):
            s = ov.get("start_sec")
            e = ov.get("end_sec")
            s_eff = max(0.0, s) if s is not None else 0.0
            e_eff = min(dur, e) if e is not None else dur
            if e_eff <= s_eff:
                continue
            x1 = (s_eff / dur) * w
            x2 = (e_eff / dur) * w
            c.create_rectangle(
                x1, 3, x2, 21,
                fill="#0891b2", outline="#67e8f9", width=1,
                tags=("ov_bar", f"ov_{i}"),
            )
            label = ov.get("text", "")[:14] + ("…" if len(ov.get("text", "")) > 14 else "")
            c.create_text(
                x1 + 4, 12, anchor="w", text=label,
                fill="white", font=("Microsoft JhengHei", 9),
                tags=(f"ov_{i}_t",),
            )

    def _render_audio_track(self):
        c = getattr(self, "audio_track_canvas", None)
        if c is None:
            return
        c.delete("all")
        dur = getattr(self, "_thumb_duration", 0)
        w = c.winfo_width()
        if dur <= 0 or w < 4:
            c.create_text(8, 12, anchor="w", text="(載入影片後此處顯示背景音區段)",
                          fill="#6b7280", font=("Microsoft JhengHei", 9))
            return
        c.create_text(4, 12, anchor="w", text="🎵", fill="#f9a8d4",
                       font=("Microsoft JhengHei", 10))
        for i, clip in enumerate(getattr(self, "bg_music_clips", [])):
            s = clip.get("start_sec")
            e = clip.get("end_sec")
            s_eff = max(0.0, s) if s is not None else 0.0
            e_eff = min(dur, e) if e is not None else dur
            if e_eff <= s_eff:
                continue
            x1 = (s_eff / dur) * w
            x2 = (e_eff / dur) * w
            c.create_rectangle(
                x1, 3, x2, 21,
                fill="#db2777", outline="#f9a8d4", width=1,
                tags=("au_bar", f"au_{i}"),
            )
            name = clip["path"].name
            label = name[:14] + ("…" if len(name) > 14 else "")
            c.create_text(
                x1 + 4, 12, anchor="w", text=label,
                fill="white", font=("Microsoft JhengHei", 9),
                tags=(f"au_{i}_t",),
            )

    def _track_canvas_and_clips(self, track_type: str):
        if track_type == "overlay":
            return self.overlay_track_canvas, self.text_overlays
        if track_type == "image":
            return self.image_track_canvas, self.image_overlays
        return self.audio_track_canvas, self.bg_music_clips

    def _track_hit_test(self, event, track_type: str):
        """回 (idx, mode) — mode ∈ {'move','resize_left','resize_right'},點空白回 None。"""
        canvas, clips = self._track_canvas_and_clips(track_type)
        dur = getattr(self, "_thumb_duration", 0)
        w = canvas.winfo_width()
        if dur <= 0 or w < 4:
            return None
        # 從右到左掃描,讓後加的優先(stack 上方)
        for i in range(len(clips) - 1, -1, -1):
            clip = clips[i]
            s = clip.get("start_sec")
            e = clip.get("end_sec")
            s_eff = max(0.0, s) if s is not None else 0.0
            e_eff = min(dur, e) if e is not None else dur
            x1 = (s_eff / dur) * w
            x2 = (e_eff / dur) * w
            if event.x < x1 - 2 or event.x > x2 + 2:
                continue
            if 3 <= event.y <= 21:
                if abs(event.x - x1) <= 5:
                    return (i, "resize_left")
                if abs(event.x - x2) <= 5:
                    return (i, "resize_right")
                return (i, "move")
        return None

    def _track_motion(self, event, track_type: str):
        """純 hover — 切換 cursor(沒拖曳時)。"""
        if self._track_drag_state:
            return
        canvas, _ = self._track_canvas_and_clips(track_type)
        hit = self._track_hit_test(event, track_type)
        if hit is None:
            canvas.configure(cursor="hand2")
        elif hit[1] in ("resize_left", "resize_right"):
            canvas.configure(cursor="sb_h_double_arrow")
        else:
            canvas.configure(cursor="fleur")

    def _track_click(self, event, track_type: str):
        hit = self._track_hit_test(event, track_type)
        if hit is None:
            return
        idx, mode = hit
        _, clips = self._track_canvas_and_clips(track_type)
        dur = getattr(self, "_thumb_duration", 0)
        clip = clips[idx]
        s = clip.get("start_sec")
        e = clip.get("end_sec")
        s_eff = max(0.0, s) if s is not None else 0.0
        e_eff = min(dur, e) if e is not None else dur
        self._track_drag_state = {
            "type":        track_type,
            "idx":         idx,
            "mode":        mode,
            "start_x":     event.x,
            "init_start":  s_eff,
            "init_end":    e_eff,
        }

    def _track_drag(self, event, track_type: str):
        ds = self._track_drag_state
        if not ds or ds["type"] != track_type:
            return
        canvas, clips = self._track_canvas_and_clips(track_type)
        idx = ds["idx"]
        dur = getattr(self, "_thumb_duration", 0)
        w = canvas.winfo_width()
        if dur <= 0 or w < 4:
            return
        delta_x = event.x - ds["start_x"]
        delta_sec = (delta_x / w) * dur
        init_s = ds["init_start"]
        init_e = ds["init_end"]
        d_len = init_e - init_s

        if ds["mode"] == "move":
            new_s = init_s + delta_sec
            new_e = init_e + delta_sec
            # clamp 不出邊界,維持原 duration
            if new_s < 0:
                new_s = 0; new_e = d_len
            if new_e > dur:
                new_e = dur; new_s = max(0, dur - d_len)
        elif ds["mode"] == "resize_left":
            new_s = max(0, min(init_e - 0.3, init_s + delta_sec))
            new_e = init_e
        elif ds["mode"] == "resize_right":
            new_s = init_s
            new_e = max(init_s + 0.3, min(dur, init_e + delta_sec))
        else:
            return

        clips[idx]["start_sec"] = round(new_s, 2)
        clips[idx]["end_sec"] = round(new_e, 2)
        # 即時 re-render 該條 track
        if track_type == "overlay":
            self._render_overlay_track()
        elif track_type == "image":
            self._render_image_track()
        else:
            self._render_audio_track()

    def _track_release(self, event, track_type: str):
        if not self._track_drag_state or self._track_drag_state["type"] != track_type:
            self._track_drag_state = None
            return
        self._track_drag_state = None
        # commit:重畫主清單(讓側邊列也更新時間顯示)
        if track_type == "overlay":
            self._render_text_overlay_list()
        elif track_type == "image":
            self._render_image_overlay_list()
        else:
            self._render_bg_clips_list()

    def _update_timeline_indicator(self, t: float):
        if not getattr(self, "_thumb_indicator_id", None) or \
                not getattr(self, "_thumb_duration", 0):
            return
        if not self._thumb_photo:
            return
        canvas_w = self._thumb_photo.width()
        x = (t / self._thumb_duration) * canvas_w
        try:
            self.timeline_canvas.coords(self._thumb_indicator_id, x, 0, x, 60)
        except Exception:
            pass

    # ----------------- 音訊控制 -----------------

    def _update_vol_lbl(self):
        self.audio_vol_lbl.configure(text=f"{int(self.audio_vol_var.get()*100)}%")
        self.bg_vol_lbl.configure(text=f"{int(self.bg_vol_var.get()*100)}%")
        if self.mute_var.get():
            self.audio_vol_lbl.configure(text="靜音")

    def _pick_bg_music(self):
        p = filedialog.askopenfilename(
            title="選背景音樂",
            filetypes=[("Audio", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"),
                       ("All", "*.*")],
        )
        if not p:
            return
        self.bg_music_entry.delete(0, "end")
        self.bg_music_entry.insert(0, str(p))

    def _add_bg_clip(self):
        path_str = self.bg_music_entry.get().strip()
        if not path_str:
            messagebox.showwarning("背景音", "請先選音檔")
            return
        p = Path(path_str)
        if not p.exists():
            messagebox.showerror("背景音", f"檔案不存在:{p}")
            return
        start_sec = parse_time_str(self.bg_start_var.get())
        end_sec = parse_time_str(self.bg_end_var.get())
        clip = {
            "path":     p,
            "start_sec": start_sec,
            "end_sec":   end_sec,
            "volume":    float(self.bg_vol_var.get()),
        }
        self.bg_music_clips.append(clip)
        # 清空輸入欄
        self.bg_music_entry.delete(0, "end")
        self.bg_start_var.set("")
        self.bg_end_var.set("")
        self._render_bg_clips_list()
        self._render_audio_track()
        self.status_lbl.configure(text=f"✓ 加入背景音 clip(總計 {len(self.bg_music_clips)})")

    def _clear_bg_clips(self):
        self.bg_music_clips.clear()
        self._render_bg_clips_list()
        self._render_audio_track()

    def _remove_bg_clip(self, idx: int):
        if 0 <= idx < len(self.bg_music_clips):
            self.bg_music_clips.pop(idx)
            self._render_bg_clips_list()
            self._render_audio_track()

    def _render_bg_clips_list(self):
        for w in list(self.bg_clips_scroll.winfo_children()):
            try: w.destroy()
            except Exception: pass
        if not self.bg_music_clips:
            ctk.CTkLabel(
                self.bg_clips_scroll, text="(尚無背景音 — 上方輸入後按「+ 加入」)",
                font=ctk.CTkFont(size=10),
                text_color=("#9ca3af", "#6e7681"),
            ).pack(pady=6)
            return
        for i, clip in enumerate(self.bg_music_clips):
            row = ctk.CTkFrame(self.bg_clips_scroll,
                                fg_color=("#fce7f3", "#1f0a1c"),
                                corner_radius=6)
            row.pack(fill="x", padx=2, pady=1)
            s, e = clip.get("start_sec"), clip.get("end_sec")
            t_str = ("全片" if (s is None and e is None)
                      else f"{sec_to_short_ts(s)}~{sec_to_short_ts(e)}")
            vol_str = f"{int(clip['volume']*100)}%"
            preview = clip["path"].name
            if len(preview) > 30:
                preview = preview[:27] + "…"
            ctk.CTkLabel(
                row,
                text=f"🎵 {preview}  ·  {t_str}  ·  {vol_str}",
                font=ctk.CTkFont(family="Consolas", size=10),
                anchor="w",
            ).pack(side="left", padx=8, pady=3, fill="x", expand=True)
            ctk.CTkButton(
                row, text="×", width=24, height=22,
                fg_color="#dc2626", hover_color="#b91c1c",
                command=lambda idx=i: self._remove_bg_clip(idx),
            ).pack(side="right", padx=4)

    def _pick_secondary_srt(self):
        p = filedialog.askopenfilename(
            title="選第二字幕(下層小字)",
            filetypes=[("SRT", "*.srt"), ("All", "*.*")],
        )
        if not p:
            return
        self.secondary_srt_path = Path(p)
        self.secondary_srt_entry.delete(0, "end")
        self.secondary_srt_entry.insert(0, str(p))
        self.status_lbl.configure(text=f"✓ 第二字幕:{self.secondary_srt_path.name}")

    def _clear_secondary_srt(self):
        self.secondary_srt_path = None
        self.secondary_srt_entry.delete(0, "end")

    def _apply_aspect_preset(self, ratio_w: int, ratio_h: int):
        """套用長寬比預設 — 算出最大內接矩形,置中,設成 crop。"""
        if not self.media_path or not self.player:
            messagebox.showwarning("長寬比", "請先載入影片")
            return
        ow, oh = get_video_dimensions(self.media_path)
        if ow == 0 or oh == 0:
            messagebox.showerror("長寬比", "讀不到影片解析度")
            return
        target_ratio = ratio_w / ratio_h
        current_ratio = ow / oh
        if current_ratio > target_ratio:
            new_h = oh
            new_w = int(oh * target_ratio)
        else:
            new_w = ow
            new_h = int(ow / target_ratio)
        # 對齊偶數
        new_w -= new_w % 2
        new_h -= new_h % 2
        cx = (ow - new_w) // 2
        cy = (oh - new_h) // 2
        cx -= cx % 2
        cy -= cy % 2

        # 邊界:若 crop 等同原片(目標比例就是原片比例)→ 直接清空避免 VLC 黑屏
        if new_w >= ow - 4 and new_h >= oh - 4:
            self._clear_crop()
            self.status_lbl.configure(
                text=f"✓ 原片已經是 {ratio_w}:{ratio_h},不需裁切")
            return

        self.crop_region = (cx, cy, new_w, new_h, ow, oh)
        # 記下這是「比例」裁切 → 預覽用 VLC 比例裁切字串(最穩)
        self._crop_aspect = (ratio_w, ratio_h)
        self.crop_status_lbl.configure(
            text=f"裁切: {new_w}×{new_h} @ ({cx}, {cy})  {ratio_w}:{ratio_h}  原 {ow}×{oh}",
            text_color=("#0891b2", "#67e8f9"),
        )
        self.status_lbl.configure(text=f"✓ 套用 {ratio_w}:{ratio_h} 長寬比預設")
        self._apply_crop_preview()

    def _apply_crop_preview(self):
        """把當前裁切套到 VLC 做即時預覽。

        長寬比預設(9:16 / 1:1 / 16:9)→ 用 VLC 的「比例」裁切字串
        video_set_crop_geometry("9:16"),由 VLC 自動置中裁切。這是 VLC
        內建的裁切模式(等同 VLC 選單的 16:9 / 4:3 / 1:1),最穩定。
        先前用 "WxH+X+Y" 視窗格式在這版 libvlc 套不上去(9:16 全黑、
        1:1 異常)。框選裁切是任意視窗 → 才用 "WxH+X+Y" 格式。
        """
        if self.player is None or self.player.player is None:
            return
        p = self.player.player
        try:
            if getattr(self, "_crop_aspect", None):
                rw, rh = self._crop_aspect
                p.video_set_crop_geometry(f"{rw}:{rh}")
            elif self.crop_region:
                x, y, w, h, _, _ = self.crop_region
                p.video_set_crop_geometry(f"{int(w)}x{int(h)}+{int(x)}+{int(y)}")
            else:
                p.video_set_crop_geometry(None)
        except Exception:
            pass

    # ----------------- 文字疊加 -----------------

    def _first_available_font(self) -> str:
        """掃 TEXT_FONTS 找第一個實際存在的,給下拉預設用。"""
        for name, p in TEXT_FONTS.items():
            if p.exists():
                return name
        # 全沒有 → 還是回第一個(輸出時會報錯)
        return next(iter(TEXT_FONTS.keys()))

    # ----------------- 快捷鍵 -----------------

    def _setup_shortcuts(self):
        """全域快捷鍵 — Space 播停 / J K L 倒停順 / I O 標起終 / Del 移除最後 cut / Ctrl+S 存項目 / S 截圖。"""
        bindings = {
            "<space>":        lambda e: self._kb_toggle_play(),
            "<KeyPress-j>":   lambda e: self._kb_rate_step(-1),
            "<KeyPress-k>":   lambda e: self._kb_pause(),
            "<KeyPress-l>":   lambda e: self._kb_rate_step(1),
            "<KeyPress-i>":   lambda e: self._mark_cut_in(),
            "<KeyPress-o>":   lambda e: self._mark_cut_out(),
            "<Delete>":       lambda e: self._kb_delete_last_cut(),
            "<Control-s>":    lambda e: self._save_project(),
            "<Control-o>":    lambda e: self._open_project(),
            "<Control-z>":    lambda e: self._kb_undo(),
            "<Control-y>":    lambda e: self._kb_redo(),
            "<KeyPress-s>":   lambda e: self._save_frame_png(),
            "<Right>":        lambda e: self._seek_rel(5),
            "<Left>":         lambda e: self._seek_rel(-5),
        }
        for keyseq, callback in bindings.items():
            try:
                self.bind_all(keyseq, callback)
            except Exception:
                pass

    def _kb_toggle_play(self):
        # 若焦點在輸入框,別搶 Space
        focused = self.focus_get()
        if isinstance(focused, (ctk.CTkEntry, ctk.CTkTextbox)) or \
           (focused and focused.winfo_class() in ("Entry", "Text")):
            return
        self._toggle_play()

    def _kb_pause(self):
        if self.player and self.player.is_playing():
            self.player.pause()
            self.play_btn.configure(text="▶ 播放")

    def _kb_rate_step(self, direction: int):
        """J: 倒退/減速,L: 快進/加速。先播狀態 → 速度切換,暫停 → seek。"""
        if self.player is None:
            return
        if self.player.is_playing():
            cur_rate = float(self.speed_var.get().replace("x", ""))
            speeds = [float(s.replace("x", "")) for s in SPEED_OPTIONS]
            idx = min(range(len(speeds)), key=lambda i: abs(speeds[i] - cur_rate))
            new_idx = max(0, min(len(speeds) - 1, idx + direction))
            new_rate = speeds[new_idx]
            self.speed_var.set(f"{new_rate}x")
            self.player.set_rate(new_rate)
        else:
            self._seek_rel(direction * 5)

    def _kb_delete_last_cut(self):
        if not self.cuts:
            return
        self.cuts.pop()
        self._render_cut_list()
        try: self._render_tracks()
        except Exception: pass
        self.status_lbl.configure(text=f"✓ 移除最後一個剪輯片段(剩 {len(self.cuts)})")

    # ----------------- Undo/Redo(剪輯狀態 snapshot) -----------------

    def _kb_undo(self):
        if not hasattr(self, "_undo_stack"):
            self._undo_stack = []
            self._redo_stack = []
        if not self._undo_stack:
            self.status_lbl.configure(text="(沒有可還原的操作)")
            return
        # push current to redo, pop from undo
        cur = self._snapshot_state()
        self._redo_stack.append(cur)
        prev = self._undo_stack.pop()
        self._restore_state(prev)
        self.status_lbl.configure(text=f"↶ 還原(undo stack: {len(self._undo_stack)})")

    def _kb_redo(self):
        if not hasattr(self, "_redo_stack") or not self._redo_stack:
            self.status_lbl.configure(text="(沒有可重做的操作)")
            return
        cur = self._snapshot_state()
        if not hasattr(self, "_undo_stack"):
            self._undo_stack = []
        self._undo_stack.append(cur)
        nxt = self._redo_stack.pop()
        self._restore_state(nxt)
        self.status_lbl.configure(text=f"↷ 重做(redo stack: {len(self._redo_stack)})")

    def _push_undo_snapshot(self):
        """每次重大狀態改變前呼叫 — 把當前 state 存進 undo stack。"""
        if not hasattr(self, "_undo_stack"):
            self._undo_stack = []
            self._redo_stack = []
        self._undo_stack.append(self._snapshot_state())
        self._redo_stack.clear()  # 新動作後 redo 失效
        if len(self._undo_stack) > 50:
            self._undo_stack.pop(0)

    def _snapshot_state(self) -> dict:
        import copy
        return {
            "cuts": list(self.cuts),
            "crop_region": self.crop_region,
            "text_overlays": copy.deepcopy(self.text_overlays),
            "image_overlays": copy.deepcopy(self.image_overlays),
            "bg_music_clips": copy.deepcopy(self.bg_music_clips),
        }

    def _restore_state(self, s: dict):
        self.cuts = list(s.get("cuts", []))
        self.crop_region = s.get("crop_region")
        self.text_overlays = s.get("text_overlays", [])
        self.image_overlays = s.get("image_overlays", [])
        self.bg_music_clips = s.get("bg_music_clips", [])
        self._render_cut_list()
        self._render_text_overlay_list()
        self._render_image_overlay_list()
        self._render_bg_clips_list()
        self._render_tracks()
        self._apply_crop_preview()

    # ----------------- 截圖另存 PNG -----------------

    def _save_frame_png(self):
        if not self.media_path or not self.player:
            return
        try:
            tmp_jpg = self._capture_current_frame()
        except Exception as e:
            messagebox.showerror("截圖", str(e))
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = output_dir(self.media_path, "snapshots") / \
              f"{self.media_path.stem}.snapshot.{stamp}.png"
        try:
            from PIL import Image
            Image.open(tmp_jpg).save(dst, "PNG")
            self.status_lbl.configure(text=f"📷 截圖存到 output/snapshots/{dst.name}")
        except Exception as e:
            messagebox.showerror("截圖", f"PNG 存檔失敗: {e}")

    # ----------------- GIF 輸出 -----------------

    def _export_gif(self):
        """從當前 cut_in 跟 cut_out 之間輸出 GIF。沒設就用 0~10s。"""
        if not self.media_path or not self.player:
            messagebox.showwarning("GIF", "請先載入影片")
            return
        st = self.cut_in if self.cut_in is not None else self.player.get_time_sec()
        ed = self.cut_out if self.cut_out is not None else min(self.player.duration, st + 10)
        if ed <= st:
            messagebox.showwarning("GIF", "終點必須晚於起點")
            return
        if ed - st > 30:
            if not messagebox.askyesno("GIF", f"片段 {ed-st:.1f}s 太長,gif 會很大,要繼續嗎?"):
                return
        threading.Thread(target=self._run_gif_export, args=(st, ed), daemon=True).start()

    def _run_gif_export(self, st: float, ed: float):
        overlay_ref = {"ov": None}
        self.after(0, lambda: overlay_ref.update(
            ov=ProgressOverlay(self, "輸出 GIF", "ffmpeg 兩階段(palette + gen)...")))
        try:
            import tempfile, hashlib
            ffmpeg = get_ffmpeg()
            sig = hashlib.md5(f"gif_{self.media_path}_{st}_{ed}".encode()).hexdigest()[:8]
            tmp = Path(tempfile.gettempdir()) / f"srt_v2_gif_{sig}"
            tmp.mkdir(parents=True, exist_ok=True)
            palette = tmp / "palette.png"
            # 1. palette
            r = subprocess.run(
                [ffmpeg, "-y", "-ss", str(st), "-to", str(ed),
                 "-i", str(self.media_path),
                 "-vf", "fps=12,scale=480:-1:flags=lanczos,palettegen",
                 str(palette)],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                raise RuntimeError(f"palette gen failed: {r.stderr[-300:]}")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = output_dir(self.media_path, "gif") / \
                  f"{self.media_path.stem}.{stamp}.gif"
            # 2. gen
            r = subprocess.run(
                [ffmpeg, "-y", "-ss", str(st), "-to", str(ed),
                 "-i", str(self.media_path), "-i", str(palette),
                 "-lavfi", "fps=12,scale=480:-1:flags=lanczos[x];[x][1:v]paletteuse",
                 str(out)],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                raise RuntimeError(f"gif gen failed: {r.stderr[-300:]}")
            size_mb = out.stat().st_size / 1024 / 1024
            self.after(0, lambda o=out, m=size_mb: messagebox.showinfo(
                "GIF 完成", f"✓ {o.name}\n大小:{m:.1f} MB\n\n{o}"))
            self.after(0, lambda o=out: self.status_lbl.configure(
                text=f"✓ GIF → output/gif/{o.name}"))
        except Exception as e:
            err_msg = str(e)
            self.after(0, lambda: messagebox.showerror("GIF 失敗", err_msg))
        finally:
            self.after(0, lambda: overlay_ref["ov"].done() if overlay_ref["ov"] else None)

    # ----------------- 圖片疊加 -----------------

    def _pick_image_overlay(self):
        p = filedialog.askopenfilename(
            title="選圖片",
            filetypes=[("Image", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"),
                       ("All", "*.*")],
        )
        if not p:
            return
        self.image_path_entry.delete(0, "end")
        self.image_path_entry.insert(0, p)

    def _add_image_overlay(self):
        path_str = self.image_path_entry.get().strip()
        if not path_str:
            messagebox.showwarning("圖片疊加", "請先選圖片")
            return
        p = Path(path_str)
        if not p.exists():
            messagebox.showerror("圖片疊加", f"檔案不存在:{p}")
            return
        start_sec = parse_time_str(self.image_start_var.get())
        end_sec = parse_time_str(self.image_end_var.get())
        pos_name = self.image_pos_var.get()
        clip = {
            "path":       p,
            "scale":      float(self.image_scale_var.get()),
            "opacity":    float(self.image_opacity_var.get()),
            "pos_mode":   "preset",
            "pos_name":   pos_name,
            "pos":        TEXT_POSITIONS.get(pos_name, TEXT_POSITIONS["底部中央"]),
            "x_px":       0,
            "y_px":       0,
            "start_sec":  start_sec,
            "end_sec":    end_sec,
        }
        self.image_overlays.append(clip)
        self.image_path_entry.delete(0, "end")
        self.image_start_var.set("")
        self.image_end_var.set("")
        self._render_image_overlay_list()
        self._render_image_track()
        self.status_lbl.configure(text=f"✓ 加入圖片疊加(總計 {len(self.image_overlays)})")

    def _clear_image_overlays(self):
        self.image_overlays.clear()
        self._render_image_overlay_list()
        self._render_image_track()

    def _remove_image_overlay(self, idx: int):
        if 0 <= idx < len(self.image_overlays):
            self.image_overlays.pop(idx)
            self._render_image_overlay_list()
            self._render_image_track()

    def _render_image_overlay_list(self):
        for w in list(self.image_overlays_scroll.winfo_children()):
            try: w.destroy()
            except Exception: pass
        if not self.image_overlays:
            ctk.CTkLabel(
                self.image_overlays_scroll, text="(尚無圖片疊加)",
                font=ctk.CTkFont(size=10),
                text_color=("#9ca3af", "#6e7681"),
            ).pack(pady=6)
            return
        for i, ov in enumerate(self.image_overlays):
            row = ctk.CTkFrame(self.image_overlays_scroll,
                                fg_color=("#ffedd5", "#1c0d04"),
                                corner_radius=6)
            row.pack(fill="x", padx=2, pady=1)
            name = ov["path"].name
            if len(name) > 24:
                name = name[:21] + "…"
            s, e = ov.get("start_sec"), ov.get("end_sec")
            t_str = ("全片" if (s is None and e is None)
                      else f"{sec_to_short_ts(s)}~{sec_to_short_ts(e)}")
            label = (f"🖼 {name} · {ov['pos_name']} · "
                     f"{int(ov['scale']*100)}% · {int(ov['opacity']*100)}%opc · {t_str}")
            ctk.CTkLabel(row, text=label,
                          font=ctk.CTkFont(family="Consolas", size=10),
                          anchor="w").pack(side="left", padx=8, pady=3, fill="x", expand=True)
            ctk.CTkButton(row, text="×", width=24, height=22,
                          fg_color="#dc2626", hover_color="#b91c1c",
                          command=lambda idx=i: self._remove_image_overlay(idx)
                          ).pack(side="right", padx=4)

    def _render_image_track(self):
        """圖片疊加 timeline track。若 canvas 不存在則 noop。"""
        c = getattr(self, "image_track_canvas", None)
        if c is None:
            return
        c.delete("all")
        dur = getattr(self, "_thumb_duration", 0)
        w = c.winfo_width()
        if dur <= 0 or w < 4:
            c.create_text(8, 12, anchor="w", text="(載入影片後此處顯示圖片疊加區段)",
                          fill="#6b7280", font=("Microsoft JhengHei", 9))
            return
        c.create_text(4, 12, anchor="w", text="🖼", fill="#fdba74",
                       font=("Microsoft JhengHei", 10))
        for i, ov in enumerate(getattr(self, "image_overlays", [])):
            s = ov.get("start_sec")
            e = ov.get("end_sec")
            s_eff = max(0.0, s) if s is not None else 0.0
            e_eff = min(dur, e) if e is not None else dur
            if e_eff <= s_eff:
                continue
            x1 = (s_eff / dur) * w
            x2 = (e_eff / dur) * w
            c.create_rectangle(x1, 3, x2, 21,
                                fill="#ea580c", outline="#fdba74", width=1,
                                tags=("img_bar", f"img_{i}"))
            c.create_text(x1 + 4, 12, anchor="w", text=ov["path"].name[:14],
                          fill="white", font=("Microsoft JhengHei", 9))

    def _add_text_overlay(self):
        text = self.overlay_text_var.get().strip()
        if not text:
            messagebox.showwarning("文字疊加", "請先輸入文字")
            return
        font_name = self.overlay_font_var.get()
        font_path = TEXT_FONTS.get(font_name)
        if not font_path or not font_path.exists():
            for n, p in TEXT_FONTS.items():
                if p.exists():
                    font_name = n; font_path = p; break
            else:
                messagebox.showerror("文字疊加",
                                      "找不到任何可用字型(C:\\Windows\\Fonts\\ 內找不到任一檔)")
                return
        # 時間範圍
        start_sec = parse_time_str(self.overlay_start_var.get())
        end_sec = parse_time_str(self.overlay_end_var.get())
        border_name = self.overlay_border_var.get()
        border_on, border_color = TEXT_BORDERS.get(border_name, (False, None))
        overlay = {
            "text":         text,
            "font_name":    font_name,
            "font_path":    font_path,
            "size_name":    self.overlay_size_var.get(),
            "size":         TEXT_SIZES[self.overlay_size_var.get()],
            "color_name":   self.overlay_color_var.get(),
            "color":        TEXT_COLORS[self.overlay_color_var.get()],
            "pos_name":     self.overlay_pos_var.get(),
            "pos":          TEXT_POSITIONS[self.overlay_pos_var.get()],
            "pos_mode":     "preset",  # 改 absolute 後用 x_px/y_px
            "x_px":         0,
            "y_px":         0,
            "border":       border_on,
            "border_color": border_color,
            "border_name":  border_name,
            "start_sec":    start_sec,
            "end_sec":      end_sec,
        }
        self.text_overlays.append(overlay)
        # 清欄
        self.overlay_text_var.set("")
        self.overlay_start_var.set("")
        self.overlay_end_var.set("")
        self._render_text_overlay_list()
        self._render_overlay_track()

    def _clear_text_overlays(self):
        self.text_overlays.clear()
        self._render_text_overlay_list()
        self._render_overlay_track()

    def _remove_overlay(self, idx: int):
        if 0 <= idx < len(self.text_overlays):
            self.text_overlays.pop(idx)
            self._render_text_overlay_list()
            self._render_overlay_track()

    def _render_text_overlay_list(self):
        for w in list(self.overlays_scroll.winfo_children()):
            try: w.destroy()
            except Exception: pass
        if not self.text_overlays:
            ctk.CTkLabel(
                self.overlays_scroll, text="(尚無文字疊加)",
                font=ctk.CTkFont(size=10),
                text_color=("#9ca3af", "#6e7681"),
            ).pack(pady=8)
            return
        for i, ov in enumerate(self.text_overlays):
            row = ctk.CTkFrame(self.overlays_scroll, fg_color=("#cffafe", "#062a35"),
                                corner_radius=6)
            row.pack(fill="x", padx=2, pady=1)
            # 簡化:只顯示文字內容(完整 meta 鼠標 hover 在 tooltip 顯示 — 此版省略)
            preview = ov["text"][:30] + ("…" if len(ov["text"]) > 30 else "")
            ctk.CTkLabel(
                row, text=f"✨ {preview}",
                font=ctk.CTkFont(size=11),
                anchor="w",
            ).pack(side="left", padx=8, pady=3, fill="x", expand=True)
            ctk.CTkButton(
                row, text="🎯", width=28, height=22,
                fg_color="#0891b2", hover_color="#0e7490",
                command=lambda idx=i: self._drag_overlay_position(idx),
            ).pack(side="right", padx=2)
            ctk.CTkButton(
                row, text="×", width=24, height=22,
                fg_color="#dc2626", hover_color="#b91c1c",
                command=lambda idx=i: self._remove_overlay(idx),
            ).pack(side="right", padx=2)

    def _drag_overlay_position(self, idx: int):
        if not (0 <= idx < len(self.text_overlays)):
            return
        if self.player is None or self.media_path is None:
            messagebox.showwarning("拖曳定位", "請先載入影片")
            return
        try:
            if self.player.is_playing():
                self.player.pause()
                self.play_btn.configure(text="▶ 播放")
        except Exception:
            pass
        try:
            frame_path = self._capture_current_frame()
        except Exception as e:
            messagebox.showerror("截圖失敗", str(e))
            return
        ov = self.text_overlays[idx]
        def on_confirm(coords):
            x, y = coords
            ov["pos_mode"] = "absolute"
            ov["x_px"] = x
            ov["y_px"] = y
            self._render_text_overlay_list()
        TextDragDialog(self, frame_path, ov, on_confirm)

    # ----------------- 輸出 -----------------

    def _on_export(self):
        if self.media_path is None or self.player is None:
            messagebox.showerror("輸出", "請先載入影片")
            return
        if not self.cuts and not self.apply_speed_var.get() and self.crop_region is None:
            if not messagebox.askyesno(
                "輸出",
                "沒有要套用的剪輯、裁切或變速,等同直接轉檔。\n要繼續?",
            ):
                return
        threading.Thread(target=self._run_export, daemon=True).start()

    def _run_export(self):
        # 開進度遮罩
        overlay_ref = {"ov": None}
        def make_overlay():
            overlay_ref["ov"] = ProgressOverlay(self, "輸出影片", "ffmpeg 處理中,請稍候...")
        self.after(0, make_overlay)
        def set_detail(msg):
            self.after(0, lambda m=msg:
                       overlay_ref["ov"].set_detail(m) if overlay_ref["ov"] else None)
        try:
            self.after(0, lambda: self.export_btn.configure(state="disabled", text="輸出中..."))
            set_detail("準備輸出參數...")
            speed = float(self.speed_var.get().replace("x", "")) if self.apply_speed_var.get() else 1.0
            fmt_key = self.out_format.get()
            ext, codec_args = OUTPUT_FORMATS[fmt_key]
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            # 來源若本身就是先前的輸出檔(位於 output/edited 且檔名帶
            # .edited.<時戳>),就匯出回同一個 output/edited 並剝掉舊時戳,
            # 避免 output/edited/output/edited/ 目錄與 .edited.X.edited.Y 檔名層層巢狀。
            import re as _re
            src = self.media_path
            stem = src.stem
            _m = _re.match(r"^(.*)\.edited\.\d{8}_\d{6}$", stem)
            if (_m and src.parent.name == "edited"
                    and src.parent.parent.name == "output"):
                edited_dir = src.parent
                edited_dir.mkdir(parents=True, exist_ok=True)
                stem = _m.group(1)
            else:
                edited_dir = output_dir(src, "edited")
            out_path = edited_dir / f"{stem}.edited.{stamp}{ext}"
            self.after(0, lambda: self.status_lbl.configure(text=f"開始輸出 → {out_path.name}"))

            # 字幕燒入需要 SRT — 若勾了但沒 SRT 提早報錯
            burn_srt_path = None
            if self.burn_srt_var.get():
                if not self.srt_path or not Path(self.srt_path).exists():
                    raise RuntimeError("勾了「字幕燒入畫面」但沒載入 SRT")
                # 若 srt_blocks 跟原檔不同(用戶編輯過或剪輯預覽過),
                # 把當前 srt_blocks 序列化到 temp file 給 ffmpeg 用
                if getattr(self, "_srt_dirty", False) or self.cut_preview_active:
                    import tempfile
                    tmp_srt = Path(tempfile.gettempdir()) / "srt_v2_burnin.srt"
                    # 燒入要用「原片時間軸」的 SRT(尚未套 cuts/speed),用 original_srt_blocks 若有
                    blocks_for_burn = (self.original_srt_blocks
                                        if self.cut_preview_active and self.original_srt_blocks
                                        else self.srt_blocks)
                    tmp_srt.write_text(serialize_srt(blocks_for_burn), encoding="utf-8")
                    burn_srt_path = tmp_srt
                else:
                    burn_srt_path = Path(self.srt_path)

            audio_opts = {
                "mute": self.mute_var.get(),
                "main_volume": float(self.audio_vol_var.get()),
                "bg_clips": list(self.bg_music_clips),  # 每筆 {path, start_sec, end_sec, volume}
            }
            set_detail("執行 ffmpeg(視訊長度越長越久,請耐心等)...")
            burn_style = SUBTITLE_STYLES.get(self.burn_style_var.get(),
                                              SUBTITLE_STYLES["白字黑邊(預設)"])
            burn_size = SUBTITLE_SIZES.get(self.burn_size_var.get(), 24)
            sec_scale = SUBTITLE_SECONDARY_SCALES.get(
                self.burn_size_secondary_var.get(), 0.5)
            burn_secondary_size = max(10, int(burn_size * sec_scale))
            sec_srt = self.secondary_srt_path if (
                self.burn_srt_var.get() and self.secondary_srt_path and
                Path(self.secondary_srt_path).exists()
            ) else None
            self._ffmpeg_export(
                in_path=self.media_path,
                out_path=out_path,
                cuts=self.cuts,
                speed=speed,
                crop=self.crop_region,
                overlays=list(self.text_overlays),
                image_overlays=list(self.image_overlays),
                burn_srt=burn_srt_path,
                burn_style=burn_style,
                burn_size=burn_size,
                burn_secondary_size=burn_secondary_size,
                burn_srt_secondary=sec_srt,
                audio_opts=audio_opts,
                fade_in=self.fade_in_var.get(),
                fade_out=self.fade_out_var.get(),
                codec_args=codec_args,
                # 必須用 in_path(= self.media_path)的真實時長。剪輯預覽模式下
                # player 載入的是預覽檔,self.player.duration 會是剪輯後的較短
                # 時長,拿來算 keeps 會誤判 → 匯出未剪輯內容。
                total_dur=get_media_duration(self.media_path),
            )

            set_detail("收尾...")
            # 同步輸出新 SRT
            if self.export_srt_var.get() and self.srt_blocks:
                # 剪輯預覽模式下 self.srt_blocks 已是「剪輯後」字幕;要從原始
                # 字幕重新套 cuts/speed,否則 cuts 會被重複套用。
                srt_src = (self.original_srt_blocks
                           if self.cut_preview_active and self.original_srt_blocks
                           else self.srt_blocks)
                new_blocks = self._apply_edits_to_srt(srt_src, self.cuts, speed)
                new_srt = serialize_srt(new_blocks)
                srt_out = out_path.with_suffix(".srt")
                srt_out.write_text(new_srt, encoding="utf-8")
                self.after(0, lambda: self.status_lbl.configure(
                    text=f"✓ 影片 + SRT 輸出完成 → output/edited/"))
            else:
                self.after(0, lambda: self.status_lbl.configure(
                    text=f"✓ 影片輸出完成 → output/edited/"))

            self.after(0, lambda: messagebox.showinfo(
                "完成", f"輸出完成\n\n{out_path}"))
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self.after(0, lambda: messagebox.showerror("輸出失敗", f"{e}\n\n{tb[-500:]}"))
            self.after(0, lambda: self.status_lbl.configure(text=f"❌ {e}"))
        finally:
            self.after(0, lambda: self.export_btn.configure(state="normal", text="開始輸出 ▸"))
            self.after(0, lambda: overlay_ref["ov"].done() if overlay_ref["ov"] else None)

    @staticmethod
    def _esc_ffpath(p: Path | str) -> str:
        """ffmpeg filter 參數中的路徑要把 : 跳脫掉(它是 option separator)。"""
        s = str(p).replace("\\", "/")
        s = s.replace(":", "\\:")
        return s

    def _build_drawtext_filters(self, overlays: list, tmp_dir: Path) -> list[str]:
        """每個 overlay 生成一個 drawtext=... filter 字串。
        - 文字 + 字型檔都 copy 到 tmp_dir 用 ASCII 短檔名,filter 只用 basename
          (徹底避開 Windows `C:` drive letter 跟 libavfilter escape 地獄)
        - 位置:preset 用 ffmpeg expression / absolute 用像素值
        - 時間:`enable='between(t,A,B)'` 控制顯示區間
        """
        import shutil as _sh
        out = []
        for i, ov in enumerate(overlays):
            # 文字寫到 tmp_dir
            txt_file = tmp_dir / f"overlay_{i:02d}.txt"
            txt_file.write_text(ov["text"], encoding="utf-8")
            # 字型也 copy 到 tmp_dir 用短檔名
            src_font = Path(ov["font_path"])
            local_font = tmp_dir / f"font_{i:02d}{src_font.suffix}"
            if not local_font.exists():
                try:
                    _sh.copyfile(str(src_font), str(local_font))
                except Exception:
                    # 字型 copy 失敗 → 退回原路徑 + 老 escape
                    local_font = None
            # 位置
            if ov.get("pos_mode") == "absolute":
                x_val = str(ov.get("x_px", 0))
                y_val = str(ov.get("y_px", 0))
            else:
                x_val, y_val = ov.get("pos", TEXT_POSITIONS["底部中央"])
            font_arg = local_font.name if local_font else self._esc_ffpath(src_font)
            parts = [
                f"textfile={txt_file.name}",  # cwd=tmp_dir,用 basename
                f"fontfile={font_arg}",
                f"fontcolor={ov['color']}",
                f"fontsize={ov['size']}",
                f"x={x_val}",
                f"y={y_val}",
            ]
            if ov.get("border"):
                parts.append("borderw=2")
                bc = ov.get("border_color") or "black"
                parts.append(f"bordercolor={bc}")
            # 時間區間
            s, e = ov.get("start_sec"), ov.get("end_sec")
            if s is not None or e is not None:
                a = s if s is not None else 0.0
                b = e if e is not None else 1e9
                parts.append(f"enable='between(t,{a},{b})'")
            out.append("drawtext=" + ":".join(parts))
        return out

    def _ffmpeg_export(self, in_path: Path, out_path: Path,
                        cuts: list, speed: float, crop, overlays: list,
                        burn_srt: Path | None, burn_style: dict | None,
                        burn_size: int = 24,
                        burn_secondary_size: int = 12,
                        burn_srt_secondary: Path | None = None,
                        image_overlays: list | None = None,
                        audio_opts: dict | None = None,
                        fade_in: bool = False, fade_out: bool = False,
                        codec_args: list = None, total_dur: float = 0):
        """ffmpeg 輸出 pipeline:
        - 有 cuts:用 stream-copy 切出保留片段 → concat
        - 有 crop / overlays / speed / burn_srt:filter 階段 re-encode(必須)
        - filter chain 順序:crop → subtitles(燒入)→ drawtext → setpts
        - audio_opts:{mute, main_volume, bg_music, bg_volume}
        """
        ffmpeg = get_ffmpeg()
        ao = audio_opts or {}
        mute = ao.get("mute", False)
        main_vol = ao.get("main_volume", 1.0)
        bg_clips = [c for c in ao.get("bg_clips", []) if Path(c.get("path", "")).exists()]
        has_bg = bool(bg_clips)
        img_overlays = [o for o in (image_overlays or [])
                        if Path(o.get("path", "")).exists()]
        has_img = bool(img_overlays)
        has_audio_mod = mute or abs(main_vol - 1.0) > 0.01 or has_bg
        need_filter = (bool(crop) or bool(overlays) or
                        abs(speed - 1.0) > 0.001 or bool(burn_srt) or
                        has_audio_mod or has_img)

        # 計算「保留片段」(刪除片段的反集)
        keeps = []
        cursor = 0.0
        for cs, ce in cuts:
            if cs > cursor + 0.05:
                keeps.append((cursor, cs))
            cursor = max(cursor, ce)
        if cursor < total_dur - 0.05:
            keeps.append((cursor, total_dur))
        if not keeps:
            # 沒 cuts → 整段都保留
            keeps = [(0.0, total_dur)]

        import tempfile, hashlib
        tmp_dir = Path(tempfile.gettempdir()) / f"srt_v2_{hashlib.md5(str(in_path).encode()).hexdigest()[:8]}"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Phase 1:把保留片段接成一個乾淨檔
            if len(keeps) == 1 and keeps[0] == (0.0, total_dur):
                # 沒切 → 直接拿原檔
                concat_input = in_path
                concat_mode = "file"  # 單檔
            else:
                # 用 select 濾鏡一次過保留多片段(frame-accurate),取代會掉幀的
                # stream-copy 切片 + concat;產出單一乾淨檔,下游當單檔處理。
                cut_file = tmp_dir / "cut_joined.mp4"
                self._ffmpeg_cut_join(in_path, keeps, cut_file,
                                      preset="medium", crf=18)
                concat_input = cut_file
                concat_mode = "file"

            # Phase 3:最終輸出 — 視是否需要 filter 決定一步到位 or 兩階段
            if not need_filter:
                # 不用 filter → 直接 concat / 直接編碼
                if concat_mode == "concat":
                    cmd = [
                        ffmpeg, "-y",
                        "-f", "concat", "-safe", "0",
                        "-i", str(concat_input),
                        *codec_args,
                        str(out_path),
                    ]
                else:
                    cmd = [
                        ffmpeg, "-y",
                        "-i", str(concat_input),
                        *codec_args,
                        str(out_path),
                    ]
            else:
                # 要套 filter → 先 concat 到中間 mp4,再 filter pass
                if concat_mode == "concat":
                    mid = tmp_dir / "concat_mid.mp4"
                    cmd = [
                        ffmpeg, "-y",
                        "-f", "concat", "-safe", "0",
                        "-i", str(concat_input),
                        "-c", "copy",
                        str(mid),
                    ]
                    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(tmp_dir),
                                       encoding="utf-8", errors="replace")
                    if r.returncode != 0:
                        raise RuntimeError(f"concat 失敗: {r.stderr[-300:]}")
                    filter_input = mid
                else:
                    filter_input = concat_input

                # 組 video filter chain — crop → subtitles(燒入)→ drawtext... → setpts
                vf_parts = []
                if crop:
                    cx, cy, cw, ch, _, _ = crop
                    vf_parts.append(f"crop={cw}:{ch}:{cx}:{cy}")
                if burn_srt:
                    # 字幕 copy 到 tmp_dir 用 ASCII 檔名,filter 只給 relative 檔名
                    # 完全避開 Windows `C:` drive letter 跟 libavfilter 兩層 escape 地獄
                    import shutil as _sh
                    bs = burn_style or SUBTITLE_STYLES["白字黑邊(預設)"]
                    outline_w = 1 if bs.get("border", True) else 0

                    def _build_ass_style(fontsize: int, marginv: int) -> str:
                        return ("FontName=Microsoft JhengHei"
                                f"\\,Fontsize={fontsize}"
                                f"\\,PrimaryColour={bs['primary']}"
                                f"\\,OutlineColour={bs['outline']}"
                                "\\,BorderStyle=1"
                                f"\\,Outline={outline_w}"
                                "\\,Shadow=0"
                                f"\\,MarginV={marginv}"
                                "\\,Alignment=2")  # bottom-center

                    # 字級:主依使用者選擇,第二依使用者選擇相對比例(小=½, 大=¾)
                    primary_size = burn_size or 24
                    secondary_size = burn_secondary_size or max(10, primary_size // 2)
                    # MarginV 算法:libass 以 baseline 計距,字會比 fontsize 還高(含上下 padding)
                    #   secondary MarginV = 15(貼底)
                    #   primary MarginV = secondary_marginv + secondary_size × 1.6
                    #   1.6 倍係數保留行間,即使主字幕 wrap 成 2 行也不會壓到第二字幕的 top
                    secondary_marginv = 15
                    primary_marginv = (
                        secondary_marginv + int(secondary_size * 1.6)
                    ) if burn_srt_secondary else 20

                    # 主字幕(中)
                    local_srt = tmp_dir / "burn_sub.srt"
                    try:
                        _sh.copyfile(str(burn_srt), str(local_srt))
                    except Exception as ex:
                        raise RuntimeError(f"複製主字幕到 tmp 失敗: {ex}")
                    vf_parts.append(
                        f"subtitles=burn_sub.srt:force_style="
                        f"{_build_ass_style(fontsize=primary_size, marginv=primary_marginv)}")

                    # 第二字幕(半高小字)— 接在主字幕底下,幾乎零間距
                    if burn_srt_secondary and Path(burn_srt_secondary).exists():
                        local_srt2 = tmp_dir / "burn_sub2.srt"
                        try:
                            _sh.copyfile(str(burn_srt_secondary), str(local_srt2))
                        except Exception as ex:
                            raise RuntimeError(f"複製第二字幕到 tmp 失敗: {ex}")
                        vf_parts.append(
                            f"subtitles=burn_sub2.srt:force_style="
                            f"{_build_ass_style(fontsize=secondary_size, marginv=secondary_marginv)}")
                if overlays:
                    vf_parts.extend(self._build_drawtext_filters(overlays, tmp_dir))
                if abs(speed - 1.0) > 0.001:
                    vf_parts.append(f"setpts=PTS/{speed}")
                # 淡入淡出(套在最後,以最終 duration 為基準)
                eff_dur_for_fade = sum(min(ed, total_dur) - max(0, cs)
                                        for cs, ed in keeps) / max(speed, 0.01) \
                                   if keeps else (total_dur / max(speed, 0.01))
                if fade_in:
                    vf_parts.append("fade=t=in:st=0:d=1.5")
                if fade_out and eff_dur_for_fade > 2:
                    vf_parts.append(f"fade=t=out:st={eff_dur_for_fade-1.5:.2f}:d=1.5")

                # 音訊處理:多個 bg clip 或 image overlay → filter_complex / 純 mute → -an / 否則 -filter:a
                if has_bg or has_img:
                    # filter_complex 完整 pipeline:images + bg music
                    cmd = [ffmpeg, "-y", "-i", str(filter_input)]
                    # image inputs(每張 -loop 1)
                    img_input_offset = 1
                    for io in img_overlays:
                        cmd += ["-loop", "1", "-i", str(io["path"])]
                    # bg music inputs
                    bg_input_offset = 1 + len(img_overlays)
                    for clip in bg_clips:
                        cmd += ["-stream_loop", "-1", "-i", str(clip["path"])]
                    fc_parts = []
                    # video chain step 1:crop/subtitles/drawtext/setpts
                    cur_v = "[0:v]"
                    if vf_parts:
                        fc_parts.append(f"{cur_v}{','.join(vf_parts)}[vbase]")
                        cur_v = "[vbase]"
                    # video chain step 2:逐個 image overlay
                    for i, io in enumerate(img_overlays):
                        img_idx = img_input_offset + i
                        scale = float(io.get("scale", 0.2))
                        opacity = float(io.get("opacity", 1.0))
                        img_chain = [f"scale=iw*{scale}:ih*{scale}"]
                        if opacity < 1.0:
                            img_chain += ["format=rgba",
                                          f"colorchannelmixer=aa={opacity}"]
                        img_label = f"[img{i}]"
                        fc_parts.append(f"[{img_idx}:v]{','.join(img_chain)}{img_label}")
                        # overlay position
                        if io.get("pos_mode") == "absolute":
                            x_expr = str(io.get("x_px", 0))
                            y_expr = str(io.get("y_px", 0))
                        else:
                            # preset 位置 — 同 drawtext 用 (W,H,w,h) 計算
                            pos = io.get("pos", TEXT_POSITIONS["底部右側"])
                            x_expr = pos[0].replace("text_w", "w").replace("text_h", "h")
                            y_expr = pos[1].replace("text_w", "w").replace("text_h", "h")
                        s = io.get("start_sec")
                        e = io.get("end_sec")
                        enable_str = ""
                        if s is not None or e is not None:
                            a = s if s is not None else 0.0
                            b = e if e is not None else 1e9
                            enable_str = f":enable='between(t,{a},{b})'"
                        nxt = f"[v{i+1}]"
                        fc_parts.append(
                            f"{cur_v}{img_label}overlay=x={x_expr}:y={y_expr}{enable_str}{nxt}")
                        cur_v = nxt
                    video_out = cur_v if cur_v != "[0:v]" else "[0:v]"
                    if cur_v == "[0:v]":
                        # 沒套任何 video filter 也沒 overlay → 直通
                        video_out = "[0:v]"
                    # 算出影片實際時長(套 cuts 後)
                    eff_dur = sum(min(ed, total_dur) - max(0, cs)
                                   for cs, ed in keeps) if keeps else total_dur

                    audio_to_mix = []
                    # 主音軌(可選 mute / 變速)
                    if not mute:
                        main_a_chain = [f"volume={main_vol}"]
                        if abs(speed - 1.0) > 0.001:
                            main_a_chain.append(self._atempo_chain(speed))
                        fc_parts.append(f"[0:a]{','.join(main_a_chain)}[ma]")
                        audio_to_mix.append("[ma]")
                    # 每個 bg clip:trim 到 [start, end] + delay + 音量
                    for i, clip in enumerate(bg_clips):
                        ff_input_idx = bg_input_offset + i  # 因為前面 img inputs
                        s = clip.get("start_sec")
                        e = clip.get("end_sec")
                        vol = clip.get("volume", 0.3)
                        st = max(0.0, s) if s is not None else 0.0
                        end_t = e if e is not None else eff_dur
                        dur_t = max(0.1, min(end_t, eff_dur) - st)
                        delay_ms = int(st * 1000)
                        chain = [f"volume={vol}", f"atrim=duration={dur_t}"]
                        if delay_ms > 0:
                            chain.append(f"adelay={delay_ms}|{delay_ms}")
                        if abs(speed - 1.0) > 0.001:
                            chain.append(self._atempo_chain(speed))
                        label = f"[bg{i}a]"
                        fc_parts.append(f"[{ff_input_idx}:a]{','.join(chain)}{label}")
                        audio_to_mix.append(label)

                    if len(audio_to_mix) == 0:
                        cmd += ["-filter_complex", ";".join(fc_parts),
                                "-map", video_out, "-an"]
                    elif len(audio_to_mix) == 1:
                        # 單音軌(只有 image overlay 沒 bg)→ 直接 map,不需要 amix
                        cmd += ["-filter_complex", ";".join(fc_parts),
                                "-map", video_out, "-map", audio_to_mix[0]]
                    else:
                        fc_parts.append(
                            f"{''.join(audio_to_mix)}amix=inputs={len(audio_to_mix)}"
                            f":duration=first:dropout_transition=0[aout]"
                        )
                        cmd += ["-filter_complex", ";".join(fc_parts),
                                "-map", video_out, "-map", "[aout]"]
                    cmd += [*codec_args, str(out_path)]
                elif mute:
                    # 純靜音:-an + 視訊 filter
                    cmd = [ffmpeg, "-y", "-i", str(filter_input)]
                    if vf_parts:
                        cmd += ["-vf", ",".join(vf_parts)]
                    cmd += ["-an", *codec_args, str(out_path)]
                else:
                    # 簡單路徑:單音軌調整音量 + 速度
                    cmd = [ffmpeg, "-y", "-i", str(filter_input)]
                    if vf_parts:
                        cmd += ["-vf", ",".join(vf_parts)]
                    a_chain = []
                    if abs(main_vol - 1.0) > 0.01:
                        a_chain.append(f"volume={main_vol}")
                    if abs(speed - 1.0) > 0.001:
                        a_chain.append(self._atempo_chain(speed))
                    if a_chain:
                        cmd += ["-filter:a", ",".join(a_chain)]
                    cmd += [*codec_args, str(out_path)]

            try:
                print(f"[ffmpeg] final cmd:\n  {' '.join(repr(c) for c in cmd)}", flush=True)
            except UnicodeEncodeError:
                # pythonw 模式 stdout 是 cp1252,無法印中文 — 略過
                pass
            r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(tmp_dir),
                                encoding="utf-8", errors="replace")
            if r.returncode != 0:
                try:
                    print(f"[ffmpeg] STDERR (full):\n{r.stderr}", flush=True)
                except UnicodeEncodeError:
                    pass
                raise RuntimeError(f"ffmpeg 輸出失敗:\n\n{r.stderr[-2000:]}")
        finally:
            try:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

    def _atempo_chain(self, speed: float) -> str:
        """ffmpeg atempo 單次只接受 0.5~2.0,要超出範圍要串接。"""
        # 0.5~2.0 直接 single
        if 0.5 <= speed <= 2.0:
            return f"atempo={speed}"
        # 超出範圍 chain。e.g. 3x = 1.5 * 2.0
        chain = []
        remaining = speed
        while remaining > 2.0:
            chain.append("atempo=2.0")
            remaining /= 2.0
        while remaining < 0.5:
            chain.append("atempo=0.5")
            remaining /= 0.5
        chain.append(f"atempo={remaining}")
        return ",".join(chain)

    def _apply_edits_to_srt(self, blocks: list, cuts: list, speed: float) -> list:
        """把剪輯 + 變速套用到 SRT。
        步驟:
        1. 標記每個 block 是否完全落在某個 cut 內 → 整 block 刪除
        2. 對殘存 block,計算「跨越了幾個 cut」累積減去的秒數 → 平移
        3. 變速 → 全部 timestamps 除以 speed
        """
        if not cuts and abs(speed - 1.0) < 0.001:
            return blocks  # 沒改

        new = []
        for idx, st, ed, lines in blocks:
            # 整 block 落在 cut 內 → 刪
            skip = False
            for cs, ce in cuts:
                if st >= cs and ed <= ce:
                    skip = True
                    break
            if skip:
                continue

            # 算這個 block 的 st/ed 累積被前面 cut 砍掉多少秒
            offset_st = sum(min(ce, st) - cs for cs, ce in cuts if cs < st)
            offset_ed = sum(min(ce, ed) - cs for cs, ce in cuts if cs < ed)
            new_st = max(0.0, st - offset_st)
            new_ed = max(new_st + 0.1, ed - offset_ed)

            # 變速 → /speed
            new_st /= speed
            new_ed /= speed

            new.append((idx, new_st, new_ed, lines))
        return new

    # ----------------- 文字疊加 marquee preview -----------------

    def _color_hex_to_int(self, c: str) -> int:
        """'#fde047' / 'white' → 0xRRGGBB int (VLC marquee Color)。"""
        if not c:
            return 0xFFFFFF
        if c.startswith("#"):
            hex_part = c[1:7]
            try:
                return int(hex_part, 16)
            except ValueError:
                return 0xFFFFFF
        named = {
            "white": 0xFFFFFF, "black": 0x000000,
            "red": 0xFF0000, "yellow": 0xFFFF00,
            "blue": 0x0000FF, "green": 0x00FF00,
            "cyan": 0x00FFFF, "magenta": 0xFF00FF,
        }
        return named.get(c.lower(), 0xFFFFFF)

    # preset 名稱 → (VLC marquee Position anchor, x_offset, y_offset)
    _MARQUEE_ANCHOR = {
        "頂部中央": (4, 0, 30),
        "頂部左側": (5, 30, 30),
        "頂部右側": (6, 30, 30),
        "畫面中央": (0, 0, 0),
        "底部中央": (8, 0, 30),
        "底部左側": (9, 30, 30),
        "底部右側": (10, 30, 30),
    }

    def _update_overlay_preview(self, current_time: float):
        """VLC marquee filter 一次只能顯示一個文字 → 找當前時間 active 的第一筆 overlay 顯示。"""
        if self.player is None or self.player.player is None:
            return
        try:
            import vlc
        except Exception:
            return
        p = self.player.player

        active = None
        for ov in self.text_overlays:
            s = ov.get("start_sec")
            e = ov.get("end_sec")
            if s is not None and current_time < s:
                continue
            if e is not None and current_time > e:
                continue
            active = ov
            break

        if active is None:
            try:
                p.video_set_marquee_int(vlc.VideoMarqueeOption.Enable, 0)
            except Exception:
                pass
            return

        try:
            p.video_set_marquee_int(vlc.VideoMarqueeOption.Enable, 1)
            p.video_set_marquee_string(vlc.VideoMarqueeOption.Text, active["text"])
            p.video_set_marquee_int(vlc.VideoMarqueeOption.Size, int(active["size"]))
            p.video_set_marquee_int(vlc.VideoMarqueeOption.Color,
                                     self._color_hex_to_int(active.get("color", "white")))
            p.video_set_marquee_int(vlc.VideoMarqueeOption.Opacity, 255)
            p.video_set_marquee_int(vlc.VideoMarqueeOption.Timeout, 0)
            p.video_set_marquee_int(vlc.VideoMarqueeOption.Refresh, 100)
            if active.get("pos_mode") == "absolute":
                # top-left anchor + 絕對 x/y
                p.video_set_marquee_int(vlc.VideoMarqueeOption.Position, 5)
                p.video_set_marquee_int(vlc.VideoMarqueeOption.X, int(active.get("x_px", 0)))
                p.video_set_marquee_int(vlc.VideoMarqueeOption.Y, int(active.get("y_px", 0)))
            else:
                anchor, ox, oy = self._MARQUEE_ANCHOR.get(
                    active.get("pos_name", "底部中央"), (8, 0, 30))
                p.video_set_marquee_int(vlc.VideoMarqueeOption.Position, anchor)
                p.video_set_marquee_int(vlc.VideoMarqueeOption.X, ox)
                p.video_set_marquee_int(vlc.VideoMarqueeOption.Y, oy)
        except Exception:
            pass

    # ----------------- 圖片疊加 logo preview -----------------

    def _get_scaled_image(self, overlay) -> Path | None:
        """依 overlay 的 scale 把圖片預縮放成暫存 PNG,回傳路徑(快取)。

        VLC logo filter 不能縮放圖片 → 先用 Pillow 縮好再交給 logo;
        同時統一轉成 ASCII 檔名的 PNG,避開中文路徑 / 格式問題。
        """
        try:
            src = Path(overlay["path"])
        except Exception:
            return None
        if not src.exists():
            return None
        scale = float(overlay.get("scale", 1.0) or 1.0)
        scale = max(0.01, min(8.0, scale))
        key = (str(src), round(scale, 3))
        cached = self._img_logo_cache.get(key)
        if cached and Path(cached).exists():
            return Path(cached)
        try:
            from PIL import Image
            img = Image.open(src).convert("RGBA")
            nw = max(1, int(round(img.width * scale)))
            nh = max(1, int(round(img.height * scale)))
            img = img.resize((nw, nh), Image.LANCZOS)
            name = "logo_" + hashlib.md5(repr(key).encode()).hexdigest()[:14] + ".png"
            out = IMG_CACHE_DIR / name
            IMG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            img.save(out)
            self._img_logo_cache[key] = out
            return out
        except Exception:
            return None

    def _update_image_overlay_preview(self, current_time: float):
        """VLC logo filter 即時預覽圖片疊加(對應文字的 marquee)。

        logo filter 一次只能顯示一張 → 找當前時間 active 的第一筆。
        簽章沒變就不重設,避免每個 tick 重載圖片。
        """
        if self.player is None or self.player.player is None:
            return
        try:
            import vlc
        except Exception:
            return
        p = self.player.player

        active = None
        for ov in self.image_overlays:
            s = ov.get("start_sec")
            e = ov.get("end_sec")
            if s is not None and current_time < s:
                continue
            if e is not None and current_time > e:
                continue
            active = ov
            break

        if active is None:
            try:
                p.video_set_logo_int(vlc.VideoLogoOption.logo_enable, 0)
            except Exception:
                pass
            self._img_logo_sig = None
            return

        sig = (str(active.get("path")), active.get("scale"), active.get("opacity"),
               active.get("pos_mode"), active.get("pos_name"),
               active.get("x_px"), active.get("y_px"))
        if sig == self._img_logo_sig:
            return

        logo_path = self._get_scaled_image(active)
        if logo_path is None:
            return
        try:
            p.video_set_logo_string(vlc.VideoLogoOption.logo_file, str(logo_path))
            opacity = max(0.0, min(1.0, float(active.get("opacity", 1.0) or 1.0)))
            p.video_set_logo_int(vlc.VideoLogoOption.logo_opacity, int(opacity * 255))
            if active.get("pos_mode") == "absolute":
                p.video_set_logo_int(vlc.VideoLogoOption.logo_position, -1)
                p.video_set_logo_int(vlc.VideoLogoOption.logo_x, int(active.get("x_px", 0)))
                p.video_set_logo_int(vlc.VideoLogoOption.logo_y, int(active.get("y_px", 0)))
            else:
                anchor, ox, oy = self._MARQUEE_ANCHOR.get(
                    active.get("pos_name", "底部中央"), (8, 0, 30))
                p.video_set_logo_int(vlc.VideoLogoOption.logo_position, anchor)
                p.video_set_logo_int(vlc.VideoLogoOption.logo_x, ox)
                p.video_set_logo_int(vlc.VideoLogoOption.logo_y, oy)
            p.video_set_logo_int(vlc.VideoLogoOption.logo_enable, 1)
            self._img_logo_sig = sig
        except Exception:
            pass

    # ----------------- tick:更新時間 / 高亮字幕 / overlay preview -----------------

    def _tick(self):
        try:
            if self.player and not self._scrub_dragging:
                t = self.player.get_time_sec()
                self._update_time_display(t)
                self.scrubber.set(min(t, self.player.duration))
                self._highlight_current_block(t)
                self._update_overlay_preview(t)
                self._update_image_overlay_preview(t)
                self._update_timeline_indicator(t)
                # 影片播畢 → 按鈕還原「播放」,使用者可再按一次重播
                if self.player.is_ended():
                    self.play_btn.configure(text="▶ 播放")
        except Exception:
            pass
        finally:
            self.after(200, self._tick)

    def _update_time_display(self, t: float):
        if self.player is None:
            return
        dur = self.player.duration
        def fmt(s):
            m = int(s // 60); sec = int(s % 60)
            return f"{m:02d}:{sec:02d}"
        self.time_lbl.configure(text=f"{fmt(t)} / {fmt(dur)}")

    def _highlight_current_block(self, t: float):
        """找當前播放秒數對應的 SRT block,高亮 + scroll into view。"""
        if not self.srt_blocks or not hasattr(self, "srt_block_widgets"):
            return
        # binary search
        active_idx = None
        for i, w in enumerate(self.srt_block_widgets):
            if w["start"] <= t <= w["end"] + 0.3:
                active_idx = i
                break
        if active_idx == self.current_active_block_idx:
            return
        # 之前的取消高亮
        if self.current_active_block_idx is not None and \
                self.current_active_block_idx < len(self.srt_block_widgets):
            old = self.srt_block_widgets[self.current_active_block_idx]
            try:
                old["frame"].configure(fg_color=("#ffffff", "#161b22"),
                                       border_color=("#e2e8f0", "#262b33"))
                old["text"].configure(font=ctk.CTkFont(size=11, weight="normal"))
            except Exception:
                pass
        # 新的高亮
        if active_idx is not None:
            new = self.srt_block_widgets[active_idx]
            try:
                new["frame"].configure(fg_color=("#dbeafe", "#0c2447"),
                                       border_color=("#3b82f6", "#3b82f6"))
                new["text"].configure(font=ctk.CTkFont(size=11, weight="bold"))
            except Exception:
                pass
        self.current_active_block_idx = active_idx


# ============================================================
# main
# ============================================================

if __name__ == "__main__":
    try:
        import vlc  # 確認 python-vlc 跟 libvlc 都在
    except Exception as e:
        import tkinter as tk
        from tkinter import messagebox as mb
        root = tk.Tk(); root.withdraw()
        mb.showerror(
            "缺 VLC",
            f"python-vlc 載入失敗:\n{e}\n\n"
            "請先安裝 VLC media player:\n"
            "https://www.videolan.org/vlc/\n\n"
            "安裝後重新啟動本程式。"
        )
        sys.exit(1)

    app = SRTToolV2()
    app.mainloop()
