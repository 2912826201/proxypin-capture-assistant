#!/usr/bin/env python3
"""Export existing ProxyPin captures, with an optional evidence-based JSON mapping.

Python 3.10+ standard library only. No request replay, downloads, or capture-setting
changes. Without --mapping, response values and headers are never written out.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Embeddable/isolated Python does not automatically expose sibling scripts.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from proxypin_mcp import DEFAULT_ENDPOINT, MCPError, ProxyPinMCP, read_token, _flow_time_ms, body_chunk_limit

SENSITIVE = re.compile(r"(^|[_-])(authorization|cookie|token|secret|password|passwd|credential|session|api[_-]?key|signature|sign|device[_-]?id|android[_-]?id|imei|imsi)([_-]|$)", re.I)
PATH_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
RESOURCE_SIGNATURE_KEYS = frozenset({"signature", "sign", "sig", "expires", "expire", "expiry", "expiration",
    "x-amz-signature", "x-amz-date", "x-amz-expires", "x-amz-algorithm", "x-amz-signedheaders",
    "x-oss-signature", "x-oss-date", "x-oss-expires", "x-oss-signature-version", "x-oss-additional-headers"})


def resource_signature_key(value: str) -> bool:
    return value.lower().replace("_", "-") in RESOURCE_SIGNATURE_KEYS


def sensitive_key(value: str) -> bool:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return bool(SENSITIVE.search(normalized)) or value.lower().endswith("accesskeyid") or value.lower() in {"auth", "auth_key", "authkey", "bearer", "jwt", "key", "sig", "accesskey", "access_token", "accesstoken", "refreshtoken", "sessionid", "deviceid", "openid", "unionid", "uid", "userid", "user_id", "email", "phone"}


def safe_url(value: str) -> str:
    """Export request metadata without URL credentials or query values."""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if parsed.scheme not in ("http", "https") or not hostname:
            return "<non-http-url>"
        host = "[" + hostname + "]" if ":" in hostname else hostname
        if parsed.port:
            host += ":" + str(parsed.port)
        query = [(key, "<redacted>" if val else "") for key, val in parse_qsl(parsed.query, keep_blank_values=True)
                 if not sensitive_key(key) and not resource_signature_key(key)]
        return urlunsplit((parsed.scheme, host, parsed.path, urlencode(query), ""))
    except ValueError:
        return "<invalid-url>"


def path_parts(expression: Any, allow_wildcard: bool = True) -> list[str]:
    if not isinstance(expression, str) or not expression or len(expression) > 512:
        raise MCPError("Mapping paths must be short JSON dot paths, without literals or code.")
    expression = expression[2:] if expression.startswith("$.") else expression
    if expression == "$":
        return []
    parts = expression.split(".")
    if len(parts) > 24 or any(part != "*" and not PATH_KEY.fullmatch(part) for part in parts):
        raise MCPError("Only JSON object keys and .* array steps are supported in mapping paths.")
    if not allow_wildcard and "*" in parts:
        raise MCPError("Name, ID, and decoder paths must identify one scalar value; wildcards are not supported there.")
    if any(sensitive_key(part) or part.lower() in {"headers", "header", "avatar", "avatars", "advertisement", "advertisements", "ads", "adverts"} for part in parts):
        raise MCPError("Mapping paths must not select credentials, headers, avatars, or advertisements.")
    return parts


def values_at(value: Any, expression: str) -> list[Any]:
    current = [value]
    for part in path_parts(expression):
        result = []
        for item in current:
            if part == "*" and isinstance(item, list):
                result.extend(item)
            elif part != "*" and isinstance(item, dict) and part in item:
                result.append(item[part])
        current = result
    return current


def scalar_at(value: Any, expression: str | None) -> Any:
    if expression is None:
        return None
    values = values_at(value, expression)
    return values[0] if len(values) == 1 else None


def validate_mapping(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, dict) or set(profile) != {"version", "rules"} or type(profile["version"]) is not int or profile["version"] != 1:
        raise MCPError("Mapping must contain only version:1 and rules; credentials and header values are not supported.")
    rules = profile["rules"]
    if not isinstance(rules, list) or not 1 <= len(rules) <= 50:
        raise MCPError("Mapping rules must be a nonempty array of at most 50 observed-response rules.")
    for rule in rules:
        required = {"match", "decode", "records", "fields"}
        if not isinstance(rule, dict) or not required <= set(rule) or set(rule) - required - {"allow_signed_image_urls"}:
            raise MCPError("Each mapping rule requires match, decode, records, and fields, with an optional allow_signed_image_urls boolean.")
        if "allow_signed_image_urls" in rule and type(rule["allow_signed_image_urls"]) is not bool:
            raise MCPError("allow_signed_image_urls must be a boolean set by the agent after confirming resource signatures.")
        match = rule["match"]
        if not isinstance(match, dict) or set(match) != {"host", "path"} or not isinstance(match["host"], str) or not re.fullmatch(r"[A-Za-z0-9.-]+", match["host"]):
            raise MCPError("Mapping match must specify an exact observed host and URL path.")
        if not isinstance(match["path"], str) or not match["path"].startswith("/") or any(char in match["path"] for char in "?#\r\n"):
            raise MCPError("Mapping URL path must contain no query, fragment, credentials, or newline.")
        path_parts(rule["records"])
        fields = rule["fields"]
        if not isinstance(fields, dict) or not {"name", "url"} <= set(fields) or set(fields) - {"name", "url", "id"}:
            raise MCPError("Mapping fields require name and url paths, with an optional id path; literal values are not supported.")
        for key, expression in fields.items():
            path_parts(expression, allow_wildcard=key == "url")
        decode = rule["decode"]
        if not isinstance(decode, dict) or decode.get("type") not in ("json", "base64_json"):
            raise MCPError("Mapping decode.type must be json or explicitly confirmed base64_json.")
        allowed = {"type", "success"} if decode["type"] == "json" else {"type", "path", "confirmed", "success"}
        if set(decode) - allowed:
            raise MCPError("Decoder contains unrecognized options; secrets and headers are not supported.")
        if decode["type"] == "base64_json":
            if decode.get("confirmed") is not True:
                raise MCPError("Base64 JSON decoding requires confirmed:true after inspecting a real response.")
            path_parts(decode.get("path"), False)
        if "success" in decode:
            success = decode["success"]
            if not isinstance(success, dict) or set(success) != {"path", "equals"} or type(success["equals"]) not in (str, int, bool, type(None)):
                raise MCPError("Decoder success check requires a JSON path and a string, integer, boolean, or null equality.")
            if isinstance(success["equals"], str) and (len(success["equals"]) > 128 or any(ord(c) < 32 for c in success["equals"])):
                raise MCPError("Decoder success strings must be short observed business status values.")
            path_parts(success["path"], False)
    return profile


def decode_json(text: str, decoder: dict[str, Any]) -> Any:
    try:
        value = json.loads(text)
        success = decoder.get("success")
        if success:
            actual = scalar_at(value, success["path"])
            if type(actual) is not type(success["equals"]) or actual != success["equals"]:
                raise MCPError("Captured envelope did not pass the confirmed success check.")
        if decoder["type"] == "base64_json":
            encoded = scalar_at(value, decoder["path"])
            if not isinstance(encoded, str):
                raise MCPError("Confirmed Base64 envelope path did not contain a string.")
            value = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
        return value
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise MCPError("Captured JSON or confirmed Base64 JSON is incomplete or invalid.") from exc


def read_response(client: ProxyPinMCP, flow_id: str) -> tuple[str | None, dict[str, Any]]:
    """Read complete text with bounded byte slices, preserving Unicode boundaries."""
    chunks, offset, expected_total = [], 0, None
    meta: dict[str, Any] = {}
    chunk_limit = body_chunk_limit(client)
    while True:
        for adjustment in range(min(4, chunk_limit)):
            limit = chunk_limit - adjustment
            part = client.call("get_flow_body", {"id": flow_id, "side": "response", "offset": offset, "limit": limit})
            if not isinstance(part, dict):
                raise MCPError("get_flow_body returned invalid metadata.")
            if not part.get("available") or part.get("binary"):
                if offset:
                    raise MCPError("Captured body became unavailable during pagination.")
                return None, {"format": "binary" if part.get("binary") else "unavailable", "bytes": part.get("total") if type(part.get("total")) is int else None}
            returned, total, actual_offset = part.get("returned"), part.get("total"), part.get("offset")
            if any(type(number) is not int for number in (returned, total, actual_offset)):
                raise MCPError("Captured body byte metadata is invalid.")
            if total < 0 or total > client.max_body_bytes or returned < 0 or returned > limit or actual_offset != offset or offset + returned > total:
                raise MCPError("Captured body pagination is inconsistent or exceeds its size limit.")
            if expected_total is not None and total != expected_total:
                raise MCPError("Captured body size changed during pagination.")
            text = part.get("text")
            if not isinstance(text, str):
                raise MCPError("Captured text body is missing.")
            if len(text.encode("utf-8")) == returned and not (text.endswith("\ufffd") and offset + returned < total):
                break
        else:
            raise MCPError("Captured UTF-8 byte slices could not be reconstructed safely.")
        expected_total = total
        meta = {"format": "text", "bytes": total}
        chunks.append(text)
        offset += returned
        if part.get("truncated") is not (offset < total):
            raise MCPError("Captured body truncation metadata is inconsistent.")
        if offset == total:
            return "".join(chunks), meta
        if returned == 0:
            raise MCPError("Captured body pagination made no progress.")


class CaptureExporter:
    def __init__(self, client: ProxyPinMCP, mapping: dict[str, Any] | None = None):
        self.client = client
        self.mapping = validate_mapping(mapping) if mapping is not None else None
        self.requests: list[dict[str, Any]] = []
        self.products: list[dict[str, Any]] = []
        self.unresolved: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self._product_keys: set[tuple[Any, ...]] = set()
        self._mapping_flow_ids: set[str] = set()
        self.mapping_stats = {"matched_records": 0, "empty_arrays": 0}
        self.selection = {"listing_pages": 0, "stop_reason": None, "limit": None, "max_pages": None}

    @staticmethod
    def _matches(flow: dict[str, Any], url: str | None, host: str | None, since_ms: int) -> bool:
        original = flow.get("url")
        if not isinstance(original, str):
            raise MCPError("Captured flow has no valid URL.")
        if url is not None and original != url:
            return False
        if host and urlsplit(original).hostname != host.lower():
            return False
        return not since_ms or _flow_time_ms(flow.get("time")) >= since_ms

    def select(self, *, flow_ids: list[str] | None = None, url: str | None = None,
               host: str | None = None, since_ms: int = 0, limit: int = 20, max_pages: int = 10) -> list[dict[str, Any]]:
        if not 1 <= limit <= 10000 or not 1 <= max_pages <= 1000 or since_ms < 0:
            raise MCPError("Selection requires limit 1..10000, max-pages 1..1000, and nonnegative since-ms.")
        self.selection.update(limit=limit, max_pages=max_pages)
        self.client.require_tools(["get_flow_body", "get_flow_detail" if flow_ids else "list_flows"])
        if flow_ids:
            result = []
            for flow_id in list(dict.fromkeys(flow_ids))[:limit]:
                if not isinstance(flow_id, str) or not flow_id or len(flow_id) > 256 or "\n" in flow_id or "\r" in flow_id:
                    raise MCPError("Flow IDs must be the actual compact IDs from ProxyPin.")
                detail = self.client.call("get_flow_detail", {"id": flow_id, "redact": True, "preview_bytes": 0})
                if not isinstance(detail, dict) or detail.get("id") != flow_id:
                    raise MCPError("get_flow_detail did not return the requested flow.")
                response = detail.get("response") or {}
                flow = {key: detail.get(key) for key in ("id", "url", "method", "time")}
                flow["status"] = response.get("status") if isinstance(response, dict) else None
                if self._matches(flow, url, host, since_ms):
                    result.append(flow)
            self.selection["stop_reason"] = "explicit_ids_limited" if len(set(flow_ids)) > limit else "explicit_ids_read"
            return result
        selected, seen = [], set()
        page_size = min(limit, 100)
        tools = getattr(self.client, "_tools", None)
        if isinstance(tools, dict):
            maximum = tools.get("list_flows", {}).get("inputSchema", {}).get("properties", {}).get("limit", {}).get("maximum")
            if type(maximum) is int and maximum > 0:
                page_size = min(page_size, maximum)
        offset = 0
        for page in range(max_pages):
            args: dict[str, Any] = {"limit": page_size, "offset": offset}
            if host:
                args["host"] = host
            if url:
                args["keyword"] = url
            if since_ms:
                args["since_ms"] = since_ms
            result = self.client.call("list_flows", args)
            self.selection["listing_pages"] += 1
            flows = result.get("flows") if isinstance(result, dict) else None
            if not isinstance(flows, list):
                raise MCPError("list_flows returned invalid metadata.")
            if not flows:
                self.selection["stop_reason"] = "empty_page"
                break
            new_ids = 0
            for flow in flows:
                if not isinstance(flow, dict) or not isinstance(flow.get("id"), str) or not flow["id"]:
                    raise MCPError("list_flows returned an invalid flow ID.")
                if flow["id"] in seen:
                    continue
                seen.add(flow["id"])
                new_ids += 1
                if self._matches(flow, url, host, since_ms):
                    selected.append(flow)
                    if len(selected) == limit:
                        self.selection["stop_reason"] = "limit_reached"
                        return selected
            if not new_ids:
                self.selection["stop_reason"] = "repeated_flow_ids"
                break
            # Servers may cap a page below the requested limit. Advance by
            # returned rows; only an empty/repeated page proves no progress.
            offset += len(flows)
        else:
            self.selection["stop_reason"] = "max_pages_reached"
        return selected

    def _map(self, text: str, flow: dict[str, Any]) -> None:
        if self.mapping is None:
            return
        parsed = urlsplit(flow["url"])
        for rule in self.mapping["rules"]:
            if rule["match"]["host"].lower() != parsed.hostname or rule["match"]["path"] != parsed.path:
                continue
            self._mapping_flow_ids.add(flow["id"])
            data = decode_json(text, rule["decode"])
            records = values_at(data, rule["records"])
            if not records:
                current = [data]
                empty_array = False
                for part in path_parts(rule["records"]):
                    if part == "*" and current and all(isinstance(item, list) for item in current) and all(not item for item in current):
                        empty_array = True
                        break
                    current = [child for item in current for child in (item if part == "*" and isinstance(item, list) else [item[part]] if isinstance(item, dict) and part in item else [])]
                    if not current:
                        break
                if empty_array:
                    self.mapping_stats["empty_arrays"] += 1
                else:
                    self.errors.append({"request_id": flow["id"], "reason": "mapping_records_path_missing"})
                continue
            self.mapping_stats["matched_records"] += len(records)
            for record in records:
                if not isinstance(record, dict):
                    raise MCPError("Mapped records must be JSON objects observed to represent products.")
                name = scalar_at(record, rule["fields"]["name"])
                product_id = scalar_at(record, rule["fields"].get("id"))
                if product_id is not None and (isinstance(product_id, bool) or not isinstance(product_id, (str, int))):
                    product_id = None
                urls = values_at(record, rule["fields"]["url"])
                if not urls:
                    self.unresolved.append({"name": name if isinstance(name, str) else None, "url": None, "id": product_id,
                                            "source": safe_url(flow["url"]), "request_id": flow["id"], "reason": "mapped_url_missing"})
                for value in urls:
                    parsed_url = urlsplit(value) if isinstance(value, str) else None
                    query_keys = [key for key, _ in parse_qsl(parsed_url.query)] if parsed_url else []
                    signed_resource = any(resource_signature_key(key) for key in query_keys)
                    account_credentials = any(sensitive_key(key) and not resource_signature_key(key) for key in query_keys)
                    reason = None
                    if not parsed_url or parsed_url.scheme not in ("http", "https") or not parsed_url.hostname or parsed_url.username is not None or parsed_url.password is not None:
                        reason = "mapped_url_invalid"
                    elif account_credentials or (signed_resource and not rule.get("allow_signed_image_urls", False)):
                        reason = "mapped_url_has_sensitive_query"
                    elif not isinstance(name, str) or not name.strip():
                        reason = "mapped_name_missing"
                    row = {"name": name.strip() if isinstance(name, str) else None,
                           "url": safe_url(value) if isinstance(value, str) else None,
                           "source": safe_url(flow["url"]), "request_id": flow["id"]}
                    if product_id is not None:
                        row["id"] = product_id
                    if reason:
                        row["reason"] = reason
                        if reason == "mapped_url_has_sensitive_query":
                            row.update(protected_url=True, download_url_available=False,
                                       needs_url_review=True, expiry_status="not_checked")
                        self.unresolved.append(row)
                    else:
                        row["url"] = value  # An actual, explicitly mapped URL, never a guessed field.
                        if signed_resource:
                            row["expiry_status"] = "not_checked"
                        key = (name.strip(), value, str(product_id), row["source"])
                        if key not in self._product_keys:
                            self._product_keys.add(key)
                            self.products.append(row)

    def export(self, flows: list[dict[str, Any]]) -> None:
        if not flows:
            self.errors.append({"reason": "no_flows_selected"})
        for flow in flows:
            method = flow.get("method") if isinstance(flow.get("method"), str) else "UNKNOWN"
            path = urlsplit(flow["url"]).path or "/"
            request = {"name": method + " " + path, "url": safe_url(flow["url"]), "method": method,
                       "status": flow.get("status") if type(flow.get("status")) is int else None,
                       "request_id": flow["id"], "response_summary": None}
            self.requests.append(request)
            try:
                text, summary = read_response(self.client, flow["id"])
                if text is not None:
                    try:
                        decoded = json.loads(text)
                        summary["format"] = "json"
                        summary["root_type"] = "object" if isinstance(decoded, dict) else "array" if isinstance(decoded, list) else "scalar"
                        if isinstance(decoded, dict):
                            summary["top_level_fields"] = [key for key in decoded if PATH_KEY.fullmatch(key) and not sensitive_key(key)][:25]
                        elif isinstance(decoded, list):
                            summary["array_items"] = len(decoded)
                    except ValueError:
                        pass  # Text content is summarized by format and size only.
                    self._map(text, flow)
                elif self.mapping is not None and any(rule["match"]["host"].lower() == urlsplit(flow["url"]).hostname and rule["match"]["path"] == path for rule in self.mapping["rules"]):
                    raise MCPError("A matched mapping requires an available complete text response.")
                request["response_summary"] = summary
            except (MCPError, ValueError) as exc:
                request["response_summary"] = {"format": "error", "reason": self.client.safe_text(exc)}
                self.errors.append({"request_id": flow["id"], "reason": self.client.safe_text(exc)})
        if self.mapping is not None and flows and not self._mapping_flow_ids:
            self.errors.append({"reason": "mapping_did_not_match_selected_flows"})

    def write(self, output: Path) -> dict[str, Any]:
        output.mkdir(parents=True, exist_ok=True)
        report = {"generated_utc": datetime.now(timezone.utc).isoformat(), "run_complete": not self.errors,
                  "scope": "selected existing captured flows only", "coverage_is_all_app_requests": False,
                  "mapping_applied": self.mapping is not None, "selection": self.selection,
                  "product_result_ready": self.mapping is not None and bool(self.products) and not self.errors,
                  "mapping": {"matched_flows": len(self._mapping_flow_ids), **self.mapping_stats},
                  "counts": {"requests": len(self.requests), "products": len(self.products), "unresolved": len(self.unresolved),
                             "unique_product_urls": len({row["url"] for row in self.products}), "errors": len(self.errors), "mcp_rpc_calls": self.client.rpc_calls},
                  "notes": ["No business requests, images, headers, response values, or configuration changes are exported by default.",
                            "Product rows require an explicit mapping based on actual observed response fields.",
                            "Listing is bounded by limit and max-pages; it is not a claim of full-app coverage."]}
        for name, value in (("requests.json", self.requests), ("products.json", self.products),
                            ("unresolved-images.json", self.unresolved), ("errors.json", self.errors), ("report.json", report)):
            (output / name).write_text(self.client.safe_text(json.dumps(value, ensure_ascii=False, indent=2)) + "\n", encoding="utf-8")
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--no-token", action="store_true", help="Only for a trusted MCP server without authentication")
    parser.add_argument("--require-token", action="store_true", help="Require mobile Bearer authentication even through loopback USB forwarding")
    parser.add_argument("--flow-id", action="append", default=[], help="Actual captured flow ID; may be repeated")
    parser.add_argument("--url", help="Exact original captured URL; never sent as a business request")
    parser.add_argument("--host", help="Exact captured host, e.g. example.example.test")
    parser.add_argument("--since-ms", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20, help="Maximum number of captured flows exported")
    parser.add_argument("--max-pages", type=int, default=10, help="Maximum list_flows pages inspected")
    parser.add_argument("--mapping", type=Path, help="Agent-created JSON mapping from a verified response sample")
    parser.add_argument("--output-dir", type=Path, default=Path("capture-export"))
    args = parser.parse_args(argv)
    client = exporter = None
    try:
        profile = validate_mapping(json.loads(args.mapping.read_text(encoding="utf-8-sig"))) if args.mapping else None
        client = ProxyPinMCP(args.endpoint, read_token(args.no_token, args.endpoint, args.require_token), args.timeout)
        exporter = CaptureExporter(client, profile)
        selected = exporter.select(flow_ids=args.flow_id, url=args.url, host=args.host,
                                   since_ms=args.since_ms, limit=args.limit, max_pages=args.max_pages)
        exporter.export(selected)
        report = exporter.write(args.output_dir)
        print(client.safe_text(json.dumps({"output_dir": str(args.output_dir), "counts": report["counts"], "selection": report["selection"]}, ensure_ascii=False)))
        return 2 if exporter.errors else 0
    except (MCPError, ValueError, OSError) as exc:
        message = client.safe_text(exc) if client else "Invalid mapping or local input; no captured response values were exported."
        if exporter:
            exporter.errors.append({"reason": message})
            exporter.write(args.output_dir)
        print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
