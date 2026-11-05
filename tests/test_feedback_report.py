"""Offline feedback fixtures. No fixture represents a real case or is uploaded."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import feedback_report as feedback


def fixture():
    return {"version": 1, "problem_key": "popup-not-ready", "title": "Wait for observed page readiness",
            "stage": "device", "environment": {"host_os": "Windows", "device_kind": "emulator", "device_os": "Android 9", "device_family": "Generic phone", "proxypin_version": "1.3.3"},
            "symptom": "A startup popup blocked the intended scrolling action.",
            "root_cause": "The action preceded visible popup readiness.", "root_cause_status": "confirmed",
            "resolution": ["Wait for the currently observed popup, then close its visible control.", "Verify the unobstructed page before scrolling."],
            "verification": {"kind": "live", "checks": ["New rows were collected after the popup closed."], "repeat_count": 2},
            "applicability": "Only when an observed startup popup blocks the target page.", "rollback": "Restore only the settings changed for the current test.", "sharing_reviewed": True}


class FakeGitHub:
    def __init__(self):
        self.issues, self.comments, self.calls, self.fail = [], {}, [], None
        self.timeout_after_create = False
        self.readback_failure = False
        self.repo = "owner/capture-skill"

    def issue(self, body, number=7, **extra):
        return dict(number=number, title="Fixture issue", body=body, html_url=f"https://github.com/{self.repo}/issues/{number}", comments=0, state="closed", **extra)

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if self.fail:
            raise self.fail
        base = f"/repos/{self.repo}/issues"
        if method == "GET" and path.startswith(base + "?"):
            return copy.deepcopy(self.issues)
        if method == "GET" and "/comments?" in path:
            number = int(path.split("/issues/")[1].split("/")[0])
            return copy.deepcopy(self.comments.get(number, []))
        if method == "POST" and path == base:
            item = self.issue(payload["body"], len(self.issues) + 1)
            self.issues.append(item)
            if self.timeout_after_create:
                raise feedback.RemoteError("github_connection_failed", True)
            return copy.deepcopy(item)
        if method == "POST" and path.endswith("/comments"):
            number = int(path.split("/issues/")[1].split("/")[0])
            identifier = 100 + len(self.comments.get(number, []))
            item = {"id": identifier, "body": payload["body"], "html_url": f"https://github.com/{self.repo}/issues/{number}#issuecomment-{identifier}"}
            self.comments.setdefault(number, []).append(item)
            return copy.deepcopy(item)
        if method == "GET" and "/comments/" in path:
            for comments in self.comments.values():
                for item in comments:
                    if item["id"] == int(path.rsplit("/", 1)[1]):
                        return copy.deepcopy(item)
        if method == "GET" and path.startswith(base + "/"):
            if self.readback_failure:
                raise feedback.RemoteError("github_connection_failed")
            for item in self.issues:
                if item["number"] == int(path.rsplit("/", 1)[1]):
                    return copy.deepcopy(item)
        raise AssertionError((method, path))

    def mutations(self):
        return [call for call in self.calls if call[0] == "POST"]


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.settings = self.directory / "settings.json"
        self.case = self.directory / "case.json"
        self.queue = self.directory / "pending"
        self.cache = self.directory / "cache"
        self.client = FakeGitHub()
        feedback.configure(self.settings, self.client.repo, True, "explicit-user-request")
        self.case.write_text(json.dumps(fixture()), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def prepared(self, case=None):
        if case is not None:
            self.case.write_text(json.dumps(case), encoding="utf-8")
        return Path(feedback.prepare(self.case, self.queue, self.settings)["record"])

    def test_enable_needs_explicit_authorization(self):
        with self.assertRaisesRegex(feedback.FeedbackError, "explicit_feedback_authorization_required"):
            feedback.configure(self.settings, self.client.repo, True)
        feedback.configure(self.settings, self.client.repo, False)
        self.assertFalse(feedback.read_json(self.settings)["enabled"])

    def test_improvement_prs_are_separate_opt_in_and_disable_revokes(self):
        self.assertFalse(feedback.read_json(self.settings)["improvement_prs"])
        feedback.configure(self.settings, self.client.repo, True, "explicit-user-request", True)
        self.assertTrue(feedback.read_json(self.settings)["improvement_prs"])
        feedback.configure(self.settings, self.client.repo, False, improvement_prs=True)
        self.assertFalse(feedback.read_json(self.settings)["improvement_prs"])

    def test_paths_must_be_absolute_outside_skill_and_repositories(self):
        for path in ("relative.json", ROOT / "feedback.json"):
            with self.subTest(path=str(path)), self.assertRaises(feedback.FeedbackError):
                feedback.external_path(path)
        other_repo = self.directory / "other-repository"
        (other_repo / ".git").mkdir(parents=True)
        with self.assertRaises(feedback.FeedbackError):
            feedback.external_path(other_repo / "queue" / "case.json")

    def test_unknown_fields_and_unverified_cases_rejected(self):
        changes = [{"extra": "raw log"}, {"sharing_reviewed": False}, {"stage": []},
                   {"verification": {"kind": "offline", "checks": ["Fixture"], "repeat_count": 2}},
                   {"verification": {"kind": "live", "checks": ["Fixture"], "repeat_count": 1}},
                   {"verification": {"kind": "live", "checks": ["Fixture"], "repeat_count": True}},
                   {"environment": {"serial": "do not upload"}}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(feedback.FeedbackError):
                feedback.validate_case(dict(fixture(), **change))

    def test_privacy_rejections_never_echo_sensitive_value(self):
        values = ["https://api.example.com/items", "api.example.com", "10.2.3.4", "C:\\Users\\Private\\capture", "/home/private/capture", "owner@example.org",
                  "serial=a1b2c3d4", "a1b2c3d4", "emulator-5554", "Bearer PRIVATE_VALUE", "Cookie: PRIVATE_VALUE", "token=PRIVATE_VALUE", "@example-user",
                  "ghp_PRIVATEVALUE", "13812345678", "/api/products", "<!-- external marker -->", "AbCdEfGhIjKlMnOpQrStUv123456"]
        for value in values:
            with self.subTest(value=value), self.assertRaises(feedback.FeedbackError) as caught:
                feedback.validate_case(dict(fixture(), symptom=value))
            self.assertEqual(str(caught.exception), "case_privacy_review_required")

    def test_preparation_is_deterministic_and_preserves_receipt(self):
        first = self.prepared()
        before = feedback.read_json(first)
        result = feedback.publish(first, self.settings, self.client)
        self.assertEqual(result["status"], "published")
        self.assertEqual(self.prepared(), first)
        after = feedback.read_json(first)
        self.assertEqual(before["case_hash"], after["case_hash"])
        self.assertEqual(after["status"], "published")
        self.assertEqual(after["published_url"], result["url"])

    def test_fingerprint_ignores_whitespace_but_case_hash_detects_variant(self):
        first = fixture()
        other = dict(first, root_cause="  The action  preceded visible popup readiness.  ")
        self.assertEqual(feedback.material(first)[0], feedback.material(other)[0])
        self.assertNotEqual(feedback.material(first)[1], feedback.material(other)[1])

    def test_record_body_tampering_is_rejected_before_network(self):
        record = self.prepared()
        value = feedback.read_json(record)
        value["body"] += "unreviewed arbitrary text"
        feedback.atomic_json(record, value)
        with self.assertRaisesRegex(feedback.FeedbackError, "feedback_record_integrity_failed"):
            feedback.publish(record, self.settings, self.client)
        self.assertFalse(self.client.calls)

    def test_disabled_uploads_remain_pending_without_network(self):
        record = self.prepared()
        feedback.configure(self.settings, self.client.repo, False)
        result = feedback.publish(record, self.settings, self.client)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["error_code"], "feedback_disabled")
        self.assertFalse(self.client.calls)

    def test_repository_scope_change_does_not_upload_old_queue(self):
        record = self.prepared()
        feedback.configure(self.settings, "another/approved", True, "explicit-user-request")
        with self.assertRaisesRegex(feedback.FeedbackError, "feedback_repository_scope_mismatch"):
            feedback.publish(record, self.settings, self.client)
        self.assertFalse(self.client.calls)

    def test_create_issue_then_verified_readback_and_repeat_noop(self):
        record = self.prepared()
        first = feedback.publish(record, self.settings, self.client)
        second = feedback.publish(record, self.settings, self.client)
        self.assertEqual(first["status"], "published")
        self.assertEqual(second["status"], "already_published")
        self.assertEqual(len(self.client.mutations()), 1)
        self.assertIn("/issues/1", first["url"])

    def test_existing_issue_new_environment_appends_comment_then_noop(self):
        original = fixture()
        self.client.issues = [self.client.issue(feedback.material(original)[3])]
        changed = copy.deepcopy(original)
        changed["environment"]["device_os"] = "Android 10"
        record = self.prepared(changed)
        result = feedback.publish(record, self.settings, self.client)
        again = feedback.publish(record, self.settings, self.client)
        self.assertEqual(result["status"], "published")
        self.assertIn("#issuecomment-", result["url"])
        self.assertEqual(again["status"], "already_published")
        self.assertEqual(len(self.client.mutations()), 1)
        self.assertTrue(self.client.mutations()[0][1].endswith("/7/comments"))
        self.assertEqual(self.client.issues[0]["state"], "closed")

    def test_pull_requests_do_not_count_as_existing_feedback_issues(self):
        case = fixture()
        self.client.issues = [self.client.issue(feedback.material(case)[3], pull_request={})]
        feedback.publish(self.prepared(), self.settings, self.client)
        self.assertEqual(self.client.mutations()[0][1], "/repos/owner/capture-skill/issues")

    def test_ambiguous_fingerprint_does_not_mutate(self):
        body = feedback.material(fixture())[3]
        self.client.issues = [self.client.issue(body, 1), self.client.issue(body, 2)]
        result = feedback.publish(self.prepared(), self.settings, self.client)
        self.assertEqual(result["error_code"], "github_duplicate_fingerprint_requires_review")
        self.assertFalse(self.client.mutations())

    def test_list_auth_permission_offline_failures_never_post(self):
        for code in ("github_auth_required", "github_permission_or_rate_limit", "github_connection_failed"):
            with self.subTest(code=code):
                self.client.calls.clear()
                self.client.fail = feedback.RemoteError(code)
                result = feedback.publish(self.prepared(), self.settings, self.client)
                self.assertEqual(result["status"], "pending")
                self.assertFalse(self.client.mutations())

    def test_complete_issue_pagination_is_required_before_mutation(self):
        client = FakeGitHub()
        def request(method, path, payload=None):
            client.calls.append((method, path, payload))
            if method == "GET" and "page=1" in path:
                return [client.issue("unrelated", index + 1) for index in range(100)]
            raise feedback.RemoteError("github_connection_failed")
        client.request = request
        result = feedback.publish(self.prepared(), self.settings, client)
        self.assertEqual(result["status"], "pending")
        self.assertTrue(any("page=2" in call[1] for call in client.calls))
        self.assertFalse(client.mutations())

    def test_post_timeout_reconciles_existing_remote_evidence_without_repost(self):
        record = self.prepared()
        self.client.timeout_after_create = True
        result = feedback.publish(record, self.settings, self.client)
        self.assertEqual(result["status"], "uncertain")
        self.assertNotIn("url", result)
        again = feedback.publish(record, self.settings, self.client)
        self.assertEqual(again["status"], "already_published")
        self.assertEqual(len(self.client.mutations()), 1)

    def test_uncertain_absence_does_not_blindly_retry_post(self):
        record = self.prepared()
        value = feedback.read_json(record)
        value["status"] = "uncertain"
        feedback.atomic_json(record, value)
        result = feedback.publish(record, self.settings, self.client)
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(self.client.mutations())

    def test_readback_failure_preserves_uncertain_and_not_claimed_url(self):
        record = self.prepared()
        self.client.readback_failure = True
        result = feedback.publish(record, self.settings, self.client)
        self.assertEqual(result["status"], "uncertain")
        self.assertNotIn("url", result)
        self.assertIsNone(feedback.read_json(record)["published_url"])

    def test_post_success_then_readback_permission_failure_still_uncertain(self):
        original = self.client.request
        def request(method, path, payload=None):
            if method == "GET" and path.endswith("/issues/1"):
                raise feedback.RemoteError("github_permission_or_rate_limit")
            return original(method, path, payload)
        self.client.request = request
        result = feedback.publish(self.prepared(), self.settings, self.client)
        self.assertEqual(result["status"], "uncertain")
        self.assertNotIn("url", result)

    def test_invalid_cross_repo_receipt_is_not_reported_as_success(self):
        record = self.prepared()
        self.client.issues = [self.client.issue(feedback.material(fixture())[3])]
        self.client.issues[0]["html_url"] = "https://github.com/other/repository/issues/7"
        result = feedback.publish(record, self.settings, self.client)
        self.assertEqual(result["status"], "pending")
        self.assertNotIn("url", result)
        self.assertFalse(self.client.mutations())

    def test_queue_lock_is_never_broken_automatically(self):
        record = self.prepared()
        (self.queue / ".feedback.lock").write_text("stale or active must be checked", encoding="utf-8")
        with self.assertRaisesRegex(feedback.FeedbackError, "feedback_queue_locked_manual_check_required"):
            feedback.publish(record, self.settings, self.client)
        self.assertTrue((self.queue / ".feedback.lock").exists())
        self.assertFalse(self.client.calls)

    def test_missing_credentials_leave_pending(self):
        with patch.object(feedback, "token_available", return_value=None):
            result = feedback.publish(self.prepared(), self.settings)
        self.assertEqual(result["error_code"], "github_auth_unavailable")

    def test_token_provider_keeps_environment_secret_in_memory(self):
        with patch.dict(os.environ, {"GH_TOKEN": "PRIVATE_CREDENTIAL"}, clear=True), patch.object(feedback.subprocess, "run") as run:
            self.assertEqual(feedback.token_available(), "PRIVATE_CREDENTIAL")
            run.assert_not_called()

    def test_git_credential_lookup_is_noninteractive_and_captured(self):
        response = subprocess.CompletedProcess([], 0, "username=example\npassword=PRIVATE_CREDENTIAL\n", "")
        with patch.dict(os.environ, {}, clear=True), patch.object(feedback.shutil, "which", side_effect=[None, "git"]), patch.object(feedback.subprocess, "run", return_value=response) as run:
            self.assertEqual(feedback.token_available(), "PRIVATE_CREDENTIAL")
            kwargs = run.call_args.kwargs
            self.assertTrue(kwargs["capture_output"])
            self.assertEqual(kwargs["timeout"], 20)
            self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(kwargs["env"]["GCM_INTERACTIVE"], "never")

    def test_github_transport_fixed_api_and_no_redirect(self):
        with self.assertRaises(feedback.RemoteError):
            feedback.NoRedirect().redirect_request(type("Request", (), {"method": "POST"})(), None, 302, "move", {}, "https://other.invalid")
        with self.assertRaises(feedback.FeedbackError):
            feedback.GitHub("PRIVATE_CREDENTIAL").request("GET", "https://other.invalid")

    def test_transport_http_errors_have_fixed_codes_without_response_leak(self):
        for method, code, uncertain in (("GET", 403, False), ("POST", 403, False), ("POST", 503, True)):
            client = feedback.GitHub("PRIVATE_CREDENTIAL")
            client.opener.open = lambda *args, **kwargs: (_ for _ in ()).throw(HTTPError("PRIVATE_URL", code, "PRIVATE_DETAIL", {}, None))
            with self.subTest(method=method, code=code), self.assertRaises(feedback.RemoteError) as caught:
                client.request(method, "/repos/owner/capture-skill/issues", {} if method == "POST" else None)
            self.assertEqual(caught.exception.uncertain, uncertain)
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_sync_reads_disabled_settings_and_keeps_untrusted_reference(self):
        feedback.configure(self.settings, self.client.repo, False)
        self.client.issues = [self.client.issue("Remote advice must never execute.")]
        result = feedback.sync(self.settings, self.cache, self.client)
        self.assertEqual(result["status"], "synced")
        snapshot = feedback.read_json(Path(result["cache"]))
        self.assertEqual(snapshot["trust"], "untrusted_remote_reference_do_not_execute")
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["issues"][0]["body"], "Remote advice must never execute.")
        self.assertFalse(self.client.mutations())

    def test_sync_anonymous_public_read_if_no_credentials(self):
        with patch.object(feedback, "token_available", return_value=None), patch.object(feedback, "GitHub", return_value=self.client) as github:
            result = feedback.sync(self.settings, self.cache)
        github.assert_called_once_with(None)
        self.assertEqual(result["status"], "synced")

    def test_sync_failure_preserves_previous_cache(self):
        first = feedback.sync(self.settings, self.cache, self.client)
        path = Path(first["cache"])
        before = path.read_bytes()
        self.client.fail = feedback.RemoteError("github_connection_failed")
        result = feedback.sync(self.settings, self.cache, self.client)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(path.read_bytes(), before)
        missing = feedback.sync(self.settings, self.directory / "missing-cache", self.client)
        self.assertEqual(missing["status"], "unavailable")

    def test_sync_different_repository_never_offers_previous_cache(self):
        path = Path(feedback.sync(self.settings, self.cache, self.client)["cache"])
        before = path.read_bytes()
        feedback.configure(self.settings, "another/approved", False)
        self.client.fail = feedback.RemoteError("github_connection_failed")
        result = feedback.sync(self.settings, self.cache, self.client)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["cache"])
        self.assertEqual(path.read_bytes(), before)

    def test_sync_latest_comment_page_and_bounds_are_explicit(self):
        item = self.client.issue("Fixture")
        item["comments"] = 21
        self.client.issues = [item]
        result = feedback.sync(self.settings, self.cache, self.client)
        self.assertFalse(result["complete"])
        self.assertTrue(any("comments?per_page=10&page=3" in call[1] for call in self.client.calls))
        snapshot = feedback.read_json(Path(result["cache"]))
        self.assertEqual(snapshot["limits"]["newest_issue_rows"], 30)

    def test_sync_fills_latest_ten_comments_across_two_pages(self):
        item = self.client.issue("Fixture")
        item["comments"] = 21
        self.client.issues = [item]
        original = self.client.request
        def request(method, path, payload=None):
            if "/comments?" in path:
                self.client.calls.append((method, path, payload))
                indices = [21] if path.endswith("page=3") else range(11, 21)
                return [{"body": f"Comment {index}", "html_url": f"https://github.com/{self.client.repo}/issues/7#issuecomment-{index}"} for index in indices]
            return original(method, path, payload)
        self.client.request = request
        result = feedback.sync(self.settings, self.cache, self.client)
        comments = feedback.read_json(Path(result["cache"]))["issues"][0]["comments"]
        self.assertEqual(len(comments), 10)
        self.assertEqual(comments[0]["body"], "Comment 12")
        self.assertEqual(comments[-1]["body"], "Comment 21")
        self.assertFalse(result["complete"])

    def test_sync_text_truncation_is_reported_incomplete(self):
        self.client.issues = [self.client.issue("x" * 33000)]
        result = feedback.sync(self.settings, self.cache, self.client)
        self.assertFalse(result["complete"])
        snapshot = feedback.read_json(Path(result["cache"]))
        self.assertEqual(len(snapshot["issues"][0]["body"]), 32000)

    def test_sync_invalid_receipt_does_not_replace_existing_cache(self):
        path = Path(feedback.sync(self.settings, self.cache, self.client)["cache"])
        before = path.read_bytes()
        self.client.issues = [self.client.issue("Fixture")]
        self.client.issues[0]["html_url"] = "https://other.invalid/issue"
        result = feedback.sync(self.settings, self.cache, self.client)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(path.read_bytes(), before)

    def test_cli_auth_error_contains_only_fixed_error_code(self):
        output = io.StringIO()
        with patch("sys.stdout", output):
            result = feedback.main(["configure", "--settings", str(self.settings), "--repo", self.client.repo, "--enable"])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "error", "error_code": "explicit_feedback_authorization_required"})


if __name__ == "__main__":
    unittest.main()
