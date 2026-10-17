# Isolated code execution

The Docker transport runs the existing OpenHands runner. It does not replace LangGraph, Ollama or the document plane.

Build the local image from the application directory:

```powershell
docker build -f Dockerfile.codeplane -t forgeflow-codeplane:1.49.6 .
```

Set these existing configuration values and restart the API:

```dotenv
CODEPLANE_TRANSPORT=docker
CODEPLANE_DOCKER_IMAGE=forgeflow-codeplane:1.49.6
CODEPLANE_DOCKER_TEST_COMMAND=python -m pytest -q
```

The daemon and image must be available. Missing runtime dependencies produce an explicit unavailable result; this mode never falls back to a host subprocess. Local Ollama is reached through host.docker.internal. Original source files are copied into a separate workspace, excluding private configuration and symlinks. Container code cannot write host Git control files.

Inspect a stopped code task under its result's **文件检查点与恢复** panel. Restore requires manager permission, a specific checkpoint version and the latest file hash. Current content is backed up before replacement. Running/uncertain workspaces, stale hashes and old checkpoint versions are refused. Recheck and reapprove any restored result.

The image provides Python, Node and Chromium; task dependencies still need installation within the task workspace. It uses bridge networking and is not an outbound-network firewall. File checkpoints are not task resume, and cannot roll back databases or remote systems. Files created after a checkpoint are not deleted by single-file restore. Crash-left checkpoint locks require investigation rather than automatic removal.

Tests with `FORGEFLOW_RUN_DOCKER_TESTS=1` exercise actual Docker mounts and a Chromium interaction. Ordinary regressions skip those hardware checks when the explicit flag is absent.


Code-runtime SDK calls share the root task's server-owned call budget. Missing final usage retains the reserved quota rather than guessing zero. Package.json test/typecheck/build scripts are selected for Node projects; successful build output without executed tests remains unverified. Environment dependencies and actual application behavior still require their own checks.
