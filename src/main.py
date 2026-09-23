"""Send GitHub Trending repositories to Telegram without a permanent server."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


LOG = logging.getLogger("github-trending-bot")
GITHUB_TRENDING_URL = "https://github.com/trending"
GITHUB_API_URL = "https://api.github.com"
HACKER_NEWS_API_URL = "https://hacker-news.firebaseio.com/v0"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
DEFAULT_DOTNET_REPOSITORIES = (
    "dotnet/runtime",
    "dotnet/aspnetcore",
    "dotnet/sdk",
    "dotnet/roslyn",
    "dotnet/efcore",
    "dotnet/maui",
    "dotnet/roslyn-analyzers",
    "dotnet/templating",
    "dotnet/msbuild",
    "dotnet/diagnostics",
)
PERIOD_LABELS = {
    "daily": "روزانه",
    "weekly": "هفتگی",
    "monthly": "ماهانه",
}
PERIOD_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}


@dataclass(frozen=True)
class Repository:
    name: str
    url: str
    description: str
    language: str
    stars: str
    forks: str
    stars_gained: str
    source: str = "GitHub"
    source_score: str = ""


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _number_from_text(value: str) -> str:
    value = _clean(value)
    return value.replace(",", "") or "0"


def _find_stat(article: BeautifulSoup, suffix: str) -> str:
    for link in article.select("a[href]"):
        href = link.get("href", "")
        if href.rstrip("/").endswith(suffix):
            return _number_from_text(link.get_text(" ", strip=True))
    return "0"


def parse_trending_html(markup: str, limit: int = 10) -> list[Repository]:
    """Parse GitHub's public trending HTML, keeping this independent of the network."""
    soup = BeautifulSoup(markup, "html.parser")
    repositories: list[Repository] = []
    for article in soup.select("article.Box-row"):
        anchor = article.select_one("h2 a[href]")
        if not anchor:
            continue

        path = anchor.get("href", "").strip()
        if not path.startswith("/"):
            continue
        name = _clean(anchor.get_text(" ", strip=True)).replace(" ", "")
        if not name:
            continue

        description_node = article.select_one("p")
        language_node = article.select_one('[itemprop="programmingLanguage"]')
        stars_node = article.select_one('a[href$="/stargazers"]')
        gained = ""
        for text in article.stripped_strings:
            if re.search(r"stars?\s+(today|this week|this month)", text, re.I):
                gained = _clean(text)
                break

        repositories.append(
            Repository(
                name=name,
                url=urljoin("https://github.com", path),
                description=_clean(description_node.get_text(" ", strip=True) if description_node else ""),
                language=_clean(language_node.get_text(" ", strip=True) if language_node else "نامشخص"),
                stars=_number_from_text(stars_node.get_text(" ", strip=True) if stars_node else "0"),
                forks=_find_stat(article, "/forks"),
                stars_gained=gained,
            )
        )
        if len(repositories) >= limit:
            break
    return repositories


def fetch_trending(period: str, limit: int, github_token: str = "") -> list[Repository]:
    """Fetch GitHub Trending, with a recent-repositories fallback if HTML is unavailable."""
    if period not in PERIOD_LABELS:
        raise ValueError(f"Unsupported period: {period}")

    headers = {
        "User-Agent": "github-trending-telegram-bot/1.0",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        response = requests.get(
            f"{GITHUB_TRENDING_URL}?since={period}", headers=headers, timeout=30
        )
        response.raise_for_status()
        repositories = parse_trending_html(response.text, limit)
        if repositories:
            return repositories
    except requests.RequestException as exc:
        LOG.warning("GitHub Trending page unavailable: %s", exc)

    # A safe fallback for a temporary GitHub Trending HTML change/outage.
    return fetch_recently_created(limit, PERIOD_DAYS[period], github_token)


def fetch_recently_created(limit: int, days: int, github_token: str = "") -> list[Repository]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "github-trending-telegram-bot/1.0"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    response = requests.get(
        "https://api.github.com/search/repositories",
        params={"q": f"created:>{since}", "sort": "stars", "order": "desc", "per_page": limit},
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    items = response.json().get("items", [])
    return [
        Repository(
            name=item.get("full_name", "unknown"),
            url=item.get("html_url", "https://github.com"),
            description=_clean(item.get("description")),
            language=item.get("language") or "نامشخص",
            stars=str(item.get("stargazers_count", 0)),
            forks=str(item.get("forks_count", 0)),
            stars_gained="",
        )
        for item in items
    ]


def _github_repo_name(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if not owner or not repo:
        return None
    return f"{owner}/{repo}"


def _fetch_hacker_news_item(item_id: int) -> dict | None:
    response = requests.get(f"{HACKER_NEWS_API_URL}/item/{item_id}.json", timeout=15)
    response.raise_for_status()
    item = response.json()
    return item if isinstance(item, dict) else None


def _github_repository_from_hn(
    item: dict, github_token: str = ""
) -> Repository | None:
    full_name = _github_repo_name(item.get("url", ""))
    if not full_name:
        return None

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-trending-telegram-bot/1.0",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    try:
        response = requests.get(
            f"{GITHUB_API_URL}/repos/{full_name}", headers=headers, timeout=20
        )
        response.raise_for_status()
        data = response.json()
        return Repository(
            name=data.get("full_name", full_name),
            url=data.get("html_url", f"https://github.com/{full_name}"),
            description=_clean(data.get("description")) or _clean(item.get("title")),
            language=data.get("language") or "نامشخص",
            stars=str(data.get("stargazers_count", 0)),
            forks=str(data.get("forks_count", 0)),
            stars_gained="",
            source="Hacker News",
            source_score=str(item.get("score", 0)),
        )
    except requests.RequestException as exc:
        LOG.debug("Could not enrich HN repository %s from GitHub: %s", full_name, exc)
        return Repository(
            name=full_name,
            url=f"https://github.com/{full_name}",
            description=_clean(item.get("title")),
            language="نامشخص",
            stars="0",
            forks="0",
            stars_gained="",
            source="Hacker News",
            source_score=str(item.get("score", 0)),
        )


def fetch_hacker_news(
    period: str, limit: int, github_token: str = ""
) -> list[Repository]:
    """Find recent GitHub repositories discussed on Hacker News."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=PERIOD_DAYS[period])
    try:
        story_ids: list[int] = []
        # Keep the daily run bounded: both feeds overlap heavily, and fetching
        # the first 40 from each is enough to find the requested repositories.
        for feed in ("topstories", "newstories"):
            response = requests.get(f"{HACKER_NEWS_API_URL}/{feed}.json", timeout=15)
            response.raise_for_status()
            story_ids.extend(response.json()[:40])

        unique_ids = list(dict.fromkeys(story_ids))
        items: list[dict] = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = [executor.submit(_fetch_hacker_news_item, item_id) for item_id in unique_ids]
            for future in as_completed(futures):
                try:
                    item = future.result()
                except (requests.RequestException, ValueError, TypeError):
                    continue
                if (
                    item
                    and item.get("type") == "story"
                    and item.get("time")
                    and datetime.fromtimestamp(item["time"], timezone.utc) >= cutoff
                    and _github_repo_name(item.get("url", ""))
                ):
                    items.append(item)

        items.sort(key=lambda item: (item.get("score", 0), item.get("time", 0)), reverse=True)
        repositories: list[Repository] = []
        seen: set[str] = set()
        for item in items:
            full_name = _github_repo_name(item.get("url", ""))
            if not full_name or full_name.lower() in seen:
                continue
            repository = _github_repository_from_hn(item, github_token)
            if repository:
                repositories.append(repository)
                seen.add(full_name.lower())
            if len(repositories) >= limit:
                break
        return repositories
    except (requests.RequestException, ValueError, TypeError) as exc:
        LOG.warning("Hacker News feed unavailable: %s", exc)
        return []


def _repository_key(repository: Repository) -> str:
    return (_github_repo_name(repository.url) or repository.name).lower()


def merge_sources(
    github_repositories: list[Repository],
    hacker_news_repositories: list[Repository],
    limit: int,
) -> list[Repository]:
    """Return up to ``limit`` repositories from each source, without duplicates."""
    result: list[Repository] = []
    seen: set[str] = set()

    def add(repository: Repository) -> None:
        key = _repository_key(repository)
        if key not in seen and len(result) < limit * 2:
            result.append(repository)
            seen.add(key)

    for repository in github_repositories[:limit]:
        add(repository)
    hacker_news_count = 0
    for repository in hacker_news_repositories:
        before = len(result)
        add(repository)
        if len(result) > before:
            hacker_news_count += 1
        if hacker_news_count >= limit:
            break
    return result


def _github_headers(github_token: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-trending-telegram-bot/1.0",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    return headers


def _github_api_get(path: str, github_token: str = "", params: dict | None = None):
    response = requests.get(
        f"{GITHUB_API_URL}{path}",
        headers=_github_headers(github_token),
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _github_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _dotnet_repositories() -> list[str]:
    configured = os.getenv("DOTNET_REPOSITORIES", "").strip()
    repositories = configured.split(",") if configured else DEFAULT_DOTNET_REPOSITORIES
    return list(dict.fromkeys(repository.strip() for repository in repositories if repository.strip()))


def _dotnet_repo_activity(
    repository: str,
    cutoff: datetime,
    github_token: str,
    max_items: int,
) -> dict[str, list[dict]]:
    activity = {"merges": [], "releases": [], "issues": [], "commits": []}
    try:
        pulls = _github_api_get(
            f"/repos/{repository}/pulls",
            github_token,
            {"state": "closed", "sort": "updated", "direction": "desc", "per_page": 50},
        )
        for pull in pulls:
            merged_at = _github_datetime(pull.get("merged_at"))
            if not merged_at or merged_at < cutoff:
                continue
            activity["merges"].append(
                {
                    "repo": repository,
                    "title": _clean(pull.get("title")) or f"PR #{pull.get('number')}",
                    "url": pull.get("html_url", f"https://github.com/{repository}/pulls"),
                    "author": _clean((pull.get("user") or {}).get("login")),
                    "timestamp": pull.get("merged_at", ""),
                }
            )
            if len(activity["merges"]) >= max_items:
                break

        releases = _github_api_get(
            f"/repos/{repository}/releases",
            github_token,
            {"per_page": 20},
        )
        for release in releases:
            published_at = _github_datetime(release.get("published_at") or release.get("created_at"))
            if not published_at or published_at < cutoff:
                continue
            activity["releases"].append(
                {
                    "repo": repository,
                    "title": _clean(release.get("name")) or _clean(release.get("tag_name")) or "Release",
                    "tag": _clean(release.get("tag_name")),
                    "url": release.get("html_url", f"https://github.com/{repository}/releases"),
                    "timestamp": release.get("published_at") or release.get("created_at", ""),
                }
            )
            if len(activity["releases"]) >= max_items:
                break

        issues = _github_api_get(
            f"/repos/{repository}/issues",
            github_token,
            {"state": "all", "sort": "updated", "direction": "desc", "per_page": 50},
        )
        recent_issues = []
        for issue in issues:
            if issue.get("pull_request"):
                continue
            updated_at = _github_datetime(issue.get("updated_at"))
            if not updated_at or updated_at < cutoff:
                continue
            labels = [
                _clean(label.get("name"))
                for label in issue.get("labels", [])
                if _clean(label.get("name"))
            ]
            recent_issues.append(
                {
                    "repo": repository,
                    "title": _clean(issue.get("title")) or f"Issue #{issue.get('number')}",
                    "url": issue.get("html_url", f"https://github.com/{repository}/issues"),
                    "comments": int(issue.get("comments", 0) or 0),
                    "labels": labels,
                    "timestamp": issue.get("updated_at", ""),
                }
            )
        recent_issues.sort(
            key=lambda item: (item["comments"], item.get("timestamp", "")), reverse=True
        )
        activity["issues"] = recent_issues[:max_items]

        commits = _github_api_get(
            f"/repos/{repository}/commits",
            github_token,
            {"since": cutoff.isoformat(), "per_page": 50},
        )
        if commits:
            activity["commits"].append(
                {
                    "repo": repository,
                    "count": len(commits),
                    "subjects": [
                        _clean((commit.get("commit") or {}).get("message", "")).split("\n", 1)[0]
                        for commit in commits[:3]
                        if _clean((commit.get("commit") or {}).get("message", ""))
                    ],
                    "url": f"https://github.com/{repository}/commits",
                }
            )
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        LOG.warning("Could not inspect .NET repository %s: %s", repository, exc)
    return activity


def fetch_dotnet_updates(
    lookback_hours: int = 26,
    max_items: int = 10,
    github_token: str = "",
) -> dict[str, list[dict]]:
    """Collect notable activity from Microsoft's official dotnet GitHub organization."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    combined = {"merges": [], "releases": [], "issues": [], "commits": []}
    repositories = _dotnet_repositories()
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(repositories)))) as executor:
        futures = {
            executor.submit(
                _dotnet_repo_activity, repository, cutoff, github_token, max_items
            ): repository
            for repository in repositories
        }
        for future in as_completed(futures):
            repository = futures[future]
            try:
                activity = future.result()
            except Exception as exc:  # keep one unavailable repo from blocking the digest
                LOG.warning(".NET repository task failed for %s: %s", repository, exc)
                continue
            for category, items in activity.items():
                combined[category].extend(items)

    combined["merges"].sort(key=lambda item: item.get("timestamp", ""), reverse=True)
    combined["releases"].sort(key=lambda item: item.get("timestamp", ""), reverse=True)
    combined["issues"].sort(
        key=lambda item: (item.get("comments", 0), item.get("timestamp", "")), reverse=True
    )
    combined["merges"] = combined["merges"][:max_items]
    combined["releases"] = combined["releases"][:max_items]
    combined["issues"] = combined["issues"][:max_items]
    combined["commits"] = sorted(
        combined["commits"], key=lambda item: item["count"], reverse=True
    )[:max_items]
    return combined


def fallback_explanation(repo: Repository) -> str:
    if repo.description:
        return f"این پروژه دربارهٔ «{repo.description}» است و می‌تواند برای بررسی و یادگیری بیشتر مناسب باشد."
    return "توضیح رسمی برای این پروژه در صفحهٔ GitHub ثبت نشده است؛ برای شناخت دقیق‌تر، فایل README را ببینید."


def explain_in_persian(repo: Repository, client=None, model: str = "gemini-3.5-flash-lite") -> str:
    if client is None:
        return fallback_explanation(repo)

    prompt = f"""
تو یک سردبیر فنی فارسی‌زبان هستی. برای ریپوی زیر یک توضیح کوتاه و دقیق فارسی بنویس.
حداکثر ۳ جمله، بدون تیتر و بدون Markdown. اگر توضیح رسمی مبهم است، ادعای قطعی جدید نساز.
نام ریپو: {repo.name}
زبان: {repo.language}
توضیح رسمی GitHub: {repo.description or 'ثبت نشده'}
""".strip()
    try:
        from google.genai import types

        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction="پاسخ را فقط به فارسی روان و فنی برگردان.",
                temperature=0.2,
                max_output_tokens=180,
            ),
        )
        text = _clean(getattr(response, "text", ""))
        return text or fallback_explanation(repo)
    except Exception as exc:  # keep delivery alive if one AI request fails
        LOG.warning("Persian explanation failed for %s: %s", repo.name, exc)
        return fallback_explanation(repo)


def build_message(period: str, repositories: Iterable[Repository], client=None, model: str = "gemini-3.5-flash-lite") -> str:
    now = datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Tehran")))
    lines = [
        f"<b>ترندهای {html.escape(PERIOD_LABELS[period])} GitHub و Hacker News</b>",
        f"<i>{now:%Y-%m-%d %H:%M} به وقت تهران</i>",
        "",
    ]
    for index, repo in enumerate(repositories, 1):
        explanation = explain_in_persian(repo, client=client, model=model)
        stats = f"⭐ {html.escape(repo.stars)}  |  🍴 {html.escape(repo.forks)}  |  🧩 {html.escape(repo.language)}"
        if repo.stars_gained:
            stats += f"  |  📈 {html.escape(repo.stars_gained)}"
        if repo.source != "GitHub":
            stats += f"  |  📰 {html.escape(repo.source)}"
            if repo.source_score:
                stats += f"  |  ⬆️ {html.escape(repo.source_score)} امتیاز"
        lines.extend(
            [
                f"<b>{index}. <a href=\"{html.escape(repo.url, quote=True)}\">{html.escape(repo.name)}</a></b>",
                stats,
                html.escape(explanation),
                "",
            ]
        )
    return "\n".join(lines).strip()


def _dotnet_fallback_report(updates: dict[str, list[dict]]) -> dict[str, list[str]]:
    """Build a Persian-only report when the translation service is unavailable."""
    return {
        "summary": [
            f"در ۲۶ ساعت گذشته {len(updates.get('merges', []))} تغییر در پروژه‌های رسمی .NET ادغام شده است.",
            f"{len(updates.get('releases', []))} انتشار، {len(updates.get('issues', []))} ایراد فعال و خلاصهٔ تغییرات کد بررسی شد.",
        ],
        # Do not invent a useless description or leak raw English titles when
        # Gemini is unavailable. The overview remains useful; item sections
        # are omitted until a real Persian summary is available.
        "merges": [],
        "releases": [],
        "issues": [],
        "commits": [],
    }


def _parse_json_response(value: str) -> dict:
    value = value.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE | re.DOTALL).strip()
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI response did not contain a JSON object")
    parsed = json.loads(value[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("AI response was not a JSON object")
    return parsed


def _dotnet_localized_report(
    updates: dict[str, list[dict]], client=None, model: str = "gemini-3.5-flash-lite"
) -> dict[str, list[str]]:
    fallback = _dotnet_fallback_report(updates)
    if client is None:
        return fallback

    compact = {
        "merges": [
            {"repo": item.get("repo", ""), "title": item.get("title", "")}
            for item in updates.get("merges", [])[:10]
        ],
        "releases": [
            {"repo": item.get("repo", ""), "tag": item.get("tag", ""), "title": item.get("title", "")}
            for item in updates.get("releases", [])[:10]
        ],
        "issues": [
            {"repo": item.get("repo", ""), "title": item.get("title", ""), "comments": item.get("comments", 0)}
            for item in updates.get("issues", [])[:10]
        ],
        "commits": [
            {"repo": item.get("repo", ""), "count": item.get("count", 0), "subjects": item.get("subjects", [])}
            for item in updates.get("commits", [])[:10]
        ],
    }
    prompt = f"""
تو ویراستار فنی فارسی‌زبان هستی. دادهٔ زیر فعالیت ۲۶ ساعت اخیر پروژه‌های رسمی .NET مایکروسافت است.
فقط یک JSON معتبر برگردان و هیچ متن دیگری ننویس. مقدار همهٔ رشته‌ها باید فارسی روان و کوتاه باشد.
ساختار خروجی دقیقاً این باشد:
{{"summary": ["..."], "merges": ["..."], "releases": ["..."], "issues": ["..."], "commits": ["..."]}}
آرایه‌های merges، releases، issues و commits را دقیقاً به همان ترتیب ورودی و با همان تعداد برگردان.
برای هر Merge و commit، دقیقاً بگو چه چیزی تغییر کرده و اثر یا دلیل اهمیت آن چیست؛ از جملهٔ کلی مثل «یک تغییر مهم انجام شد» استفاده نکن.
برای issue، خود مشکل را به فارسی خلاصه کن و بگو برای کدام قابلیت یا گروه از کاربران مهم است؛ تعداد نظر به‌تنهایی خلاصه محسوب نمی‌شود.
برای release، قابلیت یا اصلاح اصلی را فارسی و کوتاه توضیح بده.
برای هر مورد حداکثر یک جملهٔ کوتاه بنویس. در summary حداکثر ۴ نکتهٔ مهم بنویس. از متن انگلیسی طولانی، Markdown، bullet marker و تکرار عنوان خام خودداری کن.
نام‌های فنی ضروری مانند .NET، MAUI، NativeAOT، API، JSON، SDK و نام ریپوها را می‌توانی حفظ کنی.
داده:
{json.dumps(compact, ensure_ascii=False)}
""".strip()
    try:
        from google.genai import types

        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction="فقط JSON معتبر با متن فارسی تولید کن.",
                temperature=0.1,
                max_output_tokens=1600,
            ),
        )
        parsed = _parse_json_response(getattr(response, "text", ""))
        result: dict[str, list[str]] = {}
        for key in ("summary", "merges", "releases", "issues", "commits"):
            values = parsed.get(key)
            if not isinstance(values, list):
                result[key] = fallback[key]
                continue
            localized_values = [
                _clean(value) for value in values if isinstance(value, str) and value.strip()
            ]
            if key == "summary" and not localized_values:
                result[key] = fallback[key]
                continue
            result[key] = localized_values[: len(fallback[key]) or len(localized_values)]
        return result
    except Exception as exc:
        LOG.warning(".NET Persian localization failed; using safe fallback: %s", exc)
        return fallback


def _dotnet_link_line(item: dict, description: str, prefix: str = "") -> str:
    repo = html.escape(item.get("repo", ""))
    url = html.escape(item.get("url", "https://github.com/dotnet"), quote=True)
    suffix = f" — نویسنده: {html.escape(item['author'])}" if item.get("author") else ""
    if item.get("tag"):
        suffix += f" — نسخهٔ {html.escape(item['tag'])}"
    if item.get("comments"):
        suffix += f" — 💬 {item['comments']} نظر"
    return f"• {prefix}<a href=\"{url}\">{repo}</a>: {html.escape(description)}{suffix}"


def build_dotnet_message(
    updates: dict[str, list[dict]], client=None, model: str = "gemini-3.5-flash-lite"
) -> str:
    """Build a compact daily digest of notable official .NET activity."""
    now = datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Tehran")))
    merges = updates.get("merges", [])
    releases = updates.get("releases", [])
    issues = updates.get("issues", [])
    commits = updates.get("commits", [])
    localized = _dotnet_localized_report(updates, client=client, model=model)
    total_commits = sum(item.get("count", 0) for item in commits)
    lines = [
        "<b>گزارش روزانه پروژه‌های .NET مایکروسافت</b>",
        f"<i>{now:%Y-%m-%d %H:%M} به وقت تهران</i>",
        f"ادغام: {len(merges)}  |  انتشار: {len(releases)}  |  ایراد مهم: {len(issues)}  |  تغییر کد: {total_commits}",
        "",
        "<b>جمع‌بندی فارسی</b>",
    ]
    lines.extend(f"• {html.escape(item)}" for item in localized["summary"])
    lines.append("")
    if releases and localized["releases"]:
        lines.append("<b>🚀 انتشارهای جدید</b>")
        lines.extend(
            _dotnet_link_line(item, description)
            for item, description in zip(releases, localized["releases"])
        )
        lines.append("")
    if merges and localized["merges"]:
        lines.append("<b>🔀 ادغام‌های جدید</b>")
        lines.extend(
            _dotnet_link_line(item, description)
            for item, description in zip(merges, localized["merges"])
        )
        lines.append("")
    if issues and localized["issues"]:
        lines.append("<b>🔥 ایرادهای فعال</b>")
        lines.extend(
            _dotnet_link_line(item, description)
            for item, description in zip(issues, localized["issues"])
        )
        lines.append("")
    if commits and localized["commits"]:
        lines.append("<b>🧱 تغییرات کد</b>")
        for item, description in zip(commits, localized["commits"]):
            repo = html.escape(item.get("repo", ""))
            url = html.escape(item.get("url", "https://github.com/dotnet"), quote=True)
            lines.append(
                f"• <a href=\"{url}\">{repo}</a>: {html.escape(description)} "
                f"(تعداد تغییرات: {item.get('count', 0)})"
            )
    if not any((merges, releases, issues, commits)):
        lines.append("در بازهٔ بررسی‌شده تغییر قابل‌توجهی پیدا نشد؛ این خودش خبر خوبی برای یک روز آرام است. 🌱")
    return "\n".join(lines).strip()


def split_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(block) > limit:
            chunks.append(block[:limit])
            block = block[limit:]
        current = block
    if current:
        chunks.append(current)
    return chunks


def send_telegram(text: str, token: str, chat_id: str) -> None:
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    url = TELEGRAM_API_URL.format(token=token)
    for chunk in split_message(text):
        response = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram error: {body.get('description', body)}")
        time.sleep(0.25)


def periods_for_run(period: str, now: datetime) -> list[str]:
    if period != "auto":
        return [period] if period != "all" else [*PERIOD_LABELS, "dotnet"]
    weekly_day = int(os.getenv("WEEKLY_DAY", "0"))
    monthly_day = int(os.getenv("MONTHLY_DAY", "1"))
    result = ["daily", "dotnet"]
    if now.weekday() == weekly_day:
        result.append("weekly")
    if now.day == monthly_day:
        result.append("monthly")
    return result


def make_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        LOG.warning("GEMINI_API_KEY is empty; using GitHub descriptions as fallback")
        return None
    from google import genai

    return genai.Client(api_key=api_key)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--period",
        choices=["auto", "daily", "weekly", "monthly", "dotnet", "all"],
        default="auto",
    )
    args = parser.parse_args()

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")
    timezone_name = os.getenv("TIMEZONE", "Asia/Tehran")
    now = datetime.now(ZoneInfo(timezone_name))
    periods = periods_for_run(args.period, now)
    limit = max(1, min(int(os.getenv("TOP_N", "10")), 20))
    github_token = os.getenv("GITHUB_TOKEN", "").strip()
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    client = make_gemini_client()

    for period in periods:
        if period == "dotnet":
            LOG.info("Fetching daily Microsoft .NET activity")
            updates = fetch_dotnet_updates(
                lookback_hours=max(1, int(os.getenv("DOTNET_LOOKBACK_HOURS", "26"))),
                max_items=max(1, min(int(os.getenv("DOTNET_MAX_ITEMS", "10")), 20)),
                github_token=github_token,
            )
            message = build_dotnet_message(updates, client=client, model=gemini_model)
            send_telegram(
                message,
                os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
                os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            )
            LOG.info("Sent Microsoft .NET activity digest")
            continue
        LOG.info("Fetching %s GitHub and Hacker News trends", period)
        github_repositories = fetch_trending(period, limit, github_token)
        hacker_news_repositories = fetch_hacker_news(period, limit * 2, github_token)
        repositories = merge_sources(github_repositories, hacker_news_repositories, limit)
        if not repositories:
            raise RuntimeError(f"No repositories found for {period}")
        message = build_message(period, repositories, client=client, model=gemini_model)
        send_telegram(message, os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip())
        LOG.info("Sent %s trends", period)
    return 0


if __name__ == "__main__":
    sys.exit(main())
