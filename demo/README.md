# Demo videos

Short product films of Olano: the real dashboard in a browser window (or, in the phone format, in an
iPhone), with captions, a pointer, a voiceover and soft music. Each one comes in landscape
(1920×1080, for YouTube and the website) and portrait (1080×1920, for Reels, Shorts and TikTok).

Nothing is filmed or edited by hand. A script drives the real dashboard in headless Chromium and
records it frame by frame; the voice is generated from the written lines, the music is composed in
code, and one command rebuilds a video after a caption, a line or the dashboard itself changes.

```
 flow-<demo>.mjs ──► record.mjs ──► frames (JPEG) + marks.json (when each step starts)
   (the script)      (Chromium, stage.html,      │
                      the dashboard from         │   voiceover.mjs ──► audio/<demo>/vo0..N.mp3
                      serve.py)                  │   music.py      ──► audio/<demo>/music.mp3
                                                 ▼
                              build.sh ─► mix.mjs (voice on the marks, music ducked, −14 LUFS)
                                         ─► ffmpeg ─► videos/olano-<demo>-<layout>.mp4 (H.264, 60 fps)
                                                       └─► clips.sh ─► videos/clips/ (one per chapter)
```

## The videos

| Demo | Story | Fictional team (`teams/`) |
|---|---|---|
| `tour` | Olano in a minute: agents, a chat, the org chart, the scheduler, an approval | Harbor & Pine, an online home-goods shop |
| `create` | A new agent from the New agent dialog, then its first conversation | Casa Lumen, a small hotel |
| `chat` | One question: the steps, a chart in the answer, the report as a file, another model | Kettle Hill Coffee, a roaster |
| `team` | One agent asks two others; the exchange on record; shared issues | Fieldnote Studio, a marketing studio |
| `schedule` | A weekday task, the calendar, Run now, the run as its own conversation | Brightside Lettings, a property manager |
| `control` | An approval before an email is sent, the Vault, Insights, Revisions | Marlow & Finch, an accounting firm |
| `install` | The one-line Docker install, the setup code, the first-run wizard | none: a real, fresh install |
| `source` | Clone, `./install.sh`, `olano setup`, `olano doctor`, `olano dashboard` | Harbor & Pine, for the final shot |

## How the dashboard is real and the agents are not

`serve.py` boots a throwaway dashboard with the launcher the browser tests use
(`tests/e2e/harness.py`): the real runtime API and the real React app from source, in a temporary
home. No model is called and no key is read. Each demo's team, files, tasks, issues and earlier
conversations come from `teams/<demo>.py`, and its agents answer from a script
(`ScriptedRunner`): tool steps, pauses, text, an approval, a hand-off to another agent. The runner
emits the same events and writes the same transcript rows as the real one, so the dashboard draws
a turn exactly as it would a real one.

Where the product computes something, the demo runs the product's own code on the made-up data:
the chart in `chat` comes from the `charts` skill (`chart.mjs`), Insights reads a month of usage
written with `record_usage_event`, Revisions shows commits made by `snapshot_home`, and the Vault
holds entries saved through its own API (with made-up values).

Two things the launcher's gateway does not have are stood in for in `serve.py`: the scheduler
(next-run times are computed from each task's cron line, and Run now plays the agent's scripted
turn on a fresh `scheduled_…` thread) and agents created during a take (they get a scripted runner
as soon as their `agent.json5` appears).

## Make them

Needs what `make test-e2e` needs (`make dev`, `npm install` in `olano/dashboard_2.0`, Playwright's
Chromium), plus `ffmpeg` with libx264 and two fonts in the work folder:

```bash
mkdir -p demo/.work && cd demo/.work
echo '{"name":"olano-demo-work","private":true}' > package.json
npm i @fontsource-variable/inter@5.1.0 @fontsource-variable/jetbrains-mono
```

Then, from the repository root:

```bash
SLOW=3 DEMO=tour demo/make.sh                 # both layouts: record, voice, music, build
SLOW=3 DEMO=chat demo/make.sh landscape       # one layout
DEMO=tour demo/build.sh landscape             # rebuild from the frames already recorded
```

A take runs the page at 1/`SLOW` speed (so a small machine still gets enough frames) and takes
`SLOW` times the video's length. `make.sh` generates the voice when `audio/<demo>/` has none
(needs `OPENAI_API_KEY`; only the written lines are sent) and fits the music to the take;
`MUSIC=1` recomposes it after the timing changed.

```bash
OPENAI_API_KEY=… node demo/voiceover.mjs chat 0 3   # only lines 0 and 3
node demo/chart.mjs demo/assets/chat-sales.json demo/assets/chat-sales.png
DEMO=tour demo/clips.sh                             # one clip per chapter
```

The finished MP4s (`videos/`) and the work folder are not committed; the scripts, the voice and
music files and the chart are.

### The installation tutorials

`install` records a real install, not the launcher: run the Docker install into a scratch
directory first, then point the flow at it and at a script that puts it back to "no accounts".

```bash
OLANO_DIR=/tmp/olano-demo bash deploy/self-host/install.sh --local
cat > /tmp/olano-demo-reset.sh <<'SH'
cd /tmp/olano-demo && docker compose down -v >/dev/null 2>&1; rm -rf data
bash setup.sh --local >/dev/null 2>&1; grep '^OLANO_SETUP_CODE=' .env | cut -d= -f2
SH
DEMO_INSTALL_RESET=/tmp/olano-demo-reset.sh SLOW=3 DEMO=install demo/make.sh
```

The terminal in both tutorials shows what the commands print: `flow-install.mjs` replays the
installer's own output from a real run, and `flow-source.mjs` uses the step headings of `install.sh`, the
prompts of `olano setup` and the tables `olano doctor` and `olano dashboard` printed on a real run.
Long output between the headings is left out, and paths are shown as `/home/you/…`.

## Writing a flow

`flow-<demo>.mjs` exports `start` (the page to open), `ready` (what to wait for), `captions`
(`{eyebrow, h1, p}` per step), `brand` (the end card's line) and `run()`. Optional: `team` (when
it is not the demo's name), `css`, `keyboardWords`, `init`.

Inside `run()`:

- `mark('step')`: **call it at the start of every step.** Voice lines are placed on the marks and
  clips are cut at them. One voice line per mark, the last one over the end card (`mark('end')`).
- `S('caption', n)`, `S('appIn')`, `S('end')`, `S('hide')`: the stage.
- `tap(locator)`, `point(locator)`: the pointer glides there (and clicks).
- `type(locator, text)`, `send(text)`: key by key; the phone format shows an iOS keyboard.
- `scroll(dy)`, `scrollTo(locator, top)`: smooth scrolling of the nearest scrollable area.
- `nav(path)`: a sidebar link when one is showing, otherwise the router.
- `term.run(cmd)`, `term.out(lines)`: the terminal of the tutorials.
- `sleep(ms)`: video milliseconds.

## Rules that keep the videos honest

- Captions never cover the app, and they say what is on screen.
- The voice says the same thing as the caption in other words, in about 2.5 words a second.
- Made-up companies and people only; addresses end in `.example`.
- Nothing on screen is invented: what the dashboard shows is what the product does with the
  demo's data, and what the terminal shows is what the commands printed.
- Look at every video before it ships: a contact sheet
  (`ffmpeg -i x.mp4 -vf "fps=1/3,scale=480:-1,tile=5x5" -frames:v 1 sheet.jpg`), then listen once.
