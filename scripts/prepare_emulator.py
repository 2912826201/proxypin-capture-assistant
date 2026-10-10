#!/usr/bin/env python3
"""Prepare and control one task-owned Android emulator (Python 3.10+).

Use --config FILE --action check|discover|prepare|start|verify|stop. Configuration
contains schema_version:1, absolute task_root/sdk_root/java_home, avd_name and
system_image/device_profile (null until discover returns observed options).
Optional commandline_tools:{url,sha256} and
jdk_archive:{publisher:"adoptium",url,sha256} describe verified official archives.
Optional port and boot_timeout_seconds select a console port and boot wait budget.
Optional writable_system defaults false; enable only for an explicitly required,
compatible task-owned test instance. Stop/start/verify applies a changed value.
This helper never installs certificates, roots Android, forwards ports or changes
global environment variables. A process launch is not boot/network/business proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

SKILL_ROOT = Path(__file__).resolve().parents[1]
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
STATE_NAME = "emulator-state.json"
MARKER = "proxypin-task-emulator-v1"
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
IMAGE = re.compile(r"^system-images;android-[0-9][A-Za-z0-9_.-]*;[A-Za-z0-9_-]+;[A-Za-z0-9_-]+$")
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


class SetupError(RuntimeError):
    pass


class Pending(SetupError):
    def __init__(self, code: str, next_action: str):
        super().__init__(code)
        self.next_action = next_action


def below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def safe_path(value, label: str) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise SetupError(f"{label}_must_be_an_absolute_path")
    if any(ord(c) < 32 for c in value) or any(c in value for c in '&|<>`"%^!'):
        raise SetupError(f"{label}_contains_unsafe_characters")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SetupError(f"{label}_must_be_an_absolute_path")
    # Reparse points can make a checked lexical destination point elsewhere.
    for candidate in (path, *path.parents):
        try:
            attributes = getattr(candidate.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            continue
        if candidate.is_symlink() or attributes & 0x400:
            raise SetupError(f"{label}_contains_a_link_or_reparse_point")
    return path.resolve()


def validate_archive(spec, kind: str) -> dict:
    allowed = {"url", "sha256"} if kind == "commandline_tools" else {"publisher", "url", "sha256"}
    if not isinstance(spec, dict) or set(spec) != allowed:
        raise SetupError(f"{kind}_requires_exact_official_url_and_sha256")
    if not isinstance(spec["sha256"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", spec["sha256"]):
        raise SetupError(f"{kind}_requires_verified_upstream_sha256")
    url = spec["url"]
    if not isinstance(url, str) or any(ord(c) < 32 for c in url):
        raise SetupError(f"{kind}_invalid_url")
    parts = urlsplit(url)
    decoded_path = unquote(parts.path)
    if any(piece in (".", "..") for piece in decoded_path.split("/")) or "\\" in decoded_path or any(ord(c) < 32 for c in decoded_path):
        raise SetupError(f"{kind}_invalid_official_archive_path")
    if (parts.scheme != "https" or parts.username or parts.password or parts.query or parts.fragment
            or parts.port not in (None, 443)):
        raise SetupError(f"{kind}_requires_an_exact_official_https_url")
    if kind == "commandline_tools":
        if parts.hostname != "dl.google.com" or not re.fullmatch(
                r"/android/repository/commandlinetools-(win|mac|linux)-[0-9]+_latest\.zip", parts.path):
            raise SetupError("commandline_tools_source_is_not_official_google_archive")
        platform = "win" if os.name == "nt" else "mac" if sys.platform == "darwin" else "linux"
        if f"commandlinetools-{platform}-" not in parts.path:
            raise SetupError("commandline_tools_archive_does_not_match_host_os")
    else:
        if spec["publisher"] != "adoptium" or parts.hostname != "github.com" or not re.fullmatch(
                r"/adoptium/temurin[0-9]+-binaries/releases/download/[^/]+/OpenJDK[0-9]+U-jdk_[A-Za-z0-9_.+-]+\.(zip|tar\.gz)",
                decoded_path):
            raise SetupError("jdk_source_requires_an_exact_adoptium_temurin_jdk_release_archive")
    return spec


def load_config(path: Path) -> dict:
    if path.stat().st_size > 1024 * 1024:
        raise SetupError("config_file_too_large")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    required = {"schema_version", "task_root", "sdk_root", "java_home", "system_image", "avd_name", "device_profile"}
    optional = {"commandline_tools", "jdk_archive", "port", "boot_timeout_seconds", "writable_system"}
    if not isinstance(data, dict) or not required <= set(data) or set(data) - required - optional:
        raise SetupError("config_has_missing_or_unknown_fields")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise SetupError("unsupported_config_schema")
    for key in ("task_root", "sdk_root", "java_home"):
        data[key] = safe_path(data[key], key)
    excluded = {SKILL_ROOT}
    excluded.update(p for p in SKILL_ROOT.parents if (p / ".git").exists())
    if any(below(data["task_root"], root) for root in excluded):
        raise SetupError("task_root_must_be_outside_skill_and_repository")
    if data["task_root"] == Path(data["task_root"].anchor) or data["task_root"] == Path.home().resolve():
        raise SetupError("task_root_must_be_a_dedicated_directory")
    if data["system_image"] is not None and (not isinstance(data["system_image"], str) or not IMAGE.fullmatch(data["system_image"])):
        raise SetupError("system_image_must_be_an_observed_sdkmanager_package_id")
    for key in ("avd_name", "device_profile"):
        if key == "device_profile" and data[key] is None:
            continue
        if not isinstance(data[key], str) or not IDENTIFIER.fullmatch(data[key]) or RESERVED.match(data[key]):
            raise SetupError(f"{key}_must_be_a_safe_observed_identifier")
    if type(data.get("writable_system", False)) is not bool:
        raise SetupError("writable_system_must_be_a_boolean")
    data.setdefault("writable_system", False)
    if "port" in data and (type(data["port"]) is not int or data["port"] % 2 or not 1024 <= data["port"] <= 65534):
        raise SetupError("port_must_be_an_even_nonprivileged_console_port")
    budget = data.get("boot_timeout_seconds", 30)
    if type(budget) is not int or not 1 <= budget <= 600:
        raise SetupError("boot_timeout_seconds_must_be_1_to_600")
    data["boot_timeout_seconds"] = budget
    for key in ("commandline_tools", "jdk_archive"):
        if key in data:
            validate_archive(data[key], key)
    data["_config_path"] = path.resolve()
    return data


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_archive(spec: dict, destination: Path, kind: str) -> None:
    """Download only a validated official archive; redirects remain source-scoped."""
    validate_archive(spec, kind)
    if destination.exists():
        raise SetupError("archive_download_destination_already_exists")
    hosts = {"dl.google.com"} if kind == "commandline_tools" else {
        "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
        "github-releases.githubusercontent.com"}
    opener = build_opener(ProxyHandler({}), NoRedirect())
    url = spec["url"]
    digest = hashlib.sha256()
    created = False
    try:
        for _ in range(7):
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.hostname not in hosts or parts.username or parts.password or parts.fragment or parts.port not in (None, 443):
                raise SetupError("official_download_redirect_left_allowed_sources")
            try:
                response = opener.open(Request(url, headers={"User-Agent": "ProxyPinEmulatorSetup/1"}), timeout=45)
            except HTTPError as exc:
                if exc.code not in (301, 302, 303, 307, 308) or not exc.headers.get("Location"):
                    raise SetupError("official_archive_download_failed") from exc
                url = urljoin(url, exc.headers["Location"])
                exc.close()
                continue
            with response, destination.open("xb") as output:
                created = True
                size = 0
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise SetupError("official_archive_exceeds_size_limit")
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != spec["sha256"].lower():
                raise SetupError("official_archive_sha256_mismatch")
            return
        raise SetupError("official_archive_redirect_limit_exceeded")
    except Exception:
        if created:
            destination.unlink(missing_ok=True)
        raise


def archive_target(name: str, root: Path) -> Path:
    if not name or "\\" in name or any(ord(c) < 32 for c in name) or ":" in name:
        raise SetupError("archive_contains_unsafe_path")
    parts = PurePosixPath(name).parts
    if not parts or name.startswith("/") or any(p in (".", "..") or p.rstrip(" .") != p or RESERVED.match(p) for p in parts):
        raise SetupError("archive_contains_unsafe_path")
    target = root.joinpath(*parts).resolve()
    if not below(target, root.resolve()) or target == root.resolve():
        raise SetupError("archive_path_escapes_destination")
    return target


def extract_archive(archive: Path, destination: Path) -> None:
    """Preflight before writing; materialize safe internal file links as files.

    Official Unix JDK archives can contain file aliases. They may refer only to
    another archived regular file; external links, directory links and cycles
    are refused. No filesystem links are created or followed during extraction.
    """
    if destination.exists():
        raise SetupError("archive_destination_already_exists")
    members = []
    seen = set()
    total = 0
    with (zipfile.ZipFile(archive) if zipfile.is_zipfile(archive) else tarfile.open(archive, "r:gz")) as source:
        is_zip = isinstance(source, zipfile.ZipFile)
        entries = source.infolist() if is_zip else source.getmembers()
        if len(entries) > 100000:
            raise SetupError("archive_has_too_many_members")
        for entry in entries:
            # ZipInfo.filename can normalize separators and truncate NUL bytes.
            name = entry.orig_filename if is_zip else entry.name
            target = archive_target(name, destination)
            key = str(target).casefold()
            if key in seen:
                raise SetupError("archive_contains_duplicate_paths")
            seen.add(key)
            mode = entry.external_attr >> 16 if is_zip else entry.mode
            directory = entry.is_dir() if is_zip else entry.isdir()
            link = None
            is_link = (mode & 0o170000) == 0o120000 if is_zip else entry.issym() or entry.islnk()
            if is_link:
                if is_zip:
                    if entry.file_size > 4096:
                        raise SetupError("archive_link_target_too_long")
                    with source.open(entry) as stream:
                        link_name = stream.read(4097).decode("utf-8")
                else:
                    link_name = entry.linkname
                if (not link_name or link_name.startswith("/") or "\\" in link_name or ":" in link_name
                        or any(ord(c) < 32 for c in link_name)):
                    raise SetupError("archive_contains_unsafe_link_target")
                parent = target.parent if is_zip or entry.issym() else destination
                link = parent.joinpath(*PurePosixPath(link_name).parts).resolve()
                if not below(link, destination.resolve()) or link == destination.resolve():
                    raise SetupError("archive_link_target_escapes_destination")
            elif not is_zip and not (entry.isfile() or directory):
                raise SetupError("archive_contains_special_file")
            total += entry.file_size if is_zip else entry.size
            if total > MAX_ARCHIVE_BYTES:
                raise SetupError("archive_extracted_size_exceeds_limit")
            members.append((entry, target, directory, mode, link))
        regular = {target: entry.file_size if is_zip else entry.size
                   for entry, target, directory, _, link in members if not directory and link is None}
        links = {target: link for _, target, _, _, link in members if link is not None}

        def resolve_member(path: Path, visited: set[Path]) -> Path:
            while path not in regular:
                if path not in links or path in visited or len(visited) > 128:
                    raise SetupError("archive_link_is_external_directory_cyclic_or_too_deep")
                visited.add(path)
                path = links[path]
            return path

        aliases = {target: resolve_member(link, {target}) for target, link in links.items()}
        for _, target, _, _, _ in members:
            if any(parent in aliases for parent in target.parents):
                raise SetupError("archive_link_cannot_be_parent_of_written_member")
        if total + sum(regular[target] for target in aliases.values()) > MAX_ARCHIVE_BYTES:
            raise SetupError("archive_materialized_links_exceed_size_limit")
        destination.mkdir(parents=True)
        for entry, target, directory, mode, link in members:
            if link is not None:
                continue
            if directory:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = source.open(entry) if is_zip else source.extractfile(entry)
            if stream is None:
                raise SetupError("archive_member_cannot_be_read")
            with stream, target.open("xb") as output:
                shutil.copyfileobj(stream, output)
            if os.name != "nt" and mode & 0o111:
                target.chmod(0o755)
        for alias, original in aliases.items():
            alias.parent.mkdir(parents=True, exist_ok=True)
            with original.open("rb") as source_file, alias.open("xb") as output:
                shutil.copyfileobj(source_file, output)
            if os.name != "nt" and original.stat().st_mode & 0o111:
                alias.chmod(0o755)


def run_tool(args: list[str], env: dict, timeout: int = 45) -> subprocess.CompletedProcess:
    # All dynamic arguments have been validated. Never invoke a shell or send yes.
    try:
        return subprocess.run(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise Pending("tool_timed_out_or_requires_interaction",
                      "Agent: inspect the current tool prompt in an available local interactive terminal; proceed by its actual instructions. Ask the user only if tools or permissions cannot perform the required action.") from exc
    except OSError as exc:
        raise SetupError("verified_tool_could_not_be_executed") from exc


def table_packages(text: str) -> tuple[set[str], set[str]]:
    installed, available = set(), set()
    section = None
    for line in text.splitlines():
        heading = line.strip().lower()
        if heading.startswith("installed packages"):
            section = installed
        elif heading.startswith("available packages"):
            section = available
        elif heading.startswith("available updates"):
            section = None
        elif section is not None and "|" in line:
            package = line.split("|", 1)[0].strip()
            if package.lower() != "path" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.;-]*", package):
                section.add(package)
    return installed, available


def device_profiles(text: str) -> list[dict]:
    return [{"id": int(number), "name": name} for number, name in
            re.findall(r'(?m)^\s*id:\s*([0-9]+)\s+or\s+"([A-Za-z0-9][A-Za-z0-9_.-]{0,63})"', text)]


def read_ini(path: Path) -> dict[str, str]:
    safe_path(str(path), "avd_metadata")
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise SetupError("avd_metadata_file_missing_or_too_large")
    result = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith(("#", ";")):
            continue
        if "=" not in line:
            raise SetupError("avd_metadata_has_invalid_structure")
        key, value = (part.strip() for part in line.split("=", 1))
        if key in result or not re.fullmatch(r"[A-Za-z0-9_.-]+", key):
            raise SetupError("avd_metadata_has_duplicate_or_invalid_keys")
        result[key] = value
    return result


class EmulatorTask:
    def __init__(self, config: dict):
        self.config = config
        self.root, self.sdk, self.java = (config[key] for key in ("task_root", "sdk_root", "java_home"))
        self.state_path = self.root / STATE_NAME
        identity = {key: str(config[key]) for key in ("task_root", "sdk_root", "java_home", "avd_name")}
        self.fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        if self.state_path.exists():
            safe_path(str(self.state_path), "state_file")
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if (not isinstance(self.state, dict) or self.state.get("owner") != MARKER or
                    self.state.get("config_fingerprint") != self.fingerprint or
                    not re.fullmatch(r"[0-9a-f]{32}", str(self.state.get("task_id", "")))):
                raise SetupError("task_state_does_not_belong_to_this_configuration")
            bound = self.state.get("bound_avd_options")
            selected = {key: config[key] for key in ("system_image", "device_profile")}
            if bound is not None and bound != selected:
                raise Pending("created_avd_image_or_profile_is_bound_to_task",
                              "Agent: preserve this existing task AVD. To use a different image or profile, create a new config with a new dedicated task_root and avd_name; do not overwrite the existing AVD.")
        else:
            initial_files = list(self.root.iterdir()) if self.root.exists() else []
            if any(item.resolve() != config.get("_config_path") for item in initial_files):
                raise SetupError("task_root_is_not_empty_and_has_no_owned_state")
            self.state = {"schema_version": 1, "owner": MARKER, "task_id": uuid.uuid4().hex,
                          "config_fingerprint": self.fingerprint, "identity": identity, "state": "new", "tools": {}}
        self.env = os.environ.copy()
        self.env.update({"JAVA_HOME": str(self.java), "ANDROID_HOME": str(self.sdk), "ANDROID_SDK_ROOT": str(self.sdk),
                         "ANDROID_USER_HOME": str(safe_path(str(self.root / "android-user"), "android_user_home")),
                         "ANDROID_EMULATOR_HOME": str(safe_path(str(self.root / "emulator-home"), "android_emulator_home")),
                         "ANDROID_AVD_HOME": str(safe_path(str(self.root / "avd"), "android_avd_home"))})
        self.env["PATH"] = str(self.java / "bin") + os.pathsep + self.env.get("PATH", "")
        self.tools = self.find_tools()

    def find_tools(self) -> dict[str, Path | None]:
        exe = ".exe" if os.name == "nt" else ""
        batch = ".bat" if os.name == "nt" else ""
        candidates = sorted((self.sdk / "cmdline-tools").glob("*/bin/sdkmanager" + batch))
        latest = self.sdk / "cmdline-tools" / "latest" / "bin" / ("sdkmanager" + batch)
        if latest in candidates:
            candidates.remove(latest)
            candidates.insert(0, latest)
        sdkmanager = candidates[0] if candidates else None
        found = {"java": self.java / "bin" / ("java" + exe), "sdkmanager": sdkmanager,
                 "avdmanager": sdkmanager.parent / ("avdmanager" + batch) if sdkmanager else None,
                 "emulator": self.sdk / "emulator" / ("emulator" + exe),
                 "adb": self.sdk / "platform-tools" / ("adb" + exe)}
        for key, path in found.items():
            found[key] = safe_path(str(path), key) if path and path.is_file() else None
        return found

    def command(self, tool: str, *args: str, timeout: int = 45) -> subprocess.CompletedProcess:
        if not self.tools.get(tool):
            raise Pending(f"{tool}_missing", "Agent: run prepare with verified official archive metadata or supply a working existing tool path.")
        return run_tool([str(self.tools[tool]), *args], self.env, timeout)

    def save(self, report: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.state.update(state=report["state"], last_action=report["action"], updated_utc=datetime.now(timezone.utc).isoformat())
        temporary = self.state_path.with_suffix(".tmp")
        if temporary.exists():
            safe_path(str(temporary), "state_temporary_file")
            temporary.unlink()
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.state_path)

    def acceleration(self) -> dict:
        result = self.command("emulator", "-accel-check")
        return {"state": "verified" if result.returncode == 0 else "unavailable", "exit_code": result.returncode}

    def check(self) -> dict:
        checks = {}
        for tool, args in (("java", ("-version",)), ("sdkmanager", ("--version",)),
                           ("adb", ("version",)), ("emulator", ("-version",))):
            path = self.tools[tool]
            checks[tool] = {"path": str(path) if path else None, "state": "missing"}
            if path:
                result = self.command(tool, *args)
                checks[tool].update(state="verified" if result.returncode == 0 else "execution_failed", exit_code=result.returncode)
        checks["acceleration"] = self.acceleration() if self.tools["emulator"] else {"state": "not_checked"}
        ready = all(checks[k]["state"] == "verified" for k in ("java", "sdkmanager", "adb", "emulator"))
        return {"ok": ready, "state": "tools_checked", "next_action": "Agent: run discover to prepare missing verified JDK/CLI tools and obtain current image/profile options; choose them before prepare.", "errors": [], "checks": checks}

    def provision_archive(self, kind: str) -> Path:
        spec = self.config.get(kind)
        if spec is None:
            raise Pending(f"{kind}_official_archive_metadata_missing", f"Agent: obtain the current official {kind} archive URL and its upstream SHA256, then add both to the config. Do not download an unidentified or unhashed archive.")
        staging = safe_path(str(self.root / "tools" / (kind + "-" + uuid.uuid4().hex)), "archive_staging")
        staging.mkdir(parents=True)
        archive = staging / ("archive.zip" if urlsplit(spec["url"]).path.endswith(".zip") else "archive.tar.gz")
        download_archive(spec, archive, kind)
        destination = staging / "extracted"
        extract_archive(archive, destination)
        return destination

    def ensure_discovery_tools(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.tools["java"]:
            if self.java.exists() or not below(self.java, self.root):
                raise Pending("java_home_not_a_new_task_owned_destination", "Agent: reuse a working existing JDK, or select a nonexistent java_home beneath task_root for the verified JDK archive.")
            extracted = self.provision_archive("jdk_archive")
            name = "java.exe" if os.name == "nt" else "java"
            candidates = [p for p in extracted.rglob(name) if p.parent.name == "bin" and p.is_file()]
            if len(candidates) != 1:
                raise SetupError("jdk_archive_has_no_unique_java_home")
            self.java.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(candidates[0].parent.parent), str(self.java))
            self.tools = self.find_tools()
        if self.command("java", "-version").returncode != 0:
            raise SetupError("configured_jdk_does_not_run_on_this_host")
        if not self.tools["sdkmanager"]:
            extracted = self.provision_archive("commandline_tools")
            source = extracted / "cmdline-tools"
            if not (source / "lib").is_dir():
                raise SetupError("google_commandline_archive_has_invalid_layout")
            target = self.sdk / "cmdline-tools" / ("proxypin-" + self.state["task_id"])
            if target.exists():
                raise SetupError("task_commandline_tools_destination_already_exists")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            self.tools = self.find_tools()
        if self.command("sdkmanager", "--version").returncode != 0:
            raise SetupError("sdkmanager_or_configured_jdk_is_incompatible")
        if not self.tools.get("avdmanager"):
            raise SetupError("commandline_tools_are_missing_avdmanager")
        self.state["tools"] = {key: str(value) for key, value in self.tools.items() if value}

    def discover(self) -> dict:
        self.ensure_discovery_tools()
        listing = self.command("sdkmanager", "--sdk_root=" + str(self.sdk), "--list", "--channel=0", timeout=120)
        profiles_result = self.command("avdmanager", "list", "device")
        if listing.returncode != 0 or profiles_result.returncode != 0:
            raise SetupError("current_sdk_or_device_profile_discovery_failed")
        installed, available = table_packages(listing.stdout)
        profiles = device_profiles(profiles_result.stdout)
        images = sorted(package for package in installed | available if IMAGE.fullmatch(package))
        if not images or not profiles:
            raise Pending("current_discovery_did_not_return_image_and_profile_options",
                          "Agent: inspect the current SDK metadata/tool output; retry discovery after resolving the actual tool/network issue. Do not invent image or profile identifiers.")
        self.state["discovered_options"] = {"system_images": images, "device_profiles": profiles}
        return {"ok": True, "state": "options_discovered", "next_action": "Agent: choose compatible system_image and device_profile from these observed options, set the two config fields, then run prepare. Discover has not accepted SDK licenses, installed emulator packages or created an AVD.", "errors": [], "system_images": images, "device_profiles": profiles, "installed_packages": sorted(installed), "available_packages": sorted(available), "tools": self.state["tools"]}

    def selected_options(self) -> dict:
        if self.config["system_image"] is None or self.config["device_profile"] is None:
            raise Pending("image_and_profile_not_selected_after_discovery",
                          "Agent: run discover, choose compatible observed system_image/device_profile, update those two config fields and rerun prepare.")
        return {key: self.config[key] for key in ("system_image", "device_profile")}

    def verify_avd_files(self, avd_path: Path, listing: str, resolved_profile: str) -> None:
        sections = re.split(r"(?m)^\s*Name:\s*", listing)
        matching = [section for section in sections[1:] if section.splitlines()[0].strip() == self.config["avd_name"]]
        if len(matching) != 1:
            raise Pending("task_avd_not_uniquely_listed", "Agent: inspect the task AVD creation result; rerun prepare only after its actual name and directory are listed.")
        listed_path = re.search(r"(?m)^\s*Path:\s*(.+?)\s*$", matching[0])
        if not listed_path or safe_path(listed_path.group(1), "listed_avd_path") != avd_path:
            raise SetupError("listed_avd_path_does_not_match_task_creation_intent")
        outer = read_ini(avd_path.with_suffix(".ini"))
        if safe_path(outer.get("path", ""), "registered_avd_path") != avd_path:
            raise SetupError("registered_avd_path_does_not_match_task_creation_intent")
        actual = read_ini(avd_path / "config.ini")
        expected_image = self.sdk.joinpath(*self.config["system_image"].split(";")).resolve()
        image_value = actual.get("image.sysdir.1")
        if not image_value:
            raise SetupError("task_avd_image_metadata_missing")
        actual_image = Path(image_value)
        if not actual_image.is_absolute():
            actual_image = self.sdk / actual_image
        if safe_path(str(actual_image), "actual_avd_image") != expected_image:
            raise SetupError("task_avd_image_does_not_match_creation_intent")
        if actual.get("hw.device.name") != resolved_profile:
            raise SetupError("task_avd_device_profile_does_not_match_creation_intent")

    def prepare(self) -> dict:
        selected = self.selected_options()
        self.ensure_discovery_tools()
        listing = self.command("sdkmanager", "--sdk_root=" + str(self.sdk), "--list", "--channel=0", timeout=120)
        if listing.returncode != 0:
            raise SetupError("sdkmanager_package_listing_failed")
        installed, available = table_packages(listing.stdout)
        wanted = ["platform-tools", "emulator", self.config["system_image"]]
        if self.config["system_image"] not in installed | available:
            raise Pending("configured_system_image_not_in_current_sdkmanager_list", "Agent: inspect the current stable sdkmanager list and choose a compatible observed system_image; do not invent a package ID.")
        missing = [package for package in wanted if package not in installed]
        if missing:
            licenses = self.command("sdkmanager", "--sdk_root=" + str(self.sdk), "--licenses", timeout=30)
            if licenses.returncode != 0 or not re.search(r"(?mi)^\s*all sdk package licenses accepted\.?\s*$", licenses.stdout):
                raise Pending("sdk_licenses_require_actual_interactive_review", "Agent: inspect the current licenses in an available local interactive terminal and handle each actual prompt after reviewing it. Do not pipe unconditional yes. Ask the user only when tools or permissions cannot provide the required input.")
            result = self.command("sdkmanager", "--sdk_root=" + str(self.sdk), "--channel=0", *missing, timeout=600)
            if result.returncode != 0:
                raise Pending("sdk_package_install_failed_or_requires_interaction", "Agent: inspect current sdkmanager diagnostics/prompts and resolve that specific issue; existing SDK packages have been preserved.")
            listing = self.command("sdkmanager", "--sdk_root=" + str(self.sdk), "--list", "--channel=0", timeout=120)
            after, _ = table_packages(listing.stdout)
            if listing.returncode != 0 or not set(wanted) <= after:
                raise SetupError("required_sdk_packages_not_verified_after_install")
        self.tools = self.find_tools()
        if not all(self.tools.get(k) for k in ("adb", "emulator", "avdmanager")):
            raise SetupError("installed_sdk_packages_are_missing_required_executables")
        profiles = self.command("avdmanager", "list", "device")
        observed_profiles = device_profiles(profiles.stdout)
        matching_profiles = [profile for profile in observed_profiles if self.config["device_profile"] in (profile["name"], str(profile["id"]))]
        if profiles.returncode != 0 or len(matching_profiles) != 1:
            raise Pending("device_profile_not_in_current_avdmanager_list", "Agent: choose an actual device_profile from avdmanager list device.")
        resolved_profile = matching_profiles[0]["name"]
        for key in ("ANDROID_USER_HOME", "ANDROID_EMULATOR_HOME", "ANDROID_AVD_HOME"):
            Path(self.env[key]).mkdir(parents=True, exist_ok=True)
        listing = self.command("avdmanager", "list", "avd")
        if listing.returncode != 0:
            raise SetupError("avd_listing_failed")
        avd_path = safe_path(str(self.root / "avd" / (self.config["avd_name"] + ".avd")), "task_avd")
        exists = re.search(r"(?m)^\s*Name:\s*" + re.escape(self.config["avd_name"]) + r"\s*$", listing.stdout)
        if exists or avd_path.exists() or avd_path.with_suffix(".ini").exists():
            intent = self.state.get("creation_intent", {})
            owned = self.state.get("owned_avd") == str(avd_path)
            intended = (intent.get("avd_path") == str(avd_path) and intent.get("task_id") == self.state["task_id"]
                        and intent.get("before_directory_absent") is True and intent.get("before_name_absent") is True
                        and intent.get("selected_options") == selected and intent.get("resolved_profile") == resolved_profile)
            if not (owned or intended) or not exists or not avd_path.is_dir():
                raise Pending("avd_name_already_exists_without_matching_task_ownership", "Agent: preserve the existing AVD and use a new dedicated task_root with a new unique avd_name; do not overwrite or delete the existing AVD or its state.")
            self.verify_avd_files(avd_path, listing.stdout, resolved_profile)
        else:
            self.state["creation_intent"] = {"task_id": self.state["task_id"], "avd_name": self.config["avd_name"],
                                             "avd_path": str(avd_path), "selected_options": selected,
                                             "resolved_profile": resolved_profile,
                                             "before_directory_absent": True, "before_name_absent": True,
                                             "recorded_utc": datetime.now(timezone.utc).isoformat()}
            self.save({"ok": False, "state": "avd_creation_intent_saved", "action": "prepare", "errors": [], "next_action": "Agent: create only this recorded AVD; inspect actual prompts if interaction is required."})
            created = self.command("avdmanager", "create", "avd", "-n", self.config["avd_name"], "-k", self.config["system_image"],
                                   "-p", str(avd_path), "-d", self.config["device_profile"], timeout=60)
            if created.returncode != 0 or not avd_path.is_dir():
                raise Pending("avd_creation_not_verified_or_requires_interaction", "Agent: inspect the actual avdmanager prompt in an available local interactive terminal. Do not assume its answer or add -f; ask the user only if tools cannot handle the prompt.")
            listing = self.command("avdmanager", "list", "avd")
            if listing.returncode != 0:
                raise SetupError("created_task_avd_listing_failed")
            self.verify_avd_files(avd_path, listing.stdout, resolved_profile)
        self.state["owned_avd"] = str(avd_path)
        self.state["bound_avd_options"] = selected
        self.state.pop("creation_intent", None)
        self.state["tools"] = {key: str(value) for key, value in self.tools.items() if value}
        self.state["packages_verified"] = wanted
        return {"ok": True, "state": "prepared", "next_action": "Agent: run start; after launch run verify, then separately validate network and target App business behavior.", "errors": [], "packages": wanted, "avd": str(avd_path)}

    def require_avd(self) -> None:
        path = safe_path(str(self.root / "avd" / (self.config["avd_name"] + ".avd")), "task_avd")
        if self.state.get("owned_avd") != str(path) or not path.is_dir() or self.state.get("bound_avd_options") != self.selected_options():
            raise Pending("no_verified_task_owned_avd", "Agent: complete prepare for this configuration before controlling an emulator.")

    def expected_serial(self) -> str:
        port = self.state.get("console_port")
        serial = self.state.get("serial")
        if type(port) is not int or port % 2 or not 1024 <= port <= 65534 or serial != f"emulator-{port}":
            raise Pending("no_owned_started_instance", "Agent: run start for this task; do not select the first connected device.")
        return serial

    def verify_identity(self, serial: str) -> None:
        result = self.command("adb", "-s", serial, "emu", "avd", "name", timeout=15)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip() and line.strip() != "OK"]
        if result.returncode != 0 or lines != [self.config["avd_name"]]:
            raise Pending("emulator_identity_not_verified", "Agent: inspect the task instance and recorded serial/AVD name. Do not operate or stop a different connected device.")

    def start(self) -> dict:
        self.require_avd()
        if self.state.get("serial"):
            serial = self.expected_serial()
            connected = self.command("adb", "devices", "-l", timeout=15)
            if connected.returncode != 0:
                raise SetupError("adb_device_enumeration_failed")
            if re.search(r"(?m)^" + re.escape(serial) + r"\s+", connected.stdout):
                self.verify_identity(serial)
                if self.state.get("launch_writable_system", False) != self.config["writable_system"]:
                    raise Pending("writable_system_change_requires_task_restart", "Agent: run stop for this verified task instance, then start and verify with the new writable_system setting. Do not manually launch an unrecorded replacement.")
                return {"ok": True, "state": "instance_already_identified", "next_action": "Agent: run verify; network and business behavior remain separately unverified.", "errors": [], "serial": serial}
            # A disconnected old instance is not silently replaced by another device.
            self.state["last_disconnected_serial"] = serial
            for key in ("serial", "console_port", "pid"):
                self.state.pop(key, None)
        accel = self.acceleration()
        if accel["state"] != "verified":
            raise Pending("emulator_acceleration_unavailable", "Agent: inspect emulator -accel-check and current official host requirements. Handle the specific available setting; prompt the user only for inaccessible BIOS, restart, administrator or tool-limited actions.")
        help_result = self.command("emulator", "-help-port")
        match = re.search(r"\b([0-9]{4,5})\s*(?:\.\.|-|to|through|and)\s*([0-9]{4,5})\b", help_result.stdout, re.I)
        if help_result.returncode != 0 or not match:
            raise Pending("emulator_console_port_range_not_verified", "Agent: inspect the current emulator port help and adapt the parser to its actual supported range; do not guess a console port.")
        low, high = map(int, match.groups())
        if not 1024 <= low <= high <= 65534:
            raise SetupError("emulator_help_has_invalid_console_port_range")
        ports = [self.config["port"]] if "port" in self.config else range(low + low % 2, high + 1, 2)
        chosen = None
        for port in ports:
            if not low <= port <= high:
                raise SetupError("configured_port_is_outside_current_emulator_range")
            sockets = []
            try:
                for value in (port, port + 1):
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sockets.append(sock)
                    sock.bind(("127.0.0.1", value))
                chosen = port
                break
            except OSError:
                pass
            finally:
                for sock in sockets:
                    sock.close()
        if chosen is None:
            raise Pending("no_free_emulator_console_and_adb_port_pair", "Agent: choose another available even port within the current supported range; preserve other processes.")
        logs = safe_path(str(self.root / "logs"), "task_logs")
        logs.mkdir(exist_ok=True)
        log_path = logs / ("emulator-" + uuid.uuid4().hex + ".log")
        args = [str(self.tools["emulator"]), "-avd", self.config["avd_name"], "-port", str(chosen),
                "-no-snapshot", "-no-window", "-gpu", "auto"]
        if self.config["writable_system"]:
            args.append("-writable-system")
        self.state.update(console_port=chosen, serial=f"emulator-{chosen}", log_path=str(log_path),
                          launch_arguments=args, launch_writable_system=self.config["writable_system"])
        # Persist the intended instance before spawn so a failed later state write
        # cannot leave an unrecorded emulator running.
        self.save({"ok": False, "state": "launch_intent_saved", "action": "start", "errors": [], "next_action": "Agent: verify actual task launch outcome before device operations."})
        kwargs = {"env": self.env, "stdin": subprocess.DEVNULL, "stderr": subprocess.STDOUT}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        with log_path.open("xb") as output:
            process = subprocess.Popen(args, stdout=output, **kwargs)
        if process.poll() is not None:
            self.state["last_failed_launch_log"] = str(log_path)
            raise Pending("emulator_process_exited_during_launch", "Agent: inspect the task launch log and resolve the actual startup error before retrying; no boot or network readiness has been established.")
        self.state.update(console_port=chosen, serial=f"emulator-{chosen}", pid=process.pid, log_path=str(log_path))
        return {"ok": True, "state": "process_started", "next_action": "Agent: run verify. Process creation does not prove boot, network connectivity, VPN, MCP or target App readiness.", "errors": [], "serial": self.state["serial"], "pid": process.pid, "checks": {"acceleration": accel, "boot": "not_checked", "network": "not_checked", "business": "not_checked"}}

    def verify(self) -> dict:
        self.require_avd()
        if self.state.get("launch_writable_system", False) != self.config["writable_system"]:
            raise Pending("writable_system_change_requires_task_restart", "Agent: run stop, then start and verify to apply this task's changed writable_system setting.")
        serial = self.expected_serial()
        self.verify_identity(serial)
        accel = self.acceleration()
        deadline = time.monotonic() + self.config["boot_timeout_seconds"]
        booted = False
        while True:
            boot = self.command("adb", "-s", serial, "shell", "getprop", "sys.boot_completed", timeout=15)
            if boot.returncode == 0 and boot.stdout.strip() == "1":
                booted = True
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        if not booted:
            raise Pending("emulator_identified_but_boot_not_completed", "Agent: inspect this task's emulator log and current boot state, then rerun verify; do not claim network or App readiness.")
        api = self.command("adb", "-s", serial, "shell", "getprop", "ro.build.version.sdk", timeout=15)
        abi = self.command("adb", "-s", serial, "shell", "getprop", "ro.product.cpu.abilist", timeout=15)
        if api.returncode != 0 or not re.fullmatch(r"[0-9]{1,3}", api.stdout.strip()) or abi.returncode != 0 or not re.fullmatch(r"[A-Za-z0-9_,-]+", abi.stdout.strip()):
            raise SetupError("emulator_api_or_abi_could_not_be_verified")
        expected_api = int(re.match(r"system-images;android-([0-9]+)", self.config["system_image"]).group(1))
        expected_abi = self.config["system_image"].split(";")[-1]
        if int(api.stdout.strip()) != expected_api or expected_abi not in abi.stdout.strip().split(","):
            raise SetupError("emulator_api_or_abi_does_not_match_configured_image")
        self.state["verified_device"] = {"serial": serial, "api": int(api.stdout.strip()), "abi": abi.stdout.strip().split(",")}
        return {"ok": accel["state"] == "verified", "state": "device_verified", "next_action": "Agent: separately verify actual network connectivity and the target App without interception, then configure ProxyPin VPN/MCP. This helper has not verified those layers.", "errors": [] if accel["state"] == "verified" else ["acceleration_unavailable"], "device": self.state["verified_device"], "checks": {"identity": "verified", "acceleration": accel, "boot": "verified", "network": "not_checked", "business": "not_checked", "vpn": "not_checked", "mcp": "not_checked"}}

    def stop(self) -> dict:
        self.require_avd()
        serial = self.expected_serial()
        self.verify_identity(serial)
        result = self.command("adb", "-s", serial, "emu", "kill", timeout=15)
        if result.returncode != 0:
            raise SetupError("task_emulator_stop_command_failed")
        # Keep AVD/SDK and the launch record; exit still requires independent proof.
        self.state["last_stopped_serial"] = serial
        self.state.pop("serial", None)
        self.state.pop("console_port", None)
        self.state.pop("pid", None)
        return {"ok": True, "state": "stop_requested", "next_action": "Agent: confirm this instance has exited and finish any task-owned ProxyPin cleanup. SDK, JDK and AVD files were preserved.", "errors": [], "serial": serial}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--action", required=True, choices=("check", "discover", "prepare", "start", "verify", "stop"))
    args = parser.parse_args(argv)
    task = None
    try:
        task = EmulatorTask(load_config(args.config))
        report = getattr(task, args.action)()
    except Pending as exc:
        report = {"ok": False, "state": "pending", "next_action": exc.next_action, "errors": [str(exc)]}
    except SetupError as exc:
        report = {"ok": False, "state": "blocked", "next_action": "Agent: correct the specific validation or tool error before retrying this action.", "errors": [str(exc)]}
    except (OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile):
        report = {"ok": False, "state": "blocked", "next_action": "Agent: inspect local config, task files and official archive/tool diagnostics; no unsafe workaround was attempted.", "errors": ["local_input_archive_or_tool_operation_failed"]}
    report["action"] = args.action
    if task is not None:
        try:
            task.save(report)
            report["state_file"] = str(task.state_path)
        except (OSError, SetupError):
            report.update(ok=False, state="blocked", next_action="Agent: resolve task-state write permissions before continuing.")
            report["errors"].append("task_state_could_not_be_saved")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
