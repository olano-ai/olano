# Revisions

Automatic version history for the deployment's configuration and the files your
agents work with, in the **Revisions** tab.

## What is tracked

History follows a deliberate **allow-list**, so a new kind of file can never
slip in by accident:

- `config.yaml`, `organization.yaml`, the approval rules, and every agent's
  `agent.json5` (with its `env` values replaced by `__vault__`, so secrets are
  never stored).
- Inside every agent's workspace and in installed skills, files of these types:
    - notes and documents: Markdown, text, HTML, CSV, PDF, Word, Excel,
      PowerPoint (and their OpenDocument equivalents), EPUB;
    - data and configuration: JSON, JSON5, YAML, TOML, INI, XML;
    - scripts and source code: Python, notebooks, JavaScript and TypeScript,
      shell, and other common languages (executable scripts keep their
      executable flag);
    - images, audio and video: PNG, JPEG, GIF, WebP, SVG, MP3, WAV, M4A, MP4,
      MOV, WebM and similar.

Never tracked: `.env` files, the vault, databases, session transcripts, logs,
archives, hidden folders, dependency and cache folders (`node_modules`,
virtual environments, `__pycache__`), another git repository inside a
workspace, a customer-service agent's private per-customer memory, and
configuration files whose name says they hold a credential (`token.json`,
`client_secret.json`, `credentials.yaml`).

Commits are attributed to whoever caused the change (the agent or the human),
so you can answer "who changed this, and what did it look like before". Changes
are debounced (default 45 seconds) so a burst of edits becomes one entry.

If you have data-deletion obligations, set `revisions.include_memory: false`:
memory files carry distilled personal data, and this keeps them out of history.

## Files that are held back

Some files are tracked but cannot be recorded as they are right now. Each one
keeps the **last version that was recorded** (it is never recorded as deleted,
so a push never removes it from GitHub), and the **Revisions** page lists it
with the reason, until the cause is fixed:

| Reason | Cause | Fix |
|---|---|---|
| Too large | Larger than `revisions.max_file_kb`: 50 MB by default. It can be raised to 100 MB, the most GitHub accepts for one file; above 50 MB GitHub warns on every push. | Make the file smaller, or keep it elsewhere. |
| Looks like a credential | A text file (or an `agent.json5` outside its `env` block) contains something shaped like an API key, an access token, a private key or a password inside a database URL. History is pushed to GitHub, so it never stores one. | Move the secret to the **Vault** and reference it; the file is recorded again on the next change. |
| Does not parse | An `agent.json5` that cannot be read, so its secrets cannot be removed safely. | Fix the file's syntax. |

Very large files make history grow: every version of a 90 MB video is kept.
The **Revisions** page shows the history's total size, and the disk clean-up
repacks it.

## Seeing what changed

Select any commit on the **Revisions** page to see what it did: how many files
it changed, how many lines were added and removed, which files were added,
modified or deleted, and the diff of each file side by side or unified. A
commit made by a pull from GitHub is a *merge*: its file list shows what the
pull changed on this deployment.

## Syncing with GitHub

The history can be copied to a GitHub repository you own, as an off-site backup
and as a place to edit files by hand. The history on your deployment stays the
source of truth; GitHub is a copy you push to and pull from.

### Connecting

1. On GitHub, [create a new repository](https://github.com/new). Make it
   **private** and leave it **empty**: no README, license or `.gitignore`. A
   repository that already has commits is refused the first time, and a public
   one needs an explicit confirmation, because the history holds your
   configuration and your agents' memory.
2. Choose how the deployment signs in to GitHub. When GitHub is already
   connected on the **Connections** page, that connection is selected for you
   and there is nothing to paste; otherwise the access token is.
    - **GitHub connection**: reuse the GitHub account connected on the
      **Connections** page. It reaches every repository that account can reach.
    - **Access token**: create a
      [fine-grained personal access token](https://github.com/settings/personal-access-tokens/new)
      with *Repository access* set to *Only select repositories* (the new one)
      and the permission *Contents: Read and write*. A classic token with the
      `repo` scope also works. The token is stored encrypted in the vault under
      a reserved name: it is not shown on the Vault page, it is never written to
      `config.yaml` or the history, and agents cannot read it. A token can be
      limited to this one repository, which a connection cannot.
3. On **Revisions**, choose **Connect a GitHub repository**, enter the
   repository as `owner/name` (or paste its URL), the token, the branch
   (default `main`) and the automatic push mode, then **Connect and push**. The
   whole history is pushed at once. The window then shows each step (checking
   the repository, recording a snapshot, uploading the history, saving the
   connection) with a progress bar and the upload's size and speed; a large
   history can take several minutes. Closing the window does not stop it: the
   **GitHub sync** card keeps showing the progress until it finishes.

What is pushed is exactly what the history tracks: `config.yaml`,
`organization.yaml`, approval rules, every `agent.json5` with its `env` values
replaced by `__vault__`, and workspace markdown, including memory unless
`revisions.include_memory` is off. `.env` files, the vault, databases, sessions
and tokens are never in the history, so they are never pushed.

### Pushing

**Push** sends new commits to GitHub. **Automatic push** decides whether the
deployment also pushes on its own:

| Mode | Behaviour |
|---|---|
| Manual (default) | Only when you press **Push**. |
| Once a day | At most once every 24 hours, when there is something new to send. |
| After every commit | Right after each commit (commits are already grouped: by default 45 seconds after the last change). |

A push never overwrites commits on GitHub that the deployment does not have.
When GitHub has moved on (someone edited a file there), the push stops, the
page says a pull is needed, and automatic pushing pauses until you pull. You can
also choose **Force push**, which replaces GitHub's branch with the deployment's
history; the page asks for confirmation, because the commits only GitHub had are
dropped from the branch.

### Pulling

**Pull** brings GitHub's changes into the deployment. Pulling is always manual.
**Check for updates** only asks GitHub what is new, without changing anything.

- When only GitHub has new commits, its files are applied as they are.
- When both sides changed *different* parts, the changes are merged
  automatically.
- When both sides changed the *same* lines, nothing is applied. The page lists
  the files in conflict; for each one, compare this deployment's version with
  GitHub's and choose **Keep this box's version**, **Take GitHub's version**,
  or **Edit the merged text** (the text starts with both versions between
  `<<<<<<<` and `>>>>>>>` markers; remove the markers before completing). Then
  **Complete merge**. **Abort merge** drops the pull and changes nothing.

After a pull the page lists what was updated, added and deleted, and offers to
restart agents whose configuration changed (their new configuration applies
from the restart). A pull is careful about what it writes:

- Only files the history tracks are written. Anything else in the GitHub
  repository (a README, a `.env`, any other file) is ignored and listed, and
  the next push removes it from GitHub's branch.
- `env` values in `agent.json5` keep this deployment's real values. A variable
  that only exists on GitHub is left out, with a note to set it on the agent's
  **Variables** tab.
- The `revisions:` block of `config.yaml` always stays as it is on the
  deployment, so a pull can never disconnect GitHub or switch history off.
- Deleting `config.yaml`, `organization.yaml`, the approval rules or an
  agent's `agent.json5` on GitHub is not applied: remove an agent from the
  dashboard instead.
- Files with invalid YAML or JSON5 stop the pull before anything is written.
- If an agent writes one of the affected files while the pull is running, the
  pull stops without applying anything; pull again.

### When several people sync at once

Only one GitHub operation runs at a time for the deployment: a push, a pull,
**Check for updates**, a merge, connecting, disconnecting, or the automatic
push. If another admin (or the automatic push) is already running one, the
page says which operation and who started it, and the buttons come back as soon
as it finishes; nothing is queued behind it. Pressing **Push** when GitHub
already has every commit sends nothing.

### Disconnecting

**Disconnect** stops syncing and deletes the stored access token. The GitHub
repository and the history on the deployment are both left as they are.
Connecting again later needs an empty repository.

