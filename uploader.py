"""
uploader.py — YouTube Data API v3 upload automation.

Features:
  - OAuth2 authentication with token refresh
  - Auto-generate optimized titles, descriptions, and tags
  - Upload as YouTube Short (vertical, #Shorts tag)
  - Resumable upload for large files
  - Metadata optimization for maximum discoverability
"""

import os
import re
import json
import time
import random
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

import config
import database as db


# ── Metadata generation ───────────────────────────────────────────────────────

class MetadataGenerator:
    """Generates YouTube-optimized titles, descriptions, and tags."""

    TITLE_TEMPLATES = [
        "{hook} 🤯 #Shorts",
        "Nobody Talks About This 😳 {topic}",
        "This Will Change How You See {topic} 🔥",
        "{hook} (You Were Lied To) #Shorts",
        "The Truth About {topic} Nobody Tells You",
        "Why {topic} Is NOT What You Think 👀",
        "{hook} — This Changes Everything",
        "I Can't Believe {topic} Is Real 😱",
    ]

    DESCRIPTION_TEMPLATE = """{hook}

{body_preview}

{hashtags}

🔔 Follow for daily mind-blowing facts!
📲 Share with someone who needs to see this!

---
{additional_hashtags}
"""

    NICHE_TAGS = {
        "facts": ["facts", "didyouknow", "mindblown", "learnontiktok",
                  "amazingfacts", "interestingfacts", "todayilearned",
                  "knowledge", "education", "viral"],
        "psychology": ["psychology", "psychologyfacts", "mindpsychology",
                      "humanbehavior", "brainscience", "mentaltricks",
                      "psychologytricks", "mindset", "emotional"],
        "science": ["science", "sciencefacts", "physicsfacts", "chemistry",
                   "biology", "spacefacts", "technology", "innovation"],
        "history": ["history", "historyfacts", "ancienthistory",
                   "historicaltruths", "didyouknow", "historymeme"],
        "finance": ["finance", "moneytips", "investing", "personalfinance",
                   "financialfreedom", "wealthbuilding", "moneyadvice"],
        "motivation": ["motivation", "success", "mindset", "selfdevelopment",
                      "productivity", "habits", "discipline", "goals"],
    }

    def generate_title(self, script: Dict) -> str:
        hook = script.get("hook", "")[:60].rstrip(".!?")
        topic = script.get("title", "This Topic")

        # Clean up hook for title
        hook_clean = re.sub(r"[^\w\s'-]", "", hook).strip()
        if len(hook_clean) > 55:
            hook_clean = hook_clean[:52] + "..."

        template = random.choice(self.TITLE_TEMPLATES)
        title = template.format(hook=hook_clean, topic=topic)

        # Ensure YouTube title limit (100 chars)
        if len(title) > 100:
            title = title[:97] + "..."

        return title

    def generate_description(self, script: Dict) -> str:
        hook = script.get("hook", "")
        body = script.get("body", "")
        body_preview = " ".join(body.split()[:30]) + "..."

        niche_tags = self.NICHE_TAGS.get(config.NICHE, self.NICHE_TAGS["facts"])
        hashtags = " ".join(f"#{t}" for t in niche_tags[:8])
        additional = " ".join(f"#{t}" for t in ["Shorts", "YouTubeShorts",
                                                   "viral", "trending", "fyp",
                                                   "reels", "shortsvideo"])

        return self.DESCRIPTION_TEMPLATE.format(
            hook=hook,
            body_preview=body_preview,
            hashtags=hashtags,
            additional_hashtags=additional,
        ).strip()

    def generate_tags(self, script: Dict) -> List[str]:
        base_tags = self.NICHE_TAGS.get(config.NICHE, self.NICHE_TAGS["facts"])
        shorts_tags = ["Shorts", "YouTubeShorts", "viral", "trending",
                      "fyp", "reels", "shortsvideo", "short"]
        keyword_tags = script.get("search_keywords", [])

        all_tags = base_tags + shorts_tags + keyword_tags
        # YouTube allows up to 500 chars total for tags
        result = []
        total_chars = 0
        for tag in all_tags:
            if total_chars + len(tag) + 1 < 490:
                result.append(tag)
                total_chars += len(tag) + 1

        return result[:30]  # max 30 tags


# ── YouTube Auth ──────────────────────────────────────────────────────────────

class YouTubeAuth:
    TOKEN_FILE = Path("./data/youtube_token.json")

    def get_credentials(self):
        """Get valid OAuth2 credentials, refreshing if needed."""
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        creds = None

        # Load existing token
        if self.TOKEN_FILE.exists():
            try:
                with open(self.TOKEN_FILE) as f:
                    token_data = json.load(f)
                creds = Credentials.from_authorized_user_info(token_data)
            except Exception as e:
                logger.warning(f"Failed to load token: {e}")

        # Use refresh token if available
        if not creds and config.YOUTUBE_REFRESH_TOKEN:
            creds = Credentials(
                token=None,
                refresh_token=config.YOUTUBE_REFRESH_TOKEN,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=config.YOUTUBE_CLIENT_ID,
                client_secret=config.YOUTUBE_CLIENT_SECRET,
            )

        if not creds:
            raise ValueError(
                "No YouTube credentials found.\n"
                "Run setup/youtube_auth.py to authenticate."
            )

        # Refresh if expired
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self._save_token(creds)

        return creds

    def _save_token(self, creds):
        self.TOKEN_FILE.parent.mkdir(exist_ok=True)
        with open(self.TOKEN_FILE, "w") as f:
            json.dump({
                "token": creds.token,
                "refresh_token": creds.refresh_token,
                "token_uri": creds.token_uri,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "scopes": list(creds.scopes) if creds.scopes else [],
            }, f)


# ── Uploader ──────────────────────────────────────────────────────────────────

class YouTubeUploader:
    SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
              "https://www.googleapis.com/auth/youtube"]
    CHUNK_SIZE = 1024 * 1024 * 8  # 8MB chunks for resumable upload

    def __init__(self):
        self.auth = YouTubeAuth()
        self.meta = MetadataGenerator()
        self._service = None

    def _get_service(self):
        if self._service:
            return self._service
        from googleapiclient.discovery import build
        creds = self.auth.get_credentials()
        self._service = build("youtube", "v3", credentials=creds)
        return self._service

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=5, max=60))
    def _upload_video(self, file_path: str, metadata: Dict) -> Optional[str]:
        """
        Upload video with resumable upload protocol.
        Returns YouTube video ID on success.
        """
        from googleapiclient.http import MediaFileUpload

        service = self._get_service()
        file_size = Path(file_path).stat().st_size

        body = {
            "snippet": {
                "title": metadata["title"],
                "description": metadata["description"],
                "tags": metadata["tags"],
                "categoryId": "22",  # People & Blogs (works for Shorts)
                "defaultLanguage": "en",
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(
            file_path,
            mimetype="video/mp4",
            chunksize=self.CHUNK_SIZE,
            resumable=True,
        )

        logger.info(f"📤 Starting upload: {Path(file_path).name} ({file_size // (1024*1024):.1f}MB)")

        request = service.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        # Execute resumable upload with progress
        response = None
        retry_count = 0
        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    logger.info(f"  Upload progress: {pct}%")
            except Exception as e:
                retry_count += 1
                if retry_count > 5:
                    raise
                logger.warning(f"Upload chunk error: {e}, retrying...")
                time.sleep(5 * retry_count)

        video_id = response.get("id")
        if not video_id:
            raise ValueError(f"No video ID in response: {response}")

        logger.success(f"✅ Uploaded: https://youtube.com/shorts/{video_id}")
        return video_id

    def upload(self, video_path: str, script: Dict, db_video_id: int) -> Optional[Dict]:
        """
        Full upload pipeline: generate metadata, upload, save to DB.
        """
        if not Path(video_path).exists():
            logger.error(f"Video file not found: {video_path}")
            return None

        # Check daily limit
        uploaded_today = db.videos_uploaded_today()
        if uploaded_today >= config.VIDEOS_PER_DAY:
            logger.warning(f"Daily upload limit reached ({config.VIDEOS_PER_DAY} videos)")
            return None

        # Generate metadata
        title = self.meta.generate_title(script)
        description = self.meta.generate_description(script)
        tags = self.meta.generate_tags(script)

        logger.info(f"📋 Title: {title}")

        metadata = {
            "title": title,
            "description": description,
            "tags": tags,
        }

        # Upload
        youtube_id = self._upload_video(video_path, metadata)
        if not youtube_id:
            return None

        # Save to DB
        upload_data = {
            "youtube_id": youtube_id,
            "title": title,
            "description": description,
            "tags": tags,
        }
        upload_id = db.save_upload(db_video_id, upload_data)

        result = {
            **upload_data,
            "upload_id": upload_id,
            "url": f"https://youtube.com/shorts/{youtube_id}",
        }

        logger.success(f"🎉 Video live: {result['url']}")
        return result


# ── OAuth setup helper (run once) ─────────────────────────────────────────────

def run_oauth_setup():
    """
    Run this ONCE to generate refresh token.
    Opens browser for Google OAuth consent.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow
    import json

    client_config = {
        "installed": {
            "client_id": config.YOUTUBE_CLIENT_ID,
            "client_secret": config.YOUTUBE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }

    flow = InstalledAppFlow.from_client_config(
        client_config,
        scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )
    creds = flow.run_local_server(port=8080)

    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes),
    }

    token_file = Path("./data/youtube_token.json")
    token_file.parent.mkdir(exist_ok=True)
    with open(token_file, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"\n✅ Token saved to {token_file}")
    print(f"   Refresh token: {creds.refresh_token}")
    print("   Add YOUTUBE_REFRESH_TOKEN to your .env file")


if __name__ == "__main__":
    import sys
    if "--setup-oauth" in sys.argv:
        run_oauth_setup()
    else:
        print("Run with --setup-oauth to authenticate with YouTube")
