"""Python port of GitTok.dev's public taste profile and signal behavior.

Upstream: BlackShoreTech/gittok.dev @ 565fbd4, src/lib/taste (see notices).
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math


SIGNAL_WEIGHTS = {
    "star": 1.0,
    "open": 0.6,
    "share": 0.6,
    "fork": 0.5,
    "dwell_long": 0.3,
    "dwell_medium": 0.1,
    "skip_fast": -0.25,
    "not_interested": -1.0,
}
CONFIDENCE_K = 5
PROFILE_STRENGTH_K = 5
SESSION_DECAY = 0.977
AFFINITY_EPSILON = 0.005
MAX_CREDITED_TOPICS = 5


def now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def empty_profile(at=None):
    return {"v": 2, "topics": {}, "languages": {},
            "corpus": {"topics": {}, "documents": 0}, "updatedAt": now_ms() if at is None else at}


def clean_profile(value):
    """Accept only bounded, finite browser profile data."""
    if not isinstance(value, dict) or value.get("v") != 2:
        return empty_profile()
    try:
        result = empty_profile()
        for dimension in ("topics", "languages"):
            source = value[dimension]
            if not isinstance(source, dict) or len(source) > 2000:
                return empty_profile()
            for key, affinity in source.items():
                if not isinstance(key, str) or len(key) > 100 or not isinstance(affinity, dict):
                    return empty_profile()
                score = affinity["score"]
                events = affinity["events"]
                updated = affinity["updatedAt"]
                if (not isinstance(score, (int, float)) or not math.isfinite(score)
                        or not isinstance(events, int) or not 0 <= events <= 1_000_000
                        or not isinstance(updated, (int, float)) or not math.isfinite(updated)):
                    return empty_profile()
                result[dimension][key] = {"score": float(score), "events": events, "updatedAt": updated}
        corpus = value["corpus"]
        counts = corpus["topics"]
        documents = corpus["documents"]
        if (not isinstance(counts, dict) or len(counts) > 5000
                or not isinstance(documents, int) or not 0 <= documents <= 1_000_000):
            return empty_profile()
        for key, count in counts.items():
            if not isinstance(key, str) or len(key) > 100 or not isinstance(count, int) or not 0 <= count <= documents:
                return empty_profile()
            result["corpus"]["topics"][key] = count
        result["corpus"]["documents"] = documents
        updated = value["updatedAt"]
        if not isinstance(updated, (int, float)) or not math.isfinite(updated):
            return empty_profile()
        result["updatedAt"] = updated
        return result
    except (KeyError, TypeError):
        return empty_profile()


def effective_score(affinity):
    events = affinity["events"]
    return affinity["score"] * events / (events + CONFIDENCE_K)


def topic_idf(corpus, topic):
    return math.log((corpus["documents"] + 1) / (corpus["topics"].get(topic, 0) + 1)) + 1


def profile_strength(profile):
    total = sum(abs(effective_score(value)) for value in profile["topics"].values())
    return total / (total + PROFILE_STRENGTH_K)


def observe_repos(profile, repos):
    result = deepcopy(profile)
    for repo in repos:
        for topic in set(str(entry).lower() for entry in repo.get("topics", []) if isinstance(entry, str)):
            counts = result["corpus"]["topics"]
            counts[topic] = counts.get(topic, 0) + 1
    result["corpus"]["documents"] += len(repos)
    return result


def credits_for(signal, corpus):
    weight = SIGNAL_WEIGHTS[signal["kind"]]
    suppressed = {topic.lower() for topic in signal.get("suppressed", []) if isinstance(topic, str)}
    topics = list(dict.fromkeys(topic.lower() for topic in signal.get("topics", []) if isinstance(topic, str)))
    candidates = [topic for topic in topics if topic not in suppressed]
    candidates.sort(key=lambda topic: -topic_idf(corpus, topic))
    chosen = candidates[:MAX_CREDITED_TOPICS]
    total = sum(topic_idf(corpus, topic) for topic in chosen)
    return ({topic: weight * topic_idf(corpus, topic) / total for topic in chosen} if total else {}, weight)


def apply_signal(profile, signal):
    if signal.get("kind") not in SIGNAL_WEIGHTS:
        raise ValueError("Unknown taste signal")
    result = deepcopy(profile)
    credits, language_delta = credits_for(signal, result["corpus"])
    at = signal.get("at", now_ms())

    def credit(target, key, delta):
        current = target.get(key, {"score": 0.0, "events": 0, "updatedAt": at})
        score = current["score"] + delta
        if abs(score) < AFFINITY_EPSILON:
            target.pop(key, None)
        else:
            target[key] = {"score": score, "events": current["events"] + 1, "updatedAt": at}

    for topic, delta in credits.items():
        credit(result["topics"], topic, delta)
    language = signal.get("language")
    if isinstance(language, str) and language:
        credit(result["languages"], language, language_delta)
    result["updatedAt"] = at
    return result


def apply_session_decay(profile, at=None):
    result = deepcopy(profile)
    for dimension in ("topics", "languages"):
        result[dimension] = {
            key: {**affinity, "score": affinity["score"] * SESSION_DECAY}
            for key, affinity in result[dimension].items()
            if abs(affinity["score"] * SESSION_DECAY) >= AFFINITY_EPSILON
        }
    result["updatedAt"] = now_ms() if at is None else at
    return result
