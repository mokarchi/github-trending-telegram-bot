from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from main import parse_trending_html, periods_for_run, split_message


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


if __name__ == "__main__":
    test_parse_trending_html()
    test_split_message()
    test_periods_for_run()
    print("offline tests passed")
