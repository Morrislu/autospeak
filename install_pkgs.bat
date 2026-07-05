@echo off
echo 安裝套件中...
pip install "googletrans==4.0.0rc1" edge-tts sounddevice soundfile > install_log.txt 2>&1
echo 安裝完成，檢查結果：
python check_env.py
pause
