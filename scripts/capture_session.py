#!/usr/bin/env python3
"""Keep evidence and the next capture step in a local, portable session.

Python 3.10+ standard library only. This helper never installs software, controls
devices, connects to MCP, captures traffic, downloads images, or proves business
success. An agent-observed record is the agent's explicit observation, not a
truth judgment by this program. Keep credentials and raw bodies in their original
evidence files; the state stores only paths, sizes, SHA256, and fixed labels.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

# Embeddable Python and python -I may omit this script's directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_capture import resource_signature_key, sensitive_key

STEPS = ("target", "host_tools", "route_device", "app_baseline", "mcp",
         "capture_route", "real_flow", "required_body", "mapping",
         "sample_output", "batch", "deliver", "restore")
KINDS = ("agent-observed", "file-validated", "user-reported")
REASONS = ("user-action-required", "device-locked", "tool-unavailable",
           "evidence-missing", "verification-failed", "other")
ROUTE_REASONS = ("emulator-incompatible", "physical-unavailable", "user-selected")
STATE_FILE = "capture-session.json"
SKILL_ROOT = Path(__file__).resolve().parent.parent
ACTIONS = {
    "target": "Observe the target, goal, scope, and required device route.",
    "host_tools": "Check actual host tools and runtime; retain nonsecret evidence.",
    "route_device": "Verify the selected device route and device control capability.",
    "app_baseline": "Verify the target App works before capture and record its baseline.",
    "mcp": "Verify the actual MCP connection and tools/list separately from device control.",
    "capture_route": "Verify actual VPN/proxy routing and required HTTPS decryption.",
    "real_flow": "Associate a real target action, baseline/time, and new captured flow.",
    "required_body": "Read the required complete body; verify business status from evidence.",
    "mapping": "Check extraction fields against the complete observed response.",
    "sample_output": "Validate a real output array, then record an observed source spot check.",
    "batch": "Record observed read-only pagination, stop reason, and coverage limits; validate the final output separately from the sample.",
    "deliver": "Validate the final output when batch was requested, verify delivered files, and report actual counts, coverage, and remaining work.",
    "restore": "Restore only changes made by this task and verify their actual state.",
    "complete": "Workflow records are complete; assess business success from the actual evidence.",
}


class SessionError(Exception):
    """A static, nonsecret error code suitable for stdout."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def external_work_dir(work: Path) -> None:
    if work.resolve().is_relative_to(SKILL_ROOT.resolve()):
        raise SessionError("work_dir_must_be_outside_skill_repository")


def portable_path(path: Path, work: Path) -> str:
    # Relative paths survive moving the whole session/evidence tree. Cross-drive
    # evidence must be placed in the chosen work directory instead of pinning it
    # to a machine-specific drive path.
    try:
        value = os.path.relpath(path.resolve(), work.resolve())
    except ValueError as exc:
        raise SessionError("evidence_on_other_drive_copy_it_into_work_dir") from exc
    if any(ord(char) < 32 for char in value) or re.search(
            r"(?i)(token|password|secret|authorization|cookie)[=:]", value):
        raise SessionError("evidence_path_contains_sensitive_or_control_text")
    return Path(value).as_posix()


def file_evidence(path: Path, work: Path, kind: str) -> dict:
    try:
        path = path.resolve(strict=True)
        if not path.is_file() or path.stat().st_size == 0:
            raise SessionError("evidence_must_be_a_nonempty_local_file")
        return {"path": portable_path(path, work), "bytes": path.stat().st_size,
                "sha256": digest(path), "kind": kind, "recorded_utc": now()}
    except OSError as exc:
        raise SessionError("evidence_missing_or_unreadable") from exc


def matches(evidence: dict, work: Path) -> bool:
    try:
        path = work / evidence["path"]
        return (path.is_file() and path.stat().st_size == evidence["bytes"]
                and digest(path) == evidence["sha256"])
    except (OSError, KeyError, TypeError):
        return False


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(path)
    except FileExistsError as exc:
        raise SessionError("state_temporary_file_exists_do_not_overwrite") from exc


@contextmanager
def locked(work: Path):
    external_work_dir(work)
    lock = work / "capture-session.lock"
    try:
        with lock.open("x", encoding="ascii") as stream:
            stream.write(str(os.getpid()))
    except FileExistsError as exc:
        raise SessionError("session_busy_or_stale_lock_review_before_retry") from exc
    except OSError as exc:
        raise SessionError("work_dir_unavailable") from exc
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def load_state(work: Path) -> dict:
    try:
        state = json.loads((work / STATE_FILE).read_text(encoding="utf-8"))
        if (not isinstance(state, dict) or state.get("version") != 1
                or set(state.get("steps", {})) != set(STEPS)
                or state.get("goal") not in {"products", "requests", "data"}
                or state.get("route") not in {"physical", "emulator", "browser", "existing-capture"}):
            raise SessionError("invalid_session_state")
        for entry in state["steps"].values():
            if (not isinstance(entry, dict) or not isinstance(entry.get("evidence"), list)
                    or entry.get("status") not in {"pending", "recorded", "skipped"}):
                raise SessionError("invalid_session_state")
            if entry.get("pending_reason") is not None and entry["pending_reason"] not in REASONS:
                raise SessionError("invalid_session_state")
        recovery = state.get("failure_recovery")
        if recovery is not None:
            if not isinstance(recovery, dict) or not isinstance(recovery.get("evidence"), list):
                raise SessionError("invalid_session_state")
            for evidence in recovery["evidence"]:
                if (not isinstance(evidence, dict) or evidence.get("kind") not in KINDS
                        or evidence.get("business_step") not in STEPS
                        or evidence.get("pending_reason") not in REASONS):
                    raise SessionError("invalid_session_state")
            accepted_index = recovery.get("accepted_evidence")
            if accepted_index is not None and (
                    type(accepted_index) is not int or not 0 <= accepted_index < len(recovery["evidence"])
                    or recovery["evidence"][accepted_index]["kind"] != "agent-observed"):
                raise SessionError("invalid_session_state")
        return state
    except FileNotFoundError as exc:
        raise SessionError("session_not_initialized") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise SessionError("invalid_or_unreadable_session_state") from exc


def current_step(state: dict, work: Path) -> str:
    for step in STEPS:
        entry = state["steps"][step]
        if entry["status"] == "skipped":
            continue
        if step == "real_flow" and state.get("existing_capture"):
            if not matches(state["existing_capture"]["evidence"], work):
                return step
        if entry["status"] != "recorded":
            return step
        try:
            accepted = entry["evidence"][entry["accepted_evidence"]]
        except (KeyError, IndexError, TypeError) as exc:
            raise SessionError("invalid_accepted_evidence") from exc
        if not matches(accepted, work):
            return step
        if step == "sample_output":
            validation = state.get("output_validation")
            if not validation or not matches(validation["evidence"], work):
                return step
        if step == "deliver" and delivery_error(state, work):
            return step
    return "complete"


def state_errors(state: dict, work: Path) -> list[str]:
    errors = []
    imported = state.get("existing_capture")
    if imported and not matches(imported["evidence"], work):
        errors.append("existing_capture_changed_or_unavailable")
    for step in STEPS:
        entry = state["steps"][step]
        if entry["status"] == "recorded":
            if not matches(entry["evidence"][entry["accepted_evidence"]], work):
                errors.append("accepted_evidence_changed_or_unavailable:" + step)
    validation = state.get("output_validation")
    if validation and not matches(validation["evidence"], work):
        errors.append("validated_output_changed_or_unavailable")
    final = state.get("final_output_validation")
    if final and not matches(final["evidence"], work):
        errors.append("validated_final_output_changed_or_unavailable")
    if state["steps"]["deliver"]["status"] == "recorded":
        delivery = delivery_error(state, work)
        if delivery and delivery not in errors:
            errors.append(delivery)
    recovery = state.get("failure_recovery")
    if recovery and recovery.get("accepted_evidence") is not None:
        accepted = recovery["evidence"][recovery["accepted_evidence"]]
        if not matches(accepted, work):
            errors.append("failure_recovery_evidence_changed_or_unavailable")
    return errors


def delivery_error(state: dict, work: Path) -> str | None:
    final = state.get("final_output_validation")
    if state["batch_requested"] and not final:
        return "final_output_validation_required_for_batch"
    # A final output that was explicitly selected must stay valid. Never silently
    # fall back to the smaller sample when the delivered final file changes.
    validation = final or state.get("output_validation")
    if not validation:
        return "output_validation_required_before_delivery"
    if not matches(validation["evidence"], work):
        return "delivery_output_changed_or_unavailable"
    return None


def recovery_status(state: dict, work: Path) -> dict:
    recovery = state.get("failure_recovery") or {"evidence": []}
    records = recovery["evidence"]
    valid_count = sum(matches(item, work) for item in records)
    value = {"confirmation": "not-recorded", "evidence_count": len(records),
             "evidence_integrity": {"valid": valid_count, "changed_or_unavailable": len(records) - valid_count},
             "business_step_at_observation": None,
             "scope": "failure_recovery_record_only_not_success_route_restore"}
    index = recovery.get("accepted_evidence")
    if index is not None:
        accepted = records[index]
        value["confirmation"] = ("agent-observed-recorded-not-auto-verified" if matches(accepted, work)
                                 else "accepted-evidence-changed-or-unavailable")
        value["business_step_at_observation"] = accepted["business_step"]
    elif records:
        value["confirmation"] = "awaiting-agent-observation"
        value["business_step_at_observation"] = records[-1]["business_step"]
    return value


def result(state: dict | None, work: Path | None, *, errors=(), **extra) -> dict:
    step = current_step(state, work) if state is not None else None
    value = {"ok": not bool(errors), "current_step": step,
             "next_action": ACTIONS.get(step, "Initialize or review the session errors."),
             "errors": list(errors), "business_success": "not_auto_verified",
             "evidence_notice": "File checks prove local integrity only; agent observations are caller assertions."}
    if state is not None:
        value.update(workflow_complete=step == "complete", goal=state["goal"],
                     route=state["route"], installation_status="not_checked_by_helper",
                     steps=[{"step": item, "status": state["steps"][item]["status"],
                             "evidence_count": len(state["steps"][item]["evidence"])} for item in STEPS])
        value.update(failure_recovery=recovery_status(state, work),
                     success_route_restore=state["steps"]["restore"]["status"],
                     pending_reason=state["steps"][step].get("pending_reason") if step in STEPS else None)
    value.update(extra)
    return value


def save(state: dict, work: Path) -> None:
    state["updated_utc"] = now()
    atomic_json(work / STATE_FILE, state)


def init_session(options, work: Path) -> dict:
    external_work_dir(work)
    # Restrict labels instead of storing arbitrary URLs, bodies, or auth strings.
    label = options.target.strip()
    if (not 1 <= len(label) <= 100 or not all(
            char.isalnum() or char in " ._()-" for char in label)
            or re.search(r"(?i)\bbearer\s+\S+", label)):
        raise SessionError("target_must_be_a_short_nonsecret_label_not_a_url_or_body")
    if options.require_physical and options.route not in {"physical", "existing-capture"}:
        raise SessionError("physical_device_requirement_cannot_use_other_route")
    if options.route == "existing-capture":
        if (not options.capture_file or not options.capture_origin
                or options.capture_applicability != "target-and-goal-reviewed"):
            raise SessionError("existing_capture_requires_file_origin_and_applicability_review")
        if options.require_physical and options.capture_origin != "physical":
            raise SessionError("physical_requirement_conflicts_with_capture_origin")
    elif options.capture_file or options.capture_origin or options.capture_applicability:
        raise SessionError("capture_import_options_require_existing_capture_route")
    work.mkdir(parents=True, exist_ok=True)
    with locked(work):
        if (work / STATE_FILE).exists():
            raise SessionError("session_exists_use_status_do_not_reinitialize")
        state = {"version": 1, "created_utc": now(), "target_label": label,
                 "goal": options.goal, "route": options.route,
                 "require_physical": options.require_physical, "batch_requested": options.batch,
                 "steps": {step: {"status": "pending", "evidence": []} for step in STEPS},
                 "history": [], "output_validation": None, "final_output_validation": None,
                 "failure_recovery": {"evidence": [], "accepted_evidence": None}}
        if not options.batch:
            state["steps"]["batch"].update(status="skipped", reason="batch_not_requested")
        if options.goal == "requests":
            state["steps"]["mapping"].update(status="skipped", reason="request_summary_needs_no_product_mapping")
        if options.route == "existing-capture":
            evidence = file_evidence(options.capture_file, work, "file-validated")
            state["existing_capture"] = {"evidence": evidence, "origin": options.capture_origin,
                "applicability": options.capture_applicability,
                "applicability_kind": "agent-observed", "applicability_is_caller_assertion": True}
            for step in STEPS[1:6]:
                state["steps"][step].update(status="skipped", reason="existing_capture_reviewed_for_target_and_goal")
        state["history"].append({"command": "init", "utc": now()})
        save(state, work)
    return result(state, work)


def require_current(state: dict, work: Path, step: str) -> None:
    if step != current_step(state, work):
        raise SessionError("step_out_of_order")
    imported = state.get("existing_capture")
    if imported and not matches(imported["evidence"], work):
        raise SessionError("existing_capture_changed_or_unavailable")


def invalidate_after(state: dict, step: str) -> None:
    # Re-observation after evidence changed retains every previous evidence record,
    # but dependent stages must be observed again.
    for downstream in STEPS[STEPS.index(step) + 1:]:
        entry = state["steps"][downstream]
        if entry["status"] != "skipped":
            entry["status"] = "pending"
            entry.pop("accepted_evidence", None)
    if STEPS.index(step) < STEPS.index("sample_output"):
        state["output_validation"] = None
    if STEPS.index(step) <= STEPS.index("sample_output"):
        state["final_output_validation"] = None


def pass_step(options, state: dict, work: Path) -> dict:
    require_current(state, work, options.step)
    evidence = file_evidence(options.evidence_file, work, options.evidence_kind)
    entry = state["steps"][options.step]
    entry["evidence"].append(evidence)
    state["history"].append({"command": "pass", "step": options.step,
                             "kind": options.evidence_kind, "utc": now()})
    if options.evidence_kind != "agent-observed":
        save(state, work)
        return result(state, work, errors=["agent_observation_required_to_advance"],
                      next_action="Verify the evidence with actual observation; user reports and file existence do not complete this step.")
    if options.step == "sample_output":
        validation = state.get("output_validation")
        if not validation or not matches(validation["evidence"], work):
            save(state, work)
            return result(state, work, errors=["validate_real_output_before_source_spot_check"])
    if options.step == "deliver":
        error = delivery_error(state, work)
        if error:
            save(state, work)
            return result(state, work, errors=[error],
                          next_action="Validate the actual final output at batch or deliver, then observe the delivery evidence.")
    invalidate_after(state, options.step)
    entry.update(status="recorded", accepted_evidence=len(entry["evidence"]) - 1)
    entry.pop("pending_reason", None)
    if options.step == "sample_output":
        state["output_validation"]["source_spot_check"] = "agent-observed-not-auto-verified"
    if options.step == "deliver" and state.get("final_output_validation"):
        state["final_output_validation"]["source_spot_check"] = "agent-observed-not-auto-verified"
    save(state, work)
    return result(state, work)


def pending_step(options, state: dict, work: Path) -> dict:
    # A missing imported capture is itself a reason to record a pending failure;
    # acknowledging a blocker does not advance any dependency.
    if options.step != current_step(state, work):
        raise SessionError("step_out_of_order")
    entry = state["steps"][options.step]
    entry.update(status="pending", pending_reason=options.reason)
    state["history"].append({"command": "pending", "step": options.step,
                             "reason": options.reason, "utc": now()})
    save(state, work)
    return result(state, work, pending_reason=options.reason,
                  next_action="Resolve the recorded pending condition, then verify actual evidence. " + ACTIONS[options.step])


def record_recovery(options, state: dict, work: Path) -> dict:
    step = current_step(state, work)
    if (step in {"complete", "restore"} or state["steps"][step]["status"] != "pending"
            or not state["steps"][step].get("pending_reason")):
        raise SessionError("recovery_requires_explicitly_pending_business_stage")
    evidence = file_evidence(options.evidence_file, work, options.evidence_kind)
    evidence.update(business_step=step, pending_reason=state["steps"][step]["pending_reason"])
    recovery = state.setdefault("failure_recovery", {"evidence": [], "accepted_evidence": None})
    recovery["evidence"].append(evidence)
    state["history"].append({"command": "recovery", "business_step": step,
                             "kind": options.evidence_kind, "utc": now()})
    if options.evidence_kind != "agent-observed":
        save(state, work)
        return result(state, work, errors=["agent_observation_required_to_confirm_failure_recovery"],
                      next_action="Verify recovery of this task's changes with observation; the original business stage remains pending.")
    recovery["accepted_evidence"] = len(recovery["evidence"]) - 1
    save(state, work)
    return result(state, work,
                  next_action="Failure recovery observation is recorded. Resolve the original pending business stage; no workflow stage was advanced.")


def set_route(options, state: dict, work: Path) -> dict:
    if state["require_physical"] and options.route != "physical":
        raise SessionError("physical_device_requirement_cannot_use_other_route")
    if state["route"] == "existing-capture":
        raise SessionError("existing_capture_route_change_requires_new_session")
    if state["route"] not in {"physical", "emulator"}:
        raise SessionError("route_change_requires_a_physical_or_emulator_session")
    current = current_step(state, work)
    capture_index = STEPS.index("real_flow")
    if (current == "complete" or STEPS.index(current) >= capture_index
            or any(state["steps"][step]["evidence"] for step in STEPS[capture_index:])):
        raise SessionError("capture_stage_started_create_a_new_session_for_another_route")
    if state["route"] == options.route:
        raise SessionError("route_is_already_selected")
    evidence = file_evidence(options.evidence_file, work, "agent-observed")
    previous = state["route"]
    state["route"] = options.route
    # The history is append-only. Recheck the route and every dependent stage;
    # target/host tools and all previous evidence records are preserved.
    invalidate_after(state, "host_tools")
    state["history"].append({"command": "set-route", "from_route": previous,
                             "to_route": options.route, "reason": options.reason,
                             "evidence": evidence, "utc": now()})
    save(state, work)
    return result(state, work, route_decision_kind="agent-observed-caller-assertion")


def inspect_url(value, allow_signed: bool, *, image_resource=True, redacted_summary=False) -> bool:
    if not isinstance(value, str) or value != value.strip() or any(ord(c) < 32 for c in value):
        raise SessionError("output_contains_invalid_url")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise SessionError("output_contains_invalid_url")
        if parsed.username is not None or parsed.password is not None:
            raise SessionError("output_url_contains_credentials")
        parsed.port  # Validate malformed ports without echoing the URL.
        hostname = parsed.hostname.lower().rstrip(".")
        if (hostname in {"example.com", "example.net", "example.org", "localhost"}
                or any(hostname.endswith("." + suffix) for suffix in (
                    "example.com", "example.net", "example.org", "test", "invalid", "example", "localhost"))):
            raise SessionError("output_contains_example_or_local_url")
        segments = set(unquote(parsed.path).lower().split("/"))
        if image_resource and segments & {"auth", "login", "logout", "oauth", "authorize", "token", "session", "account", "accounts"}:
            raise SessionError("account_or_auth_url_is_not_a_deliverable_image")
        if parsed.fragment:
            raise SessionError("output_url_fragment_requires_review")
        signed = False
        for key, item in parse_qsl(parsed.query, keep_blank_values=True):
            if redacted_summary and item in {"<redacted>", ""}:
                continue
            if resource_signature_key(key):
                signed = True
            elif sensitive_key(key):
                raise SessionError("output_url_contains_sensitive_query")
        if signed and not allow_signed:
            raise SessionError("signed_image_url_requires_explicit_resource_review")
        return signed
    except ValueError as exc:
        raise SessionError("output_contains_invalid_url") from exc


def inspect_metadata(value, allow_signed: bool, *, image_resource=False) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if sensitive_key(str(key)):
                raise SessionError("output_metadata_contains_sensitive_field")
            inspect_metadata(item, allow_signed, image_resource=image_resource)
    elif isinstance(value, list):
        for item in value:
            inspect_metadata(item, allow_signed, image_resource=image_resource)
    elif isinstance(value, str):
        if value.startswith(("http://", "https://")):
            inspect_url(value, allow_signed, image_resource=image_resource, redacted_summary=True)
            return
        if re.search(r"(?i)\b(?:bearer\s+\S+|(?:password|token|cookie|authorization|secret)\s*[:=]\s*\S+)", value):
            raise SessionError("output_metadata_contains_credential_text")


def output_stats(path: Path, goal: str, allow_signed: bool) -> dict:
    try:
        rows = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise SessionError("output_is_not_a_readable_json_array") from exc
    if not isinstance(rows, list) or not rows:
        raise SessionError("output_requires_a_nonempty_array_not_example_or_empty_success")
    urls = set()
    for row in rows:
        if not isinstance(row, dict):
            raise SessionError("output_rows_must_be_objects")
        if goal in {"products", "requests"}:
            if not isinstance(row.get("name"), str) or not row["name"].strip():
                raise SessionError("output_row_requires_real_name")
            signed = inspect_url(row.get("url"), allow_signed and goal == "products",
                                 image_resource=goal == "products", redacted_summary=goal == "requests")
            if signed and row.get("expiry_status") != "not_checked":
                raise SessionError("signed_image_row_requires_expiry_status_not_checked")
            urls.add(row["url"])
        if goal == "requests" and not {"method", "status", "request_id", "response_summary"} <= set(row):
            raise SessionError("request_row_missing_required_summary_fields")
        inspect_metadata(row, allow_signed and goal == "products")
    return {"rows": len(rows), "unique_urls": len(urls), "relationships_preserved": True,
            "name_url_authenticity": "requires_agent_source_spot_check",
            "url_accessibility": "not_checked_offline", "coverage": "not_inferred_from_count"}


def preview_products(products: Path, stats: dict) -> dict:
    source = Path(__file__).resolve().with_name("download_images.py")
    destination = products.resolve().with_name("download_images.py")
    if not source.is_file():
        raise SessionError("bundled_downloader_missing")
    if destination.exists():
        if not destination.is_file() or digest(destination) != digest(source):
            raise SessionError("existing_downloader_differs_do_not_overwrite")
    else:
        with source.open("rb") as src, destination.open("xb") as dest:
            shutil.copyfileobj(src, dest)
    try:
        # Default preview is specified by the bundled downloader. Never pass
        # --download. Capture its output so no URLs or values reach helper stdout.
        preview = subprocess.run([sys.executable, "-I", str(destination), str(products.resolve())],
                                 capture_output=True, text=True, timeout=30,
                                 cwd=str(products.resolve().parent))
        summary = json.loads(preview.stdout.splitlines()[0])
        if (preview.returncode != 0 or summary.get("mode") != "preview"
                or summary.get("unique_urls") != stats["unique_urls"]):
            raise SessionError("downloader_preview_failed")
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as exc:
        raise SessionError("downloader_preview_failed") from exc
    return {"mode": "preview", "network": "not_requested", "image_writes": "not_requested",
            "downloader_sha256": digest(destination)}


def validate_output(options, state: dict, work: Path) -> dict:
    step = current_step(state, work)
    if step not in {"sample_output", "batch", "deliver"}:
        raise SessionError("step_out_of_order")
    require_current(state, work, step)
    goal = next(item for item in ("products", "requests", "data") if getattr(options, item) is not None)
    if goal != state["goal"]:
        raise SessionError("output_type_does_not_match_session_goal")
    path = getattr(options, goal).resolve()
    evidence = file_evidence(path, work, "file-validated")
    stats = output_stats(path, goal, options.allow_signed_image_urls)
    preview = preview_products(path, stats) if goal == "products" else {"mode": "not_applicable"}
    role = "sample" if step == "sample_output" else "final"
    validation = {"evidence": evidence, "statistics": stats,
                  "preview": preview, "source_spot_check": "pending"}
    if role == "sample":
        # A replacement structural check cannot reuse an old source spot check.
        invalidate_after(state, "sample_output")
        entry = state["steps"]["sample_output"]
        entry["status"] = "pending"
        entry.pop("accepted_evidence", None)
        state["output_validation"] = validation
    else:
        state["final_output_validation"] = validation
        if step == "deliver":
            # Revalidating a changed delivered file must still require a fresh
            # delivery observation and a fresh restore observation afterwards.
            invalidate_after(state, "deliver")
            entry = state["steps"]["deliver"]
            entry["status"] = "pending"
            entry.pop("accepted_evidence", None)
    state["history"].append({"command": "validate-output", "kind": "file-validated",
                             "output_role": role, "evidence": evidence, "utc": now()})
    save(state, work)
    return result(state, work, validation_scope="local_structure_and_default_preview_only",
                  statistics=stats, preview=preview, output_role=role,
                  next_action="Spot-check names, URLs, business status, and scope against real complete evidence; pass " + step + " with agent-observed evidence.")


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally repeats invalid argument values, potentially secrets.
        raise SessionError("invalid_arguments_use_command_help")


def parser() -> argparse.ArgumentParser:
    top = JsonParser(description=__doc__)
    commands = top.add_subparsers(dest="command", required=True, parser_class=JsonParser)
    init = commands.add_parser("init", help="Create a new evidence session without overwriting an old one")
    init.add_argument("--work-dir", required=True, type=Path)
    init.add_argument("--target", required=True, help="Short nonsecret App/site label, not a URL or response")
    init.add_argument("--goal", required=True, choices=("products", "requests", "data"))
    init.add_argument("--route", required=True, choices=("physical", "emulator", "browser", "existing-capture"))
    init.add_argument("--require-physical", action="store_true", help="Preserve the user's real-device requirement")
    init.add_argument("--batch", action="store_true", help="Require bounded batch/coverage evidence")
    init.add_argument("--capture-file", type=Path, help="Real existing capture; its bytes are never stored in state")
    init.add_argument("--capture-origin", choices=("physical", "emulator", "browser"))
    init.add_argument("--capture-applicability", choices=("target-and-goal-reviewed",), help="Caller observation of applicability, not an automatic HAR check")
    commands.add_parser("status", help="Read current stage and evidence integrity without changing records").add_argument("--work-dir", required=True, type=Path)
    passed = commands.add_parser("pass", help="Record real local evidence; only explicit agent observation advances")
    passed.add_argument("--work-dir", required=True, type=Path)
    passed.add_argument("--step", required=True, choices=STEPS)
    passed.add_argument("--evidence-file", required=True, type=Path)
    passed.add_argument("--evidence-kind", required=True, choices=KINDS)
    pending = commands.add_parser("pending", help="Keep the current stage pending with a fixed nonsecret reason")
    pending.add_argument("--work-dir", required=True, type=Path)
    pending.add_argument("--step", required=True, choices=STEPS)
    pending.add_argument("--reason", required=True, choices=REASONS)
    changed = commands.add_parser("set-route", help="Record a device-route fallback before real_flow; preserve old evidence")
    changed.add_argument("--work-dir", required=True, type=Path)
    changed.add_argument("--route", required=True, choices=("physical", "emulator"))
    changed.add_argument("--reason", required=True, choices=ROUTE_REASONS)
    changed.add_argument("--evidence-file", required=True, type=Path)
    recovery = commands.add_parser("recovery", help="Record restoration after an explicitly pending failure without advancing the workflow")
    recovery.add_argument("--work-dir", required=True, type=Path)
    recovery.add_argument("--evidence-file", required=True, type=Path)
    recovery.add_argument("--evidence-kind", required=True, choices=KINDS)
    validated = commands.add_parser("validate-output", help="Validate sample at sample_output or final output at batch/deliver; truth still needs a source spot check")
    validated.add_argument("--work-dir", required=True, type=Path)
    outputs = validated.add_mutually_exclusive_group(required=True)
    for goal in ("products", "requests", "data"):
        outputs.add_argument("--" + goal, type=Path)
    validated.add_argument("--allow-signed-image-urls", action="store_true", help="Only after the agent confirms resource signatures; never allows account/auth credentials")
    return top


def main(argv=None) -> int:
    state = work = None
    try:
        options = parser().parse_args(argv)
        work = options.work_dir.resolve()
        external_work_dir(work)
        if options.command == "init":
            response = init_session(options, work)
        elif options.command == "status":
            state = load_state(work)
            response = result(state, work, errors=state_errors(state, work))
        else:
            with locked(work):
                state = load_state(work)
                if options.command == "pass":
                    response = pass_step(options, state, work)
                elif options.command == "pending":
                    response = pending_step(options, state, work)
                elif options.command == "set-route":
                    response = set_route(options, state, work)
                elif options.command == "recovery":
                    response = record_recovery(options, state, work)
                else:
                    response = validate_output(options, state, work)
    except SessionError as exc:
        try:
            response = result(state, work, errors=[str(exc)])
        except (SessionError, KeyError, IndexError, TypeError):
            response = result(None, None, errors=["invalid_session_state"])
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        response = result(None, None, errors=["local_operation_failed_no_input_values_exported"])
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
