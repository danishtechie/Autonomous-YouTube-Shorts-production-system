"""
voice_generator.py — High-quality TTS with ElevenLabs (primary) and fallback.

Features:
  - ElevenLabs API integration (primary — highest quality)
  - gTTS fallback (free, lower quality)
  - Audio normalization and cleanup with FFmpeg
  - Returns path to clean MP3 + audio metadata
"""

import os
import re
import json
import time
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Optional
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

import config


class VoiceGenerator:
    def __init__(self):
        self.output_dir = config.AUDIO_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ── ElevenLabs ────────────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
    def _generate_elevenlabs(self, text: str, output_path: Path) -> bool:
        """Generate voice using ElevenLabs API."""
        import requests

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{config.ELEVENLABS_VOICE_ID}"
        headers = {
            "Accept": "audio/mpeg",
            "xi-api-key": config.ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
        }
        payload = {
            "text": text,
            "model_id": "eleven_turbo_v2",   # fast + high quality
            "voice_settings": {
                "stability": 0.45,            # some variation = more natural
                "similarity_boost": 0.85,
                "style": 0.20,               # slight expressiveness
                "use_speaker_boost": True,
            },
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=60)

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 30))
            logger.warning(f"ElevenLabs rate limited, waiting {retry_after}s")
            time.sleep(retry_after)
            raise Exception("Rate limited")

        if resp.status_code != 200:
            raise Exception(f"ElevenLabs error {resp.status_code}: {resp.text[:200]}")

        output_path.write_bytes(resp.content)
        logger.success(f"ElevenLabs audio saved: {output_path}")
        return True

    # ── gTTS fallback ─────────────────────────────────────────────────────────

    def _generate_gtts(self, text: str, output_path: Path) -> bool:
        """Fallback TTS using Google Text-to-Speech (gTTS)."""
        try:
            from gtts import gTTS
            tts = gTTS(text=text, lang="en", slow=False, tld="com")
            tmp = output_path.with_suffix(".tmp.mp3")
            tts.save(str(tmp))
            tmp.rename(output_path)
            logger.info(f"gTTS audio saved: {output_path}")
            return True
        except Exception as e:
            logger.error(f"gTTS failed: {e}")
            return False

    # ── Audio post-processing ─────────────────────────────────────────────────

    def _normalize_audio(self, input_path: Path, output_path: Path) -> bool:
        """
        Apply audio normalization, noise reduction, and format standardization.
        Target: -16 LUFS (YouTube standard), 48kHz, stereo.
        """
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-af", (
                "loudnorm=I=-16:TP=-1.5:LRA=11,"  # LUFS normalization
                "highpass=f=80,"                    # remove low rumble
                "lowpass=f=12000,"                  # remove harsh highs
                "acompressor=threshold=-24dB:ratio=3:attack=2:release=50"  # compress peaks
            ),
            "-ar", "48000",
            "-ac", "2",
            "-codec:a", "libmp3lame",
            "-q:a", "2",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logger.error(f"Audio normalization failed: {result.stderr[-300:]}")
            return False
        return True

    def _get_audio_duration(self, audio_path: Path) -> float:
        """Get duration of audio file in seconds."""
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            str(audio_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return 0.0
        try:
            data = json.loads(result.stdout)
            return float(data["streams"][0].get("duration", 0))
        except Exception:
            return 0.0

    # ── Text preprocessing ────────────────────────────────────────────────────

    def _preprocess_text(self, text: str) -> str:
        """
        Clean script text for optimal TTS output.
        Remove markdown, normalize punctuation for speech rhythm.
        """
        # Remove markdown formatting
        text = re.sub(r"\*+([^*]+)\*+", r"\1", text)   # bold/italic
        text = re.sub(r"_([^_]+)_", r"\1", text)        # underline
        text = re.sub(r"#+\s", "", text)                 # headers

        # Normalize punctuation for pauses
        text = re.sub(r"—", ", ", text)                  # em-dash → comma pause
        text = re.sub(r"\.\.\.", "...", text)             # ellipsis stays
        text = re.sub(r"([.!?])\s+", r"\1 ", text)       # normalize spacing after sentence end

        # Expand common abbreviations for TTS
        abbrevs = {
            "vs.": "versus",
            "Dr.": "Doctor",
            "Mr.": "Mister",
            "Mrs.": "Misses",
            "St.": "Saint",
            "&": "and",
            "%": "percent",
        }
        for abbr, full in abbrevs.items():
            text = text.replace(abbr, full)

        # Remove excess whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r" {2,}", " ", text)

        return text.strip()

    # ── Main entry point ──────────────────────────────────────────────────────

    def generate(self, script: Dict, video_id: int) -> Optional[Dict]:
        """
        Generate voiceover for a script.
        Returns dict with audio metadata or None on failure.
        """
        text = self._preprocess_text(script["full_text"])
        filename = f"audio_{video_id}_{int(time.time())}"
        raw_path = self.output_dir / f"{filename}_raw.mp3"
        final_path = self.output_dir / f"{filename}.mp3"

        logger.info(f"🎙️ Generating voice ({len(text.split())} words)...")

        # Try ElevenLabs first
        success = False
        if config.ELEVENLABS_API_KEY:
            try:
                success = self._generate_elevenlabs(text, raw_path)
                logger.info("Voice source: ElevenLabs")
            except Exception as e:
                logger.warning(f"ElevenLabs failed: {e}, falling back to gTTS")

        # Fallback to gTTS
        if not success:
            try:
                success = self._generate_gtts(text, raw_path)
                logger.info("Voice source: gTTS (fallback)")
            except Exception as e:
                logger.error(f"gTTS fallback also failed: {e}")
                return None

        if not success or not raw_path.exists():
            logger.error("Audio generation failed — no file produced")
            return None

        # Normalize audio
        logger.info("🔊 Normalizing audio...")
        if self._normalize_audio(raw_path, final_path):
            raw_path.unlink(missing_ok=True)  # cleanup raw
        else:
            # Use raw if normalization fails
            logger.warning("Normalization failed, using raw audio")
            raw_path.rename(final_path)

        duration = self._get_audio_duration(final_path)
        file_size = final_path.stat().st_size

        logger.success(
            f"✅ Audio ready: {final_path.name} "
            f"({duration:.1f}s, {file_size // 1024}KB)"
        )

        return {
            "audio_path": str(final_path),
            "duration": duration,
            "file_size": file_size,
            "word_count": len(text.split()),
        }


# ── CLI entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    gen = VoiceGenerator()
    test_script = {
        "full_text": (
            "Your phone is designed to be addictive. Not by accident — by intent. "
            "In 2007, Apple filed a patent for infinite scroll. "
            "The same mechanism used in slot machines. "
            "But here's what they didn't tell you: every notification "
            "gives you a 0.5 second dopamine hit — the same as cocaine. "
            "Your brain literally cannot resist it. "
            "And the apps know exactly how long to wait before sending another. "
            "The average person checks their phone 96 times a day. "
            "That's once every 10 minutes. Every single day. "
            "Delete the apps off your home screen. See what happens. "
            "Follow for more things they never taught you."
        )
    }
    result = gen.generate(test_script, video_id=999)
    if result:
        print(f"Audio: {result['audio_path']}")
        print(f"Duration: {result['duration']}s")
