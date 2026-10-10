#!/usr/bin/env python3
"""Preview or download an explicit image URL list using Python 3.10+.

Windows, macOS and Linux; standard library only. No credentials, API discovery,
URL rewriting or automatic download. Examples:
  python download_images.py products.json
  python download_images.py products.json --download --output ./images
  python download_images.py --download --workers 4 --limit 100

With no positional input, products.json beside this script is used. With no
--output, downloaded-images beside this script is used. Preview is the default:
only an explicit --download performs network requests or creates output files.
Flat JSON product records retain all fields other than url in the manifest's
mappings. Different products sharing a URL keep separate mappings and download
one file. URL strings and the older nested mappings format are also accepted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import ssl
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def checked_url(value) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("Every image URL must be a string without surrounding whitespace")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Expected an absolute HTTP(S) URL: {value!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs containing embedded credentials are not accepted")
    if any(ord(character) < 32 for character in value):
        raise ValueError("URL contains a control character")
    return value


def load_input(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        rows = json.loads(text)
        if isinstance(rows, dict):
            rows = rows.get("images", rows.get("records"))
        if not isinstance(rows, list):
            raise ValueError("JSON input must be an array of URL strings or objects with a url field")
    else:
        rows = [line.strip() for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
    images = {}
    for row in rows:
        if isinstance(row, str):
            url, mappings = checked_url(row), []
        elif isinstance(row, dict):
            url = checked_url(row.get("url"))
            if "mappings" in row:
                # Compatibility with the previous per-URL nested index format.
                mappings = row["mappings"]
                if not isinstance(mappings, list) or not all(isinstance(item, dict) for item in mappings):
                    raise ValueError("The mappings field must be an array of objects")
            else:
                # Flat product rows can share one URL while retaining each product.
                metadata = {key: value for key, value in row.items() if key != "url"}
                mappings = [metadata] if metadata else []
        else:
            raise ValueError("Input contains an unsupported record")
        entry = images.setdefault(url, {"url": url, "mappings": {}})
        for mapping in mappings:
            entry["mappings"][json.dumps(mapping, ensure_ascii=False, sort_keys=True)] = mapping
    return [{"url": entry["url"], "mappings": list(entry["mappings"].values())}
            for entry in images.values()]


def filename(url: str) -> str:
    suffix = Path(urlsplit(url).path).suffix
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
        suffix = ".image"
    return hashlib.sha256(url.encode("utf-8")).hexdigest() + suffix


def jpeg_dimensions(header: bytes):
    offset = 2
    while offset + 4 < len(header):
        if header[offset] != 0xFF:
            break
        while offset < len(header) and header[offset] == 0xFF:
            offset += 1
        if offset >= len(header):
            break
        marker = header[offset]
        offset += 1
        if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(header):
            break
        length = int.from_bytes(header[offset:offset + 2], "big")
        if length < 2:
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and offset + 7 <= len(header):
            return (int.from_bytes(header[offset + 5:offset + 7], "big"),
                    int.from_bytes(header[offset + 3:offset + 5], "big"))
        offset += length
    return None, None


def image_metadata(path: Path) -> dict:
    """Check common raster magic/header and hash all bytes, without executing content.

    This is a structural header check, not a full image pixel-decoding test.
    PNG/JPEG/GIF also need their expected end markers; no third-party packages.
    """
    byte_count = path.stat().st_size
    if byte_count < 8:
        raise ValueError("Response is too short to be an image")
    with path.open("rb") as stream:
        header = stream.read(65536)
        stream.seek(max(0, byte_count - 16))
        tail = stream.read()
    width = height = None
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(header) < 33 or header[8:16] != b"\x00\x00\x00\x0dIHDR" or tail[-12:-4] != b"\x00\x00\x00\x00IEND":
            raise ValueError("Incomplete PNG header or end marker")
        image_format = "PNG"
        width, height = struct.unpack(">II", header[16:24])
    elif header.startswith(b"\xff\xd8\xff"):
        if not tail.endswith(b"\xff\xd9"):
            raise ValueError("Incomplete JPEG end marker")
        image_format = "JPEG"
        width, height = jpeg_dimensions(header)
    elif header.startswith((b"GIF87a", b"GIF89a")):
        if len(header) < 13 or not tail.endswith(b";"):
            raise ValueError("Incomplete GIF header or end marker")
        image_format = "GIF"
        width, height = struct.unpack("<HH", header[6:10])
    elif header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        if len(header) < 20 or int.from_bytes(header[4:8], "little") + 8 != byte_count:
            raise ValueError("Incomplete WebP container")
        image_format = "WEBP"
        if header[12:16] == b"VP8X" and len(header) >= 30:
            width = 1 + int.from_bytes(header[24:27], "little")
            height = 1 + int.from_bytes(header[27:30], "little")
    elif header.startswith(b"BM") and len(header) >= 26:
        if int.from_bytes(header[2:6], "little") != byte_count:
            raise ValueError("Incomplete BMP container")
        image_format = "BMP"
        if int.from_bytes(header[14:18], "little") >= 40:
            width, height = struct.unpack("<ii", header[18:26])
            height = abs(height)
    elif header.startswith((b"II*\x00", b"MM\x00*")):
        image_format = "TIFF"
        order = "little" if header[:2] == b"II" else "big"
        if not 8 <= int.from_bytes(header[4:8], order) < byte_count:
            raise ValueError("Invalid TIFF directory offset")
    elif len(header) >= 16 and header[4:8] == b"ftyp" and (b"avif" in header[8:64] or b"avis" in header[8:64]):
        image_format = "AVIF"
        if int.from_bytes(header[:4], "big") > byte_count:
            raise ValueError("Incomplete AVIF header")
    else:
        raise ValueError("Unsupported image header; response may be HTML, JSON or a non-raster file")
    if width is not None and (width <= 0 or height is None or height <= 0):
        raise ValueError("Invalid image dimensions")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"format": image_format, "width": width, "height": height,
            "bytes": byte_count, "sha256": digest.hexdigest(),
            "validation": "raster magic/header and end/container checks"}


def resume_record(entry: dict, output: Path, previous: dict | None):
    if not previous or previous.get("status") != "ok" or not previous.get("sha256"):
        return None
    path = output / filename(entry["url"])
    if previous.get("file") != path.name or not path.exists():
        return None
    try:
        metadata = image_metadata(path)
    except (OSError, ValueError):
        return None
    if metadata["sha256"] != previous["sha256"] or metadata["bytes"] != previous.get("bytes"):
        return None
    return {**entry, **metadata, "file": path.name, "status": "ok", "reused": True,
            "attempts": 0, "checked_utc": utc_now()}


def download_one(entry: dict, output: Path, previous: dict | None, options) -> dict:
    resumed = resume_record(entry, output, previous)
    if resumed:
        return resumed
    path = output / filename(entry["url"])
    partial = path.with_name(path.name + ".part")
    errors = []
    for attempt in range(1, options.retry + 2):
        try:
            request = Request(entry["url"], headers={"User-Agent": "PortableImageDownloader/1.0"})
            # Use the OS/default CA trust store. There is no TLS bypass option.
            with urlopen(request, timeout=options.timeout, context=ssl.create_default_context()) as response:
                checked_url(response.geturl())
                size = 0
                with partial.open("wb") as stream:
                    while chunk := response.read(128 * 1024):
                        size += len(chunk)
                        if size > options.max_bytes:
                            raise ValueError(f"Image exceeds --max-bytes ({options.max_bytes})")
                        stream.write(chunk)
                metadata = image_metadata(partial)
                partial.replace(path)
                return {**entry, **metadata, "file": path.name, "status": "ok", "reused": False,
                        "attempts": attempt, "content_type": response.headers.get("Content-Type", ""),
                        "final_url": response.geturl(), "checked_utc": utc_now()}
        except (HTTPError, URLError, HTTPException, OSError, ValueError) as error:
            partial.unlink(missing_ok=True)
            errors.append(f"{type(error).__name__}: {error}")
            if attempt <= options.retry:
                time.sleep(min(attempt, 2))
    return {**entry, "file": path.name, "status": "failed", "reused": False,
            "attempts": options.retry + 1, "errors": errors, "checked_utc": utc_now()}


def main(argv=None) -> int:
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", nargs="?", type=Path, default=base / "products.json")
    parser.add_argument("--download", action="store_true", help="Explicitly enable image downloads")
    parser.add_argument("--output", type=Path, default=base / "downloaded-images")
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    parser.add_argument("--limit", type=int, help="Process only the first N unique URLs")
    parser.add_argument("--retry", type=int, choices=range(0, 3), default=2, help="Retries after the first attempt (0..2)")
    parser.add_argument("--timeout", type=float, default=30, help="Socket timeout in seconds")
    parser.add_argument("--max-bytes", type=int, default=128 * 1024 * 1024, help="Maximum bytes per image")
    options = parser.parse_args(argv)
    if options.limit is not None and options.limit < 1:
        parser.error("--limit must be at least 1")
    if options.timeout <= 0 or options.max_bytes < 8:
        parser.error("--timeout must be positive and --max-bytes must be at least 8")
    try:
        all_entries = load_input(options.input.resolve())
    except (OSError, ValueError) as error:
        print(f"Input error: {error}", file=sys.stderr)
        return 2
    entries = all_entries[:options.limit] if options.limit else all_entries
    output = options.output.resolve()
    print(json.dumps({"mode": "download" if options.download else "preview",
                      "input": str(options.input.resolve()), "output": str(output),
                      "unique_urls": len(all_entries), "selected_urls": len(entries),
                      "workers": options.workers, "retry": options.retry,
                      "preview_urls": [entry["url"] for entry in entries[:5]]}, ensure_ascii=False), flush=True)
    if not options.download:
        print("Preview complete. No network requests or output writes. Add --download to download.", flush=True)
        return 0
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "download-manifest.json"
    previous = {}
    if manifest_path.exists():
        try:
            previous = {row["url"]: row for row in json.loads(manifest_path.read_text(encoding="utf-8"))["records"]}
        except (OSError, ValueError, KeyError, TypeError):
            print("Previous manifest could not be read; existing files will not be blindly reused.", file=sys.stderr)
    started = utc_now()
    results = []

    def save_manifest():
        records = {**previous, **{row["url"]: row for row in results}}
        atomic_json(manifest_path, {"started_utc": started, "updated_utc": utc_now(),
                                    "input": str(options.input.resolve()),
                                    "records": [records[url] for url in sorted(records)]})

    with ThreadPoolExecutor(max_workers=options.workers) as executor:
        futures = [executor.submit(download_one, entry, output, previous.get(entry["url"]), options)
                   for entry in entries]
        for future in as_completed(futures):
            results.append(future.result())
            if len(results) % 25 == 0 or len(results) == len(entries):
                save_manifest()
                print(f"Completed {len(results)}/{len(entries)} URLs", flush=True)
    save_manifest()
    failures = [row for row in results if row["status"] == "failed"]
    atomic_json(output / "download-errors.json", failures)
    summary = {"selected_urls": len(entries), "successful_urls": len(results) - len(failures),
               "reused_files": sum(row.get("reused", False) for row in results),
               "failed_urls": len(failures), "manifest": str(manifest_path)}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
