"""Low-frequency reader for GitHub's public Trending page."""

from datetime import datetime, timezone
import html
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from github_client import GitHubError


ARTICLE = re.compile(r'<article\b[^>]*class="[^"]*\bBox-row\b[^"]*"[^>]*>(.*?)</article>', re.S)
REPO = re.compile(r'<h2\b[^>]*>.*?<a\b[^>]*href="/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)"', re.S)
TAGS = re.compile(r'<[^>]+>')
ID = re.compile(r'&quot;repository_id&quot;:(\d+)')
STARS = re.compile(r'<a\b[^>]*href="/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+/stargazers"[^>]*>(.*?)</a>', re.S)
LANGUAGE = re.compile(r'<span\b[^>]*itemprop="programmingLanguage"[^>]*>(.*?)</span>', re.S)
DESCRIPTION = re.compile(r'<p\b[^>]*>(.*?)</p>', re.S)
GAINED = re.compile(r'([\d,]+)\s+stars?\s+(?:today|this week|this month)', re.I)


def plain(fragment):
    return " ".join(html.unescape(TAGS.sub(" ", fragment)).split())


def parse_trending(document, period="daily"):
    """Keep GitHub's displayed order; fail loudly on an unexpected page."""
    items = []
    for article in ARTICLE.findall(document):
        match = REPO.search(article)
        if not match:
            continue
        owner, name = match.groups()
        fullname = f"{owner}/{name}"
        repo_id = ID.search(article)
        stars = STARS.search(article)
        language = LANGUAGE.search(article)
        description = DESCRIPTION.search(article)
        gained = GAINED.search(plain(article))
        items.append({
            "id": f"gh-{repo_id.group(1)}" if repo_id else f"ghname-{fullname.lower()}",
            "name": name, "full_name": fullname, "author": owner,
            "summary": plain(description.group(1))[:500] if description else "暂无描述",
            "language": plain(language.group(1))[:60] if language else "",
            "topics": [],
            "stars": int(re.sub(r"\D", "", plain(stars.group(1))) or 0) if stars else 0,
            "trending_stars": int(gained.group(1).replace(",", "")) if gained else None,
            "trending_rank": len(items) + 1, "trending_period": period,
            "reason": f"Trending #{len(items) + 1}", "updated": "", "age_days": 0,
            "source_url": f"https://github.com/{fullname}",
        })
    if not items:
        raise GitHubError("GitHub Trending 页面结构已变化，请打开原榜查看", 502)
    return items


def fetch_trending(period="daily", language="", timeout=12):
    if period not in {"daily", "weekly", "monthly"} or not re.fullmatch(r"[A-Za-z0-9+#-]{0,40}", language):
        raise ValueError("Trending 筛选条件无效")
    url = "https://github.com/trending" + ("/" + quote(language, safe="") if language else "") + "?since=" + period
    request = Request(url, headers={"User-Agent": "gitnote-local", "Accept": "text/html"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(2_000_001)
    except (HTTPError, URLError, TimeoutError) as error:
        raise GitHubError("暂时无法读取 GitHub Trending", 502) from error
    if len(body) > 2_000_000:
        raise GitHubError("GitHub Trending 页面过大", 502)
    return {"items": parse_trending(body.decode("utf-8", "replace"), period),
            "fetched_at": datetime.now(timezone.utc).isoformat(), "period": period,
            "source_url": url}


def trending_page(cache, period="daily", language="", fetcher=fetch_trending):
    key = f"trending:{period}:{language.lower()}"
    result = cache.get(key)
    if result is not None:
        return {**result, "stale": bool(result.get("stale"))}
    if cache.get(key + ":retry") is not None:
        raise GitHubError("GitHub Trending 暂时不可用，请稍后重试", 502)
    try:
        result = fetcher(period, language)
        cache.put(key, {**result, "stale": False}, 1800)
        return {**result, "stale": False}
    except GitHubError:
        stale = cache.get(key, allow_expired=True)
        if stale is not None:
            cache.put(key, {**stale, "stale": True}, 300)
            return {**stale, "stale": True}
        cache.put(key + ":retry", {"failed": True}, 300)
        raise
