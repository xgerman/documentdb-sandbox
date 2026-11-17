"""Opt-in integration test using only new, uniquely named resources."""

import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
import unittest

import sandbox


@unittest.skipUnless(os.environ.get("SANDBOX_LIVE_TEST") == "1", "opt-in container integration")
class LiveTests(unittest.TestCase):
    def test_seed_sibling_isolation_and_wal_capture(self):
        executable = os.environ.get("CONTAINER_RUNTIME") or shutil.which("podman") or shutil.which("docker")
        runtime = sandbox.Runtime(executable)
        base = Path(__file__).resolve().parents[1] / ".test-data"
        base.mkdir(exist_ok=True, mode=0o700)
        temporary = tempfile.TemporaryDirectory(prefix="live-", dir=base)
        root = Path(temporary.name)
        data = root / "fixture"
        data.mkdir(mode=0o700)
        store = root / "store"
        store.mkdir(mode=0o700)
        for kind in ("seeds", "branches"):
            (store / kind).mkdir(mode=0o700)
        fixture = "sandbox-fixture-" + secrets.token_hex(5)
        credentials = {"username": "sandbox_fixture", "password": secrets.token_hex(24)}
        image = os.environ.get(
            "DOCUMENTDB_IMAGE",
            "ghcr.io/documentdb/documentdb/documentdb-local:pg17-0.114.0",
        )
        branches = []
        created = False

        def cli(*args):
            result = sandbox.command([
                sys.executable, Path(sandbox.__file__), "--root", store,
                "--runtime", executable, *args,
            ])
            if credentials["password"] in result.stdout + result.stderr:
                self.fail("CLI leaked the fixture password.")
            return json.loads(result.stdout)

        def wire(container, javascript):
            auth = (
                'if(!db.getSiblingDB("admin").auth('
                'process.env.SANDBOX_USER,process.env.SANDBOX_PASSWORD))quit(2);'
            )
            result = runtime.run(
                "exec", "--env", "SANDBOX_USER", "--env", "SANDBOX_PASSWORD",
                container, "mongosh", "--quiet",
                "mongodb://127.0.0.1:10260/?tls=false&retryWrites=false",
                "--eval", auth + javascript,
                env={**os.environ, "SANDBOX_USER": credentials["username"],
                     "SANDBOX_PASSWORD": credentials["password"]},
            )
            return result.stdout.strip()

        try:
            runtime.run(
                "run", "-d", "--name", fixture, "--restart", "no",
                "--label", f"{sandbox.LABEL}={fixture}",
                "--mount", f"type=bind,source={data},target=/data",
                "--env", "USERNAME", "--env", "PASSWORD",
                "--env", "INIT_DATA=false", "--env", "ENABLE_TELEMETRY=false",
                "--env", "DOCUMENTDB_USAGE_TELEMETRY=false", image,
                env={**os.environ, "USERNAME": credentials["username"],
                     "PASSWORD": credentials["password"]},
            )
            created = True
            runtime.wait(fixture, credentials, 180)
            wire(fixture, 'const c=db.getSiblingDB("fixture").items;'
                 'c.insertOne({_id:"one",value:1});c.createIndex({value:1});')
            cli("seed", "eval", fixture)
            self.assertFalse(runtime.inspect(fixture)["State"]["Running"])
            for record in (True, False):
                result = cli("fork", "eval", *(["--record-wal"] if record else []))
                branches.append(result["id"])
                self.assertEqual(Path(result["credentials_file"]).stat().st_mode & 0o777, 0o600)
            first, second = branches
            self.assertEqual(wire(second, 'print(db.getSiblingDB("fixture").items.findOne().value)'), "1")
            wire(first, 'const c=db.getSiblingDB("fixture").items;'
                 'c.updateOne({_id:"one"},{$set:{value:2}});'
                 'c.insertOne({_id:"two",value:3});')
            self.assertEqual(wire(first, 'print(db.getSiblingDB("fixture").items.findOne({_id:"one"}).value)'), "2")
            self.assertEqual(wire(second, 'print(db.getSiblingDB("fixture").items.findOne({_id:"one"}).value)'), "1")
            self.assertEqual(wire(second, 'print(db.getSiblingDB("fixture").items.getIndexes().length)'), "2")
            captured = cli("capture", first)
            self.assertEqual(captured["coverage"], "best-effort")
            manifest = json.loads((Path(captured["directory"]) / "manifest.json").read_text())
            self.assertTrue(any(relation.get("collection") == "items" for relation in manifest["relations"]))
            self.assertTrue(list((Path(captured["directory"]) / "wal").iterdir()))
            audit_image = os.environ.get("SANDBOX_AUDIT_IMAGE")
            if audit_image:
                output = Path(captured["directory"])
                spill = output / "spill"
                spill.mkdir(mode=0o700)
                decoded = runtime.run(
                    "run", "--rm", "--network", "none", "--user", "0",
                    "--mount", f"type=bind,source={output},target=/capture,ro",
                    "--mount", f"type=bind,source={spill},target=/spill",
                    audit_image, "--wal-dir", "/capture/wal",
                    "--manifest", "/capture/manifest.json", "--spill-dir", "/spill",
                    "--from-lsn", captured["from_lsn"], "--to-lsn", captured["to_lsn"],
                    "--format", "jsonl",
                )
                events = [json.loads(line) for line in decoded.stdout.splitlines()]
                effects = [event for event in events if event.get("eventType") == "document"
                           and event.get("outcome") == "committed"
                           and event.get("database") == "fixture"]
                self.assertTrue(any(event["operation"] == "insert" for event in effects))
                self.assertTrue(any(event["operation"] == "update" for event in effects))
            # A fresh fork must still see the unchanged seed.
            third = cli("fork", "eval")["id"]
            branches.append(third)
            self.assertEqual(wire(third, 'print(db.getSiblingDB("fixture").items.findOne({_id:"one"}).value)'), "1")
            report = {"runtime": executable, "image": image, "forks": len(branches),
                      "wal_segments": len(list((Path(captured["directory"]) / "wal").iterdir())),
                      "audit_decoded": bool(audit_image)}
            print("\nLive result:", json.dumps(report))
        finally:
            # Include failed forks whose startup did not return an ID.
            for directory in (store / "branches").iterdir():
                cli("discard", directory.name)
            if created:
                runtime.run("stop", "--time", "60", fixture)
                runtime.run("rm", fixture)
            temporary.cleanup()
