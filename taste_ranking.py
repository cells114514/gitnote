"""GitTok.dev compatible topic sampling and batch ranking."""

import math
import random as random_module

from taste_profile import effective_score, profile_strength


EXPLORATION_RATE = 0.25
NEGATIVE_MUTE_THRESHOLD = -0.5
MAX_TOPIC_SHARE = 0.15
SAMPLING_TEMPERATURE = 0.5
DIVERSITY_WINDOW = 10
DIVERSITY_MAX_PER_WINDOW = 2


def _cap_shares(probabilities, cap):
    shares = probabilities[:]
    for _ in range(8):
        excess = sum(max(0, share - cap) for share in shares)
        if excess <= 0:
            break
        room = sum(share for share in shares if share < cap)
        if room <= 0:
            break
        shares = [cap if share >= cap else share + excess * share / room for share in shares]
    return [min(share, cap) for share in shares]


def _pick(values, rand):
    return values[min(len(values) - 1, int(rand() * len(values)))]


def sample_topic(profile, pool, rand=None):
    if not pool:
        raise ValueError("Topic pool is empty")
    rand = rand or random_module.random
    learned = [(topic, effective_score(value)) for topic, value in profile["topics"].items()]
    muted = {topic for topic, score in learned if score < NEGATIVE_MUTE_THRESHOLD}
    unmuted = [topic for topic in pool if topic not in muted]
    explore = unmuted or pool
    exploit = [(topic, score) for topic, score in learned if score > 0]
    if not exploit:
        return _pick(explore, rand)
    exploit_share = (1 - EXPLORATION_RATE) * profile_strength(profile)
    if rand() >= exploit_share:
        return _pick(explore, rand)
    cap = min(1, MAX_TOPIC_SHARE / exploit_share)
    highest = max(score for _, score in exploit)
    weights = [math.exp((score - highest) / SAMPLING_TEMPERATURE) for _, score in exploit]
    total = sum(weights)
    shares = _cap_shares([weight / total for weight in weights], cap)
    claimed = sum(shares)
    if rand() >= claimed:
        return _pick(explore, rand)
    roll = rand() * claimed
    for (topic, _), share in zip(exploit, shares):
        roll -= share
        if roll < 0:
            return topic
    return exploit[-1][0]


def score_project(profile, project):
    score = sum(effective_score(profile["topics"][topic.lower()])
                for topic in project.get("topics", []) if topic.lower() in profile["topics"])
    language = project.get("language")
    if language in profile["languages"]:
        score += effective_score(profile["languages"][language])
    return score


def enforce_diversity(projects):
    remaining = projects[:]
    placed = []
    while remaining:
        window = placed[-(DIVERSITY_WINDOW - 1):]
        index = next((index for index, project in enumerate(remaining)
                      if not project.get("language") or sum(pr.get("language") == project["language"] for pr in window) < DIVERSITY_MAX_PER_WINDOW), 0)
        placed.append(remaining.pop(index))
    return placed


def rank_batch(profile, projects):
    return enforce_diversity(sorted(projects, key=lambda project: -score_project(profile, project)))
