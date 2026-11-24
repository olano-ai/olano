# Security

## Reporting a vulnerability

Email **support@olano.ai** with the affected version or commit, the steps to
reproduce, and the impact you expect. Please do not open a public issue for a
security problem. We acknowledge every report, keep you informed while we work
on it, and credit you in the fix if you want that.

## What this repository enforces

- **Dependency audit gate.** CI fails when a locked dependency carries a known
  vulnerability (`pip-audit` against `uv.lock`); `make audit` runs the same
  check locally. The gate waives only the advisories listed under
  [Known advisories that do not affect Olano](#known-advisories-that-do-not-affect-olano),
  each with its reason there.
- **Dependabot.** Weekly grouped version updates for the Python lock, GitHub
  Actions, the Docker images and the npm trees (`.github/dependabot.yml`). Two
  packages are bumped by hand: `deepagents` is pinned exactly because the
  builder carries documented workarounds for it (see `CLAUDE.md`), and
  `playwright` stays below 1.57 to match the bundled Chromium.
- **Credentials.** Secrets resolve per agent through the vault and the agent's
  `config.env`, never through committed files. `.env.example` carries
  placeholders and clearly marked insecure defaults meant for local
  experiments only.
- **Relay signing.** A deployment authenticates to the Olano relays with an
  Ed25519 instance key; the relays keep public keys only.

## Known advisories that do not affect Olano

Dependabot and `pip-audit` report every advisory filed against a locked
package version, whether or not Olano can reach the vulnerable code. When an
advisory has no fixed release we can install **and** no code path in Olano
that reaches it, we do not force an upgrade. We record it here, waive it in
the CI audit gate, and dismiss the Dependabot alert as *Vulnerable code is not
actually used* with a link to this section.

Each entry names the release that would let us drop it. When that release
ships, the fix is an ordinary dependency bump, and the entry and its waiver are
removed in the same change. The ID in the first column is the one the CI
waiver uses; the aliases are the same advisory in other databases.
`tests/test_audit_waivers_in_sync.py` fails when this list, the waivers in
`.github/workflows/ci.yml` and `make audit` disagree, so none of them can
outlive the others.

Last reviewed: 2026-11-02.

### chromadb (locked at 1.5.9)

| Advisory | Also known as | Severity | What it is |
|---|---|---|---|
| `PYSEC-2026-311` | GHSA-f4j7-r4q5-qw2c, CVE-2026-45829 | Critical | Pre-authentication code injection in the chromadb server |
| `CVE-2026-45833` | GHSA-36p7-vc44-83pf, PYSEC-2026-3814 | Critical | Code injection through `trust_remote_code` on the server's collections API |
| `CVE-2026-45830` | GHSA-2wm9-hf6c-p5cr, PYSEC-2026-3813 | High | An authenticated server user can read and write any tenant's collections |
| `CVE-2026-45831` | GHSA-xph7-9rjv-w5fr, PYSEC-2026-3815 | High | The server's `SimpleRBACAuthorizationProvider` ignores which tenant, database or collection a permission covers |

**Why Olano is not affected.** All four are in chromadb's HTTP server. Olano
uses chromadb only as an embedded, on-disk library: `chromadb.PersistentClient`
in `olano/memory/knowledge_vault.py`, and `langchain-chroma` with a
`persist_directory` in `olano/memory/rag_memory.py`. It never starts the
chromadb server and never creates an `HttpClient`, so none of the affected
endpoints exist in an Olano process.

**What lifts it.** A chromadb release that fixes them. None exists yet; 1.5.9
is the latest.

### json-repair (locked at 0.53.1)

| Advisory | Also known as | Severity | What it is |
|---|---|---|---|
| `GHSA-xf7x-x43h-rpqh` | | High | A circular JSON Schema `$ref` makes `SchemaRepairer` loop without bound (CPU denial of service) |

**Why Olano is not affected.** json-repair is installed only because
`langchain-qwq` (the Qwen model provider) depends on it; nothing in Olano
imports it. `langchain-qwq` calls one function, `json_repair.loads(text)`, on
tool-call argument strings, and never passes a schema, so the vulnerable schema
resolution never runs.

**What lifts it.** The bug is fixed in json-repair 0.60.1, but `langchain-qwq`
requires json-repair below 0.54.0, and 0.3.5 (its latest release) still does.
A `langchain-qwq` release that lifts that cap.

### oauthlib (locked at 3.3.1)

| Advisory | Also known as | Severity | What it is |
|---|---|---|---|
| `CVE-2026-49265` | GHSA-xpv3-w29h-x7cv, PYSEC-2026-4114 | Moderate | An authorization server compares the PKCE `code_verifier` with `==`, a timing side channel on its token endpoint |
| `CVE-2026-49264` | GHSA-hj66-6f7g-4r5v | Moderate | `RevocationEndpoint` with `enable_jsonp=True` reflects the request's `callback` parameter unvalidated into a JavaScript response |
| `CVE-2026-49272` | GHSA-3jp2-3mrg-2vxc | High | An authorization server's device-code grant (`DeviceCodeGrant.validate_token_request`, RFC 8628) issues a token without validating the `device_code` parameter |

**Why Olano is not affected.** All three are in oauthlib's authorization-server
code. Olano uses oauthlib only as a client, through `google-auth-oauthlib`,
`requests-oauthlib` and `tweepy`, and a client never verifies a code verifier,
serves a revocation endpoint or runs a device-code grant for other clients. The
second one also needs a server that opts in to JSONP, which is off by default. Olano's own OAuth server (for MCP
external access) is built on the `mcp` SDK, not on oauthlib, and compares
secrets with `hmac.compare_digest`.

**What lifts it.** All three are fixed in oauthlib 4.0.0, but `tweepy` requires
oauthlib below 4, and 4.17.0 (its latest release) still does. A `tweepy`
release that lifts that cap. Forcing oauthlib 4.0.0 past the cap would probably
work, since its breaking changes are all server-side, but it would override a
declared constraint to fix code Olano never runs, so we wait.

`CVE-2026-49264` is already reported by Dependabot but not yet by `pip-audit`.
It is waived in advance so that CI does not fail on an unreachable path the
day `pip-audit`'s feed picks it up.

### Adding or removing an entry

1. If a fixed release is installable, bump to it. Do not add an entry.
2. Otherwise, confirm that Olano never reaches the vulnerable code, and write
   down how you confirmed it. Unused code is the bar here, not low severity.
3. Add a row above, with the ID the waiver uses in backticks. Add
   `--ignore-vuln <ID>` to the audit step in `.github/workflows/ci.yml` and to
   `make audit`. Dismiss the Dependabot alert as *Vulnerable code is not
   actually used*, linking here.
4. When the release named under **What lifts it** ships, bump to it, and remove
   the row and both waivers in the same change.

## Scope

This file covers the code in this repository. Reports about the managed
service at olano.ai go to the same address.
