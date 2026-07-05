#!/usr/bin/env python3
"""
PY_auto_translate.py - 即時語音翻譯系統
支援：YouTube/TED URL (有/無字幕) | 系統音訊 (Teams/Webex/小魚 等)
用法：python PY_auto_translate.py [--help]
"""

import sys
import os
import time
import threading
import queue
import asyncio
import tempfile
import subprocess
import re
import numpy as np
import tkinter as tk
from tkinter import ttk

# ══════════════════════════════════════════════════════════════════════════════
# 套件載入
# ══════════════════════════════════════════════════════════════════════════════
try:
    import sounddevice as sd
    import soundfile as sf
    AUDIO_OK = True
except Exception as e:
    AUDIO_OK = False
    print(f"⚠ 音訊套件未就緒: {e}")

try:
    from faster_whisper import WhisperModel
    WHISPER_OK = True
except Exception:
    WHISPER_OK = False
    print("⚠ faster-whisper 未安裝")

try:
    from deep_translator import GoogleTranslator
    TRANS_OK = True
except Exception:
    TRANS_OK = False
    print("⚠ deep-translator 未安裝")

try:
    from dotenv import load_dotenv
    from google import genai as _genai
    load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
    _gemini_key = os.getenv("GEMINI_API_KEY", "")
    AI_OK = bool(_gemini_key and len(_gemini_key) > 10)
    _gemini_client = _genai.Client(api_key=_gemini_key) if AI_OK else None
except Exception:
    AI_OK = False
    _gemini_key = ""
    _gemini_client = None

TTS_OK = True  # 用 PowerShell 系統語音，無需額外套件

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    TRANSCRIPT_OK = True
except Exception:
    TRANSCRIPT_OK = False

try:
    import yt_dlp
    YTDLP_OK = True
except Exception:
    YTDLP_OK = False

# ══════════════════════════════════════════════════════════════════════════════
# 常數與全域設定
# ══════════════════════════════════════════════════════════════════════════════
SAMPLE_RATE    = 16000
ENERGY_THRESH  = 0.008          # VAD 能量閾值
SILENCE_SEC    = 0.6            # 靜音超過此秒數 → 句子結束
MIN_SPEECH_SEC = 0.4            # 最短有效語音片段
SENTENCE_TIMEOUT = 12.0         # 強制送出上限（秒）
TTS_VOICE_A    = "Microsoft Hanhan Desktop - Chinese (Taiwan)"  # 講者 A（預設）
TTS_VOICE_B    = TTS_VOICE_A                                     # 講者 B（啟動時嘗試找第二聲音）

subtitle_queue        = queue.Queue()
tts_queue             = queue.Queue()
sentence_output_queue = queue.Queue()  # chunk 模式 → 主視窗
sentence_queue        = queue.Queue()  # 整句模式 → 第二視窗
stop_event            = threading.Event()

# 翻譯輸出資料夾（每次啟動建一個新檔案）
_OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "translation_output")
_output_file = None


def _start_output_file(settings: dict):
    """建立新的輸出檔，寫入 GUI 設定標頭"""
    global _output_file
    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(_OUTPUT_DIR, f"trans_{ts}.txt")
    _output_file = open(path, "w", encoding="utf-8")
    _output_file.write("═" * 50 + "\n")
    _output_file.write("  翻譯設定\n")
    _output_file.write("═" * 50 + "\n")
    for k, v in settings.items():
        _output_file.write(f"  {k}: {v}\n")
    _output_file.write("═" * 50 + "\n\n")
    _output_file.flush()
    print(f"[輸出] 翻譯存檔：{path}")


def _close_output_file():
    global _output_file
    if _output_file:
        _output_file.close()
        _output_file = None


def _write_output(en: str, zh: str):
    if _output_file:
        try:
            ts = time.strftime("%H:%M:%S")
            _output_file.write(f"[{ts}]\nEN: {en}\nZH: {zh}\n{'─'*50}\n")
            _output_file.flush()
        except Exception:
            pass

_whisper_model   = None
_sentence_buffer = ""
_sentence_buf_t  = 0.0
_speaker_context = []  # [(label, zh_segment), ...] 保留最近 4 輪語者記憶

# Gemini rate limit 保護：免費版 15 req/min，最小間隔 5 秒
_gemini_last_call = 0.0
_GEMINI_MIN_INTERVAL = 8.0  # 秒（免費版 15req/min，保守用 8s 間隔）


def _init_tts_voices():
    """背景執行緒：找第二個 SAPI 聲音給講者 B（不阻塞啟動）"""
    global TTS_VOICE_B
    try:
        ps = ("Add-Type -AssemblyName System.Speech; "
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }")
        r = subprocess.run(['powershell', '-WindowStyle', 'Hidden', '-Command', ps],
                           capture_output=True, text=True, timeout=8,
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        voices = [v.strip() for v in r.stdout.strip().splitlines() if v.strip()]
        print(f"[TTS] 可用聲音: {voices}")
        zh_voices = [v for v in voices if 'Chinese' in v or 'Hanhan' in v or 'Zhiwei' in v]
        if len(zh_voices) >= 2:
            TTS_VOICE_B = zh_voices[1]
        print(f"[TTS] 講者A={TTS_VOICE_A}  講者B={TTS_VOICE_B}")
    except Exception as e:
        print(f"[TTS] 聲音探索失敗: {e}")


# 背景執行，不阻塞主程式啟動
threading.Thread(target=_init_tts_voices, daemon=True).start()


# ── 語義判斷：FLUSH / WAIT ────────────────────────────────────────────────────
def semantic_should_flush(buffer: str) -> bool:
    """判斷緩衝區是否語義完整可翻譯"""
    words = buffer.split()
    if len(words) < 6:
        return False
    # 明顯句尾標點 → 直接 FLUSH
    if buffer.rstrip()[-1:] in '.!?。！？':
        return True
    # 字數 >= 20 才問 Gemini（減少 API 呼叫次數，節省 rate limit）
    if len(words) >= 20 and AI_OK and _gemini_client:
        if time.time() - _gemini_last_call < _GEMINI_MIN_INTERVAL:
            return False  # 太頻繁，改用 timeout 機制
        try:
            _gemini_last_call_ref = time.time()
            resp = _gemini_client.models.generate_content(
                model="gemini-2.0-flash",
                contents=(
                    "Reply FLUSH if the following speech text is semantically complete, "
                    "or WAIT if it's mid-sentence. Reply FLUSH or WAIT only:\n\n\"" + buffer[-200:] + "\""
                )
            )
            return "FLUSH" in resp.text.upper()
        except Exception:
            pass
    return False


def process_chunk(en_text: str, mode: str, use_tts: bool,
                  scene: str = "general", diarize: bool = False):
    """依模式處理 Whisper 輸出。

    chunk 模式：Google 翻譯 → 字幕視窗 + 主輸出視窗（快速）
    整句模式：Google 翻譯 → 字幕視窗（即時回饋）
             + 累積後 Gemini → 第二視窗（高品質，較慢）
    """
    global _sentence_buffer, _sentence_buf_t

    # ── 永遠先做快速翻譯，即時更新字幕 ──────────────────────────────────
    zh_fast = translate_text(en_text)
    subtitle_queue.put((en_text, zh_fast))

    if mode == 'chunk':
        sentence_output_queue.put((en_text, zh_fast))
        _write_output(en_text, zh_fast)
        if use_tts:
            tts_queue.put(zh_fast)

    else:
        # 整句模式：累積 → Gemini 語義判斷 → 輸出到第二視窗
        if not _sentence_buffer:
            _sentence_buf_t = time.time()
        _sentence_buffer += (" " if _sentence_buffer else "") + en_text.strip()

        wc = len(_sentence_buffer.split())
        timed_out = time.time() - _sentence_buf_t >= SENTENCE_TIMEOUT
        too_long = len(_sentence_buffer) > 2000

        if timed_out or too_long or semantic_should_flush(_sentence_buffer):
            complete = _sentence_buffer.strip()
            _sentence_buffer = ""
            reason = "timeout" if timed_out else ("too_long" if too_long else "flush")
            print(f"[翻譯] 觸發={reason} 字數={wc} diarize={diarize}")
            zh = translate_text_ai(complete, scene, diarize=diarize)
            print(f"[翻譯] 結果: {zh[:80]}...")
            sentence_queue.put((complete, zh))   # → 第二視窗
            _write_output(complete, zh)
            if use_tts:
                tts_queue.put(zh)


# ══════════════════════════════════════════════════════════════════════════════
# 工具函數
# ══════════════════════════════════════════════════════════════════════════════
def find_ffmpeg() -> str | None:
    """找到可用的 ffmpeg 路徑（Windows / Linux 通用）"""
    # 先試系統 PATH
    cmd = 'where' if sys.platform == 'win32' else 'which'
    try:
        r = subprocess.run([cmd, 'ffmpeg'], capture_output=True, text=True)
        if r.returncode == 0:
            return 'ffmpeg'
    except FileNotFoundError:
        pass
    # Windows 常見安裝路徑
    candidates = [
        r'C:\Users\morris.lu\AppData\Local\Microsoft\WinGet\Packages'
        r'\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe'
        r'\ffmpeg-8.0-full_build\bin\ffmpeg.exe',
        r'D:\Morris\under_cook\1217_Mindshare_training'
        r'\ffmpeg-8.1-full_build-shared\ffmpeg-8.1-full_build-shared\bin\ffmpeg.exe',
        # WSL 路徑（從 Linux 執行時用）
        '/mnt/c/Users/morris.lu/AppData/Local/Microsoft/WinGet/Packages/'
        'Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/'
        'ffmpeg-8.0-full_build/bin/ffmpeg.exe',
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None

FFMPEG = find_ffmpeg()


def extract_video_id(url: str) -> str | None:
    patterns = [
        r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})',
        r'(?:embed/)([A-Za-z0-9_-]{11})',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 字幕視窗（懸浮置頂）
# ══════════════════════════════════════════════════════════════════════════════
class SubtitleWindow:
    def __init__(self, root: tk.Tk):
        self.win = tk.Toplevel(root)
        self.win.title("翻譯字幕")
        self.win.attributes('-topmost', True)
        self.win.attributes('-alpha', 0.88)
        self.win.configure(bg='black')
        self.win.overrideredirect(True)  # 無邊框

        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        self.win.geometry(f"{sw}x120+0+{sh - 155}")

        self.en_var = tk.StringVar()
        self.zh_var = tk.StringVar()

        tk.Label(self.win, textvariable=self.en_var, fg='#FFDD00', bg='black',
                 font=('Arial', 12), wraplength=sw - 40).pack(pady=(8, 2))
        tk.Label(self.win, textvariable=self.zh_var, fg='white', bg='black',
                 font=('Arial', 20, 'bold'), wraplength=sw - 40).pack(pady=(2, 8))

        # 右鍵關閉
        self.win.bind('<Button-3>', lambda e: self.win.destroy())

    def update(self, en: str, zh: str):
        self.en_var.set(en)
        self.zh_var.set(zh)


class SentenceWindow:
    """整句 AI 翻譯輸出視窗（整句模式專用）"""
    def __init__(self, root: tk.Tk):
        self.win = tk.Toplevel(root)
        self.win.title("整句翻譯（AI 高品質）")
        sw = root.winfo_screenwidth()
        self.win.geometry(f"760x420+{(sw - 760)//2}+80")
        self.win.attributes('-topmost', True)

        tk.Label(self.win, text="整句翻譯（Gemini AI）",
                 font=('Arial', 11, 'bold'), fg='#1a73e8').pack(pady=(6, 0))

        frame = tk.Frame(self.win)
        frame.pack(fill='both', expand=True, padx=6, pady=6)
        sb = tk.Scrollbar(frame)
        sb.pack(side='right', fill='y')
        self.text = tk.Text(frame, wrap='word', yscrollcommand=sb.set,
                            font=('Arial', 12), state='disabled', bg='#fafafa')
        self.text.pack(fill='both', expand=True)
        sb.config(command=self.text.yview)

    def append(self, en: str, zh: str):
        self.text.config(state='normal')
        ts = time.strftime("%H:%M:%S")
        self.text.insert(tk.END, f"[{ts}] EN: {en}\n")
        self.text.insert(tk.END, f"ZH: {zh}\n{'─'*55}\n")
        self.text.see(tk.END)
        self.text.config(state='disabled')

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# 翻譯
# ══════════════════════════════════════════════════════════════════════════════
def translate_text(text: str) -> str:
    """Google 翻譯（逐格模式用）"""
    text = text.strip()
    if not text or not TRANS_OK:
        return "[翻譯套件未安裝]" if not TRANS_OK else ""
    if len(text) > 4900:
        text = text[:4900]
    try:
        return GoogleTranslator(source='en', target='zh-TW').translate(text)
    except Exception as e:
        return f"[翻譯失敗: {e}]"


_SCENE_CONFIG = {
    "youtube": {
        "model": "gemini-2.0-flash",
        "system": (
            "你是即時字幕翻譯員。以下是 YouTube/TED 演講的語音辨識英文片段，"
            "可能有辨識錯誤。請理解語意後翻譯成流暢的繁體中文。"
            "只輸出翻譯，不加解釋。"
        ),
    },
    "teams": {
        "model": "gemini-2.5-flash",
        "system": (
            "你是資深商業口譯員，專精繁體中文台灣商業用語。"
            "以下是商業會議的語音辨識英文片段，可能有辨識錯誤或口語停頓。"
            "請：1) 先修正明顯的辨識錯誤 2) 翻譯成正式、準確的繁體中文商業用語。"
            "使用台灣慣用詞彙（如：專案、會議、簡報、確認、執行）。"
            "只輸出翻譯結果，不加任何說明。"
        ),
    },
    "general": {
        "model": "gemini-2.0-flash",
        "system": (
            "你是專業口譯員。以下是語音辨識的英文片段，請翻譯成自然流暢的繁體中文。"
            "只輸出翻譯，不加解釋。"
        ),
    },
}
_prev_translations = []  # 保留前 2 句作為脈絡

_DIARIZE_SYSTEM = (
    "你是專業的多人對話即時口譯員。以下是語音辨識的英文片段，可能來自多人對話（訪談、會議、影集）。\n"
    "請依照以下規則處理：\n"
    "1. 判斷這段話是**單人獨白**還是**多人對話**\n"
    "2. 若是單人：直接輸出繁體中文翻譯，不加任何標籤\n"
    "3. 若是多人：用【A】【B】【C】標記不同說話者，每人一行，格式：\n"
    "   【A】：她的繁體中文翻譯\n"
    "   【B】：他的繁體中文翻譯\n"
    "4. 根據問答關係、人稱代詞（I/you/he/she）、語氣轉變來判斷說話者切換\n"
    "5. 語者標籤要前後一致（若上輪【A】是主持人，這輪也維持）\n"
    "只輸出翻譯結果，不加任何說明或解釋。"
)


def _diarize_fallback(en_text: str, zh_text: str) -> str:
    """Gemini 失敗時用標點和問句特徵做基本說話人推斷"""
    # 以英文問句偵測換人：以 So/How/What/Did/Are/Is/Can 開頭 或含 ?
    INTERVIEWER_STARTS = ('So ', 'How ', 'What ', 'When ', 'Where ', 'Why ',
                          'Did ', 'Do ', 'Are ', 'Is ', 'Can ', 'Could ', 'Would ',
                          'Tell me', 'And so')
    en_sents = [s.strip() for s in re.split(r'(?<=[.!?])\s+', en_text) if s.strip()]
    zh_sents = [s.strip() for s in re.split(r'(?<=[。！？])\s*|(?<=。)|(?<=！)|(?<=？)', zh_text) if s.strip()]

    if len(en_sents) <= 1 or len(zh_sents) <= 1:
        return zh_text  # 單句不貼標籤

    result, label = [], 'A'
    prev_label = None
    for i, en_s in enumerate(en_sents):
        is_q = '?' in en_s or any(en_s.startswith(p) for p in INTERVIEWER_STARTS)
        new_label = 'B' if is_q else 'A'
        if new_label != label:
            label = new_label
        zh_s = zh_sents[i] if i < len(zh_sents) else ""
        if zh_s:
            result.append(f"【{label}】：{zh_s}")
    return '\n'.join(result) if result else zh_text


def translate_text_ai(text: str, scene: str = "general", diarize: bool = False) -> str:
    """Gemini AI 翻譯（整句模式，支援說話人辨識，含 rate limit + 重試）"""
    global _speaker_context, _gemini_last_call
    if not AI_OK or _gemini_client is None:
        zh = translate_text(text)
        return _diarize_fallback(text, zh) if diarize else zh

    # Rate limit 保護
    now = time.time()
    wait = _GEMINI_MIN_INTERVAL - (now - _gemini_last_call)
    if wait > 0:
        print(f"[Gemini] 等待 {wait:.1f}s 避免 rate limit")
        time.sleep(wait)

    cfg = _SCENE_CONFIG.get(scene, _SCENE_CONFIG["general"])
    # diarize 固定用 gemini-2.0-flash（最穩定，2.5-flash 偶有 quota 問題）
    model = "gemini-2.0-flash" if diarize else cfg["model"]

    if diarize:
        context = ""
        if _speaker_context:
            ctx_lines = "\n".join(
                f"【{label}】：{seg}" for label, seg in _speaker_context[-4:]
            )
            context = f"【前輪對話記憶（維持語者一致性）】\n{ctx_lines}\n\n"
        prompt = _DIARIZE_SYSTEM + "\n\n" + context + "【待翻譯片段】\n" + text
    else:
        context = ("【前文參考】\n" + "\n".join(_prev_translations[-2:]) + "\n\n"
                   if _prev_translations else "")
        prompt = cfg["system"] + "\n\n" + context + "【待翻譯】\n" + text

    try:
        _gemini_last_call = time.time()
        resp = _gemini_client.models.generate_content(model=model, contents=prompt)
        result = resp.text.strip()

        if diarize:
            for line in result.splitlines():
                m = re.match(r'【([A-Z])】[：:]?\s*(.+)', line)
                if m:
                    _speaker_context.append((m.group(1), m.group(2).strip()))
            if len(_speaker_context) > 8:
                _speaker_context = _speaker_context[-8:]
        else:
            _prev_translations.append(result)
            if len(_prev_translations) > 5:
                _prev_translations.pop(0)
        return result
    except Exception as e:
        # ClientError = rate limit，延長下次間隔，直接 fallback 不重試
        _gemini_last_call = time.time() + 10  # 懲罰：下次至少等 10+5=15s
        print(f"[Gemini] 失敗 ({type(e).__name__})，fallback Google")
        zh = translate_text(text)
        return _diarize_fallback(text, zh) if diarize else zh


# ══════════════════════════════════════════════════════════════════════════════
# TTS 播放（PowerShell 系統語音，延遲 < 0.1s，無 threading 問題）
# ══════════════════════════════════════════════════════════════════════════════
_tts_proc = None  # 追蹤目前播放的 TTS process

def _speak(text: str, voice: str = None, rate: int = 3, wait: bool = False):
    """用 PowerShell 呼叫 Windows SAPI 發音"""
    global _tts_proc
    if _tts_proc and _tts_proc.poll() is None:
        _tts_proc.terminate()
    v = voice or TTS_VOICE_A
    safe = text.replace("'", " ").replace('"', " ")
    ps_cmd = (
        f"Add-Type -AssemblyName System.Speech; "
        f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SelectVoice('{v}'); "
        f"$s.Rate = {rate}; "
        f"$s.Speak('{safe}')"
    )
    _tts_proc = subprocess.Popen(
        ['powershell', '-WindowStyle', 'Hidden', '-Command', ps_cmd],
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    )
    if wait and _tts_proc:
        _tts_proc.wait()


def _speak_diarized(text: str):
    """解析【A】【B】標籤，用不同語速依序播放各講者"""
    # 偵測是否有多講者標籤
    if not re.search(r'【[A-Z]】', text):
        _speak(text)
        return
    # 拆分為 (label, segment) 清單
    parts = re.split(r'(【[A-Z]】[：:]?\s*)', text)
    current_label = 'A'
    segments = []
    for part in parts:
        m = re.match(r'【([A-Z])】', part)
        if m:
            current_label = m.group(1)
        elif part.strip():
            segments.append((current_label, part.strip()))
    for label, seg in segments:
        if stop_event.is_set():
            break
        # A: 女聲快速, B: 同聲音但慢速（若有第二聲音則切換）
        if label == 'B':
            _speak(seg, voice=TTS_VOICE_B, rate=0, wait=True)
        else:
            _speak(seg, voice=TTS_VOICE_A, rate=3, wait=True)


def tts_worker():
    """背景 TTS 執行緒：只播最新一句，丟棄積壓"""
    while not stop_event.is_set():
        try:
            text = tts_queue.get(timeout=0.5)
            if not text:
                continue
            # 清空積壓，只播最新
            while not tts_queue.empty():
                try:
                    text = tts_queue.get_nowait()
                except queue.Empty:
                    break
            # 有說話者標籤 → 多聲線；否則普通播放
            if re.search(r'【[A-Z]】', text):
                _speak_diarized(text)
            else:
                _speak(text)
        except queue.Empty:
            pass
        except Exception as e:
            print(f"TTS 錯誤: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Whisper STT
# ══════════════════════════════════════════════════════════════════════════════
def get_whisper() -> 'WhisperModel | None':
    global _whisper_model
    if WHISPER_OK and _whisper_model is None:
        subtitle_queue.put(("[載入 Whisper tiny 模型...]", ""))
        _whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
        subtitle_queue.put(("[Whisper 就緒]", ""))
    return _whisper_model


def transcribe(audio_np: np.ndarray) -> str:
    model = get_whisper()
    if model is None:
        return ""
    segments, _ = model.transcribe(
        audio_np.astype(np.float32),
        language="en",
        beam_size=1,
        vad_filter=True
    )
    return " ".join(s.text for s in segments).strip()


# ══════════════════════════════════════════════════════════════════════════════
# 音訊下載（yt-dlp → ffmpeg → WAV）
# ══════════════════════════════════════════════════════════════════════════════
def download_audio_wav(url: str, out_wav: str) -> bool:
    """下載 URL 音訊，轉為 16kHz 單聲道 WAV"""
    if not YTDLP_OK or FFMPEG is None:
        subtitle_queue.put(("[yt-dlp 或 ffmpeg 未就緒]", ""))
        return False

    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            # 找最佳音訊格式 URL
            audio_url = None
            for fmt in (info.get('formats') or []):
                if fmt.get('acodec') != 'none' and fmt.get('vcodec') == 'none':
                    audio_url = fmt['url']
                    break
            if not audio_url:
                audio_url = info.get('url') or info['webpage_url']

        result = subprocess.run(
            [FFMPEG, '-y', '-i', audio_url,
             '-ar', str(SAMPLE_RATE), '-ac', '1', '-f', 'wav', out_wav],
            capture_output=True, timeout=300
        )
        return os.path.exists(out_wav) and os.path.getsize(out_wav) > 1000
    except Exception as e:
        subtitle_queue.put((f"[下載失敗: {e}]", ""))
        return False


# ══════════════════════════════════════════════════════════════════════════════
# YouTube 字幕模式
# ══════════════════════════════════════════════════════════════════════════════
def get_captions(url: str) -> list | None:
    """取得 YouTube 字幕，回傳 [(start, duration, text), ...]"""
    if not TRANSCRIPT_OK:
        return None
    vid = extract_video_id(url)
    if not vid:
        return None
    try:
        tlist = YouTubeTranscriptApi.list_transcripts(vid)
        for lang in ['en', 'en-US', 'en-GB']:
            try:
                raw = tlist.find_transcript([lang]).fetch()
                return [(s.start, s.duration, s.text) for s in raw]
            except Exception:
                pass
        try:
            raw = tlist.find_generated_transcript(['en']).fetch()
            return [(s.start, s.duration, s.text) for s in raw]
        except Exception:
            pass
    except Exception:
        pass
    return None


def run_url_with_captions(url: str, captions: list, use_tts: bool, wav_path: str):
    """有字幕：預翻譯 + 同步字幕 + 播放音訊"""
    subtitle_queue.put((f"[翻譯 {len(captions)} 段字幕中...]", ""))

    translated = []
    for i, (start, dur, text) in enumerate(captions):
        if stop_event.is_set():
            return
        zh = translate_text(text)
        translated.append((start, text, zh))
        if i % 30 == 0 and i > 0:
            subtitle_queue.put((f"[翻譯進度 {i}/{len(captions)}]", ""))

    subtitle_queue.put(("[翻譯完成，開始播放]", ""))

    # 讀取並播放音訊
    data, sr = sf.read(wav_path)
    sd.play(data, sr)
    play_start = time.time()

    idx = 0
    while idx < len(translated) and not stop_event.is_set():
        elapsed = time.time() - play_start
        ts, en, zh = translated[idx]
        if elapsed >= ts:
            subtitle_queue.put((en, zh))
            if use_tts:
                tts_queue.put(zh)
            idx += 1
        else:
            time.sleep(0.05)

    sd.wait()


def vad_split(audio: np.ndarray) -> list:
    """對已下載音訊做 VAD 切段（自適應閾值）"""
    FRAME          = 160
    SILENCE_FRAMES = int(SILENCE_SEC * SAMPLE_RATE)
    MIN_SPEECH     = int(MIN_SPEECH_SEC * SAMPLE_RATE)

    # 自適應閾值：取全段 RMS 分佈的低百分位，避免音量差異問題
    rms_all = np.array([
        float(np.sqrt(np.mean(audio[i:i+FRAME]**2)))
        for i in range(0, len(audio)-FRAME, FRAME)
    ])
    rms_all = rms_all[rms_all > 0]
    if len(rms_all) == 0:
        return []
    # 閾值 = 最大 RMS 的 8%，最低不低於 0.001
    adaptive_thresh = max(float(np.percentile(rms_all, 95)) * 0.08, 0.001)
    subtitle_queue.put((f"[VAD 自適應閾值: {adaptive_thresh:.4f}, 音訊最大: {rms_all.max():.4f}]", ""))

    segments = []
    speech_start = 0
    in_speech    = False
    silence_cnt  = 0

    for i in range(0, len(audio) - FRAME, FRAME):
        rms = float(np.sqrt(np.mean(audio[i:i + FRAME] ** 2)))
        if rms > adaptive_thresh:
            if not in_speech:
                speech_start = i
                in_speech = True
            silence_cnt = 0
        elif in_speech:
            silence_cnt += FRAME
            if silence_cnt >= SILENCE_FRAMES:
                seg = audio[speech_start:i]
                if len(seg) >= MIN_SPEECH:
                    segments.append((speech_start, seg))
                in_speech = False
                silence_cnt = 0
    return segments


def run_url_whisper(wav_path: str, use_tts: bool, mode: str, scene: str = "general", diarize: bool = False):
    """無字幕：VAD 切段 + Whisper 辨識"""
    get_whisper()
    data, sr = sf.read(wav_path)
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = data.astype(np.float32)

    sd.play(data, sr)
    play_start = time.time()
    segments = vad_split(data)
    subtitle_queue.put((f"[VAD 偵測到 {len(segments)} 個語音段，辨識中...]", ""))

    for start_sample, seg in segments:
        if stop_event.is_set():
            break
        target_sec = start_sample / SAMPLE_RATE
        while time.time() - play_start < target_sec - 0.5 and not stop_event.is_set():
            time.sleep(0.05)
        en = transcribe(seg)
        if en:
            process_chunk(en, mode, use_tts, scene, diarize=diarize)

    sd.wait()


def url_worker(url: str, use_tts: bool, mode: str, scene: str = "general", diarize: bool = False):
    """URL 模式主流程（在背景執行緒執行）"""
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        wav_path = f.name
    try:
        subtitle_queue.put(("[下載音訊中，請稍候...]", ""))
        ok = download_audio_wav(url, wav_path)
        if not ok or stop_event.is_set():
            return

        captions = get_captions(url)
        if captions:
            subtitle_queue.put((f"[找到 {len(captions)} 段字幕]", ""))
            run_url_with_captions(url, captions, use_tts, wav_path)
        else:
            subtitle_queue.put(("[未找到字幕，使用 Whisper]", ""))
            run_url_whisper(wav_path, use_tts, mode, scene, diarize=diarize)
    finally:
        if os.path.exists(wav_path):
            os.unlink(wav_path)


# ══════════════════════════════════════════════════════════════════════════════
# 即時音訊模式（麥克風 / 系統音訊）
# ══════════════════════════════════════════════════════════════════════════════
def live_worker(device_index: int | None, is_loopback: bool, use_tts: bool,
                mode: str, scene: str = "general", diarize: bool = False):
    """即時音訊：sounddevice 擷取 → Whisper → 翻譯"""
    if not AUDIO_OK:
        subtitle_queue.put(("[sounddevice 未安裝]", ""))
        return

    get_whisper()
    FRAME          = 160
    SILENCE_FRAMES = int(SILENCE_SEC * SAMPLE_RATE)
    MIN_SPEECH     = int(MIN_SPEECH_SEC * SAMPLE_RATE)
    MAX_SPEECH     = int(15.0 * SAMPLE_RATE)  # 連續說話 15 秒強制截斷

    raw_buf      = np.zeros(0, dtype=np.float32)
    speech_accum = np.zeros(0, dtype=np.float32)
    in_speech    = False
    silence_cnt  = 0
    segment_queue = queue.Queue()

    # 即時模式：前 2 秒取樣定自適應閾值
    _live_thresh = [ENERGY_THRESH]  # 用 list 讓 vad_loop 可寫入

    subtitle_queue.put(("[即時 VAD 模式啟動，校準環境音 2 秒...]", ""))

    # 查裝置原生設定（避免 WASAPI 裝置拒絕非原生 samplerate/channels）
    if device_index is not None:
        dev_info   = sd.query_devices(device_index)
        native_sr  = int(dev_info['default_samplerate'])
        native_ch  = max(1, dev_info['max_input_channels'])
    else:
        native_sr  = SAMPLE_RATE
        native_ch  = 1
    print(f"[音訊] 裝置原生 samplerate={native_sr} channels={native_ch}")

    def callback(indata, frames, time_info, status):
        nonlocal raw_buf
        # 多聲道 → mono
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata.flatten()
        # 若原生 SR 不同於 Whisper 需要的 16000，做線性 resample
        if native_sr != SAMPLE_RATE:
            target_len = int(len(mono) * SAMPLE_RATE / native_sr)
            if target_len > 0:
                mono = np.interp(
                    np.linspace(0, len(mono) - 1, target_len),
                    np.arange(len(mono)),
                    mono
                ).astype(np.float32)
        raw_buf = np.concatenate([raw_buf, mono])

    _vad_debug_counter = [0]  # 每 N 幀印一次 RMS，避免洗屏

    def vad_loop():
        """VAD 執行緒：前 2 秒校準環境音，之後偵測語音段落"""
        nonlocal raw_buf, speech_accum, in_speech, silence_cnt
        calibration_buf = []
        calibrated = False
        cal_target = SAMPLE_RATE * 2  # 2 秒校準

        while not stop_event.is_set():
            if len(raw_buf) < FRAME:
                time.sleep(0.005)
                continue
            frame = raw_buf[:FRAME].copy()
            raw_buf = raw_buf[FRAME:]
            rms = float(np.sqrt(np.mean(frame ** 2)))

            # 校準階段
            if not calibrated:
                calibration_buf.append(rms)
                if len(calibration_buf) * FRAME >= cal_target:
                    # 用低百分位（10th）捕捉真正的靜音底，避免校準期間有音訊時誤拉高閾值
                    p10 = float(np.percentile(calibration_buf, 10))
                    p80 = float(np.percentile(calibration_buf, 80))
                    # 閾值 = p10 * 4，但上限 0.04（確保不會把語音擋掉），下限 0.003
                    _live_thresh[0] = max(min(p10 * 4.0, 0.04), 0.003)
                    calibrated = True
                    print(f"[VAD] 校準完成 p10={p10:.5f} p80={p80:.5f} thresh={_live_thresh[0]:.5f}")
                    subtitle_queue.put((f"[VAD 閾值={_live_thresh[0]:.4f}，開始聆聽]", ""))
                continue

            thresh = _live_thresh[0]
            _vad_debug_counter[0] += 1
            if _vad_debug_counter[0] % 200 == 0:  # 每 200 幀（約 2 秒）印一次
                print(f"[VAD] rms={rms:.5f} thresh={thresh:.5f} in_speech={in_speech}")

            if rms > thresh:
                if not in_speech:
                    print(f"[VAD] 偵測到語音開始 rms={rms:.5f}")
                in_speech = True
                silence_cnt = 0
                speech_accum = np.concatenate([speech_accum, frame])
                # 超過 15 秒連續語音 → 強制截斷送辨識
                if len(speech_accum) >= MAX_SPEECH:
                    seg_len = len(speech_accum)
                    print(f"[VAD] 強制截斷 {seg_len/SAMPLE_RATE:.1f}s（連續語音過長）")
                    segment_queue.put(speech_accum.copy())
                    speech_accum = np.zeros(0, dtype=np.float32)
                    silence_cnt = 0
                    # in_speech 保持 True，繼續累積下一段
            elif in_speech:
                speech_accum = np.concatenate([speech_accum, frame])
                silence_cnt += FRAME
                if silence_cnt >= SILENCE_FRAMES:
                    seg_len = len(speech_accum)
                    print(f"[VAD] 語音結束，長度={seg_len} 樣本 ({seg_len/SAMPLE_RATE:.2f}秒) 最小={MIN_SPEECH}")
                    if seg_len >= MIN_SPEECH:
                        segment_queue.put(speech_accum.copy())
                        print(f"[VAD] 片段加入辨識佇列")
                    else:
                        print(f"[VAD] 片段太短，丟棄")
                    speech_accum = np.zeros(0, dtype=np.float32)
                    in_speech = False
                    silence_cnt = 0

    vad_thread = threading.Thread(target=vad_loop, daemon=True)
    vad_thread.start()

    # 開啟音訊串流（使用裝置原生 samplerate/channels，callback 內再 resample）
    if is_loopback:
        wasapi_extra = sd.WasapiSettings(loopback=True)
        stream_ctx = sd.InputStream(samplerate=native_sr, channels=native_ch, dtype='float32',
                                    device=device_index, extra_settings=wasapi_extra,
                                    callback=callback)
        print(f"[音訊] WASAPI loopback 模式 device={device_index} sr={native_sr} ch={native_ch}")
        subtitle_queue.put(("[系統音訊(loopback)模式]", ""))
    else:
        stream_ctx = sd.InputStream(samplerate=native_sr, channels=native_ch, dtype='float32',
                                    device=device_index, callback=callback)
        print(f"[音訊] 輸入模式 device={device_index} sr={native_sr} ch={native_ch}")
        subtitle_queue.put((f"[輸入裝置模式 sr={native_sr}]", ""))

    print(f"[live_worker] 開始主循環 diarize={diarize}")
    with stream_ctx:
        while not stop_event.is_set():
            try:
                segment = segment_queue.get(timeout=0.5) if not segment_queue.empty() else None
                if segment is None:
                    time.sleep(0.05)
                    continue
                print(f"[Whisper] 開始辨識 長度={len(segment)/SAMPLE_RATE:.2f}秒")
                en = transcribe(segment)
                print(f"[Whisper] 辨識結果: '{en}'")
                if en:
                    if mode == 'chunk':
                        subtitle_queue.put((en, ""))
                    process_chunk(en, mode, use_tts, scene, diarize=diarize)
            except Exception as exc:
                print(f"[live_worker] 循環異常（繼續運作）: {exc}")
    print("[live_worker] 主循環結束")


# ══════════════════════════════════════════════════════════════════════════════
# 主視窗
# ══════════════════════════════════════════════════════════════════════════════
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("即時語音翻譯系統")
        self.root.resizable(False, False)

        self.subtitle_win  = None
        self.sentence_win  = None
        self.running       = False

        self._build_ui()
        self._poll_subtitle()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build_ui(self):
        pad = dict(padx=10, pady=4)

        # 標題
        tk.Label(self.root, text="即時語音翻譯系統",
                 font=('Arial', 14, 'bold')).grid(row=0, column=0, columnspan=3, **pad)

        # 輸入來源模式
        tk.Label(self.root, text="來源").grid(row=1, column=0, **pad, sticky='w')
        self.mode_var = tk.StringVar(value="speaker")  # 預設：喇叭直通
        tk.Radiobutton(self.root, text="🔊 喇叭直通（通用）",
                       variable=self.mode_var, value="speaker",
                       command=self._on_mode).grid(row=1, column=1, sticky='w')
        tk.Radiobutton(self.root, text="影片 URL",
                       variable=self.mode_var, value="url",
                       command=self._on_mode).grid(row=1, column=2, sticky='w')
        tk.Radiobutton(self.root, text="指定裝置",
                       variable=self.mode_var, value="live",
                       command=self._on_mode).grid(row=1, column=3, sticky='w')

        # URL 輸入
        tk.Label(self.root, text="URL").grid(row=2, column=0, **pad, sticky='w')
        self.url_var = tk.StringVar()
        self.url_entry = tk.Entry(self.root, textvariable=self.url_var, width=55,
                                  font=('Arial', 10))
        self.url_entry.grid(row=2, column=1, columnspan=2, **pad)

        # 音訊裝置（即時模式用）
        tk.Label(self.root, text="音訊裝置").grid(row=3, column=0, **pad, sticky='w')
        self.device_var = tk.StringVar(value="預設輸入裝置")
        self.device_combo = ttk.Combobox(self.root, textvariable=self.device_var,
                                          width=45, state='disabled')
        self.device_combo.grid(row=3, column=1, columnspan=2, **pad)
        self._load_devices()

        # 翻譯模式
        tk.Label(self.root, text="翻譯模式").grid(row=4, column=0, **pad, sticky='w')
        self.trans_mode_var = tk.StringVar(value="sentence")  # 預設整句模式（配合說話人區分）
        tk.Radiobutton(self.root, text="逐格翻譯（即時）",
                       variable=self.trans_mode_var, value="chunk").grid(
                       row=4, column=1, sticky='w')
        tk.Radiobutton(self.root, text="整句翻譯（AI整合，品質較高）",
                       variable=self.trans_mode_var, value="sentence").grid(
                       row=4, column=2, sticky='w')

        # 場景選擇（整句模式用）
        tk.Label(self.root, text="翻譯場景").grid(row=5, column=0, **pad, sticky='w')
        self.scene_var = tk.StringVar(value="general")
        tk.Radiobutton(self.root, text="通用",
                       variable=self.scene_var, value="general").grid(
                       row=5, column=1, sticky='w')
        tk.Radiobutton(self.root, text="YouTube / TED 演講",
                       variable=self.scene_var, value="youtube").grid(
                       row=5, column=2, sticky='w')
        tk.Radiobutton(self.root, text="Teams 商業會議（最高準確性）",
                       variable=self.scene_var, value="teams").grid(
                       row=5, column=3, sticky='w')

        # 選項列
        opt_frame = tk.Frame(self.root)
        opt_frame.grid(row=6, column=0, columnspan=4, sticky='w', padx=10)
        self.tts_var = tk.BooleanVar(value=True)
        tk.Checkbutton(opt_frame, text="🔊 中文語音輸出",
                       variable=self.tts_var, font=('Arial', 10)).pack(side='left', padx=8)
        self.diarize_var = tk.BooleanVar(value=True)  # 預設開啟說話人區分
        tk.Checkbutton(opt_frame, text="👥 說話人區分（對話/會議）",
                       variable=self.diarize_var, font=('Arial', 10)).pack(side='left', padx=8)

        # 狀態列
        self.status_var = tk.StringVar(value="就緒")
        tk.Label(self.root, textvariable=self.status_var, fg='#555',
                 font=('Arial', 9)).grid(row=7, column=0, columnspan=4, **pad)

        # 開始/停止按鈕
        self.btn = tk.Button(self.root, text="▶  開始翻譯",
                              command=self._toggle,
                              bg='#4CAF50', fg='white',
                              font=('Arial', 13, 'bold'), width=18)
        self.btn.grid(row=8, column=0, columnspan=4, pady=8)

        # 翻譯輸出區（整句結果顯示）
        tk.Label(self.root, text="翻譯輸出",
                 font=('Arial', 10, 'bold')).grid(row=9, column=0, columnspan=4,
                 sticky='w', padx=10, pady=(8, 2))
        out_frame = tk.Frame(self.root)
        out_frame.grid(row=10, column=0, columnspan=4, padx=10, pady=(0, 4), sticky='nsew')
        self.out_text = tk.Text(out_frame, height=10, width=80, font=('Arial', 11),
                                wrap='word', state='disabled', bg='#f8f8f8')
        scrollbar = ttk.Scrollbar(out_frame, orient='vertical',
                                   command=self.out_text.yview)
        self.out_text.configure(yscrollcommand=scrollbar.set)
        self.out_text.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')

        btn_frame = tk.Frame(self.root)
        btn_frame.grid(row=11, column=0, columnspan=4, pady=(0, 4))
        tk.Button(btn_frame, text="清除輸出",
                  command=self._clear_output).pack(side='left', padx=5)

        # 環境狀態
        env_info = (
            f"Whisper:{'✓' if WHISPER_OK else '✗'}  "
            f"Google翻譯:{'✓' if TRANS_OK else '✗'}  "
            f"AI翻譯:{'✓' if AI_OK else '✗(未設定.env)'}  "
            f"TTS:{'✓' if TTS_OK else '✗'}  "
            f"音訊:{'✓' if AUDIO_OK else '✗'}  "
            f"ffmpeg:{'✓' if FFMPEG else '✗'}"
        )
        tk.Label(self.root, text=env_info, fg='gray',
                 font=('Arial', 8)).grid(row=12, column=0, columnspan=4, pady=(0, 8))

        self._on_mode()

    def _load_devices(self):
        if not AUDIO_OK:
            return
        devices = sd.query_devices()
        print("=" * 60)
        print("[裝置清單] 所有音訊裝置：")
        for i, d in enumerate(devices):
            print(f"  [{i}] {d['name']}  in={d['max_input_channels']} out={d['max_output_channels']} hostapi={d['hostapi']}")
        print("=" * 60)

        names = ["預設輸入裝置"]
        self._device_map = {0: (None, False)}
        stereo_mix_idx = None        # combobox index
        self._stereo_mix_device = (None, False)  # (device_idx, is_loopback)

        # 輸入裝置：WASAPI(hostapi=3) 優先，其他次之
        for hostapi_filter in [3, None]:
            for i, d in enumerate(devices):
                if d['max_input_channels'] <= 0:
                    continue
                if hostapi_filter is not None and d['hostapi'] != hostapi_filter:
                    continue
                if hostapi_filter is None and d['hostapi'] == 3:
                    continue  # 已在上一輪加過
                label = d['name']
                if '立體聲混音' in label or 'Stereo Mix' in label.lower():
                    names.append(f"⭐ [{i}] {label} ← Teams/系統音訊推薦")
                    stereo_mix_idx = len(names) - 1
                    self._stereo_mix_device = (i, False)
                else:
                    names.append(f"🎤 [{i}] {label}")
                self._device_map[len(names) - 1] = (i, False)

        # 輸出裝置 WASAPI loopback（只取 hostapi=3，其他 hostapi 不支援 loopback）
        for i, d in enumerate(devices):
            if d['max_output_channels'] > 0 and d['hostapi'] == 3:
                names.append(f"🔊 [{i}] {d['name']} (WASAPI loopback)")
                self._device_map[len(names) - 1] = (i, True)
        print(f"[裝置清單] 下拉選項共 {len(names)} 項：")
        for k, v in self._device_map.items():
            print(f"  combobox[{k}] → device={v[0]} loopback={v[1]}  name={names[k]}")
        self.device_combo['values'] = names
        # 自動選中立體聲混音（最適合擷取 Teams/系統音訊）
        self.device_combo.current(stereo_mix_idx if stereo_mix_idx else 0)
        print(f"[裝置清單] 共 {len(names)} 項，預設選 combobox[{stereo_mix_idx or 0}]")

    def _on_mode(self):
        m = self.mode_var.get()
        self.url_entry.config(state='normal' if m == 'url' else 'disabled')
        self.device_combo.config(state='readonly' if m == 'live' else 'disabled')

    # ── 控制 ─────────────────────────────────────────────────────────────────
    def _toggle(self):
        if self.running:
            self._stop()
        else:
            self._start()

    def _start(self):
        stop_event.clear()
        self.running = True
        self.btn.config(text="⏹  停止", bg='#f44336')

        use_tts    = self.tts_var.get()
        src_mode   = self.mode_var.get()
        trans_mode = self.trans_mode_var.get()
        scene      = self.scene_var.get()
        diarize    = self.diarize_var.get()

        # 開字幕視窗
        self.subtitle_win = SubtitleWindow(self.root)

        # 整句模式 → 開第二視窗
        if trans_mode == 'sentence':
            self.sentence_win = SentenceWindow(self.root)
        else:
            self.sentence_win = None

        # 建立本次翻譯輸出檔（含 GUI 設定標頭）
        _start_output_file({
            '音訊來源':   {'speaker': '喇叭直通', 'url': 'URL', 'live': '指定裝置'}.get(src_mode, src_mode),
            '翻譯模式':   '逐格（即時）' if trans_mode == 'chunk' else '整句（AI）',
            '翻譯場景':   scene,
            '說話人區分': '是' if diarize else '否',
            '中文語音':   '是' if use_tts else '否',
        })

        # TTS 執行緒
        tts_thread = threading.Thread(target=tts_worker, daemon=True)
        tts_thread.start()

        if src_mode == "speaker":
            # 喇叭直通：自動選立體聲混音，無需使用者設定
            idx, is_loopback = self._stereo_mix_device
            if idx is None:
                self.status_var.set("⚠ 未找到立體聲混音裝置，請改用「指定裝置」")
                self._stop()
                return
            self.status_var.set(f"🔊 喇叭直通（device={idx}）...")
            print(f"[開始] 喇叭直通 device={idx} loopback={is_loopback} diarize={diarize}")
            t = threading.Thread(target=live_worker,
                                 args=(idx, is_loopback, use_tts, trans_mode, scene, diarize), daemon=True)
        elif src_mode == "url":
            url = self.url_var.get().strip()
            if not url:
                self.status_var.set("請輸入 URL")
                self._stop()
                return
            self.status_var.set("準備中...")
            t = threading.Thread(target=url_worker,
                                 args=(url, use_tts, trans_mode, scene, diarize), daemon=True)
        else:  # live
            self.status_var.set("即時擷取中...")
            combo_idx = self.device_combo.current()
            idx, is_loopback = self._device_map.get(combo_idx, (None, False))
            print(f"[開始] combobox選擇index={combo_idx} → device={idx} loopback={is_loopback}")
            t = threading.Thread(target=live_worker,
                                 args=(idx, is_loopback, use_tts, trans_mode, scene, diarize), daemon=True)

        t.start()

    def _stop(self):
        stop_event.set()
        if AUDIO_OK:
            sd.stop()
        # 立即殺掉 TTS process
        global _tts_proc
        if _tts_proc and _tts_proc.poll() is None:
            _tts_proc.terminate()
            _tts_proc = None
        # 清空 TTS queue，避免殘留項目繼續播放
        while not tts_queue.empty():
            try:
                tts_queue.get_nowait()
            except Exception:
                break
        _close_output_file()
        self.running = False
        self.btn.config(text="▶  開始翻譯", bg='#4CAF50')
        self.status_var.set("已停止")
        if self.subtitle_win:
            try:
                self.subtitle_win.win.destroy()
            except Exception:
                pass
            self.subtitle_win = None
        if self.sentence_win:
            self.sentence_win.destroy()
            self.sentence_win = None

    def _on_close(self):
        self._stop()
        self.root.destroy()

    def _clear_output(self):
        self.out_text.config(state='normal')
        self.out_text.delete('1.0', tk.END)
        self.out_text.config(state='disabled')

    def _append_output(self, en: str, zh: str):
        self.out_text.config(state='normal')
        self.out_text.insert(tk.END, f"EN: {en}\nZH: {zh}\n{'─'*50}\n")
        self.out_text.see(tk.END)
        self.out_text.config(state='disabled')

    # ── 輪詢（每 100ms）──────────────────────────────────────────────────────
    def _poll_subtitle(self):
        # 懸浮字幕
        try:
            while True:
                en, zh = subtitle_queue.get_nowait()
                self.status_var.set(en[:70] if en else "")
                if self.subtitle_win:
                    try:
                        self.subtitle_win.update(en, zh)
                    except Exception:
                        pass
        except queue.Empty:
            pass

        # 主視窗輸出區（chunk 模式）
        try:
            while True:
                en, zh = sentence_output_queue.get_nowait()
                self._append_output(en, zh)
        except queue.Empty:
            pass

        # 第二視窗（整句模式 AI 翻譯）
        if self.sentence_win:
            try:
                while True:
                    en, zh = sentence_queue.get_nowait()
                    self.sentence_win.append(en, zh)
            except queue.Empty:
                pass

        self.root.after(100, self._poll_subtitle)

    def run(self):
        self.root.mainloop()


# ══════════════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    if '--help' in sys.argv or '-h' in sys.argv:
        print(__doc__)
        sys.exit(0)

    import traceback
    log_path = os.path.join(os.path.dirname(__file__), 'translate_log.txt')

    class _Tee:
        """同時輸出到 console 和 log 檔"""
        def __init__(self, *streams):
            self.streams = streams
        def write(self, data):
            for s in self.streams:
                try: s.write(data)
                except: pass
        def flush(self):
            for s in self.streams:
                try: s.flush()
                except: pass

    _log_file = open(log_path, 'a', encoding='utf-8', buffering=1)
    sys.stdout = _Tee(sys.__stdout__, _log_file)
    sys.stderr = _Tee(sys.__stderr__, _log_file)
    print(f"\n{'='*60}\n[啟動] {time.strftime('%Y-%m-%d %H:%M:%S')}\n{'='*60}")

    try:
        App().run()
    except Exception:
        print("[FATAL] 主程式異常：")
        traceback.print_exc()
    finally:
        print("[結束] 程式退出")
        _log_file.close()
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
