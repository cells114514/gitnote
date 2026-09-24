import unittest

from app import repo_detail
from cache import MemoryCache
from discovery import search_keyword, stars_top
from github_client import GitHubClient, GitHubError
from trending import parse_trending, trending_page


def repository(number):
    return {"id": number, "name": f"repo-{number}", "full_name": f"owner/repo-{number}",
            "html_url": f"https://github.com/owner/repo-{number}", "owner": {"login": "owner"},
            "stargazers_count": 100 - number, "description": "test", "language": "Python"}


class ListingTests(unittest.TestCase):
    def test_search_uses_remote_page_filters_and_limit(self):
        class Client:
            def search(self, params, token=None, cache_ttl=None):
                self.params = params
                self.ttl = cache_ttl
                return {"items": [repository(31)], "total_count": 60, "incomplete_results": True}
        client = Client()
        result = search_keyword(client, "parser", page=2, language="Python", topic="cli",
                                star_band="stars:50..200", sort="updated")
        self.assertEqual(client.params["page"], 2)
        self.assertEqual(client.params["per_page"], 30)
        self.assertIn("topic:cli", client.params["q"])
        self.assertEqual(client.params["sort"], "updated")
        self.assertEqual(client.ttl, 600)
        self.assertFalse(result["has_more"])
        self.assertTrue(result["incomplete_results"])

    def test_stars_top_keeps_github_order(self):
        class Client:
            def search(self, params, token=None, cache_ttl=None):
                self.params = params
                return {"items": [repository(1), repository(2)], "total_count": 2000}
        client = Client()
        result = stars_top(client, page=1)
        self.assertEqual([item["id"] for item in result["items"]], ["gh-1", "gh-2"])
        self.assertEqual(client.params["sort"], "stars")
        self.assertTrue(result["has_more"])

    def test_trending_parses_and_uses_stale_cache(self):
        fixture = '''<article class="Box-row"><h2><a href="/owner/sample">owner / sample</a></h2>
        <p class="col-9">A useful <b>repository</b></p>
        <span itemprop="programmingLanguage">Python</span>
        <a href="/owner/sample/stargazers">1,234</a>
        <span class="d-inline-block float-sm-right">42 stars today</span></article>'''
        item = parse_trending(fixture)[0]
        self.assertEqual((item["full_name"], item["stars"], item["trending_stars"]),
                         ("owner/sample", 1234, 42))
        cache = MemoryCache()
        cache.put("trending:daily:", {"items": [item], "fetched_at": "old"}, -1)
        from github_client import GitHubError
        result = trending_page(cache, fetcher=lambda *_: (_ for _ in ()).throw(GitHubError("down")))
        self.assertTrue(result["stale"])


class DetailTests(unittest.TestCase):
    def test_detail_reads_rendered_readme_on_demand(self):
        class Client:
            def __init__(self): self.paths = []
            def get_json(self, path, token=None, cache_ttl=0):
                self.paths.append(path)
                return {"full_name": "owner/sample", "forks_count": 8, "open_issues_count": 2,
                        "license": {"spdx_id": "MIT"}, "description": "description", "default_branch": "main"}
            def get_html(self, path, token=None, cache_ttl=0):
                self.paths.append(path)
                return '<div data-path="docs/README.md"><h1>Hello</h1><script>bad()</script></div>'
        client = Client()
        basic = repo_detail(client, "owner/sample")
        self.assertEqual(len(client.paths), 1)
        self.assertEqual(basic["forks"], 8)
        detailed = repo_detail(client, "owner/sample", include_readme=True)
        self.assertIn("<h1>Hello</h1>", detailed["readme_html"])
        self.assertEqual(detailed["readme_path"], "docs/README.md")
        self.assertEqual(len(client.paths), 3)
        with self.assertRaises(ValueError):
            repo_detail(client, "owner/../../bad")

    def test_readme_failure_keeps_repository_detail(self):
        class Client:
            def get_json(self, path, token=None, cache_ttl=0):
                return {"full_name": "owner/sample", "forks_count": 3}
            def get_html(self, path, token=None, cache_ttl=0):
                raise GitHubError("README 内容过大", 413)
        detail = repo_detail(Client(), "owner/sample", include_readme=True)
        self.assertEqual(detail["forks"], 3)
        self.assertEqual(detail["readme_error"], "README 内容过大")
        self.assertNotIn("readme_html", detail)

    def test_html_media_type_is_cached_separately_and_bounded(self):
        calls = []
        def transport(url, headers):
            calls.append(headers["Accept"])
            if "html" in headers["Accept"]:
                return 200, b"<h1>Rendered</h1>", {"X-RateLimit-Resource": "core"}
            return 200, b'{"content":"json"}', {"X-RateLimit-Resource": "core"}
        client = GitHubClient(MemoryCache(), transport)
        self.assertEqual(client.get_html("/repos/owner/sample/readme"), "<h1>Rendered</h1>")
        self.assertEqual(client.get_html("/repos/owner/sample/readme"), "<h1>Rendered</h1>")
        self.assertEqual(client.get_json("/repos/owner/sample/readme", cache_ttl=3600)["content"], "json")
        self.assertEqual(len(calls), 2)
        large = GitHubClient(MemoryCache(), lambda *_: (200, b"<" + b"x" * 750_001, {}))
        with self.assertRaises(GitHubError):
            large.get_html("/repos/owner/sample/readme")


if __name__ == "__main__":
    unittest.main()
