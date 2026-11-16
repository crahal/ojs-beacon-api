from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/retire_lightsail_disk.sh"

# Exercise the actual standalone shell entry point with stateful command stubs.
# No AWS credentials, sockets or real cloud operations are used by these tests.
STUB = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

path = Path(os.environ["RETIRE_TEST_STATE"])
state = json.loads(path.read_text())
arguments = sys.argv[1:]
tool = Path(sys.argv[0]).name
state["calls"].append([tool, *arguments])

def finish(value=None, code=0):
    path.write_text(json.dumps(state))
    if value is not None:
        print(json.dumps(value))
    sys.exit(code)

if tool == "curl":
    mode = state.get("health", "ok")
    if mode == "bad_after_restart" and state.get("restarted"):
        mode = "bad"
    path.write_text(json.dumps(state))
    if mode == "tls_failure":
        sys.exit(60)
    print(json.dumps({"status": "ok" if mode != "bad" else "error"}))
    sys.stdout.write("302" if mode == "redirect" else "200")
    sys.exit(0)

service = "sts" if "sts" in arguments else "lightsail"
action = arguments[arguments.index(service) + 1]
if state.get("fail_action") == action:
    finish(code=1)
if state.get("malformed_action") == action:
    path.write_text(json.dumps(state))
    print("untrusted invalid JSON")
    sys.exit(0)
if action == "get-caller-identity":
    finish({"Account": state["account"]})
if action == "get-instance":
    finish({"instance": state["instance"]})
if action == "get-disk":
    name = arguments[arguments.index("--disk-name") + 1]
    disk = state["old"] if name == state["old"]["name"] else state["keep"]
    finish({"disk": disk})
if action == "get-disks":
    disks = [state["keep"]]
    if state["old"]["state"] != "deleted":
        disks.append(state["old"])
    finish({"disks": disks})
if action == "get-operation":
    finish({"operation": state["operation"]})

kind = {"stop-instance": "StopInstance", "detach-disk": "DetachDisk",
        "start-instance": "StartInstance", "delete-disk": "DeleteDisk"}[action]
if action == "stop-instance":
    state["instance"]["state"]["name"] = "stopped"
elif action == "detach-disk":
    assert state["instance"]["state"]["name"] == "stopped"
    state["old"].update(state="available", isAttached=False, attachedTo="")
elif action == "start-instance":
    assert state["old"]["state"] == "available"
    state["instance"]["state"]["name"] = "running"
    state["restarted"] = True
elif action == "delete-disk":
    assert state["old"]["state"] == "available"
    assert "--no-force-delete-add-ons" in arguments
    state["old"]["state"] = "deleting" if state.get("delete_stuck") else "deleted"
name = state["instance"]["name"] if action.endswith("instance") else state["old"]["name"]
state["operation"] = {"id": "fixture-operation", "resourceName": name,
    "operationType": kind, "location": {"regionName": "eu-west-2"},
    "status": "Failed" if state.get("failed_operation") == action else "Succeeded",
    "isTerminal": True}
if state.get("replace_keep_after") == action:
    state["keep"]["arn"] += "replacement"
finish({"operations": [state["operation"]]})
'''


class RetireLightsailDiskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.directory.chmod(0o700)
        self.receipt = self.directory / "receipt.json"
        self.state_path = self.directory / "state.json"
        bin_directory = self.directory / "bin"
        bin_directory.mkdir()
        for name in ("aws", "curl"):
            executable = bin_directory / name
            executable.write_text(STUB)
            executable.chmod(0o700)
        self.environment = dict(os.environ, PATH=str(bin_directory) + os.pathsep + os.environ["PATH"],
                                RETIRE_TEST_STATE=str(self.state_path))
        location = {"regionName": "eu-west-2", "availabilityZone": "eu-west-2a"}
        account = "0" * 12
        instance = {"name": "fixture-instance", "arn": f"arn:aws:lightsail:eu-west-2:{account}:Instance/fixture-i",
                    "location": location, "resourceType": "Instance", "state": {"name": "running"}}
        disks = []
        for name, size in (("fixture-old", 256), ("fixture-keep", 512)):
            disks.append({"name": name, "arn": f"arn:aws:lightsail:eu-west-2:{account}:Disk/{name}",
                          "location": location, "resourceType": "Disk", "sizeInGb": size,
                          "isSystemDisk": False, "isAttached": True, "attachedTo": instance["name"],
                          "state": "in-use"})
        self.state_path.write_text(json.dumps({"account": account, "instance": instance,
                                              "old": disks[0], "keep": disks[1], "calls": []}))

    def state(self):
        return json.loads(self.state_path.read_text())

    def alter(self, **values):
        state = self.state()
        state.update(values)
        self.state_path.write_text(json.dumps(state))

    def invoke(self, stage, *extra):
        options = ["--receipt", str(self.receipt), "--wait-seconds", "1"]
        if stage == "detach":
            options += ["--region", "eu-west-2", "--instance", "fixture-instance", "--old-disk", "fixture-old",
                        "--keep-disk", "fixture-keep", "--keep-uuid", "00000000-0000-0000-0000-000000000000",
                        "--api-origin", "https://api.example.invalid"]
        return subprocess.run(["bash", str(SCRIPT), stage, *options, *extra], env=self.environment,
                              text=True, capture_output=True, timeout=20)

    def mutations(self):
        actions = {"stop-instance", "detach-disk", "start-instance", "delete-disk"}
        return [next(item for item in call if item in actions)
                for call in self.state()["calls"] if any(item in actions for item in call)]

    def detach(self):
        result = self.invoke("detach", "--maintenance-ready")
        self.assertEqual(result.returncode, 0, result.stderr)

    def delete(self):
        return self.invoke("delete", "--verified-detached-boot-mount-api-history")

    def test_two_separate_stages_and_private_immutable_identity_receipt(self):
        self.detach()
        self.assertEqual(self.mutations(), ["stop-instance", "detach-disk", "start-instance"])
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt["stage"], "detached_restarted_healthy")
        self.assertEqual(stat.S_IMODE(self.receipt.stat().st_mode), 0o600)
        self.assertEqual(receipt["keep_disk_arn"], self.state()["keep"]["arn"])
        result = self.delete()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.mutations(), ["stop-instance", "detach-disk", "start-instance", "delete-disk"])
        self.assertEqual(json.loads(self.receipt.read_text())["stage"], "deleted")

    def test_missing_maintenance_ack_never_calls_cloud(self):
        self.assertNotEqual(self.invoke("detach").returncode, 0)
        self.assertEqual(self.state()["calls"], [])

    def test_missing_independent_ack_preserves_detached_disk(self):
        self.detach()
        before = self.state()["calls"]
        self.assertNotEqual(self.invoke("delete").returncode, 0)
        self.assertEqual(self.state()["calls"], before)
        self.assertNotIn("delete-disk", self.mutations())

    def test_missing_or_failed_aws_evidence_never_stops_instance(self):
        for mode in ("fail_action", "malformed_action"):
            with self.subTest(mode=mode):
                self.alter(**{mode: "get-disk"})
                self.assertNotEqual(self.invoke("detach", "--maintenance-ready").returncode, 0)
                self.assertEqual(self.mutations(), [])
                self.alter(**{mode: None})

    def test_wrong_disk_size_system_disk_or_attachment_is_refused(self):
        original = self.state()["old"]
        for override in ({"sizeInGb": 512}, {"isSystemDisk": True}, {"attachedTo": "other-instance"},
                         {"name": "other-disk"}):
            with self.subTest(override=override):
                self.alter(old={**original, **override})
                self.assertNotEqual(self.invoke("detach", "--maintenance-ready").returncode, 0)
                self.assertEqual(self.mutations(), [])
        self.alter(old=original)

    def test_changed_retained_arn_during_stop_blocks_detach(self):
        self.alter(replace_keep_after="stop-instance")
        result = self.invoke("detach", "--maintenance-ready")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutations(), ["stop-instance"])
        self.assertEqual(self.state()["old"]["state"], "in-use")

    def test_failed_aws_stop_operation_never_detaches(self):
        self.alter(failed_operation="stop-instance")
        self.assertNotEqual(self.invoke("detach", "--maintenance-ready").returncode, 0)
        self.assertEqual(self.mutations(), ["stop-instance"])

    def test_https_errors_before_stop_and_before_delete_preserve_disk(self):
        for health in ("bad", "tls_failure", "redirect"):
            with self.subTest(health=health):
                self.alter(health=health)
                self.assertNotEqual(self.invoke("detach", "--maintenance-ready").returncode, 0)
                self.assertEqual(self.mutations(), [])
        self.alter(health="ok")
        self.detach()
        for health in ("bad", "tls_failure", "redirect"):
            with self.subTest(delete_health=health):
                self.alter(health=health)
                self.assertNotEqual(self.delete().returncode, 0)
                self.assertNotIn("delete-disk", self.mutations())

    def test_post_restart_health_failure_preserves_old_disk_and_blocks_delete(self):
        self.alter(health="bad_after_restart")
        self.assertNotEqual(self.invoke("detach", "--maintenance-ready").returncode, 0)
        self.assertEqual(self.state()["old"]["state"], "available")
        self.assertEqual(json.loads(self.receipt.read_text())["stage"], "detached_restarted")
        self.alter(health="ok")
        self.assertNotEqual(self.delete().returncode, 0)
        self.assertNotIn("delete-disk", self.mutations())

    def test_replacement_old_disk_or_instance_blocks_delete(self):
        self.detach()
        for key in ("old", "instance", "keep"):
            with self.subTest(key=key):
                original = self.state()[key]
                self.alter(**{key: {**original, "arn": original["arn"] + "replacement"}})
                self.assertNotEqual(self.delete().returncode, 0)
                self.assertNotIn("delete-disk", self.mutations())
                self.alter(**{key: original})

    def test_wrong_account_or_reattached_old_disk_blocks_delete(self):
        self.detach()
        account = self.state()["account"]
        self.alter(account="1" * 12)
        self.assertNotEqual(self.delete().returncode, 0)
        self.alter(account=account)
        old = self.state()["old"]
        self.alter(old={**old, "state": "in-use", "isAttached": True, "attachedTo": "fixture-instance"})
        self.assertNotEqual(self.delete().returncode, 0)
        self.assertNotIn("delete-disk", self.mutations())

    def test_deleting_disk_is_not_reported_as_completed(self):
        self.detach()
        self.alter(delete_stuck=True)
        result = self.delete()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.receipt.read_text())["stage"], "delete_requested")
        self.assertNotIn("deletion completed", result.stdout)

    def test_addon_or_aws_delete_refusal_is_not_retried_with_force(self):
        self.detach()
        self.alter(fail_action="delete-disk")
        self.assertNotEqual(self.delete().returncode, 0)
        self.assertEqual(self.state()["old"]["state"], "available")
        self.assertEqual(self.mutations().count("delete-disk"), 1)
        self.assertEqual(json.loads(self.receipt.read_text())["stage"], "delete_requested")
        calls = self.state()["calls"]
        self.assertTrue(all("--force-delete-add-ons" not in call for call in calls))

    def test_private_receipt_directory_and_file_are_mandatory(self):
        self.directory.chmod(0o755)
        self.assertNotEqual(self.invoke("detach", "--maintenance-ready").returncode, 0)
        self.assertEqual(self.state()["calls"], [])
        self.directory.chmod(0o700)
        self.detach()
        self.receipt.chmod(0o644)
        self.assertNotEqual(self.delete().returncode, 0)
        self.assertNotIn("delete-disk", self.mutations())

    def test_existing_receipt_is_never_overwritten_or_used_to_repeat_detach(self):
        self.detach()
        before = self.receipt.read_bytes()
        calls = self.state()["calls"]
        self.assertNotEqual(self.invoke("detach", "--maintenance-ready").returncode, 0)
        self.assertEqual(self.receipt.read_bytes(), before)
        self.assertEqual(self.state()["calls"], calls)

    def test_untrusted_origin_forms_refused_before_aws(self):
        for origin in ("http://api.example.invalid", "https://user@api.example.invalid",
                       "https://api.example.invalid/?token=private", "https://api.example.invalid/#fragment",
                       "https://api.example.invalid/", "https://api.example.invalid:0"):
            with self.subTest(origin=origin):
                self.assertNotEqual(self.invoke("detach", "--maintenance-ready", "--api-origin", origin).returncode, 0)
                self.assertEqual(self.state()["calls"], [])

    def test_health_uses_only_exact_origin_with_no_redirects_or_tls_bypass(self):
        self.detach()
        calls = [call for call in self.state()["calls"] if call[0] == "curl"]
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call[1], "-q")
            self.assertEqual(call[-1], "https://api.example.invalid/health")
            self.assertIn("--noproxy", call)
            self.assertIn("--max-redirs", call)
            self.assertNotIn("-k", call)
            self.assertNotIn("--insecure", call)
            self.assertNotIn("--location", call)


if __name__ == "__main__":
    unittest.main()
