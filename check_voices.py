import asyncio
import edge_tts

async def main():
    voices = await edge_tts.list_voices()
    zh_tw = [v for v in voices if v['Locale'].startswith('zh-TW')]
    for v in zh_tw:
        print(v['ShortName'], '-', v['Gender'])

asyncio.run(main())
