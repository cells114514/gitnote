"""Search candidate generation aligned with GitTok.dev's public feed module."""

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random

from taste_profile import observe_repos, effective_score
from taste_ranking import rank_batch, sample_topic


TOPICS = json.loads((Path(__file__).resolve().parent / "topics.json").read_text(encoding="utf-8"))
STAR_BANDS = [("stars:50..200", 2), ("stars:200..1000", 3),
              ("stars:1000..5000", 3), ("stars:5000..20000", 2),
              ("stars:20000..100000", 1)]
ORDERINGS = [(None, None), ("stars", "desc"), ("stars", "asc"),
             ("forks", "desc"), ("forks", "asc"), ("updated", "desc"),
             ("help-wanted-issues", "desc")]
PER_PAGE = 25
MAX_DRAWS = 3


def _pick_weighted(entries, rng):
    roll = rng.random() * sum(weight for _, weight in entries)
    for value, weight in entries:
        roll -= weight
        if roll < 0:
            return value
    return entries[-1][0]


def random_query(profile, pool, memory, rng, language="", star_band=""):
    topic = sample_topic(profile, pool, rng.random)
    band = star_band or _pick_weighted(STAR_BANDS, rng)
    sort, order = rng.choice(ORDERINGS)
    q = f"{band} topic:{topic}" + (f" language:{language.lower()}" if language else "")
    previous = memory.get(q)
    if isinstance(previous, dict):
        count = min(max(0, int(previous.get("total_projects", 0))), 1000)
        ceiling = max(1, math.ceil(count / PER_PAGE))
    else:
        ceiling = 4
    page = rng.randrange(1, ceiling + 1)
    if ceiling > 1 and previous and page == previous.get("current_page"):
        page = page % ceiling + 1
    params = {"q": q, "per_page": PER_PAGE, "page": page}
    if sort:
        params.update({"sort": sort, "order": order})
    return params


def _age_days(timestamp):
    try:
        updated = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return max(0, (datetime.now(timezone.utc) - updated).days)
    except (AttributeError, ValueError):
        return 0


def normalize_repo(raw):
    if not isinstance(raw, dict) or raw.get("private") or raw.get("archived"):
        return None
    repo_id = raw.get("id")
    url = raw.get("html_url")
    if not isinstance(repo_id, int) or not isinstance(url, str) or not url.startswith("https://github.com/"):
        return None
    owner = raw.get("owner") if isinstance(raw.get("owner"), dict) else {}
    name = str(raw.get("name") or "")[:120]
    topics = [topic for topic in raw.get("topics", []) if isinstance(topic, str)][:30] if isinstance(raw.get("topics"), list) else []
    updated = raw.get("pushed_at") or raw.get("updated_at") or ""
    return {"id": f"gh-{repo_id}", "name": name, "summary": str(raw.get("description") or "暂无描述")[:500],
            "author": str(owner.get("login") or "未知作者")[:100], "language": str(raw.get("language") or "")[:60],
            "topics": topics, "stars": max(0, int(raw.get("stargazers_count") or 0)),
            "updated": updated[:10], "age_days": _age_days(updated),
            "source_url": url, "full_name": str(raw.get("full_name") or name)[:200]}


def discover(client, profile, memory=None, seen=None, hidden=None, topic="", language="", star_band="", token=None, rng=None):
    rng = rng or random.Random()
    memory = dict(memory or {})
    seen = set(seen or [])
    hidden = set(hidden or [])
    pool = [topic] if topic else TOPICS
    items = []
    unique = set()
    incomplete = False
    last_query = ""
    for _ in range(MAX_DRAWS):
        params = random_query(profile, pool, memory, rng, language, star_band)
        data = client.search(params, token=token)
        q = params["q"]
        last_query = q
        memory[q] = {"current_page": params["page"], "total_projects": int(data.get("total_count", 0))}
        raw = data.get("items") if isinstance(data.get("items"), list) else []
        if not raw and params["page"] != 1:
            params["page"] = 1
            data = client.search(params, token=token)
            memory[q] = {"current_page": 1, "total_projects": int(data.get("total_count", 0))}
            raw = data.get("items") if isinstance(data.get("items"), list) else []
        if not raw:
            continue
        incomplete = incomplete or bool(data.get("incomplete_results"))
        for entry in raw[:12]:
            item = normalize_repo(entry)
            if item and item["id"] not in seen | hidden | unique:
                items.append(item)
                unique.add(item["id"])
        # Mix several GitTok-style query draws when a single topic would fill
        # the whole waterfall with one language. Keep at most 12 per draw.
        languages = {item["language"] for item in items if item["language"]}
        if len(items) >= 12 and (language or len(languages) >= 3):
            break
    next_profile = observe_repos(profile, items)
    ranked = rank_batch(next_profile, items)
    for item in ranked:
        matches = [(topic, effective_score(next_profile["topics"][topic.lower()]))
                   for topic in item["topics"] if topic.lower() in next_profile["topics"]]
        matches = [pair for pair in matches if pair[1] > 0]
        item["reason"] = f"匹配你喜欢的「{max(matches, key=lambda pair: pair[1])[0]}」" if matches else "探索新话题"
    return {"items": ranked, "profile": next_profile, "memory": memory,
            "incomplete": incomplete, "query": last_query}


def search_keyword(client, keyword, token=None, page=1, language="", topic="", star_band="", sort="best"):
    """Search GitHub's public repository index, without filtering a local batch."""
    terms = [keyword[:100], "archived:false"]
    if language:
        terms.append(f"language:{language}")
    if topic:
        terms.append(f"topic:{topic}")
    if star_band:
        terms.append(star_band)
    params = {"q": " ".join(terms), "per_page": 30, "page": page}
    if sort != "best":
        params.update({"sort": sort, "order": "desc"})
    data = client.search(params, token=token, cache_ttl=600)
    items = [normalize_repo(entry) for entry in data.get("items", [])]
    total = max(0, int(data.get("total_count") or 0))
    return {"items": [item for item in items if item], "total_count": total,
            "incomplete_results": bool(data.get("incomplete_results")), "page": page,
            "has_more": page * 30 < min(total, 1000)}


def stars_top(client, token=None, page=1, language=""):
    q = "stars:>0 archived:false" + (f" language:{language}" if language else "")
    data = client.search({"q": q, "sort": "stars", "order": "desc", "per_page": 30, "page": page},
                         token=token, cache_ttl=7200)
    items = [normalize_repo(entry) for entry in data.get("items", [])]
    total = max(0, int(data.get("total_count") or 0))
    return {"items": [item for item in items if item], "total_count": total,
            "incomplete_results": bool(data.get("incomplete_results")), "page": page,
            "has_more": page * 30 < min(total, 1000)}
