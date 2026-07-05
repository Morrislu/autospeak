@echo off
cd /d D:\Morris\under_cook\0703_auto_speaking_traslation

:: 確認套件
python -c "import faster_whisper" 2>nul || (
    echo 安裝 faster-whisper...
    python -m pip install faster-whisper
)
python -c "import deep_translator" 2>nul || (
    echo 安裝 deep-translator...
    python -m pip install deep-translator
)
python -c "import edge_tts" 2>nul || (
    echo 安裝 edge-tts...
    python -m pip install edge-tts
)
python -c "import sounddevice" 2>nul || (
    echo 安裝 sounddevice...
    python -m pip install sounddevice soundfile
)
python -c "import youtube_transcript_api" 2>nul || (
    echo 安裝 youtube-transcript-api...
    python -m pip install youtube-transcript-api
)

:: 啟動
python PY_auto_translate.py
pause
