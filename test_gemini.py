import os
from dotenv import load_dotenv
from google import genai

load_dotenv('.env')
key = os.getenv("GEMINI_API_KEY", "")
print(f"Key 長度: {len(key)}")

client = genai.Client(api_key=key)
resp = client.models.generate_content(
    model="gemini-2.0-flash",
    contents="翻譯成繁體中文，只輸出翻譯：We need water to survive, and many regions of the world are running out."
)
print("翻譯結果:", resp.text.strip())
