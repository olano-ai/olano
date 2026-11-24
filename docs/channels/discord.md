# Discord Channel — Setup Guide

This guide walks you from "I have nothing" to "my Olano agent is replying in Discord". It covers every config field on the dashboard's `Edit connector: discord` form and explains the purpose of each one.

**Dashboard:** When you add or edit the Discord connector in the web UI, an inline **User guide** panel opens above the tip footer with the same essentials (token, IDs, invite URL, first message).

> **TL;DR for the impatient:**
>
> 1. [Create a Discord application](#1-create-a-discord-application) → copy **Bot Token**.
> 2. [Invite the bot to your server](#3-invite-the-bot-to-your-server) with the right permissions.
> 3. [Find your IDs](#4-find-your-discord-ids) (Server, Channel, Role).
> 4. [Fill in the dashboard form](#5-configure-olano) → save.
> 5. [Say hi](#6-say-the-first-hi).

---

## What each field means

The dashboard's `Edit connector: discord` form maps directly to these adapter config keys (`olano/channels/discord_channel.py`):

| Field on dashboard | Config key | What it actually does | Required? |
|---|---|---|---|
| **Bot token** | `token` | The runtime credential Discord uses to authenticate your bot. Encodes your Application ID inside it — that's why we don't ask for App ID separately. Keep it secret; if it leaks, regenerate it immediately. | **Yes** |
| **Allowed channels** | `allowed_channels` | List of channel snowflake IDs (numbers) the bot is allowed to respond in. Empty = respond in **every** channel of every allowed guild. DMs always bypass this gate. | No (recommended) |
| `guild_ids` (extra config) | `guild_ids` | List of server (guild) IDs the bot is allowed to operate in. Empty = every server the bot is invited to. Useful if you only want one staging server to be live while testing. | No |
| `allowed_roles` (extra config) | `allowed_roles` | List of role **names** (strings, not IDs). A user must have at least one of these roles to chat with the bot. Empty = anyone in an allowed channel. | No |
| `command_prefix` (extra config) | `command_prefix` | Reserved for future text-prefix commands; current adapter only uses slash commands (`/chat`, `/new`, `/clear`, `/status`). | No |
| `max_message_length` (extra config) | `max_message_length` | Per-message char cap before splitting (default `1900`; Discord's hard limit is `2000`). | No |

Everything below the four built-in fields is passed through as-is — type unknown keys into the dashboard's JSON area or edit `agent.json5` directly.

---

## 1. Create a Discord application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and sign in.
2. Click **New Application** → name it (e.g. `olano-merli-dev`) → **Create**.
3. In the left sidebar, click **Bot**.
4. Under **Privileged Gateway Intents**, enable:
   - ✅ **Server Members Intent** (needed for `allowed_roles` to see member roles)
   - ✅ **Message Content Intent** (needed for `on_message` text)
   - Presence Intent — optional, only if you want presence info.
5. Click **Reset Token** → **Copy**. **Save it once** — you can't view it again, only regenerate.

   This is the value for the **Bot token** field.

> ℹ️ The portal also shows an **Application ID** and a **Public Key**. Olano uses the gateway WebSocket model, so you don't need either of those — the bot token already encodes the Application ID, and the Public Key is only used by HTTP-interaction bots (serverless / Lambda style). See the README's "Trading & Brokerage Tools" / Discord notes for the full architectural explanation.

## 2. Choose the bot's permissions

In the developer portal:

1. Left sidebar → **Bot** → scroll down to **Bot Permissions**.
2. Tick the boxes the bot actually needs. For the standard chat use case:

   - ✅ **View Channels** — see the channels it's added to
   - ✅ **Send Messages** — post replies
   - ✅ **Send Messages in Threads** — reply in threads
   - ✅ **Read Message History** — see prior context in a channel
   - ✅ **Embed Links / Attach Files** — render rich responses
   - ✅ **Use Slash Commands** — register `/chat`, `/new`, `/clear`, `/status`
   - ✅ **Add Reactions** — optional, for ack/typing patterns
   - ❌ Avoid **Administrator** — too much blast radius for an LLM bot

   Discord aggregates these into a `permissions` integer (e.g. `274877910080`). You'll need it for the invite URL.

3. Left sidebar → **OAuth2** → **URL Generator**:
   - **SCOPES**: tick `bot` and `applications.commands`
   - **BOT PERMISSIONS**: re-tick the same boxes as above
   - Copy the generated URL at the bottom — that's your invite link.

## 3. Invite the bot to your server

1. Open the URL from step 2.3 in a browser logged in as a Discord user with **Manage Server** rights on the target server.
2. Pick the server → **Authorize**.
3. The bot now appears in the server's **Offline** list. It'll come online as soon as Olano starts talking to the gateway.

If you only want the bot to see **one specific channel** in a busy server, also do:

1. In Discord, right-click the channel → **Edit Channel** → **Permissions**.
2. Add the bot user (or its role — Discord auto-creates a role per bot named after the bot) and grant `View Channel` + `Send Messages`.
3. On every other channel, deny `View Channel` for that role.

This Discord-side restriction is independent of `allowed_channels` in Olano — they reinforce each other but don't replace each other:

- **Discord permissions** stop the bot from *reading* channels → cleanest, but per-channel UI work.
- **`allowed_channels` allowlist** stops the bot from *replying* even if it can read. One-line change in Olano.

Use both for sensitive deployments.

## 4. Find your Discord IDs

All Discord IDs are 17–19 digit "snowflakes". Two ways to grab them:

### A. Enable Developer Mode (one-time)

1. Discord client → **User Settings** (gear icon, bottom-left)
2. **Advanced** → toggle **Developer Mode** on
3. Right-clicking now reveals a **Copy ID** option:

   | Want | Right-click | Copy |
   |---|---|---|
   | **Server (guild) ID** → `guild_ids` | The server icon in the left sidebar | "Copy Server ID" |
   | **Channel ID** → `allowed_channels` | The channel name in the channel list | "Copy Channel ID" |
   | **Role ID** | A role mention or `Server Settings → Roles → role` | "Copy Role ID" |
   | **User ID** (for proactive DMs) | The user's avatar / name | "Copy User ID" |

### B. From the URL bar

When you click on a channel, the URL becomes:

```
https://discord.com/channels/123456789012345678/987654321098765432
                              ^ guild_id          ^ channel_id
```

For DM channels the first segment is `@me`:

```
https://discord.com/channels/@me/555444333222111000
                                  ^ DM channel ID (not the user's ID)
```

> Note: `allowed_roles` takes role **names** (strings), not IDs — e.g. `["agent-users", "moderators"]`. Names are what you see in `Server Settings → Roles`. Only collect role IDs if you decide to extend the adapter to match by ID.

## 5. Configure Olano

### Option A — dashboard (the form you already opened)

**Where this is stored:** Saving applies changes to that agent’s **`agent.json5`** on disk — `~/.olano/agents/<agent>/agent.json5` or `<project>/agents/<agent>/agent.json5` (local wins when both exist). Under `channels` you’ll see one object per connector (`type`, `token`, `guild_ids`, `allowed_channels`, …). Re-open **Edit** after save to confirm the row reloads from that file; restart the runtime so the bot reads it.

1. Open the Olano dashboard → your agent → **Connectors** → click the discord row → **Edit**.
2. Fill in:
   - **Bot token**: paste the token from step 1.5. For better hygiene, paste `${DISCORD_BOT_TOKEN}` and put the real token in your `.env` so it never enters the config file on disk.
   - **Allowed servers** / **Allowed channels**: type each 17–19 digit snowflake **on its own line**, or separate with commas or spaces. You can still paste a JSON array (e.g. `["1484827833078513700"]`); the form normalizes to string arrays in `agent.json5` without JavaScript number precision issues.
   - **Allowed roles**: role **names** as shown under Server Settings → Roles, one per line or comma-separated (or a JSON string array). These are not numeric role IDs.
3. Click **Save changes**.
4. For keys not on the form (e.g. `max_message_length`), edit `agent.json5` directly — see Option B.

### Option B — `agent.json5` (full control)

```json5
// agents/merli/agent.json5 (abbreviated)
{
  "name": "merli",
  "channels": [
    {
      "type": "discord",
      "token": "${DISCORD_BOT_TOKEN}",
      "guild_ids": ["123456789012345678"],
      "allowed_channels": ["1484827833078513700"],
      "allowed_roles": ["agent-users"],
      "max_message_length": 1900
    }
  ]
}
```

And in `.env`:

```bash
DISCORD_BOT_TOKEN=MTQ4Ng...your.long.token.here...xf4
```

### What each combination means

| `guild_ids` | `allowed_channels` | `allowed_roles` | Behavior |
|---|---|---|---|
| `[]` | `[]` | `[]` | Wide open — replies in any channel of any server it's in, to anyone. **Unsafe in production.** |
| `[G]` | `[]` | `[]` | Only inside server `G`, but every channel + every member. Good for testing. |
| `[G]` | `[C]` | `[]` | Only in channel `C` of server `G`. Recommended starting point. |
| `[G]` | `[C]` | `["agent-users"]` | Only in channel `C` of server `G`, only members with the `agent-users` role. Recommended for production. |
| `[]` | `[C]` | `[]` | In channel `C` regardless of server (rare; works if the channel ID is unique, which it always is). |

DMs always bypass `allowed_channels` (a DM has no "channel" you can list ahead of time). Restrict DMs via `allowed_roles` or `guild_ids` if needed — note that role membership is per-server, so a strict `allowed_roles` config effectively disables DMs from anyone outside the configured guild.

## 6. Say the first "hi"

After saving:

1. Start (or reload) the runtime so the channel adapter picks up the new config:

   ```bash
   olano serve            # full runtime (all agents, all channels)
   # or, for one agent only:
   olano serve --agents merli
   ```

   On startup you should see in the logs:

   ```
   Discord channel starting
   Discord bot connected as merli-dev#1234
   Discord slash commands synced
   ```

   The bot's status in your server flips from **Offline** to **Online** within a few seconds.

2. There are three ways to talk to it. Pick whichever feels natural:

   ### a) Plain message in the allowlisted channel

   Just type in the channel:

   ```
   hi merli, are you there?
   ```

   The bot responds inline. This uses Discord's regular message events — note the bot only sees this if it has `View Channel` + `Read Message History` permission AND **Message Content Intent** is enabled in the developer portal (step 1.4). If nothing happens, that's the most common cause.

   ### b) Slash command `/chat`

   In the same channel, type `/` — Discord pops up the autocomplete with `/chat`, `/new`, `/clear`, `/status`. Pick `/chat` and type your message. This works even if **Message Content Intent** is disabled, because Discord forwards interaction payloads explicitly.

   ### c) Direct message

   Click the bot's avatar in the member list → **Message**. DMs bypass `allowed_channels`, so as long as you pass `allowed_roles` (or there's no role gate) the bot will reply.

3. Useful slash commands once the conversation is going:

   | Command | Effect |
   |---|---|
   | `/chat <text>` | Send a message via slash command (defers + follows up so long replies don't time out) |
   | `/new` (alias `/clear`) | Drop the current LangGraph thread and start a fresh conversation in this channel |
   | `/status` | Show the current `thread_id` and the bot's UTC time — quick sanity check |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Bot stays **Offline** in the member list | Token is wrong or revoked. Check the runtime logs for `discord.errors.LoginFailure`. Regenerate the token in the developer portal. |
| Bot is online but ignores all messages | **Message Content Intent** not enabled in the developer portal. Privileged intents must be toggled on **and** the bot needs the `MESSAGE_CONTENT` privileged scope. |
| Slash commands don't appear in the autocomplete | Discord caches the global command list for up to 1 hour. To get instant updates during development, set `guild_ids` — the adapter syncs commands per-guild and they show up immediately. |
| Log: `50001` / **Missing Access** when syncing slash commands | The bot cannot register commands for that **guild id**: wrong ID, bot not in that server, or the bot was invited without the **`applications.commands`** OAuth scope. Re-invite using the URL Generator with `bot` + `applications.commands` (see §2–3). If you use several `guild_ids`, one bad ID no longer blocks the others — check logs for which guild failed. |
| `/chat` says "This bot is not enabled in this channel." | The channel ID isn't in `allowed_channels`. Fix the list and reload. |
| `/chat` says "Not authorized in this server." | The guild ID isn't in `guild_ids`. |
| `/chat` says "You don't have the required role." | User doesn't have any role from `allowed_roles`. Either grant the role or empty the list. |
| Bot replies in the wrong channel / multiple channels | `allowed_channels` is empty. Add the channel ID(s) you want and remove the others. |
| Long messages get cut off | Lower `max_message_length` if Discord rejects 1900-char chunks (rare); the adapter splits on code-fence boundaries by default. |
| `Discord gateway latency too high` in logs | The health watchdog is doing its job — flaky network. The adapter will rebuild the WebSocket automatically. If it persists, check your container's networking. |

## Security notes

- **Never commit your bot token** to git. Always reference it as `${DISCORD_BOT_TOKEN}` and store the value in `.env` (which is in `.gitignore`).
- A leaked bot token gives full control of the bot — including reading every message it can see. **Regenerate immediately** if leaked; the old token is invalidated atomically.
- Olano's PII / output scan still runs on Discord channel responses — secrets the agent might emit are redacted before send. See `olano/security/`.
- Use **`allowed_roles`** for production deployments. Without it, anyone who can DM the bot can chat with it (and rack up your model bill).
- Consider running the bot in a **dedicated test server** for the first week. The cost of a misbehaving agent posting in `#general` is non-zero.

## Reference

- Bot adapter source: `olano/channels/discord_channel.py`
- Health watchdog: `olano/channels/_health.py`
- Config validation: `olano/runtime/api.py` → `_normalize_channel_lists`
- Discord intents documentation: <https://discord.com/developers/docs/topics/gateway#gateway-intents>
- Discord permissions calculator: <https://discordapi.com/permissions.html>
