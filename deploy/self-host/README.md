# Olano, self-hosted

Run Olano on a machine you own: a laptop, a home server, a VPS. The stack is
three containers from the public images, started with Docker Compose:

| Service | Image | What it does |
|---|---|---|
| `olano` | `ghcr.io/olano-ai/olano-backend` | the runtime: agents, channels, scheduler, the API on 8765 |
| `web` | `ghcr.io/olano-ai/olano-dashboard` | the dashboard, served by Caddy on the same origin as the API |
| `postgres` | `postgres:16` | conversation state and the agents' store |

Models are paid for either way you like, and both can be used at once:

- **Olano credits.** Create an API key on [cloud.olano.ai](https://cloud.olano.ai)
  (API keys), buy account credits there, and paste the key in the dashboard
  (the setup wizard, or Providers → Olano). The box then runs the Olano
  model tiers, image generation, vision, speech and web search on your
  account's credits, with no provider accounts.
- **Your own provider keys** (Anthropic, OpenAI, Google, …), pasted in the
  same places.

## Requirements

- Docker Engine with the Compose plugin (`docker compose version` answers).
  Linux, macOS (Docker Desktop) or Windows (Docker Desktop with WSL 2). On
  Linux the installer below offers to install it.
- 2 CPUs and 4 GB of RAM to start with; more agents want more. 10 GB of disk.
- For https: ports 80 and 443 reachable from the internet (see
  [The address](#the-address)).

## Install

One line:

```bash
curl -fsSL https://raw.githubusercontent.com/olano-ai/olano/main/deploy/self-host/install.sh | bash
```

`install.sh` checks for Docker and the Compose plugin (on Linux it offers to
install them with Docker's official script, and asks first; it reads the
answer from your terminal, and with no terminal it stops and says how),
downloads the files below into `~/olano` (or `$OLANO_DIR`), and runs
`setup.sh` there. Options for `setup.sh` go after `bash -s --`:

```bash
curl -fsSL https://raw.githubusercontent.com/olano-ai/olano/main/deploy/self-host/install.sh \
  | bash -s -- --host olano.example.com --sandbox
```

Re-running it updates the stack's files; your `.env` and data directory are
never touched.

Or by hand (the same files, then the same script):

```bash
mkdir olano && cd olano
for f in docker-compose.yml docker-compose.https.yml docker-compose.sandbox.yml \
         Caddyfile .env.example setup.sh; do
  curl -fsSLO "https://raw.githubusercontent.com/olano-ai/olano/main/deploy/self-host/$f"
done
bash setup.sh
```

To run from source instead (the Python package and the dashboard dev server),
see the repository's top-level README.

`setup.sh` writes `.env` (a random Postgres password, a one-time setup code,
the compose project name, a memory cap for the runtime, the address), pulls
the images and starts the stack, then prints the address and the setup code.
Open the address: the setup wizard runs on the first visit, asks for the setup
code, creates your admin account, asks how your agents' models are paid for,
and introduces Agent Father. `bash setup.sh --help` lists every option.

Everything the install keeps (agents, the vault, conversations, files, logs)
lives in `./data` next to the compose file. Back that directory up and you
have backed up the install; point `OLANO_DATA_DIR` in `.env` at another path
to keep it elsewhere.

### The setup code

Until the first account exists, whoever reaches the dashboard can create it
and becomes the admin. On a public server that could be a stranger, so the
dashboard asks for a one-time setup code before it creates that account. It
is printed by `setup.sh`, kept in `.env` as `OLANO_SETUP_CODE`, and printed in
the runtime's log under `FIRST-RUN SETUP CODE`:

```bash
docker compose logs olano | grep -A2 "FIRST-RUN SETUP CODE"
```

Case, spaces and dashes do not matter. Ten wrong tries lock the form for 15
minutes. The code stops working once the first account exists. If you started
the stack without `setup.sh` (no `OLANO_SETUP_CODE` in the environment), the
runtime generates a code itself, keeps it in `data/run/setup-code` and prints
it in the log the same way.

### The address

On the first run, with no address option, `setup.sh` asks a what-is-my-ip
service (api.ipify.org, then icanhazip.com) for the machine's public IPv4:

- **The address is on one of the machine's own interfaces** (a typical VPS):
  https on `<ip-with-dashes>.sslip.io`, e.g. `https://203-0-113-7.sslip.io`.
- **Otherwise** (a laptop, a home server, a cloud VM behind NAT such as AWS
  EC2): `http://localhost:3200`, plus a hint about the options below.

Later runs keep the address `.env` holds. Change it with:

```bash
bash setup.sh --host olano.example.com   # your domain, https (point its DNS at the machine first)
bash setup.sh --host auto                # https on <your-ip>.sslip.io, e.g. a NAT'd cloud VM
                                         # whose ports 80 and 443 are forwarded to it
bash setup.sh --local                    # http://localhost:3200 only (same as --host local)
```

In either https mode Caddy obtains a Let's Encrypt certificate on the first
request and renews it on its own; `setup.sh` sets `OLANO_SITE_ADDRESS`, the
public URL agents use in the links they send, and adds
`docker-compose.https.yml` (which publishes ports 80 and 443) to
`COMPOSE_FILE` in `.env`. **In a cloud firewall, open 22, 80 and 443 and
nothing else.** sslip.io names share Let's Encrypt's rate limit with every
other sslip.io user, so issuance can be refused for a while when that limit is
hit; a domain of your own is the reliable route.

### Ports

| Host port | Bound to | Serves |
|---|---|---|
| `3200` (`OLANO_HTTP_PORT`) | `127.0.0.1` (`OLANO_HTTP_BIND`) | plain http, in every mode |
| `80` / `443` | all interfaces | the https site; only with `--host` |

The plain-http port is published on the machine's loopback only, so it is
never reachable from the network. From another computer use an SSH tunnel,
which also works while a certificate is still pending:

```bash
ssh -L 3200:localhost:3200 you@your-server   # then open http://localhost:3200
```

`OLANO_HTTP_BIND=0.0.0.0` in `.env` opens port 3200 to your LAN instead. That
is unencrypted http: do it only on a network you trust, never on a public
server. (Installs from before this change published 3200 on every interface;
re-running `setup.sh` does not change `OLANO_HTTP_BIND`, so set it if you
relied on LAN access.)

The runtime's own port (8765) is reachable only from Caddy inside the compose
network; the dashboard and the API share one origin on purpose (see the
Caddyfile). Values in `.env` may be quoted or not; `setup.sh` reads both.

### Sandbox mode

```bash
bash setup.sh --sandbox      # and --no-sandbox to turn it off again
```

By default an agent's shell commands, the Terminal and the chat `!` shell run
inside the runtime container, next to the vault's key, every agent's files and
the runtime itself. Sandbox mode gives every agent its own container instead
(non-root, resource-capped, only that agent's workspace mounted), as on Olano
Cloud. An agent whose sandbox cannot start gets no shell rather than falling
back to the runtime container.

**The trade-off:** the runtime starts those containers through the host's
Docker socket, which `docker-compose.sandbox.yml` mounts into it. A container
holding the Docker socket can do anything root can do on the host. Agents
never get the socket, but a flaw in the runtime becomes a flaw in the host.
Use sandbox mode on a machine you alone control.

What `--sandbox` does: pulls `ghcr.io/olano-ai/olano-sandbox:latest` (or
`OLANO_SANDBOX_IMAGE`) and tags it `olano-sandbox:latest`, the name the runtime
starts; sets `OLANO_SANDBOX_EXEC=1`; pins `OLANO_DATA_DIR` to an absolute path;
and adds `docker-compose.sandbox.yml` to `COMPOSE_FILE`. The sandboxes are
started by the host's Docker daemon, which mounts each agent's workspace by
the path the runtime sees, so the override mounts the data directory at the
same absolute path inside the runtime as on the host. Re-run `setup.sh` after
an update to pull a newer sandbox image.

### Unattended installs

Both keys can go into `.env` instead of the dashboard:

```bash
bash setup.sh --olano-key sk-olano-...     # Olano credits from the first boot
# or edit .env: ANTHROPIC_API_KEY=..., OPENAI_API_KEY=..., OLANO_CREDITS_KEY=...
```

### If the image pull fails

The images are public: the Olano images on `ghcr.io` and Postgres on Docker
Hub need no login. A failed `docker compose pull` is usually the network, so
re-run `setup.sh`; layers that finished downloading are kept. When every image
is already on the machine from an earlier run, `setup.sh` starts with those and
says so. `429 Too Many Requests` from `registry-1.docker.io` is Docker Hub's
limit on anonymous pulls from your address (common behind a shared office or
cloud NAT); `docker login` with a free Docker account raises it.

## Day to day

```bash
docker compose ps                 # what is running
docker compose logs -f olano      # the runtime's log
docker compose pull && docker compose up -d   # update to the current release
docker compose down               # stop (the data directory and volumes stay)
```

To update on your own schedule, pin a version in `.env`
(`OLANO_BACKEND_IMAGE=ghcr.io/olano-ai/olano-backend:0.3.2`, the same tag
for the dashboard image) and change it when you choose to.

## What this is not

This is not a managed Olano Cloud deployment, and it must not be dressed up
as one: do not set `LIONCLAW_DEPLOYMENT_TOKEN`, `LIONCLAW_DEPLOYMENT_MANAGED`
or an installation manifest under `/etc/olano`. Those belong to boxes that
cloud.olano.ai provisions and keeps in step with a control plane; on a
self-hosted box a deployment token without its manifest reads as a broken
managed install and the runtime quarantines itself. A self-hosted box runs in
the open posture: agents' shell commands run inside the runtime container
(unless you chose sandbox mode), there is no plan and no hidden dashboard section,
and credits come from the Olano API key above, never from a deployment.

Want the managed service instead? Deploy at [cloud.olano.ai](https://cloud.olano.ai)
and the same dashboard arrives with infrastructure, updates, snapshots and a
plan's included credits.
