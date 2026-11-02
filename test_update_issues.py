import unittest
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from unittest.mock import patch

from update_issues import (
    UpdaterError,
    collect_issue_activity,
    get_app_report,
    latest_checkpoint,
    load_config,
    main,
    render_report,
)


class ConfigTests(unittest.TestCase):
    def test_bundled_and_workspace_configs_have_expected_mapping_counts(self):
        expected = {"gaming_platform": 17, "insurance": 17, "social_media": 15, "multi_tenant": 19}
        config_paths = [
            Path(__file__).with_name("config.yaml"),
            Path(__file__).parent.parent / "config.yaml",
        ]
        for config_path in config_paths:
            with self.subTest(config=config_path):
                config = load_config(config_path)
                self.assertEqual(
                    {name: len(entry["pull_from"][0]["ids"]) for name, entry in config.items()},
                    expected,
                )


class CheckpointTests(unittest.TestCase):
    def test_human_comment_does_not_change_checkpoint(self):
        comments = [
            {"body": "Regular update", "created_at": "2026-10-20T08:00:00Z"},
            {"body": "<!-- app-updater:v1 app=gaming_platform through=2026-10-21T09:00:00Z -->"},
        ]
        self.assertEqual(
            latest_checkpoint(comments, "gaming_platform"),
            datetime(2026, 10, 21, 9, tzinfo=timezone.utc),
        )

    def test_checkpoint_is_independent_per_app(self):
        comments = [{"body": "<!-- app-updater:v1 app=insurance through=2026-10-21T09:00:00Z -->"}]
        self.assertIsNone(latest_checkpoint(comments, "gaming_platform"))


class CollectionTests(unittest.TestCase):
    @patch("update_issues.gh_json")
    def test_collection_filters_noise_and_marks_latest_comment_versions(self, mock_gh_json):
        since = datetime(2026, 10, 20, tzinfo=timezone.utc)
        cutoff = datetime(2026, 10, 21, tzinfo=timezone.utc)
        issue = {
            "number": 135,
            "created_at": "2026-10-19T10:00:00Z",
            "updated_at": "2026-10-20T13:00:00Z",
            "state": "open",
        }
        timeline = [
            {"event": "cross-referenced", "created_at": "2026-10-20T10:00:00Z"},
            {"event": "parent_issue_added", "created_at": "2026-10-20T10:01:00Z"},
            {"event": "assigned", "created_at": "2026-10-20T10:02:00Z"},
        ]
        comments = [
            {
                "created_at": "2026-10-19T10:00:00Z",
                "updated_at": "2026-10-20T11:00:00Z",
                "body": "latest edited body",
            },
            {
                "created_at": "2026-10-20T12:00:00Z",
                "updated_at": "2026-10-20T13:00:00Z",
                "body": "new comment, latest body",
            },
        ]
        mock_gh_json.side_effect = [issue, timeline, comments]

        activity = collect_issue_activity("example/source", 135, since, cutoff)

        self.assertEqual([event["event"] for event in activity["events"]], ["assigned"])
        self.assertEqual([comment["activity_kind"] for comment in activity["comments"]], [
            "edited (latest version)",
            "new",
        ])


class AppReportTests(unittest.TestCase):
    @patch("update_issues.collect_issue_activity")
    @patch("update_issues.gh_json", return_value=[])
    def test_comment_activity_is_not_inferred_from_markdown_heading(self, mock_gh_json, mock_collect):
        mock_collect.return_value = {
            "issue": {
                "number": 93,
                "title": "Multi-Tenant Solution",
                "html_url": "https://github.com/example/source/issues/93",
                "state": "open",
                "assignees": [],
                "labels": [],
                "milestone": None,
            },
            "events": [],
            "comments": [{
                "activity_kind": "new",
                "created_at": "2026-10-28T08:45:25Z",
                "updated_at": "2026-10-28T08:45:25Z",
                "user": {"login": "developer"},
                "body": "umbrella comment",
                "html_url": "https://github.com/example/source/issues/93#issuecomment-1",
            }],
        }
        config = {
            "id": 709,
            "repo": "example/destination",
            "pull_from": [{"repo": "example/source", "ids": [93]}],
        }
        args = Namespace(since=None, template=Path(__file__).with_name("report_template.md"))

        destination_repo, body, issue_number, has_activity = get_app_report(
            "multi_tenant", config, args, datetime(2026, 10, 28, 9, tzinfo=timezone.utc)
        )

        self.assertEqual((destination_repo, issue_number), ("example/destination", 709))
        self.assertTrue(has_activity)
        self.assertIn("umbrella comment", body)

    @patch("update_issues.gh_post_comment")
    @patch("update_issues.get_app_report")
    @patch("update_issues.load_config")
    def test_apply_without_app_posts_all_configured_reports(
        self,
        mock_load_config,
        mock_get_app_report,
        mock_post_comment,
    ):
        mock_load_config.return_value = {"one": {}, "two": {}}
        mock_get_app_report.side_effect = [
            ("example/destination", "report one", 701, True),
            ("example/destination", "report two", 702, True),
        ]

        result = main(["--apply"])

        self.assertEqual(result, 0)
        self.assertEqual(mock_get_app_report.call_count, 2)
        self.assertEqual(mock_post_comment.call_count, 2)


class RenderTests(unittest.TestCase):
    def test_custom_template_controls_layout_and_keeps_checkpoint_marker(self):
        cutoff = datetime(2026, 10, 21, 9, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            template_path = Path(directory) / "custom.md"
            template_path.write_text(
                "# {{ app_title }}\n"
                "{% for issue in issues %}#{{ issue.number }}"
                "{% for event in issue.events %}:{{ event.summary }}{% endfor %}"
                "{% for comment in issue.comments %}:{{ comment.body | code_block }}{% endfor %}"
                "{% endfor %}\n{{ marker }}",
                encoding="utf-8",
            )
            activity = [{
                "issue": {
                    "number": 135,
                    "title": "Sample issue",
                    "html_url": "https://github.com/example/repo/issues/135",
                    "state": "open",
                    "assignees": [],
                    "labels": [],
                    "milestone": None,
                },
                "events": [{"event": "assigned", "created_at": "2026-10-20T10:00:00Z"}],
                "comments": [{
                    "activity_kind": "new",
                    "created_at": "2026-10-20T11:00:00Z",
                    "updated_at": "2026-10-20T11:00:00Z",
                    "user": {"login": "dev"},
                    "body": "latest body",
                }],
            }]
            report = render_report("gaming_platform", "example/repo", None, cutoff, activity, template_path)

        self.assertIn("# Gaming Platform", report)
        self.assertIn("#135", report)
        self.assertIn("assigned", report)
        self.assertIn("latest body", report)
        self.assertTrue(report.endswith("<!-- app-updater:v1 app=gaming_platform through=2026-10-21T09:00:00Z -->"))

    def test_custom_template_must_include_checkpoint_marker(self):
        cutoff = datetime(2026, 10, 21, 9, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            template_path = Path(directory) / "custom.md"
            template_path.write_text("{{ issues | length }}", encoding="utf-8")
            with self.assertRaises(UpdaterError):
                render_report("gaming_platform", "example/repo", None, cutoff, [], template_path)

    def test_report_contains_latest_comment_and_marker(self):
        start = datetime(2026, 10, 20, tzinfo=timezone.utc)
        cutoff = datetime(2026, 10, 21, 9, tzinfo=timezone.utc)
        activity = {
            "issue": {
                "number": 135,
                "title": "Sample issue",
                "html_url": "https://github.com/example/repo/issues/135",
                "state": "open",
                "assignees": [{"login": "dev"}],
                "labels": [{"name": "needs review"}],
                "milestone": {"title": "Sprint 1", "html_url": "https://github.com/example/repo/milestone/1"},
            },
            "events": [
                {"event": "assigned", "created_at": "2026-10-20T10:00:00Z", "assignee": {"login": "dev"}},
                {
                    "event": "sub_issue_added",
                    "created_at": "2026-10-20T10:30:00Z",
                    "actor": {"login": "dev"},
                    "sub_issue": {
                        "number": 136,
                        "title": "Child work item",
                        "repository": {"full_name": "example/repo"},
                    },
                },
            ],
            "comments": [{
                "activity_kind": "edited (latest version)",
                "created_at": "2026-10-19T10:00:00Z",
                "updated_at": "2026-10-20T11:00:00Z",
                "user": {"login": "dev"},
                "body": "latest body",
                "html_url": "https://github.com/example/repo/issues/135#issuecomment-1",
            }],
        }
        report = render_report("gaming_platform", "example/repo", start, cutoff, [activity])
        self.assertIn("assigned", report)
        self.assertIn("@dev", report)
        self.assertIn("[Sprint 1](https://github.com/example/repo/milestone/1)", report)
        self.assertIn("[needs review](https://github.com/example/repo/labels/needs%20review)", report)
        self.assertIn("[#136 Child work item](https://github.com/example/repo/issues/136)", report)
        self.assertIn("2026-10-20T00:00:00Z - 2026-10-21T09:00:00Z", report)
        self.assertIn("created 2026-10-19T10:00:00Z; updated 2026-10-20T11:00:00Z", report)
        self.assertIn("latest body", report)
        self.assertIn("app=gaming_platform through=2026-10-21T09:00:00Z", report)

    def test_initial_report_names_history_start_without_epoch_timestamp(self):
        cutoff = datetime(2026, 10, 21, 9, tzinfo=timezone.utc)
        report = render_report("gaming_platform", "example/repo", None, cutoff, [])
        self.assertIn("beginning of available source history", report)
        self.assertIn(" - 2026-10-21T09:00:00Z", report)
        self.assertNotIn("1970", report)


if __name__ == "__main__":
    unittest.main()