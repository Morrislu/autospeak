# AutoSpeak - 即時語音翻譯系統

即時將英文語音翻譯成繁體中文字幕 + 語音輸出。支援系統音訊（Teams/Zoom/YouTube）、麥克風、URL 三種音訊來源。

## 功能

- **喇叭直通**：自動擷取所有系統音訊（Teams、Zoom、YouTube、Netflix 等）
- **逐格翻譯**：每段辨識立即顯示，速度快（Google Translate）
- **整句翻譯**：累積後 Gemini AI 翻譯，品質高，彈出專用視窗
- **說話人辨識**：自動標記 【A】/【B】 說話人，不同聲音播放
- **中文語音輸出**：Windows SAPI5 TTS，延遲 < 0.1s
- **翻譯存檔**：每次執行自動存 `translation_output/trans_*.txt`

---

## 快速開始

### 步驟一：申請免費 Gemini API Key

1. 前往 [Google AI Studio](https://aistudio.google.com/)
2. 登入 Google 帳號
3. 點左上角 **「Get API key」** → **「Create API key」**
4. 複製產生的 key（格式為 `AIzaSy...`）

> Gemini 免費版每分鐘 15 次請求，日常使用完全夠用。

---

### 步驟二：下載此專案

```bash
git clone https://github.com/Morrislu/autospeak.git
cd autospeak
```

---

### 步驟三：填入 API Key

在專案根目錄建立 `.env` 檔案，填入你的 key：

**方法 A（命令列）**
```bash
echo GEMINI_API_KEY=貼上你的key > .env
```

**方法 B（手動）**

用記事本在專案資料夾新建 `.env` 檔案，內容：
```
GEMINI_API_KEY=AIzaSy你的key
```

---

### 步驟四：安裝套件

```bash
pip install faster-whisper sounddevice soundfile numpy deep-translator python-dotenv google-genai
```

或雙擊 `install_pkgs.bat`（Windows）。

---

### 步驟五：啟動

```bash
python PY_auto_translate.py
```

或雙擊 `run_translate.bat`（Windows）。

---

## 前置條件（Windows）

- **Python 3.11+**
- **立體聲混音**：右鍵工作列喇叭 → 音效設定 → 錄製 → 右鍵啟用「立體聲混音」
- （URL 模式）`ffmpeg` 和 `yt-dlp` 加入 PATH

---

## 技術架構

| 元件 | 工具 |
|------|------|
| STT | faster-whisper tiny (CPU, 免費) |
| 快速翻譯 | Google Translate (deep-translator, 免費) |
| AI 翻譯 | Gemini 2.0 Flash (免費版) |
| TTS | Windows SAPI5 PowerShell (內建) |
| 音訊擷取 | sounddevice WASAPI |
| GUI | tkinter |

**總費用：$0**
