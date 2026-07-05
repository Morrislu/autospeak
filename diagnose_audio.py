"""診斷下載音訊的音量分佈"""
import sys
import numpy as np
import soundfile as sf

wav_path = sys.argv[1] if len(sys.argv) > 1 else input("WAV 路徑: ")
data, sr = sf.read(wav_path)
if data.ndim > 1:
    data = data.mean(axis=1)
data = data.astype(np.float32)

FRAME = 160
rms_values = [
    float(np.sqrt(np.mean(data[i:i+FRAME]**2)))
    for i in range(0, len(data)-FRAME, FRAME*10)  # 每100ms取樣
]
rms_values = [r for r in rms_values if r > 0]

print(f"音訊長度: {len(data)/sr:.1f} 秒")
print(f"RMS 最大: {max(rms_values):.5f}")
print(f"RMS 平均: {sum(rms_values)/len(rms_values):.5f}")
print(f"RMS 中位: {sorted(rms_values)[len(rms_values)//2]:.5f}")
print(f"目前 ENERGY_THRESH: 0.008")
print(f"建議 ENERGY_THRESH: {max(rms_values)*0.05:.5f}  (最大值的5%)")
