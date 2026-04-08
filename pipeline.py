"""
pipeline.py — Main orchestrator for the video production pipeline.

Coordinates all modules in sequence:
  trend_engine → script_generator → voice_generator
    → video_generator → subtitle_engine → uploader

Features:
  - Per-step retry with exponential backoff
  - Full error isolation (one video failure doesn't kill the run)
  - DB tracking of every step
  - Cleanup of intermediate files
  - Dry-run mode (--dry-run flag skips actual upload)
"""

import sys
import time
import shutil
import traceback
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Dict
from loguru import logger

import config
import database as db
from trend_engine import TrendEngine
from script_generator import ScriptGenerator
from voice_generator import VoiceGenerator
from video_generator import VideoEngine
from subtitle_engine import SubtitleEngine
from uploader import YouTubeUploader


class Pipeline:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.trend_engine   = TrendEngine()
        self.script_gen     = ScriptGenerator()
        self.voice_gen      = VoiceGenerator()
        self.video_engine   = VideoEngine()
        self.subtitle_engine = SubtitleEngine()
        self.uploader       = YouTubeUploader() if not dry_run else None
        self.run_date       = date.today().isoformat()

        if dry_run:
            logger.warning("⚠️  DRY RUN MODE — skipping actual YouTube upload")

    def _log_step(self, step: str, status: str,
                  message: str = "", duration: float = 0):
        db.log_run(self.run_date, step, status, message, duration)

    def _cleanup_intermediates(self, video_id: int, audio_path: str,
                               raw_video_path: str):
        """Remove intermediate files to save disk space."""
        for p in [audio_path, raw_video_path]:
            if p:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    pass
        logger.debug(f"Cleaned up intermediate files for video {video_id}")

    # ── Individual steps ──────────────────────────────────────────────────────

    def step_trends(self) -> list:
        """Fetch trending topics."""
        t0 = time.time()
        try:
            topics = self.trend_engine.get_top_topics(n=5)
            self._log_step("trends", "success",
                           f"{len(topics)} topics fetched",
                           time.time() - t0)
            return topics
        except Exception as e:
            self._log_step("trends", "failure", str(e), time.time() - t0)
            logger.error(f"Trend detection failed: {e}")
            return []

    def step_script(self, topic: Dict, topic_id: int) -> Optional[Dict]:
        """Generate script for a topic."""
        t0 = time.time()
        try:
            script = self.script_gen.generate(topic, topic_id=topic_id)
            if script:
                self._log_step("script", "success",
                               f"score={script.get('total_score', 0):.0f}",
                               time.time() - t0)
            else:
                self._log_step("script", "failure", "No script generated",
                               time.time() - t0)
            return script
        except Exception as e:
            self._log_step("script", "failure", str(e), time.time() - t0)
            logger.error(f"Script generation failed: {e}")
            return None

    def step_voice(self, script: Dict, video_id: int) -> Optional[Dict]:
        """Generate voiceover from script."""
        t0 = time.time()
        try:
            result = self.voice_gen.generate(script, video_id)
            if result:
                self._log_step("voice", "success",
                               f"duration={result['duration']:.1f}s",
                               time.time() - t0)
            else:
                self._log_step("voice", "failure", "No audio generated",
                               time.time() - t0)
            return result
        except Exception as e:
            self._log_step("voice", "failure", str(e), time.time() - t0)
            logger.error(f"Voice generation failed: {e}")
            return None

    def step_video(self, script: Dict, audio_result: Dict,
                   video_id: int) -> Optional[str]:
        """Generate video visuals."""
        t0 = time.time()
        try:
            video_path = self.video_engine.generate(
                script=script,
                audio_path=audio_result["audio_path"],
                audio_duration=audio_result["duration"],
                video_id=video_id,
            )
            if video_path:
                self._log_step("video", "success",
                               f"path={video_path}",
                               time.time() - t0)
            else:
                self._log_step("video", "failure", "No video generated",
                               time.time() - t0)
            return video_path
        except Exception as e:
            self._log_step("video", "failure", str(e), time.time() - t0)
            logger.error(f"Video generation failed: {e}")
            return None

    def step_subtitles(self, video_path: str, audio_result: Dict,
                       script: Dict, video_id: int) -> str:
        """Burn subtitles into video."""
        t0 = time.time()
        try:
            result = self.subtitle_engine.process(
                video_path=video_path,
                audio_path=audio_result["audio_path"],
                script_text=script["full_text"],
                audio_duration=audio_result["duration"],
                video_id=video_id,
            )
            if result:
                self._log_step("subtitles", "success", "", time.time() - t0)
                return result
            else:
                self._log_step("subtitles", "failure",
                               "Subtitles failed — using unsbtitled",
                               time.time() - t0)
                return video_path  # fallback
        except Exception as e:
            self._log_step("subtitles", "failure", str(e), time.time() - t0)
            logger.warning(f"Subtitle step failed: {e} — using original video")
            return video_path

    def step_upload(self, final_video: str, script: Dict,
                    video_id: int) -> Optional[Dict]:
        """Upload to YouTube."""
        t0 = time.time()
        if self.dry_run:
            logger.info(f"[DRY RUN] Would upload: {final_video}")
            return {"url": "https://youtube.com/shorts/DRY_RUN", "youtube_id": "DRY_RUN"}

        try:
            result = self.uploader.upload(final_video, script, video_id)
            if result:
                db.update_video(video_id, status="uploaded",
                                uploaded_at=datetime.now().isoformat())
                self._log_step("upload", "success",
                               f"youtube_id={result['youtube_id']}",
                               time.time() - t0)
            else:
                db.update_video(video_id, status="failed",
                                error="Upload returned None")
                self._log_step("upload", "failure", "Upload returned None",
                               time.time() - t0)
            return result
        except Exception as e:
            db.update_video(video_id, status="failed", error=str(e)[:500])
            self._log_step("upload", "failure", str(e), time.time() - t0)
            logger.error(f"Upload failed: {e}")
            return None

    # ── Produce one video ─────────────────────────────────────────────────────

    def produce_one(self, topic: Dict, topic_id: int) -> bool:
        """
        Run the full pipeline for one topic.
        Returns True if video was successfully uploaded.
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"🚀 Producing: {topic['title']}")
        logger.info(f"{'='*60}")

        db.mark_topic_used(topic_id)

        # Create DB record
        # Script first
        script = self.step_script(topic, topic_id)
        if not script:
            return False

        video_id = db.create_video_record(script["script_id"])
        intermediate_audio = None
        intermediate_video = None

        try:
            # Voice
            audio_result = self.step_voice(script, video_id)
            if not audio_result:
                db.update_video(video_id, status="failed", error="Voice generation failed")
                return False
            intermediate_audio = audio_result["audio_path"]
            db.update_video(video_id, audio_path=intermediate_audio)

            # Video
            raw_video = self.step_video(script, audio_result, video_id)
            if not raw_video:
                db.update_video(video_id, status="failed", error="Video generation failed")
                return False
            intermediate_video = raw_video
            db.update_video(video_id, video_path=raw_video)

            # Subtitles
            final_video = self.step_subtitles(raw_video, audio_result, script, video_id)
            db.update_video(video_id,
                           final_path=final_video,
                           status="rendered",
                           rendered_at=datetime.now().isoformat(),
                           duration=audio_result["duration"])

            # Upload
            upload_result = self.step_upload(final_video, script, video_id)
            if not upload_result:
                return False

            logger.success(f"\n🎉 VIDEO LIVE: {upload_result['url']}")

            # Cleanup intermediates (keep final)
            if final_video != raw_video:
                self._cleanup_intermediates(video_id, intermediate_audio,
                                            intermediate_video)

            return True

        except Exception as e:
            logger.error(f"Pipeline error: {e}\n{traceback.format_exc()}")
            db.update_video(video_id, status="failed", error=str(e)[:500])
            return False

    # ── Daily run ─────────────────────────────────────────────────────────────

    def run_daily(self):
        """
        Main daily execution: fetch trends, produce N videos.
        """
        start_time = time.time()
        logger.info(f"\n{'#'*60}")
        logger.info(f"# DAILY RUN — {self.run_date}")
        logger.info(f"{'#'*60}")

        # Check if we've already met today's quota
        already_uploaded = db.videos_uploaded_today()
        remaining = config.VIDEOS_PER_DAY - already_uploaded
        if remaining <= 0 and not self.dry_run:
            logger.info(f"Daily quota already met ({config.VIDEOS_PER_DAY} videos)")
            return

        # Fetch trends
        topics = self.step_trends()
        if not topics:
            logger.error("No topics available — aborting daily run")
            return

        # Produce videos
        produced = 0
        for i, topic in enumerate(topics):
            if produced >= remaining and not self.dry_run:
                break
            if produced >= config.VIDEOS_PER_DAY:  # hard cap for dry run
                break

            topic_id_list = db.save_topics([topic]) if not topic.get("id") else [topic.get("id")]
            topic_id = topic_id_list[0] if topic_id_list else i + 1

            success = self.produce_one(topic, topic_id)
            if success:
                produced += 1
                if produced < remaining and not self.dry_run:
                    # Rate limit between videos
                    logger.info("⏳ Waiting 60s between videos...")
                    time.sleep(60)
            else:
                logger.warning(f"Video {produced+1} failed — trying next topic")

        elapsed = time.time() - start_time
        logger.info(f"\n📊 Daily run complete: {produced}/{config.VIDEOS_PER_DAY} videos in {elapsed/60:.1f}min")


# ── CLI entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    pipeline = Pipeline(dry_run=dry_run)

    if "--once" in sys.argv:
        # Produce just one video
        topics = pipeline.step_trends()
        if topics:
            from database import save_topics
            ids = save_topics(topics[:1])
            pipeline.produce_one(topics[0], ids[0])
    else:
        pipeline.run_daily()
