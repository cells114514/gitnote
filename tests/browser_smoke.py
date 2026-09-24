"""Optional browser smoke test: start app.py and run `python tests/browser_smoke.py`."""

import json
import os
from pathlib import Path
import tempfile
from playwright.sync_api import sync_playwright


def item(number, name):
    return {"id": f"gh-{number}", "name": name, "full_name": f"owner/{name}", "author": "owner",
            "summary": "A public project", "language": "Python", "topics": ["tools"],
            "stars": max(0, 1000 - number), "source_url": f"https://github.com/owner/{name}"}


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        discovery_calls = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def discover(route):
            body = route.request.post_data_json
            number = len(discovery_calls) + 1
            discovery_calls.append(body)
            if number == 3:
                route.fulfill(status=429, content_type="application/json",
                              body=json.dumps({"error": "测试限流", "retry_at": 0}))
                return
            result = {"items": [item(1000 + number, f"batch-{number}")],
                      "profile": body["profile"], "memory": body["memory"],
                      "incomplete": False, "rate": {"resources": {}}}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

        def listings(route):
            body = route.request.post_data_json
            path = route.request.url.split("/api/")[-1]
            if path == "search":
                number = body.get("page", 1)
                result = {"items": [item(number, f"search-{number}")], "page": number,
                          "total_count": 60, "has_more": number == 1, "rate": {"resources": {}}}
            elif path == "stars-top":
                result = {"items": [item(10, "stars-top")], "page": 1,
                          "total_count": 1, "has_more": False, "rate": {"resources": {}}}
            else:
                result = {"items": [{**item(20, "trending-top"), "trending_stars": 42}],
                          "period": body.get("period", "daily"), "fetched_at": "2026-09-24T00:00:00Z",
                          "source_url": "https://github.com/trending", "rate": {"resources": {}}}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

        page.route("**/api/discover", discover)
        page.route("**/api/search", listings)
        page.route("**/api/stars-top", listings)
        page.route("**/api/trending", listings)
        page.route("**/api/repo-detail", lambda route: route.fulfill(status=200,
            content_type="application/json", body=json.dumps({"forks": 2, "open_issues": 1,
                "readme_html": '<h1>Test README</h1><table><tr><td>Cell</td></tr></table><pre><code>print(1)</code></pre><img src="https://example.org/tracker.png" alt="Chart"><script>window.__gitnote_xss = true</script><iframe src="https://example.org/"></iframe><a href="javascript:alert(1)">bad link</a>',
                "default_branch": "main", "readme_path": "README.md", "description": "A public project"})))
        page.goto(os.environ.get("GITNOTE_TEST_URL", "http://127.0.0.1:8765/"), wait_until="networkidle")
        assert page.title().startswith("gitnote")
        page.locator(".project-card").filter(has_text="batch-1").wait_for()
        assert len(discovery_calls) == 1
        assert "换一批" in page.locator("#discoverButton").inner_text()
        page.locator(".project-card").filter(has_text="batch-1").locator('[data-action="save"]').click()
        page.evaluate("() => { const button = document.querySelector('#discoverButton'); button.click(); button.click(); }")
        page.locator(".project-card").filter(has_text="batch-2").wait_for()
        assert len(discovery_calls) == 2
        assert page.locator(".project-card").filter(has_text="batch-1").count() == 0
        page.locator("#discoverButton").click()
        page.get_by_text("测试限流").first.wait_for()
        assert len(discovery_calls) == 3
        assert page.locator(".project-card").filter(has_text="batch-2").count() == 1
        page.locator('[data-mode="saved"]').click()
        page.locator(".project-card").filter(has_text="batch-1").wait_for()
        page.locator('[data-mode="recommended"]').click()
        page.locator("#themeButton").click()
        assert page.locator("html").get_attribute("data-theme") == "dark"
        page.reload(wait_until="networkidle")
        assert page.locator("html").get_attribute("data-theme") == "dark"
        assert len(discovery_calls) == 4
        assert page.evaluate("getComputedStyle(document.documentElement).backgroundColor") == "rgb(30, 30, 30)"
        page.locator("#search").fill("parser")
        page.locator("#remoteSearch").click()
        page.locator(".project-card").filter(has_text="search-1").wait_for()
        assert page.locator("#loadMore").is_visible()
        page.locator("#loadMore").click()
        page.locator(".project-card").filter(has_text="search-2").wait_for()
        page.locator(".project-card").filter(has_text="search-1").click(position={"x": 100, "y": 100})
        page.locator("#readmeMount h1").get_by_text("Test README").wait_for()
        assert page.locator("#readmeMount table").count() == 1
        assert page.locator("#readmeMount script").count() == 0
        assert page.locator("#readmeMount iframe").count() == 0
        assert page.locator("#readmeMount a").filter(has_text="bad link").get_attribute("href") is None
        assert page.evaluate("window.__gitnote_xss") is None
        assert page.locator(".readme-image-toggle").count() == 1
        page.locator("#detailClose").click()
        page.locator('[data-mode="stars"]').click()
        page.locator(".project-card").filter(has_text="stars-top").wait_for()
        page.locator('[data-mode="trending"]').click()
        page.locator(".project-card").filter(has_text="trending-top").wait_for()
        assert not errors, errors
        page.screenshot(path=str(Path(tempfile.gettempdir()) / "gitnote-browser-smoke.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator(".project-card").first.focus()
        page.keyboard.press("Enter")
        assert page.locator("#detailDialog").is_visible()
        assert page.locator("#detailDialog").bounding_box()["width"] <= 390
        page.screenshot(path=str(Path(tempfile.gettempdir()) / "gitnote-browser-smoke-mobile.png"), full_page=True)
        page.keyboard.press("Escape")
        assert not page.locator("#detailDialog").is_visible()
        if os.environ.get("GITNOTE_REAL_README") == "1":
            live = browser.new_page(viewport={"width": 1280, "height": 900})
            def live_discover(route):
                body = route.request.post_data_json
                project = {**item(9000, "requests"), "full_name": "psf/requests",
                           "author": "psf", "source_url": "https://github.com/psf/requests"}
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "items": [project], "profile": body["profile"], "memory": body["memory"],
                    "rate": {"resources": {}}}))
            live.route("**/api/discover", live_discover)
            live.goto(os.environ.get("GITNOTE_TEST_URL", "http://127.0.0.1:8765/"), wait_until="networkidle")
            live.locator(".project-card").filter(has_text="requests").click(position={"x": 100, "y": 100})
            live.locator("#readmeMount h1").first.wait_for(timeout=20000)
            assert "Requests" in live.locator("#readmeMount h1").first.inner_text()
            live.screenshot(path=str(Path(tempfile.gettempdir()) / "gitnote-real-readme.png"))
            live.close()
        browser.close()
        print("Browser smoke passed")


if __name__ == "__main__":
    main()
