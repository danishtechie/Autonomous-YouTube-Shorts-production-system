"""
script_generator.py — LLM-powered viral script generation with scoring.
Supports: Anthropic, OpenAI, Groq (free)
"""

import re
import json
import time
from typing import Dict, Optional, List
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

import config
import database as db


SYSTEM_PROMPT = """You are a viral YouTube Shorts scriptwriter with 10M+ views experience.
You write short, punchy, high-retention scripts for the {niche} niche.

Your scripts follow this exact structure:
1. HOOK (2-3 seconds): A shocking statement, counterintuitive fact, or provocative question that makes people STOP scrolling. No fluff. Immediate value signal.
2. BODY (25-35 seconds): Fast-paced delivery. One idea every 5 seconds. Short sentences. Use "But here's the thing...", "And it gets worse...", "Here's what no one tells you..." to maintain curiosity gaps. Never waste words.
3. CTA (3-5 seconds): Payoff + action trigger. Either a revelation, a cliffhanger, or a direct "Follow for more" that feels earned.

Rules:
- Write at NATURAL speech pace: ~130 words/minute
- Total: 65-100 words for 30-45 seconds
- NO filler phrases ("in today's video", "hey guys", "don't forget to like")
- Use sensory language and specific numbers when possible
- Each sentence must earn its place
- Write for audio: avoid complex punctuation

Return ONLY a valid JSON object with this exact structure, no markdown, no explanation, nothing else:
{
  "title": "Clickbait YouTube title 50-60 chars",
  "hook": "First 2-3 seconds of script",
  "body": "Main content with newlines between sections",
  "cta": "Closing 3-5 seconds",
  "full_text": "Complete script as single string for TTS",
  "search_keywords": ["keyword1", "keyword2", "keyword3"],
  "thumbnail_concept": "Brief visual concept for thumbnail"
}"""

SCRIPT_PROMPT = """Create a viral YouTube Shorts script about: "{topic}"

Target niche: {niche}
Target duration: {duration} seconds
Voice style: Confident, slightly urgent, conversational.

Return ONLY the JSON object. No markdown, no explanation, no text before or after."""


class ScriptScorer:
    HOOK_POWER_WORDS = [
        "secret", "truth", "lie", "never", "always", "shocking", "exposed",
        "hidden", "real", "dark", "dangerous", "millions", "actually", "proof",
        "discovered", "finally", "forbidden", "money", "brain", "death", "life",
        "power", "fear", "success", "failure", "hack", "trick", "reason",
    ]
    RETENTION_BRIDGES = [
        "but here's", "and it gets", "here's the thing", "but wait",
        "it turns out", "but the real", "here's why", "but nobody",
        "the crazy part", "and this changed",
    ]
    EMOTION_TRIGGERS = [
        "fear", "anger", "surprise", "joy", "anxiety", "relief", "curiosity",
        "money", "death", "love", "health", "success", "failure", "power", "freedom",
    ]

    def score_hook(self, hook: str) -> float:
        h = hook.lower()
        score = 30.0
        score += min(40, sum(1 for w in self.HOOK_POWER_WORDS if w in h) * 8)
        if "?" in hook: score += 10
        if re.search(r"\d+", hook): score += 8
        words = len(hook.split())
        if words <= 12: score += 12
        elif words <= 18: score += 6
        elif words > 25: score -= 10
        if re.match(r"^(did you know|have you ever|today we)", h): score -= 15
        return min(100, max(0, score))

    def score_retention(self, body: str) -> float:
        b = body.lower()
        score = 30.0
        score += min(35, sum(1 for br in self.RETENTION_BRIDGES if br in b) * 10)
        sentences = [s.strip() for s in re.split(r"[.!?]", body) if s.strip()]
        avg_len = sum(len(s.split()) for s in sentences) / max(len(sentences), 1)
        if avg_len <= 8: score += 20
        elif avg_len <= 12: score += 10
        elif avg_len > 20: score -= 15
        paragraphs = [p for p in body.split("\n") if p.strip()]
        if 3 <= len(paragraphs) <= 6: score += 15
        return min(100, max(0, score))

    def score_emotion(self, full_text: str) -> float:
        t = full_text.lower()
        score = 30.0
        score += min(50, sum(1 for e in self.EMOTION_TRIGGERS if e in t) * 7)
        score += min(20, sum(1 for c in ["but", "however", "yet", "despite"] if f" {c} " in t) * 5)
        return min(100, max(0, score))

    def score(self, script: Dict) -> Dict:
        h = self.score_hook(script.get("hook", ""))
        r = self.score_retention(script.get("body", ""))
        e = self.score_emotion(script.get("full_text", ""))
        return {**script,
                "hook_score": round(h, 1),
                "retention_score": round(r, 1),
                "emotion_score": round(e, 1),
                "total_score": round(h * 0.40 + r * 0.35 + e * 0.25, 1)}


class LLMClient:
    def __init__(self):
        self.provider = config.LLM_PROVIDER
        self.model = config.LLM_MODEL

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
    def generate(self, system: str, user: str) -> str:
        if self.provider == "groq":
            return self._groq(system, user)
        elif self.provider == "anthropic":
            return self._anthropic(system, user)
        elif self.provider == "openai":
            return self._openai(system, user)
        else:
            raise ValueError(f"Unknown provider: {self.provider}")

    def _groq(self, system: str, user: str) -> str:
        import os
        from groq import Groq
        key = os.getenv("GROQ_API_KEY")
        if not key:
            raise ValueError("GROQ_API_KEY not set in .env")
        client = Groq(api_key=key)
        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=1024,
            temperature=0.85,
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content

    def _anthropic(self, system: str, user: str) -> str:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=self.model, max_tokens=1024, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return msg.content[0].text

    def _openai(self, system: str, user: str) -> str:
        from openai import OpenAI
        client = OpenAI(api_key=config.OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=1024, temperature=0.9,
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content


class ScriptGenerator:
    def __init__(self):
        self.llm = LLMClient()
        self.scorer = ScriptScorer()
        self.min_score = 45.0  # relaxed for Groq free tier

    def _parse_json(self, raw: str) -> Dict:
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            raw = raw[start:end]
        raw = re.sub(r",\s*([}\]])", r"\1", raw)
        return json.loads(raw)

    def _estimate_duration(self, text: str) -> float:
        return round((len(text.split()) / 130) * 60, 1)

    def _validate(self, script: Dict) -> List[str]:
        errors = []
        for f in ["title", "hook", "body", "cta", "full_text"]:
            if not script.get(f):
                errors.append(f"Missing: {f}")
        dur = self._estimate_duration(script.get("full_text", ""))
        if dur < config.MIN_VIDEO_DURATION:
            errors.append(f"Too short: {dur}s")
        if dur > config.MAX_VIDEO_DURATION:
            errors.append(f"Too long: {dur}s")
        return errors

    def generate(self, topic: Dict, topic_id: Optional[int] = None,
                 max_attempts: int = 3) -> Optional[Dict]:
        system = SYSTEM_PROMPT.format(niche=config.NICHE)
        best_script, best_score = None, 0

        for attempt in range(1, max_attempts + 1):
            logger.info(f"📝 Script attempt {attempt}/{max_attempts}: {topic['title']}")
            try:
                raw = self.llm.generate(system, SCRIPT_PROMPT.format(
                    topic=topic["title"],
                    niche=config.NICHE,
                    duration=config.MAX_VIDEO_DURATION,
                ))
                logger.debug(f"Raw response: {raw[:300]}")
                script = self._parse_json(raw)

                # Auto-fix missing full_text
                if not script.get("full_text"):
                    script["full_text"] = " ".join(filter(None, [
                        script.get("hook", ""),
                        script.get("body", ""),
                        script.get("cta", ""),
                    ]))

                script["est_duration"] = self._estimate_duration(script["full_text"])
                script["word_count"] = len(script["full_text"].split())
                script["topic_id"] = topic_id

                errors = self._validate(script)
                if errors:
                    logger.warning(f"Validation: {errors}")
                    continue

                scored = self.scorer.score(script)
                logger.info(
                    f"  Score: {scored['total_score']:.0f} "
                    f"(hook={scored['hook_score']:.0f} "
                    f"ret={scored['retention_score']:.0f} "
                    f"emo={scored['emotion_score']:.0f})"
                )

                if scored["total_score"] > best_score:
                    best_score, best_script = scored["total_score"], scored

                if scored["total_score"] >= self.min_score:
                    logger.success(f"✅ Script accepted ({scored['total_score']:.0f})")
                    break

                logger.warning(f"Score {scored['total_score']:.0f} < {self.min_score} — retrying")
                time.sleep(2)

            except json.JSONDecodeError as e:
                logger.error(f"JSON error: {e} | Raw: {raw[:300] if 'raw' in dir() else 'N/A'}")
            except Exception as e:
                logger.error(f"Generation error: {e}")
                time.sleep(3)

        if not best_script:
            logger.error("All attempts failed")
            return None

        script_id = db.save_script(best_script)
        best_script["script_id"] = script_id
        logger.info(f"💾 Script saved (id={script_id})")
        return best_script


if __name__ == "__main__":
    gen = ScriptGenerator()
    topic = {"title": "The Dark Reason You Can't Stop Checking Your Phone", "source": "test", "score": 85}
    script = gen.generate(topic, topic_id=1)
    if script:
        print(f"\n{'='*60}")
        print(f"TITLE:    {script['title']}")
        print(f"SCORE:    {script['total_score']}")
        print(f"DURATION: {script['est_duration']}s")
        print(f"\nHOOK:\n{script['hook']}")
        print(f"\nBODY:\n{script['body']}")
        print(f"\nCTA:\n{script['cta']}")