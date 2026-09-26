# C3 file changes

Inventory checked on 2026-09-25 against `HEAD`
(`5de0e9e46fa8719b7a305378f85197a7488e85c8`). C3 changes 22 files:
14 added and 8 modified. Updating this document makes 23 changed files.
Paths below are repository-relative; this replaces the previous C2 inventory.

C3 packages the existing controller runtime and web service into two ARM64
images. The runtime contains ROS and Supervisor; web contains only the shared
protocol, API and compiled frontend. Both use UID/GID 10001. Compose defines
mounts and isolation, while host preparation and robot cutover remain C4 work.

| Status | File | What changed and why |
| --- | --- | --- |
| Modified | `CHANGES.md` | Replaces the C2 inventory with the complete C3 file inventory, operational changes and validation boundary. |
| Modified | `CONVERSATION_MEMORY.md` | Records image architecture, dependency resolution, TLS behavior, successful emulated ARM64 validation, reproduction commands and the local release identity for future sessions. Keeps C4–C6 and real-robot checks pending. |
| Modified | `docs/MENTORPI_CONTAINER_REFACTOR_DESIGN.md` | Marks C3 complete with emulated ARM64 build/IPC evidence, selects UID/GID 10001 and the initial rootful Docker Engine 29.8 / Compose 5.5 baseline, and links the image guide. Physical validation remains pending. |
| Added | `docker/ubuntu_tank/Dockerfile` | Defines separate final `runtime` and `web` targets using digest-pinned Ubuntu and Node bases. Installs the ROS workspace into the runtime image; builds local protocol/web wheels and frontend assets for web. Uses dated Ubuntu/ROS archives alongside live repositories to resolve the native lock, retains dependency manifests, and runs installed import/CLI checks. Final images run non-root and declare safe health checks. |
| Added | `docker/ubuntu_tank/README.md` | Documents build and smoke commands, dependency provenance, release identities, mount ownership, retained TLS, supported Docker configuration and image transport. Separates packaging verification from Pi deployment and physical stop testing. |
| Added | `docker/ubuntu_tank/build.py` | Stages an allowlist of application inputs instead of sending the checkout to Docker. Excludes caches, developer installations and private material; hashes the staged inputs and records Git revision/dirty state. Builds and loads both ARM64 targets, checks architecture, extracts dependency manifests without executing the images, and writes `release.json` only after both succeed. Supports a named builder and context-only staging; untracked application sources are excluded. |
| Added | `docker/ubuntu_tank/compose.yaml` | Defines runtime and web with read-only roots, UID/GID 10001, dropped capabilities, init/signal handling, bounded Docker logs and temporary writable directories. Runtime receives only `/dev/rrc` and has loopback-only networking; web publishes port 8443 and mounts shared IPC read-only. Configuration, keys and the stable owner-lock directory are read-only mounts. Missing bind sources fail instead of being created. Requires image references and the verified serial-device group from the operator/C4 tooling. |
| Added | `docker/ubuntu_tank/dependencies.json` | Lists separate APT roots for runtime, ROS build tools, web and web build tools so each stage installs its required dependencies without introducing ROS into web. |
| Added | `docker/ubuntu_tank/healthcheck.py` | Checks runtime operator/lifecycle status or web's local HTTPS version endpoint. Probes never acquire control, Start or Arm; a stopped controller is healthy, and web health does not depend on runtime availability. |
| Added | `docker/ubuntu_tank/install_dependencies.py` | Converts the native package lock into APT version pins, pins Supervisor 4.3.0-1, and checks downloaded locked archives against version, architecture and SHA256 before installation. Records installed versions and archive hashes per stage; additional container packages resolve through signed APT metadata. Runs inside image builds, without changing host packages. |
| Added | `docker/ubuntu_tank/ros-snapshot.asc` | Supplies the public signing key for the dated ROS snapshot, which uses a different key from the live repository. Allows signature verification of the archived packages required by the native lock; contains no private credentials. |
| Added | `docker/ubuntu_tank/runtime-entrypoint.sh` | Sources installed ROS/workspace environments, sets production configuration and private runtime paths, enforces loopback Fast DDS and SROS2, and rejects simulation overrides. Normally invokes runtime startup validation; explicit command arguments run inside the installed environment for diagnostics and terminal clients. |
| Added | `docker/ubuntu_tank/runtime_start.py` | Checks application UID, IPC/private-directory ownership and permissions, controller configuration, SROS2 enclave directories and the mapped serial-device type. Holds an exclusive cooperating-owner lock across Supervisor startup without opening serial or starting/arming the controller. Detecting legacy serial holders and preparing host paths remain C4 responsibilities. |
| Added | `docker/ubuntu_tank/web-entrypoint.sh` | Starts the installed web module with mounted configuration, port 8443 and compiled frontend assets. Uses shell `exec` for signal delivery and preserves explicit CLI overrides for diagnostic/test invocation. |
| Added | `docker/ubuntu_tank/smoke.py` | Runs exact image IDs from a completed release in disposable containers with no devices or published ports. Checks ROS-free web imports, HTTPS/static assets, mode-0600 IPC over the read-only web mount and reconnection after replacing the runtime fixture. Cleans up its containers/volume and optionally uses a container-scoped ARM64 emulator on x86. |
| Modified | `ubuntu_tank/README.md` | Adds a prominent link to C3 build/verification instructions and states that host cutover and robot validation remain C4/C5 work. The older native-delivery instructions are otherwise retained. |
| Modified | `ubuntu_tank/deploy.sh` | Adds the new web configuration/TLS behavior suite to the existing development test command. Does not add Docker deployment or host provisioning. |
| Modified | `ubuntu_tank/docs/CONTAINER_SUPERVISION.md` | Replaces the statement that images/Compose are pending with a link to C3. Keeps host ownership, deployment and Pi cutover pending and preserves the distinction from historical native acceptance. |
| Modified | `ubuntu_tank/src/ubuntu_tank_web/ubuntu_tank_web/entrypoint.py` | Stops silently falling back to default configuration when the file is missing, malformed or invalid; requires a YAML mapping. Normal startup now validates existing TLS material instead of creating/replacing it, and requires certificate/key paths unless `--no-tls` is explicit. OpenAPI export remains independent of host configuration and TLS. Also tidies imports and updates the module documentation. |
| Modified | `ubuntu_tank/src/ubuntu_tank_web/ubuntu_tank_web/tls.py` | Adds `validate_tls_certificate`, which checks certificate validity dates and loads the certificate/key through Python's SSL implementation to reject malformed or mismatched material without writing files. Retains provisioning helpers for separate owner operations; moves `Sequence` to `collections.abc`. |
| Added | `ubuntu_tank/tests/fixtures/container_ipc.py` | Runs the installed operator IPC and lifecycle servers in container mode, using real socket permissions and peer credentials. Its process double reports the controller stopped and rejects mutations, allowing the image smoke test to verify packaging and IPC without ROS or motors. |
| Added | `ubuntu_tank/tests/test_container_web.py` | Adds three behavior tests: reject missing/corrupt/expired/mismatched TLS without replacing files; reject missing/invalid configuration without defaults; and run a real HTTPS/static server using retained certificate bytes. Verifies startup behavior through APIs and a subprocess. |
| Modified | `ubuntu_tank/tests/test_source_boundary.sh` | Recognizes Python's `ssl` module as standard library so the existing dependency audit accepts the new TLS validation code without inventing an external package dependency. |

The existing `docker/ubuntu_tank/supervisord.conf` and C2 operator/supervisor
implementation are reused unchanged. Open IDE tabs do not imply those files
changed in C3. `review.md` was refreshed to `PASS`; it is Git-ignored and excluded
from the source count. Generated build contexts, images and manifests are also
outside that count.

Validation completed during C3 implementation:

- Both final ARM64 images built under BuildKit emulation on the x86 development
  computer; installed runtime imports/CLI checks and ROS-free web checks passed.
- The paired image smoke test passed HTTPS, compiled assets, shared IPC permissions
  and reconnection after socket-server replacement. The production web entrypoint
  also served HTTPS with runtime absent.
- `./ubuntu_tank/deploy.sh test` passed, including existing C1/C2 coverage, browser
  tests and the three new web tests. Native ROS/DDS and live RRC checks skipped
  on the development computer.
- Frontend type checking, new-code Python lint/format checks, shell lint,
  Compose configuration parsing and whitespace checks passed. Independent
  critical-only review reported `PASS`.

Build evidence is retained at `ubuntu_tank/.work/c3-build-9/release.json`, with
release ID `8bb3fc6d640889530c61654aebdacf38b77e19281145466f68622e7ed4e1e694`.
The built application input hashes match the completed C3 working tree.

These checks do not validate production runtime startup on a Pi 5, host serial
exclusion, ROS/DDS communication, controller Start, shutdown/pause failure timing,
or real motor motion. C4–C6 remain pending. No robot deployment, motor operation,
commit or push was performed.
