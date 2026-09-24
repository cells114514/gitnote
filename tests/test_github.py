import json
import io
import random
import unittest
from unittest.mock import patch

from cache import MemoryCache
from discovery import discover, normalize_repo, random_query
from github_auth import AuthManager
from github_client import GitHubClient, GitHubError
from taste_profile import (apply_session_decay, apply_signal, effective_score,
                           empty_profile, observe_repos, profile_strength, topic_idf)
from taste_ranking import enforce_diversity, rank_batch, sample_topic


def repo(repo_id, topic="python", language="Python"):
    return {"id": repo_id, "private": False, "archived": False,
            "html_url": f"https://github.com/example/repo-{repo_id}", "name": f"repo-{repo_id}",
            "full_name": f"example/repo-{repo_id}", "description": "A public repository",
            "owner": {"login": "example"}, "topics": [topic], "language": language,
            "stargazers_count": 111, "pushed_at": "2026-09-20T00:00:00Z"}


class TasteTests(unittest.TestCase):
    def test_rarity_attribution_confidence_and_suppression(self):
        profile = observe_repos(empty_profile(0), [{"topics": ["common"]}] * 9 + [{"topics": ["rare"]}])
        self.assertGreater(topic_idf(profile["corpus"], "rare"), topic_idf(profile["corpus"], "common"))
        scored = apply_signal(profile, {"kind": "star", "topics": ["common", "rare"], "language": "Python", "at": 100})
        self.assertGreater(scored["topics"]["rare"]["score"], scored["topics"]["common"]["score"])
        self.assertAlmostEqual(effective_score(scored["languages"]["Python"]), 1 / 6)
        suppressed = apply_signal(profile, {"kind": "star", "topics": ["common", "rare"],
                                            "language": "Python", "suppressed": ["rare"], "at": 100})
        self.assertNotIn("rare", suppressed["topics"])
        self.assertAlmostEqual(suppressed["topics"]["common"]["score"], 1)

    def test_decay_and_strength(self):
        profile = apply_signal(empty_profile(0), {"kind": "star", "topics": ["python"], "language": "Python", "at": 1})
        strength = profile_strength(profile)
        self.assertGreater(strength, 0)
        aged = apply_session_decay(profile, 2)
        self.assertAlmostEqual(aged["topics"]["python"]["score"], 0.977)
        self.assertEqual(aged["topics"]["python"]["events"], 1)
        self.assertLess(profile_strength(aged), strength)

    def test_topic_sampling_and_batch_diversity(self):
        profile = empty_profile(0)
        profile["topics"]["python"] = {"score": 500, "events": 100, "updatedAt": 0}
        self.assertEqual(sample_topic(profile, ["rust"], lambda: 0.99), "rust")
        self.assertEqual(sample_topic(empty_profile(0), ["a", "b"], lambda: 0.7), "b")
        projects = [{"topics": ["python"], "language": "Python", "id": i} for i in range(3)] + [
            {"topics": ["rust"], "language": "Rust", "id": 3}]
        ranked = rank_batch(profile, projects)
        self.assertEqual(ranked[2]["language"], "Rust")
        self.assertEqual(len(enforce_diversity(projects)), 4)

    def test_strong_single_topic_still_explores(self):
        profile = empty_profile(0)
        profile["topics"]["python"] = {"score": 5000, "events": 100, "updatedAt": 0}
        rng = random.Random(7)
        draws = [sample_topic(profile, ["rust", "go"], rng.random) for _ in range(5000)]
        self.assertLess(draws.count("python") / len(draws), 0.18)
        self.assertGreater(draws.count("python") / len(draws), 0.12)


class DiscoveryTests(unittest.TestCase):
    def test_query_uses_gittok_bounds_and_memory(self):
        profile = empty_profile(0)
        rng = random.Random(3)
        first = random_query(profile, ["python"], {}, rng)
        self.assertIn(first["q"].split()[0], [band for band, _ in __import__("discovery").STAR_BANDS])
        self.assertEqual(first["per_page"], 25)
        memory = {first["q"]: {"current_page": first["page"], "total_projects": 25}}
        second = random_query(profile, ["python"], memory, random.Random(3))
        self.assertEqual(second["page"], 1)

    def test_discovery_deduplicates_and_updates_corpus(self):
        class FakeClient:
            def __init__(self): self.calls = []
            def search(self, params, token=None):
                self.calls.append((params, token))
                return {"items": [repo(1), repo(1), repo(2, "rust", "Rust")], "total_count": 2}
        client = FakeClient()
        result = discover(client, empty_profile(0), seen=["gh-1"], rng=random.Random(1))
        self.assertEqual([item["id"] for item in result["items"]], ["gh-2"])
        self.assertEqual(result["profile"]["corpus"]["documents"], 1)
        self.assertEqual(len(client.calls), 3)
        self.assertFalse(normalize_repo({**repo(3), "private": True}))

    def test_waterfall_combines_topic_draws_for_language_variety(self):
        class FakeClient:
            def __init__(self): self.calls = 0
            def search(self, params, token=None):
                self.calls += 1
                language = ["Python", "Rust", "Java"][self.calls - 1]
                return {"items": [repo(self.calls * 100 + i, language.lower(), language) for i in range(25)],
                        "total_count": 25}
        client = FakeClient()
        result = discover(client, empty_profile(0), rng=random.Random(1))
        self.assertEqual(client.calls, 3)
        self.assertEqual(len(result["items"]), 36)
        self.assertEqual({item["language"] for item in result["items"]}, {"Python", "Rust", "Java"})


class ClientAndAuthTests(unittest.TestCase):
    def test_authenticated_search_header_cache_and_rate(self):
        calls = []
        def transport(url, headers):
            calls.append((url, headers))
            return 200, json.dumps({"items": [], "total_count": 0}).encode(), {
                "X-RateLimit-Resource": "search", "X-RateLimit-Limit": "30", "X-RateLimit-Remaining": "29"}
        client = GitHubClient(MemoryCache(), transport)
        params = {"q": "topic:python stars:50..200", "per_page": 25, "page": 1}
        client.search(params, token="secret")
        client.search(params, token="secret")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["Authorization"], "Bearer secret")
        self.assertEqual(client.rate_snapshot()["resources"]["search"]["remaining"], 29)

    def test_rate_limit_stops_followups(self):
        def transport(url, headers):
            return 429, b"{}", {"Retry-After": "60", "X-RateLimit-Resource": "search"}
        client = GitHubClient(MemoryCache(), transport)
        with self.assertRaises(GitHubError) as first:
            client.search({"q": "x"})
        with self.assertRaises(GitHubError) as second:
            client.search({"q": "y"})
        self.assertEqual(first.exception.status, 429)
        self.assertEqual(second.exception.status, 429)

    def test_auth_start_keeps_secret_out_of_url_and_validates_state(self):
        client = GitHubClient(MemoryCache(), lambda *_: (200, b'{}', {}))
        auth = AuthManager(client, "public-client", "private-secret")
        session, url = auth.start(8765)
        self.assertIn("code_challenge_method=S256", url)
        self.assertNotIn("private-secret", url)
        with self.assertRaises(GitHubError):
            auth.callback(session, "wrong", "code")

    def test_auth_callback_keeps_token_in_memory_and_rejects_old_scopes(self):
        def transport(url, headers):
            self.assertEqual(headers["Authorization"], "Bearer secret-token")
            return 200, b'{"login":"reader"}', {"X-RateLimit-Resource": "core"}
        auth = AuthManager(GitHubClient(MemoryCache(), transport), "client", "secret")
        session, _ = auth.start(8765)
        state = auth.sessions[session]["state"]
        with patch("github_auth.urlopen", return_value=io.BytesIO(b'{"access_token":"secret-token","scope":""}')):
            self.assertEqual(auth.callback(session, state, "one-time-code"), "reader")
        self.assertEqual(auth.status(session)["login"], "reader")
        self.assertNotIn("token", auth.status(session))
        auth.disconnect(session)
        self.assertIsNone(auth.token(session))
        session, _ = auth.start(8765)
        with patch("github_auth.urlopen", return_value=io.BytesIO(b'{"access_token":"secret-token","scope":"repo"}')):
            with self.assertRaises(GitHubError):
                auth.callback(session, auth.sessions[session]["state"], "another-code")


if __name__ == "__main__":
    unittest.main()
