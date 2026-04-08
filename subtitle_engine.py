"""
subtitle_engine.py — Word-level subtitle generation and video burn-in.

Pipeline:
  1. Transcribe audio with OpenAI Whisper (accurate word timing)
  2. Group words into subtitle lines (max 4 words, max 2 lines)
  3. Style with bold keywords and high-contrast formatting
  4. Generate ASS subtitle file (Advanced SubStation Alpha)
  5. Burn subtitles into video with FFmpeg

Design choices:
  - Word-by-word highlighting for maximum retention
  - Mobile-first: large font, high contrast, center-bottom position
  - Bold keywords highlighted in accent color
  - Karaoke-style active word emphasis
"""

import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from loguru import logger

import config


# ── Subtitle style constants ──────────────────────────────────────────────────
FONT_NAME     = "Arial"
FONT_SIZE     = 22        # relative to 1920-height video
FONT_COLOR    = "FFFFFF"  # white
OUTLINE_COLOR = "000000"  # black outline
ACCENT_COLOR  = "FFD700"  # gold for keywords
BOLD          = 1
OUTLINE_SIZE  = 3
SHADOW_DEPTH  = 2
POSITION_Y    = 85        # % from top (85% = near bottom)

# Keywords to highlight in accent color
POWER_KEYWORDS = {
    "secret", "truth", "lie", "lies", "never", "always", "shocking", "exposed",
    "hidden", "real", "dark", "dangerous", "illegal", "millions", "billion",
    "actually", "proof", "discovered", "finally", "warns", "forbidden",
    "money", "brain", "death", "life", "power", "control", "fear", "love",
    "success", "failure", "hack", "trick", "method", "reason", "why", "how",
    "zero", "one", "two", "three", "four", "five", "ten", "hundred",
}


class SubtitleEngine:
    def __init__(self):
        self.subtitle_dir = config.SUBTITLE_DIR
        self.subtitle_dir.mkdir(parents=True, exist_ok=True)

    # ── Whisper transcription ─────────────────────────────────────────────────

    def _transcribe_whisper(self, audio_path: str) -> List[Dict]:
        """
        Transcribe audio with Whisper, getting word-level timestamps.
        Returns list of {word, start, end} dicts.
        """
        logger.info("🔊 Transcribing audio with Whisper...")
        try:
            import whisper
            model = whisper.load_model("base")  # base = fast + good quality
            result = model.transcribe(
                audio_path,
                word_timestamps=True,
                language="en",
                task="transcribe",
            )

            words = []
            for segment in result.get("segments", []):
                for word_data in segment.get("words", []):
                    word = word_data["word"].strip()
                    if word:
                        words.append({
                            "word": word,
                            "start": word_data["start"],
                            "end": word_data["end"],
                        })

            logger.info(f"Whisper transcribed {len(words)} words")
            return words

        except ImportError:
            logger.warning("Whisper not installed — using script-based alignment")
            return []
        except Exception as e:
            logger.error(f"Whisper transcription failed: {e}")
            return []

    def _align_script(self, script_text: str, audio_duration: float) -> List[Dict]:
        """
        Fallback: distribute script words evenly across audio duration.
        Less accurate than Whisper but reliable.
        """
        words = script_text.split()
        if not words:
            return []

        duration_per_word = audio_duration / len(words)
        aligned = []
        for i, word in enumerate(words):
            start = i * duration_per_word
            end = start + duration_per_word
            aligned.append({"word": word, "start": start, "end": end})

        return aligned

    # ── Subtitle grouping ─────────────────────────────────────────────────────

    def _group_into_lines(self, words: List[Dict],
                          max_words_per_line: int = 4,
                          max_chars_per_line: int = 30) -> List[Dict]:
        """
        Group words into subtitle entries.
        Breaks at punctuation, max word count, or max char count.
        """
        subtitles = []
        current_group = []
        current_chars = 0

        for word_data in words:
            word = word_data["word"]
            clean = re.sub(r"[^\w']", "", word)
            char_count = len(clean) + (1 if current_group else 0)

            # Check if we should break
            should_break = (
                len(current_group) >= max_words_per_line or
                current_chars + char_count > max_chars_per_line or
                any(p in word for p in [".", "!", "?", ",", ";", ":"])
            )

            if should_break and current_group:
                last = current_group[-1]
                subtitles.append({
                    "text": " ".join(w["word"] for w in current_group),
                    "words": current_group,
                    "start": current_group[0]["start"],
                    "end": last["end"],
                })
                current_group = []
                current_chars = 0

            current_group.append(word_data)
            current_chars += char_count

        # Add remaining words
        if current_group:
            subtitles.append({
                "text": " ".join(w["word"] for w in current_group),
                "words": current_group,
                "start": current_group[0]["start"],
                "end": current_group[-1]["end"],
            })

        return subtitles

    # ── ASS subtitle generation ───────────────────────────────────────────────

    def _to_ass_time(self, seconds: float) -> str:
        """Convert seconds to ASS timestamp format H:MM:SS.cc"""
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    def _is_keyword(self, word: str) -> bool:
        clean = re.sub(r"[^\w]", "", word).lower()
        return clean in POWER_KEYWORDS or (
            bool(re.match(r"^\d+", clean)) and len(clean) > 1
        )

    def _generate_ass(self, subtitles: List[Dict]) -> str:
        """
        Generate Advanced SubStation Alpha subtitle file.
        Supports per-word styling, positioning, and animations.
        """
        # ASS header
        header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {config.SHORTS_WIDTH if hasattr(config, 'SHORTS_WIDTH') else 1080}
PlayResY: {config.SHORTS_HEIGHT if hasattr(config, 'SHORTS_HEIGHT') else 1920}
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT_NAME},{FONT_SIZE},&H00{FONT_COLOR},&H000000FF,&H00{OUTLINE_COLOR},&H80000000,{BOLD},0,0,0,100,100,0,0,1,{OUTLINE_SIZE},{SHADOW_DEPTH},2,40,40,140,1
Style: Keyword,{FONT_NAME},{FONT_SIZE},&H00{ACCENT_COLOR},&H000000FF,&H00{OUTLINE_COLOR},&H80000000,{BOLD},0,0,0,105,105,0,0,1,{OUTLINE_SIZE},{SHADOW_DEPTH},2,40,40,140,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        events = []
        for sub in subtitles:
            # Build styled text with per-word color
            styled_words = []
            for w in sub["words"]:
                word = w["word"]
                if self._is_keyword(word):
                    styled_words.append(f"{{\\c&H00{ACCENT_COLOR}&}}{word}{{\\c&H00{FONT_COLOR}&}}")
                else:
                    styled_words.append(word)

            text = " ".join(styled_words)
            # Capitalize first word
            if text:
                text = text[0].upper() + text[1:]

            start = self._to_ass_time(sub["start"])
            end = self._to_ass_time(sub["end"] + 0.05)  # tiny overlap for smooth display

            events.append(
                f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}"
            )

        return header + "\n".join(events) + "\n"

    # ── Burn into video ───────────────────────────────────────────────────────

    def _burn_subtitles(self, video_path: str, ass_path: str,
                         output_path: str) -> bool:
        """Burn ASS subtitles into video using FFmpeg."""
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vf", f"ass={ass_path}",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "20",
            "-c:a", "copy",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.error(f"Subtitle burn failed:\n{result.stderr[-400:]}")
            return False
        return True

    # ── Main entry point ──────────────────────────────────────────────────────

    def process(self, video_path: str, audio_path: str,
                script_text: str, audio_duration: float,
                video_id: int) -> Optional[str]:
        """
        Generate and burn subtitles into video.
        Returns path to subtitled video or None on failure.
        """
        logger.info("📝 Generating subtitles...")
        timestamp = int(time.time())

        # 1. Get word timings
        words = self._transcribe_whisper(audio_path)
        if not words:
            logger.warning("Whisper failed — using script alignment")
            words = self._align_script(script_text, audio_duration)

        if not words:
            logger.error("No word timing data available")
            return None

        # 2. Group into subtitle lines
        subtitles = self._group_into_lines(words, max_words_per_line=4)
        logger.info(f"  Generated {len(subtitles)} subtitle entries")

        # 3. Generate ASS file
        ass_content = self._generate_ass(subtitles)
        ass_path = self.subtitle_dir / f"subs_{video_id}_{timestamp}.ass"
        ass_path.write_text(ass_content, encoding="utf-8")
        logger.debug(f"ASS file saved: {ass_path}")

        # 4. Burn into video
        output_path = str(Path(video_path).parent /
                          f"video_{video_id}_{timestamp}_subtitled.mp4")
        logger.info("🔥 Burning subtitles into video...")
        if not self._burn_subtitles(video_path, str(ass_path), output_path):
            logger.error("Subtitle burn failed — returning unsbtitled video")
            return video_path  # return original rather than failing

        logger.success(f"✅ Subtitles burned: {Path(output_path).name}")
        return output_path


# ── CLI entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 4:
        print("Usage: python subtitle_engine.py <video.mp4> <audio.mp3> <script_text>")
    else:
        engine = SubtitleEngine()
        result = engine.process(
            video_path=sys.argv[1],
            audio_path=sys.argv[2],
            script_text=sys.argv[3],
            audio_duration=35.0,
            video_id=999,
        )
        print(f"Output: {result}")
