# Revisions

Automatic version history for your deployment's configuration and markdown
files. Every meaningful change is committed, attributed to the agent or human
who made it, and browsable here — so you can see what an agent changed in
`MEMORY.md` last Tuesday, and what a config looked like before an edit. See
[Revisions](../feature-guides/revisions.md).

- **History**: the list of commits, newest first, ten to a page. The arrows
  above and below the list go to the newest page, the previous page, the next
  page and the oldest page, and the label says which page you are on and how
  many there are. Type a path in the filter box to see only the commits that
  touched one file (the list starts again on its newest page), and **View** to
  read that file as it was at a commit. A **merge** badge marks a commit that joined GitHub's
  changes with this deployment's, and **not on GitHub** marks commits a push has
  not sent yet.
- **Select a commit** to open its details: the full commit id, author, date and
  message, a summary (files changed, lines added and removed, how many files
  were added, modified and deleted), and the list of files. Select a file to see
  its diff, **side by side** (the default on a computer) or **unified** (the
  default on a phone); your choice is remembered. **Whole file** shows every
  line around the changes, and **Open file at this commit** shows the file as it
  was.
- **GitHub sync** (admins only): connect a private GitHub repository, then
  **Push**, **Pull** and **Check for updates**, choose **Automatic push**, and
  resolve a pull whose changes collide with this deployment's. The card shows
  the repository, its branch, how the deployment signs in and the automatic
  push mode; a status line (up to date, commits to push, commits to pull, or
  GitHub has moved on); and when the last push, pull and check happened. While
  a connection, push or pull runs, the card shows each step as it happens, a
  progress bar, and while the history moves over the network how many objects
  and megabytes have been sent or received and at what speed. Everyone viewing
  the page sees it, including who started it.
- **Held back**: a warning lists any tracked file whose current version is not
  being recorded (too large, looks like it holds a credential, or an agent
  configuration that does not parse), with the reason.
- Documents, images, audio and video show as **binary** in a commit, with
  **Download** buttons for the version before and after; opening one at a
  commit offers a download instead of a text view.
- **Snapshot now** records the current state immediately instead of waiting for
  the next automatic commit.
