import type { LocalDict } from "../../core/i18n";

// Config view translations. Shared keys (nav.*, common.*) resolve via the useT
// fallback into the central dictionary, so only Config-local strings live here.
export const STR: LocalDict = {
  en: {
    "page.title": "Config",
    "page.subtitle": "Global Olano settings — the contents of config.yaml.",
    "badge.restart": "Restart required",
    "badge.restart.title": "Saved immediately, but only takes effect after the runtime restarts.",
    "configPath": "Editing {path}",

    "btn.save": "Save changes",
    "btn.saving": "Saving…",
    "btn.reset": "Discard",
    "unsaved.pill": "Unsaved changes",
    "unsaved.title": "Save your changes?",
    "unsaved.body":
      "You changed settings on this page that have not been saved. Leaving now discards them.",
    "unsaved.bodyInvalid":
      "Some fields on this page are invalid (marked in red), so the changes cannot be saved as they are. Fix them and save, or leave and discard them.",
    "unsaved.saveLeave": "Save and leave",
    "unsaved.leave": "Leave without saving",
    "unsaved.stay": "Stay on this page",
    "unsaved.nudge": "You have unsaved changes on the Config page — remember to save them.",

    "toast.loadFailed": "Couldn't load the config.",
    "toast.saved": "Config saved.",
    "toast.savedRestart": "Config saved. Restart the runtime for port / Team accounts changes to take effect.",
    "toast.saveFailed": "Couldn't save the config.",
    "toast.notAdmin": "Only admins can change global settings.",

    // General
    "section.general": "General",
    "section.general.hint": "Defaults the CLI and dashboard use when no agent is named.",
    "field.defaultAgent": "Default agent",
    "field.defaultAgent.hint": "Used when a command doesn't name an agent.",
    "field.preferredModel": "Preferred model",
    "field.preferredModel.hint": "Fallback model for agents with none set. Blank ⇒ built-in default.",
    "field.theme": "Theme",
    "field.theme.hint": "Appearance for this browser. Saved locally — not part of config.yaml.",
    "model.builtinDefault": "Built-in default",
    "model.inheritAgent": "Inherit the agent's model",

    // Cognition models
    "section.cognition": "Cognition models",
    "section.cognition.hint": "Models the always-on Cortex engine uses for its phases.",
    "field.fastModel": "Fast model",
    "field.fastModel.hint":
      "Cheap, quick model driving most cognition phases (idea capture, triage, scoring). Format is provider:model, e.g. \u201colano:turbo\u201d, \u201copenai:gpt-5.4-mini\u201d, \u201canthropic:claude-haiku-4-5-20251001\u201d. On a deployment connected to cloud.olano.ai this defaults to \u201colano:turbo\u201d \u2014 the fast Olano tier, billed in credits with no API key needed; elsewhere it defaults to a bring-your-own-key model, whose provider key must be set under Secrets. Clearing the field restores whichever of those two applies to this box. Cognition runs constantly, so this is the model that drives most of the cost \u2014 keep it cheap.",
    "field.deepModel": "Deep model",
    "field.deepModel.hint":
      "Heavier model for deep runs (exploration, prototype drafting) \u2014 same provider:model format as above. On a deployment connected to cloud.olano.ai this defaults to \u201colano:pro\u201d, billed in credits with no API key needed; elsewhere it defaults to a bring-your-own-key model. Leave it blank to inherit whatever model each agent already uses, which is the right choice if you would rather not run two model bills.",

    // Default model aliases (default:turbo / default:pro / default:max)
    "section.defaultModels": "Default models",
    "section.defaultModels.hint":
      "What the deployment-wide model aliases point at. Any agent, subagent or picker set to \u201cDefault \u00b7 Turbo/Pro/Max\u201d follows the target you choose here \u2014 change it once and every agent using that alias switches on its next reload, with no per-agent edits. Catalog agents install on Default \u00b7 Turbo (experts) and Default \u00b7 Pro (teams).",
    "model.builtinTarget": "Box default \u2014 {model}",
    "field.defaultTurbo": "Turbo target",
    "field.defaultTurbo.hint":
      "The concrete model behind \u201cDefault \u00b7 Turbo\u201d \u2014 the everyday tier most agents and catalog experts run on. Pick any model in provider:model form; another default:* alias is not allowed (aliases cannot chain). Left on the box default it resolves to the fast Olano tier on a deployment connected to cloud.olano.ai (billed in credits, no API key), and to a bring-your-own-key model elsewhere (its provider must be connected or have a key under Secrets, or every agent on this alias fails at its next turn). Applies when an agent next boots or reloads.",
    "field.defaultPro": "Pro target",
    "field.defaultPro.hint":
      "The concrete model behind \u201cDefault \u00b7 Pro\u201d \u2014 the heavier tier team supervisors install on. Same rules as the Turbo target: provider:model format, no default:* values, box default = the Olano Pro tier on a managed deployment or a bring-your-own-key model elsewhere, and changes apply on each agent's next boot or reload.",
    "field.defaultMax": "Max target",
    "field.defaultMax.hint":
      "The concrete model behind \u201cDefault \u00b7 Max\u201d \u2014 the most capable (and most expensive) tier, for agents you explicitly point at it. Same rules as the other targets: provider:model format, no default:* values, box default = the Olano Max tier on a managed deployment or a bring-your-own-key model elsewhere, and changes apply on each agent's next boot or reload.",

    // Speech (STT/TTS)
    "section.speech": "Speech",
    "section.speech.hint":
      "Voice-note transcription and voice replies on the chat platforms. Transcription (STT) defaults to the Olano service on managed deployments and to off elsewhere; voice replies (TTS) are off on every install until you pick a provider here.",
    "field.speechStt": "Speech-to-text (voice notes)",
    "field.speechStt.hint": "Transcribes inbound voice messages for the agent. Options: Disabled = voice notes ask the sender to resend as text (the default on self-hosted installs); Olano = managed transcription billed in olano credits (the default on managed deployments, no API key needed); OpenAI/Groq = your own API key; Qwen is TTS-only and will not transcribe; Custom = any self-hosted OpenAI-compatible server.",
    "field.speechTts": "Text-to-speech (voice replies)",
    "field.speechTts.hint": "Voice-bubble replies to voice messages, and the text_to_speech tool. Options: Disabled = replies stay text-only (the default on every install — voice replies are always an explicit opt-in); Olano = managed synthesis billed in olano credits (no API key needed, managed deployments only); OpenAI/Groq/Qwen = your own API key; Custom = any self-hosted OpenAI-compatible server.",
    "field.speechModel": "Model",
    "field.speechModel.hint": "Blank ⇒ the provider's default.",
    "field.speechVoice": "Voice",
    "field.speechVoice.hint": "Blank ⇒ the provider's default voice. The play button reads two short sample sentences with the provider, model and voice chosen here, including changes you have not saved yet, so you can hear a voice before keeping it; press it again to stop. It is a real speech call, billed like a voice reply (olano credits on Olano, your own key otherwise).",
    "speech.preview.play": "Preview this voice",
    "speech.preview.loading": "Generating the sample (click to cancel)",
    "speech.preview.stop": "Stop the preview",
    "speech.preview.failed": "Couldn't play the voice preview.",
    "speech.preview.sample": "Hello! This is how your agent will sound when it replies with a voice message.",
    "field.speechFormat": "Audio format",
    "field.speechFormat.hint":
      "The audio container requested from the provider — this is what decides whether a voice reply arrives as a native voice bubble or as an attached audio file. Only Ogg/Opus can be a bubble (Telegram's round player, a WhatsApp push-to-talk); every other container is delivered as a playable audio file instead. Options: Auto (the default — leave it on this) asks for Opus on OpenAI and Olano, WAV on Groq PlayAI (the only container it supports), and asks for nothing at all on Qwen (DashScope has no such parameter) or a Custom server (which may reject a format it never implemented); Opus gives voice bubbles — pick it explicitly for a self-hosted server that supports it; MP3, WAV, FLAC, AAC and M4A are all delivered as audio files, never bubbles (WAV and FLAC are uncompressed, so expect several MB per reply); PCM is raw samples for post-processing pipelines and is not playable in a chat app. Whatever the provider really returns is detected from the audio itself, so a provider that ignores this setting downgrades to a correctly-labelled audio file rather than a broken bubble.",
    "speech.format.auto": "Auto (per provider — recommended)",
    "speech.format.opus": "Opus / Ogg (voice bubbles)",
    "speech.format.mp3": "MP3 (audio file)",
    "speech.format.wav": "WAV (audio file, uncompressed)",
    "speech.format.flac": "FLAC (audio file, lossless)",
    "speech.format.aac": "AAC (audio file)",
    "speech.format.m4a": "M4A (audio file)",
    "speech.format.pcm": "PCM (raw samples — not playable in chat)",
    "field.speechUrl": "Endpoint URL",
    "field.speechUrl.hint": "Any OpenAI-compatible server, e.g. self-hosted Speaches / faster-whisper.",
    "field.speechKeyName": "API key credential",
    "field.speechKeyName.hint": "Name of the vault/env credential holding the key. Blank if the server needs none.",
    "speech.provider.disabled": "Disabled (default)",
    "speech.provider.olano": "Olano (managed — billed in olano credits)",
    "speech.provider.openai": "OpenAI",
    "speech.provider.groq": "Groq",
    "speech.provider.qwen": "Qwen TTS (Alibaba)",
    "speech.provider.custom": "Custom (self-hosted)",
    "placeholder.providerDefault": "Provider default",

    // Media & search
    "section.media": "Media & search",
    "section.media.hint":
      "Which service powers image understanding, image generation, and web search. Each list shows the Olano managed service (billed in olano credits, when your plan includes it) plus any provider whose API key this box already has.",
    "field.mediaVision": "Image understanding (vision)",
    "field.mediaVision.hint":
      "The model that reads images for the agent (the image_describe tool). Auto (default) uses Olano Vision on a managed plan, otherwise the first vision-capable provider key this box has. Pick “Olano Vision” to bill it in olano credits with no key, or pick one of your own providers' vision models (only providers whose key is present are listed). Overrides the legacy LIONCLAW_VISION_MODEL environment variable when set; leave on Auto to keep honoring it.",
    "field.mediaImage": "Image generation & editing",
    "field.mediaImage.hint":
      "The backend for the image_generate / image_edit tools. Auto (default) uses Olano Image on a managed plan, otherwise your OpenAI or Gemini image key. Pick “Olano Image” to bill it in olano credits with no key, or one of your own providers' image models (OpenAI gpt-image / DALL·E, Google Imagen / Gemini image). Editing needs a backend that serves image edits — Gemini image models generate only. Overrides the legacy LIONCLAW_IMAGE_MODEL environment variable when set; leave on Auto to keep honoring it.",
    "field.webSearch": "Web search",
    "field.webSearch.hint":
      "The source for the web_search / page_extract tools. Auto (default) prefers your own Tavily key when TAVILY_API_KEY is set, otherwise the Olano managed web search (billed in olano credits). “Olano web search” forces the managed service even if a Tavily key is present; “Tavily” forces your own key (shown only when TAVILY_API_KEY is set); “Disabled” turns web search off entirely and the tools return an error telling the agent it is off.",
    "media.auto": "Auto (recommended)",
    "media.group.olano": "Olano managed (olano credits)",
    "media.group.byok": "Your providers",

    // Network & access
    "section.network": "Network & access",
    "section.network.hint": "Ports and the Team accounts sign-in gate. Changes apply after a restart.",
    "field.runtimeApiPort": "Runtime API port",
    "field.dashboardPort": "Dashboard port",
    "field.multiUser": "Team accounts",
    "field.multiUser.hint":
      "Require per-person sign-in to this dashboard, with human accounts the admin invites (owner/admin + members) and per-agent access control. Off: anyone who can reach the dashboard acts as the owner/admin (classic single-owner install). When this key has never been set, the default follows the install: cloud-managed deployments run with it ON; manual installs default to off. Saving this form stores your explicit choice, which always wins. Turning it on with no accounts yet shows the create-admin screen on next load. Stored as multi_user_enabled in config.yaml. Not related to the per-agent \"Multi-user mode\" tab, which isolates a shared agent's chat end-users from each other.",
    "field.highlightsAll": "Share highlights fleet-wide",
    "field.highlightsAll.hint": "Show every agent’s daily/weekly/monthly work highlights to all users. Off (default): members see only their own agents’ highlights.",
    "field.playground": "Playground",
    "field.playground.hint": "Adds the Playground tab: an animated 2D world where this deployment’s agents walk around, and where character generation also produces animation sprite sheets (a slow, multi-step image job). On or off (default: off) — while it is off the tab is hidden, the view is never loaded, nothing renders in the background, and character generation makes only the quick profile portrait. The switch is deployment-wide (every user of this box sees the same thing), applies immediately with no restart, and agents whose characters were generated while it was off get their sprites the next time you regenerate them. On a managed deployment your plan must also allow the Playground tab.",

    // Member bot creation (config.yaml self_serve_agents:)
    "section.selfServe": "Member bot creation",
    "section.selfServe.hint":
      "Whether people who are not admins can create bots of their own — the “New bot” button in the simple view, and the same creation endpoint everywhere else. Admins can always create bots; nothing in this section applies to them. Changes apply on save, no restart needed. Stored as self_serve_agents in config.yaml.",
    "field.ssaEnabled": "Let members create bots",
    "field.ssaEnabled.hint":
      "On (the default): every signed-in member gets a New bot button and can create bots of their own, within the limit and template list below; whoever creates a bot automatically owns it. Off: members only chat with bots an admin has granted them, and the button disappears from their view (admins keep theirs). This changes creation only — members can never edit, reconfigure or delete bots they don’t own, in either position.",
    "field.ssaMax": "Bots per member",
    "field.ssaMax.hint":
      "How many bots one member may own at once — bots they created, or were made owner of. A whole number from 1 to 100; the default is 3. Counted by ownership, so being invited into someone else’s bot never uses up this allowance. Admins are exempt. A member at the limit gets a clear “limit reached” message instead of a new bot.",
    "field.ssaArchetypes": "Allowed templates",
    "field.ssaArchetypes.placeholder": "Any template (leave empty)",
    "field.ssaArchetypes.hint":
      "Restrict which ready-made templates members may create bots from. One template name per line, exactly as the template is named in the catalog (e.g. sales-helper). Leave empty — the default — to allow every template in the catalog and also from-scratch bots described in plain words. A non-empty list is strict: members can only pick from it, and from-scratch creation is refused too. Never applies to admins.",

    // Multi-bot group threads (config.yaml groups:)
    "section.groups": "Group chats",
    "section.groups.hint":
      "One conversation that several of your bots share, alongside the usual one-to-one chats. You post once and an addressed member answers; every message in the thread is labelled with who wrote it. In the simple view a “Group chats” section appears in the bot list with a “New group” button, and each group gets an info panel for adding and removing members. Changes apply on save, no restart needed. Stored as groups in config.yaml.",
    "field.grpEnabled": "Enable group chats",
    "field.grpEnabled.hint":
      "On (the default): the group surface exists — people can create groups, open them, and add or remove bots from a group’s info panel, and bots given the “groups” tool can read and post in groups they belong to. Off: no group section appears anywhere, existing groups become unreachable, and every group web address answers “not found”. Turning it off never deletes a group; turning it back on brings the same groups and their transcripts back exactly as they were. Whether the bots then answer each other is the separate “Let bots reply to each other” switch below, which is also on by default.",
    "field.grpMin": "Fewest bots in a group",
    "field.grpMax": "Most bots in a group",
    "field.grpMin.hint":
      "The smallest roster someone may create a group with. A whole number from 2 to 6; the default is 2. Below two members a “group” is just a one-to-one chat, which is why 2 is the floor. This is checked when a group is created and when a member is removed — the last removal that would take a group under this number is refused, with the reason shown.",
    "field.grpMax.hint":
      "The largest roster a group may reach, checked on creation and on every “add a bot”. A whole number from 2 to 6 and never below the “Fewest bots” value above; the default is 6. Each member is a running bot holding its own copy of the conversation, so this is a real cost and memory ceiling rather than a style choice. Lower it freely — groups already over the new limit keep working and simply refuse further additions.",
    "field.grpAutonomous": "Let bots reply to each other",
    "field.grpAutonomous.hint":
      "On (the default): when a bot’s reply names another member with an @mention, that member is handed the next turn automatically — so a single message from you can set off a chain of bots answering each other with nobody in between, and every bot-to-bot answer is tagged with who it is answering. That is the point of a group, and it is also the setting that spends model calls you did not individually ask for, which is why it is separate from the switch above and why the two limits below exist. Every stop — hop limit, timeout, or two bots volleying — is written into the conversation, so a chain never just goes quiet. Off: you post, the bots you named answer, and the thread waits for you; an @mention in a reply then only marks who should pick the work up. If you would rather watch a few threads first, turn it off here and back on later — existing groups are kept either way.",
    "field.grpHops": "Bot-to-bot turns per message",
    "field.grpHops.hint":
      "How many times bots may hand the turn to each other after one message from you. A whole number from 1 to 10; the default is 3. Counted per message you send, so a five-message conversation with the default allows at most three bot-to-bot turns each time, not three in total. When the allowance runs out the thread stops and says so in the conversation. Only applies while “Let bots reply to each other” is on — with it off, no message ever gets a second turn.",
    "field.grpTimeout": "Chain time limit (seconds)",
    "field.grpTimeout.hint":
      "How long one chain of bot-to-bot replies may run in total before it stops itself, in seconds. A whole number from 10 to 1800 (30 minutes); the default is 180 (3 minutes). This is wall-clock time across the whole chain, so a slow model reaching the limit mid-turn ends the chain there with a note in the conversation rather than leaving it hanging. Raise it together with the turn count above — a bigger turn allowance under the same time limit just converts “out of turns” into “out of time”. Only applies while “Let bots reply to each other” is on.",

    // Agent self-configuration (config.yaml agent_autonomy:)
    "section.autonomy": "Agent self-configuration",
    "section.autonomy.hint":
      "Deployment-wide defaults for what agents may change about THEMSELVES when asked to in a conversation — each switch grants a set of self_* chat tools to every agent, and “Send magic links” grants the magic_link_* tools agents use to hand a person a browser page. All on by default. A single agent can override any switch in either direction from its own page (Agents → agent → Chat self-configuration, three-state with Inherit). Changes apply to each agent's next tool call, no restart. Exception: agents in customer-service mode (per-customer isolation) inherit everything Off regardless of these switches — strangers must not reconfigure a support bot — and only an explicit per-agent On re-enables a capability there. These govern the agent acting on itself only — human permissions and the AgentFather management tools are unaffected. Stored as agent_autonomy in config.yaml.",
    "field.aut.explore_archetypes": "Browse archetype library",
    "field.aut.explore_archetypes.hint":
      "Read-only: agents can search and read the ready-made expert/team templates, e.g. to suggest a specialist worth adding. Grants no install ability by itself.",
    "field.aut.change_model": "Change their own model",
    "field.aut.change_model.hint":
      "Agents can list the models this deployment can reach and switch themselves to one (applies after the agent restarts). Unconnected providers are refused. On a managed deployment a bigger model spends credits faster.",
    "field.aut.modify_prompt": "Edit their own instructions",
    "field.aut.modify_prompt.hint":
      "Agents can rewrite their own system prompt when asked (full replacement, applies after restart). Every change lands in Config → Revisions under the agent's name, reviewable and revertible.",
    "field.aut.restart_self": "Restart themselves",
    "field.aut.restart_self.hint":
      "Agents can restart themselves so saved changes take effect — a few seconds of downtime, scheduled just after the reply that announces it.",
    "field.aut.add_subagents": "Add subagents",
    "field.aut.add_subagents.hint":
      "Agents can add specialist subagents to themselves — from scratch or drafted from an archetype — and delegate work to them. Applies after restart.",
    "field.aut.connect_mcps": "Connect services (MCP servers and built-in toolkits)",
    "field.aut.connect_mcps.hint":
      "Agents can search the MCP directory and add, remove or OAuth-authorize MCP servers on themselves. Sign-ins happen in the user's own browser; API keys go through the vault, never into config files.",
    "field.aut.connect_platforms": "Connect messaging platforms",
    "field.aut.connect_platforms.hint":
      "Agents can wire messaging channels onto themselves: token platforms (Telegram, Discord, Slack — the bot token is stored in the vault) and WhatsApp by QR shown right in the chat. A new channel is a new inbound door; agents are instructed to insist on an allowed-users list.",
    "field.aut.manage_skills": "Manage their own skills",
    "field.aut.manage_skills.hint":
      "Agents can list the skills they have and switch individual ones on or off for themselves — the same switches as each agent's Skills tab. Never installs, edits or deletes a skill, and switching off a shared skill affects only the agent that did it. Applies after that agent restarts.",
    "field.aut.magic_links": "Send magic links",
    "field.aut.magic_links.hint":
      "Agents can send magic links: short-lived, PIN-protected pages a person opens in their own browser to finish what a chat cannot — sign in to a service or MCP server (OAuth / device flow), type an API key straight into the vault so it never passes through the conversation, connect WhatsApp/WeChat by QR or Telegram/Discord/Slack by bot token, edit an agent's instructions or one file, or open the dashboard on their phone. This is the default way agents connect themselves to anything, so it ships On. Off removes every magic_link_* tool from every agent that inherits this switch; one agent can turn it back On from its own page, and a single tool can be dropped per agent on its Tools tab. Owners only (on messengers, the channel's allowed-users list), group chats refused, approval card on the stricter security profiles. Config → Magic links (enabled) stays the hard off switch for the whole deployment and separately gates the dashboard sign-in link.",

    // Message rate limits (home-wide defaults)
    "section.rateLimits": "Message rate limits",
    "section.rateLimits.hint":
      "Default caps on inbound messages, per agent. Agents can tighten these individually. Empty = unlimited. Scheduled and autonomous work is exempt.",
    "field.rlPerMinute": "Messages / minute",
    "field.rlPerHour": "Messages / hour",
    "field.rlPerDay": "Messages / day",
    "field.rlUnlimited": "unlimited",
    "field.rlPerPlatform": "Per-platform overrides (advanced)",
    "field.rlPerPlatform.hint":
      "Default caps for a specific channel (min / hour / day), e.g. telegram or dashboard.",
    "field.rlAddPlatform": "+ Add platform…",
    "field.rlRemove": "Remove",
    "field.rlPh.per_minute": "/min",
    "field.rlPh.per_hour": "/hr",
    "field.rlPh.per_day": "/day",

    // Magic links
    "section.magicLinks": "Magic links",
    "section.magicLinks.hint":
      "Short-lived pages this instance mints so someone with chat but no dashboard can finish a sign-in, edit a file, or hand over an API key, on their own phone.",
    "field.mlEnabled": "Enable magic links",
    "field.mlEnabled.hint":
      "Off ⇒ nothing can mint a magic link (agents, chat commands, the dashboard, the CLI) and existing links stop opening.",
    "field.mlTtl": "Link lifetime (min)",
    "field.mlTtl.hint": "2–1440 minutes. Shorter is safer.",
    "field.mlPin": "PIN",
    "field.mlPin.hint": "A forwarded or leaked link is useless without it.",
    "mlPin.required": "Required (recommended)",
    "mlPin.off": "Off — the link alone opens the page",
    "field.mlAttempts": "Wrong PINs allowed",
    "field.mlAttempts.hint": "1–10, then the link burns itself.",
    "field.mlGroups": "Allow links in group chats",
    "field.mlGroups.hint":
      "Off ⇒ an agent refuses to send a link in a group, where every member would see the link and its PIN.",
    // Dashboard access (owner-login) — signs the owner into the dashboard.
    "section.ownerLogin": "Dashboard access",
    "section.ownerLogin.hint":
      "Let an agent send you a one-time magic link that signs you into this dashboard on your phone — no password to type.",
    "field.olEnabled": "Dashboard magic links",
    "field.olEnabled.hint":
      "Off ⇒ agents and chat commands can't mint a magic link that signs you into the dashboard.",
    "field.olAutoBind": "Skip the password for a single-admin deployment",
    "field.olAutoBind.hint":
      "Trusts that the allowlisted chat user is the admin and signs them in without a password. Only safe on a single-admin instance.",
    "field.olSessionHours": "Dashboard session length (hours)",
    "field.olSessionHours.hint":
      "1–720. How long a session opened from a magic link stays valid before re-auth.",

    // Disk monitor & cleanup
    // Usage budgets
    "section.budgets": "Usage budgets",
    "section.budgets.hint":
      "Ceilings on how many tokens the agents on this deployment may spend, and what it may cost. A turn is refused once a budget is exhausted, and the refusal names the budget. Changes apply to the next turn — no restart.",
    "budgets.col.budget": "Budget",
    "budgets.col.used": "Used",
    "budgets.col.limit": "Limit",
    "budgets.window.daily": "Resets daily (UTC)",
    "budgets.window.weekly": "Resets weekly (Mondays, UTC)",
    "budgets.window.monthly": "Resets monthly (1st, UTC)",
    "budgets.window.lifetime": "Never resets",
    "budgets.overridden": "set here, over the plan's {plan}",
    "budgets.signed": "set by your plan",
    "budgets.placeholder": "Plan default: {plan}",
    "budgets.unmanaged":
      "This install is unmanaged, so nothing is counted or enforced — the limits below are inert until the deployment is licensed.",
    "budgets.help":
      "Token budgets are runaway-loop guards, and you own them: enter a number of tokens (e.g. 50000000000), the word unlimited to remove the ceiling entirely, or leave the field empty to use your plan's. They count every token an agent spends, including calls on your own provider keys — so a budget can stop a BYOK agent even though nothing was billed to Olano. The two cost budgets are set by your plan and cannot be changed here; they cap what Olano itself pays upstream, and managed spend is separately limited by your credit balance.",
    "field.budgetAlerts": "Budget notifications",
    "field.budgetAlerts.hint":
      "Raise a dashboard notification as a budget fills, so nobody first meets a ceiling as a refused turn. On by default. Turning this off silences every budget notification, including the one saying a budget is spent and turns are being refused.",
    "field.budgetPercents": "Warn at",
    "field.budgetPercents.hint":
      "Percentages of a budget that raise a notification, comma-separated — e.g. 50, 60, 70, 80, 90, 95, 99 (the default). Each fires once per budget per window, and a turn that jumps several at once raises only the highest. Values must be 1–99; 100% is always announced and needs no entry. Applies to every budget above, tokens and cost alike.",

    "section.disk": "Disk monitor & cleanup",
    "section.disk.hint":
      "Watches disk usage and frees space automatically before the server runs out. Changes apply within one check interval — no restart.",
    "field.dmEnabled": "Monitoring & alerts",
    "field.dmEnabled.hint": "Measure disk usage on a schedule and alert when it fills up.",
    "field.dmPath": "Path to watch",
    "field.dmPath.hint": "Filesystem to measure. / covers the whole system disk.",
    "field.dmInterval": "Check every (min)",
    "field.dmInterval.hint": "5–1440 minutes.",
    "field.dmWarn": "Warn at (%)",
    "field.dmWarn.hint": "Logs a warning and shows a dashboard alert.",
    "field.dmCleanupPct": "Auto-clean at (%)",
    "field.dmCleanupPct.hint": "Runs the cleanup actions below.",
    "field.dmThresholdOrder": "Auto-clean must be at or above the warn threshold.",
    "field.dmCleanupEnabled": "Automatic cleanup",
    "field.dmCleanupEnabled.hint":
      "Run the selected actions when usage crosses the auto-clean threshold. Safe actions only delete caches and old logs; aggressive ones can also remove data other software would re-download.",
    "disk.actions.hint":
      "Pick what gets cleaned. Everything is on by default — including the aggressive docker_prune, which removes things other software would re-download (e.g. all unused Docker images). Untick an action to opt out, e.g. docker_prune if other software on this server relies on cached Docker data.",
    "disk.badge.aggressive": "aggressive",
    "disk.badge.aggressive.title":
      "Frees the most space, but removes things other programs may want back (they re-download on next use).",
    "disk.badge.unavailable": "unavailable here",
    "field.dmCooldown": "Cooldown (min)",
    "field.dmCooldown.hint": "Minimum gap between automatic cleanups.",
    "field.dmForceDays": "Also clean every (days)",
    "field.dmForceDays.hint": "0 = only when the threshold is crossed.",
    "field.dmVacuum": "Journal size cap",
    "field.dmVacuum.hint": "Old system logs are trimmed down to this size, e.g. 200M or 1G.",

    // Paths
    // MCP connection health (config.yaml → mcp_health)
    "section.gateway": "Olano model gateway",
    "section.gateway.hint": "Whether this deployment holds the key that Olano-managed models (olano:turbo, olano:pro, and the built-in image, vision and speech tools) authenticate with. The key arrives automatically — at provisioning, or handed over on the settings poll — so there is nothing to configure here. This card exists so a failure is visible without a shell.",
    "field.gwState": "Status",
    "field.gwState.hint": "Connected means this box holds a working gateway key and Olano-managed models are billed in credits. Not working means the box is attached to cloud.olano.ai but has no key yet, so every olano: model call fails until one arrives.",
    "gw.ok": "connected",
    "gw.degraded": "not working",
    "gw.explain": "Olano-managed models will fail until a key arrives. The box retries on its own and is handed one on its settings poll — usually within 15 minutes. Restarting the instance makes it immediate.",
    "gw.lastError": "Last error: {error}",
    "gw.retry": "Retrying in ~{seconds}s (attempt {attempts}). A restart retries straight away.",
    "gw.envRevoked": "A gateway key set in this machine's environment file was revoked and is being ignored in favour of the current one. Nothing is broken; that line is now dead weight.",
    "gw.sentinelBuilds": "{n} model(s) were built before a key was available — their calls fail until the agent is rebuilt or the instance restarts.",
    "gw.lastSuccess": "Key last obtained {when}",
    "gw.unrouted": "The gateway is not serving {groups}, which this deployment is entitled to use. Calls to those services fail with an invalid-model error — image understanding, image generation and speech all run through them. This is an Olano-side routing problem: nothing on this box can fix it, and your own provider keys are unaffected.",
    "section.catalogs": "Cloud catalogs",
    "section.catalogs.hint":
      "Model pricing, the MCP directory, credit rates, agent archetypes and official connector clients sync from cloud.olano.ai in the background (the revision manifest is checked hourly by default). Each chip shows a catalog's revision, where it was served from (cloud / disk / bundled), and for connectors how many official apps are available. Sync now re-fetches everything immediately — use it right after enabling a new official connector in the admin console.",
    "catalogs.syncNow": "Sync now",
    "catalogs.syncing": "Syncing…",
    "catalogs.toast.synced": "Catalogs synced — {updated} of {total} updated.",
    "catalogs.toast.failed": "Catalog sync failed: {detail}",
    "catalogs.loadFailed": "Could not load catalog status.",
    "catalogs.fetchedAt": "Last fetched {when}",
    "catalogs.neverFetched": "Never fetched from the cloud",
    "section.diskEncryption": "Disk encryption",
    "section.diskEncryption.hint":
      "This instance keeps everything it stores (agents, memory, files, conversations, the vault, the database) on an encrypted volume that is unlocked automatically when it boots. This card shows the state of that volume and holds the one action it asks of you: creating a recovery key, a second key that only you have.",
    "diskEncryption.status": "Custody",
    "diskEncryption.status.hint":
      "Standard custody protects your data against disclosure from stolen disks, snapshots, backups and offline storage access. The instance decrypts automatically at boot using Olano-operated key infrastructure. Olano's production control plane retains the technical capability to authorize key release.",
    "diskEncryption.status.standard": "Standard custody",
    "diskEncryption.status.other": "Sealed ({mode})",
    "diskEncryption.volume": "Encrypted volume",
    "diskEncryption.volume.hint":
      "The identifier of the encrypted volume (its LUKS UUID). It is not a secret: it names the volume in the instance's own logs and in the Olano console, so a header backup or a key release can be matched to the right volume.",
    "diskEncryption.recovery": "Recovery key",
    "diskEncryption.recovery.hint":
      "A second key that opens the encrypted volume without Olano's key service, for example to read your data from a disk image if this instance or Olano is gone. It is generated on the instance, shown to you exactly once and never stored by Olano: keep it in a password manager or printed in a safe place. Without one, your data can be opened by Olano's key service alone.",
    "diskEncryption.recovery.none": "No recovery key yet",
    "diskEncryption.recovery.pending": "Pending confirmation",
    "diskEncryption.recovery.pendingUntil":
      "A key was shown and has not been confirmed. It expires {when}; until then no other key can be created. If you still have it, confirm it by re-entering it; otherwise cancel it and start over.",
    "diskEncryption.recovery.acknowledged": "Active",
    "diskEncryption.recovery.activeSince": "Active since {when}.",
    "diskEncryption.recovery.create": "Create recovery key",
    "diskEncryption.recovery.replace": "Replace recovery key",
    "diskEncryption.recovery.confirmPending": "Confirm the shown key",
    "diskEncryption.recovery.cancelPending": "Cancel pending key",
    "diskEncryption.recovery.replaceConfirm.title": "Replace the recovery key?",
    "diskEncryption.recovery.replaceConfirm.body":
      "A new key is generated and shown once. Your current key keeps opening the volume until you confirm the new one by re-entering it; from that moment only the new key works, and every place you stored the old one must be updated.",
    "diskEncryption.recovery.cancelConfirm.title": "Cancel the pending recovery key?",
    "diskEncryption.recovery.cancelConfirm.body":
      "The key that was shown is removed from the volume and stops working; it cannot be brought back. Your current recovery key, if you have one, is not affected. You can create a new key afterwards.",
    "diskEncryption.recovery.cancelled": "The pending recovery key was cancelled.",
    "diskEncryption.header": "Header backup",
    "diskEncryption.header.hint":
      "A copy of the volume's encryption header (the part that holds the keyslots, never your data) that Olano keeps off the instance. If the header on the disk is ever damaged, the backup lets your recovery key open the volume again. It is refreshed whenever the keys change; the generation number says which copy is current.",
    "diskEncryption.header.generation": "Generation {n}",
    "diskEncryption.header.uploadedAt": "Backed up {when}.",
    "diskEncryption.header.none": "Not backed up yet",
    "diskEncryption.service": "Encryption service",
    "diskEncryption.service.hint":
      "The sealer on this instance: the program that unlocks the volume at boot, runs the recovery-key ceremony and backs the header up. The dashboard talks to it only from this card, only for a signed-in admin, and never through Olano.",
    "diskEncryption.service.ok": "Sealer {version}",
    "diskEncryption.service.unavailable": "Unavailable",
    "diskEncryption.service.unavailable.hint":
      "The encryption service on this instance is not answering right now. Your data stays encrypted and the instance keeps working; only the recovery-key actions here are paused. Try again in a minute. If this persists after a restart of the instance, contact Olano support.",
    "diskEncryption.loadFailed": "Could not read the encryption status of this instance.",
    "diskEncryption.refresh": "Refresh",
    "diskEncryption.dialog.createTitle": "Create recovery key",
    "diskEncryption.dialog.replaceTitle": "Replace recovery key",
    "diskEncryption.dialog.intro":
      "The instance generates a new recovery key and adds it to the encrypted volume. The key is 32 characters (letters A to Z and digits 2 to 7), shown as eight groups of four. It appears once, on the next screen: copy it into a password manager or write it down before you continue. You then confirm it by typing it back, which proves the copy you kept is right. Olano never sees the key; the console only ever learns that one exists.",
    "diskEncryption.dialog.introReplace":
      "Your current key keeps working until the new one is confirmed. Once you confirm the new key, the old one is removed from the volume and stops working.",
    "diskEncryption.dialog.start": "Generate the key",
    "diskEncryption.dialog.starting": "Generating…",
    "diskEncryption.dialog.showTitle": "Your recovery key",
    "diskEncryption.dialog.showWarning":
      "This is the only time the key is shown. Olano does not store it and cannot show it again. If you lose it later, create a new one from the Disk encryption card; the lost key stops working once the new one is confirmed.",
    "diskEncryption.dialog.copy": "Copy the key",
    "diskEncryption.dialog.stored": "I have stored this key somewhere safe.",
    "diskEncryption.dialog.next": "Next: confirm it",
    "diskEncryption.dialog.confirmTitle": "Confirm the recovery key",
    "diskEncryption.dialog.confirmBody":
      "Type or paste the key you just stored. Dashes, spaces and lower case are fine. The instance checks it against the volume itself; nothing else can confirm a key.",
    "diskEncryption.dialog.confirmLabel": "Recovery key",
    "diskEncryption.dialog.confirm": "Confirm",
    "diskEncryption.dialog.confirming": "Checking…",
    "diskEncryption.dialog.invalid": "A recovery key is 32 characters, letters A to Z and digits 2 to 7.",
    "diskEncryption.dialog.mismatch":
      "That is not the key that was shown. Check the copy you stored and try again. The key expires {when} if it is not confirmed.",
    "diskEncryption.dialog.expiredTitle": "The key expired",
    "diskEncryption.dialog.expiredBody":
      "The key was not confirmed in time and has been removed from the volume; it no longer opens anything. Start over to generate a new one.",
    "diskEncryption.dialog.startOver": "Start over",
    "diskEncryption.dialog.doneTitle": "Recovery key active",
    "diskEncryption.dialog.doneBody":
      "The key you stored now opens the encrypted volume. Keep it safe: it is the only copy.",
    "diskEncryption.dialog.doneReplaced":
      "The previous key has been removed from the volume and no longer works.",
    "diskEncryption.dialog.close": "Close",
    "diskEncryption.dialog.cancel": "Cancel",
    "diskEncryption.dialog.abandonTitle": "Abandon the key?",
    "diskEncryption.dialog.abandonBody":
      "Closing now cancels the key that was just shown: it is removed from the volume and stops working. Your current recovery key, if you have one, is not affected.",
    "diskEncryption.dialog.abandon": "Cancel the key",
    "diskEncryption.dialog.keepGoing": "Keep going",
    "diskEncryption.dialog.busy": "The instance is in the middle of another step. Wait a moment and try again.",
    "diskEncryption.dialog.pendingExists":
      "A key is already waiting for confirmation. Confirm or cancel it from the Disk encryption card first.",
    "diskEncryption.dialog.failed": "The instance refused: {detail}",
    "diskEncryption.dialog.unavailable":
      "The encryption service on this instance is not answering. Try again in a minute.",
    "catalog.common.copyFailed": "Could not copy to the clipboard.",
    "section.externalAccess": "External access",
    "section.externalAccess.hint":
      "Let systems OUTSIDE this deployment send your agents messages — over MCP (Claude, ChatGPT, Cursor, agent frameworks) and A2A (other agents). Both doors are off by default and each agent still has to opt in on its External access tab with a public description.",
    "section.externalAccess.direction":
      "Direction check: this is about others reaching YOUR agents. Servers your agents connect to live on each agent's MCP tab; agents your agents call live under Remote agents. Nothing here changes those.",
    "field.ea.mcp": "MCP endpoint",
    "field.ea.mcp.hint":
      "Serve the deployment's MCP endpoint at <public URL>/api/mcp (Streamable HTTP). Agents flagged “Reachable over MCP” become ask_<agent> tools there. Off (default) answers 404 to everyone, whatever the agents say. Callers always need a bearer token or an approved OAuth sign-in. Applies immediately.",
    "field.ea.a2a": "A2A endpoints",
    "field.ea.a2a.hint":
      "Serve an Agent Card and JSON-RPC endpoint at <public URL>/api/a2a/agents/<agent> for every agent flagged “Reachable over A2A”. Off (default) answers 404. Same credentials as MCP. Applies immediately.",
    "field.ea.requireHttps": "Require HTTPS",
    "field.ea.requireHttps.hint":
      "Refuse to serve any of this when the deployment's public URL is plain http, because tokens travel in the Authorization header. On by default. Loopback and private-network URLs (development) are exempt. Turn off only for a box that is never reachable from the internet.",
    "field.ea.a2aRetention": "A2A task retention (days)",
    "field.ea.a2aRetention.hint":
      "How long the record of each inbound A2A call (its task id, status, the message and the reply) stays retrievable by the caller through the protocol's task lookup, after which it is deleted from the deployment's task store. Only the A2A envelope is affected: the conversation itself stays in the agent's thread and transcript, and the Agent to Agent view keeps its own log. 7 by default; 1–3650.",
    "field.ea.ttl": "Default token lifetime (days)",
    "field.ea.ttl.hint":
      "Lifetime of a bearer token minted without an explicit expiry. 90 by default; 0 means never expires. 0–3650.",
    "field.ea.oauth": "Built-in OAuth sign-in",
    "field.ea.oauth.hint":
      "Olano's own OAuth 2.1 authorization server at <public URL>/api/oauth. It is what lets an OAuth-only client — a Claude.ai custom connector, an A2A client following the card — connect without an external identity provider: the client registers, sends the person to a consent page here, an admin picks which agents it may reach, and it receives short-lived tokens plus a rotating refresh token. Everything it issues appears in the token list below.",
    "field.ea.oauthEnabled": "Authorization server",
    "field.ea.oauthEnabled.hint":
      "On by default (it only answers while an endpoint above is on). Off ⇒ OAuth-only clients cannot connect; pasted tokens still work.",
    "field.ea.oauthDcr": "Dynamic client registration",
    "field.ea.oauthDcr.hint":
      "Accept RFC 7591 registrations so a client can sign up by itself before its first sign-in. On by default; registration grants nothing — the consent page does. Off ⇒ only clients already registered can start a sign-in.",
    "field.ea.oauthAccess": "Access token lifetime (minutes)",
    "field.ea.oauthAccess.hint": "How long an OAuth access token works before the client must refresh. 60 by default; 5–1440.",
    "field.ea.oauthRefresh": "Refresh token lifetime (days)",
    "field.ea.oauthRefresh.hint": "How long a client can keep renewing without a new consent. Each refresh rotates the token. 30 by default; 1–365.",
    "field.ea.jwt": "External identity provider (optional)",
    "field.ea.jwt.hint":
      "Also accept access tokens issued by your own authorization server (Auth0, Keycloak, Okta, Entra…). Tokens are verified against the issuer's published keys; the audience must be this deployment's endpoint and the token's scopes decide the agents (agent:<name> or agents:*). Leave both fields empty to disable.",
    "field.ea.jwtIssuer": "Issuer",
    "field.ea.jwtIssuer.hint": "The exact `iss` value the provider puts in its tokens, e.g. https://your-tenant.auth0.com/.",
    "field.ea.jwtJwks": "JWKS URL",
    "field.ea.jwtJwks.hint": "Where the provider publishes its signing keys, usually <issuer>/.well-known/jwks.json.",
    "field.ea.jwtAudience": "Audience",
    "field.ea.jwtAudience.hint":
      "The `aud` a token must carry, e.g. `https://api.example.com`. Empty means the URL of the endpoint the token is presented to, which is what the standard requires: `<public URL>/api/mcp` on the MCP endpoint, and the agent's own `<public URL>/api/a2a/agents/<agent>` on A2A (`<public URL>/api/a2a/agents` is accepted there too). A value set here replaces both; set it only if your provider cannot issue those audiences.",
    "field.ea.jwt.half": "Set both the issuer and the JWKS URL, or neither.",
    "field.aut.external_access": "Publish itself for outside callers and mint access tokens",
    "field.aut.external_access.hint":
      "Whether an agent may switch its own inbound exposure (MCP / A2A) and send a token-minting link from chat. The tools additionally refuse anyone who is not an administrator, so this only decides whether they exist at all.",
    "section.mobileApp": "Mobile app",
    "section.mobileApp.hint": "What the Olano mobile app shows to everyone who signs in to this deployment, admins included. Phones download this layout when the app opens and every time it comes back to the foreground, so a change reaches them within one open, with no app update and no runtime restart. Hiding something only removes it from the phone: the dashboard and the API behind it keep working, and nothing is deleted.",
    "field.maCortex": "Show the Cortex tab",
    "field.maCortex.hint": "On (default): the app's tab bar includes Cortex, where people see each agent's ideas, projects and Pulse findings and act on them. Off: the tab is removed, and a phone that was on a Cortex screen moves to Chats. Cortex keeps running on the deployment either way; this only decides whether the phone offers it.",
    "field.maInbox": "Show the Inbox tab",
    "field.maInbox.hint": "On (default): the tab bar includes Inbox, with the sections switched on below. Off: the whole Inbox tab and its badge are removed, whatever the three switches below say. The Inbox is also removed when all three sections below are off. Push notifications are separate (they follow each person's notification settings on the phone).",
    "field.maInboxApprovals": "Approvals section",
    "field.maInboxApprovals.hint": "On (default): the Inbox shows actions an agent paused for a person to approve or reject, and Cortex items waiting for approval. Off: the section is hidden and no longer counts toward the Inbox badge. Paused actions still wait, and can still be answered in the dashboard's Approvals view.",
    "field.maInboxAlerts": "Alerts section",
    "field.maInboxAlerts.hint": "On (default): the Inbox shows the deployment's notifications (failed background runs, connections that need reconnecting, and similar), with their fix-it buttons. Off: the section is hidden and unread alerts no longer count toward the Inbox badge. Alerts still appear on the dashboard's bell.",
    "field.maInboxIssues": "Issues section",
    "field.maInboxIssues.hint": "On (default): the Inbox lists the issues assigned to or raised by agents and people, and opens them for comments. Off: the section is hidden. Issues keep working for agents and in the dashboard.",
    "field.maFiles": "Show Files in Settings",
    "field.maFiles.hint": "On (default): the app's Settings tab has a Files row for browsing and downloading each agent's workspace files (read-only for members), and agent screens keep their Files link. Off: those entries are removed from the phone. Files an agent names in a reply still open from the chat.",
    "field.maCredits": "Show Credits and Usage in Settings",
    "field.maCredits.hint": "On (default): the Settings tab has a Credits and Usage row, shown only on deployments that bill in credits, with the balance and recent spend. Off: the row is removed, so customers never see credit balances on the phone. Usage is still metered and visible in the dashboard's Insights.",
    "field.maTranscribeRow": "Show \"Transcribe on this phone\" in Settings",
    "field.maTranscribeRow.hint": "On (default): people can choose, per phone, whether a voice note is turned into words on the phone or on the deployment. Off: the switch is removed and every phone uses the default set below, including phones where someone had already chosen.",
    "field.maTranscribeDefault": "Transcribe voice notes on the phone by default",
    "field.maTranscribeDefault.hint": "Where a voice note becomes words when the person has not chosen (and on every phone while the switch above is hidden). Off (default): the recording is sent to the deployment and transcribed by its speech-to-text service, the same transcript the agent gets on Telegram or WhatsApp. On: phones that can recognize speech (iPhones with permission granted) transcribe it themselves and send only the words, which is faster and keeps the audio on the phone; any other phone still sends the recording.",
    // Agent-to-agent conversations (config.yaml → peer_threads)
    "section.peerThreads": "Agent-to-agent conversations",
    "section.peerThreads.hint":
      "When one agent asks another for something with talk_to_agent, the pair shares one conversation thread, so the second agent remembers what the first already asked. Left unbounded, that thread grows for the life of the deployment and every exchange carries all of it: slower replies, more tokens, and on a busy deployment enough memory to restart the runtime. These bounds retire the shared thread and start a fresh one; the old one stays readable in the Agent to Agent view. Agent-to-agent exchanges are mostly one-shot requests, so the defaults are short. Read at every exchange, with no restart.",
    "field.ptRotate": "Start a fresh thread when a bound is reached",
    "field.ptRotate.hint": "On (the default): a pair's shared thread is retired once it is older than the days below or holds more rows than the limit below, and the next exchange opens a new one. Off: a pair keeps its one thread forever, however long it gets; the two bounds below are ignored.",
    "field.ptDays": "Retire after (days)",
    "field.ptDays.hint": "A pair's thread is retired once its first exchange is this many days old, from 1 to 365. Default 3. Counted from the first message in the thread, not the last, so a pair that talks every day still starts over on schedule.",
    "field.ptTurns": "Retire after (rows)",
    "field.ptTurns.hint": "The same, once the thread's transcript holds this many rows, from 10 to 100000. Default 200. A row is one message, so an exchange (a request and its reply) is about two; 200 rows is roughly 100 exchanges. Whichever of the two bounds is reached first applies.",
    // Checkpoint history (config.yaml → checkpoint_gc)
    "section.checkpointGc": "Conversation history cleanup",
    "section.checkpointGc.hint":
      "Every step an agent takes saves a checkpoint of its working state so a turn can resume after an approval, a restart or a crash, and nothing in the engine ever deletes an old one. A reply only needs the newest few, so this sweep removes, per conversation, every checkpoint older than the last full snapshot of the message list. Conversations keep working exactly as before and the transcript you read is never touched; what goes is history nothing can read back. Runs in the background a few minutes after each boot and then on the interval below, largest conversations first, in a bounded pass. Postgres only; the SQLite backend is left alone. Read before every pass, with no restart.",
    "field.gcEnabled": "Sweep old checkpoints",
    "field.gcEnabled.hint": "On (the default): the sweep runs ten minutes after boot and then every interval below. Off: nothing is removed and history grows without bound; one deployment reached 40 GB of it before this existed. Turning it back on takes effect at the next pass.",
    "field.gcInterval": "Run every (hours)",
    "field.gcInterval.hint": "How often a pass runs after the first one, from 1 to 168 hours. Default 6. A pass that runs out of its time budget stops between conversations and the next pass continues where it left off, so a shorter interval only matters on a deployment with a very large history.",
    "field.gcKeepMin": "Always keep (checkpoints)",
    "field.gcKeepMin.hint": "Checkpoints always kept per conversation whatever the snapshots say, from 1 to 1000. Default 3. The margin keeps the sweep clear of a turn in flight; raising it keeps a little more history around, it never makes a reply faster.",
    "section.mcpHealth": "MCP connection health",
    "section.pages": "Deployed pages",
    "section.pages.hint": "Pages an agent publishes with pages_deploy run commands: a build (for example npm run build for a React or Vite app, served as an immutable release) or a long-running server that viewers reach through the page URL, or both. They run in the agent's own sandbox, like its shell commands, behind the agent's viewer password. Plain pages published with pages_start only serve files and are not affected by anything here. Changes apply within a few seconds, with no restart.",
    "field.pgEnabled": "Run page builds and servers",
    "field.pgEnabled.hint": "On (the default): pages published with pages_deploy are built and their servers kept running, restarted after a crash and probed for health. Off: no build runs, every page server is stopped within a few seconds, those pages answer 503 to viewers, and pages_deploy refuses new deploys. Static releases that were already built keep serving. Plain pages are never affected.",
    "field.pgMaxServices": "Servers per agent",
    "field.pgMaxServices.hint": "How many pages with a running server one agent may have serving at once, from 1 to 50. Default 5. A deploy over the limit is refused with the names of the agent's running servers; stopping one with pages_stop frees a slot. Static builds do not count, because they run nothing after the build.",
    "field.pgHealth": "Health check every (seconds)",
    "field.pgHealth.hint": "How often each running page server's health path is requested, from 5 to 3600 seconds. Default 30. Any answer below 500 counts as healthy; three failures in a row restart the server. The health path is chosen per page when it is deployed ('/' unless the agent set another).",
    "field.pgTimeout": "Request timeout (seconds)",
    "field.pgTimeout.hint": "The longest a viewer's request waits on a page server, from 5 to 3600 seconds. Default 120. It applies to the first byte of the answer and to every gap between chunks of a streamed answer, so a long download or a server-sent events stream keeps working as long as data keeps arriving. Past it the viewer gets 504.",
    "field.pgBody": "Largest upload (MB)",
    "field.pgBody.hint": "The largest request body forwarded to a page server, from 1 to 1024 MB. Default 25. A form post or upload over it is answered 413 before it reaches the server. Raise it for pages that accept file uploads.",
    "field.pgConcurrency": "Requests at once, per page",
    "field.pgConcurrency.hint": "How many viewer requests one page server handles at the same time, from 1 to 1024. Default 64. Past it, viewers get 503 with Retry-After instead of an unbounded queue, which keeps one busy page from holding the runtime's resources. Applies the next time a server starts.",
    "section.mcpHealth.hint":
      "Keeps MCP servers connected without a manual re-authorization: renews OAuth access tokens before they lapse and reconnects servers that dropped out. Changes apply within one check interval — no restart.",
    "mcpHealth.lastCheck": "Last check {minutes} min ago — {connected} of {total} servers connected.",
    "field.mhEnabled": "Automatic upkeep",
    "field.mhEnabled.hint":
      "Master switch for the whole sweep, on by default. On, the runtime renews OAuth tokens and reconnects dropped servers on the interval below. Off, a token is only renewed by the next request that happens to need one — for an idle agent that can be long after the grant became unrecoverable — and a server that failed to connect stays down until someone opens the agent's MCP tab and clicks Reload MCP. Servers already connected and in use are unaffected either way.",
    "field.mhRefreshTokens": "Renew OAuth tokens",
    "field.mhRefreshTokens.hint":
      "On by default. Renews a stored access token once it is inside the renewal window below, by calling the provider's OAuth token endpoint directly — so it succeeds even while the MCP server itself is unreachable. Only servers using OAuth with a stored refresh token are touched; one that needs you to sign in again is left alone and keeps showing Authorize. Off leaves tokens to expire and be renewed lazily on the next tool call.",
    "field.mhAutoReconnect": "Reconnect dropped servers",
    "field.mhAutoReconnect.hint":
      "On by default. Re-connects servers whose last attempt timed out or errored, retrying with a widening gap (5 minutes, doubling, capped at 6 hours) so a permanently dead server can't rebuild agents on a loop. Never retries servers you disabled, ones with no launch command, or ones waiting for authorization — reconnecting cannot grant consent. A manual Reload MCP always retries immediately regardless of this setting.",
    "field.mhInterval": "Check every (min)",
    "field.mhInterval.hint":
      "1–1440 minutes; default 10. How often the sweep runs. Must not exceed the renewal window, or a token could first be noticed after it already expired.",
    "field.mhWindow": "Renew when under (min)",
    "field.mhWindow.hint":
      "1–720 minutes; default 20. How much life an access token must have left before it is renewed. Keep it comfortably above the check interval. Only used when \"Renew OAuth tokens\" is on.",
    "field.mhWindowOrder": "The renewal window must be at or above the check interval.",
    "section.contextWindow": "Context window",
    "section.contextWindow.hint":
      "How each agent's conversation is bounded inside its model's context window. When a thread reaches the trigger, everything older than the keep budget is summarised into one paragraph (the full text is saved under the workspace's .olano/conversation_history/ folder) and only the summary plus the kept tail reaches the model from then on; /compact does the same on demand. Oversized MCP tool results are offloaded to a workspace file instead of filling the window. Applies to an agent at its next start or reload; any field can be overridden per agent in its agent.json5 under context_window.",
    "field.cwKeepFraction": "Keep after compaction (share of window)",
    "field.cwKeepFraction.hint":
      "Share of the model's context window kept verbatim after a compaction, used when the model's window is known (the catalog lists it, or the provider reports it). A decimal between 0.05 and 0.8, e.g. 0.35 on a 272k-token model keeps the last ~95k tokens of conversation. Default 0.35. Must be below the trigger share. Lower values free more space per compaction but make the agent forget earlier work sooner.",
    "field.cwTriggerFraction": "Compact at (share of window)",
    "field.cwTriggerFraction.hint":
      "Share of the window at which automatic compaction fires, used when the window is known. A decimal between 0.3 and 0.98. Default 0.85. The manual compact_conversation tool (the /compact command) becomes available at half of this.",
    "field.cwKeepTokens": "Keep after compaction (tokens, unknown window)",
    "field.cwKeepTokens.hint":
      "The keep budget as an absolute token count, used only when the model's window is unknown to this box (a model newer than the catalog, a self-hosted one). An integer between 2000 and 2000000. Default 40000. Must be below the token trigger.",
    "field.cwTriggerTokens": "Compact at (tokens, unknown window)",
    "field.cwTriggerTokens.hint":
      "Token count at which automatic compaction fires when the window is unknown. An integer between 8000 and 4000000. Default 170000. Set it below the smallest window your unknown models actually have, or the provider rejects the request before compaction can run.",
    "field.cwMcpLimit": "MCP result limit (tokens)",
    "field.cwMcpLimit.hint":
      "An MCP tool result larger than this (estimated at four characters per token) is saved to the workspace's .olano/large_tool_results/ folder and replaced in the conversation by the file path plus a preview of about 1,000 tokens that follows the content: JSON keeps its structure with long lists sampled, while tables and logs keep the header, the first and last lines, every error line and one example of each kind of line, with a count of what was left out. The agent reads the rest with read_file or grep. An integer between 0 and 200000; 0 turns the cap off. Default 5000, roughly one page of search results. Results from Olano's own tools are governed by a separate 20000-token offload instead.",
    "field.cwFractionOrder": "The keep share must be below the compaction trigger share.",
    "field.cwTokenOrder": "The keep token budget must be below the token trigger.",
    "section.sandbox": "Sandbox resources",
    "section.sandbox.hint":
      "How much of this machine one agent's isolated sandbox container may use when it runs shell commands. Left empty, both limits size themselves from the host — a bigger machine gives every agent more room with nothing to configure.",
    "sandbox.summary":
      "Host: {hostCpus} CPUs · {hostMem} RAM — each agent's sandbox is capped at {cpus} CPUs · {memory} ({source}).",
    "sandbox.envOverride":
      "An environment variable (LIONCLAW_SANDBOX_MEMORY / LIONCLAW_SANDBOX_CPUS) is set on this deployment and takes precedence over the matching field below, so a value saved here will not take effect until that variable is removed.",
    "field.sbMemory": "Memory limit",
    "field.sbMemory.hint":
      "Hard RAM ceiling for each agent's sandbox container. A command that exceeds it is stopped by the kernel inside the sandbox — the agent and this dashboard keep running. Accepts a docker size string: a number with an optional b/k/m/g suffix, like 4g or 2048m; minimum 6m. Leave empty for the automatic limit: 25% of detected host RAM, never below 512m. An individual agent can override this in its own configuration, and the LIONCLAW_SANDBOX_MEMORY environment variable overrides both. Saving a change recreates each agent's sandbox container the next time that agent starts or is reloaded — its workspace and shared installed tools live on the host and survive, but anything installed ad-hoc inside the container is lost.",
    "field.sbMemory.placeholder": "auto (25% of host RAM)",
    "field.sbCpus": "CPU limit",
    "field.sbCpus.hint":
      "CPU ceiling for each agent's sandbox container, in cores: 0.5 is half a core, 2 is two cores. Accepts 0.1 to 64. This is a ceiling, not a reservation — an idle sandbox uses nothing, and several busy agents can together exceed any one agent's share, which is why the automatic value is a generous fraction rather than the machine divided by your agent count. Leave empty for the automatic limit: 50% of detected host cores, never below 0.5. An individual agent can override this in its own configuration, and the LIONCLAW_SANDBOX_CPUS environment variable overrides both. Saving a change recreates each agent's sandbox container on its next start, exactly as the memory limit does.",
    "field.sbCpus.placeholder": "auto (50% of host cores)",
    "section.egress": "Outbound access",
    "section.egress.hint":
      "Which internet destinations your agents' tools may reach. Both lists empty — the default — means every public destination is allowed, exactly as before these lists existed. A single agent can narrow this further in its own configuration (Agent → Advanced → egress_allow / egress_deny). Applies to the next tool call after saving; no restart. These lists govern everything an agent can reach the internet with: its web and HTTP tools, the headless browser, every integration toolkit, and its sandbox shell (a curl, pip install or git clone inside a shell command). A blocked destination comes back as a readable error rather than a hang. Internal and private-network addresses are always blocked independently of these lists, and your deployment stays able to reach its own control plane whatever you put here.",
    "field.egAllow": "Allowed domains",
    "field.egAllow.hint":
      "Domains every agent's tools may reach — one per line, as a bare domain like api.github.com or example.com. A domain covers its subdomains; a leading *., an https:// prefix, a port or a path are stripped on save, and anything that is not a domain name or IPv4 address is rejected. Leave empty to allow every public destination. When a list is set, any destination not on it is refused with an error the agent can read. Remember that an agent needs every service it actually uses on this list — a support agent reading email and writing to your CRM needs both. An agent whose own egress_allow is non-empty uses its list INSTEAD of this one; an agent with an empty list inherits this one. The deny list is checked first and always wins.",
    "field.egDeny": "Blocked domains",
    "field.egDeny.hint":
      "Domains no agent's tools may reach — same format as the allowed list, one per line. Checked before the allowed list, so a domain here stays blocked even when it is also allowed. Every agent's own egress_deny is ADDED to this list: an agent can block more, never less. Leave empty to block nothing beyond the always-on private-network protection.",
    "placeholder.egDomains": "one domain per line, e.g. api.github.com",

    "section.paths": "Paths",
    "section.paths.hint": "One path per line.",
    "field.extraBindRoots": "Extra bind roots",
    "field.extraBindRoots.hint": "Additional directories agents may read/write outside the workspace.",
    "field.archetypeDirs": "Archetype directories",
    "field.archetypeDirs.hint": "Extra directories scanned for agent archetypes.",
    "placeholder.onePerLine": "One path per line",
  },
  zh: {
    "page.title": "配置",
    "page.subtitle": "Olano 全局设置 —— config.yaml 的内容。",
    "badge.restart": "需要重启",
    "badge.restart.title": "立即保存，但需重启运行时后才生效。",
    "configPath": "正在编辑 {path}",
    "btn.save": "保存更改",
    "btn.saving": "保存中…",
    "btn.reset": "放弃",
    "unsaved.pill": "有未保存的更改",
    "unsaved.title": "要保存更改吗？",
    "unsaved.body": "你修改了本页的设置但尚未保存。现在离开会丢弃这些更改。",
    "unsaved.bodyInvalid":
      "本页有些字段无效（已标红），当前无法按原样保存。请修正后保存，或离开并丢弃更改。",
    "unsaved.saveLeave": "保存并离开",
    "unsaved.leave": "不保存直接离开",
    "unsaved.stay": "留在本页",
    "unsaved.nudge": "配置页有未保存的更改——记得保存。",
    "toast.loadFailed": "无法加载配置。",
    "toast.saved": "配置已保存。",
    "toast.savedRestart": "配置已保存。重启运行时后端口 / 团队账号更改才会生效。",
    "toast.saveFailed": "无法保存配置。",
    "toast.notAdmin": "只有管理员可以更改全局设置。",
    "section.general": "通用",
    "section.general.hint": "未指定智能体时 CLI 与仪表盘使用的默认值。",
    "field.defaultAgent": "默认智能体",
    "field.defaultAgent.hint": "命令未指定智能体时使用。",
    "field.preferredModel": "首选模型",
    "field.preferredModel.hint": "未设置模型的智能体的回退模型。留空 ⇒ 内置默认。",
    "field.theme": "主题",
    "field.theme.hint": "此浏览器的外观。仅本地保存 — 不属于 config.yaml。",
    "model.builtinDefault": "内置默认",
    "model.inheritAgent": "继承智能体自身模型",

    "section.cognition": "认知模型",
    "section.cognition.hint": "常驻 Cortex 引擎各阶段使用的模型。",
    "field.fastModel": "快速模型",
    "field.fastModel.hint":
      "\u9a71\u52a8\u5927\u591a\u6570\u8ba4\u77e5\u9636\u6bb5\u7684\u5ec9\u4ef7\u5feb\u901f\u6a21\u578b\u3002\u683c\u5f0f\u4e3a provider:model\uff0c\u5982\u201colano:turbo\u201d\u3001\u201copenai:gpt-5.4-mini\u201d\u3002\u5df2\u8fde\u63a5 cloud.olano.ai \u7684\u90e8\u7f72\u9ed8\u8ba4\u4e3a\u201colano:turbo\u201d\uff08Olano \u5feb\u901f\u5c42\u7ea7\uff0c\u6309\u79ef\u5206\u8ba1\u8d39\uff0c\u65e0\u9700 API \u5bc6\u94a5\uff09\uff1b\u5426\u5219\u9ed8\u8ba4\u4f7f\u7528\u81ea\u5907\u5bc6\u94a5\u7684\u6a21\u578b\uff08\u9700\u5728 Secrets \u4e2d\u8bbe\u7f6e\uff09\u3002\u6e05\u7a7a\u5219\u6062\u590d\u4e3a\u672c\u673a\u5668\u5bf9\u5e94\u7684\u9ed8\u8ba4\u503c\u3002\u8ba4\u77e5\u6301\u7eed\u8fd0\u884c\uff0c\u6b64\u6a21\u578b\u5360\u4e3b\u8981\u6210\u672c\u2014\u2014\u8bf7\u4fdd\u6301\u5ec9\u4ef7\u3002",
    "field.deepModel": "深度模型",
    "field.deepModel.hint":
      "\u7528\u4e8e\u6df1\u5ea6\u8fd0\u884c\u7684\u66f4\u5f3a\u6a21\u578b\uff0c\u683c\u5f0f\u540c\u4e0a\u3002\u5df2\u8fde\u63a5 cloud.olano.ai \u7684\u90e8\u7f72\u9ed8\u8ba4\u4e3a\u201colano:pro\u201d\uff08\u6309\u79ef\u5206\u8ba1\u8d39\uff0c\u65e0\u9700 API \u5bc6\u94a5\uff09\uff1b\u5426\u5219\u9ed8\u8ba4\u4f7f\u7528\u81ea\u5907\u5bc6\u94a5\u7684\u6a21\u578b\u3002\u7559\u7a7a \u21d2 \u7ee7\u627f\u667a\u80fd\u4f53\u81ea\u8eab\u7684\u6a21\u578b\u3002",
    "section.defaultModels": "默认模型",
    "section.defaultModels.hint":
      "部署级模型别名的指向。任何设置为 \"Default · Turbo/Pro/Max\" 的智能体、子智能体或选择器都跟随此处的目标 —— 改一次，所有使用该别名的智能体在下次重载时切换，无需逐个编辑。目录智能体安装时使用 Default · Turbo（专家）和 Default · Pro（团队）。",
    "model.builtinTarget": "本机默认 — {model}",
    "field.defaultTurbo": "Turbo 目标",
    "field.defaultTurbo.hint":
      "\"Default · Turbo\" 背后的具体模型 —— 大多数智能体与目录专家日常运行的层级。可选任意 provider:model 格式的模型；不允许再填 default:* 别名（别名不能链式指向）。保持本机默认时：已连接 cloud.olano.ai 的部署解析为 Olano 快速层级（按积分计费，无需 API 密钥），其他安装解析为自备密钥模型（其服务商需已连接或在 Secrets 中配好密钥，否则使用该别名的智能体下一轮就会失败）。在智能体下次启动或重载时生效。",
    "field.defaultPro": "Pro 目标",
    "field.defaultPro.hint":
      "\"Default · Pro\" 背后的具体模型 —— 团队主管安装时使用的更强层级。规则与 Turbo 目标相同：provider:model 格式、不允许 default:* 值；本机默认在托管部署为 Olano Pro 层级，其他安装为自备密钥模型；在智能体下次启动或重载时生效。",
    "field.defaultMax": "Max 目标",
    "field.defaultMax.hint":
      "\"Default · Max\" 背后的具体模型 —— 能力最强（也最贵）的层级，供你显式指定的智能体使用。规则与其他目标相同：provider:model 格式、不允许 default:* 值；本机默认在托管部署为 Olano Max 层级，其他安装为自备密钥模型；在智能体下次启动或重载时生效。",
    "section.speech": "语音",
    "section.speech.hint":
      "聊天平台上的语音转写与语音回复。语音转写（STT）在托管部署上默认使用 Olano 服务，其他安装默认关闭；语音回复（TTS）在所有安装上默认关闭，需在此处选择服务商后才启用。",
    "field.speechStt": "语音转文字（语音消息）",
    "field.speechStt.hint": "为智能体转写入站语音消息。选项：关闭 = 收到语音时请对方改发文字（自托管安装的默认值）；Olano = 托管转写，以 olano 积分计费（托管部署的默认值，无需 API 密钥）；OpenAI/Groq = 使用你自己的 API 密钥；Qwen 仅支持语音合成，不能转写；自定义 = 任意自托管的 OpenAI 兼容服务。",
    "field.speechTts": "文字转语音（语音回复）",
    "field.speechTts.hint": "对语音消息的语音回复，以及 text_to_speech 工具。选项：关闭 = 仅文字回复（所有安装的默认值 — 语音回复始终需要显式开启）；Olano = 托管合成，以 olano 积分计费（无需 API 密钥，仅限托管部署）；OpenAI/Groq/Qwen = 使用你自己的 API 密钥；自定义 = 任意自托管的 OpenAI 兼容服务。",
    "field.speechModel": "模型",
    "field.speechModel.hint": "留空 ⇒ 服务商默认。",
    "field.speechVoice": "音色",
    "field.speechVoice.hint": "留空 ⇒ 服务商默认音色。播放按钮会用此处选择的服务商、模型和音色（包括尚未保存的更改）朗读两句简短的示例，方便你在保存前试听；再按一次即可停止。这是一次真实的语音调用，计费方式与语音回复相同（Olano 以 olano 积分计费，其他服务商使用你自己的密钥）。",
    "speech.preview.play": "试听此音色",
    "speech.preview.loading": "正在生成示例（点击取消）",
    "speech.preview.stop": "停止试听",
    "speech.preview.failed": "无法播放试听音频。",
    "speech.preview.sample": "你好！这就是你的智能体用语音消息回复时的声音。",
    "field.speechFormat": "音频格式",
    "field.speechFormat.hint":
      "向服务商请求的音频封装格式 —— 它决定语音回复是以原生语音气泡送达，还是以音频文件附件送达。只有 Ogg/Opus 能渲染成语音气泡（Telegram 的圆形播放器、WhatsApp 的按住说话）；其他格式一律作为可播放的音频文件发送。选项：自动（默认，建议保持）在 OpenAI 与 Olano 上请求 Opus，在 Groq PlayAI 上请求 WAV（它唯一支持的格式），在 Qwen（DashScope 没有该参数）与自定义服务（可能不支持而直接报错）上则不发送任何格式；Opus 可获得语音气泡 —— 自托管服务若支持请显式选它；MP3、WAV、FLAC、AAC、M4A 均以音频文件送达，不会是气泡（WAV 与 FLAC 未压缩，每条回复可能有数 MB）；PCM 是供后处理流水线使用的原始采样，聊天软件无法播放。服务商实际返回的格式会从音频数据本身识别出来，因此即使服务商忽略此设置，也只会退化为标注正确的音频文件，而不会产生无法播放的气泡。",
    "speech.format.auto": "自动（按服务商 —— 推荐）",
    "speech.format.opus": "Opus / Ogg（语音气泡）",
    "speech.format.mp3": "MP3（音频文件）",
    "speech.format.wav": "WAV（音频文件，未压缩）",
    "speech.format.flac": "FLAC（音频文件，无损）",
    "speech.format.aac": "AAC（音频文件）",
    "speech.format.m4a": "M4A（音频文件）",
    "speech.format.pcm": "PCM（原始采样 —— 聊天中无法播放）",
    "field.speechUrl": "端点 URL",
    "field.speechUrl.hint": "任何 OpenAI 兼容服务，如自托管 Speaches / faster-whisper。",
    "field.speechKeyName": "API 密钥凭据",
    "field.speechKeyName.hint": "存放密钥的保管库/环境变量凭据名。服务无需密钥则留空。",
    "speech.provider.disabled": "关闭（默认）",
    "speech.provider.olano": "Olano（托管 — 以 olano 积分计费）",
    "speech.provider.openai": "OpenAI",
    "speech.provider.groq": "Groq",
    "speech.provider.qwen": "Qwen TTS（阿里云）",
    "speech.provider.custom": "自定义（自托管）",
    "placeholder.providerDefault": "服务商默认",

    // 多媒体与搜索
    "section.media": "多媒体与搜索",
    "section.media.hint":
      "为图像理解、图像生成和网页搜索选择服务来源。每个列表都会显示 Olano 托管服务（以 olano 积分计费，需你的套餐包含该功能），以及本机已配置 API 密钥的任意服务商。",
    "field.mediaVision": "图像理解（视觉）",
    "field.mediaVision.hint":
      "为智能体读取图像的模型（image_describe 工具）。自动（默认）在托管套餐上使用 Olano Vision，否则使用本机拥有密钥的第一个具备视觉能力的服务商。选择“Olano Vision”可无需密钥、以 olano 积分计费；也可选择你自己服务商的视觉模型（仅列出已配置密钥的服务商）。设置后会覆盖旧的 LIONCLAW_VISION_MODEL 环境变量；保持“自动”则继续沿用该变量。",
    "field.mediaImage": "图像生成与编辑",
    "field.mediaImage.hint":
      "image_generate / image_edit 工具的后端。自动（默认）在托管套餐上使用 Olano Image，否则使用你的 OpenAI 或 Gemini 图像密钥。选择“Olano Image”可无需密钥、以 olano 积分计费；也可选择你自己服务商的图像模型（OpenAI gpt-image / DALL·E、Google Imagen / Gemini image）。编辑需要支持图像编辑的后端——Gemini 图像模型只能生成。设置后会覆盖旧的 LIONCLAW_IMAGE_MODEL 环境变量；保持“自动”则继续沿用该变量。",
    "field.webSearch": "网页搜索",
    "field.webSearch.hint":
      "web_search / page_extract 工具的来源。自动（默认）在设置了 TAVILY_API_KEY 时优先使用你自己的 Tavily 密钥，否则使用 Olano 托管网页搜索（以 olano 积分计费）。“Olano 网页搜索”即使存在 Tavily 密钥也强制使用托管服务；“Tavily”强制使用你自己的密钥（仅在设置了 TAVILY_API_KEY 时显示）；“关闭”完全禁用网页搜索，工具会返回错误告知智能体该功能已关闭。",
    "media.auto": "自动（推荐）",
    "media.group.olano": "Olano 托管（olano 积分）",
    "media.group.byok": "你的服务商",

    "section.network": "网络与访问",
    "section.network.hint": "端口与团队账号登录开关。更改将在重启后生效。",
    "field.runtimeApiPort": "运行时 API 端口",
    "field.dashboardPort": "仪表盘端口",
    "field.multiUser": "团队账号",
    "field.multiUser.hint":
      "要求每个人登录本仪表盘：管理员邀请的人类账号（所有者/管理员 + 成员），并强制按智能体的访问控制。关闭时：任何能访问仪表盘的人都以所有者/管理员身份操作（经典单用户安装）。该键从未设置过时，默认值取决于安装方式：云托管部署默认开启；手动安装默认关闭。保存本表单会存储你的明确选择，且始终优先。若在尚无账号时开启，下次加载会显示创建管理员界面。在 config.yaml 中以 multi_user_enabled 存储。与智能体页的「多用户模式」标签无关——那是让共享智能体的聊天终端用户彼此隔离。",
    "field.highlightsAll": "全队共享要点",
    "field.highlightsAll.hint": "向所有用户展示每个智能体的每日/每周/每月工作要点。关闭（默认）：成员仅可见自己智能体的要点。",
    "field.playground": "演练场",
    "field.playground.hint": "启用演练场标签页：一个 2D 动画世界，本部署的智能体在其中走动；开启后，角色生成还会额外产出动画精灵图（较慢的多步图像任务）。开启或关闭（默认关闭）——关闭时标签页隐藏、视图从不加载、后台无任何渲染，角色生成只产出快速的头像。该开关对整个部署生效（本机所有用户看到的一致），立即生效无需重启；在关闭期间生成过角色的智能体，需重新生成才会得到精灵图。托管部署还需套餐允许演练场标签页。",

    // 成员自助创建机器人（config.yaml self_serve_agents:）
    "section.selfServe": "成员创建机器人",
    "section.selfServe.hint":
      "决定非管理员成员能否创建自己的机器人——即简易视图中的“新建机器人”按钮，以及其他各处的同一创建接口。管理员始终可以创建机器人，本区块的任何设置都不影响他们。保存后立即生效，无需重启。以 self_serve_agents 存储在 config.yaml 中。",
    "field.ssaEnabled": "允许成员创建机器人",
    "field.ssaEnabled.hint":
      "开启（默认）：每位已登录成员都会看到“新建机器人”按钮，可在下方的数量上限和模板列表范围内创建自己的机器人；创建者自动成为该机器人的所有者。关闭：成员只能与管理员授权的机器人聊天，按钮从其界面消失（管理员仍保留）。此开关只影响创建——无论开关状态如何，成员都不能编辑、重新配置或删除不属于自己的机器人。",
    "field.ssaMax": "每位成员的机器人上限",
    "field.ssaMax.hint":
      "一位成员同时可拥有的机器人数量——包括自己创建的和被设为所有者的。1 到 100 的整数；默认 3。按所有权计数，被邀请使用他人的机器人不占用此额度。管理员不受限制。达到上限的成员再创建时会收到明确的“已达上限”提示，而不是新机器人。",
    "field.ssaArchetypes": "允许的模板",
    "field.ssaArchetypes.placeholder": "任何模板（留空）",
    "field.ssaArchetypes.hint":
      "限制成员可以从哪些现成模板创建机器人。每行一个模板名，须与目录中的模板名完全一致（例如 sales-helper）。留空（默认）表示允许目录中的所有模板，也允许用自然语言描述从零创建。列表非空时为严格限制：成员只能从列表中选择，从零创建也会被拒绝。对管理员从不生效。",

    // 多机器人群聊（config.yaml groups:）
    "section.groups": "群聊",
    "section.groups.hint":
      "让你的多个机器人共享同一个对话，与常规的一对一聊天并存。你发一条消息，由被指名的成员回复；对话中每条消息都会标明作者。在简洁视图中，机器人列表会出现“群聊”分区和“新建群聊”按钮，每个群聊都有一个用于添加和移除成员的信息面板。保存后立即生效，无需重启。在 config.yaml 中存储为 groups。",
    "field.grpEnabled": "启用群聊",
    "field.grpEnabled.hint":
      "开启（默认）：群聊功能可用——用户可以创建群聊、打开群聊，并在群聊信息面板中添加或移除机器人；获得 “groups” 工具的机器人可以读取并在其所属群聊中发言。关闭：任何位置都不显示群聊分区，已有群聊无法访问，所有群聊网址均返回“未找到”。关闭不会删除任何群聊；重新开启后，原有群聊及其记录会原样恢复。机器人之间是否相互回复由下方单独的“允许机器人相互回复”开关决定，该开关同样默认开启。",
    "field.grpMin": "群聊最少机器人数",
    "field.grpMax": "群聊最多机器人数",
    "field.grpMin.hint":
      "创建群聊时允许的最小成员数。2 到 6 之间的整数，默认为 2。少于两名成员的“群聊”其实就是一对一聊天，因此下限为 2。创建群聊和移除成员时都会检查此项——会使群聊低于该人数的移除操作将被拒绝，并说明原因。",
    "field.grpMax.hint":
      "群聊可达到的最大成员数，在创建群聊和每次“添加机器人”时检查。2 到 6 之间的整数，且不得小于上面的“最少机器人数”，默认为 6。每个成员都是一个运行中的机器人，各自持有一份对话副本，因此这是实实在在的成本和内存上限，而非风格选择。可以放心调低——已超过新上限的群聊仍可正常使用，只是无法再添加成员。",
    "field.grpAutonomous": "允许机器人相互回复",
    "field.grpAutonomous.hint":
      "开启（默认）：当某个机器人的回复用 @ 提及另一名成员时，该成员会自动获得下一轮发言权——因此你的一条消息可能引发一连串机器人之间的往返对话，中间没有人参与，而每条机器人之间的回复都会标注它在回复谁。这正是群聊的意义所在，但也正是会产生你未逐条确认的模型调用的设置，所以它与上面的开关分开，也正因如此才有下面两个限制。每一次终止——轮次用尽、超时、或两个机器人来回拉扯——都会写入对话中，因此对话链绝不会无声中断。关闭：你发消息，被点名的成员回复，然后等待你的下一条消息；此时回复中的 @ 提及只是标记应由谁接手。如果你想先观察几轮对话，可以在这里关闭，之后再开启——无论开关状态如何，已有群聊都会保留。",
    "field.grpHops": "每条消息的机器人往返轮次",
    "field.grpHops.hint":
      "你发出一条消息后，机器人之间最多可以相互传递多少轮发言权。1 到 10 之间的整数，默认为 3。按你发送的每条消息分别计算，因此在默认值下，一段五条消息的对话每次最多允许三轮机器人往返，而不是总共三轮。轮次用尽时，对话会停止并在其中说明。仅在“允许机器人相互回复”开启时生效——关闭时任何消息都不会有第二轮回复。",
    "field.grpTimeout": "对话链时间上限（秒）",
    "field.grpTimeout.hint":
      "一条机器人往返对话链最长可运行多久后自行停止，单位为秒。10 到 1800（30 分钟）之间的整数，默认为 180（3 分钟）。这是整条对话链的实际耗时，因此如果某个较慢的模型在回复中途达到上限，对话链会就此结束并在对话中留下说明，而不会一直悬着。请与上面的轮次上限一同调整——在相同时间上限下提高轮次，只会把“轮次用尽”变成“时间用尽”。仅在“允许机器人相互回复”开启时生效。",

    // 智能体自我配置（config.yaml agent_autonomy:）
    "section.autonomy": "智能体自我配置",
    "section.autonomy.hint":
      "部署级默认值：当有人在对话中提出时，智能体可以对“自己”做哪些更改——每个开关为所有智能体授予一组 self_* 聊天工具。默认全部开启。单个智能体可在其页面（智能体 → 该智能体 → 对话中自我配置，三态含“继承”）双向覆盖任意开关。更改对每个智能体的下一次工具调用生效，无需重启。例外：客服模式（按客户隔离）的智能体无论这些开关如何都默认继承为“关”——陌生人不能重新配置客服机器人——只有该智能体自己的显式“开”才会启用。这些只约束智能体对自身的操作——人的权限与 AgentFather 管理工具不受影响。以 agent_autonomy 存储在 config.yaml 中。",
    "field.aut.explore_archetypes": "浏览模板库",
    "field.aut.explore_archetypes.hint":
      "只读：智能体可以搜索并阅读现成的专家/团队模板，例如建议值得添加的专家。仅此项不授予任何安装能力。",
    "field.aut.change_model": "更换自己的模型",
    "field.aut.change_model.hint":
      "智能体可以列出本部署可用的模型并把自己切换过去（该智能体重启后生效）。供应商未接入的模型会被拒绝。在托管部署上，更大的模型消耗积分更快。",
    "field.aut.modify_prompt": "编辑自己的指令",
    "field.aut.modify_prompt.hint":
      "应要求，智能体可以重写自己的系统提示词（整体替换，重启后生效）。每次更改都会以该智能体的名义记录在「配置 → 修订历史」中，可审查、可回滚。",
    "field.aut.restart_self": "重启自己",
    "field.aut.restart_self.hint":
      "智能体可以重启自己，让已保存的更改生效——几秒钟的停机，安排在宣布重启的回复送达之后。",
    "field.aut.add_subagents": "添加子智能体",
    "field.aut.add_subagents.hint":
      "智能体可以为自己添加专家子智能体——从零编写或以模板起草——并向其委派工作。重启后生效。",
    "field.aut.connect_mcps": "连接服务（MCP 服务器和内置工具包）",
    "field.aut.connect_mcps.hint":
      "智能体可以搜索 MCP 目录，并在自己身上添加、移除或进行 OAuth 授权。登录在用户自己的浏览器中完成；API 密钥经由保险库，绝不写入配置文件。",
    "field.aut.connect_platforms": "连接消息平台",
    "field.aut.connect_platforms.hint":
      "智能体可以为自己接入消息频道：令牌类平台（Telegram、Discord、Slack——机器人令牌存入保险库），以及在聊天中直接显示二维码配对的 WhatsApp。新频道即新的入站入口；智能体会被要求坚持设置允许用户名单。",
    "field.aut.manage_skills": "管理自己的技能",
    "field.aut.manage_skills.hint":
      "智能体可以列出自己拥有的技能并逐个开关——与每个智能体“技能”标签页上的开关相同。绝不会安装、编辑或删除技能；关闭共享技能只影响执行操作的那个智能体。在该智能体重启后生效。",
    "field.aut.magic_links": "发送魔法链接",
    "field.aut.magic_links.hint":
      "智能体可以发送魔法链接：有效期短、受 PIN 保护的页面，由用户在自己的浏览器中打开，完成聊天里做不到的事——登录某个服务或 MCP 服务器（OAuth/设备码流程）、把 API 密钥直接填入保险库（不经过对话）、通过二维码连接 WhatsApp/微信或通过机器人令牌连接 Telegram/Discord/Slack、编辑智能体指令或某个文件、在手机上打开仪表板。这是智能体连接任何东西的默认方式，因此默认开启。关闭后，所有继承此开关的智能体都会失去全部 magic_link_* 工具；单个智能体可在自己的页面重新开启，也可以在其“工具”标签页单独关闭某一个工具。仅限所有者（在消息平台上以频道的允许用户列表为准），群聊会被拒绝，在更严格的安全配置下会弹出审批卡片。配置 → 魔法链接（enabled）仍是整个部署的硬开关，并单独控制仪表板登录链接。",
    // 消息速率限制（全局默认）
    "section.rateLimits": "消息速率限制",
    "section.rateLimits.hint":
      "每个智能体入站消息的默认上限。智能体可单独收紧。留空 = 不限制。计划任务与自主运行不受限制。",
    "field.rlPerMinute": "消息 / 分钟",
    "field.rlPerHour": "消息 / 小时",
    "field.rlPerDay": "消息 / 天",
    "field.rlUnlimited": "不限制",
    "field.rlPerPlatform": "按平台覆盖（高级）",
    "field.rlPerPlatform.hint": "为特定渠道设置默认上限（分 / 小时 / 天），如 telegram 或 dashboard。",
    "field.rlAddPlatform": "+ 添加平台…",
    "field.rlRemove": "移除",
    "field.rlPh.per_minute": "/分",
    "field.rlPh.per_hour": "/时",
    "field.rlPh.per_day": "/天",
    // 魔法链接
    "section.magicLinks": "魔法链接",
    "section.magicLinks.hint":
      "本实例签发的短时效页面，让只有聊天、没有控制台的人在自己的手机上完成登录、编辑文件或提交 API 密钥。",
    "field.mlEnabled": "启用魔法链接",
    "field.mlEnabled.hint": "关闭后，任何入口（智能体、聊天命令、控制台、CLI）都无法签发魔法链接，已有链接也将无法打开。",
    "field.mlTtl": "链接有效期（分钟）",
    "field.mlTtl.hint": "2–1440 分钟。越短越安全。",
    "field.mlPin": "PIN 码",
    "field.mlPin.hint": "链接被转发或泄露时，没有 PIN 码也打不开。",
    "mlPin.required": "必需（推荐）",
    "mlPin.off": "关闭——仅凭链接即可打开页面",
    "field.mlAttempts": "允许输错次数",
    "field.mlAttempts.hint": "1–10，超过后链接自动作废。",
    "field.mlGroups": "允许在群聊中发送链接",
    "field.mlGroups.hint": "关闭后，智能体会拒绝在群聊中发送链接——群里每个人都会看到链接和 PIN 码。",
    // 仪表盘访问 —— 让所有者登录仪表盘。
    "section.ownerLogin": "仪表盘访问",
    "section.ownerLogin.hint": "让智能体给你发送一次性魔法链接，在手机上直接登录本仪表盘 —— 无需输入密码。",
    "field.olEnabled": "仪表盘魔法链接",
    "field.olEnabled.hint": "关闭后，智能体和聊天命令都无法签发登录仪表盘的魔法链接。",
    "field.olAutoBind": "单管理员部署时跳过密码",
    "field.olAutoBind.hint": "信任白名单中的聊天用户即为管理员，并在无需密码的情况下将其登录。仅在单管理员实例上安全。",
    "field.olSessionHours": "仪表盘会话时长（小时）",
    "field.olSessionHours.hint": "1–720。通过魔法链接打开的会话在需要重新认证前的有效时长。",
    // 用量预算
    "section.budgets": "用量预算",
    "section.budgets.hint":
      "本部署的智能体可消耗多少 token、花费多少成本的上限。预算耗尽后对话会被拒绝，拒绝信息会指明是哪个预算。更改在下一次对话即生效 — 无需重启。",
    "budgets.col.budget": "预算",
    "budgets.col.used": "已用",
    "budgets.col.limit": "上限",
    "budgets.window.daily": "每日重置（UTC）",
    "budgets.window.weekly": "每周重置（周一，UTC）",
    "budgets.window.monthly": "每月重置（1 日，UTC）",
    "budgets.window.lifetime": "永不重置",
    "budgets.overridden": "在此设置，覆盖套餐的 {plan}",
    "budgets.signed": "由套餐设定",
    "budgets.placeholder": "套餐默认值：{plan}",
    "budgets.unmanaged":
      "本实例未纳管，因此不会统计也不会强制执行 —— 在部署获得授权之前，下列上限不起作用。",
    "budgets.help":
      "Token 预算是防止失控循环的护栏，由你掌控：填入 token 数量（例如 50000000000）、填 unlimited 完全取消上限，或留空以使用套餐的默认值。它统计智能体消耗的每一个 token，包括使用你自己的服务商密钥发起的调用 —— 因此即使没有向 Olano 计费，预算也可能拦下 BYOK 智能体。两项成本预算由套餐设定，无法在此修改；它们限制的是 Olano 自身向上游支付的费用，而托管调用的花费另有你的额度余额加以限制。",
    "field.budgetAlerts": "预算通知",
    "field.budgetAlerts.hint":
      "预算逐渐用满时在仪表盘发出通知，避免任何人第一次察觉上限是因为对话被拒。默认开启。关闭后将不再有任何预算通知，包括「预算已耗尽、对话正在被拒绝」这一条。",
    "field.budgetPercents": "告警阈值",
    "field.budgetPercents.hint":
      "触发通知的预算百分比，用逗号分隔 —— 例如 50, 60, 70, 80, 90, 95, 99（默认值）。每个阈值在每个预算的每个周期内只触发一次；若一次对话跨过多个阈值，只会提示最高的那个。取值须为 1–99；100% 始终会通知，无需填写。对上方所有预算生效，token 与成本预算皆然。",

    // 磁盘监控与清理
    "section.disk": "磁盘监控与清理",
    "section.disk.hint":
      "监控磁盘占用，并在服务器空间耗尽前自动释放空间。更改会在一个检查周期内生效 — 无需重启。",
    "field.dmEnabled": "监控与告警",
    "field.dmEnabled.hint": "按计划测量磁盘占用，占用过高时发出告警。",
    "field.dmPath": "监控路径",
    "field.dmPath.hint": "要测量的文件系统。/ 覆盖整个系统盘。",
    "field.dmInterval": "检查间隔（分钟）",
    "field.dmInterval.hint": "5–1440 分钟。",
    "field.dmWarn": "告警阈值（%）",
    "field.dmWarn.hint": "记录警告并在仪表盘显示提醒。",
    "field.dmCleanupPct": "自动清理阈值（%）",
    "field.dmCleanupPct.hint": "触发下方的清理动作。",
    "field.dmThresholdOrder": "自动清理阈值必须不低于告警阈值。",
    "field.dmCleanupEnabled": "自动清理",
    "field.dmCleanupEnabled.hint":
      "当占用越过自动清理阈值时运行所选动作。安全动作只删除缓存和旧日志；激进动作还会移除其他软件需重新下载的数据。",
    "disk.actions.hint":
      "选择要清理的内容。所有动作默认开启 — 包括激进的 docker_prune，它会移除其他软件需要重新下载的内容（如所有未使用的 Docker 镜像）。取消勾选即可退出某项，例如此服务器上有其他软件依赖 Docker 缓存数据时取消 docker_prune。",
    "disk.badge.aggressive": "激进",
    "disk.badge.aggressive.title": "释放空间最多，但会移除其他程序可能还需要的内容（下次使用时重新下载）。",
    "disk.badge.unavailable": "此主机不可用",
    "field.dmCooldown": "冷却时间（分钟）",
    "field.dmCooldown.hint": "两次自动清理之间的最小间隔。",
    "field.dmForceDays": "定期清理（天）",
    "field.dmForceDays.hint": "0 = 仅在越过阈值时清理。",
    "field.dmVacuum": "日志大小上限",
    "field.dmVacuum.hint": "旧系统日志将裁剪到该大小，如 200M 或 1G。",
    "section.gateway": "Olano 模型网关",
    "section.gateway.hint": "此部署是否持有 Olano 托管模型（olano:turbo、olano:pro，以及内置的图像、视觉和语音工具）用于认证的密钥。密钥会自动获取——在开通时，或通过设置轮询下发——因此此处无需配置。此卡片的作用是让故障无需登录服务器即可看见。",
    "field.gwState": "状态",
    "field.gwState.hint": "“已连接”表示此实例持有可用的网关密钥，Olano 托管模型按额度计费。“无法使用”表示实例已连接到 cloud.olano.ai 但尚未获得密钥，在密钥到达前所有 olano: 模型调用都会失败。",
    "gw.ok": "已连接",
    "gw.degraded": "无法使用",
    "gw.explain": "在密钥到达前，Olano 托管模型将无法使用。实例会自动重试，并会在设置轮询时获得密钥——通常在 15 分钟内。重启实例可立即完成。",
    "gw.lastError": "最近错误：{error}",
    "gw.retry": "约 {seconds} 秒后重试（第 {attempts} 次）。重启可立即重试。",
    "gw.envRevoked": "本机环境文件中设置的网关密钥已被吊销，现已忽略并改用当前密钥。功能正常，该行配置已无作用。",
    "gw.sentinelBuilds": "有 {n} 个模型在密钥可用前完成构建——在智能体重建或实例重启前，其调用会失败。",
    "gw.lastSuccess": "上次获得密钥：{when}",
    "gw.unrouted": "网关未提供本部署有权使用的 {groups}。对这些服务的调用会返回“无效模型”错误——图像理解、图像生成和语音功能均依赖它们。这属于 Olano 侧的路由问题：本机无法修复，您自己的服务商密钥不受影响。",
    "section.catalogs": "云端目录",
    "section.catalogs.hint":
      "模型价格、MCP 目录、积分汇率、智能体原型和官方连接器客户端会在后台从 cloud.olano.ai 同步（默认每小时检查一次版本清单）。每个标签显示目录的版本、数据来源（cloud / disk / bundled），连接器目录还会显示可用官方应用的数量。“立即同步”会马上重新拉取全部目录——在管理控制台启用新的官方连接器后可立即使用。",
    "catalogs.syncNow": "立即同步",
    "catalogs.syncing": "同步中…",
    "catalogs.toast.synced": "目录已同步 —— {total} 个中有 {updated} 个已更新。",
    "catalogs.toast.failed": "目录同步失败：{detail}",
    "catalogs.loadFailed": "无法加载目录状态。",
    "catalogs.fetchedAt": "上次拉取：{when}",
    "catalogs.neverFetched": "尚未从云端拉取",
    "section.diskEncryption": "磁盘加密",
    "section.diskEncryption.hint":
      "此实例存储的一切（智能体、记忆、文件、对话、保险库、数据库）都保存在一个加密卷上，开机时自动解锁。此卡片显示该卷的状态，并提供它唯一需要你完成的操作：创建恢复密钥，一把只有你持有的第二把钥匙。",
    "diskEncryption.status": "托管方式",
    "diskEncryption.status.hint":
      "标准托管可防止你的数据因磁盘被盗、快照、备份和离线存储访问而泄露。实例在开机时使用由 Olano 运营的密钥基础设施自动解密。Olano 的生产控制平面保留授权密钥释放的技术能力。",
    "diskEncryption.status.standard": "标准托管",
    "diskEncryption.status.other": "已加密（{mode}）",
    "diskEncryption.volume": "加密卷",
    "diskEncryption.volume.hint":
      "加密卷的标识符（其 LUKS UUID）。它不是秘密：它在实例自己的日志和 Olano 控制台中标识该卷，以便把头部备份或密钥释放对应到正确的卷。",
    "diskEncryption.recovery": "恢复密钥",
    "diskEncryption.recovery.hint":
      "一把无需 Olano 密钥服务即可打开加密卷的第二把钥匙，例如在此实例或 Olano 不复存在时从磁盘镜像读取你的数据。它在实例上生成，只向你显示一次，Olano 从不保存：请把它放进密码管理器或打印后妥善保管。没有它，你的数据只能由 Olano 的密钥服务打开。",
    "diskEncryption.recovery.none": "尚无恢复密钥",
    "diskEncryption.recovery.pending": "待确认",
    "diskEncryption.recovery.pendingUntil":
      "已显示了一把密钥但尚未确认。它将于 {when} 过期；在此之前无法创建其他密钥。如果你还保存着它，请重新输入以确认；否则请取消并重新开始。",
    "diskEncryption.recovery.acknowledged": "已生效",
    "diskEncryption.recovery.activeSince": "自 {when} 起生效。",
    "diskEncryption.recovery.create": "创建恢复密钥",
    "diskEncryption.recovery.replace": "更换恢复密钥",
    "diskEncryption.recovery.confirmPending": "确认已显示的密钥",
    "diskEncryption.recovery.cancelPending": "取消待确认的密钥",
    "diskEncryption.recovery.replaceConfirm.title": "更换恢复密钥？",
    "diskEncryption.recovery.replaceConfirm.body":
      "将生成并一次性显示一把新密钥。在你重新输入并确认新密钥之前，当前密钥仍可打开该卷；从确认那一刻起只有新密钥有效，所有保存旧密钥的地方都必须更新。",
    "diskEncryption.recovery.cancelConfirm.title": "取消待确认的恢复密钥？",
    "diskEncryption.recovery.cancelConfirm.body":
      "已显示的密钥将从卷中移除并失效，且无法找回。你当前的恢复密钥（如果有）不受影响。之后可以再创建新密钥。",
    "diskEncryption.recovery.cancelled": "待确认的恢复密钥已取消。",
    "diskEncryption.header": "头部备份",
    "diskEncryption.header.hint":
      "卷的加密头部（存放密钥槽的部分，绝不包含你的数据）的副本，由 Olano 保存在实例之外。如果磁盘上的头部损坏，这份备份能让你的恢复密钥再次打开该卷。每当密钥变化时都会刷新；代数编号表示当前是哪一份副本。",
    "diskEncryption.header.generation": "第 {n} 代",
    "diskEncryption.header.uploadedAt": "备份于 {when}。",
    "diskEncryption.header.none": "尚未备份",
    "diskEncryption.service": "加密服务",
    "diskEncryption.service.hint":
      "此实例上的 sealer：开机时解锁卷、执行恢复密钥流程并备份头部的程序。仪表盘只在此卡片中、只为已登录的管理员与它通信，且从不经过 Olano。",
    "diskEncryption.service.ok": "Sealer {version}",
    "diskEncryption.service.unavailable": "不可用",
    "diskEncryption.service.unavailable.hint":
      "此实例上的加密服务目前没有响应。你的数据仍处于加密状态，实例照常运行；只有这里的恢复密钥操作暂停。请一分钟后再试。如果重启实例后仍然如此，请联系 Olano 支持。",
    "diskEncryption.loadFailed": "无法读取此实例的加密状态。",
    "diskEncryption.refresh": "刷新",
    "diskEncryption.dialog.createTitle": "创建恢复密钥",
    "diskEncryption.dialog.replaceTitle": "更换恢复密钥",
    "diskEncryption.dialog.intro":
      "实例将生成一把新的恢复密钥并把它加入加密卷。密钥由 32 个字符组成（字母 A 到 Z 和数字 2 到 7），以八组四个字符显示。它只在下一屏显示一次：继续之前请把它复制进密码管理器或抄写下来。随后你需要把它重新输入以确认，这证明你保存的副本是正确的。Olano 从不会看到这把密钥；控制台只知道密钥已存在。",
    "diskEncryption.dialog.introReplace":
      "在新密钥确认之前，当前密钥仍然有效。一旦你确认新密钥，旧密钥就会从卷中移除并失效。",
    "diskEncryption.dialog.start": "生成密钥",
    "diskEncryption.dialog.starting": "生成中…",
    "diskEncryption.dialog.showTitle": "你的恢复密钥",
    "diskEncryption.dialog.showWarning":
      "这是密钥唯一一次显示。Olano 不会保存它，也无法再次显示。如果之后丢失，请在“磁盘加密”卡片中创建新密钥；新密钥确认后，丢失的密钥即失效。",
    "diskEncryption.dialog.copy": "复制密钥",
    "diskEncryption.dialog.stored": "我已把这把密钥妥善保存。",
    "diskEncryption.dialog.next": "下一步：确认",
    "diskEncryption.dialog.confirmTitle": "确认恢复密钥",
    "diskEncryption.dialog.confirmBody":
      "输入或粘贴你刚保存的密钥。连字符、空格和小写都可以。实例会直接用卷来核对；没有其他方式能确认密钥。",
    "diskEncryption.dialog.confirmLabel": "恢复密钥",
    "diskEncryption.dialog.confirm": "确认",
    "diskEncryption.dialog.confirming": "核对中…",
    "diskEncryption.dialog.invalid": "恢复密钥由 32 个字符组成：字母 A 到 Z 和数字 2 到 7。",
    "diskEncryption.dialog.mismatch":
      "这不是刚才显示的密钥。请检查你保存的副本后重试。如果未确认，该密钥将于 {when} 过期。",
    "diskEncryption.dialog.expiredTitle": "密钥已过期",
    "diskEncryption.dialog.expiredBody":
      "该密钥未能及时确认，已从卷中移除，不再能打开任何东西。请重新开始以生成新密钥。",
    "diskEncryption.dialog.startOver": "重新开始",
    "diskEncryption.dialog.doneTitle": "恢复密钥已生效",
    "diskEncryption.dialog.doneBody":
      "你保存的密钥现在可以打开加密卷。请妥善保管：它是唯一的副本。",
    "diskEncryption.dialog.doneReplaced":
      "之前的密钥已从卷中移除，不再有效。",
    "diskEncryption.dialog.close": "关闭",
    "diskEncryption.dialog.cancel": "取消",
    "diskEncryption.dialog.abandonTitle": "放弃这把密钥？",
    "diskEncryption.dialog.abandonBody":
      "现在关闭会取消刚显示的密钥：它将从卷中移除并失效。你当前的恢复密钥（如果有）不受影响。",
    "diskEncryption.dialog.abandon": "取消密钥",
    "diskEncryption.dialog.keepGoing": "继续",
    "diskEncryption.dialog.busy": "实例正在执行另一个步骤。请稍候再试。",
    "diskEncryption.dialog.pendingExists":
      "已有一把密钥在等待确认。请先在“磁盘加密”卡片中确认或取消它。",
    "diskEncryption.dialog.failed": "实例拒绝了请求：{detail}",
    "diskEncryption.dialog.unavailable":
      "此实例上的加密服务没有响应。请一分钟后再试。",
    "catalog.common.copyFailed": "无法复制到剪贴板。",
    "section.externalAccess": "外部访问",
    "section.externalAccess.hint": "允许此部署之外的系统向您的智能体发送消息——通过 MCP（Claude、ChatGPT、Cursor、智能体框架）和 A2A（其他智能体）。两个入口默认关闭，每个智能体仍需在其“外部访问”标签页中填写公开描述并主动开启。",
    "section.externalAccess.direction": "方向确认：这里是关于他人访问您的智能体。智能体连接的服务器位于各智能体的 MCP 标签页；智能体调用的智能体位于“远程智能体”。此处的设置不会改变它们。",
    "field.ea.mcp": "MCP 端点",
    "field.ea.mcp.hint": "在 <公开 URL>/api/mcp 提供部署的 MCP 端点（Streamable HTTP）。标记为“可通过 MCP 访问”的智能体将成为 ask_<agent> 工具。关闭（默认）时对所有人返回 404，无论智能体如何设置。调用方始终需要持有者令牌或已批准的 OAuth 登录。立即生效。",
    "field.ea.a2a": "A2A 端点",
    "field.ea.a2a.hint": "为每个标记为“可通过 A2A 访问”的智能体在 <公开 URL>/api/a2a/agents/<agent> 提供 Agent Card 和 JSON-RPC 端点。关闭（默认）时返回 404。凭据与 MCP 相同。立即生效。",
    "field.ea.requireHttps": "要求 HTTPS",
    "field.ea.requireHttps.hint": "当部署的公开 URL 为明文 http 时拒绝提供上述任何服务，因为令牌通过 Authorization 请求头传输。默认开启。回环和私有网络 URL（开发环境）除外。仅对永远无法从互联网访问的机器关闭。",
    "field.ea.a2aRetention": "A2A 任务保留（天）",
    "field.ea.a2aRetention.hint": "每次入站 A2A 调用的记录（任务 ID、状态、消息和回复）在从部署的任务存储中删除之前，可供调用方通过协议的任务查询检索的时长。仅影响 A2A 信封：对话本身保留在智能体的线程和记录中，“智能体对话”视图也保留自己的日志。默认 7；范围 1–3650。",
    "field.ea.ttl": "默认令牌有效期（天）",
    "field.ea.ttl.hint": "未指定过期时间时铸造的持有者令牌的有效期。默认 90；0 表示永不过期。范围 0–3650。",
    "field.ea.oauth": "内置 OAuth 登录",
    "field.ea.oauth.hint": "Olano 自己的 OAuth 2.1 授权服务器，位于 <公开 URL>/api/oauth。它让仅支持 OAuth 的客户端（例如 Claude.ai 自定义连接器、遵循卡片的 A2A 客户端）无需外部身份提供方即可连接：客户端注册后将用户引导至此处的同意页面，管理员选择其可访问的智能体，随后获得短期令牌和轮换的刷新令牌。它颁发的所有令牌都会出现在下方的令牌列表中。",
    "field.ea.oauthEnabled": "授权服务器",
    "field.ea.oauthEnabled.hint": "默认开启（仅在上方某个端点开启时响应）。关闭 ⇒ 仅支持 OAuth 的客户端无法连接；粘贴的令牌仍可使用。",
    "field.ea.oauthDcr": "动态客户端注册",
    "field.ea.oauthDcr.hint": "接受 RFC 7591 注册，使客户端可在首次登录前自行注册。默认开启；注册本身不授予任何权限——同意页面才会授予。关闭 ⇒ 只有已注册的客户端才能发起登录。",
    "field.ea.oauthAccess": "访问令牌有效期（分钟）",
    "field.ea.oauthAccess.hint": "OAuth 访问令牌在客户端必须刷新之前的有效时长。默认 60；范围 5–1440。",
    "field.ea.oauthRefresh": "刷新令牌有效期（天）",
    "field.ea.oauthRefresh.hint": "客户端在无需重新同意的情况下可持续续期的时长。每次刷新都会轮换令牌。默认 30；范围 1–365。",
    "field.ea.jwt": "外部身份提供方（可选）",
    "field.ea.jwt.hint": "同时接受您自己的授权服务器（Auth0、Keycloak、Okta、Entra…）颁发的访问令牌。令牌将根据颁发者发布的密钥验证；受众必须是此部署的端点，令牌的 scope 决定可访问的智能体（agent:<name> 或 agents:*）。两个字段留空即禁用。",
    "field.ea.jwtIssuer": "颁发者",
    "field.ea.jwtIssuer.hint": "提供方在令牌中写入的确切 `iss` 值，例如 https://your-tenant.auth0.com/。",
    "field.ea.jwtJwks": "JWKS URL",
    "field.ea.jwtJwks.hint": "提供方发布签名密钥的位置，通常为 <issuer>/.well-known/jwks.json。",
    "field.ea.jwtAudience": "受众",
    "field.ea.jwtAudience.hint": "令牌必须携带的 `aud`，例如 `https://api.example.com`。留空表示令牌所用端点的 URL（标准要求）：MCP 端点为 `<public URL>/api/mcp`，A2A 为该智能体自己的 `<public URL>/api/a2a/agents/<agent>`（该处也接受 `<public URL>/api/a2a/agents`）。在此设置的值会同时替代两者；仅当您的提供方无法颁发这些受众时才设置。",
    "field.ea.jwt.half": "请同时设置颁发者和 JWKS URL，或都不设置。",
    "field.aut.external_access": "为外部调用方发布自身并铸造访问令牌",
    "field.aut.external_access.hint": "智能体是否可以在聊天中切换自身的入站暴露（MCP / A2A）并发送令牌铸造链接。这些工具还会拒绝任何非管理员，因此此项仅决定它们是否存在。",
    "section.mobileApp": "移动应用",
    "section.mobileApp.hint": "Olano 移动应用向登录此部署的每个人（包括管理员）显示的内容。手机在每次打开应用以及每次回到前台时下载此布局，因此更改会在下一次打开时生效，无需更新应用或重启运行时。隐藏只会从手机上移除入口：仪表盘和背后的 API 照常工作，不会删除任何内容。",
    "field.maCortex": "显示 Cortex 标签页",
    "field.maCortex.hint": "开启（默认）：标签栏包含 Cortex，可查看并处理每个代理的想法、项目和 Pulse 发现。关闭：移除该标签页，正在 Cortex 页面的手机会切换到“聊天”。无论如何 Cortex 都会在部署上继续运行，此项只决定手机是否提供入口。",
    "field.maInbox": "显示收件箱标签页",
    "field.maInbox.hint": "开启（默认）：标签栏包含收件箱，并显示下方开启的分区。关闭：整个收件箱标签页及其角标被移除，不论下方三个开关如何设置。下方三个分区全部关闭时，收件箱也会被移除。推送通知是独立的（遵循每个人在手机上的通知设置）。",
    "field.maInboxApprovals": "审批分区",
    "field.maInboxApprovals.hint": "开启（默认）：收件箱显示代理暂停等待人工批准或拒绝的操作，以及等待批准的 Cortex 项目。关闭：隐藏该分区，且不再计入收件箱角标。暂停的操作仍在等待，可在仪表盘的审批视图中处理。",
    "field.maInboxAlerts": "提醒分区",
    "field.maInboxAlerts.hint": "开启（默认）：收件箱显示部署的通知（失败的后台运行、需要重新连接的连接等）及其修复按钮。关闭：隐藏该分区，未读提醒不再计入收件箱角标。提醒仍会出现在仪表盘的铃铛中。",
    "field.maInboxIssues": "问题分区",
    "field.maInboxIssues.hint": "开启（默认）：收件箱列出分配给代理和人员或由其提出的问题，并可打开评论。关闭：隐藏该分区。问题功能对代理和仪表盘仍然可用。",
    "field.maFiles": "在设置中显示“文件”",
    "field.maFiles.hint": "开启（默认）：应用的设置标签页有“文件”一行，可浏览和下载每个代理的工作区文件（成员为只读），代理页面也保留文件链接。关闭：从手机上移除这些入口。代理在回复中提到的文件仍可从聊天中打开。",
    "field.maCredits": "在设置中显示“额度与用量”",
    "field.maCredits.hint": "开启（默认）：设置标签页有“额度与用量”一行（仅在按额度计费的部署上显示），包含余额和近期消费。关闭：移除该行，客户在手机上看不到额度余额。用量仍会计量，并可在仪表盘的洞察中查看。",
    "field.maTranscribeRow": "在设置中显示“在本机转写”",
    "field.maTranscribeRow.hint": "开启（默认）：每台手机可以选择语音消息在手机上还是在部署上转为文字。关闭：移除该开关，所有手机都使用下方设置的默认值，包括已经做过选择的手机。",
    "field.maTranscribeDefault": "默认在手机上转写语音消息",
    "field.maTranscribeDefault.hint": "当用户未做选择时（以及上方开关被隐藏时的所有手机）语音消息在哪里转为文字。关闭（默认）：录音发送到部署，由其语音转文字服务转写，与代理在 Telegram 或 WhatsApp 上得到的转写相同。开启：能够识别语音的手机（已授权的 iPhone）自行转写并只发送文字，速度更快且音频保留在手机上；其他手机仍发送录音。",
    // 智能体之间的对话 (config.yaml → peer_threads)
    "section.peerThreads": "智能体之间的对话",
    "section.peerThreads.hint":
      "当一个智能体通过 talk_to_agent 向另一个智能体提出请求时，这一对智能体共用一个对话线程，因此第二个智能体记得第一个已经问过什么。若不设上限，该线程会在部署的整个生命周期内不断增长，每次交流都携带全部历史：回复更慢、消耗更多令牌，在繁忙的部署上甚至会占用足以让运行时重启的内存。这些上限会让共用线程退役并开启新线程；旧线程仍可在“智能体之间”视图中查看。智能体之间的交流大多是一次性请求，所以默认值较短。每次交流时读取，无需重启。",
    "field.ptRotate": "达到上限时开启新线程",
    "field.ptRotate.hint": "开启（默认）：当一对智能体的共用线程超过下方天数或行数上限时退役，下一次交流会开启新线程。关闭：一对智能体永远保留同一个线程，无论多长；下方两个上限被忽略。",
    "field.ptDays": "多少天后退役",
    "field.ptDays.hint": "线程的第一次交流距今达到此天数后退役，1 到 365。默认 3。从线程中的第一条消息起算，而不是最后一条，因此每天都交流的一对智能体仍会按期重新开始。",
    "field.ptTurns": "多少行后退役",
    "field.ptTurns.hint": "同上，当线程的记录达到此行数时退役，10 到 100000。默认 200。一行是一条消息，一次交流（请求加回复）约为两行；200 行大约是 100 次交流。两个上限中先达到的生效。",
    // 检查点历史 (config.yaml → checkpoint_gc)
    "section.checkpointGc": "对话历史清理",
    "section.checkpointGc.hint":
      "智能体的每一步都会保存一个工作状态检查点，以便在审批、重启或崩溃后恢复，而引擎从不删除旧检查点。一次回复只需要最新的几个，因此此清理会按对话删除早于消息列表最近一次完整快照的所有检查点。对话照常工作，你阅读的记录不会被触及；删除的只是无法再读回的历史。在每次启动后几分钟于后台运行，之后按下方间隔运行，从最大的对话开始，每次有时间上限。仅限 Postgres；SQLite 后端不受影响。每次运行前读取，无需重启。",
    "field.gcEnabled": "清理旧检查点",
    "field.gcEnabled.hint": "开启（默认）：启动十分钟后运行一次，之后按下方间隔运行。关闭：不删除任何内容，历史无限增长；在此功能出现前曾有部署累积到 40 GB。重新开启后在下一次运行时生效。",
    "field.gcInterval": "运行间隔（小时）",
    "field.gcInterval.hint": "第一次之后每隔多少小时运行一次，1 到 168。默认 6。一次运行用尽时间预算后会在对话之间停下，下一次从停下处继续，因此更短的间隔只对历史极大的部署有意义。",
    "field.gcKeepMin": "始终保留（检查点数）",
    "field.gcKeepMin.hint": "无论快照情况如何，每个对话始终保留的检查点数，1 到 1000。默认 3。这个余量让清理避开正在进行的回合；调高只会多保留一点历史，不会让回复更快。",
    "section.mcpHealth": "MCP 连接健康",
    "section.pages": "已部署页面",
    "section.pages.hint": "智能体通过 pages_deploy 发布的页面会运行命令：构建（例如 React 或 Vite 应用的 npm run build，以不可变版本提供），或通过页面网址访问的常驻服务器，或两者兼有。它们在智能体自己的沙箱中运行，与其 shell 命令相同，并受智能体的查看密码保护。通过 pages_start 发布的普通页面只提供文件，不受此处设置影响。修改在几秒内生效，无需重启。",
    "field.pgEnabled": "运行页面构建和服务器",
    "field.pgEnabled.hint": "开启（默认）：通过 pages_deploy 发布的页面会被构建，其服务器保持运行、崩溃后自动重启并接受健康检查。关闭：不运行任何构建，所有页面服务器在几秒内停止，这些页面对访问者返回 503，pages_deploy 拒绝新的部署。已构建的静态版本继续提供。普通页面不受影响。",
    "field.pgMaxServices": "每个智能体的服务器数",
    "field.pgMaxServices.hint": "一个智能体可同时运行服务器的页面数量，1 到 50。默认 5。超出限制的部署会被拒绝，并列出该智能体正在运行的服务器；用 pages_stop 停止一个即可腾出名额。静态构建不计入，因为构建后不再运行任何进程。",
    "field.pgHealth": "健康检查间隔（秒）",
    "field.pgHealth.hint": "每个运行中的页面服务器的健康路径被请求的频率，5 到 3600 秒。默认 30。任何低于 500 的响应都算健康；连续三次失败会重启服务器。健康路径在部署每个页面时设定（除非智能体另行设置，否则为 '/'）。",
    "field.pgTimeout": "请求超时（秒）",
    "field.pgTimeout.hint": "访问者的请求等待页面服务器的最长时间，5 到 3600 秒。默认 120。它适用于响应的第一个字节以及流式响应各数据块之间的间隔，因此只要数据持续到达，长时间下载或服务器推送事件流都能正常工作。超时后访问者收到 504。",
    "field.pgBody": "最大上传（MB）",
    "field.pgBody.hint": "转发给页面服务器的最大请求体，1 到 1024 MB。默认 25。超过的表单提交或上传会在到达服务器前返回 413。接受文件上传的页面可调高此值。",
    "field.pgConcurrency": "每个页面的并发请求",
    "field.pgConcurrency.hint": "一个页面服务器同时处理的访问者请求数，1 到 1024。默认 64。超过后访问者收到带 Retry-After 的 503，而不是无限排队，避免一个繁忙页面占用运行时资源。在服务器下次启动时生效。",
    "section.mcpHealth.hint":
      "无需手动重新授权即可保持 MCP 服务器在线：在 OAuth 访问令牌过期前续期，并重新连接掉线的服务器。更改会在一个检查周期内生效，无需重启。",
    "mcpHealth.lastCheck": "上次检查在 {minutes} 分钟前 —— {total} 个服务器中有 {connected} 个已连接。",
    "field.mhEnabled": "自动维护",
    "field.mhEnabled.hint":
      "整个巡检的总开关，默认开启。开启后，运行时会按下方的周期续期 OAuth 令牌并重连掉线的服务器。关闭后，令牌只会在下一次恰好需要它的请求中续期——对空闲代理而言，那可能已在授权无法恢复之后——而连接失败的服务器会一直掉线，直到有人在代理的 MCP 标签页点击「重新加载 MCP」。已连接并正在使用的服务器不受影响。",
    "field.mhRefreshTokens": "续期 OAuth 令牌",
    "field.mhRefreshTokens.hint":
      "默认开启。当存储的访问令牌进入下方的续期窗口时，直接调用提供方的 OAuth 令牌端点续期——因此即使 MCP 服务器本身不可达也能成功。仅处理使用 OAuth 且已存有刷新令牌的服务器；需要你重新登录的服务器不会被触碰，会继续显示「授权」。关闭后，令牌将等到过期，并在下一次工具调用时惰性续期。",
    "field.mhAutoReconnect": "重连掉线的服务器",
    "field.mhAutoReconnect.hint":
      "默认开启。重新连接上次尝试超时或报错的服务器，重试间隔逐步拉长（5 分钟起，逐次翻倍，上限 6 小时），以免一个彻底失效的服务器不停触发代理重建。绝不重试你已禁用的服务器、没有启动命令的服务器，或正在等待授权的服务器——重连无法代替用户授予同意。无论此项设置如何，手动「重新加载 MCP」始终立即重试。",
    "field.mhInterval": "检查周期（分钟）",
    "field.mhInterval.hint":
      "1–1440 分钟，默认 10。巡检运行的频率。不得超过续期窗口，否则令牌可能在已经过期之后才被发现。",
    "field.mhWindow": "剩余不足则续期（分钟）",
    "field.mhWindow.hint":
      "1–720 分钟，默认 20。访问令牌剩余多少有效期时进行续期。请让它明显大于检查周期。仅在开启「续期 OAuth 令牌」时使用。",
    "field.mhWindowOrder": "续期窗口必须大于或等于检查周期。",
    "section.contextWindow": "上下文窗口",
    "section.contextWindow.hint":
      "每个智能体的对话如何被限制在其模型的上下文窗口内。当会话达到触发阈值时，早于保留预算的内容会被压缩成一段摘要（全文保存在工作区的 .olano/conversation_history/ 目录），此后模型只会看到摘要加保留的尾部；/compact 可按需执行同样的操作。过大的 MCP 工具结果会被转存到工作区文件，而不是占满窗口。在智能体下次启动或重新加载时生效；每个字段都可以在 agent.json5 的 context_window 中按智能体覆盖。",
    "field.cwKeepFraction": "压缩后保留（窗口占比）",
    "field.cwKeepFraction.hint":
      "压缩后按原文保留的模型上下文窗口比例，在已知模型窗口大小时使用（目录中有记录，或提供方上报）。0.05 到 0.8 之间的小数，例如在 272k 令牌的模型上 0.35 会保留最近约 95k 令牌的对话。默认 0.35。必须小于触发占比。值越小每次压缩释放的空间越多，但智能体会更早忘记之前的工作。",
    "field.cwTriggerFraction": "压缩触发点（窗口占比）",
    "field.cwTriggerFraction.hint":
      "已知窗口大小时自动压缩的触发占比。0.3 到 0.98 之间的小数。默认 0.85。手动的 compact_conversation 工具（/compact 命令）在达到此值一半时即可使用。",
    "field.cwKeepTokens": "压缩后保留（令牌数，窗口未知时）",
    "field.cwKeepTokens.hint":
      "以绝对令牌数表示的保留预算，仅在本机不知道模型窗口大小时使用（比目录更新的模型、自托管模型）。2000 到 2000000 之间的整数。默认 40000。必须小于令牌触发点。",
    "field.cwTriggerTokens": "压缩触发点（令牌数，窗口未知时）",
    "field.cwTriggerTokens.hint":
      "窗口未知时自动压缩的触发令牌数。8000 到 4000000 之间的整数。默认 170000。请设置为低于未知模型实际最小窗口的值，否则提供方会在压缩运行前拒绝请求。",
    "field.cwMcpLimit": "MCP 结果上限（令牌数）",
    "field.cwMcpLimit.hint":
      "大于此值的 MCP 工具结果（按每 4 个字符一个令牌估算）会被保存到工作区的 .olano/large_tool_results/ 目录，并在对话中替换为文件路径加一段约 1000 令牌、按内容生成的预览：JSON 保留结构并对长列表抽样，表格和日志保留表头、首尾行、每一条错误行以及每类行的一个示例，并注明省略了多少内容。智能体可用 read_file 或 grep 读取其余内容。0 到 200000 之间的整数；0 表示关闭上限。默认 5000，约为一页搜索结果。Olano 自带工具的结果由单独的 20000 令牌转存机制管理。",
    "field.cwFractionOrder": "保留占比必须小于压缩触发占比。",
    "field.cwTokenOrder": "保留令牌预算必须小于令牌触发点。",
    "section.sandbox": "沙箱资源",
    "section.sandbox.hint":
      "单个代理的隔离沙箱容器在运行 Shell 命令时可以使用本机多少资源。两项留空时，限额会根据主机规格自动计算——机器越大，每个代理可用的资源越多，无需任何配置。",
    "sandbox.summary":
      "主机：{hostCpus} 个 CPU · {hostMem} 内存 —— 每个代理的沙箱上限为 {cpus} 个 CPU · {memory}（{source}）。",
    "sandbox.envOverride":
      "本部署设置了环境变量（LIONCLAW_SANDBOX_MEMORY / LIONCLAW_SANDBOX_CPUS），其优先级高于下方对应字段；在移除该变量之前，这里保存的值不会生效。",
    "field.sbMemory": "内存上限",
    "field.sbMemory.hint":
      "每个代理沙箱容器的内存硬上限。超出限额的命令会被内核在沙箱内终止——代理和本面板会继续运行。接受 docker 尺寸字符串：数字加可选的 b/k/m/g 后缀，例如 4g 或 2048m；最小为 6m。留空则使用自动限额：检测到的主机内存的 25%，且不低于 512m。单个代理可在自己的配置中覆盖此值，而 LIONCLAW_SANDBOX_MEMORY 环境变量的优先级高于两者。保存修改后，每个代理会在下次启动或重新加载时重建其沙箱容器——工作区和共享安装的工具位于主机上，不会丢失，但容器内临时安装的内容会丢失。",
    "field.sbMemory.placeholder": "自动（主机内存的 25%）",
    "field.sbCpus": "CPU 上限",
    "field.sbCpus.hint":
      "每个代理沙箱容器的 CPU 上限，以核心数计：0.5 表示半个核心，2 表示两个核心。取值范围 0.1 到 64。这是上限而非预留——空闲沙箱不占用任何资源，多个繁忙代理的总用量可以超过单个代理的份额，因此自动值取的是一个较宽松的比例，而不是用机器规格除以代理数量。留空则使用自动限额：检测到的主机核心数的 50%，且不低于 0.5。单个代理可在自己的配置中覆盖此值，而 LIONCLAW_SANDBOX_CPUS 环境变量的优先级高于两者。保存修改后，与内存上限一样，每个代理会在下次启动时重建其沙箱容器。",
    "field.sbCpus.placeholder": "自动（主机核心数的 50%）",
    "section.egress": "出站访问",
    "section.egress.hint":
      "你的代理的工具可以访问哪些互联网目标。两个列表都为空（默认）表示允许访问所有公共目标——与这些列表出现之前完全一致。单个代理可以在自己的配置中进一步收窄（代理 → 高级 → egress_allow / egress_deny）。保存后对下一次工具调用即时生效，无需重启。这些列表约束代理访问互联网的所有途径：网页与 HTTP 工具、无头浏览器、全部集成工具包，以及沙箱 Shell（Shell 命令中的 curl、pip install 或 git clone）。被阻止的目标会返回可读的错误，而不是长时间挂起。内网与私有网络地址始终被阻止，与这些列表无关；无论你在此填写什么，本部署都能继续访问自己的控制平面。",
    "field.egAllow": "允许的域名",
    "field.egAllow.hint":
      "所有代理的工具可以访问的域名——每行一个，写成裸域名，如 api.github.com 或 example.com。一个域名涵盖其所有子域名；开头的 *.、https:// 前缀、端口或路径会在保存时被去掉，不是域名或 IPv4 地址的条目会被拒绝。留空则允许所有公共目标。设置了列表后，不在列表中的目标会被拒绝，并返回代理可读的错误。请注意，代理实际用到的每个服务都需要在列表中——一个既读邮件又写入 CRM 的客服代理两者都需要。自身 egress_allow 非空的代理会使用自己的列表而不是这个列表；列表为空的代理则继承这个列表。阻止列表会先被检查，并且总是优先。",
    "field.egDeny": "阻止的域名",
    "field.egDeny.hint":
      "任何代理的工具都不得访问的域名——格式与允许列表相同，每行一个。在允许列表之前检查，因此这里的域名即使同时被允许也会保持阻止。每个代理自己的 egress_deny 会被追加到这个列表：代理可以多阻止，不能少阻止。留空则除始终开启的私有网络保护外不阻止任何内容。",
    "placeholder.egDomains": "每行一个域名，例如 api.github.com",

    "section.paths": "路径",
    "section.paths.hint": "每行一个路径。",
    "field.extraBindRoots": "额外绑定根目录",
    "field.extraBindRoots.hint": "智能体可在工作区之外读写的额外目录。",
    "field.archetypeDirs": "原型目录",
    "field.archetypeDirs.hint": "扫描智能体原型的额外目录。",
    "placeholder.onePerLine": "每行一个路径",
  },
  es: {
    "page.title": "Configuración",
    "page.subtitle": "Ajustes globales de Olano — el contenido de config.yaml.",
    "badge.restart": "Requiere reinicio",
    "badge.restart.title": "Se guarda al instante, pero solo se aplica tras reiniciar el runtime.",
    "configPath": "Editando {path}",
    "btn.save": "Guardar cambios",
    "btn.saving": "Guardando…",
    "btn.reset": "Descartar",
    "unsaved.pill": "Cambios sin guardar",
    "unsaved.title": "¿Guardar los cambios?",
    "unsaved.body":
      "Has cambiado ajustes en esta página que no se han guardado. Si sales ahora se descartan.",
    "unsaved.bodyInvalid":
      "Algunos campos de esta página no son válidos (marcados en rojo), así que los cambios no se pueden guardar tal cual. Corrígelos y guarda, o sal y descártalos.",
    "unsaved.saveLeave": "Guardar y salir",
    "unsaved.leave": "Salir sin guardar",
    "unsaved.stay": "Quedarme en esta página",
    "unsaved.nudge": "Tienes cambios sin guardar en la página de Configuración: recuerda guardarlos.",
    "toast.loadFailed": "No se pudo cargar la configuración.",
    "toast.saved": "Configuración guardada.",
    "toast.savedRestart": "Configuración guardada. Reinicia el runtime para aplicar los cambios de puerto / Cuentas del equipo.",
    "toast.saveFailed": "No se pudo guardar la configuración.",
    "toast.notAdmin": "Solo los administradores pueden cambiar los ajustes globales.",
    "section.general": "General",
    "section.general.hint": "Valores por defecto que usan la CLI y el panel cuando no se nombra un agente.",
    "field.defaultAgent": "Agente por defecto",
    "field.defaultAgent.hint": "Se usa cuando un comando no nombra un agente.",
    "field.preferredModel": "Modelo preferido",
    "field.preferredModel.hint": "Modelo de reserva para agentes sin uno. Vacío ⇒ predeterminado.",
    "field.theme": "Tema",
    "field.theme.hint": "Apariencia para este navegador. Se guarda localmente — no forma parte de config.yaml.",
    "model.builtinDefault": "Predeterminado integrado",
    "model.inheritAgent": "Heredar el modelo del agente",

    "section.cognition": "Modelos de cognición",
    "section.cognition.hint": "Modelos que usa el motor Cortex siempre activo en sus fases.",
    "field.fastModel": "Modelo rápido",
    "field.fastModel.hint":
      "Modelo barato y r\u00e1pido para la mayor\u00eda de las fases de cognici\u00f3n. Formato provider:model, p. ej. \u201colano:turbo\u201d, \u201copenai:gpt-5.4-mini\u201d. En un despliegue conectado a cloud.olano.ai el valor por defecto es \u201colano:turbo\u201d \u2014 la capa r\u00e1pida de Olano, facturada en cr\u00e9ditos y sin clave API; en otros casos usa un modelo con tu propia clave (config\u00farala en Secrets). Vaciar el campo restaura el valor por defecto de esta m\u00e1quina. La cognici\u00f3n corre constantemente: este modelo domina el coste, mantenlo barato.",
    "field.deepModel": "Modelo profundo",
    "field.deepModel.hint":
      "Modelo m\u00e1s potente para ejecuciones profundas, mismo formato provider:model. En un despliegue conectado a cloud.olano.ai el valor por defecto es \u201colano:pro\u201d, facturado en cr\u00e9ditos y sin clave API; en otros casos usa un modelo con tu propia clave. Vac\u00edo \u21d2 hereda el modelo del propio agente.",
    "section.defaultModels": "Modelos predeterminados",
    "section.defaultModels.hint":
      "A qué apuntan los alias de modelo del despliegue. Cualquier agente, subagente o selector configurado como \"Default · Turbo/Pro/Max\" sigue el destino que elijas aquí — cámbialo una vez y todos los agentes que usan ese alias cambian en su próxima recarga, sin editar agente por agente. Los agentes del catálogo se instalan con Default · Turbo (expertos) y Default · Pro (equipos).",
    "model.builtinTarget": "Predeterminado de esta máquina — {model}",
    "field.defaultTurbo": "Destino de Turbo",
    "field.defaultTurbo.hint":
      "El modelo concreto detrás de \"Default · Turbo\" — el nivel diario en el que corren la mayoría de agentes y expertos del catálogo. Elige cualquier modelo en formato provider:model; no se permite otro alias default:* (los alias no se encadenan). Dejado en el predeterminado: en un despliegue conectado a cloud.olano.ai resuelve al nivel rápido de Olano (facturado en créditos, sin clave API); en otras instalaciones, a un modelo con tu propia clave (su proveedor debe estar conectado o con clave en Secrets, o todo agente con este alias fallará en su siguiente turno). Se aplica cuando cada agente arranca o se recarga.",
    "field.defaultPro": "Destino de Pro",
    "field.defaultPro.hint":
      "El modelo concreto detrás de \"Default · Pro\" — el nivel más potente con el que se instalan los supervisores de equipo. Mismas reglas que el destino de Turbo: formato provider:model, sin valores default:*; el predeterminado es el nivel Olano Pro en un despliegue gestionado o un modelo con tu propia clave en otros casos; se aplica en el siguiente arranque o recarga de cada agente.",
    "field.defaultMax": "Destino de Max",
    "field.defaultMax.hint":
      "El modelo concreto detrás de \"Default · Max\" — el nivel más capaz (y más caro), para agentes que apuntes a él explícitamente. Mismas reglas que los otros destinos: formato provider:model, sin valores default:*; el predeterminado es el nivel Olano Max en un despliegue gestionado o un modelo con tu propia clave en otros casos; se aplica en el siguiente arranque o recarga de cada agente.",
    "section.speech": "Voz",
    "section.speech.hint":
      "Transcripción de notas de voz y respuestas de voz en las plataformas de chat. La transcripción (STT) usa por defecto el servicio Olano en despliegues gestionados y está desactivada en el resto; las respuestas de voz (TTS) están desactivadas en toda instalación hasta que elijas un proveedor aquí.",
    "field.speechStt": "Voz a texto (notas de voz)",
    "field.speechStt.hint": "Transcribe los mensajes de voz entrantes para el agente. Opciones: Desactivado = las notas de voz piden al remitente reenviar como texto (predeterminado en instalaciones autoalojadas); Olano = transcripción gestionada facturada en créditos olano (predeterminado en despliegues gestionados, sin clave API); OpenAI/Groq = tu propia clave API; Qwen es solo TTS y no transcribe; Personalizado = cualquier servidor autoalojado compatible con OpenAI.",
    "field.speechTts": "Texto a voz (respuestas de voz)",
    "field.speechTts.hint": "Respuestas con burbuja de voz a mensajes de voz, y la herramienta text_to_speech. Opciones: Desactivado = solo respuestas de texto (predeterminado en toda instalación — las respuestas de voz siempre se activan explícitamente); Olano = síntesis gestionada facturada en créditos olano (sin clave API, solo despliegues gestionados); OpenAI/Groq/Qwen = tu propia clave API; Personalizado = cualquier servidor autoalojado compatible con OpenAI.",
    "field.speechModel": "Modelo",
    "field.speechModel.hint": "Vacío ⇒ el predeterminado del proveedor.",
    "field.speechVoice": "Voz",
    "field.speechVoice.hint": "Vacío ⇒ la voz predeterminada del proveedor. El botón de reproducir lee dos frases de muestra con el proveedor, el modelo y la voz elegidos aquí, incluidos los cambios aún sin guardar, para que escuches una voz antes de quedarte con ella; púlsalo de nuevo para detenerla. Es una llamada de voz real y se factura como una respuesta de voz (créditos olano en Olano, tu propia clave en los demás).",
    "speech.preview.play": "Escuchar esta voz",
    "speech.preview.loading": "Generando la muestra (haz clic para cancelar)",
    "speech.preview.stop": "Detener la muestra",
    "speech.preview.failed": "No se pudo reproducir la muestra de voz.",
    "speech.preview.sample": "¡Hola! Así sonará tu agente cuando responda con un mensaje de voz.",
    "field.speechFormat": "Formato de audio",
    "field.speechFormat.hint":
      "El contenedor de audio que se pide al proveedor — es lo que decide si una respuesta de voz llega como burbuja de voz nativa o como archivo de audio adjunto. Solo Ogg/Opus puede ser burbuja (el reproductor redondo de Telegram, un push-to-talk de WhatsApp); cualquier otro contenedor se entrega como archivo de audio reproducible. Opciones: Auto (el predeterminado — déjalo así) pide Opus en OpenAI y Olano, WAV en Groq PlayAI (el único contenedor que admite) y no pide nada en Qwen (DashScope no tiene ese parámetro) ni en un servidor personalizado (que podría rechazar un formato que nunca implementó); Opus da burbujas de voz — elígelo explícitamente para un servidor autoalojado que lo admita; MP3, WAV, FLAC, AAC y M4A se entregan siempre como archivos de audio, nunca como burbujas (WAV y FLAC no están comprimidos, así que espera varios MB por respuesta); PCM son muestras en bruto para tuberías de posprocesado y no se puede reproducir en una app de chat. Lo que el proveedor devuelva realmente se detecta a partir del propio audio, así que un proveedor que ignore este ajuste degrada a un archivo de audio correctamente etiquetado en vez de a una burbuja rota.",
    "speech.format.auto": "Auto (según el proveedor — recomendado)",
    "speech.format.opus": "Opus / Ogg (burbujas de voz)",
    "speech.format.mp3": "MP3 (archivo de audio)",
    "speech.format.wav": "WAV (archivo de audio, sin comprimir)",
    "speech.format.flac": "FLAC (archivo de audio, sin pérdida)",
    "speech.format.aac": "AAC (archivo de audio)",
    "speech.format.m4a": "M4A (archivo de audio)",
    "speech.format.pcm": "PCM (muestras en bruto — no se reproduce en el chat)",
    "field.speechUrl": "URL del endpoint",
    "field.speechUrl.hint": "Cualquier servidor compatible con OpenAI, p. ej. Speaches / faster-whisper autoalojado.",
    "field.speechKeyName": "Credencial de la clave API",
    "field.speechKeyName.hint": "Nombre de la credencial (bóveda/entorno) con la clave. Vacío si el servidor no necesita.",
    "speech.provider.disabled": "Desactivado (predeterminado)",
    "speech.provider.olano": "Olano (gestionado — facturado en créditos olano)",
    "speech.provider.openai": "OpenAI",
    "speech.provider.groq": "Groq",
    "speech.provider.qwen": "Qwen TTS (Alibaba)",
    "speech.provider.custom": "Personalizado (autoalojado)",
    "placeholder.providerDefault": "Predeterminado del proveedor",

    // Multimedia y búsqueda
    "section.media": "Multimedia y búsqueda",
    "section.media.hint":
      "Qué servicio impulsa la comprensión de imágenes, la generación de imágenes y la búsqueda web. Cada lista muestra el servicio gestionado de Olano (facturado en créditos olano, cuando tu plan lo incluye) más cualquier proveedor cuya clave API ya tenga esta instancia.",
    "field.mediaVision": "Comprensión de imágenes (visión)",
    "field.mediaVision.hint":
      "El modelo que lee imágenes para el agente (la herramienta image_describe). Automático (predeterminado) usa Olano Vision en un plan gestionado; si no, el primer proveedor con capacidad de visión cuya clave tenga esta instancia. Elige “Olano Vision” para facturarlo en créditos olano sin clave, o uno de los modelos de visión de tus propios proveedores (solo se listan los proveedores con clave presente). Anula la variable de entorno heredada LIONCLAW_VISION_MODEL cuando se establece; déjalo en Automático para seguir respetándola.",
    "field.mediaImage": "Generación y edición de imágenes",
    "field.mediaImage.hint":
      "El backend de las herramientas image_generate / image_edit. Automático (predeterminado) usa Olano Image en un plan gestionado; si no, tu clave de imágenes de OpenAI o Gemini. Elige “Olano Image” para facturarlo en créditos olano sin clave, o uno de los modelos de imágenes de tus proveedores (OpenAI gpt-image / DALL·E, Google Imagen / Gemini image). La edición necesita un backend que sirva ediciones de imágenes: los modelos de imagen de Gemini solo generan. Anula la variable de entorno heredada LIONCLAW_IMAGE_MODEL cuando se establece; déjalo en Automático para seguir respetándola.",
    "field.webSearch": "Búsqueda web",
    "field.webSearch.hint":
      "La fuente de las herramientas web_search / page_extract. Automático (predeterminado) prefiere tu propia clave de Tavily cuando TAVILY_API_KEY está definida; si no, la búsqueda web gestionada de Olano (facturada en créditos olano). “Búsqueda web de Olano” fuerza el servicio gestionado aunque haya una clave de Tavily; “Tavily” fuerza tu propia clave (solo se muestra cuando TAVILY_API_KEY está definida); “Desactivada” apaga la búsqueda web por completo y las herramientas devuelven un error indicando al agente que está desactivada.",
    "media.auto": "Automático (recomendado)",
    "media.group.olano": "Gestionado por Olano (créditos olano)",
    "media.group.byok": "Tus proveedores",

    "section.network": "Red y acceso",
    "section.network.hint": "Puertos y la puerta de acceso de Cuentas del equipo. Los cambios se aplican tras reiniciar.",
    "field.runtimeApiPort": "Puerto de la API del runtime",
    "field.dashboardPort": "Puerto del panel",
    "field.multiUser": "Cuentas del equipo",
    "field.multiUser.hint":
      "Exigir inicio de sesión por persona en este panel, con cuentas humanas que invita el administrador (propietario/admin + miembros) y control de acceso por agente. Apagado: cualquiera que alcance el panel actúa como propietario/administrador (instalación clásica de un solo usuario). Si esta clave nunca se ha definido, el valor por defecto depende de la instalación: los despliegues gestionados en la nube lo activan; las instalaciones manuales lo dejan apagado. Guardar este formulario almacena tu elección explícita, que siempre gana. Si lo activas sin cuentas creadas, la próxima carga muestra la pantalla de crear administrador. Se guarda como multi_user_enabled en config.yaml. No confundir con la pestaña «Modo multiusuario» de cada agente, que aísla entre sí a los usuarios finales de un agente compartido.",
    "field.highlightsAll": "Compartir resúmenes con todo el equipo",
    "field.highlightsAll.hint": "Mostrar los resúmenes de trabajo diarios/semanales/mensuales de cada agente a todos los usuarios. Desactivado (predeterminado): los miembros solo ven los de sus agentes.",
    "field.playground": "Zona de pruebas",
    "field.playground.hint": "Añade la pestaña Zona de pruebas: un mundo 2D animado donde se mueven los agentes de este despliegue y donde la generación de personajes produce además hojas de sprites de animación (un trabajo de imagen lento y de varios pasos). Activado o desactivado (predeterminado: desactivado) — mientras está desactivado la pestaña se oculta, la vista nunca se carga, nada se renderiza en segundo plano y la generación de personajes solo crea el retrato rápido. El interruptor afecta a todo el despliegue (todos los usuarios de esta máquina ven lo mismo), se aplica al instante sin reiniciar, y los agentes cuyos personajes se generaron con la opción desactivada obtienen sus sprites la próxima vez que los regeneres. En un despliegue gestionado, tu plan también debe permitir esta pestaña.",

    // Creación de bots por miembros (config.yaml self_serve_agents:)
    "section.selfServe": "Creación de bots por miembros",
    "section.selfServe.hint":
      "Si las personas que no son administradoras pueden crear sus propios bots: el botón «Nuevo bot» de la vista simple y el mismo punto de creación en el resto del panel. Los administradores siempre pueden crear bots; nada de esta sección les aplica. Los cambios se aplican al guardar, sin reiniciar. Se guarda como self_serve_agents en config.yaml.",
    "field.ssaEnabled": "Permitir que los miembros creen bots",
    "field.ssaEnabled.hint":
      "Activado (predeterminado): cada miembro con sesión iniciada ve el botón «Nuevo bot» y puede crear bots propios, dentro del límite y la lista de plantillas de abajo; quien crea un bot pasa a ser su propietario automáticamente. Desactivado: los miembros solo chatean con los bots que un administrador les concede, y el botón desaparece de su vista (los administradores conservan el suyo). Solo afecta a la creación: en cualquiera de las dos posiciones, un miembro nunca puede editar, reconfigurar ni borrar bots que no le pertenecen.",
    "field.ssaMax": "Bots por miembro",
    "field.ssaMax.hint":
      "Cuántos bots puede poseer un miembro a la vez: los que creó o de los que fue nombrado propietario. Un número entero de 1 a 100; el predeterminado es 3. Se cuenta por propiedad, así que ser invitado al bot de otra persona nunca consume este cupo. Los administradores están exentos. Un miembro que alcanza el límite recibe un mensaje claro de «límite alcanzado» en lugar de un bot nuevo.",
    "field.ssaArchetypes": "Plantillas permitidas",
    "field.ssaArchetypes.placeholder": "Cualquier plantilla (dejar vacío)",
    "field.ssaArchetypes.hint":
      "Restringe de qué plantillas listas para usar pueden crear bots los miembros. Un nombre de plantilla por línea, exactamente como aparece en el catálogo (p. ej. sales-helper). Vacío — el valor predeterminado — permite todas las plantillas del catálogo y también bots desde cero descritos con palabras. Una lista no vacía es estricta: los miembros solo pueden elegir de ella y la creación desde cero también se rechaza. Nunca aplica a los administradores.",

    // Chats de grupo con varios bots (config.yaml groups:)
    "section.groups": "Chats de grupo",
    "section.groups.hint":
      "Una conversación que varios de tus bots comparten, junto a los chats uno a uno de siempre. Escribes una vez y responde el miembro al que te diriges; cada mensaje del hilo indica quién lo escribió. En la vista simple aparece una sección «Chats de grupo» en la lista de bots con un botón «Nuevo grupo», y cada grupo tiene un panel de información para añadir y quitar miembros. Los cambios se aplican al guardar, sin reiniciar. Se guarda como groups en config.yaml.",
    "field.grpEnabled": "Activar chats de grupo",
    "field.grpEnabled.hint":
      "Activado (predeterminado): la función existe — se pueden crear grupos, abrirlos y añadir o quitar bots desde el panel de información de cada grupo, y los bots con la herramienta «groups» pueden leer y escribir en los grupos a los que pertenecen. Desactivado: no aparece ninguna sección de grupos, los grupos existentes quedan inaccesibles y toda dirección web de grupo responde «no encontrado». Desactivarlo nunca borra un grupo; al volver a activarlo reaparecen los mismos grupos y sus transcripciones tal como estaban. Que los bots se respondan entre ellos lo decide el interruptor aparte «Permitir que los bots se respondan entre sí» de más abajo, que también viene activado.",
    "field.grpMin": "Mínimo de bots en un grupo",
    "field.grpMax": "Máximo de bots en un grupo",
    "field.grpMin.hint":
      "El grupo más pequeño que se puede crear. Un número entero del 2 al 6; el valor predeterminado es 2. Con menos de dos miembros un «grupo» es simplemente un chat uno a uno, por eso el mínimo es 2. Se comprueba al crear un grupo y al quitar un miembro — se rechaza, indicando el motivo, la eliminación que dejaría al grupo por debajo de esta cifra.",
    "field.grpMax.hint":
      "El grupo más grande posible; se comprueba al crearlo y cada vez que se añade un bot. Un número entero del 2 al 6 y nunca inferior al «Mínimo de bots» de al lado; el valor predeterminado es 6. Cada miembro es un bot en ejecución con su propia copia de la conversación, así que este es un límite real de coste y memoria, no una cuestión de estilo. Puedes bajarlo sin problema — los grupos que ya superen el nuevo límite siguen funcionando y simplemente rechazan nuevas incorporaciones.",
    "field.grpAutonomous": "Permitir que los bots se respondan entre sí",
    "field.grpAutonomous.hint":
      "Activado (predeterminado): cuando la respuesta de un bot menciona a otro miembro con @, ese miembro recibe el turno siguiente automáticamente — así un solo mensaje tuyo puede desencadenar una cadena de bots respondiéndose sin nadie en medio, y cada respuesta entre bots lleva una etiqueta con a quién contesta. Esa es la gracia de un grupo, y es también el ajuste que gasta llamadas al modelo que no pediste una por una; por eso está separado del interruptor de arriba y por eso existen los dos límites de abajo. Cada parada — límite de turnos, tiempo agotado o dos bots yendo y viniendo — se escribe en la conversación, así que una cadena nunca se queda callada sin más. Desactivado: escribes, responden los bots que nombraste y el hilo te espera; una mención @ en una respuesta solo marca entonces quién debería retomar el trabajo. Si prefieres observar unos cuantos hilos primero, desactívalo aquí y vuelve a activarlo después — los grupos existentes se conservan en ambos casos.",
    "field.grpHops": "Turnos entre bots por mensaje",
    "field.grpHops.hint":
      "Cuántas veces pueden pasarse el turno los bots tras un mensaje tuyo. Un número entero del 1 al 10; el valor predeterminado es 3. Se cuenta por cada mensaje que envías, así que una conversación de cinco mensajes con el valor predeterminado permite como mucho tres turnos entre bots cada vez, no tres en total. Cuando se agota, el hilo se detiene y lo dice en la conversación. Solo aplica mientras «Permitir que los bots se respondan entre sí» esté activado — con él desactivado ningún mensaje recibe un segundo turno.",
    "field.grpTimeout": "Límite de tiempo de la cadena (segundos)",
    "field.grpTimeout.hint":
      "Cuánto puede durar en total una cadena de respuestas entre bots antes de detenerse sola, en segundos. Un número entero del 10 al 1800 (30 minutos); el valor predeterminado es 180 (3 minutos). Es tiempo real sobre el conjunto de la cadena, así que un modelo lento que alcance el límite a mitad de turno termina la cadena ahí con una nota en la conversación en lugar de dejarla colgada. Súbelo junto con el número de turnos de al lado — más turnos bajo el mismo límite de tiempo solo convierte «sin turnos» en «sin tiempo». Solo aplica mientras «Permitir que los bots se respondan entre sí» esté activado.",

    // Autoconfiguración de agentes (config.yaml agent_autonomy:)
    "section.autonomy": "Autoconfiguración de agentes",
    "section.autonomy.hint":
      "Predeterminados del despliegue para lo que los agentes pueden cambiar sobre SÍ MISMOS cuando se lo piden en una conversación: cada interruptor concede un conjunto de herramientas self_* de chat a todos los agentes. Todo activado de fábrica. Un agente individual puede anular cualquier interruptor en ambas direcciones desde su propia página (Agentes → agente → Autoconfiguración por chat, tres estados con Heredar). Los cambios aplican en la siguiente llamada de herramienta de cada agente, sin reiniciar. Excepción: los agentes en modo atención al cliente (aislamiento por cliente) heredan todo en No independientemente de estos interruptores — los desconocidos no deben reconfigurar un bot de soporte — y solo un Sí explícito por agente reactiva una capacidad allí. Gobiernan solo al agente actuando sobre sí mismo: los permisos humanos y las herramientas de AgentFather no cambian. Se guarda como agent_autonomy en config.yaml.",
    "field.aut.explore_archetypes": "Explorar la biblioteca de plantillas",
    "field.aut.explore_archetypes.hint":
      "Solo lectura: los agentes pueden buscar y leer las plantillas de expertos/equipos, p. ej. para sugerir un especialista. Por sí solo no concede ninguna instalación.",
    "field.aut.change_model": "Cambiar su propio modelo",
    "field.aut.change_model.hint":
      "Los agentes pueden listar los modelos alcanzables y cambiarse a uno (aplica tras reiniciarse el agente). Los proveedores no conectados se rechazan. En un despliegue gestionado un modelo más grande gasta créditos más rápido.",
    "field.aut.modify_prompt": "Editar sus propias instrucciones",
    "field.aut.modify_prompt.hint":
      "Los agentes pueden reescribir su propio prompt de sistema cuando se lo piden (reemplazo completo, aplica tras reiniciar). Cada cambio queda en Configuración → Revisiones a nombre del agente, revisable y reversible.",
    "field.aut.restart_self": "Reiniciarse",
    "field.aut.restart_self.hint":
      "Los agentes pueden reiniciarse para aplicar los cambios guardados: unos segundos de pausa, programados justo después de la respuesta que lo anuncia.",
    "field.aut.add_subagents": "Añadir subagentes",
    "field.aut.add_subagents.hint":
      "Los agentes pueden añadirse subagentes especialistas —desde cero o a partir de una plantilla— y delegarles trabajo. Aplica tras reiniciar.",
    "field.aut.connect_mcps": "Conectar servicios (servidores MCP y kits de herramientas integrados)",
    "field.aut.connect_mcps.hint":
      "Los agentes pueden buscar en el directorio MCP y añadir, quitar o autorizar por OAuth servidores MCP sobre sí mismos. El inicio de sesión ocurre en el navegador del usuario; las claves API van por la bóveda, nunca a archivos de configuración.",
    "field.aut.connect_platforms": "Conectar plataformas de mensajería",
    "field.aut.connect_platforms.hint":
      "Los agentes pueden conectarse canales de mensajería: plataformas con token (Telegram, Discord, Slack — el token del bot se guarda en la bóveda) y WhatsApp por QR mostrado en el propio chat. Un canal nuevo es una puerta de entrada nueva; se instruye a los agentes a exigir una lista de usuarios permitidos.",
    "field.aut.manage_skills": "Gestionar sus propias habilidades",
    "field.aut.manage_skills.hint":
      "Los agentes pueden listar las habilidades que tienen y activar o desactivar cada una para sí mismos — los mismos interruptores que la pestaña Habilidades de cada agente. Nunca instala, edita ni borra una habilidad, y desactivar una habilidad compartida solo afecta al agente que lo hizo. Se aplica cuando ese agente se reinicia.",
    "field.aut.magic_links": "Enviar enlaces mágicos",
    "field.aut.magic_links.hint":
      "Los agentes pueden enviar enlaces mágicos: páginas de corta duración protegidas con PIN que una persona abre en su propio navegador para terminar lo que un chat no puede: iniciar sesión en un servicio o servidor MCP (OAuth / flujo de dispositivo), escribir una clave API directamente en la bóveda para que nunca pase por la conversación, conectar WhatsApp/WeChat por QR o Telegram/Discord/Slack por token de bot, editar las instrucciones de un agente o un archivo, o abrir el panel en su teléfono. Es la forma predeterminada en que los agentes se conectan a cualquier cosa, por eso viene activado. Apagado quita todas las herramientas magic_link_* de cada agente que hereda este interruptor; un agente puede volver a activarlo desde su propia página, y una sola herramienta puede quitarse por agente en su pestaña Herramientas. Solo propietarios (en mensajeros, la lista de usuarios permitidos del canal), chats de grupo rechazados, tarjeta de aprobación en los perfiles de seguridad más estrictos. Config → Enlaces mágicos (enabled) sigue siendo el interruptor duro de todo el despliegue y controla por separado el enlace de acceso al panel.",
    // Límites de tasa de mensajes (valores globales por defecto)
    "section.rateLimits": "Límites de tasa de mensajes",
    "section.rateLimits.hint":
      "Límites por defecto de mensajes entrantes, por agente. Cada agente puede endurecerlos. Vacío = sin límite. El trabajo programado y autónomo está exento.",
    "field.rlPerMinute": "Mensajes / minuto",
    "field.rlPerHour": "Mensajes / hora",
    "field.rlPerDay": "Mensajes / día",
    "field.rlUnlimited": "sin límite",
    "field.rlPerPlatform": "Anulaciones por plataforma (avanzado)",
    "field.rlPerPlatform.hint":
      "Límites por defecto para un canal concreto (min / hora / día), p. ej. telegram o dashboard.",
    "field.rlAddPlatform": "+ Añadir plataforma…",
    "field.rlRemove": "Quitar",
    "field.rlPh.per_minute": "/min",
    "field.rlPh.per_hour": "/h",
    "field.rlPh.per_day": "/día",
    // Enlaces mágicos
    "section.magicLinks": "Enlaces mágicos",
    "section.magicLinks.hint":
      "Páginas temporales que crea esta instancia para que alguien con chat pero sin panel pueda iniciar sesión, editar un archivo o entregar una clave de API, desde su propio móvil.",
    "field.mlEnabled": "Activar enlaces mágicos",
    "field.mlEnabled.hint":
      "Desactivado ⇒ nada puede crear enlaces mágicos (agentes, comandos de chat, el panel, la CLI) y los existentes dejan de abrirse.",
    "field.mlTtl": "Duración del enlace (min)",
    "field.mlTtl.hint": "2–1440 minutos. Cuanto más corto, más seguro.",
    "field.mlPin": "PIN",
    "field.mlPin.hint": "Un enlace reenviado o filtrado no sirve sin él.",
    "mlPin.required": "Obligatorio (recomendado)",
    "mlPin.off": "Desactivado — el enlace por sí solo abre la página",
    "field.mlAttempts": "PIN incorrectos permitidos",
    "field.mlAttempts.hint": "1–10; después el enlace se anula.",
    "field.mlGroups": "Permitir enlaces en chats de grupo",
    "field.mlGroups.hint":
      "Desactivado ⇒ el agente se niega a enviar un enlace en un grupo, donde todos verían el enlace y su PIN.",
    // Acceso al panel — inicia sesión del propietario en el panel.
    "section.ownerLogin": "Acceso al panel",
    "section.ownerLogin.hint":
      "Permite que un agente te envíe un enlace mágico de un solo uso que te inicia sesión en este panel desde tu móvil — sin escribir contraseña.",
    "field.olEnabled": "Enlaces mágicos al panel",
    "field.olEnabled.hint":
      "Desactivado ⇒ los agentes y los comandos de chat no pueden crear un enlace mágico que inicie sesión en el panel.",
    "field.olAutoBind": "Omitir la contraseña en un despliegue de un solo administrador",
    "field.olAutoBind.hint":
      "Confía en que el usuario de chat de la lista de permitidos es el administrador y lo inicia sesión sin contraseña. Solo es seguro en una instancia de un solo administrador.",
    "field.olSessionHours": "Duración de la sesión del panel (horas)",
    "field.olSessionHours.hint":
      "1–720. Cuánto tiempo sigue válida una sesión abierta desde un enlace mágico antes de re-autenticarse.",
    // Monitor de disco y limpieza
    // Presupuestos de uso
    "section.budgets": "Presupuestos de uso",
    "section.budgets.hint":
      "Límites de cuántos tokens pueden gastar los agentes de este despliegue, y de cuánto puede costar. Un turno se rechaza en cuanto un presupuesto se agota, y el rechazo nombra el presupuesto. Los cambios se aplican al siguiente turno — sin reiniciar.",
    "budgets.col.budget": "Presupuesto",
    "budgets.col.used": "Usado",
    "budgets.col.limit": "Límite",
    "budgets.window.daily": "Se reinicia a diario (UTC)",
    "budgets.window.weekly": "Se reinicia cada semana (lunes, UTC)",
    "budgets.window.monthly": "Se reinicia cada mes (día 1, UTC)",
    "budgets.window.lifetime": "No se reinicia nunca",
    "budgets.overridden": "definido aquí, sobre el {plan} del plan",
    "budgets.signed": "definido por tu plan",
    "budgets.placeholder": "Valor del plan: {plan}",
    "budgets.unmanaged":
      "Esta instalación no está gestionada, así que no se contabiliza ni se aplica nada — los límites de abajo son inertes hasta que el despliegue tenga licencia.",
    "budgets.help":
      "Los presupuestos de tokens son barreras contra bucles descontrolados, y son tuyos: escribe un número de tokens (p. ej. 50000000000), la palabra unlimited para quitar el límite por completo, o deja el campo vacío para usar el de tu plan. Cuentan cada token que gasta un agente, incluidas las llamadas con tus propias claves de proveedor — así que un presupuesto puede detener a un agente BYOK aunque no se le haya facturado nada a Olano. Los dos presupuestos de coste los fija tu plan y no se pueden cambiar aquí; limitan lo que Olano paga aguas arriba, y el gasto gestionado ya está limitado aparte por tu saldo de créditos.",
    "field.budgetAlerts": "Notificaciones de presupuesto",
    "field.budgetAlerts.hint":
      "Genera una notificación en el panel a medida que se llena un presupuesto, para que nadie descubra un límite por primera vez con un turno rechazado. Activado por defecto. Desactivarlo silencia todas las notificaciones de presupuesto, incluida la que avisa de que un presupuesto se agotó y se están rechazando turnos.",
    "field.budgetPercents": "Avisar al",
    "field.budgetPercents.hint":
      "Porcentajes de un presupuesto que generan una notificación, separados por comas — p. ej. 50, 60, 70, 80, 90, 95, 99 (el valor por defecto). Cada uno se dispara una vez por presupuesto y por ventana, y un turno que cruce varios a la vez solo genera el más alto. Los valores deben estar entre 1 y 99; el 100 % se anuncia siempre y no hace falta indicarlo. Se aplica a todos los presupuestos de arriba, tanto de tokens como de coste.",

    "section.disk": "Monitor de disco y limpieza",
    "section.disk.hint":
      "Vigila el uso del disco y libera espacio automáticamente antes de que el servidor se quede sin él. Los cambios se aplican dentro de un intervalo de comprobación — sin reiniciar.",
    "field.dmEnabled": "Monitoreo y alertas",
    "field.dmEnabled.hint": "Mide el uso del disco según un horario y alerta cuando se llena.",
    "field.dmPath": "Ruta a vigilar",
    "field.dmPath.hint": "Sistema de archivos a medir. / cubre todo el disco del sistema.",
    "field.dmInterval": "Comprobar cada (min)",
    "field.dmInterval.hint": "5–1440 minutos.",
    "field.dmWarn": "Avisar al (%)",
    "field.dmWarn.hint": "Registra una advertencia y muestra una alerta en el panel.",
    "field.dmCleanupPct": "Auto-limpiar al (%)",
    "field.dmCleanupPct.hint": "Ejecuta las acciones de limpieza de abajo.",
    "field.dmThresholdOrder": "El umbral de auto-limpieza debe ser igual o mayor que el de aviso.",
    "field.dmCleanupEnabled": "Limpieza automática",
    "field.dmCleanupEnabled.hint":
      "Ejecuta las acciones seleccionadas cuando el uso cruza el umbral de auto-limpieza. Las acciones seguras solo borran cachés y registros antiguos; las agresivas también pueden eliminar datos que otro software volvería a descargar.",
    "disk.actions.hint":
      "Elige qué se limpia. Todo está activado por defecto — incluida la agresiva docker_prune, que elimina cosas que otro software tendría que volver a descargar (p. ej. todas las imágenes Docker sin uso). Desmarca una acción para excluirla, p. ej. docker_prune si otro software de este servidor depende de datos Docker en caché.",
    "disk.badge.aggressive": "agresiva",
    "disk.badge.aggressive.title":
      "Libera la mayor cantidad de espacio, pero elimina cosas que otros programas podrían necesitar (se vuelven a descargar al usarse).",
    "disk.badge.unavailable": "no disponible aquí",
    "field.dmCooldown": "Enfriamiento (min)",
    "field.dmCooldown.hint": "Intervalo mínimo entre limpiezas automáticas.",
    "field.dmForceDays": "Limpiar también cada (días)",
    "field.dmForceDays.hint": "0 = solo al cruzar el umbral.",
    "field.dmVacuum": "Tope de tamaño del journal",
    "field.dmVacuum.hint": "Los registros antiguos del sistema se recortan a este tamaño, p. ej. 200M o 1G.",
    "section.gateway": "Pasarela de modelos Olano",
    "section.gateway.hint": "Si este despliegue tiene la clave con la que se autentican los modelos gestionados por Olano (olano:turbo, olano:pro y las herramientas integradas de imagen, visión y voz). La clave llega automáticamente —al aprovisionar, o entregada en el sondeo de ajustes—, así que aquí no hay nada que configurar. Esta tarjeta existe para que un fallo se vea sin necesidad de una shell.",
    "field.gwState": "Estado",
    "field.gwState.hint": "«Conectado» significa que esta instancia tiene una clave de pasarela válida y los modelos gestionados se cobran en créditos. «No funciona» significa que la instancia está conectada a cloud.olano.ai pero aún no tiene clave, así que toda llamada a un modelo olano: falla hasta que llegue una.",
    "gw.ok": "conectado",
    "gw.degraded": "no funciona",
    "gw.explain": "Los modelos gestionados por Olano fallarán hasta que llegue una clave. La instancia reintenta por su cuenta y recibe una en su sondeo de ajustes, normalmente en menos de 15 minutos. Reiniciar la instancia lo hace inmediato.",
    "gw.lastError": "Último error: {error}",
    "gw.retry": "Reintento en ~{seconds}s (intento {attempts}). Reiniciar reintenta de inmediato.",
    "gw.envRevoked": "Una clave de pasarela definida en el archivo de entorno de esta máquina fue revocada y se está ignorando en favor de la actual. No hay nada roto; esa línea ya no sirve.",
    "gw.sentinelBuilds": "{n} modelo(s) se construyeron antes de que hubiera clave; sus llamadas fallan hasta que el agente se reconstruya o la instancia se reinicie.",
    "gw.lastSuccess": "Última clave obtenida: {when}",
    "gw.unrouted": "La pasarela no está sirviendo {groups}, que esta implementación tiene derecho a usar. Las llamadas a esos servicios fallan con un error de modelo no válido: la comprensión de imágenes, la generación de imágenes y la voz dependen de ellos. Es un problema de enrutamiento del lado de Olano: nada en esta instancia puede arreglarlo, y tus propias claves de proveedor no se ven afectadas.",
    "section.catalogs": "Catálogos en la nube",
    "section.catalogs.hint":
      "Los precios de modelos, el directorio MCP, la tasa de créditos, los arquetipos de agentes y los clientes de conectores oficiales se sincronizan desde cloud.olano.ai en segundo plano (el manifiesto de revisiones se comprueba cada hora por defecto). Cada etiqueta muestra la revisión del catálogo, su origen (cloud / disk / bundled) y, para los conectores, cuántas apps oficiales hay disponibles. «Sincronizar ahora» vuelve a descargar todo de inmediato — úsalo justo después de habilitar un conector oficial en la consola de administración.",
    "catalogs.syncNow": "Sincronizar ahora",
    "catalogs.syncing": "Sincronizando…",
    "catalogs.toast.synced": "Catálogos sincronizados — {updated} de {total} actualizados.",
    "catalogs.toast.failed": "La sincronización falló: {detail}",
    "catalogs.loadFailed": "No se pudo cargar el estado de los catálogos.",
    "catalogs.fetchedAt": "Última descarga: {when}",
    "catalogs.neverFetched": "Nunca descargado de la nube",
    "section.diskEncryption": "Cifrado del disco",
    "section.diskEncryption.hint":
      "Esta instancia guarda todo lo que almacena (agentes, memoria, archivos, conversaciones, la bóveda, la base de datos) en un volumen cifrado que se desbloquea automáticamente al arrancar. Esta tarjeta muestra el estado de ese volumen y contiene la única acción que te pide: crear una clave de recuperación, una segunda llave que solo tú tienes.",
    "diskEncryption.status": "Custodia",
    "diskEncryption.status.hint":
      "La custodia estándar protege tus datos frente a la divulgación por discos robados, instantáneas, copias de seguridad y acceso al almacenamiento sin conexión. La instancia se descifra automáticamente al arrancar usando la infraestructura de claves operada por Olano. El plano de control de producción de Olano conserva la capacidad técnica de autorizar la liberación de la clave.",
    "diskEncryption.status.standard": "Custodia estándar",
    "diskEncryption.status.other": "Cifrado ({mode})",
    "diskEncryption.volume": "Volumen cifrado",
    "diskEncryption.volume.hint":
      "El identificador del volumen cifrado (su UUID de LUKS). No es un secreto: nombra el volumen en los registros de la propia instancia y en la consola de Olano, para que una copia de la cabecera o una liberación de clave se asocie al volumen correcto.",
    "diskEncryption.recovery": "Clave de recuperación",
    "diskEncryption.recovery.hint":
      "Una segunda llave que abre el volumen cifrado sin el servicio de claves de Olano, por ejemplo para leer tus datos desde una imagen del disco si esta instancia u Olano ya no existen. Se genera en la instancia, se te muestra exactamente una vez y Olano nunca la guarda: consérvala en un gestor de contraseñas o impresa en un lugar seguro. Sin ella, tus datos solo pueden abrirse con el servicio de claves de Olano.",
    "diskEncryption.recovery.none": "Aún no hay clave de recuperación",
    "diskEncryption.recovery.pending": "Pendiente de confirmar",
    "diskEncryption.recovery.pendingUntil":
      "Se mostró una clave que todavía no se ha confirmado. Caduca el {when}; hasta entonces no se puede crear otra. Si aún la tienes, confírmala volviéndola a escribir; si no, cancélala y empieza de nuevo.",
    "diskEncryption.recovery.acknowledged": "Activa",
    "diskEncryption.recovery.activeSince": "Activa desde el {when}.",
    "diskEncryption.recovery.create": "Crear clave de recuperación",
    "diskEncryption.recovery.replace": "Sustituir la clave de recuperación",
    "diskEncryption.recovery.confirmPending": "Confirmar la clave mostrada",
    "diskEncryption.recovery.cancelPending": "Cancelar la clave pendiente",
    "diskEncryption.recovery.replaceConfirm.title": "¿Sustituir la clave de recuperación?",
    "diskEncryption.recovery.replaceConfirm.body":
      "Se genera una clave nueva y se muestra una sola vez. Tu clave actual sigue abriendo el volumen hasta que confirmes la nueva volviéndola a escribir; desde ese momento solo funciona la nueva, y hay que actualizar cada lugar donde guardaste la antigua.",
    "diskEncryption.recovery.cancelConfirm.title": "¿Cancelar la clave de recuperación pendiente?",
    "diskEncryption.recovery.cancelConfirm.body":
      "La clave que se mostró se elimina del volumen y deja de funcionar; no se puede recuperar. Tu clave de recuperación actual, si tienes una, no se ve afectada. Después puedes crear una nueva.",
    "diskEncryption.recovery.cancelled": "La clave de recuperación pendiente se canceló.",
    "diskEncryption.header": "Copia de la cabecera",
    "diskEncryption.header.hint":
      "Una copia de la cabecera de cifrado del volumen (la parte que contiene las ranuras de claves, nunca tus datos) que Olano guarda fuera de la instancia. Si la cabecera del disco se daña, la copia permite que tu clave de recuperación vuelva a abrir el volumen. Se renueva cada vez que cambian las claves; el número de generación indica cuál es la copia vigente.",
    "diskEncryption.header.generation": "Generación {n}",
    "diskEncryption.header.uploadedAt": "Copiada el {when}.",
    "diskEncryption.header.none": "Todavía sin copia",
    "diskEncryption.service": "Servicio de cifrado",
    "diskEncryption.service.hint":
      "El sealer de esta instancia: el programa que desbloquea el volumen al arrancar, ejecuta la ceremonia de la clave de recuperación y copia la cabecera. El panel habla con él solo desde esta tarjeta, solo para un administrador con sesión iniciada y nunca a través de Olano.",
    "diskEncryption.service.ok": "Sealer {version}",
    "diskEncryption.service.unavailable": "No disponible",
    "diskEncryption.service.unavailable.hint":
      "El servicio de cifrado de esta instancia no responde en este momento. Tus datos siguen cifrados y la instancia sigue funcionando; solo las acciones de la clave de recuperación quedan en pausa. Vuelve a intentarlo en un minuto. Si persiste tras reiniciar la instancia, contacta con el soporte de Olano.",
    "diskEncryption.loadFailed": "No se pudo leer el estado de cifrado de esta instancia.",
    "diskEncryption.refresh": "Actualizar",
    "diskEncryption.dialog.createTitle": "Crear clave de recuperación",
    "diskEncryption.dialog.replaceTitle": "Sustituir la clave de recuperación",
    "diskEncryption.dialog.intro":
      "La instancia genera una clave de recuperación nueva y la añade al volumen cifrado. La clave tiene 32 caracteres (letras de la A a la Z y dígitos del 2 al 7), mostrados en ocho grupos de cuatro. Aparece una sola vez, en la siguiente pantalla: cópiala en un gestor de contraseñas o anótala antes de continuar. Después la confirmas escribiéndola de nuevo, lo que demuestra que la copia que guardaste es correcta. Olano nunca ve la clave; la consola solo sabe que existe una.",
    "diskEncryption.dialog.introReplace":
      "Tu clave actual sigue funcionando hasta que se confirme la nueva. En cuanto confirmes la nueva, la antigua se elimina del volumen y deja de funcionar.",
    "diskEncryption.dialog.start": "Generar la clave",
    "diskEncryption.dialog.starting": "Generando…",
    "diskEncryption.dialog.showTitle": "Tu clave de recuperación",
    "diskEncryption.dialog.showWarning":
      "Esta es la única vez que se muestra la clave. Olano no la guarda y no puede volver a mostrarla. Si la pierdes más adelante, crea una nueva desde la tarjeta Cifrado del disco; la clave perdida deja de funcionar en cuanto se confirma la nueva.",
    "diskEncryption.dialog.copy": "Copiar la clave",
    "diskEncryption.dialog.stored": "He guardado esta clave en un lugar seguro.",
    "diskEncryption.dialog.next": "Siguiente: confirmarla",
    "diskEncryption.dialog.confirmTitle": "Confirmar la clave de recuperación",
    "diskEncryption.dialog.confirmBody":
      "Escribe o pega la clave que acabas de guardar. Los guiones, los espacios y las minúsculas no importan. La instancia la comprueba contra el propio volumen; nada más puede confirmar una clave.",
    "diskEncryption.dialog.confirmLabel": "Clave de recuperación",
    "diskEncryption.dialog.confirm": "Confirmar",
    "diskEncryption.dialog.confirming": "Comprobando…",
    "diskEncryption.dialog.invalid": "Una clave de recuperación tiene 32 caracteres: letras de la A a la Z y dígitos del 2 al 7.",
    "diskEncryption.dialog.mismatch":
      "Esa no es la clave que se mostró. Revisa la copia que guardaste e inténtalo de nuevo. La clave caduca el {when} si no se confirma.",
    "diskEncryption.dialog.expiredTitle": "La clave caducó",
    "diskEncryption.dialog.expiredBody":
      "La clave no se confirmó a tiempo y se ha eliminado del volumen; ya no abre nada. Empieza de nuevo para generar otra.",
    "diskEncryption.dialog.startOver": "Empezar de nuevo",
    "diskEncryption.dialog.doneTitle": "Clave de recuperación activa",
    "diskEncryption.dialog.doneBody":
      "La clave que guardaste ahora abre el volumen cifrado. Consérvala bien: es la única copia.",
    "diskEncryption.dialog.doneReplaced":
      "La clave anterior se ha eliminado del volumen y ya no funciona.",
    "diskEncryption.dialog.close": "Cerrar",
    "diskEncryption.dialog.cancel": "Cancelar",
    "diskEncryption.dialog.abandonTitle": "¿Abandonar la clave?",
    "diskEncryption.dialog.abandonBody":
      "Cerrar ahora cancela la clave que se acaba de mostrar: se elimina del volumen y deja de funcionar. Tu clave de recuperación actual, si tienes una, no se ve afectada.",
    "diskEncryption.dialog.abandon": "Cancelar la clave",
    "diskEncryption.dialog.keepGoing": "Continuar",
    "diskEncryption.dialog.busy": "La instancia está en mitad de otro paso. Espera un momento y vuelve a intentarlo.",
    "diskEncryption.dialog.pendingExists":
      "Ya hay una clave esperando confirmación. Confírmala o cancélala primero desde la tarjeta Cifrado del disco.",
    "diskEncryption.dialog.failed": "La instancia lo rechazó: {detail}",
    "diskEncryption.dialog.unavailable":
      "El servicio de cifrado de esta instancia no responde. Vuelve a intentarlo en un minuto.",
    "catalog.common.copyFailed": "No se pudo copiar al portapapeles.",
    "section.externalAccess": "Acceso externo",
    "section.externalAccess.hint": "Permite que sistemas EXTERNOS a este despliegue envíen mensajes a tus agentes — por MCP (Claude, ChatGPT, Cursor, marcos de agentes) y por A2A (otros agentes). Ambas puertas están cerradas por defecto y cada agente debe además activarse en su pestaña Acceso externo con una descripción pública.",
    "section.externalAccess.direction": "Comprobación de dirección: esto trata de que otros lleguen a TUS agentes. Los servidores a los que se conectan tus agentes están en la pestaña MCP de cada agente; los agentes a los que llaman tus agentes están en Agentes remotos. Nada de aquí cambia eso.",
    "field.ea.mcp": "Punto de acceso MCP",
    "field.ea.mcp.hint": "Sirve el punto de acceso MCP del despliegue en <URL pública>/api/mcp (Streamable HTTP). Los agentes marcados «Accesible por MCP» se convierten allí en herramientas ask_<agente>. Apagado (por defecto) responde 404 a todos, digan lo que digan los agentes. Los llamantes siempre necesitan un token o un inicio de sesión OAuth aprobado. Se aplica de inmediato.",
    "field.ea.a2a": "Puntos de acceso A2A",
    "field.ea.a2a.hint": "Sirve una Agent Card y un punto de acceso JSON-RPC en <URL pública>/api/a2a/agents/<agente> para cada agente marcado «Accesible por A2A». Apagado (por defecto) responde 404. Mismas credenciales que MCP. Se aplica de inmediato.",
    "field.ea.requireHttps": "Exigir HTTPS",
    "field.ea.requireHttps.hint": "Rechaza servir nada de esto cuando la URL pública del despliegue es http plano, porque los tokens viajan en la cabecera Authorization. Activado por defecto. Las URL de bucle local y de red privada (desarrollo) están exentas. Desactívalo solo en una máquina que nunca sea accesible desde internet.",
    "field.ea.a2aRetention": "Retención de tareas A2A (días)",
    "field.ea.a2aRetention.hint": "Cuánto tiempo el registro de cada llamada A2A entrante (su id de tarea, estado, el mensaje y la respuesta) sigue disponible para el llamante mediante la consulta de tareas del protocolo, antes de borrarse del almacén de tareas del despliegue. Solo afecta al sobre A2A: la conversación en sí permanece en el hilo y la transcripción del agente, y la vista Agente a agente conserva su propio registro. 7 por defecto; 1–3650.",
    "field.ea.ttl": "Vigencia de token por defecto (días)",
    "field.ea.ttl.hint": "Vigencia de un token creado sin caducidad explícita. 90 por defecto; 0 significa que nunca caduca. 0–3650.",
    "field.ea.oauth": "Inicio de sesión OAuth integrado",
    "field.ea.oauth.hint": "El servidor de autorización OAuth 2.1 propio de Olano en <URL pública>/api/oauth. Es lo que permite que un cliente solo-OAuth —un conector personalizado de Claude.ai, un cliente A2A que sigue la tarjeta— se conecte sin un proveedor de identidad externo: el cliente se registra, envía a la persona a una página de consentimiento aquí, un administrador elige a qué agentes puede llegar y recibe tokens de corta duración más un token de refresco rotativo. Todo lo que emite aparece en la lista de tokens de abajo.",
    "field.ea.oauthEnabled": "Servidor de autorización",
    "field.ea.oauthEnabled.hint": "Activado por defecto (solo responde mientras un punto de acceso de arriba esté activo). Apagado ⇒ los clientes solo-OAuth no pueden conectarse; los tokens pegados siguen funcionando.",
    "field.ea.oauthDcr": "Registro dinámico de clientes",
    "field.ea.oauthDcr.hint": "Acepta registros RFC 7591 para que un cliente pueda darse de alta por sí mismo antes de su primer inicio de sesión. Activado por defecto; el registro no concede nada — lo concede la página de consentimiento. Apagado ⇒ solo los clientes ya registrados pueden iniciar sesión.",
    "field.ea.oauthAccess": "Vigencia del token de acceso (minutos)",
    "field.ea.oauthAccess.hint": "Cuánto tiempo funciona un token de acceso OAuth antes de que el cliente deba refrescar. 60 por defecto; 5–1440.",
    "field.ea.oauthRefresh": "Vigencia del token de refresco (días)",
    "field.ea.oauthRefresh.hint": "Cuánto tiempo puede un cliente seguir renovando sin un nuevo consentimiento. Cada refresco rota el token. 30 por defecto; 1–365.",
    "field.ea.jwt": "Proveedor de identidad externo (opcional)",
    "field.ea.jwt.hint": "Acepta también tokens de acceso emitidos por tu propio servidor de autorización (Auth0, Keycloak, Okta, Entra…). Los tokens se verifican con las claves publicadas por el emisor; la audiencia debe ser el punto de acceso de este despliegue y los scopes del token deciden los agentes (agent:<nombre> o agents:*). Deja ambos campos vacíos para desactivarlo.",
    "field.ea.jwtIssuer": "Emisor",
    "field.ea.jwtIssuer.hint": "El valor exacto de `iss` que el proveedor pone en sus tokens, p. ej. https://your-tenant.auth0.com/.",
    "field.ea.jwtJwks": "URL de JWKS",
    "field.ea.jwtJwks.hint": "Dónde publica el proveedor sus claves de firma, normalmente <emisor>/.well-known/jwks.json.",
    "field.ea.jwtAudience": "Audiencia",
    "field.ea.jwtAudience.hint": "El `aud` que debe llevar un token, p. ej. `https://api.example.com`. Vacío significa la URL del punto de acceso al que se presenta el token, que es lo que exige el estándar: `<public URL>/api/mcp` en el punto de acceso MCP y la URL propia del agente, `<public URL>/api/a2a/agents/<agent>`, en A2A (allí también se acepta `<public URL>/api/a2a/agents`). Un valor definido aquí sustituye a ambos; defínelo solo si tu proveedor no puede emitir esas audiencias.",
    "field.ea.jwt.half": "Define tanto el emisor como la URL de JWKS, o ninguno.",
    "field.aut.external_access": "Publicarse para llamantes externos y crear tokens de acceso",
    "field.aut.external_access.hint": "Si un agente puede cambiar su propia exposición entrante (MCP / A2A) y enviar un enlace para crear tokens desde el chat. Las herramientas rechazan además a cualquiera que no sea administrador, así que esto solo decide si existen.",
    "section.mobileApp": "App móvil",
    "section.mobileApp.hint": "Lo que la app móvil de Olano muestra a todas las personas que inician sesión en este despliegue, administradores incluidos. Los teléfonos descargan esta configuración al abrir la app y cada vez que vuelve a primer plano, así que un cambio les llega en la siguiente apertura, sin actualizar la app ni reiniciar el runtime. Ocultar algo solo lo quita del teléfono: el panel y la API siguen funcionando y no se borra nada.",
    "field.maCortex": "Mostrar la pestaña Cortex",
    "field.maCortex.hint": "Activado (predeterminado): la barra de pestañas incluye Cortex, donde se ven y gestionan las ideas, proyectos y hallazgos de Pulse de cada agente. Desactivado: se quita la pestaña y un teléfono que estaba en Cortex pasa a Chats. Cortex sigue funcionando en el despliegue; esto solo decide si el teléfono lo ofrece.",
    "field.maInbox": "Mostrar la pestaña Bandeja",
    "field.maInbox.hint": "Activado (predeterminado): la barra de pestañas incluye la Bandeja, con las secciones activadas abajo. Desactivado: se quitan la Bandeja y su contador, sin importar los tres interruptores de abajo. La Bandeja también se quita cuando las tres secciones están desactivadas. Las notificaciones push son aparte (siguen los ajustes de notificaciones de cada persona en su teléfono).",
    "field.maInboxApprovals": "Sección Aprobaciones",
    "field.maInboxApprovals.hint": "Activado (predeterminado): la Bandeja muestra las acciones que un agente pausó para que una persona las apruebe o rechace, y los elementos de Cortex pendientes de aprobación. Desactivado: la sección se oculta y deja de contar en el contador de la Bandeja. Las acciones pausadas siguen esperando y pueden responderse en la vista Aprobaciones del panel.",
    "field.maInboxAlerts": "Sección Alertas",
    "field.maInboxAlerts.hint": "Activado (predeterminado): la Bandeja muestra las notificaciones del despliegue (ejecuciones en segundo plano fallidas, conexiones que hay que reconectar, etc.) con sus botones para resolverlas. Desactivado: la sección se oculta y las alertas sin leer dejan de contar en el contador. Las alertas siguen apareciendo en la campana del panel.",
    "field.maInboxIssues": "Sección Incidencias",
    "field.maInboxIssues.hint": "Activado (predeterminado): la Bandeja lista las incidencias asignadas o abiertas por agentes y personas, y permite comentarlas. Desactivado: la sección se oculta. Las incidencias siguen funcionando para los agentes y en el panel.",
    "field.maFiles": "Mostrar Archivos en Ajustes",
    "field.maFiles.hint": "Activado (predeterminado): la pestaña Ajustes de la app tiene una fila Archivos para explorar y descargar los archivos del espacio de trabajo de cada agente (solo lectura para miembros), y las pantallas de agente conservan su enlace a Archivos. Desactivado: se quitan esas entradas del teléfono. Los archivos que un agente menciona en una respuesta siguen abriéndose desde el chat.",
    "field.maCredits": "Mostrar Créditos y uso en Ajustes",
    "field.maCredits.hint": "Activado (predeterminado): la pestaña Ajustes tiene una fila Créditos y uso, visible solo en despliegues que facturan en créditos, con el saldo y el gasto reciente. Desactivado: se quita la fila, así los clientes nunca ven saldos de créditos en el teléfono. El uso se sigue midiendo y puede verse en Insights del panel.",
    "field.maTranscribeRow": "Mostrar \"Transcribir en este teléfono\" en Ajustes",
    "field.maTranscribeRow.hint": "Activado (predeterminado): cada persona elige, por teléfono, si una nota de voz se convierte en texto en el teléfono o en el despliegue. Desactivado: se quita el interruptor y todos los teléfonos usan el valor predeterminado de abajo, incluso los que ya habían elegido.",
    "field.maTranscribeDefault": "Transcribir notas de voz en el teléfono por defecto",
    "field.maTranscribeDefault.hint": "Dónde se convierte en texto una nota de voz cuando la persona no ha elegido (y en todos los teléfonos mientras el interruptor de arriba está oculto). Desactivado (predeterminado): la grabación se envía al despliegue y la transcribe su servicio de voz a texto, la misma transcripción que el agente recibe en Telegram o WhatsApp. Activado: los teléfonos que reconocen voz (iPhone con permiso concedido) la transcriben ellos mismos y envían solo el texto, lo que es más rápido y deja el audio en el teléfono; los demás siguen enviando la grabación.",
    // Conversaciones entre agentes (config.yaml → peer_threads)
    "section.peerThreads": "Conversaciones entre agentes",
    "section.peerThreads.hint":
      "Cuando un agente pide algo a otro con talk_to_agent, la pareja comparte un hilo de conversación, así que el segundo agente recuerda lo que el primero ya preguntó. Sin límite, ese hilo crece durante toda la vida del despliegue y cada intercambio lo arrastra entero: respuestas más lentas, más tokens y, en un despliegue con mucho uso, memoria suficiente para reiniciar el runtime. Estos límites retiran el hilo compartido y abren uno nuevo; el antiguo sigue legible en la vista Agente a agente. Los intercambios entre agentes suelen ser peticiones de una sola vez, por eso los valores predeterminados son cortos. Se lee en cada intercambio, sin reiniciar.",
    "field.ptRotate": "Abrir un hilo nuevo al llegar a un límite",
    "field.ptRotate.hint": "Activado (predeterminado): el hilo compartido de una pareja se retira cuando supera los días o las filas indicados abajo, y el siguiente intercambio abre uno nuevo. Desactivado: una pareja conserva su único hilo para siempre, crezca lo que crezca; los dos límites de abajo se ignoran.",
    "field.ptDays": "Retirar tras (días)",
    "field.ptDays.hint": "El hilo de una pareja se retira cuando su primer intercambio tiene esta antigüedad en días, de 1 a 365. Predeterminado 3. Se cuenta desde el primer mensaje del hilo, no desde el último, así que una pareja que habla a diario también empieza de cero según lo previsto.",
    "field.ptTurns": "Retirar tras (filas)",
    "field.ptTurns.hint": "Lo mismo, cuando la transcripción del hilo alcanza estas filas, de 10 a 100000. Predeterminado 200. Una fila es un mensaje, así que un intercambio (petición y respuesta) son unas dos; 200 filas son unos 100 intercambios. Se aplica el primero de los dos límites que se alcance.",
    // Historial de puntos de control (config.yaml → checkpoint_gc)
    "section.checkpointGc": "Limpieza del historial de conversaciones",
    "section.checkpointGc.hint":
      "Cada paso que da un agente guarda un punto de control de su estado de trabajo para que un turno pueda reanudarse tras una aprobación, un reinicio o un fallo, y el motor nunca borra uno antiguo. Una respuesta solo necesita los más recientes, así que esta limpieza elimina, por conversación, todo punto de control anterior a la última instantánea completa de la lista de mensajes. Las conversaciones siguen funcionando igual y la transcripción que lees no se toca; lo que desaparece es historial que nada puede volver a leer. Se ejecuta en segundo plano unos minutos después de cada arranque y luego con el intervalo de abajo, las conversaciones más grandes primero, en una pasada acotada. Solo Postgres; el backend SQLite no se toca. Se lee antes de cada pasada, sin reiniciar.",
    "field.gcEnabled": "Limpiar puntos de control antiguos",
    "field.gcEnabled.hint": "Activado (predeterminado): la limpieza se ejecuta diez minutos después del arranque y luego con el intervalo de abajo. Desactivado: no se elimina nada y el historial crece sin límite; un despliegue llegó a 40 GB antes de que esto existiera. Al reactivarlo surte efecto en la siguiente pasada.",
    "field.gcInterval": "Ejecutar cada (horas)",
    "field.gcInterval.hint": "Con qué frecuencia se ejecuta una pasada después de la primera, de 1 a 168 horas. Predeterminado 6. Una pasada que agota su tiempo se detiene entre conversaciones y la siguiente continúa donde quedó, así que un intervalo más corto solo importa en un despliegue con un historial enorme.",
    "field.gcKeepMin": "Conservar siempre (puntos de control)",
    "field.gcKeepMin.hint": "Puntos de control que se conservan siempre por conversación digan lo que digan las instantáneas, de 1 a 1000. Predeterminado 3. El margen mantiene la limpieza lejos de un turno en curso; subirlo conserva un poco más de historial, nunca acelera una respuesta.",
    "section.mcpHealth": "Salud de las conexiones MCP",
    "section.pages": "Páginas desplegadas",
    "section.pages.hint": "Las páginas que un agente publica con pages_deploy ejecutan comandos: una compilación (por ejemplo npm run build para una app React o Vite, servida como versión inmutable), un servidor permanente al que los visitantes llegan por la URL de la página, o ambos. Se ejecutan en el sandbox del propio agente, como sus comandos de shell, detrás de la contraseña de acceso del agente. Las páginas simples publicadas con pages_start solo sirven archivos y no les afecta nada de aquí. Los cambios se aplican en unos segundos, sin reiniciar.",
    "field.pgEnabled": "Ejecutar compilaciones y servidores de páginas",
    "field.pgEnabled.hint": "Activado (predeterminado): las páginas publicadas con pages_deploy se compilan y sus servidores se mantienen en marcha, se reinician tras un fallo y se comprueba su salud. Desactivado: no se ejecuta ninguna compilación, todos los servidores de páginas se detienen en unos segundos, esas páginas responden 503 a los visitantes y pages_deploy rechaza despliegues nuevos. Las versiones estáticas ya compiladas siguen sirviéndose. Las páginas simples nunca se ven afectadas.",
    "field.pgMaxServices": "Servidores por agente",
    "field.pgMaxServices.hint": "Cuántas páginas con servidor en marcha puede tener un agente a la vez, de 1 a 50. Predeterminado 5. Un despliegue que supere el límite se rechaza indicando los servidores del agente en marcha; detener uno con pages_stop libera un hueco. Las compilaciones estáticas no cuentan, porque no ejecutan nada tras compilar.",
    "field.pgHealth": "Comprobar salud cada (segundos)",
    "field.pgHealth.hint": "Con qué frecuencia se solicita la ruta de salud de cada servidor de página en marcha, de 5 a 3600 segundos. Predeterminado 30. Cualquier respuesta por debajo de 500 cuenta como sana; tres fallos seguidos reinician el servidor. La ruta de salud se elige por página al desplegarla ('/' salvo que el agente indique otra).",
    "field.pgTimeout": "Tiempo de espera de la petición (segundos)",
    "field.pgTimeout.hint": "Lo máximo que la petición de un visitante espera a un servidor de página, de 5 a 3600 segundos. Predeterminado 120. Se aplica al primer byte de la respuesta y a cada pausa entre fragmentos de una respuesta en streaming, así que una descarga larga o un flujo de eventos del servidor sigue funcionando mientras lleguen datos. Pasado ese tiempo el visitante recibe 504.",
    "field.pgBody": "Subida máxima (MB)",
    "field.pgBody.hint": "El cuerpo de petición más grande que se reenvía a un servidor de página, de 1 a 1024 MB. Predeterminado 25. Un formulario o subida mayor recibe 413 antes de llegar al servidor. Súbelo para páginas que aceptan archivos.",
    "field.pgConcurrency": "Peticiones simultáneas por página",
    "field.pgConcurrency.hint": "Cuántas peticiones de visitantes atiende a la vez un servidor de página, de 1 a 1024. Predeterminado 64. Al superarlo los visitantes reciben 503 con Retry-After en lugar de una cola sin límite, para que una página muy usada no acapare los recursos del runtime. Se aplica la próxima vez que el servidor arranca.",
    "section.mcpHealth.hint":
      "Mantiene los servidores MCP conectados sin volver a autorizarlos a mano: renueva los tokens de acceso OAuth antes de que caduquen y reconecta los servidores que se cayeron. Los cambios se aplican dentro de un intervalo de comprobación, sin reiniciar.",
    "mcpHealth.lastCheck": "Última comprobación hace {minutes} min — {connected} de {total} servidores conectados.",
    "field.mhEnabled": "Mantenimiento automático",
    "field.mhEnabled.hint":
      "Interruptor general del barrido, activado por defecto. Activado, el runtime renueva los tokens OAuth y reconecta los servidores caídos según el intervalo de abajo. Desactivado, un token solo se renueva en la siguiente petición que lo necesite —para un agente inactivo eso puede ser mucho después de que la concesión ya no se pueda recuperar— y un servidor que falló al conectar seguirá caído hasta que alguien abra la pestaña MCP del agente y pulse Recargar MCP. Los servidores ya conectados y en uso no se ven afectados.",
    "field.mhRefreshTokens": "Renovar tokens OAuth",
    "field.mhRefreshTokens.hint":
      "Activado por defecto. Renueva un token de acceso guardado en cuanto entra en la ventana de renovación de abajo, llamando directamente al endpoint de token OAuth del proveedor, por lo que funciona incluso mientras el servidor MCP está inaccesible. Solo afecta a servidores con OAuth y un refresh token guardado; los que necesitan que vuelvas a iniciar sesión se dejan intactos y siguen mostrando Autorizar. Desactivado, los tokens caducan y se renuevan de forma perezosa en la siguiente llamada a una herramienta.",
    "field.mhAutoReconnect": "Reconectar servidores caídos",
    "field.mhAutoReconnect.hint":
      "Activado por defecto. Reconecta los servidores cuyo último intento expiró o dio error, reintentando con un intervalo creciente (5 minutos, duplicándose, con tope de 6 horas) para que un servidor definitivamente muerto no reconstruya agentes en bucle. Nunca reintenta servidores que desactivaste, sin comando de arranque, o a la espera de autorización: reconectar no puede otorgar el consentimiento. Recargar MCP a mano siempre reintenta de inmediato, independientemente de este ajuste.",
    "field.mhInterval": "Comprobar cada (min)",
    "field.mhInterval.hint":
      "1–1440 minutos; por defecto 10. Cada cuánto se ejecuta el barrido. No debe superar la ventana de renovación, o un token podría detectarse cuando ya haya caducado.",
    "field.mhWindow": "Renovar con menos de (min)",
    "field.mhWindow.hint":
      "1–720 minutos; por defecto 20. Cuánta vida debe quedarle a un token de acceso para renovarlo. Manténlo holgadamente por encima del intervalo de comprobación. Solo se usa con «Renovar tokens OAuth» activado.",
    "field.mhWindowOrder": "La ventana de renovación debe ser mayor o igual que el intervalo de comprobación.",
    "section.contextWindow": "Ventana de contexto",
    "section.contextWindow.hint":
      "Cómo se acota la conversación de cada agente dentro de la ventana de contexto de su modelo. Cuando un hilo alcanza el umbral de disparo, todo lo anterior al presupuesto conservado se resume en un párrafo (el texto completo se guarda en la carpeta .olano/conversation_history/ del espacio de trabajo) y desde entonces el modelo solo ve el resumen más la cola conservada; /compact hace lo mismo bajo demanda. Los resultados MCP demasiado grandes se vuelcan a un archivo del espacio de trabajo en lugar de llenar la ventana. Se aplica a un agente en su siguiente inicio o recarga; cualquier campo puede sobrescribirse por agente en su agent.json5 bajo context_window.",
    "field.cwKeepFraction": "Conservar tras compactar (fracción de la ventana)",
    "field.cwKeepFraction.hint":
      "Fracción de la ventana de contexto del modelo que se conserva literalmente tras una compactación, usada cuando se conoce la ventana del modelo (el catálogo la lista o el proveedor la informa). Un decimal entre 0.05 y 0.8; por ejemplo 0.35 en un modelo de 272k tokens conserva los últimos ~95k tokens de conversación. Por defecto 0.35. Debe ser menor que la fracción de disparo. Valores más bajos liberan más espacio por compactación pero hacen que el agente olvide antes el trabajo previo.",
    "field.cwTriggerFraction": "Compactar al llegar a (fracción de la ventana)",
    "field.cwTriggerFraction.hint":
      "Fracción de la ventana a la que se dispara la compactación automática, usada cuando se conoce la ventana. Un decimal entre 0.3 y 0.98. Por defecto 0.85. La herramienta manual compact_conversation (el comando /compact) queda disponible a la mitad de este valor.",
    "field.cwKeepTokens": "Conservar tras compactar (tokens, ventana desconocida)",
    "field.cwKeepTokens.hint":
      "El presupuesto conservado como recuento absoluto de tokens, usado solo cuando esta máquina no conoce la ventana del modelo (un modelo más nuevo que el catálogo, uno autoalojado). Un entero entre 2000 y 2000000. Por defecto 40000. Debe ser menor que el disparo en tokens.",
    "field.cwTriggerTokens": "Compactar al llegar a (tokens, ventana desconocida)",
    "field.cwTriggerTokens.hint":
      "Recuento de tokens al que se dispara la compactación automática cuando la ventana es desconocida. Un entero entre 8000 y 4000000. Por defecto 170000. Ponlo por debajo de la ventana más pequeña que realmente tengan tus modelos desconocidos, o el proveedor rechazará la petición antes de que la compactación pueda ejecutarse.",
    "field.cwMcpLimit": "Límite de resultados MCP (tokens)",
    "field.cwMcpLimit.hint":
      "Un resultado de herramienta MCP mayor que esto (estimado a cuatro caracteres por token) se guarda en la carpeta .olano/large_tool_results/ del espacio de trabajo y se sustituye en la conversación por la ruta del archivo más una vista previa de unos 1000 tokens adaptada al contenido: el JSON conserva su estructura con las listas largas muestreadas, y las tablas y los registros conservan la cabecera, las primeras y últimas líneas, cada línea de error y un ejemplo de cada tipo de línea, con el recuento de lo omitido. El agente lee el resto con read_file o grep. Un entero entre 0 y 200000; 0 desactiva el límite. Por defecto 5000, aproximadamente una página de resultados de búsqueda. Los resultados de las herramientas propias de Olano se rigen por un volcado aparte de 20000 tokens.",
    "field.cwFractionOrder": "La fracción conservada debe ser menor que la fracción de disparo de la compactación.",
    "field.cwTokenOrder": "El presupuesto de tokens conservado debe ser menor que el disparo en tokens.",
    "section.sandbox": "Recursos del entorno aislado",
    "section.sandbox.hint":
      "Cuánto de esta máquina puede usar el contenedor aislado de un agente al ejecutar comandos de shell. Si los dejas vacíos, ambos límites se calculan a partir del host: una máquina más grande da más margen a cada agente sin configurar nada.",
    "sandbox.summary":
      "Host: {hostCpus} CPU · {hostMem} de RAM — el entorno aislado de cada agente está limitado a {cpus} CPU · {memory} ({source}).",
    "sandbox.envOverride":
      "Hay una variable de entorno definida en este despliegue (LIONCLAW_SANDBOX_MEMORY / LIONCLAW_SANDBOX_CPUS) que tiene prioridad sobre el campo correspondiente de abajo, así que un valor guardado aquí no se aplicará hasta que se elimine esa variable.",
    "field.sbMemory": "Límite de memoria",
    "field.sbMemory.hint":
      "Techo fijo de RAM para el contenedor aislado de cada agente. Un comando que lo supere lo detiene el kernel dentro del entorno aislado: el agente y este panel siguen funcionando. Acepta una cadena de tamaño de docker: un número con sufijo opcional b/k/m/g, como 4g o 2048m; mínimo 6m. Déjalo vacío para el límite automático: el 25% de la RAM detectada del host, nunca por debajo de 512m. Un agente concreto puede anularlo en su propia configuración, y la variable de entorno LIONCLAW_SANDBOX_MEMORY tiene prioridad sobre ambos. Al guardar un cambio se recrea el contenedor aislado de cada agente la próxima vez que ese agente arranque o se recargue: su espacio de trabajo y las herramientas compartidas viven en el host y se conservan, pero se pierde lo instalado de forma puntual dentro del contenedor.",
    "field.sbMemory.placeholder": "automático (25% de la RAM del host)",
    "field.sbCpus": "Límite de CPU",
    "field.sbCpus.hint":
      "Techo de CPU para el contenedor aislado de cada agente, en núcleos: 0,5 es medio núcleo y 2 son dos núcleos. Acepta de 0,1 a 64. Es un techo, no una reserva: un entorno aislado inactivo no consume nada, y varios agentes ocupados pueden superar juntos la parte de cualquiera de ellos, por eso el valor automático es una fracción generosa y no la máquina dividida entre el número de agentes. Déjalo vacío para el límite automático: el 50% de los núcleos detectados del host, nunca por debajo de 0,5. Un agente concreto puede anularlo en su propia configuración, y la variable de entorno LIONCLAW_SANDBOX_CPUS tiene prioridad sobre ambos. Al guardar un cambio se recrea el contenedor aislado de cada agente en su próximo arranque, igual que con el límite de memoria.",
    "field.sbCpus.placeholder": "automático (50% de los núcleos del host)",
    "section.egress": "Acceso saliente",
    "section.egress.hint":
      "Qué destinos de internet pueden alcanzar las herramientas de tus agentes. Con ambas listas vacías —el valor por defecto— se permite cualquier destino público, exactamente como antes de que existieran estas listas. Un agente concreto puede restringirlo más en su propia configuración (Agente → Avanzado → egress_allow / egress_deny). Se aplica a la siguiente llamada de herramienta tras guardar; sin reinicio. Estas listas rigen todo aquello con lo que un agente puede alcanzar internet: sus herramientas web y HTTP, el navegador headless, todos los kits de integración y su shell aislado (un curl, pip install o git clone dentro de un comando de shell). Un destino bloqueado devuelve un error legible en lugar de quedarse colgado. Las direcciones internas y de red privada se bloquean siempre, con independencia de estas listas, y tu despliegue conserva el acceso a su propio plano de control pongas lo que pongas aquí.",
    "field.egAllow": "Dominios permitidos",
    "field.egAllow.hint":
      "Dominios que las herramientas de todos los agentes pueden alcanzar: uno por línea, como dominio a secas, por ejemplo api.github.com o example.com. Un dominio cubre sus subdominios; un *. inicial, un prefijo https://, un puerto o una ruta se eliminan al guardar, y cualquier cosa que no sea un nombre de dominio o una dirección IPv4 se rechaza. Déjalo vacío para permitir cualquier destino público. Con una lista definida, todo destino que no esté en ella se rechaza con un error que el agente puede leer. Ten en cuenta que el agente necesita en la lista todos los servicios que realmente usa: un agente de soporte que lee correo y escribe en tu CRM necesita ambos. Un agente cuyo egress_allow no esté vacío usa su lista EN LUGAR de esta; un agente con la lista vacía hereda esta. La lista de bloqueo se comprueba primero y siempre gana.",
    "field.egDeny": "Dominios bloqueados",
    "field.egDeny.hint":
      "Dominios que ninguna herramienta de ningún agente puede alcanzar: mismo formato que la lista de permitidos, uno por línea. Se comprueba antes que la lista de permitidos, así que un dominio aquí sigue bloqueado aunque también esté permitido. El egress_deny propio de cada agente se AÑADE a esta lista: un agente puede bloquear más, nunca menos. Déjalo vacío para no bloquear nada más allá de la protección de red privada, que está siempre activa.",
    "placeholder.egDomains": "un dominio por línea, p. ej. api.github.com",

    "section.paths": "Rutas",
    "section.paths.hint": "Una ruta por línea.",
    "field.extraBindRoots": "Raíces de enlace adicionales",
    "field.extraBindRoots.hint": "Directorios adicionales que los agentes pueden leer/escribir fuera del workspace.",
    "field.archetypeDirs": "Directorios de arquetipos",
    "field.archetypeDirs.hint": "Directorios adicionales explorados en busca de arquetipos de agentes.",
    "placeholder.onePerLine": "Una ruta por línea",
  },
};
