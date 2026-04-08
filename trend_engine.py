"""
trend_engine.py — Multi-source trend detection with virality scoring.

Sources:
  1. Reddit (hot posts across viral subreddits)
  2. Google Trends (pytrends)
  3. YouTube Trending (scraped via yt-dlp or RSS)
  4. SerpAPI Google Trends (if key available)

Output: Top N topics ranked by composite virality score.
"""

import time
import re
import json
import requests
from datetime import datetime
from typing import List, Dict, Optional
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_fixed

import config
import database as db


# ── Niche-to-subreddit mapping ────────────────────────────────────────────────
NICHE_SUBREDDITS = {
    "facts":       ["todayilearned", "InterestingFacts", "NoStupidQuestions", "Damnthatsinteresting"],
    "science":     ["science", "EverythingScience", "Futurology", "space"],
    "history":     ["history", "HistoryMemes", "AskHistorians", "HistoricalWhatIf"],
    "psychology":  ["psychology", "behavioraleconomics", "socialskills", "LifeProTips"],
    "finance":     ["personalfinance", "investing", "financialindependence", "Economics"],
    "motivation":  ["GetMotivated", "selfimprovement", "productivity", "DecidingToBeBetter"],
}

NICHE_KEYWORDS = {
    "facts":       ["facts", "did you know", "amazing", "secret", "hidden", "truth"],
    "science":     ["discovery", "research", "study", "breakthrough", "science", "physics"],
    "history":     ["history", "ancient", "war", "empire", "discovery", "century"],
    "psychology":  ["psychology", "brain", "behavior", "mind", "social", "manipulation"],
    "finance":     ["money", "wealth", "invest", "passive income", "rich", "stocks"],
    "motivation":  ["success", "habits", "mindset", "discipline", "goals", "motivation"],
}


class TrendEngine:
    def __init__(self):
        self.niche = config.NICHE
        self.subreddits = NICHE_SUBREDDITS.get(self.niche, NICHE_SUBREDDITS["facts"])
        self.keywords = NICHE_KEYWORDS.get(self.niche, NICHE_KEYWORDS["facts"])
        self.reddit_headers = {"User-Agent": config.REDDIT_USER_AGENT}
        self._reddit_token: Optional[str] = None

    # ── Reddit ────────────────────────────────────────────────────────────────

    def _get_reddit_token(self) -> Optional[str]:
        if not (config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET):
            return None
        try:
            resp = requests.post(
                "https://www.reddit.com/api/v1/access_token",
                auth=(config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET),
                data={"grant_type": "client_credentials"},
                headers={"User-Agent": config.REDDIT_USER_AGENT},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("access_token")
        except Exception as e:
            logger.warning(f"Reddit auth failed: {e}")
            return None

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(5))
    def _fetch_reddit_hot(self, subreddit: str) -> List[Dict]:
        token = self._reddit_token or self._get_reddit_token()
        headers = {**self.reddit_headers}
        if token:
            headers["Authorization"] = f"bearer {token}"
            base = "https://oauth.reddit.com"
        else:
            base = "https://www.reddit.com"

        url = f"{base}/r/{subreddit}/hot.json?limit=25&t=day"
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        posts = resp.json()["data"]["children"]

        topics = []
        for p in posts:
            d = p["data"]
            if d.get("is_video") or d.get("stickied"):
                continue
            title = d.get("title", "")
            score = d.get("score", 0)
            ratio = d.get("upvote_ratio", 0.5)
            comments = d.get("num_comments", 0)

            # Virality formula: engagement + novelty signals
            virality = min(100, (
                (score / 1000) * 40 +
                (comments / 100) * 30 +
                ratio * 30
            ))

            topics.append({
                "title": title,
                "source": f"reddit/r/{subreddit}",
                "score": round(virality, 2),
                "raw_score": score,
                "url": f"https://reddit.com{d.get('permalink', '')}",
            })
        return topics

    def get_reddit_trends(self) -> List[Dict]:
        all_topics = []
        for sub in self.subreddits[:4]:
            try:
                topics = self._fetch_reddit_hot(sub)
                all_topics.extend(topics)
                logger.debug(f"Reddit r/{sub}: {len(topics)} topics")
                time.sleep(1)  # rate limiting
            except Exception as e:
                logger.warning(f"Reddit r/{sub} failed: {e}")
        return all_topics

    # ── Google Trends ─────────────────────────────────────────────────────────

    def get_google_trends(self) -> List[Dict]:
        topics = []
        try:
            from pytrends.request import TrendReq
            pt = TrendReq(hl="en-US", tz=0, timeout=(10, 25))
            pt.build_payload(
                kw_list=self.keywords[:5],
                timeframe="now 1-d",
                geo="US",
            )
            related = pt.related_queries()
            for kw, data in related.items():
                if data and data.get("rising") is not None:
                    df = data["rising"].head(5)
                    for _, row in df.iterrows():
                        query = str(row.get("query", ""))
                        value = float(row.get("value", 50))
                        if len(query) > 10:
                            topics.append({
                                "title": f"{query.title()} — What You Need to Know",
                                "source": "google_trends",
                                "score": min(100, value),
                            })
        except Exception as e:
            logger.warning(f"Google Trends failed: {e}")

        # Fallback: Daily trending searches
        try:
            from pytrends.request import TrendReq
            pt = TrendReq(hl="en-US", tz=0)
            daily = pt.trending_searches(pn="united_states")
            for term in daily.iloc[:10, 0]:
                topics.append({
                    "title": f"The Truth About {term}",
                    "source": "google_trends_daily",
                    "score": 65,
                })
        except Exception as e:
            logger.warning(f"Google daily trends failed: {e}")

        return topics

    # ── YouTube Trending (RSS) ────────────────────────────────────────────────

    def get_youtube_trends(self) -> List[Dict]:
        topics = []
        try:
            import xml.etree.ElementTree as ET
            # YouTube's public trending RSS (regional)
            url = "https://www.youtube.com/feeds/videos.xml?chart=mostPopular&regionCode=US&hl=en"
            resp = requests.get(url, timeout=15,
                                headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                for entry in root.findall("atom:entry", ns)[:10]:
                    title_el = entry.find("atom:title", ns)
                    if title_el is not None:
                        topics.append({
                            "title": title_el.text,
                            "source": "youtube_trending",
                            "score": 70,
                        })
        except Exception as e:
            logger.warning(f"YouTube trends failed: {e}")
        return topics

    # ── Scoring & deduplication ───────────────────────────────────────────────

    @staticmethod
    def _title_shortability(title: str) -> float:
        """Score how suitable a topic title is for a Short script."""
        score = 50.0
        title_lower = title.lower()

        # Positive signals
        hooks = ["secret", "truth", "why", "how", "what happens", "never",
                 "always", "actually", "fact", "did you know", "proof",
                 "reason", "dark side", "real reason", "exposed", "hidden"]
        score += sum(8 for h in hooks if h in title_lower)

        # Engagement triggers
        emotional = ["shocking", "amazing", "incredible", "scary", "dangerous",
                     "insane", "crazy", "mind", "brain", "life", "death"]
        score += sum(5 for e in emotional if e in title_lower)

        # Penalize overly niche or boring titles
        boring = ["review", "tutorial", "update", "vs", "comparison", "price"]
        score -= sum(10 for b in boring if b in title_lower)

        # Length sweet spot: 40-80 chars
        if 40 <= len(title) <= 80:
            score += 10
        elif len(title) < 20:
            score -= 15

        return min(100, max(0, score))

    def _deduplicate(self, topics: List[Dict]) -> List[Dict]:
        seen = set()
        unique = []
        for t in topics:
            key = re.sub(r"[^a-z0-9]", "", t["title"].lower())[:30]
            if key not in seen:
                seen.add(key)
                unique.append(t)
        return unique

    def _boost_score(self, topics: List[Dict]) -> List[Dict]:
        for t in topics:
            shortability = self._title_shortability(t["title"])
            # Weighted composite: source virality 60% + shortability 40%
            t["score"] = round(t["score"] * 0.6 + shortability * 0.4, 2)
        return sorted(topics, key=lambda x: x["score"], reverse=True)

    # ── Main entry point ──────────────────────────────────────────────────────

    def get_top_topics(self, n: int = 5) -> List[Dict]:
        logger.info("🔍 Fetching trends from all sources...")

        all_topics = []
        all_topics.extend(self.get_reddit_trends())
        all_topics.extend(self.get_google_trends())
        all_topics.extend(self.get_youtube_trends())

        if not all_topics:
            logger.warning("All trend sources failed — using fallback topics")
            all_topics = self._fallback_topics()

        unique = self._deduplicate(all_topics)
        ranked = self._boost_score(unique)
        top = ranked[:n]

        logger.info(f"✅ Top {n} topics selected:")
        for i, t in enumerate(top, 1):
            logger.info(f"  {i}. [{t['score']:.0f}] {t['title']} ({t['source']})")

        # Persist to DB
        db.save_topics(top)
        return top

    def _fallback_topics(self) -> List[Dict]:
        """Static fallback if all APIs fail."""
        templates = {
            "facts": [
                "The Dark Truth About Why You Can't Focus",
                "Scientists Just Discovered Why Humans Fear the Dark",
                "The Real Reason You Talk to Yourself",
                "This Ancient Trick Doubled Memory Retention",
                "Why Your Brain Lies to You Every Single Day",
            ],
            "psychology": [
                "The Manipulation Tactic Used by Every Narcissist",
                "Why Smart People Make the Worst Decisions",
                "The 5-Second Rule That Rewires Your Brain",
                "How to Detect a Liar in 3 Simple Steps",
                "The Silent Habit That Destroys Relationships",
            ],
            "finance": [
                "Why 95% of People Will Never Build Wealth",
                "The Investment Secret Banks Don't Want You to Know",
                "How Compound Interest Actually Works (And Why It's Magic)",
                "The One Financial Mistake That Costs You $1M Over a Lifetime",
                "Why Most Budgets Fail Within 2 Weeks",
            ],
        }
        titles = templates.get(self.niche, templates["facts"])
        return [
            {"title": t, "source": "fallback", "score": 60 + i * 2}
            for i, t in enumerate(titles)
        ]


# ── CLI entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    engine = TrendEngine()
    topics = engine.get_top_topics(5)
    print(json.dumps(topics, indent=2))
