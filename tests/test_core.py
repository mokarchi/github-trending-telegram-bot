from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from main import build_dotnet_message, parse_trending_html, periods_for_run, split_message


HTML = """
<article class="Box-row">
  <h2><a href="/owner/repo">owner / repo</a></h2>
  <p>A useful project</p>
  <span itemprop="programmingLanguage">Python</span>
  <a href="/owner/repo/stargazers">12,345</a>
  <a href="/owner/repo/forks">678</a>
  <span>321 stars today</span>
</article>
"""


def test_parse_trending_html():
    repos = parse_trending_html(HTML, limit=10)
    assert len(repos) == 1
    assert repos[0].name == "owner/repo"
    assert repos[0].stars == "12345"
    assert repos[0].language == "Python"


def test_split_message():
    chunks = split_message("a" * 100 + "\n\n" + "b" * 100, limit=120)
    assert len(chunks) == 2
    assert all(len(chunk) <= 120 for chunk in chunks)


def test_periods_for_run():
    assert periods_for_run("daily", None) == ["daily"]


def test_dotnet_message_does_not_leak_raw_english_without_ai():
    updates = {
        "merges": [
            {
                "repo": "dotnet/runtime",
                "title": "Fix NativeAOT issue",
                "url": "https://github.com/dotnet/runtime/pull/1",
                "author": "developer",
            }
        ],
        "releases": [],
        "issues": [],
        "commits": [
            {
                "repo": "dotnet/runtime",
                "count": 2,
                "subjects": ["Fix NativeAOT issue"],
                "url": "https://github.com/dotnet/runtime/commits",
            }
        ],
    }
    message = build_dotnet_message(updates)
    assert "جمع‌بندی فارسی" in message
    assert "Fix NativeAOT issue" not in message
    assert "Mergeهای" not in message


def test_dotnet_message_uses_short_persian_ai_summaries():
    class FakeResponse:
        text = (
            '{"summary":["رفع یک مشکل مهم در NativeAOT"],'
            '"merges":["رفع مشکل اجرای وظایف JavaScript در NativeAOT."],'
            '"releases":[],"issues":[],'
            '"commits":["بهبود تولید کد و کاهش مصرف حافظه."]}'
        )

    class FakeModels:
        def generate_content(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        models = FakeModels()

    updates = {
        "merges": [{"repo": "dotnet/runtime", "title": "Fix NativeAOT issue", "url": "https://github.com/dotnet/runtime/pull/1"}],
        "releases": [],
        "issues": [],
        "commits": [{"repo": "dotnet/runtime", "count": 1, "subjects": ["Fix NativeAOT issue"], "url": "https://github.com/dotnet/runtime/commits"}],
    }
    message = build_dotnet_message(updates, client=FakeClient())
    assert "رفع مشکل اجرای وظایف JavaScript در NativeAOT" in message
    assert "بهبود تولید کد و کاهش مصرف حافظه" in message
    assert "Fix NativeAOT issue" not in message


if __name__ == "__main__":
    test_parse_trending_html()
    test_split_message()
    test_periods_for_run()
    print("offline tests passed")
