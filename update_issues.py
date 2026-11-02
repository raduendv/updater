#!/usr/bin/env python3
"""Summarize source issue activity on configured destination issues."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jinja2 import Environment, StrictUndefined, TemplateError, meta
import yaml

MARKER_RE = re.compile(
    r"<!-- app-updater:v1 app=([a-z0-9_]+) through=([0-9T:.-]+Z) -->"
)
MAX_COMMENT_LENGTH = 60_000
IGNORED_TIMELINE_EVENTS = {"cross-referenced", "parent_issue_added", "parent_issue_removed"}
REPORT_TEMPLATE = Path(__file__).with_name("report_template.md")


class UpdaterError(Exception):
    """An expected configuration, GitHub CLI, or API error."""


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp must include a timezone: {value}")
    return parsed.astimezone(timezone.utc)


def iso_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_config(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise UpdaterError(f"Cannot load config {path}: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("issues"), dict) or not data["issues"]:
        raise UpdaterError("Config must contain a non-empty 'issues' mapping")

    seen_sources: set[tuple[str, int]] = set()
    for app, config in data["issues"].items():
        if not isinstance(config, dict):
            raise UpdaterError(f"Config for {app} must be a mapping")
        if not isinstance(config.get("id"), int) or config["id"] <= 0:
            raise UpdaterError(f"Config for {app} needs a positive destination issue 'id'")
        if not isinstance(config.get("repo"), str) or "/" not in config["repo"]:
            raise UpdaterError(f"Config for {app} needs destination 'repo' in owner/name form")
        pull_from = config.get("pull_from")
        if not isinstance(pull_from, list) or not pull_from:
            raise UpdaterError(f"Config for {app} needs at least one 'pull_from' entry")
        for source in pull_from:
            if not isinstance(source, dict) or not isinstance(source.get("repo"), str):
                raise UpdaterError(f"Each pull_from entry for {app} needs a source 'repo'")
            ids = source.get("ids")
            if not isinstance(ids, list) or not ids or any(not isinstance(issue_id, int) or issue_id <= 0 for issue_id in ids):
                raise UpdaterError(f"Each pull_from entry for {app} needs positive integer issue IDs")
            for issue_id in ids:
                key = (source["repo"], issue_id)
                if key in seen_sources:
                    raise UpdaterError(f"Source issue {source['repo']}#{issue_id} is mapped more than once")
                seen_sources.add(key)
    return data["issues"]


def gh_json(endpoint: str, *, paginate: bool = False) -> Any:
    command = ["gh", "api"]
    if paginate:
        command.extend(["--paginate", "--slurp"])
    command.extend(["-H", "Accept: application/vnd.github+json", endpoint])
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise UpdaterError("GitHub CLI 'gh' is not installed or is not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise UpdaterError(f"gh api failed for {endpoint}: {detail}") from exc
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise UpdaterError(f"gh api returned invalid JSON for {endpoint}") from exc
    if paginate:
        if not isinstance(data, list) or any(not isinstance(page, list) for page in data):
            raise UpdaterError(f"Unexpected paginated response for {endpoint}")
        return [item for page in data for item in page]
    return data


def gh_post_comment(repo: str, issue_number: int, body: str) -> None:
    try:
        subprocess.run(
            ["gh", "api", "-X", "POST", f"repos/{repo}/issues/{issue_number}/comments", "-f", f"body={body}"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise UpdaterError("GitHub CLI 'gh' is not installed or is not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise UpdaterError(f"Could not post report to {repo}#{issue_number}: {detail}") from exc


def latest_checkpoint(comments: list[dict[str, Any]], app: str) -> datetime | None:
    checkpoints = []
    for comment in comments:
        for match in MARKER_RE.finditer(comment.get("body", "")):
            if match.group(1) == app:
                checkpoints.append(parse_time(match.group(2)))
    return max(checkpoints) if checkpoints else None


def event_summary(event: dict[str, Any]) -> str:
    event_type = event.get("event", "updated").replace("_", " ")
    actor = event.get("actor") or {}
    details = []
    if actor.get("login"):
        details.append(f"by {actor['login']}")
    for key, label in (("assignee", "assignee"), ("milestone", "milestone"), ("label", "label")):
        value = event.get(key)
        if isinstance(value, dict):
            value = value.get("login") or value.get("title") or value.get("name")
        if value:
            details.append(f"{label}: {value}")
    if event_type == "renamed":
        details.extend(value for value in (event.get("rename", {}).get("from"), event.get("rename", {}).get("to")) if value)
    suffix = f" ({'; '.join(details)})" if details else ""
    return f"{event_type}{suffix}"


def collect_issue_activity(
    repo: str,
    issue_number: int,
    since: datetime,
    cutoff: datetime,
) -> dict[str, Any]:
    base = f"repos/{repo}/issues/{issue_number}"
    issue = gh_json(base)
    timeline = gh_json(f"{base}/timeline?per_page=100", paginate=True)
    comments = gh_json(f"{base}/comments?per_page=100", paginate=True)

    events = [
        event for event in timeline
        if event.get("event") not in IGNORED_TIMELINE_EVENTS | {"commented"}
        and event.get("created_at")
        and since < parse_time(event["created_at"]) <= cutoff
    ]
    if (
        issue.get("created_at")
        and since < parse_time(issue["created_at"]) <= cutoff
        and not any(event.get("event") == "created" for event in events)
    ):
        events.append({"event": "created", "created_at": issue["created_at"]})

    changed_comments = []
    for comment in comments:
        created_at = parse_time(comment["created_at"])
        updated_at = parse_time(comment.get("updated_at", comment["created_at"]))
        activity_at = max(created_at, updated_at)
        if since < activity_at <= cutoff:
            comment["activity_kind"] = "new" if created_at > since else "edited (latest version)"
            comment["activity_at"] = iso_time(activity_at)
            changed_comments.append(comment)
    if not events and not changed_comments and issue.get("updated_at"):
        updated_at = parse_time(issue["updated_at"])
        if since < updated_at <= cutoff:
            events.append({"event": "updated (details unavailable via issue events)", "created_at": issue["updated_at"]})

    return {"issue": issue, "events": events, "comments": changed_comments}


def markdown_escape(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]<>()#+.!|])", r"\\\1", value)


def code_block(text: str) -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def render_report(
    app: str,
    source_repo: str,
    since: datetime | None,
    cutoff: datetime,
    activities: list[dict[str, Any]],
    template_path: Path = REPORT_TEMPLATE,
) -> str:
    since_label = iso_time(since) if since else "beginning of available source history"
    report_issues = []
    for item in activities:
        issue = item["issue"]
        if not item["events"] and not item["comments"]:
            continue
        number = issue["number"]
        url = issue.get("html_url", f"https://github.com/{source_repo}/issues/{number}")
        issue_comments = []
        for comment in sorted(
            item["comments"],
            key=lambda value: value.get("activity_at", value.get("updated_at", "")),
        ):
            created_at = comment.get("created_at", "unknown time")
            updated_at = comment.get("updated_at", created_at)
            timestamps = f"created {created_at}"
            if updated_at != created_at:
                timestamps += f"; updated {updated_at}"
            issue_comments.append({
                "kind": comment.get("activity_kind", "new"),
                "author": (comment.get("user") or {}).get("login", "unknown"),
                "created_at": created_at,
                "updated_at": updated_at,
                "timestamps": timestamps,
                "url": comment.get("html_url", url),
                "body": comment.get("body", ""),
            })
        report_issues.append({
            "number": number,
            "title": issue.get("title", f"Issue {number}"),
            "url": url,
            "state": issue.get("state", "unknown"),
            "assignees": [person.get("login", "") for person in issue.get("assignees", []) if person.get("login")],
            "labels": [
                {
                    "name": label.get("name", ""),
                    "url": f"{url.rsplit('/issues/', 1)[0]}/labels/{quote(label.get('name', ''), safe='')}",
                }
                for label in issue.get("labels", [])
            ],
            "milestone": (
                {
                    "title": issue["milestone"].get("title", ""),
                    "url": issue["milestone"].get("html_url", ""),
                }
                if issue.get("milestone")
                else None
            ),
            "events": [],
            "comments": issue_comments,
        })
        for event in sorted(item["events"], key=lambda value: value.get("created_at", "")):
            event_type = event.get("event", "")
            event_data = {
                "summary": event_summary(event),
                "created_at": event.get("created_at", "unknown time"),
                "actor": (event.get("actor") or {}).get("login", "unknown"),
                "sub_issue": None,
            }
            child = event.get("sub_issue")
            if event_type in {"sub_issue_added", "sub_issue_removed"} and isinstance(child, dict):
                child_number = child.get("number")
                child_repo = (child.get("repository") or {}).get("full_name")
                issue_repo_url = url.rsplit("/issues/", 1)[0]
                if child_repo and child_repo not in source_repo:
                    owner, name = child_repo.split("/", 1)
                    issue_repo_url = f"https://github.com/{owner}/{name}"
                if child_number:
                    event_data["sub_issue"] = {
                        "action": "added" if event_type == "sub_issue_added" else "removed",
                        "number": child_number,
                        "title": child.get("title", f"Issue {child_number}"),
                        "url": f"{issue_repo_url}/issues/{child_number}",
                    }
            report_issues[-1]["events"].append(event_data)

    try:
        template_text = template_path.read_text(encoding="utf-8")
        environment = Environment(undefined=StrictUndefined, autoescape=False, trim_blocks=True, lstrip_blocks=True)
        environment.filters["markdown_escape"] = markdown_escape
        environment.filters["code_block"] = code_block
        environment.filters["mention"] = lambda login: f"@{markdown_escape(login)}"
        parsed_template = environment.parse(template_text)
        if "marker" not in meta.find_undeclared_variables(parsed_template):
            raise UpdaterError("Report template must render the {{ marker }} variable")
        rendered = environment.from_string(template_text).render(
            app_title=app.replace("_", " ").title(),
            source_repo=source_repo,
            period_start=since_label,
            period_end=iso_time(cutoff),
            issues=report_issues,
            marker=f"<!-- app-updater:v1 app={app} through={iso_time(cutoff)} -->",
        ).strip()
        return rendered
    except OSError as error:
        raise UpdaterError(f"Cannot read report template {template_path}: {error}") from error
    except TemplateError as error:
        raise UpdaterError(f"Invalid report template {template_path}: {error}") from error


def get_app_report(
    app: str,
    config: dict[str, Any],
    args: argparse.Namespace,
    cutoff: datetime,
) -> tuple[str, str, int, bool]:
    destination_repo = config["repo"]
    destination_comments = gh_json(
        f"repos/{destination_repo}/issues/{config['id']}/comments?per_page=100", paginate=True
    )
    marker = latest_checkpoint(destination_comments, app)
    since = marker or datetime(1970, 1, 1, tzinfo=timezone.utc)
    report_since = since
    if args.since:
        override = parse_time(args.since)
        since = max(since, override)
        report_since = since
    elif marker is None:
        report_since = None
    activities = []
    source_repos = []
    for source in config["pull_from"]:
        source_repos.append(source["repo"])
        for issue_number in source["ids"]:
            activities.append(collect_issue_activity(source["repo"], issue_number, since, cutoff))
    source_label = ", ".join(dict.fromkeys(source_repos))
    body = render_report(app, source_label, report_since, cutoff, activities, args.template)
    has_activity = any(item["events"] or item["comments"] for item in activities)
    return destination_repo, body, config["id"], has_activity


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    workspace_config = Path(__file__).resolve().parent.parent / "config.yaml"
    bundled_config = Path(__file__).with_name("config.yaml")
    default_config = workspace_config if workspace_config.exists() else bundled_config
    parser.add_argument("--config", type=Path, default=default_config)
    parser.add_argument("--template", type=Path, default=REPORT_TEMPLATE, help="Markdown report template")
    parser.add_argument("--app", help="One configured app key (for example: gaming_platform)")
    parser.add_argument("--since", help="Optional UTC timestamp for an initial/shorter catch-up, e.g. 2026-10-01T00:00:00Z")
    parser.add_argument("--apply", action="store_true", help="Post reports for the selected app or all configured apps")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.app and args.app not in config:
            raise UpdaterError(f"Unknown app '{args.app}'. Configured apps: {', '.join(config)}")

        selected = [args.app] if args.app else list(config)
        cutoff = datetime.now(timezone.utc)
        if args.since and parse_time(args.since) > cutoff:
            raise UpdaterError("--since must not be later than the current run time")
        reports = []
        for app in selected:
            destination_repo, body, issue_number, has_activity = get_app_report(app, config[app], args, cutoff)
            if len(body) > MAX_COMMENT_LENGTH:
                raise UpdaterError(
                    f"Report for {app} is {len(body)} characters; GitHub's safe limit is {MAX_COMMENT_LENGTH}. "
                    "Retry with --since to shorten the catch-up period."
                )
            reports.append((app, destination_repo, issue_number, body, has_activity))
            print(f"=== {app}: {destination_repo}#{issue_number} ===")
            if not has_activity:
                print("No source activity in this period; no report comment will be posted.")
            else:
                print(body)

        if not args.apply:
            print("\nDry run only. No GitHub comments were posted.")
            return 0

        to_post = [report for report in reports if report[4]]
        if not to_post:
            print("No activity to publish.")
            return 0
        for app, destination_repo, issue_number, body, _ in to_post:
            gh_post_comment(destination_repo, issue_number, body)
            print(f"Posted {app} report to {destination_repo}#{issue_number}")
        return 0
    except (UpdaterError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())