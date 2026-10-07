"""Deterministic logic. No AI here.

- The AI only labels each key point hit / partial / miss; this file turns that into a score,
  so the same judgement always gives the same score.
- Picks questions (including adaptive difficulty).
- Moves the candidate's "stock price".
- Encodes progress so it survives across sessions (bookmarkable link or a downloaded file).
"""

import base64
import json
import os
import random
import zlib
from statistics import mean

HERE = os.path.dirname(os.path.abspath(__file__))
QUESTIONS = json.load(open(os.path.join(HERE, "questions.json"), encoding="utf-8"))
BY_ID = {q["id"]: q for q in QUESTIONS}
TOPICS = list(dict.fromkeys(q["topic"] for q in QUESTIONS))
DIFFICULTIES = ["Easy", "Medium", "Hard"]
TOPIC_CODE = {t: q["id"].rstrip("0123456789") for t in TOPICS for q in QUESTIONS if q["topic"] == t}
CODE_TOPIC = {v: k for k, v in TOPIC_CODE.items()}

START_PRICE = 100.0
HINT_PENALTY = 1.0
MISCONCEPTION_PENALTY = 1.5
MAX_SESSIONS_SAVED = 20


# ------------------------------------------------------------------ scoring
def score_answer(points, misconceptions, hint_used):
    """points: list of {"status": hit|partial|miss}. Returns a score from 0 to 10 in 0.5 steps."""
    if not points:
        return 0.0
    credit = sum(1.0 if p.get("status") == "hit" else 0.5 if p.get("status") == "partial" else 0.0 for p in points)
    raw = 10 * credit / len(points)
    raw -= MISCONCEPTION_PENALTY * min(len(misconceptions or []), 2)
    raw -= HINT_PENALTY if hint_used else 0
    return max(0.0, min(10.0, round(raw * 2) / 2))


def clean_points(q, points):
    """Make sure every key point appears exactly once with a valid status, whatever the model returned."""
    status_by_text = {}
    for p in points or []:
        if isinstance(p, dict):
            status_by_text[str(p.get("point", "")).strip().lower()] = p.get("status", "miss")
    cleaned = []
    for i, kp in enumerate(q["key_points"]):
        status = status_by_text.get(kp.strip().lower())
        if status is None and i < len(points or []) and isinstance(points[i], dict):
            status = points[i].get("status")  # model paraphrased the text; fall back to position
        cleaned.append({"point": kp, "status": status if status in ("hit", "partial", "miss") else "miss"})
    return cleaned


# ------------------------------------------------------------------ stock ticker
def move_price(price, score):
    """Score 5 keeps the price flat; 10 is +15%, 0 is -15%."""
    return round(price * (1 + (score - 5) * 0.03), 2)


def ticker_symbol(name):
    letters = "".join(ch for ch in (name or "").upper() if ch.isalpha())
    return "$" + (letters[:4] or "YOU")


# ------------------------------------------------------------------ question picking
def next_difficulty(mode, current, last_score):
    if mode != "Adaptive":
        return mode
    if last_score is None:
        return current
    i = DIFFICULTIES.index(current)
    if last_score >= 7 and i < 2:
        return DIFFICULTIES[i + 1]
    if last_score <= 4 and i > 0:
        return DIFFICULTIES[i - 1]
    return current


def pick_question(topics, difficulty, asked_ids, rng=random):
    pool = [q for q in QUESTIONS if q["topic"] in topics and q["id"] not in asked_ids]
    exact = [q for q in pool if q["difficulty"] == difficulty]
    if exact:
        return rng.choice(exact)
    if pool:  # nearest difficulty available
        target = DIFFICULTIES.index(difficulty)
        pool.sort(key=lambda q: abs(DIFFICULTIES.index(q["difficulty"]) - target))
        return pool[0]
    return None


# ------------------------------------------------------------------ session summaries
def summarize(results):
    graded = [r for r in results if r["status"] != "skipped"]
    overall = round(mean(r["score"] for r in graded), 1) if graded else 0.0
    by_topic = {}
    for t in TOPICS:
        scores = [r["score"] for r in graded if r["topic"] == t]
        if scores:
            by_topic[t] = round(mean(scores), 1)
    return overall, by_topic


def verdict(overall):
    if overall >= 8:
        return "Superday ready 🏆"
    if overall >= 6:
        return "First-round ready 👔"
    if overall >= 4:
        return "Needs more reps 📈"
    return "Back to the books 📚"


# ------------------------------------------------------------------ progress across sessions
def encode_progress(sessions):
    raw = json.dumps(sessions[-MAX_SESSIONS_SAVED:], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")


def decode_progress(code):
    try:
        code = (code or "").strip()
        data = json.loads(zlib.decompress(base64.urlsafe_b64decode(code + "=" * (-len(code) % 4))))
        if not isinstance(data, list):
            return []
        return [s for s in data if isinstance(s, dict) and "a" in s][-MAX_SESSIONS_SAVED:]
    except Exception:
        return []


def session_record(date_str, overall, n_answered, final_price, by_topic):
    return {"d": date_str, "a": overall, "n": n_answered, "px": final_price,
            "t": {TOPIC_CODE[t]: v for t, v in by_topic.items()}}
