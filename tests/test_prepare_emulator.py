"""Offline safety and workflow checks; no real tools, downloads or devices."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_emulator.py"
spec = importlib.util.spec_from_file_location("prepare_emulator_under_test", SCRIPT)
em = importlib.util.module_from_spec(spec)
spec.loader.exec_module(em)


class EmulatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.config_path = self.base / "config.json"
        self.data = {"schema_version": 1, "task_root": str(self.base / "task"),
                     "sdk_root": str(self.base / "shared-sdk"), "java_home": str(self.base / "jdk"),
                     "system_image": "system-images;android-35;google_apis;x86_64",
                     "avd_name": "capture-test-unique", "device_profile": "pixel_7", "boot_timeout_seconds": 1}
        self.observed_image = self.data["system_image"]
        self.installed = {"platform-tools", "emulator", self.data["system_image"]}
        self.calls = []
        self.fake_tools()

    def tearDown(self):
        self.temp.cleanup()

    def fake_tools(self):
        exe = ".exe" if os.name == "nt" else ""
        batch = ".bat" if os.name == "nt" else ""
        sdk = Path(self.data["sdk_root"])
        files = [Path(self.data["java_home"]) / "bin" / ("java" + exe),
                 sdk / "cmdline-tools" / "latest" / "bin" / ("sdkmanager" + batch),
                 sdk / "cmdline-tools" / "latest" / "bin" / ("avdmanager" + batch),
                 sdk / "emulator" / ("emulator" + exe), sdk / "platform-tools" / ("adb" + exe)]
        for path in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake tool: do not execute")

    def config(self):
        self.config_path.write_text(json.dumps(self.data), encoding="utf-8")
        return em.load_config(self.config_path)

    def fake_run(self, args, env, timeout=45):
        self.calls.append((args, env))
        stdout = "version 1"
        if "--list" in args:
            stdout = "Installed packages:\n" + "\n".join(p + " | 1 | installed" for p in sorted(self.installed))
            stdout += "\nAvailable packages:\n" + self.observed_image + " | 1 | available\n"
        elif "--licenses" in args:
            stdout = "All SDK package licenses accepted."
        elif "list" in args and "device" in args:
            stdout = 'id: 7 or "pixel_7"\n Name: Pixel 7'
        elif "list" in args and "avd" in args:
            path = Path(self.data["task_root"]) / "avd" / (self.data["avd_name"] + ".avd")
            stdout = "Name: " + self.data["avd_name"] + "\nPath: " + str(path) if path.exists() else "Available Android Virtual Devices:"
        elif "create" in args:
            self.materialize_avd(Path(args[args.index("-p") + 1]))
            stdout = "Created"
        elif "-help-port" in args:
            stdout = "Supported console port range: 5554 through 5682"
        elif "-accel-check" in args:
            stdout = "accel: 0; usable"
        elif args[-3:] == ["emu", "avd", "name"]:
            stdout = self.data["avd_name"] + "\nOK\n"
        elif args[-1] == "sys.boot_completed":
            stdout = "1\n"
        elif args[-1] == "ro.build.version.sdk":
            stdout = "35\n"
        elif args[-1] == "ro.product.cpu.abilist":
            stdout = "x86_64,x86\n"
        elif "--channel=0" in args and "--list" not in args:
            self.installed.update(arg for arg in args if arg in {"platform-tools", "emulator", self.data["system_image"]})
        return subprocess.CompletedProcess(args, 0, stdout)

    def materialize_avd(self, path):
        path.mkdir(parents=True, exist_ok=True)
        path.with_suffix(".ini").write_text("avd.ini.encoding=UTF-8\npath=" + str(path) + "\n", encoding="utf-8")
        image_path = "/".join(self.data["system_image"].split(";")) + "/"
        (path / "config.ini").write_text("image.sysdir.1=" + image_path + "\nhw.device.name=pixel_7\n", encoding="utf-8")

    def prepared_task(self):
        task = em.EmulatorTask(self.config())
        with patch.object(em, "run_tool", side_effect=self.fake_run):
            report = task.prepare()
        task.save({**report, "action": "prepare"})
        return task

    def test_config_rejects_skill_root_and_relative_paths(self):
        for root in (str(em.SKILL_ROOT / "task"), "relative-task", str(Path(self.data["task_root"]).anchor)):
            self.data["task_root"] = root
            with self.assertRaises(em.SetupError):
                self.config()

    def test_config_rejects_injected_identifiers_and_unknown_secret_fields(self):
        self.data["avd_name"] = "test & bad"
        with self.assertRaises(em.SetupError):
            self.config()

    def test_initial_config_can_be_saved_in_the_dedicated_task_root(self):
        root = Path(self.data["task_root"])
        root.mkdir()
        self.config_path = root / "emulator-config.json"
        task = em.EmulatorTask(self.config())
        with patch.object(em, "run_tool", side_effect=self.fake_run):
            report = task.check()
        task.save({**report, "action": "check"})
        self.assertTrue(task.state_path.is_file())
        self.data["avd_name"] = "test"
        self.data["token"] = "do-not-store"
        with self.assertRaises(em.SetupError):
            self.config()

    def test_archive_metadata_requires_identity_hash_and_matching_os(self):
        with self.assertRaises(em.SetupError):
            em.validate_archive({"url": "https://dl.google.com/android/repository/a.zip"}, "commandline_tools")
        with self.assertRaises(em.SetupError):
            em.validate_archive({"url": "https://evil.test/archive.zip", "sha256": "a" * 64}, "commandline_tools")
        with self.assertRaises(em.SetupError):
            em.validate_archive({"url": "https://github.com/other/repo/a.zip", "sha256": "a" * 64, "publisher": "adoptium"}, "jdk_archive")

    def test_zip_and_tar_traversal_are_rejected_before_writes(self):
        archive = self.base / "bad.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("safe/file", "hello")
            z.writestr("../escape", "bad")
        destination = self.base / "extract-zip"
        with self.assertRaises(em.SetupError):
            em.extract_archive(archive, destination)
        self.assertFalse(destination.exists())
        archive = self.base / "bad.tar.gz"
        with tarfile.open(archive, "w:gz") as t:
            member = tarfile.TarInfo("jdk/bin/java")
            member.type = tarfile.SYMTYPE
            member.linkname = "/outside"
            t.addfile(member)
        with self.assertRaises(em.SetupError):
            em.extract_archive(archive, self.base / "extract-tar")

    def test_zip_duplicate_case_and_reserved_windows_paths_rejected(self):
        for names in (("A/file", "a/file"), ("CON.txt",), ("x\\escape",)):
            archive = self.base / "duplicate.zip"
            with zipfile.ZipFile(archive, "w") as z:
                for name in names:
                    z.writestr(name, "bad")
            # ZipInfo normalizes Windows separators when constructing the fixture.
            if names == ("x\\escape",):
                archive.write_bytes(archive.read_bytes().replace(b"x/escape", b"x\\escape"))
            with self.assertRaises(em.SetupError):
                em.extract_archive(archive, self.base / "extract")

    def test_safe_internal_jdk_tar_file_alias_materialized_without_links(self):
        archive = self.base / "safe.tar.gz"
        with tarfile.open(archive, "w:gz") as t:
            java = tarfile.TarInfo("jdk/bin/java")
            java.size = 4
            java.mode = 0o755
            t.addfile(java, io.BytesIO(b"java"))
            alias = tarfile.TarInfo("jdk/lib/java-alias")
            alias.type = tarfile.SYMTYPE
            alias.linkname = "../bin/java"
            t.addfile(alias)
        destination = self.base / "extract-internal"
        em.extract_archive(archive, destination)
        self.assertEqual((destination / "jdk/lib/java-alias").read_bytes(), b"java")
        self.assertFalse((destination / "jdk/lib/java-alias").is_symlink())

    def test_download_hash_mismatch_removes_archive(self):
        platform = "win" if os.name == "nt" else "mac" if sys.platform == "darwin" else "linux"
        spec = {"url": f"https://dl.google.com/android/repository/commandlinetools-{platform}-123_latest.zip", "sha256": "0" * 64}
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = [b"archive bytes", b""]
        opener = Mock()
        opener.open.return_value = response
        path = self.base / "download.zip"
        with patch.object(em, "build_opener", return_value=opener), self.assertRaises(em.SetupError):
            em.download_archive(spec, path, "commandline_tools")
        self.assertFalse(path.exists())

    def test_prepare_reuses_tools_no_download_no_global_env_change(self):
        before = os.environ.copy()
        with patch.object(em, "download_archive") as download, patch.object(em, "run_tool", side_effect=self.fake_run):
            task = em.EmulatorTask(self.config())
            report = task.prepare()
        self.assertTrue(report["ok"])
        download.assert_not_called()
        self.assertEqual(dict(os.environ), before)
        self.assertFalse(any("--licenses" in args for args, _ in self.calls))
        create = next(args for args, _ in self.calls if "create" in args)
        self.assertNotIn("-f", create)
        for _, env in self.calls:
            self.assertTrue(str(env["ANDROID_AVD_HOME"]).startswith(self.data["task_root"]))

    def test_prepare_installs_only_missing_packages(self):
        self.installed.remove(self.data["system_image"])
        with patch.object(em, "run_tool", side_effect=self.fake_run):
            report = em.EmulatorTask(self.config()).prepare()
        self.assertTrue(report["ok"])
        install = next(args for args, _ in self.calls if "--channel=0" in args and "--list" not in args)
        self.assertIn(self.data["system_image"], install)
        self.assertNotIn("platform-tools", install)
        self.assertNotIn("emulator", install)

    def test_prepare_missing_google_tools_uses_verified_archive_in_unique_sdk_directory(self):
        self.data["sdk_root"] = str(Path(self.data["task_root"]) / "tools" / "sdk")
        self.installed = set()
        platform = "win" if os.name == "nt" else "mac" if sys.platform == "darwin" else "linux"
        self.data["commandline_tools"] = {"url": f"https://dl.google.com/android/repository/commandlinetools-{platform}-123_latest.zip", "sha256": "a" * 64}
        def fake_download(spec, destination, kind):
            self.assertEqual(kind, "commandline_tools")
            batch = ".bat" if os.name == "nt" else ""
            with zipfile.ZipFile(destination, "w") as z:
                z.writestr("cmdline-tools/lib/tool.jar", b"fake")
                z.writestr("cmdline-tools/bin/sdkmanager" + batch, b"fake")
                z.writestr("cmdline-tools/bin/avdmanager" + batch, b"fake")
        def install(args, env, timeout=45):
            result = self.fake_run(args, env, timeout)
            if "--channel=0" in args and "--list" not in args:
                sdk = Path(self.data["sdk_root"])
                exe = ".exe" if os.name == "nt" else ""
                for file in (sdk / "emulator" / ("emulator" + exe), sdk / "platform-tools" / ("adb" + exe)):
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_bytes(b"fake")
            return result
        with patch.object(em, "download_archive", side_effect=fake_download) as download, patch.object(em, "run_tool", side_effect=install):
            task = em.EmulatorTask(self.config())
            report = task.prepare()
        self.assertTrue(report["ok"])
        download.assert_called_once()
        self.assertIn("proxypin-" + task.state["task_id"], str(task.tools["sdkmanager"]))

    def test_prepare_missing_jdk_requires_task_owned_destination_and_verified_archive(self):
        self.data["java_home"] = str(Path(self.data["task_root"]) / "tools" / "java")
        self.data["jdk_archive"] = {"publisher": "adoptium", "url": "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.12+7/OpenJDK17U-jdk_x64_windows_hotspot_17.0.12_7.zip", "sha256": "a" * 64}
        def fake_download(spec, destination, kind):
            self.assertEqual(kind, "jdk_archive")
            exe = ".exe" if os.name == "nt" else ""
            with zipfile.ZipFile(destination, "w") as z:
                z.writestr("jdk/bin/java" + exe, b"fake")
                z.writestr("jdk/lib/modules", b"fake")
        with patch.object(em, "download_archive", side_effect=fake_download), patch.object(em, "run_tool", side_effect=self.fake_run):
            report = em.EmulatorTask(self.config()).prepare()
        self.assertTrue(report["ok"])
        self.assertTrue((Path(self.data["java_home"]) / "lib/modules").is_file())

    def test_blank_config_discover_select_then_prepare_without_fingerprint_conflict(self):
        self.data.update(system_image=None, device_profile=None)
        self.data["sdk_root"] = str(Path(self.data["task_root"]) / "tools" / "sdk")
        self.data["java_home"] = str(Path(self.data["task_root"]) / "tools" / "java")
        self.installed = set()
        platform = "win" if os.name == "nt" else "mac" if sys.platform == "darwin" else "linux"
        self.data["commandline_tools"] = {"url": f"https://dl.google.com/android/repository/commandlinetools-{platform}-123_latest.zip", "sha256": "a" * 64}
        self.data["jdk_archive"] = {"publisher": "adoptium", "url": "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.12+7/OpenJDK17U-jdk_x64_windows_hotspot_17.0.12_7.zip", "sha256": "a" * 64}
        def fake_download(spec, destination, kind):
            suffix = ".bat" if os.name == "nt" else ""
            executable = ".exe" if os.name == "nt" else ""
            with zipfile.ZipFile(destination, "w") as z:
                if kind == "jdk_archive":
                    z.writestr("jdk/bin/java" + executable, b"fake")
                    z.writestr("jdk/lib/modules", b"fake")
                else:
                    z.writestr("cmdline-tools/lib/tool.jar", b"fake")
                    z.writestr("cmdline-tools/bin/sdkmanager" + suffix, b"fake")
                    z.writestr("cmdline-tools/bin/avdmanager" + suffix, b"fake")
        def install(args, env, timeout=45):
            result = self.fake_run(args, env, timeout)
            if "--channel=0" in args and "--list" not in args:
                sdk = Path(self.data["sdk_root"])
                exe = ".exe" if os.name == "nt" else ""
                for file in (sdk / "emulator" / ("emulator" + exe), sdk / "platform-tools" / ("adb" + exe)):
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_bytes(b"fake")
            return result
        with patch.object(em, "download_archive", side_effect=fake_download), patch.object(em, "run_tool", side_effect=install):
            task = em.EmulatorTask(self.config())
            checked = task.check()
            task.save({**checked, "action": "check"})
            task = em.EmulatorTask(self.config())
            discovered = task.discover()
            task.save({**discovered, "action": "discover"})
            self.assertEqual(discovered["system_images"], [self.observed_image])
            self.assertFalse(any("--licenses" in args or "create" in args for args, _ in self.calls))
            self.data.update(system_image=self.observed_image, device_profile="pixel_7")
            task = em.EmulatorTask(self.config())
            report = task.prepare()
        self.assertTrue(report["ok"])
        self.assertEqual(task.state["bound_avd_options"]["system_image"], self.observed_image)

    def test_created_avd_cannot_change_image_or_profile(self):
        self.prepared_task()
        self.data["system_image"] = "system-images;android-36;google_apis;x86_64"
        with self.assertRaises(em.Pending):
            em.EmulatorTask(self.config())

    def test_interactive_creation_recovers_only_from_saved_intent_and_actual_metadata(self):
        task = em.EmulatorTask(self.config())
        def interactive(args, env, timeout=45):
            if "create" in args:
                self.calls.append((args, env))
                return subprocess.CompletedProcess(args, 1, "Need an interactive hardware-profile answer")
            return self.fake_run(args, env, timeout)
        with patch.object(em, "run_tool", side_effect=interactive), self.assertRaises(em.Pending):
            task.prepare()
        saved = json.loads(task.state_path.read_text())
        self.assertIn("creation_intent", saved)
        avd = Path(saved["creation_intent"]["avd_path"])
        self.materialize_avd(avd)  # Simulate the agent completing actual local prompts.
        with patch.object(em, "run_tool", side_effect=self.fake_run):
            recovered = em.EmulatorTask(self.config())
            report = recovered.prepare()
        self.assertTrue(report["ok"])
        self.assertEqual(recovered.state["owned_avd"], str(avd))
        self.assertNotIn("creation_intent", recovered.state)
        self.assertEqual(sum("create" in args for args, _ in self.calls), 1)

    def test_saved_intent_rejects_manually_created_wrong_image(self):
        task = em.EmulatorTask(self.config())
        def interactive(args, env, timeout=45):
            if "create" in args:
                return subprocess.CompletedProcess(args, 1, "Interactive input required")
            return self.fake_run(args, env, timeout)
        with patch.object(em, "run_tool", side_effect=interactive), self.assertRaises(em.Pending):
            task.prepare()
        avd = Path(task.state["creation_intent"]["avd_path"])
        self.materialize_avd(avd)
        (avd / "config.ini").write_text("image.sysdir.1=system-images/android-34/google_apis/x86_64/\nhw.device.name=pixel_7\n")
        with patch.object(em, "run_tool", side_effect=self.fake_run), self.assertRaises(em.SetupError):
            em.EmulatorTask(self.config()).prepare()

    def test_pending_licenses_never_install_or_send_yes(self):
        self.installed.remove(self.data["system_image"])
        def pending(args, env, timeout=45):
            result = self.fake_run(args, env, timeout)
            return subprocess.CompletedProcess(args, 0, "Review licenses (y/N)?") if "--licenses" in args else result
        with patch.object(em, "run_tool", side_effect=pending), self.assertRaises(em.Pending) as raised:
            em.EmulatorTask(self.config()).prepare()
        self.assertIn("Agent:", raised.exception.next_action)
        self.assertFalse(any("--channel=0" in args and "--list" not in args for args, _ in self.calls))

    def test_existing_unowned_avd_never_overwritten(self):
        task = em.EmulatorTask(self.config())
        (task.root / "avd" / (self.data["avd_name"] + ".avd")).mkdir(parents=True)
        with patch.object(em, "run_tool", side_effect=self.fake_run), self.assertRaises(em.Pending):
            task.prepare()
        self.assertFalse(any("create" in args for args, _ in self.calls))

    def test_start_checks_both_ports_and_keeps_readiness_separate(self):
        task = self.prepared_task()
        sockets = []
        def fake_socket(*args):
            sock = Mock()
            sockets.append(sock)
            return sock
        process = Mock(pid=321)
        process.poll.return_value = None
        with patch.object(em, "run_tool", side_effect=self.fake_run), patch.object(em.socket, "socket", side_effect=fake_socket), patch.object(em.subprocess, "Popen", return_value=process) as launch:
            report = task.start()
        self.assertEqual(report["state"], "process_started")
        self.assertEqual([s.bind.call_args.args[0][1] for s in sockets], [5554, 5555])
        args, kwargs = launch.call_args
        self.assertIn("-no-window", args[0])
        self.assertIn("-no-snapshot", args[0])
        self.assertNotIn("-writable-system", args[0])
        self.assertEqual(report["checks"]["network"], "not_checked")
        self.assertNotIn("shell", kwargs)

    def test_start_skips_port_pair_if_neighbor_is_occupied(self):
        task = self.prepared_task()
        checked = []
        def fake_socket(*args):
            sock = Mock()
            def bind(address):
                checked.append(address[1])
                if address[1] == 5555:
                    raise OSError("occupied")
            sock.bind.side_effect = bind
            return sock
        process = Mock(pid=123)
        process.poll.return_value = None
        with patch.object(em, "run_tool", side_effect=self.fake_run), patch.object(em.socket, "socket", side_effect=fake_socket), patch.object(em.subprocess, "Popen", return_value=process):
            report = task.start()
        self.assertEqual(checked, [5554, 5555, 5556, 5557])
        self.assertEqual(report["serial"], "emulator-5556")

    def test_start_with_failed_acceleration_does_not_launch(self):
        task = self.prepared_task()
        def failed(args, env, timeout=45):
            return subprocess.CompletedProcess(args, 1, "acceleration unavailable") if "-accel-check" in args else self.fake_run(args, env, timeout)
        with patch.object(em, "run_tool", side_effect=failed), patch.object(em.subprocess, "Popen") as launch, self.assertRaises(em.Pending):
            task.start()
        launch.assert_not_called()

    def test_writable_system_is_explicit_recorded_and_requires_task_restart(self):
        self.prepared_task()
        self.data["writable_system"] = True
        task = em.EmulatorTask(self.config())
        process = Mock(pid=123)
        process.poll.return_value = None
        with patch.object(em, "run_tool", side_effect=self.fake_run), patch.object(em.socket, "socket", return_value=Mock()), patch.object(em.subprocess, "Popen", return_value=process) as launch:
            report = task.start()
        self.assertEqual(report["state"], "process_started")
        self.assertIn("-writable-system", launch.call_args.args[0])
        self.assertTrue(task.state["launch_writable_system"])
        self.assertIn("-writable-system", json.loads(task.state_path.read_text())["launch_arguments"])
        task.save({**report, "action": "start"})
        self.data["writable_system"] = False
        changed = em.EmulatorTask(self.config())
        with self.assertRaises(em.Pending) as pending:
            changed.verify()
        self.assertIn("stop", pending.exception.next_action)

    def test_verify_crosschecks_identity_boot_api_abi(self):
        task = self.prepared_task()
        task.state.update(console_port=5580, serial="emulator-5580")
        with patch.object(em, "run_tool", side_effect=self.fake_run):
            report = task.verify()
        self.assertEqual(report["device"]["api"], 35)
        self.assertEqual(report["device"]["abi"], ["x86_64", "x86"])
        self.assertEqual(report["checks"]["business"], "not_checked")
        adb_calls = [args for args, _ in self.calls if Path(args[0]).stem == "adb"]
        self.assertTrue(all(args[1:3] == ["-s", "emulator-5580"] for args in adb_calls))

    def test_stop_refuses_other_avd_and_never_kill_server(self):
        task = self.prepared_task()
        task.state.update(console_port=5580, serial="emulator-5580")
        def other(args, env, timeout=45):
            result = self.fake_run(args, env, timeout)
            return subprocess.CompletedProcess(args, 0, "someone-else\nOK") if args[-3:] == ["emu", "avd", "name"] else result
        with patch.object(em, "run_tool", side_effect=other), self.assertRaises(em.Pending):
            task.stop()
        self.assertFalse(any("kill" in args or "kill-server" in args for args, _ in self.calls))
        with patch.object(em, "run_tool", side_effect=self.fake_run):
            report = task.stop()
        self.assertEqual(report["state"], "stop_requested")
        self.assertTrue((task.root / "avd" / (self.data["avd_name"] + ".avd")).exists())
        self.assertTrue(Path(self.data["sdk_root"]).exists())

    def test_cli_pending_report_and_task_state_no_sensitive_fields(self):
        self.config()
        output = io.StringIO()
        with patch.object(em, "run_tool", side_effect=self.fake_run), contextlib.redirect_stdout(output):
            status = em.main(["--config", str(self.config_path), "--action", "verify"])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 2)
        self.assertEqual(report["state"], "pending")
        state = json.loads(Path(report["state_file"]).read_text())
        self.assertNotIn("token", state)
        self.assertNotIn("account", state)

    def test_run_tool_uses_no_shell_or_interactive_answers(self):
        with patch.object(em.subprocess, "run", return_value=subprocess.CompletedProcess(["x"], 0, "")) as run:
            em.run_tool(["x", "--licenses"], {})
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("input", kwargs)
        self.assertNotIn("shell", kwargs)


if __name__ == "__main__":
    unittest.main()
