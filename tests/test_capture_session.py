"""Offline state-boundary tests; no device, MCP, installer, or network access."""
import contextlib
import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "capture_session.py"
SPEC = importlib.util.spec_from_file_location("capture_session", SCRIPT)
session = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(session)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "session"

    def invoke(self, command, *args, work=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = session.main([command, "--work-dir", str(work or self.work), *map(str, args)])
        response = json.loads(output.getvalue())
        for key in ("ok", "current_step", "next_action", "errors"):
            self.assertIn(key, response)
        self.assertEqual(code == 0, response["ok"])
        self.assertEqual(response["business_success"], "not_auto_verified")
        return code, response

    def init(self, *args):
        code, response = self.invoke("init", "--target", "商品App", "--goal", "products",
                                     "--route", "physical", *args)
        self.assertEqual(code, 0, response)
        return response

    def evidence(self, name="observed.txt", content="Observed nonsecret context."):
        self.work.mkdir(exist_ok=True)
        path = self.work / name
        path.write_text(content, encoding="utf-8")
        return path

    def passed(self, step, path=None, kind="agent-observed"):
        return self.invoke("pass", "--step", step, "--evidence-file", path or self.evidence(),
                           "--evidence-kind", kind)

    def reach_output(self, *args):
        self.init(*args)
        for step in session.STEPS[:session.STEPS.index("sample_output")]:
            code, response = self.passed(step, self.evidence(step + ".txt"))
            self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "sample_output")

    def products(self, rows=None, name="products.json"):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if rows is None:
            rows = [{"name": "红色杯子", "id": "one", "url": "https://images.shop-cdn.cn/photo.jpg"},
                    {"name": "蓝色杯子", "id": "two", "url": "https://images.shop-cdn.cn/photo.jpg"}]
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return path

    def state(self):
        return json.loads((self.work / session.STATE_FILE).read_text(encoding="utf-8"))

    def test_steps_cannot_be_skipped_by_pass_or_validation(self):
        self.init()
        original = (self.work / session.STATE_FILE).read_bytes()
        code, response = self.passed("real_flow")
        self.assertEqual(code, 2)
        self.assertEqual(response["errors"], ["step_out_of_order"])
        self.assertEqual(response["current_step"], "target")
        self.assertEqual(original, (self.work / session.STATE_FILE).read_bytes())
        code, response = self.invoke("validate-output", "--products", self.products())
        self.assertEqual(code, 2)
        self.assertEqual(response["current_step"], "target")

    def test_missing_empty_and_literal_pass_are_not_evidence(self):
        self.init()
        for path in (self.work / "missing.txt", self.evidence("empty.txt", "")):
            code, response = self.passed("target", path)
            self.assertEqual(code, 2)
            self.assertEqual(response["current_step"], "target")
        code, response = self.invoke("pass", "--step", "target")
        self.assertEqual(code, 2)
        self.assertEqual(response["errors"], ["invalid_arguments_use_command_help"])

    def test_user_reports_and_file_checks_stay_pending_and_kinds_are_distinct(self):
        self.init()
        for kind in ("user-reported", "file-validated"):
            code, response = self.passed("target", kind=kind)
            self.assertEqual(code, 2)
            self.assertEqual(response["current_step"], "target")
        code, response = self.passed("target")
        self.assertEqual(code, 0)
        self.assertEqual(response["current_step"], "host_tools")
        records = self.state()["steps"]["target"]["evidence"]
        self.assertEqual([r["kind"] for r in records], list(reversed(session.KINDS)))
        self.assertEqual(records[-1]["sha256"], hashlib.sha256(self.evidence().read_bytes()).hexdigest())
        self.assertNotIn("Observed nonsecret context", json.dumps(self.state()))

    def test_software_ready_does_not_mean_capture_success(self):
        self.init()
        self.passed("target")
        self.passed("host_tools")
        _, response = self.invoke("status")
        self.assertEqual(response["current_step"], "route_device")
        self.assertFalse(response["workflow_complete"])
        self.assertEqual(response["installation_status"], "not_checked_by_helper")

    def test_reinitialization_cannot_destroy_an_old_session(self):
        self.init()
        self.passed("target", kind="user-reported")
        self.invoke("pending", "--step", "target", "--reason", "user-action-required")
        original = (self.work / session.STATE_FILE).read_bytes()
        code, response = self.invoke("init", "--target", "AnotherApp", "--goal", "data", "--route", "browser")
        self.assertEqual(code, 2)
        self.assertEqual(response["errors"], ["session_exists_use_status_do_not_reinitialize"])
        self.assertEqual(original, (self.work / session.STATE_FILE).read_bytes())
        self.invoke("status")
        self.assertEqual(original, (self.work / session.STATE_FILE).read_bytes())

    def test_work_dir_refuses_skill_root_and_descendants_before_any_write(self):
        root = SCRIPT.parent.parent
        nonexistent = root / "capture-session-test-must-not-be-created"
        self.assertFalse(nonexistent.exists())
        for forbidden in (root, root / "scripts", nonexistent):
            with self.subTest(work_dir=forbidden):
                self.assertFalse((forbidden / session.STATE_FILE).exists())
                code, response = self.invoke("init", "--target", "App", "--goal", "products",
                                             "--route", "physical", work=forbidden)
                self.assertEqual(code, 2)
                self.assertEqual(response["errors"], ["work_dir_must_be_outside_skill_repository"])
                self.assertFalse((forbidden / session.STATE_FILE).exists())
                self.assertFalse((forbidden / "capture-session.lock").exists())
                self.assertFalse((forbidden / (session.STATE_FILE + ".tmp")).exists())
                code, response = self.invoke("status", work=forbidden)
                self.assertEqual(code, 2)
                self.assertEqual(response["errors"], ["work_dir_must_be_outside_skill_repository"])
        self.assertFalse(nonexistent.exists())

    def test_session_and_internal_evidence_are_portable(self):
        self.init()
        self.passed("target")
        moved = self.root / "moved-session"
        shutil.move(str(self.work), moved)
        code, response = self.invoke("status", work=moved)
        self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "host_tools")
        state = json.loads((moved / session.STATE_FILE).read_text())
        self.assertEqual(state["steps"]["target"]["evidence"][0]["path"], "observed.txt")

    def test_changed_evidence_revokes_progress_without_losing_records(self):
        self.init()
        target = self.evidence("target.txt")
        self.passed("target", target)
        self.passed("host_tools", self.evidence("tools.txt"))
        target.write_text("Changed actual context", encoding="utf-8")
        code, response = self.invoke("status")
        self.assertEqual(code, 2)
        self.assertEqual(response["current_step"], "target")
        self.assertIn("accepted_evidence_changed_or_unavailable:target", response["errors"])
        self.passed("target", target)
        state = self.state()
        self.assertEqual(len(state["steps"]["target"]["evidence"]), 2)
        self.assertEqual(len(state["steps"]["host_tools"]["evidence"]), 1)
        self.assertEqual(state["steps"]["host_tools"]["status"], "pending")

    def test_existing_capture_records_applicability_and_requires_real_flow(self):
        capture = self.root / "observed.har"
        capture.write_text('{"log":{"entries":[]}}', encoding="utf-8")
        code, response = self.invoke("init", "--target", "App", "--goal", "products",
            "--route", "existing-capture", "--capture-file", capture,
            "--capture-origin", "physical", "--capture-applicability", "target-and-goal-reviewed",
            "--require-physical")
        self.assertEqual(code, 0, response)
        self.passed("target")
        _, response = self.invoke("status")
        self.assertEqual(response["current_step"], "real_flow")
        imported = self.state()["existing_capture"]
        self.assertEqual(imported["evidence"]["kind"], "file-validated")
        self.assertTrue(imported["applicability_is_caller_assertion"])
        capture.write_text("changed HAR", encoding="utf-8")
        code, response = self.passed("real_flow")
        self.assertEqual(code, 2)
        self.assertEqual(response["errors"], ["existing_capture_changed_or_unavailable"])

    def test_true_device_cannot_be_changed_to_emulator_or_emulator_capture(self):
        code, response = self.invoke("init", "--target", "App", "--goal", "products",
                                     "--route", "emulator", "--require-physical")
        self.assertEqual(code, 2)
        self.assertIn("physical_device_requirement_cannot_use_other_route", response["errors"])
        capture = self.root / "capture.har"
        capture.write_text("observed capture")
        code, response = self.invoke("init", "--target", "App", "--goal", "products",
            "--route", "existing-capture", "--capture-file", capture, "--capture-origin", "emulator",
            "--capture-applicability", "target-and-goal-reviewed", "--require-physical")
        self.assertEqual(code, 2)
        self.assertIn("physical_requirement_conflicts_with_capture_origin", response["errors"])

    def test_import_requires_applicability_not_only_a_file(self):
        code, response = self.invoke("init", "--target", "App", "--goal", "products",
                                     "--route", "existing-capture", "--capture-file", self.root / "exists.har")
        self.assertEqual(code, 2)
        self.assertIn("existing_capture_requires_file_origin_and_applicability_review", response["errors"])

    def test_output_validation_retains_multiple_products_and_is_not_business_success(self):
        self.reach_output()
        path = self.products()
        original = path.read_bytes()
        code, response = self.invoke("validate-output", "--products", path)
        self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "sample_output")
        self.assertEqual(response["statistics"]["rows"], 2)
        self.assertEqual(response["statistics"]["unique_urls"], 1)
        self.assertEqual(response["statistics"]["name_url_authenticity"], "requires_agent_source_spot_check")
        self.assertEqual(original, path.read_bytes())
        self.assertTrue((self.work / "download_images.py").is_file())
        self.assertFalse((self.work / "downloaded-images").exists())
        self.assertFalse((self.work / "images").exists())
        self.assertNotIn("红色杯子", json.dumps(self.state(), ensure_ascii=False))
        self.assertNotIn("shop-cdn", json.dumps(response))
        code, response = self.passed("sample_output", self.evidence("spot-check.txt"))
        self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "deliver")
        self.passed("deliver")
        _, response = self.passed("restore")
        self.assertTrue(response["workflow_complete"])
        self.assertEqual(response["business_success"], "not_auto_verified")

    def test_preview_is_default_offline_and_does_not_create_images(self):
        self.reach_output()
        path = self.products(name="result.json")
        real_run = subprocess.run
        seen = []
        def inspect(command, **kwargs):
            seen.append(command)
            self.assertNotIn("--download", command)
            self.assertEqual(command[-1], str(path.resolve()))
            return real_run(command, **kwargs)
        with mock.patch.object(session.subprocess, "run", side_effect=inspect):
            code, response = self.invoke("validate-output", "--products", path)
        self.assertEqual(code, 0, response)
        self.assertEqual(len(seen), 1)
        self.assertEqual(response["preview"]["mode"], "preview")
        self.assertFalse((self.work / "downloaded-images").exists())

    def test_sample_cannot_advance_without_validation_or_with_changed_products(self):
        self.reach_output()
        code, response = self.passed("sample_output")
        self.assertEqual(code, 2)
        self.assertIn("validate_real_output_before_source_spot_check", response["errors"])
        path = self.products()
        self.invoke("validate-output", "--products", path)
        path.write_text("[]")
        code, response = self.passed("sample_output")
        self.assertEqual(code, 2)
        self.assertEqual(response["current_step"], "sample_output")

    def test_changed_output_after_spot_check_revokes_sample_and_delivery(self):
        self.reach_output()
        path = self.products()
        self.invoke("validate-output", "--products", path)
        self.passed("sample_output", self.evidence("source-check.txt"))
        path.write_text("[]", encoding="utf-8")
        code, response = self.invoke("status")
        self.assertEqual(code, 2)
        self.assertEqual(response["current_step"], "sample_output")
        self.assertIn("validated_output_changed_or_unavailable", response["errors"])
        code, response = self.passed("deliver")
        self.assertEqual(code, 2)
        self.assertEqual(response["errors"], ["step_out_of_order"])

    def test_route_fallback_rechecks_dependencies_and_preserves_history(self):
        code, response = self.invoke("init", "--target", "App", "--goal", "products", "--route", "emulator")
        self.assertEqual(code, 0, response)
        for step in ("target", "host_tools", "route_device"):
            self.passed(step, self.evidence(step + ".txt"))
        before = self.state()
        code, response = self.invoke("set-route", "--route", "physical", "--reason", "emulator-incompatible",
                                     "--evidence-file", self.evidence("incompatible.txt"))
        self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "route_device")
        self.assertEqual(response["route"], "physical")
        after = self.state()
        self.assertEqual(after["steps"]["target"], before["steps"]["target"])
        self.assertEqual(after["steps"]["host_tools"], before["steps"]["host_tools"])
        self.assertEqual(after["steps"]["route_device"]["evidence"], before["steps"]["route_device"]["evidence"])
        self.assertEqual(after["steps"]["route_device"]["status"], "pending")
        self.assertEqual(after["history"][-1]["evidence"]["kind"], "agent-observed")
        self.assertEqual(after["history"][:-1], before["history"])

    def test_route_fallback_cannot_override_physical_requirement(self):
        self.init("--require-physical")
        original = (self.work / session.STATE_FILE).read_bytes()
        code, response = self.invoke("set-route", "--route", "emulator", "--reason", "physical-unavailable",
                                     "--evidence-file", self.evidence("device.txt"))
        self.assertEqual(code, 2)
        self.assertIn("physical_device_requirement_cannot_use_other_route", response["errors"])
        self.assertEqual(original, (self.work / session.STATE_FILE).read_bytes())

    def test_route_change_after_entering_real_flow_requires_another_session(self):
        self.init()
        for step in session.STEPS[:session.STEPS.index("real_flow")]:
            self.passed(step, self.evidence(step + ".txt"))
        code, response = self.invoke("set-route", "--route", "emulator", "--reason", "user-selected",
                                     "--evidence-file", self.evidence("route.txt"))
        self.assertEqual(code, 2)
        self.assertIn("capture_stage_started_create_a_new_session_for_another_route", response["errors"])
        self.assertEqual(response["current_step"], "real_flow")
        self.passed("real_flow", self.evidence("flow.txt"))
        (self.work / "target.txt").write_text("new target observation")
        code, response = self.invoke("set-route", "--route", "emulator", "--reason", "user-selected",
                                     "--evidence-file", self.evidence("route.txt"))
        self.assertEqual(code, 2)
        self.assertIn("capture_stage_started_create_a_new_session_for_another_route", response["errors"])

    def test_examples_empty_wrong_shape_and_missing_names_are_rejected(self):
        self.reach_output()
        for rows in ({"products": []}, [], ["https://images.shop-cdn.cn/photo.jpg"],
                     [{"url": "https://images.shop-cdn.cn/photo.jpg"}],
                     [{"name": "示例", "url": "https://example.com/photo.jpg"}],
                     [{"name": "示例", "url": "https://fake.example.test/photo.jpg"}]):
            with self.subTest(rows=rows):
                code, response = self.invoke("validate-output", "--products", self.products(rows))
                self.assertEqual(code, 2)
                self.assertEqual(response["current_step"], "sample_output")
                self.assertEqual(response["business_success"], "not_auto_verified")
        self.assertFalse((self.work / "download_images.py").exists())

    def test_dangerous_url_and_metadata_values_are_never_output(self):
        self.reach_output()
        urls = ("https://user:TOP_SECRET@images.shop-cdn.cn/photo.jpg",
                "https://images.shop-cdn.cn/photo.jpg?access_token=TOP_SECRET",
                "https://images.shop-cdn.cn/photo.jpg?auth=TOP_SECRET",
                "https://images.shop-cdn.cn/account/photo.jpg",
                "https://images.shop-cdn.cn/login")
        for url in urls:
            code, response = self.invoke("validate-output", "--products", self.products([{"name": "cup", "url": url}]))
            self.assertEqual(code, 2)
            self.assertNotIn("TOP_SECRET", json.dumps(response))
            self.assertNotIn("TOP_SECRET", json.dumps(self.state()))
        for additional in ({"authorization": "TOP_SECRET"}, {"note": "Bearer TOP_SECRET"}):
            code, response = self.invoke("validate-output", "--products", self.products([
                {"name": "cup", "url": "https://images.shop-cdn.cn/photo.jpg", **additional}]))
            self.assertEqual(code, 2)
            self.assertNotIn("TOP_SECRET", json.dumps(response))

    def test_redacted_source_metadata_is_allowed_but_redacted_image_url_is_not(self):
        self.reach_output()
        path = self.products([{"name": "cup", "url": "https://images.shop-cdn.cn/photo.jpg",
                               "source": "https://api.shop-cdn.cn/catalog?token=%3Credacted%3E"}])
        code, response = self.invoke("validate-output", "--products", path)
        self.assertEqual(code, 0, response)
        path = self.products([{"name": "cup", "url": "https://images.shop-cdn.cn/photo.jpg?token=%3Credacted%3E"}])
        code, response = self.invoke("validate-output", "--products", path)
        self.assertEqual(code, 2)

    def test_request_summary_can_describe_login_with_redacted_auth_query(self):
        code, response = self.invoke("init", "--target", "App", "--goal", "requests", "--route", "browser")
        self.assertEqual(code, 0, response)
        for step in session.STEPS[:session.STEPS.index("sample_output")]:
            if step != "mapping":
                self.passed(step, self.evidence(step + ".txt"))
        path = self.work / "requests.json"
        path.write_text(json.dumps([{"name": "POST /login", "url": "https://api.shop-cdn.cn/login?auth=%3Credacted%3E",
                                    "method": "POST", "status": 200, "request_id": "flow-one",
                                    "response_summary": {"format": "json"}}]))
        code, response = self.invoke("validate-output", "--requests", path)
        self.assertEqual(code, 0, response)
        self.assertEqual(response["preview"]["mode"], "not_applicable")
        self.assertFalse((self.work / "download_images.py").exists())

    def test_signed_resource_review_does_not_allow_account_credentials(self):
        self.reach_output()
        path = self.products([{"name": "cup", "url": "https://images.shop-cdn.cn/photo.jpg?sig=signed-value",
                               "expiry_status": "not_checked"}])
        code, _ = self.invoke("validate-output", "--products", path)
        self.assertEqual(code, 2)
        code, response = self.invoke("validate-output", "--products", path, "--allow-signed-image-urls")
        self.assertEqual(code, 0, response)
        path = self.products([{"name": "cup", "url": "https://images.shop-cdn.cn/photo.jpg?sig=ok&token=TOP_SECRET",
                               "expiry_status": "not_checked"}])
        code, response = self.invoke("validate-output", "--products", path, "--allow-signed-image-urls")
        self.assertEqual(code, 2)
        self.assertNotIn("TOP_SECRET", json.dumps(response))

    def test_batch_requested_requires_its_own_observation(self):
        self.reach_output("--batch")
        self.invoke("validate-output", "--products", self.products())
        _, response = self.passed("sample_output")
        self.assertEqual(response["current_step"], "batch")
        code, _ = self.passed("deliver")
        self.assertEqual(code, 2)
        _, response = self.passed("batch")
        self.assertEqual(response["current_step"], "deliver")

    def begin_batch(self):
        self.reach_output("--batch")
        sample = self.products()
        self.invoke("validate-output", "--products", sample)
        code, response = self.passed("sample_output", self.evidence("sample-check.txt"))
        self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "batch")
        return sample

    def final_products(self):
        return self.products([
            {"name": "红色杯子", "id": "one", "url": "https://images.shop-cdn.cn/photo.jpg"},
            {"name": "蓝色杯子", "id": "two", "url": "https://images.shop-cdn.cn/photo.jpg"},
            {"name": "绿色杯子", "id": "three", "url": "https://images.shop-cdn.cn/green.jpg"}],
            name="final/products.json")

    def test_batch_final_validation_preserves_immutable_sample_and_history(self):
        sample = self.begin_batch()
        before = self.state()
        original = sample.read_bytes()
        final = self.final_products()
        code, response = self.invoke("validate-output", "--products", final)
        self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "batch")
        self.assertEqual(response["output_role"], "final")
        self.assertEqual(response["statistics"]["rows"], 3)
        after = self.state()
        self.assertEqual(after["output_validation"], before["output_validation"])
        self.assertEqual(after["history"][:-1], before["history"])
        self.assertEqual(after["final_output_validation"]["evidence"]["path"], "final/products.json")
        self.assertEqual(original, sample.read_bytes())
        self.assertTrue(final.with_name("download_images.py").exists())
        self.assertFalse(final.with_name("downloaded-images").exists())
        self.passed("batch", self.evidence("batch-check.txt"))
        code, response = self.passed("deliver", self.evidence("delivered.txt"))
        self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "restore")
        self.assertEqual(self.state()["output_validation"], before["output_validation"])

    def test_invalid_json_and_dangerous_urls_cannot_validate_batch_final(self):
        sample = self.begin_batch()
        before = self.state()
        final = self.final_products()
        final.write_text("not valid JSON", encoding="utf-8")
        code, response = self.invoke("validate-output", "--products", final)
        self.assertEqual(code, 2)
        self.assertEqual(response["current_step"], "batch")
        self.assertEqual(response["errors"], ["output_is_not_a_readable_json_array"])
        final.write_text(json.dumps([{"name": "cup", "url": "https://images.shop-cdn.cn/photo.jpg?token=TOP_SECRET"}]))
        code, response = self.invoke("validate-output", "--products", final)
        self.assertEqual(code, 2)
        self.assertNotIn("TOP_SECRET", json.dumps(response))
        self.assertIsNone(self.state()["final_output_validation"])
        self.assertEqual(before["output_validation"], self.state()["output_validation"])
        self.assertTrue(sample.is_file())

    def test_batch_delivery_requires_final_validation_and_can_validate_at_deliver(self):
        self.begin_batch()
        self.passed("batch", self.evidence("batch-check.txt"))
        code, response = self.passed("deliver", self.evidence("delivery-check.txt"))
        self.assertEqual(code, 2)
        self.assertEqual(response["current_step"], "deliver")
        self.assertEqual(response["errors"], ["final_output_validation_required_for_batch"])
        final = self.final_products()
        code, response = self.invoke("validate-output", "--products", final)
        self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "deliver")
        self.assertEqual(response["output_role"], "final")
        code, response = self.passed("deliver", self.evidence("delivery-check.txt"))
        self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "restore")

    def test_changed_final_after_complete_reopens_delivery_and_requires_observation(self):
        self.begin_batch()
        final = self.final_products()
        self.invoke("validate-output", "--products", final)
        self.passed("batch", self.evidence("batch-check.txt"))
        self.passed("deliver", self.evidence("delivery-check.txt"))
        code, response = self.passed("restore", self.evidence("restore-check.txt"))
        self.assertEqual(code, 0, response)
        self.assertTrue(response["workflow_complete"])
        rows = json.loads(final.read_text(encoding="utf-8"))
        rows.append({"name": "new observed cup", "url": "https://images.shop-cdn.cn/new.jpg"})
        final.write_text(json.dumps(rows), encoding="utf-8")
        code, response = self.invoke("status")
        self.assertEqual(code, 2)
        self.assertEqual(response["current_step"], "deliver")
        self.assertFalse(response["workflow_complete"])
        self.assertIn("validated_final_output_changed_or_unavailable", response["errors"])
        code, response = self.passed("deliver", self.evidence("delivery-check.txt"))
        self.assertEqual(code, 2)
        self.assertEqual(response["errors"], ["delivery_output_changed_or_unavailable"])
        # A fresh file check does not automatically reuse the old delivery or
        # restoration observation. Both are recorded again afterwards.
        code, response = self.invoke("validate-output", "--products", final)
        self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "deliver")
        self.assertEqual(self.state()["steps"]["restore"]["status"], "pending")
        code, response = self.passed("deliver", self.evidence("new-delivery-check.txt"))
        self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "restore")
        self.assertGreaterEqual(len(self.state()["steps"]["deliver"]["evidence"]), 3)

    def test_early_reobservation_clears_both_validations_but_keeps_history(self):
        self.begin_batch()
        self.invoke("validate-output", "--products", self.final_products())
        before = self.state()
        (self.work / "target.txt").write_text("A changed observed target context", encoding="utf-8")
        code, response = self.passed("target", self.work / "target.txt")
        self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "host_tools")
        after = self.state()
        self.assertIsNone(after["output_validation"])
        self.assertIsNone(after["final_output_validation"])
        self.assertEqual(after["history"][:-1], before["history"])

    def test_revalidating_changed_sample_requires_new_source_spot_check(self):
        self.reach_output()
        sample = self.products()
        self.invoke("validate-output", "--products", sample)
        self.passed("sample_output", self.evidence("sample-check.txt"))
        self.invoke("validate-output", "--products", self.final_products())
        before = self.state()
        rows = json.loads(sample.read_text(encoding="utf-8"))
        rows[0]["name"] = "a corrected observed name"
        sample.write_text(json.dumps(rows), encoding="utf-8")
        code, response = self.invoke("validate-output", "--products", sample)
        self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "sample_output")
        self.assertEqual(response["output_role"], "sample")
        after = self.state()
        self.assertIsNone(after["final_output_validation"])
        self.assertEqual(after["output_validation"]["source_spot_check"], "pending")
        self.assertEqual(after["steps"]["sample_output"]["evidence"], before["steps"]["sample_output"]["evidence"])

    def test_nonbatch_explicit_final_is_not_replaced_by_valid_sample_after_change(self):
        self.reach_output()
        sample = self.products()
        self.invoke("validate-output", "--products", sample)
        self.passed("sample_output", self.evidence("sample-check.txt"))
        final = self.final_products()
        code, response = self.invoke("validate-output", "--products", final)
        self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "deliver")
        final.write_text("[]", encoding="utf-8")
        code, response = self.passed("deliver", self.evidence("delivery-check.txt"))
        self.assertEqual(code, 2)
        self.assertEqual(response["errors"], ["delivery_output_changed_or_unavailable"])
        self.assertTrue(session.matches(self.state()["output_validation"]["evidence"], self.work))

    def test_pending_preserves_records_without_advance_and_invalid_reason_does_not_leak(self):
        self.init()
        self.passed("target", kind="user-reported")
        code, response = self.invoke("pending", "--step", "target", "--reason", "device-locked")
        self.assertEqual(code, 0)
        self.assertEqual(response["current_step"], "target")
        self.assertEqual(len(self.state()["steps"]["target"]["evidence"]), 1)
        code, response = self.invoke("pending", "--step", "target", "--reason", "password=TOP_SECRET")
        self.assertEqual(code, 2)
        self.assertNotIn("TOP_SECRET", json.dumps(response))

    def pending_flow(self):
        self.init()
        for step in session.STEPS[:session.STEPS.index("real_flow")]:
            code, response = self.passed(step, self.evidence(step + ".txt"))
            self.assertEqual(code, 0, response)
        code, response = self.invoke("pending", "--step", "real_flow", "--reason", "verification-failed")
        self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "real_flow")

    def recovered(self, path=None, kind="agent-observed"):
        return self.invoke("recovery", "--evidence-file", path or self.evidence("recovery-observed.txt"),
                           "--evidence-kind", kind)

    def test_failure_recovery_preserves_pending_business_steps_and_does_not_complete(self):
        self.pending_flow()
        before = self.state()
        path = self.evidence("restored-task-settings.txt", "Observed restoration of task changes to their original state.")
        code, response = self.recovered(path)
        self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "real_flow")
        self.assertEqual(response["pending_reason"], "verification-failed")
        self.assertFalse(response["workflow_complete"])
        self.assertEqual(response["success_route_restore"], "pending")
        self.assertEqual(response["failure_recovery"]["confirmation"], "agent-observed-recorded-not-auto-verified")
        self.assertEqual(response["failure_recovery"]["business_step_at_observation"], "real_flow")
        self.assertEqual(response["failure_recovery"]["evidence_integrity"], {"valid": 1, "changed_or_unavailable": 0})
        after = self.state()
        self.assertEqual(after["steps"], before["steps"])
        self.assertEqual(after["history"][:-1], before["history"])
        self.assertEqual(after["failure_recovery"]["evidence"][0]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertNotIn("Observed restoration of task changes", json.dumps(after))
        self.assertEqual(after["failure_recovery"]["accepted_evidence"], 0)

    def test_failure_recovery_user_and_file_reports_cannot_confirm_restoration(self):
        self.pending_flow()
        before_steps = self.state()["steps"]
        for count, kind in enumerate(("user-reported", "file-validated"), start=1):
            code, response = self.recovered(kind=kind)
            self.assertEqual(code, 2)
            self.assertEqual(response["errors"], ["agent_observation_required_to_confirm_failure_recovery"])
            self.assertEqual(response["current_step"], "real_flow")
            self.assertEqual(response["pending_reason"], "verification-failed")
            self.assertEqual(response["failure_recovery"]["confirmation"], "awaiting-agent-observation")
            self.assertEqual(response["failure_recovery"]["evidence_count"], count)
            self.assertIsNone(self.state()["failure_recovery"]["accepted_evidence"])
        code, response = self.recovered()
        self.assertEqual(code, 0, response)
        self.assertEqual(response["failure_recovery"]["evidence_count"], 3)
        self.assertEqual(self.state()["steps"], before_steps)
        self.assertEqual([item["kind"] for item in self.state()["failure_recovery"]["evidence"]],
                         ["user-reported", "file-validated", "agent-observed"])

    def test_failure_recovery_requires_nonempty_real_evidence_and_explicit_pending(self):
        self.init()
        code, response = self.recovered()
        self.assertEqual(code, 2)
        self.assertEqual(response["errors"], ["recovery_requires_explicitly_pending_business_stage"])
        self.invoke("pending", "--step", "target", "--reason", "tool-unavailable")
        for path in (self.work / "missing-restoration.txt", self.evidence("empty-restoration.txt", "")):
            code, response = self.recovered(path)
            self.assertEqual(code, 2)
            self.assertEqual(response["current_step"], "target")
            self.assertEqual(response["pending_reason"], "tool-unavailable")
            self.assertEqual(response["failure_recovery"]["evidence_count"], 0)

    def test_changed_failure_recovery_evidence_is_reported_without_advancing_stage(self):
        self.pending_flow()
        path = self.evidence("restore-settings.txt")
        self.recovered(path)
        path.write_text("Changed restoration evidence", encoding="utf-8")
        code, response = self.invoke("status")
        self.assertEqual(code, 2)
        self.assertEqual(response["current_step"], "real_flow")
        self.assertEqual(response["pending_reason"], "verification-failed")
        self.assertFalse(response["workflow_complete"])
        self.assertIn("failure_recovery_evidence_changed_or_unavailable", response["errors"])
        self.assertEqual(response["failure_recovery"]["confirmation"], "accepted-evidence-changed-or-unavailable")
        self.assertEqual(response["failure_recovery"]["evidence_integrity"]["changed_or_unavailable"], 1)
        code, response = self.recovered(path)
        self.assertEqual(code, 0, response)
        self.assertEqual(self.state()["failure_recovery"]["accepted_evidence"], 1)

    def test_failure_recovery_does_not_replace_success_route_restore(self):
        self.pending_flow()
        self.recovered()
        for step in ("real_flow", "required_body", "mapping"):
            self.passed(step, self.evidence(step + ".txt"))
        self.invoke("validate-output", "--products", self.products())
        self.passed("sample_output", self.evidence("sample-check.txt"))
        self.passed("deliver", self.evidence("delivery-check.txt"))
        self.invoke("pending", "--step", "restore", "--reason", "verification-failed")
        before = self.state()
        code, response = self.recovered()
        self.assertEqual(code, 2)
        self.assertEqual(response["current_step"], "restore")
        self.assertEqual(response["errors"], ["recovery_requires_explicitly_pending_business_stage"])
        self.assertEqual(self.state(), before)
        code, response = self.passed("restore", self.evidence("success-restore.txt"))
        self.assertEqual(code, 0, response)
        self.assertTrue(response["workflow_complete"])
        self.assertEqual(response["success_route_restore"], "recorded")
        self.assertEqual(response["failure_recovery"]["evidence_count"], 1)
        code, response = self.recovered()
        self.assertEqual(code, 2)
        self.assertEqual(response["current_step"], "complete")

    def test_missing_imported_capture_can_be_marked_pending_for_failure_recovery(self):
        capture = self.root / "observed.har"
        capture.write_text("Nonempty existing capture evidence", encoding="utf-8")
        code, response = self.invoke("init", "--target", "App", "--goal", "products",
            "--route", "existing-capture", "--capture-file", capture, "--capture-origin", "physical",
            "--capture-applicability", "target-and-goal-reviewed")
        self.assertEqual(code, 0, response)
        self.passed("target")
        capture.unlink()
        code, response = self.invoke("pending", "--step", "real_flow", "--reason", "evidence-missing")
        self.assertEqual(code, 0, response)
        code, response = self.recovered()
        self.assertEqual(code, 0, response)
        self.assertEqual(response["current_step"], "real_flow")
        self.assertEqual(response["pending_reason"], "evidence-missing")
        code, response = self.invoke("status")
        self.assertEqual(code, 2)
        self.assertIn("existing_capture_changed_or_unavailable", response["errors"])

    def test_existing_different_downloader_is_not_overwritten(self):
        self.reach_output()
        script = self.work / "download_images.py"
        script.write_text("keep this user's script")
        code, response = self.invoke("validate-output", "--products", self.products())
        self.assertEqual(code, 2)
        self.assertIn("existing_downloader_differs_do_not_overwrite", response["errors"])
        self.assertEqual(script.read_text(), "keep this user's script")

    def test_each_command_help_runs_in_isolated_python(self):
        for command in (None, "init", "status", "pass", "pending", "set-route", "recovery", "validate-output"):
            with self.subTest(command=command):
                args = [sys.executable, "-I", str(SCRIPT)]
                if command:
                    args.append(command)
                completed = subprocess.run([*args, "--help"], capture_output=True, text=True, timeout=30)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("usage:", completed.stdout)


if __name__ == "__main__":
    unittest.main()
