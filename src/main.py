"""Send GitHub Trending repositories to Telegram without a permanent server."""

from __future__ import annotations

import argparse
import html
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


LOG = logging.getLogger("github-trending-bot")
GITHUB_TRENDING_URL = "https://github.com/trending"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
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
        f"<b>ترندهای {html.escape(PERIOD_LABELS[period])} GitHub</b>",
        f"<i>{now:%Y-%m-%d %H:%M} به وقت تهران</i>",
        "",
    ]
    for index, repo in enumerate(repositories, 1):
        explanation = explain_in_persian(repo, client=client, model=model)
        stats = f"⭐ {html.escape(repo.stars)}  |  🍴 {html.escape(repo.forks)}  |  🧩 {html.escape(repo.language)}"
        if repo.stars_gained:
            stats += f"  |  📈 {html.escape(repo.stars_gained)}"
        lines.extend(
            [
                f"<b>{index}. <a href=\"{html.escape(repo.url, quote=True)}\">{html.escape(repo.name)}</a></b>",
                stats,
                html.escape(explanation),
                "",
            ]
        )
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
        return [period] if period != "all" else list(PERIOD_LABELS)
    weekly_day = int(os.getenv("WEEKLY_DAY", "0"))
    monthly_day = int(os.getenv("MONTHLY_DAY", "1"))
    result = ["daily"]
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
    parser.add_argument("--period", choices=["auto", "daily", "weekly", "monthly", "all"], default="auto")
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
        LOG.info("Fetching %s GitHub trends", period)
        repositories = fetch_trending(period, limit, github_token)
        if not repositories:
            raise RuntimeError(f"No repositories found for {period}")
        message = build_message(period, repositories, client=client, model=gemini_model)
        send_telegram(message, os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip())
        LOG.info("Sent %s trends", period)
    return 0


if __name__ == "__main__":
    sys.exit(main())
