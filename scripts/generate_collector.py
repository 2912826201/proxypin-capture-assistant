#!/usr/bin/env python3
"""Bundle a reviewed target adapter after an agent-verified live sample.

No installation, device commands, network traffic, login, or signing occurs here.
An evidence report records the agent's observation; this factory cannot prove it.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("collector_runtime_template", ROOT / "assets" / "collector_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
CollectorError = runtime.CollectorError


def validate_adapter(path):
    if not path.is_file() or path.suffix != ".py" or not 1 <= path.stat().st_size <= 256 * 1024:
        raise CollectorError("reviewed_python_adapter_required")
    try:
        text = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(text)
    except (SyntaxError, UnicodeError):
        raise CollectorError("invalid_adapter_python") from None
    entries = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run"]
    if len(entries) != 1 or len(entries[0].args.args) != 1 or entries[0].args.vararg or entries[0].args.kwarg:
        raise CollectorError("adapter_must_export_run_context")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [entry.name for entry in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            allowed = sys.stdlib_module_names | {"proxypin_mcp", "export_capture"}
            if any(module.split(".")[0] not in allowed for module in modules) or (isinstance(node, ast.ImportFrom) and node.level):
                raise CollectorError("adapter_import_requires_unbundled_dependency")
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and runtime.SENSITIVE.search(target.id) and not target.id.lower().endswith(("_env", "_environment")):
                    raise CollectorError("embedded_credential_in_adapter")
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and isinstance(key.value, str) and runtime.SENSITIVE.search(key.value)
                        and isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value):
                    raise CollectorError("embedded_credential_in_adapter")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if re.search(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}|(?:access_token|password|cookie|authorization)\s*[=:]\s*[^\s'\"]{8,}", value):
                raise CollectorError("embedded_credential_in_adapter")
            if value.startswith(("http://", "https://")):
                runtime.check_metadata(value)
    return text


def validate_evidence(report, config):
    required = {"version", "target", "strategy", "source", "status", "sample_count", "observed_utc", "checks"}
    if not isinstance(report, dict) or set(report) != required or type(report["version"]) is not int or report["version"] != 1:
        raise CollectorError("invalid_live_evidence_report")
    if (report["target"] != config["target"] or report["strategy"] != config["strategy"]
            or report["source"] not in {"live_capture", "direct_replay"} or report["status"] != "agent_verified"
            or type(report["sample_count"]) is not int or report["sample_count"] < 1):
        raise CollectorError("agent_verified_nonempty_live_sample_required")
    checks = report["checks"]
    if not isinstance(checks, dict) or set(checks) != {"full_body", "business_success", "mapping", "readonly"} or any(value is not True for value in checks.values()):
        raise CollectorError("live_sample_checks_incomplete")
    if config["strategy"] == "direct_http" and report["source"] != "direct_replay":
        raise CollectorError("direct_http_requires_verified_replay")
    try:
        stamp = datetime.fromisoformat(report["observed_utc"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise CollectorError("timezone_aware_observation_time_required") from None


def generate(adapter_path, config_path, evidence_path, bundle_dir, overwrite=False):
    if not bundle_dir.is_absolute():
        raise CollectorError("absolute_bundle_directory_required")
    destination = bundle_dir.resolve()
    if destination.is_relative_to(ROOT):
        raise CollectorError("bundle_must_be_outside_skill_repository")
    text = validate_adapter(adapter_path)
    config = runtime.validate_config(json.loads(config_path.read_text(encoding="utf-8-sig")), False)
    if "verification" in config:
        raise CollectorError("verification_is_factory_generated_from_live_report")
    raw_report = evidence_path.read_bytes()
    report = json.loads(raw_report.decode("utf-8-sig"))
    validate_evidence(report, config)
    config["verification"] = {"status": report["status"], "source": report["source"],
                              "evidence_sha256": hashlib.sha256(raw_report).hexdigest(), "observed_utc": report["observed_utc"]}
    runtime.validate_config(config)
    files = {"collect_data.py": (ROOT / "assets" / "collector_runtime.py").read_bytes(),
             "target_adapter.py": text.encode("utf-8"), "collector_config.json": runtime.encoded(config),
             "proxypin_mcp.py": (ROOT / "scripts" / "proxypin_mcp.py").read_bytes(),
             "export_capture.py": (ROOT / "scripts" / "export_capture.py").read_bytes()}
    if any((destination / name).exists() for name in files) and not overwrite:
        raise CollectorError("bundle_exists_use_overwrite")
    if any((destination / name).resolve() in {adapter_path.resolve(), config_path.resolve(), evidence_path.resolve()} for name in files):
        raise CollectorError("bundle_must_not_replace_input_evidence")
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        # Inputs were validated before any output is written; never import/run
        # the adapter at generation time. Existing outputs require --overwrite.
        (destination / name).write_bytes(data)
    return {"bundle_dir": str(destination), "files": list(files), "source_evidence": config["verification"],
            "verification_notice": "Agent observation recorded; factory performs offline validation only."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--evidence-report", required=True, type=Path)
    parser.add_argument("--bundle-dir", required=True, type=Path)
    parser.add_argument("--overwrite", action="store_true")
    options = parser.parse_args(argv)
    try:
        result = generate(options.adapter, options.config, options.evidence_report, options.bundle_dir, options.overwrite)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except CollectorError as error:
        print(error.code, file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError):
        print("invalid_local_factory_inputs", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
