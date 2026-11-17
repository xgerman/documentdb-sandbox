#!/usr/bin/env python3
"""Disposable documentdb-local containers backed by explicit filesystem clones."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time


LABEL = "io.documentdb.sandbox"
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
LSN = re.compile(r"[0-9A-F]+/[0-9A-F]+\Z")


class Error(Exception):
    pass


def command(args, *, env=None, check=True):
    result = subprocess.run(
        [str(arg) for arg in args], capture_output=True, text=True, env=env
    )
    if check and result.returncode:
        raise Error(result.stderr.strip()[-2000:] or f"{args[0]} failed")
    return result


def write_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        json.dump(value, out, indent=2)
        out.write("\n")
    temporary.replace(path)


def valid_name(value):
    if not NAME.fullmatch(value):
        raise Error("Names must be 1-48 lowercase letters, digits, or hyphens.")
    return value


def validate_data(data):
    if not all((data / part).exists() for part in ("PG_VERSION", "global/pg_control", "pg_wal", "base")):
        raise Error("Not a complete PostgreSQL data directory.")
    for current, dirs, files in os.walk(data):
        for name in dirs + files:
            if (Path(current) / name).is_symlink():
                raise Error("Symlinks and external tablespaces are unsupported.")
    if any((data / "pg_tblspc").iterdir()):
        raise Error("External tablespaces are unsupported.")


def clone(source, destination):
    if destination.exists() or destination.is_symlink():
        raise Error("Clone destination already exists.")
    if sys.platform == "darwin":
        args = ["/bin/cp", "-cR", source, destination]
    elif sys.platform.startswith("linux"):
        args = ["cp", "--reflink=always", "-R", "--preserve=mode,timestamps", source, destination]
    else:
        raise Error("This prototype supports APFS and Linux reflink filesystems only.")
    command(args)


def clone_probe(root):
    with tempfile.TemporaryDirectory(prefix="probe-", dir=root) as temporary:
        source = Path(temporary) / "source"
        source.mkdir()
        (source / "file").write_bytes(os.urandom(8192))
        target = Path(temporary) / "target"
        clone(source, target)
        original = (source / "file").read_bytes()
        (target / "file").write_bytes(b"independent")
        if (source / "file").read_bytes() != original:
            raise Error("Clone writes changed the source.")


class Runtime:
    def __init__(self, executable):
        self.executable = executable

    def run(self, *args, **kwargs):
        return command([self.executable, *args], **kwargs)

    def inspect(self, identifier):
        return json.loads(self.run("inspect", identifier).stdout)[0]

    def helper(self, image, data, *args):
        return self.run(
            "run", "--rm", "--network", "none", "--user", "0",
            "--env", "LC_ALL=C", "--mount",
            f"type=bind,source={data},target=/data,ro",
            "--entrypoint", args[0], image, *args[1:]
        ).stdout

    def sql(self, container, sql):
        return self.run(
            "exec", container, "psql", "-X", "-v", "ON_ERROR_STOP=1",
            "-p", "9712", "-U", "documentdb", "-d", "postgres", "-Atqc", sql,
        ).stdout.strip()

    def stop(self, container):
        info = self.inspect(container)
        if info["State"]["Running"]:
            self.run(
                "exec", "--user", "documentdb", container, "pg_ctl",
                "-D", "/data", "-w", "-t", "60", "stop", "-m", "fast",
            )
            self.run("stop", "--time", "60", container)

    def clean(self, image, data):
        validate_data(data)
        control = self.helper(image, data, "pg_controldata", "/data")
        if not re.search(r"^Database cluster state:\s+shut down\s*$", control, re.M):
            raise Error("PostgreSQL was not cleanly shut down; refusing to clone.")
        return control

    def wait(self, container, credentials, timeout):
        deadline = time.monotonic() + timeout
        token = secrets.token_hex(12)
        javascript = (
            'const a=db.getSiblingDB("admin");'
            'if(!a.auth(process.env.SANDBOX_USER,process.env.SANDBOX_PASSWORD))quit(2);'
            f'const c=db.getSiblingDB("__sandbox_probe").getCollection("{token}");'
            'c.insertOne({_id:1,value:42});'
            'if(c.findOne({_id:1}).value!==42)quit(3);c.drop();'
        )
        env = {
            **os.environ,
            "SANDBOX_USER": credentials["username"],
            "SANDBOX_PASSWORD": credentials["password"],
        }
        while time.monotonic() < deadline:
            if not self.inspect(container)["State"]["Running"]:
                raise Error("Container exited during startup; retained for inspection.")
            # Older images do not contain the built-in probe.
            health = self.run(
                "exec", container, "bash", "-c",
                "if [ -x /usr/local/bin/documentdb-healthcheck ]; then "
                "/usr/local/bin/documentdb-healthcheck; else pg_isready -p 9712; fi",
                check=False,
            )
            if health.returncode == 0:
                probe = self.run(
                    "exec", "--env", "SANDBOX_USER", "--env", "SANDBOX_PASSWORD",
                    container, "mongosh", "--quiet",
                    "mongodb://127.0.0.1:10260/?tls=false&retryWrites=false"
                    "&serverSelectionTimeoutMS=2000",
                    "--eval", javascript, env=env, check=False,
                )
                if probe.returncode == 0:
                    return
            time.sleep(2)
        raise Error("Authenticated readiness timed out; container retained for inspection.")


class Sandbox:
    def __init__(self, root, runtime):
        self.root = root.resolve()
        self.runtime = runtime

    def record(self, kind, name):
        directory = self.root / kind / valid_name(name)
        if directory.is_symlink() or directory.resolve().parent != self.root / kind:
            raise Error("Resource path escapes the sandbox store.")
        return directory, json.loads((directory / "metadata.json").read_text())

    def seed(self, name, container):
        valid_name(name)
        destination = self.root / "seeds" / name
        if destination.exists():
            raise Error("Seed already exists.")
        info = self.runtime.inspect(container)
        settings = dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)
        if settings.get("DATA_PATH", "/data") != "/data":
            raise Error("This prototype requires DATA_PATH=/data.")
        for key, expected in (("OWNER", "documentdb"), ("POSTGRESQL_PORT", "9712"),
                              ("DOCUMENTDB_PORT", "10260"), ("TLS_MODE", "allowTLS")):
            if settings.get(key, expected) != expected:
                raise Error(f"This prototype requires {key}={expected}.")
        if not settings.get("USERNAME") or not settings.get("PASSWORD"):
            raise Error("Create the fixture with explicit disposable USERNAME and PASSWORD.")
        if info.get("HostConfig", {}).get("RestartPolicy", {}).get("Name", "no") not in ("no", "", "none"):
            raise Error("Disable the fixture container's automatic restart policy first.")
        mounts = info["Mounts"]
        data_mount = next((mount for mount in mounts if mount["Destination"] == "/data"), None)
        if not data_mount or data_mount["Type"] != "bind":
            raise Error("The fixture must bind-mount a host data directory at /data.")
        if any(mount["Destination"].startswith("/data/") for mount in mounts):
            raise Error("Nested data mounts are unsupported.")
        source = Path(data_mount["Source"]).resolve()
        image = info["Image"]
        # Test storage and host/VM path visibility before stopping the fixture.
        clone_probe(self.root)
        self.runtime.helper(image, source, "test", "-f", "/data/PG_VERSION")
        self.runtime.stop(container)
        self.runtime.clean(image, source)
        destination.mkdir(mode=0o700)
        metadata = {"seed": name, "image": image, "source_container": info["Id"]}
        try:
            clone(source, destination / "data")
            self.runtime.clean(image, destination / "data")
            write_json(destination / "credentials.json", {
                "username": settings["USERNAME"], "password": settings["PASSWORD"],
            })
            write_json(destination / "metadata.json", metadata)
        except Exception:
            shutil.rmtree(destination)
            raise
        return {**metadata, "source_left_stopped": True}

    def fork(self, seed_name, record_wal, timeout):
        seed, seed_info = self.record("seeds", seed_name)
        image = seed_info["image"]
        self.runtime.clean(image, seed / "data")
        identifier = "sandbox-" + secrets.token_hex(6)
        branch = self.root / "branches" / identifier
        branch.mkdir(mode=0o700)
        credentials = json.loads((seed / "credentials.json").read_text())
        metadata = {
            "id": identifier, "seed": seed_name, "image": image,
            "record_wal": record_wal, "state": "starting",
        }
        write_json(branch / "metadata.json", metadata)
        clone(seed / "data", branch / "data")
        if record_wal:
            archive = branch / "data" / ".sandbox-wal"
            archive.mkdir(mode=0o700)
            config = branch / "data" / "postgresql.auto.conf"
            with config.open("a") as out:
                out.write(
                    "\narchive_mode = on\n"
                    "archive_command = 'test -f \"/data/.sandbox-wal/%f\" || "
                    "cp \"%p\" \"/data/.sandbox-wal/%f\"'\n"
                )
            self.runtime.run(
                "run", "--rm", "--network", "none", "--user", "0",
                "--mount", f"type=bind,source={branch / 'data'},target=/data",
                "--entrypoint", "bash", image, "-c",
                "chown --reference=/data /data/.sandbox-wal /data/postgresql.auto.conf",
            )
        self.runtime.run(
            "run", "-d", "--name", identifier, "--restart", "no",
            "--label", f"{LABEL}={identifier}",
            "--publish", "127.0.0.1::10260",
            "--mount", f"type=bind,source={branch / 'data'},target=/data",
            "--env", "CREATE_USER=false", "--env", "ENABLE_TELEMETRY=false",
            "--env", "DOCUMENTDB_USAGE_TELEMETRY=false", image,
        )
        info = self.runtime.inspect(identifier)
        metadata["container"] = info["Id"]
        write_json(branch / "metadata.json", metadata)
        self.runtime.wait(identifier, credentials, timeout)
        if record_wal:
            self.runtime.sql(identifier, "SELECT pg_switch_wal()")
            metadata["from_lsn"] = self.runtime.sql(
                identifier, "SELECT pg_current_wal_insert_lsn()"
            )
        info = self.runtime.inspect(identifier)
        port = info["NetworkSettings"]["Ports"]["10260/tcp"][0]["HostPort"]
        metadata.update(state="ready", port=int(port))
        write_json(branch / "metadata.json", metadata)
        return {
            **metadata,
            "uri": f"mongodb://127.0.0.1:{port}/?tls=false&retryWrites=false",
            "credentials_file": str(seed / "credentials.json"),
        }

    def owned(self, identifier, metadata):
        result = self.runtime.run("inspect", identifier, check=False)
        if result.returncode:
            # A missing container is normal after manual removal or failed creation.
            exists = self.runtime.run("ps", "-a", "--format", "{{.Names}}")
            if identifier in exists.stdout.split():
                raise Error("Cannot inspect the sandbox container.")
            return None
        info = json.loads(result.stdout)[0]
        labels = info["Config"].get("Labels") or {}
        if labels.get(LABEL) != identifier or (
            metadata.get("container") and info["Id"] != metadata["container"]
        ):
            raise Error("Container identity/ownership does not match; refusing operation.")
        return info

    def discard(self, identifier):
        branch, metadata = self.record("branches", identifier)
        info = self.owned(identifier, metadata)
        if info:
            if info["State"]["Running"]:
                self.runtime.run("stop", "--time", "60", identifier)
            self.runtime.run("rm", identifier)
        shutil.rmtree(branch)
        return {"id": identifier, "discarded": True}

    def capture(self, identifier):
        branch, metadata = self.record("branches", identifier)
        if not metadata["record_wal"] or metadata["state"] != "ready":
            raise Error("Capture requires a ready sandbox forked with --record-wal.")
        info = self.owned(identifier, metadata)
        if not info or not info["State"]["Running"]:
            raise Error("Capture requires the sandbox to be running. Stop agent writers first.")
        start = metadata["from_lsn"]
        end = self.runtime.sql(identifier, "SELECT pg_current_wal_insert_lsn()")
        if not LSN.fullmatch(start) or not LSN.fullmatch(end) or lsn_number(start) >= lsn_number(end):
            raise Error("No nonempty valid capture range.")
        manifest = json.loads(self.runtime.sql(identifier, MANIFEST_SQL))
        manifest.update(validFromLsn=start, validToLsn=end)
        size = int(self.runtime.sql(
            identifier, "SELECT pg_size_bytes(current_setting('wal_segment_size'))"
        ))
        self.runtime.stop(identifier)
        self.runtime.clean(metadata["image"], branch / "data")
        output = branch / "capture"
        if output.exists():
            raise Error("Capture already exists.")
        output.mkdir(mode=0o700)
        wal = output / "wal"
        wal.mkdir(mode=0o700)
        try:
            first = lsn_number(start) // size
            last = (lsn_number(end) - 1) // size
            per_log = (1 << 32) // size
            for number in range(first, last + 1):
                name = f"{manifest['timeline']:08X}{number // per_log:08X}{number % per_log:08X}"
                source = next((
                    directory / name for directory in (
                        branch / "data" / ".sandbox-wal", branch / "data" / "pg_wal"
                    ) if (directory / name).is_file()
                ), None)
                if source is None or source.stat().st_size != size:
                    raise Error(f"Missing or incomplete WAL segment {name}; capture is unusable.")
                shutil.copyfile(source, wal / name)
            write_json(output / "manifest.json", manifest)
            result = {
                "id": identifier, "mode": "activity-review", "coverage": "best-effort",
                "from_lsn": start, "to_lsn": end,
                "postgres_major": manifest["postgresMajor"], "directory": str(output),
                "warning": "Final catalog mappings are not complete historical coverage. "
                           "Missing images/IDs or DDL may make output incomplete; not an exact diff.",
            }
            write_json(output / "review.json", result)
            metadata["state"] = "captured"
            write_json(branch / "metadata.json", metadata)
            return result
        except Exception:
            shutil.rmtree(output)
            raise


def lsn_number(value):
    high, low = value.split("/")
    return (int(high, 16) << 32) + int(low, 16)


# Adapted from documentdb-wal-audit/scripts/demo.sh (MIT, DocumentDB Contributors).
MANIFEST_SQL = """
SELECT json_build_object(
 'schemaVersion',2,
 'postgresMajor',current_setting('server_version_num')::int/10000,
 'systemIdentifier',(SELECT system_identifier::text FROM pg_control_system()),
 'timeline',(SELECT timeline_id FROM pg_control_checkpoint()),
 'documentdbVersion',(SELECT extversion FROM pg_extension WHERE extname='documentdb'),
 'relations',(SELECT json_agg(entry) FROM (
   SELECT json_build_object(
     'kind','collections_catalog',
     'tblspaceOid',COALESCE(NULLIF(c.reltablespace,0),d.dattablespace)::bigint,
     'databaseOid',d.oid::bigint,'relfilenode',pg_relation_filenode(c.oid)::bigint) entry
   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   JOIN pg_database d ON d.datname=current_database()
   WHERE n.nspname='documentdb_api_catalog' AND c.relname='collections'
   UNION ALL
   SELECT json_build_object(
     'kind','document',
     'tblspaceOid',COALESCE(NULLIF(c.reltablespace,0),d.dattablespace)::bigint,
     'databaseOid',d.oid::bigint,'relfilenode',pg_relation_filenode(c.oid)::bigint,
     'collectionId',m.collection_id,'database',m.database_name,'collection',m.collection_name)
   FROM documentdb_api_catalog.collections m
   JOIN pg_class c ON c.oid=to_regclass('documentdb_data.documents_' || m.collection_id)
   JOIN pg_database d ON d.datname=current_database()
 ) relations))
"""


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.home() / ".local/share/documentdb-sandbox")
    parser.add_argument("--runtime", default=os.environ.get("CONTAINER_RUNTIME") or
                        shutil.which("podman") or shutil.which("docker"))
    commands = parser.add_subparsers(dest="action", required=True)
    seed = commands.add_parser("seed", help="Stop a dedicated fixture container and clone its /data")
    seed.add_argument("name")
    seed.add_argument("container")
    fork = commands.add_parser("fork", help="Start a disposable clone")
    fork.add_argument("seed")
    fork.add_argument("--record-wal", action="store_true")
    fork.add_argument("--timeout", type=int, default=180)
    for action in ("discard", "capture"):
        sub = commands.add_parser(action, help="Discard a sandbox" if action == "discard"
                                  else "Stop writers first; seal WAL for best-effort activity review")
        sub.add_argument("id")
    args = parser.parse_args(argv)
    try:
        if not args.runtime:
            raise Error("Podman or Docker is required.")
        root = args.root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
            raise Error("The store must be owned by this user with mode 0700.")
        for kind in ("seeds", "branches"):
            directory = root / kind
            if directory.is_symlink():
                raise Error("Store directories must not be symlinks.")
            directory.mkdir(exist_ok=True, mode=0o700)
        with (root / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            runtime = Runtime(args.runtime)
            runtime.run("info")
            app = Sandbox(root, runtime)
            if args.action == "seed":
                result = app.seed(args.name, args.container)
            elif args.action == "fork":
                result = app.fork(args.seed, args.record_wal, args.timeout)
            elif args.action == "capture":
                result = app.capture(args.id)
            else:
                result = app.discard(args.id)
            print(json.dumps(result, indent=2))
        return 0
    except (Error, OSError, ValueError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
