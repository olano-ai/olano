# Deploying Olano

**Running Olano on your own machine?** Use [`self-host/`](self-host/README.md):
the public images, one compose file and one setup script. That is the
supported install for a laptop, a home server or a VPS.

**Working on Olano itself?** Run it from a source checkout instead: see
[`CONTRIBUTING.md`](../CONTRIBUTING.md) and the root `install.sh`.

Everything else in this directory is the stack that runs on the boxes Olano
Cloud provisions and manages. It is public because those boxes run the same
public images and clone this repository to start them, not because it is an
install path: a managed box also carries an installation manifest, a
deployment token and a control-plane channel that a self-hosted box must not
imitate (see [`self-host/README.md`](self-host/README.md), "What this is not").

| File | Role on a managed box |
|---|---|
| `bootstrap-allinone.sh` | First-boot setup: Docker, `.env` pins, Caddy, then the all-in-one stack |
| `docker-compose.allinone.yml` | The runtime, the dashboard (Caddy) and Postgres on one machine |
| `Caddyfile.allinone` | Same-origin serving of the dashboard and `/api` with automatic TLS |
| `env-lib.sh` | The sticky `.env` pins every box script shares |
| `dns.sh` | Points a box's hostname at its address |
| `setup-host-user.sh` | Creates the unprivileged service user the stack runs as |
| `bootstrap-remote.sh`, `docker-compose.prod.yml`, `Caddyfile` | The headless shape: the runtime and its API with no dashboard |
| `tc-manual-host.sh`, `tc-manual-docker.sh` | Manual installs on an existing VM, host-native or in Docker |
| `build-dashboard.sh` | Builds the dashboard from a source checkout (used by in-place updates) |

Olano Cloud's own provisioning and operations tooling (creating machines on
each cloud provider, shipping code to them, restarting and inspecting them)
lives with the rest of the managed service, outside this repository.
