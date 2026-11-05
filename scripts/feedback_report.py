#!/usr/bin/env python3
"""Queue reviewed, verified, sanitized cases; submit bounded GitHub feedback.

The agent must establish the human's authorization and review privacy first.
Flags and live-verification assertions cannot independently prove either fact.
No captures, attachments, arbitrary API URLs, or automatic code changes exist here.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
STAGES = {"setup", "device", "app_baseline", "mcp", "capture", "certificate", "decode", "mapping", "pagination", "collector", "restore"}
ENVIRONMENT = {"host_os", "host_arch", "device_kind", "device_os", "device_arch", "device_family", "proxypin_version", "runtime_version", "app_family", "app_version"}
CASE_KEYS = {"version", "problem_key", "title", "stage", "environment", "symptom", "root_cause", "root_cause_status", "resolution", "verification", "applicability", "rollback", "sharing_reviewed"}
SETTINGS_KEYS = {"version", "repo", "enabled", "authorization", "improvement_prs"}
RECORD_KEYS = {"version", "repo", "case", "fingerprint", "case_hash", "title", "body", "status", "published_url", "error_code"}
PRIVATE = re.compile(
    r"(?i)(?:\b(?:https?|ftp|file|wss?)://|\bwww\.|\b[a-z0-9-]+\.(?:com|net|org|io|cn|dev|app)(?:\b|/)|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[a-f0-9]{1,4}:){3,}[a-f0-9:]*|"
    r"[a-z]:[\\/]|\\\\[a-z0-9]|/(?:Users|home|data|sdcard)/|(?:^|\s)/(?:api|v\d+|graphql)(?:/|\b)|"
    r"\b[^\s@]+@[^\s@]+\.[^\s@]+|(?<!\w)@[a-z0-9_-]{1,39}\b|\bemulator-\d+\b|\b[a-f0-9]{8,}\b|"
    r"\b(?:gh[pousr]_|github_pat_)[a-z0-9_]+|\bAKIA[A-Z0-9]{16}|\beyJ[a-z0-9_-]{12,}|"
    r"\bbearer\s+\S+|\b(?:authorization|cookie|set-cookie)\s*:|"
    r"\b(?:password|token|secret|api[-_]?key|serial|imei|imsi|android[-_]?id|session[-_]?id)\s*[=:]\s*\S+|"
    r"<!--|-->|(?<!\d)(?:\+?\d[ -]?){10,15}(?!\d))"
)


class FeedbackError(Exception):
    """Only fixed, non-sensitive codes are exposed."""


class RemoteError(FeedbackError):
    def __init__(self, code, uncertain=False):
        super().__init__(code)
        self.uncertain = uncertain


def external_path(value):
    path = Path(value)
    if not path.is_absolute():
        raise FeedbackError("absolute_external_path_required")
    path = path.resolve()
    if path.is_relative_to(ROOT):
        raise FeedbackError("feedback_must_be_outside_skill_repository")
    for parent in [path, *path.parents]:
        if (parent / ".git").exists() or (parent / "SKILL.md").is_file():
            raise FeedbackError("feedback_must_be_outside_skill_repository")
    return path


def read_json(path, max_bytes=128 * 1024):
    try:
        if path.stat().st_size > max_bytes:
            raise FeedbackError("feedback_file_too_large")
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        raise FeedbackError("invalid_feedback_file") from None


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise FeedbackError("feedback_write_failed") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def queue_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".feedback.lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise FeedbackError("feedback_queue_locked_manual_check_required") from None
    try:
        os.close(descriptor)
        yield
    finally:
        path.unlink(missing_ok=True)


def valid_repo(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value) or value.split("/")[1] in {".", ".."}:
        raise FeedbackError("invalid_feedback_repository")
    return value


def validate_settings(value):
    if (not isinstance(value, dict) or set(value) != SETTINGS_KEYS or type(value["version"]) is not int
            or value["version"] != 1 or type(value["enabled"]) is not bool or type(value["improvement_prs"]) is not bool):
        raise FeedbackError("invalid_feedback_settings")
    valid_repo(value["repo"])
    if (value["authorization"] is not None and value["authorization"] != "explicit-user-request") or (value["enabled"] and value["authorization"] != "explicit-user-request"):
        raise FeedbackError("explicit_feedback_authorization_required")
    return value


def configure(settings_path, repo, enabled, authorization=None, improvement_prs=False):
    path = external_path(settings_path)
    if enabled and authorization != "explicit-user-request":
        raise FeedbackError("explicit_feedback_authorization_required")
    settings = {"version": 1, "repo": valid_repo(repo), "enabled": enabled,
                "authorization": authorization if enabled else None, "improvement_prs": bool(improvement_prs) if enabled else False}
    validate_settings(settings)
    atomic_json(path, settings)
    return {"status": "configured", "enabled": enabled, "repo": repo, "improvement_prs": settings["improvement_prs"]}


def safe_text(value, limit=2000, single_line=False):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 and char not in "\n\t" for char in value)
            or (single_line and ("\n" in value or "\r" in value))):
        raise FeedbackError("invalid_case_text")
    if PRIVATE.search(value) or re.search(r"\b(?=[A-Za-z0-9_-]{24,}\b)(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]+\b", value):
        raise FeedbackError("case_privacy_review_required")
    return value


def text_list(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        raise FeedbackError("invalid_case_steps")
    for entry in value:
        safe_text(entry, 1000)


def validate_case(case):
    if not isinstance(case, dict) or set(case) != CASE_KEYS or type(case["version"]) is not int or case["version"] != 1:
        raise FeedbackError("invalid_case_schema")
    if not isinstance(case["problem_key"], str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", case["problem_key"]) or len(case["problem_key"]) > 80:
        raise FeedbackError("invalid_problem_key")
    safe_text(case["problem_key"], 80, True)
    if (not isinstance(case["stage"], str) or case["stage"] not in STAGES
            or not isinstance(case["root_cause_status"], str) or case["root_cause_status"] not in {"confirmed", "suspected"} or case["sharing_reviewed"] is not True):
        raise FeedbackError("reviewed_resolved_case_required")
    environment = case["environment"]
    if not isinstance(environment, dict) or not environment or not set(environment) <= ENVIRONMENT:
        raise FeedbackError("invalid_case_environment")
    for value in environment.values():
        safe_text(value, 80, True)
    safe_text(case["title"], 150, True)
    for key in ("symptom", "root_cause", "applicability", "rollback"):
        safe_text(case[key])
    text_list(case["resolution"])
    verification = case["verification"]
    if (not isinstance(verification, dict) or set(verification) != {"kind", "checks", "repeat_count"}
            or verification["kind"] != "live" or type(verification["repeat_count"]) is not int or not 2 <= verification["repeat_count"] <= 1000):
        raise FeedbackError("repeated_live_verification_required")
    text_list(verification["checks"])
    return case


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def material(case):
    validate_case(case)
    identity = {key: " ".join(case[key].split()).casefold() for key in ("problem_key", "stage", "root_cause", "root_cause_status")}
    fingerprint, case_hash = digest(identity), digest(case)
    marker = f"<!-- proxypin-feedback:{fingerprint} -->"
    exact = f"<!-- proxypin-case:{case_hash} -->"
    lines = [marker, exact, "", "## Problem", case["symptom"], "", "## Environment"]
    lines += [f"- {key}: {case['environment'][key]}" for key in sorted(case["environment"])]
    lines += ["", f"Stage: {case['stage']}", "", f"## Root cause ({case['root_cause_status']})", case["root_cause"], "", "## Verified resolution"]
    lines += [f"{index}. {step}" for index, step in enumerate(case["resolution"], 1)]
    lines += ["", "## Verification", f"Live repetitions: {case['verification']['repeat_count']}"]
    lines += [f"- {step}" for step in case["verification"]["checks"]]
    lines += ["", "## Applicability", case["applicability"], "", "## Rollback", case["rollback"], "", "Agent-reviewed sanitized case; no raw captures or attachments. Verification and privacy review are agent assertions."]
    return fingerprint, case_hash, "[Resolved] " + case["title"], "\n".join(lines) + "\n"


def validate_record(record):
    if not isinstance(record, dict) or set(record) != RECORD_KEYS or type(record["version"]) is not int or record["version"] != 1:
        raise FeedbackError("invalid_feedback_record")
    valid_repo(record["repo"])
    expected = material(record["case"])
    if tuple(record[key] for key in ("fingerprint", "case_hash", "title", "body")) != expected:
        raise FeedbackError("feedback_record_integrity_failed")
    if not isinstance(record["status"], str) or record["status"] not in {"pending", "uncertain", "published"} or (record["error_code"] is not None and not re.fullmatch(r"[a-z_]+", str(record["error_code"]))):
        raise FeedbackError("invalid_feedback_record")
    if record["published_url"] is not None:
        valid_url(record["published_url"], record["repo"])
    return record


def prepare(case_path, queue_dir, settings_path):
    case_path, queue_dir, settings_path = map(external_path, (case_path, queue_dir, settings_path))
    settings, case = validate_settings(read_json(settings_path)), validate_case(read_json(case_path))
    fingerprint, case_hash, title, body = material(case)
    path = external_path(queue_dir / (case_hash + ".json"))
    with queue_lock(queue_dir):
        if path.exists():
            record = validate_record(read_json(path))
            if record["repo"] != settings["repo"] or record["case"] != case:
                raise FeedbackError("feedback_queue_record_conflict")
        else:
            record = {"version": 1, "repo": settings["repo"], "case": case, "fingerprint": fingerprint,
                      "case_hash": case_hash, "title": title, "body": body, "status": "pending", "published_url": None, "error_code": None}
            atomic_json(path, record)
    return {"status": record["status"], "record": str(path)}


def token_available():
    def usable(value):
        return value if value and len(value) <= 4096 and not any(char.isspace() for char in value) else None
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if name in os.environ:
            return usable(os.environ[name].strip())
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GH_PROMPT_DISABLED="1")
    gh = shutil.which("gh")
    if gh:
        try:
            result = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=20, env=environment)
            token = usable(result.stdout.strip()) if result.returncode == 0 else None
            if token:
                return token
        except (OSError, subprocess.SubprocessError):
            pass
    git = shutil.which("git")
    if git:
        try:
            result = subprocess.run([git, "-c", "credential.interactive=false", "credential", "fill"], input="protocol=https\nhost=github.com\n\n", capture_output=True, text=True, timeout=20, env=environment)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if line.startswith("password="):
                        return usable(line.partition("=")[2])
        except (OSError, subprocess.SubprocessError):
            pass
    return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise RemoteError("github_redirect_rejected", request.method == "POST")


class GitHub:
    def __init__(self, token):
        self.token, self.opener = token, build_opener(NoRedirect())

    def request(self, method, path, payload=None):
        if method not in {"GET", "POST"} or not path.startswith("/repos/") or ".." in path or "#" in path:
            raise FeedbackError("invalid_github_api_path")
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2026-03-10", "User-Agent": "proxypin-capture-assistant-feedback"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request("https://api.github.com" + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise RemoteError("github_response_too_large", method == "POST")
                return json.loads(raw)
        except HTTPError as error:
            code = "github_auth_required" if error.code == 401 else "github_permission_or_rate_limit" if error.code in {403, 429} else "github_request_rejected" if error.code < 500 else "github_unavailable"
            raise RemoteError(code, method == "POST" and error.code >= 500) from None
        except (URLError, OSError, TimeoutError, HTTPException):
            raise RemoteError("github_connection_failed", method == "POST") from None
        except (ValueError, UnicodeError):
            raise RemoteError("github_invalid_response", method == "POST") from None


def valid_url(value, repo, number=None, comment=False):
    pattern = r"https://github\.com/" + re.escape(repo) + r"/issues/(" + (str(number) if number is not None else r"[1-9]\d*") + r")"
    if comment:
        pattern += r"#issuecomment-[1-9]\d*"
    elif number is None:
        pattern += r"(?:#issuecomment-[1-9]\d*)?"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise RemoteError("github_invalid_receipt")
    return value


def paginated(client, path):
    separator = "&" if "?" in path else "?"
    result = []
    for page in range(1, 21):
        batch = client.request("GET", f"{path}{separator}per_page=100&page={page}")
        if not isinstance(batch, list) or len(batch) > 100 or any(not isinstance(item, dict) for item in batch):
            raise RemoteError("github_invalid_response")
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise RemoteError("github_dedup_pagination_limit")


def find_existing(client, record):
    repo = record["repo"]
    fingerprint = "<!-- proxypin-feedback:" + record["fingerprint"] + " -->"
    exact = "<!-- proxypin-case:" + record["case_hash"] + " -->"
    issues = paginated(client, f"/repos/{repo}/issues?state=all")
    matching = [issue for issue in issues if "pull_request" not in issue and fingerprint in str(issue.get("body") or "")]
    if len(matching) > 1:
        raise RemoteError("github_duplicate_fingerprint_requires_review")
    if not matching:
        return None, None
    issue = matching[0]
    number = issue.get("number")
    if type(number) is not int or number < 1:
        raise RemoteError("github_invalid_response")
    url = valid_url(issue.get("html_url"), repo, number)
    if exact in str(issue.get("body") or ""):
        return number, url
    comments = paginated(client, f"/repos/{repo}/issues/{number}/comments")
    matches = [comment for comment in comments if fingerprint in str(comment.get("body") or "") and exact in str(comment.get("body") or "")]
    if len(matches) > 1:
        raise RemoteError("github_duplicate_case_requires_review")
    return number, valid_url(matches[0].get("html_url"), repo, number, True) if matches else None


def publish(record_path, settings_path, client=None):
    record_path, settings_path = map(external_path, (record_path, settings_path))
    settings = validate_settings(read_json(settings_path))
    with queue_lock(record_path.parent):
        record = validate_record(read_json(record_path))
        if record["repo"] != settings["repo"]:
            raise FeedbackError("feedback_repository_scope_mismatch")
        mutation_started = False
        post_completed = False
        try:
            if not settings["enabled"]:
                raise RemoteError("feedback_disabled")
            if client is None:
                token = token_available()
                if not token:
                    raise RemoteError("github_auth_unavailable")
                client = GitHub(token)
            number, url = find_existing(client, record)
            if url:
                result_status = "already_published"
            else:
                if record["status"] in {"uncertain", "published"}:
                    raise RemoteError("github_prior_submission_not_reconciled", True)
                # Search success proves absence; at most one mutation follows.
                mutation_started = True
                if number is None:
                    response = client.request("POST", f"/repos/{record['repo']}/issues", {"title": record["title"], "body": record["body"]})
                    post_completed = True
                    number = response.get("number") if isinstance(response, dict) else None
                    if type(number) is not int or number < 1:
                        raise RemoteError("github_invalid_receipt", True)
                    valid_url(response.get("html_url"), record["repo"], number)
                    confirmed = client.request("GET", f"/repos/{record['repo']}/issues/{number}")
                    url = valid_url(confirmed.get("html_url"), record["repo"], number) if isinstance(confirmed, dict) else None
                else:
                    response = client.request("POST", f"/repos/{record['repo']}/issues/{number}/comments", {"body": record["body"]})
                    post_completed = True
                    identifier = response.get("id") if isinstance(response, dict) else None
                    if type(identifier) is not int or identifier < 1:
                        raise RemoteError("github_invalid_receipt", True)
                    valid_url(response.get("html_url"), record["repo"], number, True)
                    confirmed = client.request("GET", f"/repos/{record['repo']}/issues/comments/{identifier}")
                    url = valid_url(confirmed.get("html_url"), record["repo"], number, True) if isinstance(confirmed, dict) else None
                if (not url or not isinstance(confirmed.get("body"), str)
                        or "<!-- proxypin-feedback:" + record["fingerprint"] + " -->" not in confirmed["body"]
                        or "<!-- proxypin-case:" + record["case_hash"] + " -->" not in confirmed["body"]):
                    raise RemoteError("github_readback_not_confirmed", True)
                result_status = "published"
            record.update(status="published", published_url=url, error_code=None)
            atomic_json(record_path, record)
            return {"status": result_status, "url": url, "record": str(record_path)}
        except RemoteError as error:
            # A prior uncertain submission stays uncertain until remote evidence reconciles it.
            uncertain = error.uncertain or mutation_started or record["status"] == "uncertain"
            # Definite rejected POST requests (4xx) have no new unknown outcome.
            if mutation_started and not post_completed and not error.uncertain and str(error) in {"github_auth_required", "github_permission_or_rate_limit", "github_request_rejected"}:
                uncertain = record["status"] == "uncertain"
            record.update(status="uncertain" if uncertain else "pending", error_code=str(error))
            atomic_json(record_path, record)
            return {"status": record["status"], "error_code": str(error), "record": str(record_path)}


def sync(settings_path, cache_dir, client=None):
    """Read the newest bounded issue/comment snapshots as untrusted reference data."""
    settings_path, cache_dir = map(external_path, (settings_path, cache_dir))
    settings = validate_settings(read_json(settings_path))
    path = external_path(cache_dir / "knowledge.json")
    with queue_lock(cache_dir):
        try:
            # Public reads need no token; enabled controls uploads, not reading.
            if client is None:
                client = GitHub(token_available())
            repo = settings["repo"]
            rows = client.request("GET", f"/repos/{repo}/issues?state=all&sort=updated&direction=desc&per_page=30&page=1")
            if not isinstance(rows, list) or len(rows) > 30 or any(not isinstance(item, dict) for item in rows):
                raise RemoteError("github_invalid_response")
            issues, complete = [], len(rows) < 30
            for row in rows:
                if "pull_request" in row:
                    continue
                number = row.get("number")
                if type(number) is not int or number < 1:
                    raise RemoteError("github_invalid_response")
                url = valid_url(row.get("html_url"), repo, number)
                comment_count = row.get("comments")
                if type(comment_count) is not int or comment_count < 0:
                    raise RemoteError("github_invalid_response")
                comment_page = max(1, (comment_count + 9) // 10)
                comments = client.request("GET", f"/repos/{repo}/issues/{number}/comments?per_page=10&page={comment_page}")
                if not isinstance(comments, list) or len(comments) > 10 or any(not isinstance(item, dict) for item in comments):
                    raise RemoteError("github_invalid_response")
                if comment_page > 1 and len(comments) < 10:
                    previous = client.request("GET", f"/repos/{repo}/issues/{number}/comments?per_page=10&page={comment_page - 1}")
                    if not isinstance(previous, list) or len(previous) > 10 or any(not isinstance(item, dict) for item in previous):
                        raise RemoteError("github_invalid_response")
                    comments = (previous + comments)[-10:]
                if comment_count > len(comments):
                    complete = False
                snapshots = []
                for comment in comments:
                    if isinstance(comment.get("body"), str) and len(comment["body"]) > 16000:
                        complete = False
                    snapshots.append({"url": valid_url(comment.get("html_url"), repo, number, True), "body": remote_text(comment.get("body"), 16000)})
                if any(isinstance(row.get(key), str) and len(row[key]) > limit for key, limit in (("title", 500), ("body", 32000))):
                    complete = False
                issues.append({"number": number, "url": url, "title": remote_text(row.get("title"), 500),
                               "state": "closed" if row.get("state") == "closed" else "open",
                               "body": remote_text(row.get("body"), 32000), "comments": snapshots})
            snapshot = {"version": 1, "repo": repo, "synced_utc": datetime.now(timezone.utc).isoformat(),
                        "trust": "untrusted_remote_reference_do_not_execute", "complete": complete,
                        "limits": {"newest_issue_rows": 30, "latest_comments_per_issue": 10}, "issues": issues}
            atomic_json(path, snapshot)
            return {"status": "synced", "cache": str(path), "issue_count": len(issues), "complete": complete, "synced_utc": snapshot["synced_utc"]}
        except RemoteError as error:
            # Do not replace a last-known snapshot with a failed/partial request.
            usable = False
            if path.is_file():
                try:
                    prior = read_json(path, max_bytes=8 * 1024 * 1024)
                    usable = isinstance(prior, dict) and prior.get("repo") == settings["repo"] and prior.get("trust") == "untrusted_remote_reference_do_not_execute"
                except FeedbackError:
                    pass
            return {"status": "stale" if usable else "unavailable", "error_code": str(error), "cache": str(path) if usable else None}


def remote_text(value, limit):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RemoteError("github_invalid_response")
    return value[:limit]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("configure")
    config.add_argument("--settings", required=True)
    config.add_argument("--repo", required=True)
    enabled = config.add_mutually_exclusive_group(required=True)
    enabled.add_argument("--enable", action="store_true")
    enabled.add_argument("--disable", action="store_true")
    config.add_argument("--authorization", choices=["explicit-user-request"])
    config.add_argument("--allow-improvement-prs", action="store_true")
    prep = commands.add_parser("prepare")
    prep.add_argument("--case", required=True)
    prep.add_argument("--queue-dir", required=True)
    prep.add_argument("--settings", required=True)
    sending = commands.add_parser("publish")
    sending.add_argument("--record", required=True)
    sending.add_argument("--settings", required=True)
    refresh = commands.add_parser("sync")
    refresh.add_argument("--settings", required=True)
    refresh.add_argument("--cache-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "configure":
            result = configure(args.settings, args.repo, args.enable, args.authorization, args.allow_improvement_prs)
        elif args.command == "prepare":
            result = prepare(args.case, args.queue_dir, args.settings)
        elif args.command == "publish":
            result = publish(args.record, args.settings)
        else:
            result = sync(args.settings, args.cache_dir)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] in {"configured", "published", "already_published", "synced"} or args.command == "prepare" else 2
    except (FeedbackError, OSError) as error:
        code = str(error) if isinstance(error, FeedbackError) else "feedback_local_io_failed"
        print(json.dumps({"status": "error", "error_code": code}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
