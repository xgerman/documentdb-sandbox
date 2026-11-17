---
name: documentdb-sandbox
description: Fork a disposable documentdb-local container, run an experiment, and optionally review retained physical WAL. Supports Podman and Docker.
---

# Disposable DocumentDB experiments

Use the `sandbox.py` at this project's root. It requires Python 3.10+, Podman
or Docker, and a host filesystem supporting explicit clones. Do not install
PostgreSQL or the gateway on the host.

1. Select an existing sanitized seed. If none exists, follow the README to
   create a dedicated fixture container with a host bind mount and disposable
   credentials. `seed NAME CONTAINER` stops that container; never choose an
   unrelated instance. Seeding leaves the fixture stopped.
2. Run `python3 sandbox.py fork NAME`. Add `--record-wal` only if an activity
   review is required. Parse JSON, retain the sandbox ID, and use only its
   returned URI and private credentials file. Never print the password.
3. Perform the requested experiment using a normal wire-protocol client.
   Do not access seed files, another sandbox, or an unrelated endpoint.
4. For optional activity review, stop all experiment writers, then run
   `python3 sandbox.py capture ID`. This stops the sandbox and writes a sealed
   WAL archive, a best-effort relation manifest, and `review.json`.
5. Follow the README to invoke the existing `documentdb-wal-audit` image
   matching the recorded PostgreSQL major. Inspect its exit status, warnings,
   certainty, and transaction outcomes. Present committed effects only as
   **best-effort activity**, not an exact net diff. A null image is not proof
   of document absence. Never call incomplete or failed decoding "no changes."
6. Always run `python3 sandbox.py discard ID` on completion, including failure
   paths, unless the user requested retention. Report retained IDs and paths.

There is no merge, automatic expiry, exact diff, or continuous history guarantee.
Reset means discard and fork again. The skill does not need a daemon or MCP
server. Use the root-level `--runtime` and `--root` flags consistently when
nondefault settings are used.
