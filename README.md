# 🤖 YouTube Shorts Automation Bot

**Fully autonomous AI pipeline that generates and uploads 2 viral YouTube Shorts per day.**

Zero human intervention after setup.

---

## What It Does

Every day, automatically:

1. **Detects trends** — Reddit, Google Trends, YouTube Trending
2. **Writes scripts** — LLM with virality scoring (hook strength, retention pacing, emotional triggers)
3. **Records voiceover** — ElevenLabs TTS with professional audio normalization
4. **Assembles video** — Stock footage from Pexels, 9:16 vertical format, smooth transitions
5. **Burns subtitles** — Whisper when available, otherwise script-based timing fallback
6. **Uploads to YouTube** — Optimized title, description, tags, scheduled for peak engagement

## Quick Start

```bash
cp .env.template .env     # Add your API keys
python -m pip install -r requirements.txt
python uploader.py --setup-oauth   # One-time YouTube auth
python scheduler.py --now --dry-run    # Test without uploading
python scheduler.py                    # Start daily automation
```

See [SETUP.md](SETUP.md) for full instructions.

## Project Structure

```
yt_shorts_bot/
├── trend_engine.py      # Multi-source trend detection
├── script_generator.py  # LLM script generation + scoring
├── voice_generator.py   # ElevenLabs TTS
├── video_generator.py   # FFmpeg video assembly
├── subtitle_engine.py   # Whisper + ASS subtitle burn-in
├── uploader.py          # YouTube Data API upload
├── pipeline.py          # Main orchestrator
├── scheduler.py         # Daily automation
├── database.py          # SQLite persistence
├── config.py            # Centralized config
├── requirements.txt
├── .env.template
├── Dockerfile
└── SETUP.md
```

## Estimated Monthly Cost: ~$12

| ElevenLabs Starter | $5 |
| Anthropic API | $0.50 |
| VPS (Hetzner) | $6 |

## License

MIT
