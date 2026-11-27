# Demo videos

Short product films of Olano: the real dashboard in a browser window (or, in the phone format, in an
iPhone), with captions, a pointer and a voiceover. Each one comes in landscape
(1920×1080, for YouTube and the website) and portrait (1080×1920, for Reels, Shorts and TikTok).

Nothing is filmed or edited by hand. A script drives the real dashboard in headless Chromium and
records it frame by frame; the voice is generated from the written lines, and one command rebuilds a video after a caption, a line or the dashboard itself changes.

```
 flow-<demo>.mjs ──► record.mjs ──► frames (JPEG) + marks.json (when each step starts)
   (the script)      (Chromium, stage.html,      │
                      the dashboard from         │   voiceover.mjs ──► audio/<demo>/vo0..N.mp3
                      serve.py)                  │
                                                 ▼
                              build.sh ─► mix.mjs (voice on the marks, −14 LUFS)
                                         ─► ffmpeg ─► videos/olano-<demo>-<layout>.mp4 (H.264, 60 fps)
                                                       └─► clips.sh ─► videos/clips/ (one per chapter)
```

## The videos

The finished videos are attached to the [`demo-videos-1` release](https://github.com/olano-ai/olano/releases/tag/demo-videos-1), not committed: release files cost a clone nothing, and a re-recorded video replaces its file there instead of adding a copy to the history.

| Demo | Story | Fictional team (`teams/`) | Watch |
|---|---|---|---|
| `tour` | Olano in a minute: agents, a chat, the org chart, the scheduler, an approval | Harbor & Pine, an online home-goods shop | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-tour-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-tour-portrait.mp4) |
| `create` | A new agent from the New agent dialog, then its first conversation | Casa Lumen, a small hotel | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-create-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-create-portrait.mp4) |
| `chat` | One question: the steps, a chart in the answer, the report as a file, another model | Kettle Hill Coffee, a roaster | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-chat-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-chat-portrait.mp4) |
| `team` | One agent asks two others; the exchange on record; shared issues | Fieldnote Studio, a marketing studio | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-team-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-team-portrait.mp4) |
| `schedule` | A weekday task, the calendar, Run now, the run as its own conversation | Brightside Lettings, a property manager | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-schedule-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-schedule-portrait.mp4) |
| `control` | An approval before an email is sent, the Vault, Insights, Revisions | Marlow & Finch, an accounting firm | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-control-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-control-portrait.mp4) |
| `install` | The one-line Docker install, the setup code, the first-run wizard | none: a real, fresh install | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-install-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-install-portrait.mp4) |
| `source` | Clone, `./install.sh`, `olano setup`, `olano doctor`, `olano dashboard` | Harbor & Pine, for the final shot | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-source-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-source-portrait.mp4) |
| `connect` | The Connections tab, an MCP server from the directory, a magic link from the chat | Saltmarsh Cycles, a bike shop | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-connect-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-connect-portrait.mp4) |
| `memory` | MEMORY.md, the daily log, Notes, Knowledge, then a new chat that uses a saved fact | Juniper Lane Veterinary Clinic | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-memory-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-memory-portrait.mp4) |
| `cortex` | A Pulse card, brainstorms, rated ideas, approving a promotion, a prototype, a project | Fernway Garden Centre | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-cortex-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-cortex-portrait.mp4) |
| `simple` | The simple chat interface: replies only, detail levels, a photo, a new agent | Willow Lane Vets | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-simple-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-simple-portrait.mp4) |
| `groups` | A group chat: three agents answer in turn, then one by name | Saltmarsh Cycles, a bike shop | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-groups-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-groups-portrait.mp4) |
| `pages` | An agent publishes a web page; the Pages tab, its password, the page itself | Fernhill Yoga, a yoga studio | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-pages-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-pages-portrait.mp4) |
| `mobile` | The iPhone and Android app (`mobile/`): chats, Cortex, an approval in the Inbox, Settings | Tidewater Cycles, a bike shop | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-mobile-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-mobile-portrait.mp4) |
| `external` | An agent as an MCP server: switch it on, mint a token, a real MCP client calls it | Pinecrest Veterinary Clinic | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-external-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-external-portrait.mp4) |
| `acp` | `olano acp` for editors: the Zed entry, a real ACP session from a test client | Copperline Labs, a software studio | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-acp-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-acp-portrait.mp4) |
| `browser` | Browser control: Config, Browser; a web order placed in a live browser; the approval before the form is sent; taking control | Thistledown Pottery, a ceramics studio | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-browser-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-1/olano-browser-portrait.mp4) |
| `kiwi` | Kiwi.com's public flight search MCP server added by its address; a real trip searched, then a day either way | Paper Kite Games, a board-game studio | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-kiwi-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-kiwi-portrait.mp4) |
| `coingecko` | CoinGecko's keyless public MCP server added by its address; a market snapshot, then a weekday brief on the scheduler | Tidepool Notes, a newsletter on digital payments | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-coingecko-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-coingecko-portrait.mp4) |
| `pricecheck` | Headless browser control on Amazon.sg: three earbuds compared from the live results page, then a daily price check on the scheduler | Pebble & Thread, a tailoring shop | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-pricecheck-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-pricecheck-portrait.mp4) |
| `weekend` | A family weekend from the Gardens by the Bay and NParks events pages, read live; a reminder per event on the calendar | Brightwater Swim School | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-weekend-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-weekend-portrait.mp4) |
| `calculator` | The CPF Board's public contribution calculator filled in live; the approval before Calculate; the result explained | Lantern Lane Dumplings, a dumpling kitchen | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-calculator-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-calculator-portrait.mp4) |
| `deepwiki` | DeepWiki's public MCP server from the directory; how a public repository (langchain-ai/langgraph) is organised, then the raw results in verbose mode | Tessellate Data, a data platform team | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-deepwiki-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-deepwiki-portrait.mp4) |
| `huggingface` | Hugging Face's MCP server added as a custom server with no account (`hf_whoami` off); an Indonesian speech-to-text model found on the Hub, with its licence | Lontar Labs, a voice-notes app team | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-huggingface-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-huggingface-portrait.mp4) |
| `playwright` | Microsoft's Playwright MCP server, run locally with `npx` (stdio); a package's release history read off PyPI in the server's own browser | Halyard Software, a Python services team | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-playwright-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-playwright-portrait.mp4) |
| `kalshi` | Kalshi's official MCP server added by its address, no account; the market's odds of a December Fed cut, then the Super Bowl halftime show, in plain chances | Loose Change, a podcast about money and the news | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-kalshi-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-kalshi-portrait.mp4) |
| `markets` | pdata's public prediction-market MCP server added by its address; where Polymarket and Kalshi price the same question differently, then the day's biggest moves | Second Look, a newsletter about forecasts | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-markets-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-markets-portrait.mp4) |
| `sports` | Sports Hub (`npx -y mcp-sports-hub`, stdio) with only its ESPN provider; today's NFL games with kickoff, ESPN's favourite and who is out | The Corner Booth, a sports bar | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-sports-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-sports-portrait.mp4) |
| `f1` | Sports Hub (`npx -y mcp-sports-hub`, stdio) with its Formula 1 preset; the standings and the last race, then two drivers' lap times as a chart | Slipstream, a sim-racing café | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-f1-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-f1-portrait.mp4) |
| `chess` | `lichess-mcp` (`npx`, stdio) added as a custom server; today's Lichess puzzle drawn as a board, then Magnus Carlsen's Lichess rating history as a chart, both rendered with `render_html_image` | Endgame Club, a Saturday chess club for kids | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-chess-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-chess-portrait.mp4) |
| `snack` | Open Food Facts (`npx @cyanheads/openfoodfacts-mcp-server`, stdio) added as a custom server; two cereals compared by barcode: Nutri-Score, NOVA, sugar, additives | The Okafor household's home agent | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-snack-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-snack-portrait.mp4) |
| `imagine` | Olano's built-in `image_generate` and `image_edit`: a logo and a menu photo drawn inline in the chat, then the photo made warmer with steam, opened in Files | Little Ember Bakehouse, a neighbourhood bakery | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-imagine-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-imagine-portrait.mp4) |
| `freeimages` | Pipeworx's public Wikimedia Commons MCP server added by its address; free-to-use latte art photos for a café poster with their licences, then the credit lines saved as a file | Little Fig Café | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-freeimages-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-freeimages-portrait.mp4) |
| `runweather` | Weather MCP (`npx @dangahagan/weather-mcp`, stdio, Open-Meteo data) added as a custom server; is the air good enough for a run tonight in Bangkok, and the best hour | Lamplight Runners, a beginners' running club | [desktop](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-runweather-landscape.mp4), [phone](https://github.com/olano-ai/olano/releases/download/demo-videos-2/olano-runweather-portrait.mp4) |
| `themes` | The thirteen dashboard themes: the account menu's Theme dropdown, the theme button through every theme on a chat with a chart, the Overview, the org chart, Config's list (kept per browser) | Nightjar Records, a record shop | not uploaded yet |
| `seats` | A team on one dashboard, each person with an agent of their own: an invite, a seat (agent owner), the org chart, the invited member's own view, her agent asking a colleague's agent for her, and the colleague's own view of it; every sign-in is the product's real auth | Brightmoor Architects, an architecture practice | not published yet |

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
SLOW=3 DEMO=tour demo/make.sh                 # both layouts: record, voice, build
SLOW=3 DEMO=chat demo/make.sh landscape       # one layout
DEMO=tour demo/build.sh landscape             # rebuild from the frames already recorded
```

A take runs the page at 1/`SLOW` speed (so a small machine still gets enough frames) and takes
`SLOW` times the video's length. `make.sh` generates the voice when `audio/<demo>/` has none
(needs `OPENAI_API_KEY`; only the written lines are sent). The lines are in `voiceover.mjs`, or in
the flow itself as `export const voice = [...]`.

The videos have no music. `MUSIC=1 demo/make.sh` composes an original piano bed fitted to the take
(`music.py`, numpy) and mixes it under the voice.

```bash
OPENAI_API_KEY=… node demo/voiceover.mjs chat 0 3   # only lines 0 and 3
node demo/chart.mjs demo/assets/chat-sales.json demo/assets/chat-sales.png
DEMO=tour demo/clips.sh                             # one clip per chapter
```

The finished MP4s (`videos/`) and the work folder are not committed; the scripts, the voice
files and the chart are.

### Videos that need more than the launcher

- `mobile` builds the app in `mobile/` and serves it next to the launcher's runtime (its flow's `init`
  does both; run `npm install` in `mobile/` first). It sets `device: 'phone'`, so the phone is on
  stage in landscape too.
- `connect` connects a real MCP server during the take, so it needs network access.
- `external` runs a real MCP client (`assets/mcp-ask.py`) against the take's own runtime and prints
  what it printed.
- `acp` replays `assets/acp-session.json`, the true output of the four commands on screen;
  `assets/acp-capture.py` records it again.
- `browser` drives a real live browser, so it needs docker and the image
  (`docker build -t olano-browser:latest docker/olano-browser`). The supplier's order form in it is
  a page `teams/browser.py` serves on port 8930 (`DEMO_SUPPLIER_PORT`), reached at the docker
  network's private address with `LIONCLAW_BROWSER_ALLOW_PRIVATE_NETWORK=1`, which the team file sets.
- `kiwi` and `coingecko` add a real public MCP server during the take (`https://mcp.kiwi.com`,
  `https://mcp.api.coingecko.com/mcp`, no account or key) and their agents' tool steps call it
  for real, so they need network access; the replies are built from what the server returned.
- `pricecheck`, `weekend` and `calculator` drive the product's headless browser against live public
  websites (Amazon.sg; Gardens by the Bay and NParks; the CPF Board's calculator), so they need
  network access. Where the web is reached only through an HTTPS proxy, the team files point the
  product's per-agent `BROWSER_PROXY_URL` setting at `HTTPS_PROXY`; Chromium must trust that proxy's
  certificate authority through the machine's own store (no check is turned off).
- `kalshi`, `markets`, `sports` and `f1` connect a real MCP server during the take and their agents'
  tool steps call it for real with the `mcp` SDK, so they need network access (`kalshi` and `markets`:
  `api.elections.kalshi.com`, `api.pdata.world`; `sports` and `f1`: `npx` plus `site.api.espn.com`,
  `api.jolpi.ca` and `api.openf1.org`). Behind an HTTPS proxy, the `sports` and `f1` team files start the
  Node server with `NODE_USE_ENV_PROXY=1` (Node 22.21 or later). Live odds, games and standings change, so the
  replies are built from whatever the servers return at take time.
- `deepwiki`, `huggingface` and `playwright` connect a real MCP server during the take and their
  agents' tool steps call it for real with the `mcp` SDK, so they need network access. `playwright`
  also needs `npx` (it starts `@playwright/mcp@latest` over stdio, from the command saved in the
  agent's `agent.json5`) and a Chrome for it to drive; behind an HTTPS proxy its team file passes the
  proxy through Playwright MCP's own `PLAYWRIGHT_MCP_PROXY_SERVER` setting. The MCP tab's Reload
  agent runs the product's own connect step for scripted agents (`serve.py`).
- `chess` and `snack` add a local stdio MCP server during the take (`npx lichess-mcp`,
  `npx @cyanheads/openfoodfacts-mcp-server`) and their agents' tool steps call it for real with the
  `mcp` SDK, so they need network access and `npx`. Node's `fetch` honours an HTTPS proxy only with
  `NODE_USE_ENV_PROXY=1`, which the team files set for the server. The Open Food Facts server needs
  Node 24: `teams/snack.py` puts `/opt/node24/bin` first on the runtime's `PATH` when it exists.
  `chess` works out the puzzle position with `python-chess` (`uv run --with chess`). Lichess can
  briefly answer an empty rating history after many calls; wait a few minutes and take again.
- `imagine` needs nothing extra: its three pictures were made once with the product's own
  `image_generate` / `image_edit` (`openai:gpt-image-2.5-flare`) and are kept in
  `assets/imagine-*.png` with each call's result in `assets/imagine-results.json`.
- `freeimages` and `runweather` connect a real MCP server during the take and call it for real with
  the `mcp` SDK, so they need network access (`runweather` also needs `npx`). Pipeworx caps anonymous
  use at 50 calls a day, so `teams/freeimages.py` keeps each answer in `.work/freeimages-cache.json`
  and reuses it; delete that file to ask again.

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
