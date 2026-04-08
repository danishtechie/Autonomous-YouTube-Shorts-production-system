# YouTube Shorts Automation Bot — Setup & Deployment Guide

## Architecture Overview

```
trend_engine.py        → Detects viral topics (Reddit, Google Trends, YouTube)
    ↓
script_generator.py    → LLM generates high-retention scripts with scoring
    ↓
voice_generator.py     → ElevenLabs TTS → normalized MP3
    ↓
video_generator.py     → Stock footage (Pexels/Pixabay) + FFmpeg assembly
    ↓
subtitle_engine.py     → Whisper transcription + ASS subtitle burn-in
    ↓
uploader.py            → YouTube Data API v3 upload + metadata optimization
    ↓
scheduler.py           → Daily automation at configured times
```

---

## Prerequisites

- Python 3.11 recommended
- Python 3.10-3.12 supported for this dependency set
- Do not use Python 3.13+ for this project unless you update the pinned packages
- FFmpeg installed (`ffmpeg -version` should work)
- API keys for services below

---

## Step 1: Get Your API Keys

### Required APIs

| Service | Purpose | Free Tier | Link |
|---------|---------|-----------|------|
| Anthropic / OpenAI | Script generation | ~$5 credit | anthropic.com / openai.com |
| ElevenLabs | Voice generation | 10k chars/mo | elevenlabs.io |
| Pexels | Stock footage | Free | pexels.com/api |
| YouTube Data API | Upload videos | 10k units/day | console.cloud.google.com |

### Optional APIs

| Service | Purpose | Notes |
|---------|---------|-------|
| Reddit | Trend detection | Much better trends |
| Pixabay | Extra footage | Backup source |
| SerpAPI | Google Trends | Paid |

---

## Step 2: Installation

```bash
# Clone / download project
cd yt_shorts_bot

# Create virtual environment
# Linux/macOS:
python3 -m venv venv
source venv/bin/activate

# Windows PowerShell:
python -m venv venv
.\venv\Scripts\Activate.ps1

# Install dependencies
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Install FFmpeg
# Ubuntu/Debian:
sudo apt-get install ffmpeg

# macOS:
brew install ffmpeg

# Windows:
# Download from https://ffmpeg.org/download.html and add to PATH
```

### Windows notes

- If `python` is not recognized, install Python 3.11 from https://www.python.org/downloads/windows/
- During installation, enable `Add python.exe to PATH`
- After installation, restart PowerShell before running the commands above
- If PowerShell blocks activation, run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

- If you already created `venv` with Python 3.13+ or 3.14, delete that `venv` folder and recreate it with Python 3.11 before installing requirements
- `openai-whisper` is intentionally not included in `requirements.txt` because its Windows build is unreliable; subtitles still work with fallback timing, just with less precise word alignment

### Optional: Install Whisper later

If you want more accurate word-level subtitle timing, try installing Whisper separately after the main dependencies are working:

```powershell
python -m pip install --no-build-isolation openai-whisper==20231117
```

If that still fails on Windows, you can continue without it. The bot will use script-based subtitle alignment automatically.

---

## Step 3: Configure Environment

```bash
# Copy template
cp .env.template .env

# Edit with your API keys
nano .env   # or use any text editor
```

**Minimum required config:**
```env
ANTHROPIC_API_KEY=sk-ant-...   # OR OpenAI key
ELEVENLABS_API_KEY=...         # For voice (required for quality)
PEXELS_API_KEY=...             # For stock footage
YOUTUBE_CLIENT_ID=...          # Google OAuth
YOUTUBE_CLIENT_SECRET=...
NICHE=facts                    # Your content niche
```

---

## Step 4: YouTube OAuth Setup (IMPORTANT)

You must do this ONCE manually before automation works.

### 4a. Create Google Cloud Project
1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project
3. Enable **YouTube Data API v3**
4. Go to **Credentials** → Create **OAuth 2.0 Client ID**
5. Type: **Desktop App**
6. Download the credentials JSON
7. Copy `client_id` and `client_secret` to your `.env`

### 4b. Run OAuth flow
```bash
python uploader.py --setup-oauth
```
This opens a browser window → log in with your YouTube channel account → authorize.

The script saves a refresh token to `data/youtube_token.json`.

Copy the displayed `YOUTUBE_REFRESH_TOKEN` value to your `.env`.

---

## Step 5: Test Each Module

```bash
# Test trend detection
python trend_engine.py

# Test script generation (requires LLM API key)
python script_generator.py

# Test voice generation (requires ElevenLabs key)
python voice_generator.py

# Test full pipeline in dry-run mode (no upload)
python pipeline.py --dry-run --once
```

---

## Step 6: Run the Bot

### Local Development
```bash
# Single run now
python scheduler.py --now --dry-run    # dry run (no upload)
python scheduler.py --now              # real run + upload

# Start scheduler (runs at times in .env)
python scheduler.py
```

### Production (VPS)

#### Option A: systemd service (recommended)
```bash
# Create service file
sudo nano /etc/systemd/system/yt-shorts-bot.service
```

```ini
[Unit]
Description=YouTube Shorts Automation Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/yt_shorts_bot
Environment=PATH=/home/ubuntu/yt_shorts_bot/venv/bin
ExecStart=/home/ubuntu/yt_shorts_bot/venv/bin/python scheduler.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable yt-shorts-bot
sudo systemctl start yt-shorts-bot

# Check status
sudo systemctl status yt-shorts-bot
sudo journalctl -u yt-shorts-bot -f   # live logs
```

#### Option B: cron (simpler)
```bash
# Edit crontab
crontab -e

# Add (runs at 9am and 5pm daily):
0 9,17 * * * cd /home/ubuntu/yt_shorts_bot && /home/ubuntu/yt_shorts_bot/venv/bin/python pipeline.py >> logs/cron.log 2>&1
```

#### Option C: Docker
```bash
docker build -t yt-shorts-bot .
docker run -d \
  --name yt-shorts-bot \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/logs:/app/logs \
  --restart unless-stopped \
  yt-shorts-bot
```

---

## Recommended VPS Setup

**Minimum specs:**
- 2 CPU cores (video encoding is CPU-heavy)
- 4GB RAM (Whisper needs ~1GB)
- 40GB SSD (video files accumulate)
- Ubuntu 22.04 LTS

**Recommended providers:**
- DigitalOcean ($24/mo for 2CPU/4GB)
- Hetzner CX21 (€5.95/mo — best value)
- AWS t3.medium (~$30/mo)

---

## Monitoring

### Health Check
```bash
curl http://localhost:8888
# Returns: {"status":"running","last_run":"2024-01-15","today_uploads":1,...}
```

### View Logs
```bash
tail -f logs/bot_$(date +%Y-%m-%d).log
```

### Database Stats
```bash
python -c "
import database as db, sqlite3
conn = sqlite3.connect('data/bot.db')
print('Topics:', conn.execute('SELECT COUNT(*) FROM topics').fetchone()[0])
print('Scripts:', conn.execute('SELECT COUNT(*) FROM scripts').fetchone()[0])
print('Uploaded:', conn.execute('SELECT COUNT(*) FROM uploads').fetchone()[0])
"
```

---

## Troubleshooting

| Problem | Solution |
|---------|---------|
| `ffmpeg not found` | Install FFmpeg and add to PATH |
| `ElevenLabs 429` | Exceeded monthly quota — gTTS fallback activates |
| `YouTube quota exceeded` | YouTube API has 10k unit/day limit — retry tomorrow |
| `No footage found` | Check Pexels API key, or both APIs are down — color fallback activates |
| `Whisper OOM error` | Reduce Whisper model: change `"base"` to `"tiny"` in subtitle_engine.py |
| Videos too short | Increase `MAX_VIDEO_DURATION` in .env |
| Poor script quality | Increase `min_score` threshold in script_generator.py |

---

## Cost Estimate (Per Month)

| Service | Usage | Cost |
|---------|-------|------|
| Anthropic (Haiku) | 60 scripts × ~500 tokens | ~$0.50 |
| ElevenLabs (Starter) | ~2400 chars/day × 30 | $5/mo |
| Pexels | Free | $0 |
| VPS (Hetzner CX21) | 24/7 | ~$6 |
| **Total** | | **~$12/month** |

---

## Customization

### Change niche
Set `NICHE=psychology` in `.env` (options: facts, science, history, psychology, finance, motivation)

### Adjust virality threshold
In `script_generator.py`: change `self.min_score = 55.0` (higher = stricter)

### Different voice
Change `ELEVENLABS_VOICE_ID` in `.env` (find voice IDs at elevenlabs.io)

### Add new footage sources
Extend `FootageFetcher` class in `video_generator.py`

### Change subtitle style
Modify constants in `subtitle_engine.py` (FONT_SIZE, ACCENT_COLOR, etc.)
