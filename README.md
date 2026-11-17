# documentdb-sandbox

A small prototype: **filesystem clones + `documentdb-local` containers + an
agent skill**. Fork an isolated development dataset, experiment, then discard
it. Optional WAL capture feeds the existing
[documentdb-wal-audit](https://github.com/xgerman/documentdb-wal-audit) tool for
best-effort activity review.

No extension changes, Python packages, daemon, or host PostgreSQL installation.
Not a production backup tool or a security boundary for hostile agents.

## Requirements

- Python 3.10+, Podman or Docker running.
- macOS APFS host directories shared with the container VM, or a Linux
  filesystem supporting reflinks.
- A locally available `documentdb-local` image, using `/data`, PostgreSQL port
  `9712`, wire port `10260`, and its standard `documentdb` OS user.
- Trusted, sanitized fixtures with explicit disposable credentials.

Podman is preferred when both runtimes are installed. Set `CONTAINER_RUNTIME`
or pass `--runtime`. Use a private `--root` directory (mode 0700) to override
`~/.local/share/documentdb-sandbox`.

Cloning runs on the host filesystem. Docker image layers and named volumes
do not imply clone support. There is deliberately no silent full-copy fallback.
The host directory must refer to the same files inside the selected local
Podman machine or Docker Desktop environment; remote daemons are unsupported.

## Quick start

Create a **new dedicated fixture**, not an existing service. This example uses
the public PostgreSQL 17 image; `seed` records its immutable local image ID so
forks use the identical runtime.

```bash
export CONTAINER_RUNTIME=podman # or docker
export USERNAME=sandbox_user
export PASSWORD="$(openssl rand -hex 24)"
mkdir -p "$HOME/.local/share/docdb-fixture"

"$CONTAINER_RUNTIME" run -d --name docdb-sandbox-fixture --restart=no \
  -e USERNAME -e PASSWORD -e INIT_DATA=false \
  -e ENABLE_TELEMETRY=false -e DOCUMENTDB_USAGE_TELEMETRY=false \
  -v "$HOME/.local/share/docdb-fixture:/data" \
  ghcr.io/documentdb/documentdb/documentdb-local:pg17-0.114.0
```

Wait for initialization. Populate this fixture through your preferred
wire-protocol client, or its bundled shell using `podman exec`.
Use only a dataset safe to copy. No host port is needed for initialization.

```bash
python3 sandbox.py seed eval-v1 docdb-sandbox-fixture
python3 sandbox.py fork eval-v1
# JSON: id, loopback URI, and credentials_file (never a password).

python3 sandbox.py discard sandbox-<returned-id>
```

Seeding **stops the source fixture and leaves it stopped**, verifies PostgreSQL
clean shutdown, then creates a separate CoW seed. Forks preserve data, indexes,
and initialization markers. They skip user creation and inherit the fixture's
disposable credentials. `credentials.json` is private; never commit or paste it
into an agent transcript. Secrets are not included in command JSON output.

Each fork starts a separate container with only its own `/data` mounted and a
loopback-published wire port. An authenticated insert/read/drop probe gates
readiness. Older images without the built-in healthcheck use PostgreSQL
readiness plus the same wire probe. TLS is disabled for this loopback-only
prototype; do not expose these endpoints remotely.

Failed forks remain in the private store for inspection; their directory name
is their sandbox ID and can be passed to `discard`. Only labeled, identity-
matching containers and the selected branch directory are removed. A missing
container does not prevent deleting its recorded branch files.

## Optional WAL activity review

This is **not an exact diff or an Argon replacement**. Capture discovers
physical committed effects where decoding is possible. It does not merge
documents or promise complete coverage.

```bash
python3 sandbox.py fork eval-v1 --record-wal
# Run the experiment, then STOP ALL CLIENT WRITERS.
python3 sandbox.py capture sandbox-<returned-id>
```

`--record-wal` enables a private archive inside that branch before startup.
Capture records the upper boundary, exports current relation mappings, stops
PostgreSQL, and copies every required closed/immutable segment into a private
`capture/wal` directory. Missing segments fail the operation. Capture leaves
the container stopped. The capture range starts after authenticated fork
readiness and ends before capture's catalog queries; it is not a trace of seed
creation or startup. Writers must be quiesced by the caller.

Build the existing audit project's runtime image for the `postgres_major`
returned by capture, following that project's instructions. For example, from
a checkout of **that project**, not this one:

```bash
podman build --target runtime --build-arg PG_MAJOR=17 \
  -t localhost/documentdb-wal-audit:pg17 .
```

Decode the capture (substitute its returned directory):

```bash
CAPTURE=/absolute/path/to/branches/sandbox-<id>/capture
mkdir -m 700 "$CAPTURE/spill"
read -r FROM_LSN TO_LSN < <(python3 -c \
  'import json,sys; r=json.load(open(sys.argv[1])); print(r["from_lsn"],r["to_lsn"])' \
  "$CAPTURE/review.json")

podman run --rm --network none --user 0 \
  -v "$CAPTURE:/capture:ro" -v "$CAPTURE/spill:/spill:rw" \
  localhost/documentdb-wal-audit:pg17 \
  --wal-dir /capture/wal --manifest /capture/manifest.json \
  --from-lsn "$FROM_LSN" --to-lsn "$TO_LSN" \
  --spill-dir /spill --format jsonl > "$CAPTURE/activity.jsonl"
```

Check the decoder exit status **before** interpreting output: errors can follow
partial output. Read `review.json`, event warnings, and certainty. Consider only
committed document/catalog effects. Full TOAST reconstruction, complete
maintenance coverage, and historical relation mapping are not guaranteed.
The manifest is a final-catalog snapshot, not evidence that names were valid
throughout the range. DDL, rewrites, missing IDs/images, and decoder failures
can make the report incomplete. `--strict` detects reported reconstruction
gaps; success still does not establish complete namespace coverage.

Each branch has its own archive/cache namespace because cloned PostgreSQL
clusters can share system identifier, timeline, and LSNs while their WAL
diverges. Never combine sibling archives. Archives can grow without bound;
keep experiments short, monitor free disk, and discard when finished.

An eventual exact diff should read base/final documents, use WAL only to narrow
candidates when coverage is proven, and otherwise scan more broadly.

## Agent skill

The workflow is in
[`.github/skills/documentdb-sandbox/SKILL.md`](.github/skills/documentdb-sandbox/SKILL.md).
Load it using your agent's skill mechanism or place it in your workspace's
supported skills directory, preserving access to this project's `sandbox.py`.
It orchestrates existing commands; it is not an enforcement mechanism.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 sandbox.py --help
# Optional live test: creates and cleans up only uniquely named fixtures.
SANDBOX_LIVE_TEST=1 python3 -m unittest discover -s tests -p test_live.py -v
# Also decode captured WAL if the matching audit image is available:
SANDBOX_LIVE_TEST=1 SANDBOX_AUDIT_IMAGE=localhost/documentdb-wal-audit:pg17 \
  python3 -m unittest discover -s tests -p test_live.py -v
```

No automatic expiry, source promotion, distributed topology, external
tablespaces, symlinked data, named-volume cloning, or version upgrades.
Do not run two tools against the same source fixture while seeding.

The design is intentionally small: clean stop, explicit clone, existing
container startup. See [PostgreSQL's consistency rules](https://www.postgresql.org/docs/18/backup-file.html)
and [APFS cloning](https://wadetregaskis.com/copy-on-write-on-apfs/).
The relation-manifest query is adapted from documentdb-wal-audit's MIT-licensed demo.
