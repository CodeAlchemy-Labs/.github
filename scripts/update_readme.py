#!/usr/bin/env python3
"""Regenerate the dynamic project and statistics sections of an organization README."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_BASE = "https://api.github.com"
DEFAULT_ORG = "CodeAlchemy-Labs"
DEFAULT_README_PATH = "profile/README.md"
EXCLUDED_REPOS = {".github"}

MARKER_PROJECTS_START = "<!-- PROJECTS_START -->"
MARKER_PROJECTS_END = "<!-- PROJECTS_END -->"
MARKER_STATS_START = "<!-- STATS_START -->"
MARKER_STATS_END = "<!-- STATS_END -->"

EMOJI_PATTERN = re.compile(
    "[\u00A9\u00AE\u203C\u2049\u2122\u2139\u2300-\u23FF"
    "\u25AA-\u25FF\u2600-\u27BF\u2B00-\u2BFF\u3030\u303D"
    "\u3297\u3299\U0001F000-\U0001FAFF\U0001FC00-\U0001FFFF"
    "\uFE0E\uFE0F\u200D\u20E3]"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger("update_readme")


class GitHubApiError(RuntimeError):
    """Raised when a required GitHub API request fails."""


def create_session(token: str) -> requests.Session:
    """Create a reusable requests session with GitHub headers and retries."""
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "CodeAlchemy-Labs-README-Updater",
        }
    )
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def rate_limit_reset(response: requests.Response) -> str:
    """Format the rate-limit reset timestamp when GitHub provides one."""
    epoch = response.headers.get("X-RateLimit-Reset")
    if not epoch:
        return "unknown"
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return epoch


def handle_api_response(response: requests.Response, label: str) -> Any:
    """Validate an API response and return its JSON payload."""
    if response.status_code in (401, 403):
        payload = {}
        if "json" in (response.headers.get("Content-Type", "") or "").lower():
            try:
                payload = response.json()
            except ValueError:
                payload = {}
        message = str(payload.get("message", "")).lower()
        if "rate limit" in message or response.headers.get("X-RateLimit-Remaining") == "0":
            raise GitHubApiError(
                f"GitHub API rate limit reached while calling {label}. "
                f"Reset time: {rate_limit_reset(response)}."
            )
        raise GitHubApiError(
            "GitHub rejected the token. Verify that GH_TOKEN is valid, not expired, "
            "and has the required read permissions for CodeAlchemy-Labs."
        )
    if response.status_code == 429 or response.headers.get("X-RateLimit-Remaining") == "0":
        raise GitHubApiError(
            f"GitHub API rate limit reached while calling {label}. "
            f"Reset time: {rate_limit_reset(response)}."
        )
    if response.status_code >= 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        message = payload.get("message", f"HTTP {response.status_code}")
        raise GitHubApiError(f"GitHub API error for {label}: {message}")
    remaining = response.headers.get("X-RateLimit-Remaining")
    if remaining is not None:
        try:
            if int(remaining) < 50:
                logger.warning("GitHub API rate limit is low for %s; waiting briefly.", label)
                time.sleep(2)
        except ValueError:
            pass
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubApiError(f"GitHub returned invalid JSON for {label}.") from exc


def validate_token(session: requests.Session) -> str:
    """Validate the configured token against the authenticated-user endpoint."""
    response = session.get(f"{API_BASE}/user", timeout=30)
    payload = handle_api_response(response, "GET /user")
    login = (payload or {}).get("login")
    if not login:
        raise GitHubApiError("GitHub did not return a login for the configured token.")
    logger.info("Authenticated as %s", login)
    return str(login)


def fetch_org_repos(session: requests.Session, org: str) -> list[dict[str, Any]]:
    """Fetch public, non-fork organization repositories, excluding configured names."""
    repositories: list[dict[str, Any]] = []
    page = 1
    while True:
        response = session.get(
            f"{API_BASE}/orgs/{org}/repos",
            params={"type": "public", "per_page": 100, "page": page},
            timeout=30,
        )
        payload = handle_api_response(response, f"GET /orgs/{org}/repos?page={page}")
        if not isinstance(payload, list):
            raise GitHubApiError(f"GitHub returned an invalid repository list for {org}.")
        if not payload:
            break
        repositories.extend(payload)
        if len(payload) < 100:
            break
        page += 1

    filtered = [
        repo
        for repo in repositories
        if repo.get("name") not in EXCLUDED_REPOS and not repo.get("fork", False)
    ]
    logger.info(
        "Fetched %d repositories from %s; %d retained after filtering.",
        len(repositories),
        org,
        len(filtered),
    )
    return filtered


def normalize_languages(raw: Any) -> dict[str, int]:
    """Normalize language totals, including nested payloads, to dict[str, int]."""
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, int) and not isinstance(value, bool):
            normalized[str(key)] = value
        elif isinstance(value, dict):
            for nested_key in ("bytes", "count", "value"):
                nested_value = value.get(nested_key)
                if isinstance(nested_value, int) and not isinstance(nested_value, bool):
                    normalized[str(key)] = nested_value
                    break
    return normalized


def fetch_repo_languages(
    session: requests.Session, org: str, repo_name: str
) -> dict[str, int]:
    """Fetch and normalize a repository's language totals."""
    response = session.get(f"{API_BASE}/repos/{org}/{repo_name}/languages", timeout=30)
    if response.status_code == 404:
        logger.warning("Languages are unavailable for %s/%s.", org, repo_name)
        return {}
    payload = handle_api_response(response, f"GET /repos/{org}/{repo_name}/languages")
    return normalize_languages(payload)


def parse_iso8601(value: Any) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def clean_text(value: Any) -> str:
    """Remove emoji and normalize arbitrary GitHub-provided text for Markdown."""
    text = EMOJI_PATTERN.sub("", str(value or ""))
    return " ".join(text.split())


def safe_cell(value: Any) -> str:
    """Escape a dynamic value for a Markdown table cell."""
    return clean_text(value).replace("|", "\\|")


def humanize_repo_name(name: str) -> str:
    """Convert a repository slug into a readable title."""
    cleaned = re.sub(r"[-_]+", " ", clean_text(name)).strip()
    return " ".join(part.title() for part in cleaned.split())


def truncate_text(value: Any, max_chars: int = 220) -> str:
    """Trim long descriptions on a word boundary."""
    text = clean_text(value)
    if len(text) <= max_chars:
        return text
    trimmed = text[:max_chars].rsplit(" ", 1)[0] if " " in text[:max_chars] else text[: max_chars - 1]
    return trimmed.rstrip(".,;:") + "..."


def format_date(value: Any) -> str:
    """Format an API timestamp for a repository card."""
    parsed = parse_iso8601(value)
    return parsed.strftime("%Y-%m-%d") if parsed else "Unknown"


def render_repo_stats_line(repo: dict[str, Any]) -> str:
    """Render the three requested repository counters."""
    stars = int(repo.get("stargazers_count") or 0)
    forks = int(repo.get("forks_count") or 0)
    issues = int(repo.get("open_issues_count") or 0)
    return f"Stars: {stars} · Forks: {forks} · Issues: {issues}"


def primary_language(repo: dict[str, Any]) -> str | None:
    """Return the language with the greatest detected byte count."""
    languages = normalize_languages(repo.get("languages"))
    if not languages:
        return None
    return clean_text(max(languages.items(), key=lambda item: item[1])[0])


def build_repo_card(repo: dict[str, Any]) -> str:
    """Render one repository card without emoji."""
    repo_name = clean_text(repo.get("name"))
    pretty_name = humanize_repo_name(repo_name)
    html_url = html.escape(
        EMOJI_PATTERN.sub("", str(repo.get("html_url") or "")), quote=True
    )
    lines = [f"#### [{pretty_name}]({html_url})"]

    description = truncate_text(repo.get("description"))
    if description:
        lines.extend(["", description])

    raw_languages = normalize_languages(repo.get("languages"))
    language_names = sorted(
        raw_languages,
        key=lambda language: (-raw_languages[language], language.lower()),
    )[:4]
    if language_names:
        stack = " ".join(f"`{safe_cell(language)}`" for language in language_names)
        lines.extend(["", f"**Stack:** {stack}"])

    topics = sorted(
        {
            clean_text(topic).lower()
            for topic in (repo.get("topics") or [])
            if clean_text(topic)
        }
    )
    if topics:
        lines.extend(["", "**Topics:** " + ", ".join(f"`{safe_cell(topic)}`" for topic in topics[:10])])

    homepage = html.escape(
        EMOJI_PATTERN.sub("", str(repo.get("homepage") or "")), quote=True
    )
    links = [f"[Repository]({html_url})"] if html_url else []
    if homepage and homepage != html_url:
        links.append(f"[Homepage]({homepage})")
    license_info = repo.get("license")
    license_url = license_info.get("url") if isinstance(license_info, dict) else None
    if license_url:
        clean_license_url = EMOJI_PATTERN.sub("", str(license_url))
        links.append(f"[License]({html.escape(clean_license_url, quote=True)})")
    if links:
        lines.extend(["", " · ".join(links)])

    lines.extend(["", render_repo_stats_line(repo), f"Updated: {format_date(repo.get('pushed_at'))}"])
    return "<td width=\"50%\">\n\n" + "\n\n".join(lines) + "\n\n</td>"


def project_grouping(repos: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group repositories by primary language."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for repo in repos:
        groups[primary_language(repo) or "Uncategorized"].append(repo)
    return dict(groups)


def build_projects_section(repos: list[dict[str, Any]]) -> str:
    """Render project cards grouped by primary language."""
    groups = project_grouping(repos)
    language_order = sorted(
        (name for name in groups if name != "Uncategorized"),
        key=lambda name: (
            -sum(normalize_languages(repo.get("languages")).get(name, 0) for repo in groups[name]),
            name.lower(),
        ),
    )
    if "Uncategorized" in groups:
        language_order.append("Uncategorized")

    sections: list[str] = []
    for language in language_order:
        repositories = sorted(
            groups[language],
            key=lambda repo: (
                -int(repo.get("stargazers_count") or 0),
                -(parse_iso8601(repo.get("updated_at")).timestamp()
                  if parse_iso8601(repo.get("updated_at")) else 0),
                clean_text(repo.get("name")).lower(),
            ),
        )
        heading = (
            "### Uncategorized Projects"
            if language == "Uncategorized"
            else f"### {clean_text(language)} Projects"
        )
        rows: list[str] = []
        for index in range(0, len(repositories), 2):
            cards = [build_repo_card(repo) for repo in repositories[index : index + 2]]
            if len(cards) == 1:
                cards.append('<td width="50%"></td>')
            rows.append("<tr>\n" + "\n".join(cards) + "\n</tr>")
        sections.append(heading + "\n\n<table>\n" + "\n".join(rows) + "\n</table>")
    return "\n\n".join(sections) if sections else "_No public repositories available._"


def aggregate_language_totals(repos: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate language bytes across repositories."""
    totals: dict[str, int] = defaultdict(int)
    for repo in repos:
        for language, amount in normalize_languages(repo.get("languages")).items():
            totals[clean_text(language)] += amount
    return dict(totals)


def build_top_languages_table(repos: list[dict[str, Any]]) -> str:
    """Build the language share and repository count table."""
    totals = aggregate_language_totals(repos)
    total_bytes = sum(totals.values())
    if not total_bytes:
        return "| Language | Share | Repos |\n|----------|-------|-------|\n| N/A | 0% | 0 |"
    rows = []
    for language, amount in sorted(totals.items(), key=lambda item: (-item[1], item[0].lower()))[:10]:
        share = amount / total_bytes * 100
        repo_count = sum(
            1 for repo in repos if language in normalize_languages(repo.get("languages"))
        )
        rows.append(f"| {safe_cell(language)} | {share:.0f}% | {repo_count} |")
    return "| Language | Share | Repos |\n|----------|-------|-------|\n" + "\n".join(rows)


def build_skills_table(repos: list[dict[str, Any]]) -> str:
    """Build a compact detected-language and topic summary."""
    totals = aggregate_language_totals(repos)
    languages = sorted(totals, key=lambda language: (-totals[language], language.lower()))
    topic_counts: Counter[str] = Counter()
    for repo in repos:
        topic_counts.update(
            clean_text(topic).lower()
            for topic in (repo.get("topics") or [])
            if clean_text(topic)
        )
    rows = [
        "| Category | Technologies (from detected data) |",
        "|----------|-----------------------------------|",
        "| Languages | "
        + (", ".join(f"`{safe_cell(language)}`" for language in languages[:20]) or "N/A")
        + " |",
        "| Popular topics | "
        + (
            ", ".join(f"`{safe_cell(topic)}`" for topic, _ in topic_counts.most_common(15))
            or "N/A"
        )
        + " |",
    ]
    return "\n".join(rows)


def freshness_counts(repos: list[dict[str, Any]]) -> dict[str, int]:
    """Count repositories by recency, keeping archived repositories distinct."""
    now = datetime.now(timezone.utc)
    counts = {"Active": 0, "Recent": 0, "Stale": 0, "Archived": 0}
    for repo in repos:
        if repo.get("archived"):
            counts["Archived"] += 1
            continue
        pushed_at = parse_iso8601(repo.get("pushed_at") or repo.get("updated_at"))
        if pushed_at is None:
            counts["Stale"] += 1
            continue
        age = now - pushed_at
        if age <= timedelta(days=30):
            counts["Active"] += 1
        elif age <= timedelta(days=90):
            counts["Recent"] += 1
        else:
            counts["Stale"] += 1
    return counts


def build_freshness_table(repos: list[dict[str, Any]]) -> str:
    """Build the repository freshness table."""
    counts = freshness_counts(repos)
    rows = ["| Bucket | Count |", "|--------|-------|"]
    rows.extend(f"| {label} | {counts[label]} |" for label in counts)
    return "\n".join(rows)


def aggregate_commits_last_year(repo_stats: list[dict[str, Any]]) -> int:
    """Sum yearly commits from participation or weekly activity responses."""
    total = 0
    for stats in repo_stats:
        participation = stats.get("participation") or {}
        values = participation.get("all") if isinstance(participation, dict) else None
        if isinstance(values, list):
            total += sum(int(value or 0) for value in values)
            continue
        activity = stats.get("commit_activity") or []
        if isinstance(activity, list):
            total += sum(
                int(item.get("total") or 0)
                for item in activity
                if isinstance(item, dict)
            )
    return total


def aggregate_commit_activity(repo_stats: list[dict[str, Any]]) -> dict[str, list[Any]]:
    """Aggregate weekly commits across repositories."""
    totals: dict[int, int] = defaultdict(int)
    for stats in repo_stats:
        for item in stats.get("commit_activity") or []:
            if isinstance(item, dict) and item.get("week") is not None:
                totals[int(item["week"])] += int(item.get("total") or 0)
    weeks = sorted(totals)
    return {
        "labels": [datetime.fromtimestamp(week, timezone.utc).strftime("%Y-%m-%d") for week in weeks],
        "values": [totals[week] for week in weeks],
    }


def aggregate_code_frequency(repo_stats: list[dict[str, Any]]) -> dict[str, list[Any]]:
    """Aggregate weekly additions and deletions."""
    totals: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for stats in repo_stats:
        for item in stats.get("code_frequency") or []:
            if isinstance(item, list) and len(item) >= 3:
                week = int(item[0])
                totals[week][0] += int(item[1] or 0)
                totals[week][1] += int(item[2] or 0)
    weeks = sorted(totals)
    return {
        "labels": [datetime.fromtimestamp(week, timezone.utc).strftime("%Y-%m-%d") for week in weeks],
        "additions": [totals[week][0] for week in weeks],
        "deletions": [totals[week][1] for week in weeks],
    }


def aggregate_contributors(
    repo_stats: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, set[str]]]:
    """Aggregate contributor totals and the repositories they contributed to."""
    totals: dict[str, int] = defaultdict(int)
    repos_by_contributor: dict[str, set[str]] = defaultdict(set)
    for stats in repo_stats:
        repo_name = clean_text(stats.get("repo_name") or "unknown")
        for contributor in stats.get("contributors") or []:
            if not isinstance(contributor, dict):
                continue
            login = clean_text(contributor.get("login") or contributor.get("name") or "unknown")
            totals[login] += int(contributor.get("contributions") or contributor.get("total") or 0)
            repos_by_contributor[login].add(repo_name)
    return dict(totals), dict(repos_by_contributor)


def get_repo_stats(
    session: requests.Session, org: str, repo: dict[str, Any]
) -> dict[str, Any] | None:
    """Fetch available statistics, recording a status for the final summary."""
    repo_name = str(repo.get("name") or "")
    if repo.get("archived") or int(repo.get("size") or 0) == 0:
        repo["_stats_status"] = "skipped"
        logger.info("Skipping stats for %s because it is archived or empty.", repo_name)
        return None

    stats: dict[str, Any] = {"repo_name": repo_name}
    failed = False
    for kind in ("participation", "commit_activity", "code_frequency", "contributors"):
        url = f"{API_BASE}/repos/{org}/{repo_name}/stats/{kind}"
        try:
            response = session.get(url, timeout=30)
        except requests.RequestException as exc:
            logger.warning("Request failed for %s stats/%s: %s", repo_name, kind, exc)
            failed = True
            continue
        if response.status_code == 202:
            logger.warning("Stats for %s are being computed, skipping this run.", repo_name)
            repo["_stats_status"] = "pending"
            return None
        if response.status_code in (401, 403):
            handle_api_response(response, f"GET /repos/{org}/{repo_name}/stats/{kind}")
        if response.status_code == 404:
            continue
        try:
            payload = handle_api_response(
                response, f"GET /repos/{org}/{repo_name}/stats/{kind}"
            )
        except GitHubApiError as exc:
            logger.warning("Stats request failed for %s stats/%s: %s", repo_name, kind, exc)
            failed = True
            continue
        if payload:
            stats[kind] = payload

    if len(stats) > 1:
        repo["_stats_status"] = "ok"
        return stats
    repo["_stats_status"] = "failed" if failed else "skipped"
    return None


def build_quickchart_url(config: dict[str, Any]) -> str:
    """Encode a Chart.js configuration as a QuickChart SVG URL."""
    encoded = quote(json.dumps(config, separators=(",", ":")), safe="")
    return f"https://quickchart.io/chart?c={encoded}&w=800&h=300&backgroundColor=white&format=svg"


def chart_config(chart_type: str, labels: list[str], datasets: list[dict[str, Any]], options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a Chart.js config with consistent defaults."""
    config: dict[str, Any] = {
        "type": chart_type,
        "data": {"labels": labels, "datasets": datasets},
    }
    config["options"] = options or {}
    return config


def build_charts(
    repos: list[dict[str, Any]],
    repo_stats: list[dict[str, Any]],
) -> list[str]:
    """Render the five requested charts when at least one repository has real stats."""
    if not repo_stats:
        return []

    activity = aggregate_commit_activity(repo_stats)
    activity_labels = activity["labels"][-52:] or ["No data"]
    activity_values = activity["values"][-52:] or [0]
    code = aggregate_code_frequency(repo_stats)
    code_labels = code["labels"][-52:] or ["No data"]
    additions = code["additions"][-52:] or [0]
    deletions = code["deletions"][-52:] or [0]

    contributor_totals, _ = aggregate_contributors(repo_stats)
    top_contributors = sorted(
        contributor_totals.items(), key=lambda item: (-item[1], item[0].lower())
    )[:10]
    contributor_labels = [name for name, _ in top_contributors] or ["No contributor data"]
    contributor_values = [value for _, value in top_contributors] or [0]

    language_totals = aggregate_language_totals(repos)
    language_labels = sorted(
        language_totals, key=lambda name: (-language_totals[name], name.lower())
    ) or ["N/A"]
    language_values = [language_totals[name] for name in language_labels] if language_totals else [0]

    freshness = freshness_counts(repos)
    freshness_labels = list(freshness)
    freshness_values = [freshness[name] for name in freshness_labels]

    charts = [
        (
            "### Weekly commit activity",
            "Weekly commit activity",
            chart_config(
                "line",
                activity_labels,
                [{
                    "label": "Weekly commits",
                    "data": activity_values,
                    "borderColor": "#2f81f7",
                    "backgroundColor": "rgba(47,129,247,0.2)",
                    "fill": False,
                    "tension": 0.2,
                }],
                {"plugins": {"legend": {"display": False}}, "scales": {"y": {"beginAtZero": True}}},
            ),
        ),
        (
            "### Code frequency",
            "Code frequency",
            chart_config(
                "bar",
                code_labels,
                [
                    {"label": "Additions", "data": additions, "backgroundColor": "#2f81f7"},
                    {"label": "Deletions", "data": deletions, "backgroundColor": "#d9485f"},
                ],
                {"plugins": {"legend": {"display": True}}, "scales": {"y": {"beginAtZero": True}}},
            ),
        ),
        (
            "### Top contributors chart",
            "Top contributors",
            chart_config(
                "bar",
                contributor_labels,
                [{"label": "Contributions", "data": contributor_values, "backgroundColor": "#3fb950"}],
                {"indexAxis": "y", "plugins": {"legend": {"display": False}}, "scales": {"x": {"beginAtZero": True}}},
            ),
        ),
        (
            "### Language distribution",
            "Language distribution",
            chart_config(
                "doughnut",
                language_labels,
                [{
                    "data": language_values,
                    "backgroundColor": [
                        "#2f81f7", "#3fb950", "#d9485f", "#f59e0b", "#8b5cf6",
                        "#0ea5e9", "#f97316", "#10b981", "#eab308", "#64748b",
                    ],
                }],
                {"plugins": {"legend": {"position": "bottom"}}},
            ),
        ),
        (
            "### Repository freshness chart",
            "Repository freshness",
            chart_config(
                "bar",
                freshness_labels,
                [{
                    "label": "Repositories",
                    "data": freshness_values,
                    "backgroundColor": ["#2f81f7", "#3fb950", "#f59e0b", "#d9485f"],
                }],
                {"indexAxis": "y", "plugins": {"legend": {"display": False}}, "scales": {"x": {"beginAtZero": True}}},
            ),
        ),
    ]
    lines: list[str] = []
    for heading, label, config in charts:
        lines.extend([heading, f"![{label}]({build_quickchart_url(config)})", ""])
    return lines


def build_stats_section(
    repos: list[dict[str, Any]],
    repo_stats: list[dict[str, Any]],
) -> str:
    """Build the organization metrics, tables, and charts."""
    languages = aggregate_language_totals(repos)
    contributor_totals, contributor_repos = aggregate_contributors(repo_stats)
    updated_dates = [
        parsed
        for repo in repos
        if (parsed := parse_iso8601(repo.get("pushed_at") or repo.get("updated_at"))) is not None
    ]
    last_updated = max(updated_dates).strftime("%Y-%m-%d %H:%M UTC") if updated_dates else "N/A"
    lines = [
        "### Global stats",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total repositories | {len(repos)} |",
        f"| Total stars | {sum(int(repo.get('stargazers_count') or 0) for repo in repos)} |",
        f"| Total forks | {sum(int(repo.get('forks_count') or 0) for repo in repos)} |",
        f"| Open issues | {sum(int(repo.get('open_issues_count') or 0) for repo in repos)} |",
        f"| Total commits | {aggregate_commits_last_year(repo_stats)} |",
        f"| Total contributors | {len(contributor_totals)} |",
        f"| Total languages | {len(languages)} |",
        f"| Last updated | {last_updated} |",
        "",
        "### Top languages",
        build_top_languages_table(repos),
        "",
        "### Skills / tech stack",
        build_skills_table(repos),
        "",
        "### Repository freshness",
        build_freshness_table(repos),
        "",
        "### Top contributors",
        "| Contributor | Contributions | Repositories |",
        "|-------------|---------------|--------------|",
    ]
    top_contributors = sorted(
        contributor_totals.items(), key=lambda item: (-item[1], item[0].lower())
    )[:10]
    if top_contributors:
        lines.extend(
            f"| {safe_cell(name)} | {count} | {len(contributor_repos.get(name, set()))} |"
            for name, count in top_contributors
        )
    else:
        lines.append("| N/A | 0 | 0 |")
    lines.append("")

    charts = build_charts(repos, repo_stats)
    if charts:
        lines.extend(charts)
    else:
        lines.append(
            "_Commit activity charts will appear once GitHub finishes computing stats for this organization. "
            "The next scheduled run will include them._"
        )
    return "\n".join(lines).strip()


def validate_markers(readme: str) -> None:
    """Ensure each marker pair exists exactly once and is in the correct order."""
    for start_marker, end_marker in (
        (MARKER_PROJECTS_START, MARKER_PROJECTS_END),
        (MARKER_STATS_START, MARKER_STATS_END),
    ):
        if readme.count(start_marker) != 1 or readme.count(end_marker) != 1:
            raise RuntimeError(f"README marker pair missing or duplicated: {start_marker} / {end_marker}")
        if readme.index(start_marker) >= readme.index(end_marker):
            raise RuntimeError(f"README marker pair is out of order: {start_marker} / {end_marker}")


def replace_between_markers(
    readme: str, start_marker: str, end_marker: str, new_block: str
) -> str:
    """Replace content between markers without changing surrounding editorial text."""
    start_index = readme.index(start_marker) + len(start_marker)
    end_index = readme.index(end_marker, start_index)
    return readme[:start_index] + "\n" + new_block.strip() + "\n" + readme[end_index:]


def update_readme_file(readme_path: str, projects_md: str, stats_md: str) -> bool:
    """Validate generated blocks and update the README only when needed."""
    if not projects_md.strip():
        raise RuntimeError("Generated projects block is empty.")
    if not stats_md.strip():
        raise RuntimeError("Generated stats block is empty.")
    readme_file = Path(readme_path)
    if not readme_file.is_file():
        raise RuntimeError(f"README file not found: {readme_file}")
    original = readme_file.read_text(encoding="utf-8")
    validate_markers(original)
    updated = replace_between_markers(
        original, MARKER_PROJECTS_START, MARKER_PROJECTS_END, projects_md
    )
    updated = replace_between_markers(updated, MARKER_STATS_START, MARKER_STATS_END, stats_md)
    if updated == original:
        logger.info("README content already matches; no rewrite required.")
        return False
    readme_file.write_text(updated, encoding="utf-8")
    logger.info("Updated %s", readme_path)
    return True


def _self_test() -> int:
    """Assert the language normalizer always returns the required mapping shape."""
    sample = {
        "Python": {"bytes": 100, "count": 2},
        "Java": 50,
        "Rust": {"value": 9},
        "Bad": "unexpected",
        "Boolean": True,
        "NestedInvalid": {"bytes": "not-an-integer"},
    }
    expected = {"Python": 100, "Java": 50, "Rust": 9}
    try:
        actual = normalize_languages(sample)
        assert actual == expected
        assert isinstance(actual, dict)
        assert all(isinstance(key, str) for key in actual)
        assert all(isinstance(value, int) and not isinstance(value, bool) for value in actual.values())
    except AssertionError:
        logger.exception("Self-test failed: normalize_languages returned an unexpected shape.")
        return 1
    logger.info("Self-test passed: normalize_languages returns dict[str, int].")
    return 0


def main() -> int:
    """Run the README generation workflow."""
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        return _self_test()

    token = os.getenv("GH_TOKEN") or os.getenv("ORG_READ_TOKEN")
    if not token:
        logger.error("Missing GH_TOKEN. Set the organization read token before running the updater.")
        return 1

    org = os.getenv("ORG", DEFAULT_ORG)
    readme_path = os.getenv("README_PATH", DEFAULT_README_PATH)
    try:
        readme_file = Path(readme_path)
        if not readme_file.is_file():
            raise RuntimeError(f"README file not found: {readme_file}")
        validate_markers(readme_file.read_text(encoding="utf-8"))

        with create_session(token) as session:
            validate_token(session)
            repos = fetch_org_repos(session, org)
            for repo in repos:
                repo_name = str(repo.get("name") or "")
                repo["languages"] = fetch_repo_languages(session, org, repo_name)

            repo_stats: list[dict[str, Any]] = []
            pending = 0
            failed = 0
            skipped = 0
            for repo in repos:
                repo["_stats_status"] = "skipped"
                stats = get_repo_stats(session, org, repo)
                if stats is not None:
                    repo_stats.append(stats)
                    continue
                status = repo.get("_stats_status", "skipped")
                if status == "pending":
                    pending += 1
                elif status == "failed":
                    failed += 1
                else:
                    skipped += 1

        projects_md = build_projects_section(repos)
        stats_md = build_stats_section(repos, repo_stats)
        update_readme_file(readme_path, projects_md, stats_md)
        logger.info(
            "Stats summary: %s ok, %s pending (202), %s failed, %s skipped.",
            len(repo_stats),
            pending,
            failed,
            skipped,
        )
        return 0
    except (GitHubApiError, requests.RequestException, OSError, RuntimeError) as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
