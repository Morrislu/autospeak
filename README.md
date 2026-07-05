# AutoSpeak - 即時語音翻譯系統

即時將英文語音翻譯成繁體中文字幕 + 語音輸出。支援系統音訊（Teams/Zoom/YouTube）、麥克風、URL 三種音訊來源。

## 功能

- **喇叭直通**：自動擷取所有系統音訊（Teams、Zoom、YouTube、Netflix 等）
- **逐格翻譯**：每段辨識立即顯示，速度快（Google Translate）
- **整句翻譯**：累積後 Gemini AI 翻譯，品質高，彈出專用視窗
- **說話人辨識**：自動標記 【A】/【B】 說話人，不同聲音播放
- **中文語音輸出**：Windows SAPI5 TTS，延遲 < 0.1s
- **翻譯存檔**：每次執行自動存 `translation_output/trans_*.txt`

## 安裝

```bash
pip install faster-whisper sounddevice soundfile numpy deep-translator python-dotenv google-genai
```

URL 模式另需 `ffmpeg` 和 `yt-dlp` 加入 PATH。

## 設定

建立 `.env`：
```
GEMINI_API_KEY=your_gemini_api_key
```

Gemini API Key 免費申請：https://aistudio.google.com/

## 執行

```bash
python PY_auto_translate.py
```

或雙擊 `run_translate.bat`（Windows）。

## 前置條件（Windows）

- 音效卡設定 → 錄音 → 啟用「立體聲混音」裝置
- Python 3.11+

## 技術架構

| 元件 | 工具 |
|------|------|
| STT | faster-whisper tiny (CPU) |
| 快速翻譯 | Google Translate (deep-translator) |
| AI 翻譯 | Gemini 2.0 Flash |
| TTS | Windows SAPI5 (PowerShell) |
| 音訊擷取 | sounddevice WASAPI |
| GUI | tkinter |
