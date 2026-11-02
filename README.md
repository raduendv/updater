# Issue Activity Updater

A small, standalone Python CLI that summarizes activity from configured GitHub issues and adds a report comment to each corresponding destination issue. It uses the authenticated `gh` CLI; credentials are never stored in this folder.

## Setup

Requirements: Python 3.10+, GitHub CLI (`gh`), and access to read source issues/comments/timelines and comment on destination issues.

```sh
cd updater
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
gh auth status
```

Edit `config.yaml` to map destination issues to source repositories and issue IDs. When run inside the parent project, the CLI uses its root `config.yaml`; otherwise it falls back to the bundled `updater/config.yaml`. Each app mapping should include its umbrella issue as well as detailed work issues. The checked-in mappings were initialized from the proposal JSON files; the updater does not read or depend on those files at runtime.

The posted comment layout lives in `report_template.md`. It is a Jinja2 template with loops over `issues`, each issue's `events`, and its `comments`, plus conditionals for empty sections. Edit it directly or pass `--template path/to/report.md`. Top-level values include `app_title`, `source_repo`, `period_start`, `period_end`, `issues`, and `marker`; keep `{{ marker }}` in the rendered output so the updater can find its checkpoint. `markdown_escape` and `code_block` filters are available for safe Markdown rendering.

## Run

Preview all configured reports (default; read-only):

```sh
python update_issues.py
```

Preview or publish one app:

```sh
python update_issues.py --app gaming_platform
python update_issues.py --app gaming_platform --template report_template.md
python update_issues.py --apply --app gaming_platform
python update_issues.py --apply
```

Publishing is gated only by `--apply`; with `--app` it posts that app's report, and without `--app` it posts reports for all configured apps. It only creates comments on destination issues. No-activity reports are not posted. Use `--since 2026-10-01T00:00:00Z` for an explicitly shorter initial catch-up. Once a generated marker exists, `--since` cannot move the checkpoint backwards.

The first run catches up from the beginning of available issue history unless `--since` is provided. Reports contain issue state, assignees, labels, milestone, available timeline events, and the latest available versions of comments. If a report is too large for one GitHub comment, the command refuses to post it and recommends a narrower `--since` window.

Each destination issue has its own checkpoint. The tool finds its latest matching `app-updater:v1` marker; ordinary comments never move the cursor. A checkpoint advances only when a report comment containing the marker has been successfully created. If one app fails after another succeeds, rerun the command: the successful app will be a no-op while the failed app retries.

GitHub exposes current comment bodies and update timestamps, not a complete edit history. This tool reports the latest body of a comment edited since the previous report, but cannot recover intermediate edits or deleted comments. It reports source issue open/closed state and timeline events; GitHub Projects v2 fields are not currently collected. This is a best-effort activity summary, not an audit log.