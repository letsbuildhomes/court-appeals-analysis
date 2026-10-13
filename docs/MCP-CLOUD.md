# Quick HTTP research experiment

Run the research MCP over plain HTTP, then point a coworker's Claude Code CLI at
`/mcp`. An existing reverse proxy can forward to the same service. This experiment
does not configure a proxy, DNS or certificates. Access is anonymous and read-only;
the two tools and data semantics are in [MCP.md](MCP.md).

## Run locally

Choose a snapshot explicitly. If it has not been built yet:

```sh
make research-setup
.venv/bin/python -m court research build \
  --results legacy-results --cases legacy-cases --output data/research/experiment-v1
```

Serve it:

```sh
.venv/bin/python -m court research serve --snapshot data/research/experiment-v1 \
  --transport streamable-http --port 8770
```

In another terminal:

```sh
.venv/bin/python -m court research smoke --url http://127.0.0.1:8770/mcp
claude mcp add --transport http housing-appeals http://127.0.0.1:8770/mcp
```

The CLI syntax follows the [official Claude Code MCP guide](https://code.claude.com/docs/en/mcp).
Inside Claude Code, use `/mcp` to check the connection. Ask it to call
`describe_dataset`, then count confirmed appeals by permission and show the SQL
and snapshot ID. Repeat the [conversation checks](MCP-ACCEPTANCE.md) for unknowns,
mixed representation, duration minima and unsupported impact claims.

## Docker and a server

Publish exactly the curated pair with permissions readable by the non-root
container, build the lean image and run it:

```sh
.venv/bin/python -m court research publish --snapshot data/research/experiment-v1 \
  --output data/research/shared-experiment-v1
docker build -f deploy/research/Dockerfile -t housing-research:experiment .
docker run --rm --name housing-research --read-only --cap-drop ALL \
  --security-opt no-new-privileges -p 127.0.0.1:8770:8770 \
  --mount type=bind,src="$PWD/data/research/shared-experiment-v1",dst=/snapshot,readonly \
  housing-research:experiment
```

This command works locally with the same smoke/CLI commands above. `publish`
copies only `manifest.json` and `facts.sqlite`, refuses to overwrite an existing
version, and makes the directory/files readable by container UID 10001. The image
contains no pipeline/source data. Its Dockerfile-specific deny-all context allows
only research code and locked dependency files, excluding `.env`, source documents
and call logs before Docker receives the context. The pipeline image is separate.

For a server, transfer only that published pair plus `court/__init__.py`,
`court/research/__init__.py`, `court/research/snapshot.py`, `court/research/mcp.py`,
`pyproject.toml`, `uv.lock`, and `deploy/research/Dockerfile` and `Dockerfile.dockerignore`,
keeping their paths.

For an existing proxy on the same host, keep loopback publication and allow its
exact forwarded Host:

```sh
docker run --rm --name housing-research --read-only --cap-drop ALL \
  --security-opt no-new-privileges -p 127.0.0.1:8770:8770 \
  --mount type=bind,src=/absolute/path/to/shared-experiment-v1,dst=/snapshot,readonly \
  housing-research:experiment --host 0.0.0.0 --allowed-host research.example.org
```

Forward `/mcp` unchanged to `http://127.0.0.1:8770/mcp` through the existing proxy.
If its Host includes a port, use that exact value, e.g.
`--allowed-host research.example.org:443`. Repeat `--allowed-host` for additional
addresses. The coworker then runs:

```sh
claude mcp add --transport http housing-appeals https://research.example.org/mcp
```

For direct plain HTTP access, publish `-p 8770:8770` instead of loopback and pass
`--host 0.0.0.0 --allowed-host SERVER_NAME:8770`; the coworker's URL is
`http://SERVER_NAME:8770/mcp`. Without Docker, use the same options on
`python -m court research serve --transport streamable-http`. Allow the port in the
existing network setup if needed.

## Check the experiment

```sh
curl --fail http://127.0.0.1:8770/healthz
.venv/bin/python -m court research smoke --url http://SERVER_NAME:8770/mcp \
  --expected-snapshot YOUR_RECORDED_SNAPSHOT_ID
```

The smoke client accepts HTTP or HTTPS and checks initialization, discovery,
SQL counts, provenance, denied file/schema/narrative reads and recovery. It makes
no model calls. A 421 means the request Host needs an exact `--allowed-host` entry.
Present Origin values must match an allowed HTTP/HTTPS authority; CLI requests
normally omit Origin. The SDK caps bodies at 64 KB; SQL time/step/row/result limits
remain. Snapshot hash/schema are checked before listening, and `/healthz` identifies
the pinned in-memory version. To change data, publish a new version and restart
with its directory.

The non-root Docker HTTP path and SDK handshake/query/error recovery were tested
locally. Coworker CLI/proxy routing and conversation behavior remain the experiment's
acceptance checks.
