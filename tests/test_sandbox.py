import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import sandbox


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "branches").mkdir()
        (self.root / "seeds").mkdir()
        self.runtime = Mock()
        self.app = sandbox.Sandbox(self.root, self.runtime)

    def branch(self, name="sandbox-example", **extra):
        path = self.root / "branches" / name
        path.mkdir()
        metadata = {"id": name, "container": "original", **extra}
        sandbox.write_json(path / "metadata.json", metadata)
        return path, metadata

    def test_resource_names_cannot_escape_store(self):
        for value in ("../outside", "/tmp/elsewhere", "", "a/b"):
            with self.subTest(value=value), self.assertRaises(sandbox.Error):
                self.app.record("branches", value)

    def test_resource_symlinks_are_rejected(self):
        (self.root / "branches" / "outside").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(sandbox.Error):
            self.app.record("branches", "outside")

    def test_discard_refuses_replaced_container(self):
        path, _ = self.branch()
        self.runtime.run.return_value = subprocess.CompletedProcess([], 0, json.dumps([{
            "Id": "replacement", "Config": {"Labels": {sandbox.LABEL: "sandbox-example"}}
        }]), "")
        with self.assertRaises(sandbox.Error):
            self.app.discard("sandbox-example")
        self.assertTrue(path.exists())
        self.runtime.stop.assert_not_called()

    def test_discard_removes_only_owned_branch(self):
        path, _ = self.branch()
        sibling, _ = self.branch("sandbox-sibling")
        self.runtime.run.return_value = subprocess.CompletedProcess([], 0, json.dumps([{
            "Id": "original", "Config": {"Labels": {sandbox.LABEL: "sandbox-example"}},
            "State": {"Running": True},
        }]), "")
        self.app.discard("sandbox-example")
        self.assertFalse(path.exists())
        self.assertTrue(sibling.exists())

    def test_clean_requires_shutdown_state_not_just_missing_pid(self):
        with patch("sandbox.validate_data"):
            runtime = sandbox.Runtime("unused")
            with patch.object(runtime, "helper", return_value="Database cluster state: in production\n"):
                with self.assertRaises(sandbox.Error):
                    runtime.clean("image", self.root)

    def test_seed_clone_failure_never_publishes_seed(self):
        source = self.root / "fixture"
        source.mkdir()
        self.runtime.inspect.return_value = {
            "Id": "source", "Image": "sha256:image",
            "Config": {"Env": ["USERNAME=fixture", "PASSWORD=disposable"]},
            "Mounts": [{"Destination": "/data", "Type": "bind", "Source": str(source)}],
            "HostConfig": {"RestartPolicy": {"Name": "no"}},
        }
        with patch("sandbox.clone_probe"), patch("sandbox.clone", side_effect=sandbox.Error("failed")):
            with self.assertRaises(sandbox.Error):
                self.app.seed("fixture", "source")
        self.assertFalse((self.root / "seeds" / "fixture").exists())

    def test_seed_rejects_named_volumes_before_stopping(self):
        self.runtime.inspect.return_value = {
            "Config": {"Env": ["USERNAME=fixture", "PASSWORD=disposable"]},
            "HostConfig": {},
            "Mounts": [{"Destination": "/data", "Type": "volume"}],
        }
        with self.assertRaises(sandbox.Error):
            self.app.seed("fixture", "source")
        self.runtime.stop.assert_not_called()

    def test_capture_rejects_missing_wal_and_does_not_publish_partial_output(self):
        branch, metadata = self.branch(record_wal=True, state="ready", from_lsn="0/1000000",
                                       image="image")
        self.runtime.sql.side_effect = [
            "0/1000010", json.dumps({"timeline": 1, "postgresMajor": 17}), str(16 * 1024 * 1024),
        ]
        with patch.object(self.app, "owned", return_value={"State": {"Running": True}}):
            with self.assertRaisesRegex(sandbox.Error, "Missing or incomplete WAL"):
                self.app.capture("sandbox-example")
        self.assertFalse((branch / "capture").exists())
        self.assertEqual(json.loads((branch / "metadata.json").read_text()), metadata)

    def test_capture_exports_only_required_segments_and_marks_best_effort(self):
        branch, _ = self.branch(record_wal=True, state="ready", from_lsn="0/10", image="image")
        wal = branch / "data" / "pg_wal"
        wal.mkdir(parents=True)
        (wal / "000000010000000000000000").write_bytes(b"x" * 256)
        (wal / "000000010000000000000001").write_bytes(b"not in range")
        self.runtime.sql.side_effect = [
            "0/20", json.dumps({"timeline": 1, "postgresMajor": 17}), "256",
        ]
        with patch.object(self.app, "owned", return_value={"State": {"Running": True}}):
            result = self.app.capture("sandbox-example")
        self.assertEqual(result["coverage"], "best-effort")
        self.assertEqual(
            [file.name for file in (branch / "capture" / "wal").iterdir()],
            ["000000010000000000000000"],
        )
        self.assertEqual(json.loads((branch / "capture" / "manifest.json").read_text())["validToLsn"], "0/20")

    def test_clone_requests_explicit_cow(self):
        with patch("sandbox.command") as run:
            with patch("sandbox.sys.platform", "darwin"):
                sandbox.clone(self.root / "source", self.root / "target")
                self.assertIn("-cR", run.call_args.args[0])
            with patch("sandbox.sys.platform", "linux"):
                sandbox.clone(self.root / "source", self.root / "target")
                self.assertIn("--reflink=always", run.call_args.args[0])

    def test_clone_rejects_dangling_destination_symlink(self):
        destination = self.root / "target"
        destination.symlink_to(self.root / "missing")
        with patch("sandbox.command") as run:
            with self.assertRaises(sandbox.Error):
                sandbox.clone(self.root / "source", destination)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
