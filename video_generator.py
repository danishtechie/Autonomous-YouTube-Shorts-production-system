"""
video_generator.py — Automated video assembly engine.

Pipeline:
  1. Parse script into timed segments
  2. Fetch relevant stock footage (Pexels primary, Pixabay fallback)
  3. Download and validate clips
  4. Crop to 9:16 vertical format
  5. Stitch clips with smooth transitions
  6. Sync clip timing to audio duration
  7. Export vertical MP4 optimized for Shorts

All FFmpeg-based. No manual editing required.
"""

import os
import re
import json
import math
import time
import shutil
import hashlib
import requests
import subprocess
import tempfile
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

import config


# ── Constants ─────────────────────────────────────────────────────────────────
SHORTS_WIDTH  = 1080
SHORTS_HEIGHT = 1920
MIN_CLIP_DURATION = 3.0   # seconds
MAX_CLIP_DURATION = 8.0   # seconds
CLIP_CACHE_DIR = Path(".clip_cache")
CLIP_CACHE_DIR.mkdir(exist_ok=True)


class FootageFetcher:
    """Fetches relevant stock video clips from Pexels and Pixabay."""

    PEXELS_BASE  = "https://api.pexels.com/videos"
    PIXABAY_BASE = "https://pixabay.com/api/videos"

    def __init__(self):
        self.pexels_key  = config.PEXELS_API_KEY
        self.pixabay_key = config.PIXABAY_API_KEY

    def _cache_key(self, query: str, index: int) -> str:
        return hashlib.md5(f"{query}_{index}".encode()).hexdigest()[:12]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15))
    def _search_pexels(self, query: str, count: int = 5) -> List[str]:
        if not self.pexels_key:
            return []
        headers = {"Authorization": self.pexels_key}
        params = {
            "query": query,
            "per_page": count,
            "size": "medium",
            "orientation": "portrait",  # prefer vertical
        }
        resp = requests.get(
            f"{self.PEXELS_BASE}/search",
            headers=headers, params=params, timeout=15
        )
        resp.raise_for_status()
        videos = resp.json().get("videos", [])

        urls = []
        for v in videos:
            # Prefer HD portrait videos
            files = sorted(
                v.get("video_files", []),
                key=lambda f: (
                    f.get("height", 0),     # taller = more vertical
                    f.get("quality") == "hd",
                    -abs(f.get("width", 0) / max(f.get("height", 1), 1) - 9/16)
                ),
                reverse=True
            )
            if files:
                urls.append(files[0]["link"])
        return urls

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15))
    def _search_pixabay(self, query: str, count: int = 5) -> List[str]:
        if not self.pixabay_key:
            return []
        params = {
            "key": self.pixabay_key,
            "q": query,
            "video_type": "all",
            "per_page": count,
            "safesearch": "true",
        }
        resp = requests.get(self.PIXABAY_BASE, params=params, timeout=15)
        resp.raise_for_status()
        hits = resp.json().get("hits", [])

        urls = []
        for h in hits:
            videos = h.get("videos", {})
            # Prefer medium quality for balance of quality/speed
            for quality in ["medium", "large", "small"]:
                if quality in videos and videos[quality].get("url"):
                    urls.append(videos[quality]["url"])
                    break
        return urls

    def download_clip(self, url: str, dest: Path) -> bool:
        """Download a single video clip with caching."""
        cache_path = CLIP_CACHE_DIR / (hashlib.md5(url.encode()).hexdigest()[:12] + ".mp4")

        if cache_path.exists() and cache_path.stat().st_size > 50_000:
            shutil.copy(cache_path, dest)
            logger.debug(f"Clip from cache: {cache_path.name}")
            return True

        try:
            resp = requests.get(url, timeout=30, stream=True,
                                headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()

            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)

            # Validate it's a real video
            if dest.stat().st_size < 10_000:
                dest.unlink(missing_ok=True)
                return False

            shutil.copy(dest, cache_path)  # cache for reuse
            return True
        except Exception as e:
            logger.warning(f"Clip download failed ({url[:60]}...): {e}")
            dest.unlink(missing_ok=True)
            return False

    def fetch_clips(self, keywords: List[str], total_needed: int,
                    tmp_dir: Path) -> List[Path]:
        """
        Fetch video clips for a set of keywords.
        Returns list of downloaded clip paths.
        """
        all_urls = []

        for kw in keywords[:4]:
            logger.debug(f"Searching footage: '{kw}'")
            pexels_urls = self._search_pexels(kw, count=3)
            pixabay_urls = self._search_pixabay(kw, count=2) if not pexels_urls else []
            all_urls.extend(pexels_urls)
            all_urls.extend(pixabay_urls)
            time.sleep(0.5)

        if not all_urls:
            logger.warning("No footage found from APIs — will use color backgrounds")
            return []

        # Deduplicate and shuffle for variety
        seen = set()
        unique_urls = []
        for u in all_urls:
            if u not in seen:
                seen.add(u)
                unique_urls.append(u)

        clips = []
        for i, url in enumerate(unique_urls[:total_needed + 3]):
            dest = tmp_dir / f"clip_{i:03d}.mp4"
            if self.download_clip(url, dest):
                clips.append(dest)
            if len(clips) >= total_needed:
                break

        logger.info(f"Downloaded {len(clips)}/{total_needed} clips")
        return clips


class VideoEngine:
    """Main video assembly engine using FFmpeg."""

    def __init__(self):
        self.fetcher = FootageFetcher()
        self.output_dir = config.VIDEO_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._check_ffmpeg()

    def _check_ffmpeg(self):
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError("FFmpeg not found. Install with: apt-get install ffmpeg")

    def _run_ffmpeg(self, cmd: List[str], desc: str = "FFmpeg") -> bool:
        """Run an FFmpeg command with error handling."""
        logger.debug(f"FFmpeg: {' '.join(cmd[:8])}...")
        result = subprocess.run(
            ["ffmpeg", "-y"] + cmd,
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            logger.error(f"{desc} failed:\n{result.stderr[-500:]}")
            return False
        return True

    def _get_video_info(self, path: Path) -> Dict:
        """Get video metadata via ffprobe."""
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams", "-show_format",
            str(path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {}
        try:
            return json.loads(result.stdout)
        except Exception:
            return {}

    def _get_video_duration(self, path: Path) -> float:
        info = self._get_video_info(path)
        try:
            return float(info["format"]["duration"])
        except Exception:
            return 0.0

    def _process_clip(self, clip: Path, duration: float,
                      output: Path, start_offset: float = 0) -> bool:
        """
        Crop clip to 9:16, trim to duration, apply subtle zoom.
        """
        filter_chain = (
            # Crop to portrait 9:16 from center
            f"crop=ih*9/16:ih:(iw-ih*9/16)/2:0,"
            # Scale to target resolution
            f"scale={SHORTS_WIDTH}:{SHORTS_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={SHORTS_WIDTH}:{SHORTS_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
            # Subtle zoom for dynamism (Ken Burns effect)
            f"zoompan=z='min(zoom+0.001,1.05)':d={int(duration*25)}:s={SHORTS_WIDTH}x{SHORTS_HEIGHT}:fps=30,"
            # Ensure smooth framerate
            f"fps=30"
        )

        return self._run_ffmpeg([
            "-ss", str(start_offset),
            "-i", str(clip),
            "-t", str(duration),
            "-vf", filter_chain,
            "-an",  # no audio (we use voiceover)
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            str(output),
        ], f"Process clip {clip.name}")

    def _create_color_clip(self, duration: float, output: Path,
                           color: str = "0x1a1a2e") -> bool:
        """Create a solid color background clip as fallback."""
        return self._run_ffmpeg([
            "-f", "lavfi",
            "-i", f"color=c={color}:size={SHORTS_WIDTH}x{SHORTS_HEIGHT}:rate=30:duration={duration}",
            "-c:v", "libx264",
            "-preset", "fast",
            str(output),
        ], "Create color clip")

    def _calculate_segment_durations(self, script: Dict,
                                     audio_duration: float) -> List[Dict]:
        """
        Split audio duration across script segments.
        Returns list of {keyword, duration} dicts.
        """
        # Extract meaningful visual keywords from script
        body_text = f"{script.get('hook', '')} {script.get('body', '')} {script.get('cta', '')}"
        keywords = script.get("search_keywords", [])

        if not keywords:
            # Auto-extract keywords: nouns and key phrases
            words = re.findall(r'\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)?\b', body_text)
            keywords = list(dict.fromkeys(words))[:5]  # unique, preserve order
            if not keywords:
                keywords = ["nature", "city", "technology", "people", "abstract"]

        # Segment count: roughly one clip every 5-7 seconds
        num_segments = max(3, min(10, math.ceil(audio_duration / 6)))
        base_duration = audio_duration / num_segments

        segments = []
        for i in range(num_segments):
            kw = keywords[i % len(keywords)]
            # Slight variation in clip durations
            dur = base_duration + (0.5 if i % 2 == 0 else -0.5)
            dur = max(MIN_CLIP_DURATION, min(MAX_CLIP_DURATION, dur))
            segments.append({"keyword": kw, "duration": dur})

        return segments

    def _concat_clips(self, clip_paths: List[Path], output: Path) -> bool:
        """Concatenate processed clips using FFmpeg concat filter."""
        if not clip_paths:
            return False

        if len(clip_paths) == 1:
            shutil.copy(clip_paths[0], output)
            return True

        # Create concat list file
        list_file = output.parent / "concat_list.txt"
        with open(list_file, "w") as f:
            for p in clip_paths:
                f.write(f"file '{p.absolute()}'\n")

        success = self._run_ffmpeg([
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            str(output),
        ], "Concat clips")

        list_file.unlink(missing_ok=True)
        return success

    def _add_fade_transitions(self, clip_paths: List[Path],
                               durations: List[float],
                               output: Path) -> bool:
        """
        Apply crossfade transitions between clips using xfade filter.
        More sophisticated than simple concat.
        """
        if len(clip_paths) == 1:
            shutil.copy(clip_paths[0], output)
            return True

        if len(clip_paths) > 8:
            # Too many clips for xfade filter — use concat instead
            return self._concat_clips(clip_paths, output)

        fade_duration = 0.3  # seconds
        inputs = []
        for p in clip_paths:
            inputs.extend(["-i", str(p)])

        # Build xfade filter chain
        filter_parts = []
        prev = "[0:v]"
        cumulative_offset = 0

        for i in range(1, len(clip_paths)):
            cumulative_offset += durations[i-1] - fade_duration
            out_label = f"[v{i}]" if i < len(clip_paths) - 1 else ""
            filter_parts.append(
                f"{prev}[{i}:v]xfade=transition=fade:"
                f"duration={fade_duration}:offset={cumulative_offset:.2f}"
                f"{out_label}"
            )
            prev = f"[v{i}]"

        filter_str = ";".join(filter_parts)

        return self._run_ffmpeg(
            inputs + [
                "-filter_complex", filter_str,
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                str(output),
            ],
            "Add transitions"
        )

    # ── Main generate method ──────────────────────────────────────────────────

    def generate(self, script: Dict, audio_path: str,
                 audio_duration: float, video_id: int) -> Optional[str]:
        """
        Full video generation pipeline.
        Returns path to final video or None on failure.
        """
        logger.info(f"🎬 Generating video for: {script['title']}")

        with tempfile.TemporaryDirectory(prefix="yt_shorts_") as tmp_str:
            tmp = Path(tmp_str)

            # 1. Calculate segments
            segments = self._calculate_segment_durations(script, audio_duration)
            logger.info(f"  Segments: {len(segments)}, total: {sum(s['duration'] for s in segments):.1f}s")

            # 2. Collect all needed keywords
            all_keywords = list({s["keyword"] for s in segments})

            # 3. Fetch footage
            raw_clips = self.fetcher.fetch_clips(
                all_keywords,
                total_needed=len(segments),
                tmp_dir=tmp / "raw"
            )
            (tmp / "raw").mkdir(exist_ok=True)

            # 4. Process each segment
            processed_clips = []
            clip_durations = []

            for i, seg in enumerate(segments):
                out = tmp / f"seg_{i:03d}.mp4"

                if i < len(raw_clips):
                    clip = raw_clips[i % len(raw_clips)]
                    clip_duration = self._get_video_duration(clip)

                    # Random start offset for variety
                    max_offset = max(0, clip_duration - seg["duration"] - 1)
                    start = (i * 3.7) % max_offset if max_offset > 0 else 0

                    success = self._process_clip(clip, seg["duration"], out, start)
                else:
                    # Fallback: color background
                    colors = ["0x1a1a2e", "0x16213e", "0x0f3460", "0x533483"]
                    success = self._create_color_clip(
                        seg["duration"], out, colors[i % len(colors)]
                    )

                if success and out.exists():
                    processed_clips.append(out)
                    clip_durations.append(seg["duration"])
                else:
                    logger.warning(f"Segment {i} failed, skipping")

            if not processed_clips:
                logger.error("No video segments generated!")
                return None

            # 5. Concat / transition
            video_no_audio = tmp / "video_no_audio.mp4"
            logger.info("🔗 Stitching clips...")
            if not self._add_fade_transitions(processed_clips, clip_durations,
                                               video_no_audio):
                if not self._concat_clips(processed_clips, video_no_audio):
                    logger.error("Clip assembly failed")
                    return None

            # 6. Final render: combine video + audio, trim to audio length
            timestamp = int(time.time())
            final_path = self.output_dir / f"video_{video_id}_{timestamp}_raw.mp4"

            logger.info("🎵 Combining video + audio...")
            success = self._run_ffmpeg([
                "-i", str(video_no_audio),
                "-i", audio_path,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-t", str(audio_duration),
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                str(final_path),
            ], "Merge audio+video")

            if not success or not final_path.exists():
                logger.error("Final merge failed")
                return None

            # Validate output
            output_duration = self._get_video_duration(final_path)
            logger.success(
                f"✅ Video ready: {final_path.name} "
                f"({output_duration:.1f}s, {final_path.stat().st_size // (1024*1024):.1f}MB)"
            )
            return str(final_path)


# ── CLI entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    engine = VideoEngine()
    test_script = {
        "title": "Test Video",
        "hook": "Your brain is lying to you",
        "body": "Every single day, your brain constructs a false reality.",
        "cta": "Follow for more mind-bending facts.",
        "search_keywords": ["brain", "neuroscience", "mind", "psychology"],
    }
    result = engine.generate(
        script=test_script,
        audio_path="test_audio.mp3",
        audio_duration=35.0,
        video_id=999,
    )
    print(f"Video: {result}")
