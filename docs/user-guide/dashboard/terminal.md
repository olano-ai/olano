# Terminal

A shell in the agent's workspace, from the browser. Subject to the same
boundaries the agent has — it reaches that workspace and the shared folders, not
other agents' directories.

Every terminal belongs to one agent, because its shell runs in that agent's
workspace: on a managed deployment, or a self-hosted install set up in
[sandbox mode](../getting-started.md#sandbox-mode), inside that agent's own sandbox. The
**Workspace agent** picker at the top of the page says whose shells you are
looking at: switch it and the tabs swap to that agent's terminals (its first one
opens on its own if it has none), while the other agents' shells keep running
out of sight. Each row of the picker shows how many terminals that agent has
open, and a hint beside it says whether the shell runs inside the agent's
sandbox or on the host in its workspace. **+** opens another terminal for the
selected agent; closing an agent's last tab leaves an **Open terminal** button
rather than starting a shell you did not ask for. If the agent's sandbox is not
running, the tab says so and waits — start the agent, then press **Reconnect**.
When the runtime knows *why* there is no sandbox (for example the container
failed to start), that reason is shown under the message.

The terminal does not depend on the agent's **Access** level. On a managed
deployment every running agent has a sandbox container, whether its Access is
Read, Read & Write, Read, Write & Execute or Virtual Files — the container is
started with the agent. For an agent without Execute the container is yours,
not the agent's: it is where the Terminal, the chat `!` shell and the agent's
own media/CLI tools run in isolation, while the agent itself still has no shell
tool and keeps exactly the file permissions its Access level grants. Switching
that agent to Read, Write & Execute later reuses the same container, so anything
you installed in it from the Terminal is still there.

It is a real shell, so it behaves like one: **Tab** completes file and directory
names as you type a path, ↑ and ↓ walk back through what you have run, and
`tmux` is installed if you want a session that survives closing the tab.

Two additions on top of the shell itself:

- **Command suggestions.** Once you have typed a couple of characters, commands
  you have run here before appear next to the line you are typing. Press `→` (right arrow,
  at the end of the line) to accept the first one, or tap any of them. They are
  remembered in this browser only, they are never sent anywhere, and a command
  typed with a leading space is not remembered at all — the same convention the
  shell itself uses for keeping a secret out of history. Tab still does file
  completion; the suggestions never take it over.
- **A key bar on phones and tablets.** A touch keyboard has no Tab, arrows, Esc
  or Ctrl, and a browser terminal has no right-click menu, so those live in a
  row under the terminal: Copy (the selection, or the visible screen when
  nothing is selected), Paste, Tab, ↑, ↓, Esc, Ctrl+C and Enter. If your browser
  refuses clipboard access, Paste falls back to asking for the text.
