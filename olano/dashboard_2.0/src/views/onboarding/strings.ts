import type { LocalDict } from "../../core/i18n";

export const STR: LocalDict = {
  en: {
    // chrome
    "ob.skipAll": "Skip setup for now",
    "ob.back": "Back",
    "ob.continue": "Continue",
    "ob.saving": "Saving…",
    "ob.skipStep": "Skip for now",
    "ob.requiredStep": "Required on managed deployments — complete this step to continue.",
    "ob.signout": "Sign out",
    "ob.optional": "Optional",
    "ob.getKey": "Get your {name} API key",

    // rail step labels
    "ob.nav.welcome": "Welcome",
    "ob.nav.provider": "AI provider",

    // welcome
    "ob.welcome.kicker": "Welcome to",
    "ob.welcome.title": "Your AI workforce, ready in minutes",
    "ob.welcome.mission":
      "Olano turns state-of-the-art AI models into a team of agents that work for you — chatting on your channels, searching the web, remembering what matters, and safely automating real work.",
    "ob.welcome.f1.t": "Agents",
    "ob.welcome.f1.d": "Create specialists and teams with memory, tools and their own workspace.",
    "ob.welcome.f2.t": "Channels",
    "ob.welcome.f2.d": "Talk to your agents on Telegram, Slack, WhatsApp, email and more.",
    "ob.welcome.f3.t": "Tools",
    "ob.welcome.f3.d": "Web search, browsing, files, calendars — enable them per agent.",
    "ob.welcome.f4.t": "Vault",
    "ob.welcome.f4.d": "Every key encrypted at rest, with per-agent overrides.",
    "ob.welcome.start": "Let's set it up",
    "ob.welcome.skip": "Skip the tour",

    "ob.cloud.vaultHint":
      "This deployment's vault passphrase was generated when it was provisioned — it's in your welcome email and copyable from your deployment in the cloud console.",
    "ob.cloud.open": "Open your deployment",

    "ob.vault.hint":
      "Managed olano.ai deployments generate their own random vault passphrase — it's in your welcome email and copyable from the deployment card in the cloud console. Self-hosted setups default it to the initial admin password.",
    "ob.vault.setHint":
      "The vault is unlocked on this server, so you can set your own passphrase directly — no need to look up the temporary one this deployment was provisioned with. That temporary passphrase stops working as soon as you save.",
    "ob.vault.envNote":
      "If your server sets OLANO_VAULT_KEY, update it to the new passphrase so the vault auto-unlocks after restarts.",
    "ob.vault.current": "Current passphrase",
    "ob.vault.err.wrong": "That current passphrase is incorrect.",
    "ob.vault.err.short": "Use at least 8 characters.",
    "ob.vault.err.mismatch": "Passphrases don't match.",
    "ob.vault.err.generic": "Could not change the passphrase.",

    // provider
    "ob.prov.title": "Connect an AI provider",
    "ob.prov.lead":
      "Agents think with a language model. Pick your provider and paste an API key — it's stored encrypted in the vault.",
    "ob.prov.ready": "Ready",
    "ob.prov.loading": "Loading available providers…",
    "ob.prov.loadingSlow": "Still loading providers — this can take a few seconds on first run.",
    "ob.prov.loadErr": "Couldn't load the provider list.",
    "ob.prov.retry": "Try again",
    "ob.prov.codexBadge": "Subscription",
    "ob.prov.codexLead":
      "Use your ChatGPT Plus/Pro subscription instead of an API key — sign in with OpenAI and your Codex models are ready.",
    "ob.prov.codexConnect": "Connect with ChatGPT",
    "ob.prov.codexDone": "ChatGPT subscription connected.",
    "ob.prov.more": "More providers…",
    "ob.prov.keyLabel": "{name} API key",
    "ob.prov.save": "Save key",
    "ob.prov.saved": "Key saved to the vault.",
    "ob.prov.already": "A provider is already configured — you're good to go.",
    "ob.prov.note": "You can add more providers anytime — keys live in the Vault view.",
    "ob.prov.byok":
      "Model access is managed by your deployment plan — you can skip this step.",
    "ob.prov.err.generic": "Could not save the key.",
    "ob.prov.vaultDesc": "{provider} API key",

    // provider — Olano Credits included with the plan
    "ob.prov.inc.title": "Your AI is already included",
    "ob.prov.inc.lead":
      "Your {plan} plan comes with Olano Credits, so your agents run on Olano's managed AI from the moment they start. Nothing to connect, no API keys to hunt down.",
    "ob.prov.inc.leadNoPlan":
      "Your plan comes with Olano Credits, so your agents run on Olano's managed AI from the moment they start. Nothing to connect, no API keys to hunt down.",
    "ob.prov.inc.a11y": "Included with your plan",
    "ob.prov.inc.intelligence": "Intelligence",
    "ob.prov.inc.intelligence.d": "Language models for chat, reasoning and real work",
    "ob.prov.inc.image": "Image generation & editing",
    "ob.prov.inc.image.d": "Create images from a description, or edit the ones you have",
    "ob.prov.inc.vision": "Vision",
    "ob.prov.inc.vision.d": "Agents read photos, screenshots and scanned documents",
    "ob.prov.inc.search": "Web search",
    "ob.prov.inc.search.d": "Live answers and sources from today's web",
    "ob.prov.inc.stt": "Speech to text",
    "ob.prov.inc.stt.d": "Voice notes arrive as text your agents can act on",
    "ob.prov.inc.tts": "Text to speech",
    "ob.prov.inc.tts.d": "Agents reply out loud, in a natural voice",
    "ob.prov.inc.credits": "≈ {n} credits available",
    "ob.prov.inc.byokUpsell":
      "Prefer to use your own provider keys? Bring-your-own-keys — OpenAI, Anthropic, ChatGPT subscriptions and the rest — is included on the Ultra plan and above.",
    "ob.prov.inc.byokUpgrade": "Upgrade your plan",
    "ob.prov.inc.byokHead": "Bring your own keys",
    "ob.prov.inc.byokLead":
      "Add a provider key if you want particular models billed to your own account. Your Olano Credits keep covering everything above either way.",

    // Shape C: the Olano credits card on a self-hosted install
    "ob.prov.olano.title": "Run on Olano credits",
    "ob.prov.olano.titleDone": "Olano credits connected",
    "ob.prov.olano.lead":
      "No provider accounts needed: create an API key on cloud.olano.ai, buy credits there, and paste the key here. Your agents then run on Olano's managed models, with image generation, vision, speech and web search included.",
    "ob.prov.olano.done": "Your agents run on Olano credits. Everything below is included.",
    "ob.prov.olano.zero": "This account has no credits yet, so agents on the Olano tiers cannot answer until you buy some on cloud.olano.ai (Billing). You can finish setup now and buy later.",
    "ob.prov.olano.byokHead": "Or bring your own keys",
    "ob.prov.olano.byokLead":
      "Have a provider account already? Paste its API key instead; it is stored encrypted in the vault. You can do both, now or later.",

    // inline vault unlock
    "ob.unlock.title": "Vault is locked",
    "ob.unlock.lead": "Enter the vault passphrase to save this secret.",
    "ob.unlock.field": "Vault passphrase",
    "ob.unlock.submit": "Unlock",
    "ob.unlock.err": "Wrong passphrase.",


    // rail step labels (new panels)
    "ob.nav.profile": "Your profile",
    "ob.nav.agentfather": "Meet Agent Father",
    "ob.welcome.lead":
      "Two minutes: make this account yours, confirm your AI is ready, and meet Agent Father — the agent that builds the rest of your team.",

    // your profile — claim the provisioned account (or create the first one)
    // and set the vault passphrase, in one panel.
    "ob.profile.title": "Make this account yours",
    "ob.profile.lead":
      "You're signed in with the account this instance was created with ({email}) — your email, a temporary password. Give it your name and a password of your own, and set the passphrase that protects your vault.",
    "ob.profile.leadCreate":
      "This dashboard has no sign-in yet — anyone who can reach it has full control. Create the admin account that owns it and set the passphrase that protects your vault; from then on the dashboard asks for this login.",
    "ob.profile.leadVaultOnly":
      "Your account is already yours. One thing left: the passphrase that protects your vault.",
    "ob.profile.accountHead": "Your account",
    "ob.profile.vaultHead": "Vault passphrase",
    "ob.profile.name": "Your name",
    "ob.profile.namePlaceholder": "Alex Rivera",
    "ob.profile.nameHint":
      "Shown next to everything you do in the dashboard, and how your agents address you. Free text, e.g. “Alex Rivera”; you can change it later from your profile. Your handle below is generated from it.",
    "ob.profile.handle": "Handle",
    "ob.profile.handlePending": "type your name to see it",
    "ob.profile.handleHint":
      "Your permanent @handle — lowercase letters, digits and dashes, generated from your name. Agents and the issue tracker refer to you by it. It cannot be edited, here or later; only the name can. A “-2” is added only if another account already holds it.",
    "ob.profile.email": "Your email",
    "ob.profile.emailHint":
      "The address you'll sign in with from any browser or device — a valid email such as you@example.com. Olano doesn't send mail to it; it's your username.",
    "ob.profile.password": "New password",
    "ob.profile.confirm": "Confirm password",
    "ob.profile.passwordHint":
      "Replaces the temporary password from your welcome email. At least 8 characters, and both fields must match. Every other browser still signed in with the temporary password is signed out when you save.",
    "ob.profile.passwordCreateHint":
      "At least 8 characters, and both fields must match. You'll sign in with your email and this password from any browser or device.",
    "ob.profile.passphrase": "Vault passphrase",
    "ob.profile.passphraseConfirm": "Confirm vault passphrase",
    "ob.profile.passphraseHint":
      "Encrypts every API key and secret your agents use. At least 8 characters, both fields matching; make it different from your login password. A managed instance keeps unlocking the vault by itself after restarts with the new passphrase; a self-hosted one may ask you for it.",
    "ob.profile.submit": "Save and continue",
    "ob.profile.accountDone": "Signed in as {name} (@{handle}).",
    "ob.profile.accountDoneGeneric": "Your account is yours — you're signed in with it.",
    "ob.profile.createdDone": "Admin account created — you're signed in as {email}.",
    "ob.profile.vaultDone": "Vault passphrase set.",
    "ob.profile.err.name": "Enter your name.",
    "ob.profile.err.email": "Enter the email you'll sign in with.",
    "ob.profile.err.short": "Use at least 8 characters for the password.",
    "ob.profile.err.mismatch": "Passwords don't match.",
    "ob.profile.err.exists":
      "An account already exists on this box. Sign in with it, then continue setup.",
    "ob.profile.err.generic": "Could not save your account.",

    // meet Agent Father — the last panel, finishes the tour
    "ob.father.title": "Meet Agent Father",
    "ob.father.lead":
      "Agent Father is the agent that builds and runs the rest of your team. You don't configure Olano by hand — you tell Agent Father what you need, in plain language, and approve what it proposes.",
    "ob.father.cap.create.t": "Creates agents",
    "ob.father.cap.create.d":
      "Describe a role — “a support agent for my Shopify store” — and it builds the agent: prompt, tools, memory.",
    "ob.father.cap.reconfigure.t": "Reconfigures them",
    "ob.father.cap.reconfigure.d": "Changes a model, tool, prompt or subagent on any agent when you ask.",
    "ob.father.cap.connect.t": "Connects channels & services",
    "ob.father.cap.connect.d":
      "Telegram, WhatsApp, Slack, email, your calendar and dozens of integrations — it sends you a link whenever a sign-in is needed.",
    "ob.father.cap.people.t": "Invites your team",
    "ob.father.cap.people.d": "Adds teammates, decides which agents they can talk to, and sets their permissions.",
    "ob.father.cap.diagnose.t": "Keeps things running",
    "ob.father.cap.diagnose.d": "Diagnoses a stuck agent, restarts it, tunes deployment settings.",
    "ob.father.cap.approve.t": "Asks before it acts",
    "ob.father.cap.approve.d": "Anything that changes your deployment waits for your approval card in the chat.",
    "ob.father.installing": "Bringing Agent Father online…",
    "ob.father.ready": "Agent Father is online and waiting for you.",
    "ob.father.unavailable": "Agent Father isn't included in this deployment's plan.",
    "ob.father.err": "Couldn't bring Agent Father online.",
    "ob.father.retry": "Try again",
    "ob.father.meet": "Meet Agent Father",
    "ob.father.goDashboard": "Go to the dashboard",
    "ob.father.waking":
      "When you continue, Agent Father wakes up and says hello in the chat — usually within seconds, up to a minute on a brand-new machine.",
    "ob.father.try.h": "Good first things to ask",
    "ob.father.try.1": "Create an agent that answers my customer emails.",
    "ob.father.try.2": "Which providers and connectors can I use?",
    "ob.father.try.3": "Update the sales agent so it also handles leads.",
    "ob.father.tell.h": "Tell it about yourself — it builds better with context",
    "ob.father.tell.1": "Whether this is for you personally or for a business.",
    "ob.father.tell.2": "What your business does, or what you are interested in.",
    "ob.father.tell.3": "The systems you already use — CRM, email, messaging, spreadsheets.",
    "ob.father.tell.4":
      "The repetitive things you do every day or week that you would like handled for you.",
    "ob.finish.err": "Couldn't finish setup. Check that the runtime is running and try again.",
  },
  zh: {
    "ob.skipAll": "暂时跳过设置",
    "ob.back": "返回",
    "ob.continue": "继续",
    "ob.saving": "保存中…",
    "ob.skipStep": "暂时跳过",
    "ob.requiredStep": "托管部署必须完成此步骤才能继续。",
    "ob.signout": "退出登录",
    "ob.optional": "可选",
    "ob.getKey": "获取 {name} API 密钥",

    "ob.nav.welcome": "欢迎",
    "ob.nav.provider": "AI 提供商",

    "ob.welcome.kicker": "欢迎使用",
    "ob.welcome.title": "几分钟内组建你的 AI 团队",
    "ob.welcome.mission":
      "Olano 将最先进的 AI 模型变成为你工作的智能体团队——在你的频道里对话、搜索网络、记住重要信息，并安全地自动化真实工作。",
    "ob.welcome.f1.t": "智能体",
    "ob.welcome.f1.d": "创建拥有记忆、工具和独立工作区的专家与团队。",
    "ob.welcome.f2.t": "频道",
    "ob.welcome.f2.d": "在 Telegram、Slack、WhatsApp、邮件等渠道与智能体交流。",
    "ob.welcome.f3.t": "工具",
    "ob.welcome.f3.d": "网络搜索、浏览、文件、日历——按智能体启用。",
    "ob.welcome.f4.t": "保险库",
    "ob.welcome.f4.d": "所有密钥静态加密，并支持按智能体覆盖。",
    "ob.welcome.start": "开始设置",
    "ob.welcome.skip": "跳过引导",

    "ob.cloud.vaultHint":
      "本部署的保险库口令是在开通时生成的——它在你的欢迎邮件中，也可以在云控制台的部署页面上复制。",
    "ob.cloud.open": "打开你的部署",

    "ob.vault.hint":
      "olano.ai 托管部署会生成独立的随机保险库口令——它在你的欢迎邮件中，也可以在云控制台的部署卡片上复制。自托管安装默认使用初始管理员密码。",
    "ob.vault.setHint":
      "本服务器上的保险库已解锁，你可以直接设置自己的口令，无需查找本部署预置的临时口令。保存后，该临时口令立即失效。",
    "ob.vault.envNote":
      "如果服务器设置了 OLANO_VAULT_KEY，请将其更新为新口令，以便重启后保险库自动解锁。",
    "ob.vault.current": "当前口令",
    "ob.vault.err.wrong": "当前口令不正确。",
    "ob.vault.err.short": "请至少使用 8 个字符。",
    "ob.vault.err.mismatch": "两次输入的口令不一致。",
    "ob.vault.err.generic": "无法更改口令。",

    "ob.prov.title": "连接 AI 提供商",
    "ob.prov.lead": "智能体依靠语言模型思考。选择提供商并粘贴 API 密钥——它将加密存储在保险库中。",
    "ob.prov.ready": "已就绪",
    "ob.prov.loading": "正在加载可用的提供商……",
    "ob.prov.loadingSlow": "仍在加载提供商——首次运行可能需要几秒钟。",
    "ob.prov.loadErr": "无法加载提供商列表。",
    "ob.prov.retry": "重试",
    "ob.prov.codexBadge": "订阅",
    "ob.prov.codexLead": "使用你的 ChatGPT Plus/Pro 订阅，无需 API 密钥——通过 OpenAI 登录即可使用 Codex 模型。",
    "ob.prov.codexConnect": "使用 ChatGPT 登录",
    "ob.prov.codexDone": "ChatGPT 订阅已连接。",
    "ob.prov.more": "更多提供商…",
    "ob.prov.keyLabel": "{name} API 密钥",
    "ob.prov.save": "保存密钥",
    "ob.prov.saved": "密钥已保存到保险库。",
    "ob.prov.already": "已配置提供商——可以继续。",
    "ob.prov.note": "之后可随时添加更多提供商——密钥保存在保险库视图中。",
    "ob.prov.byok": "模型访问由你的部署方案管理——可以跳过此步骤。",
    "ob.prov.err.generic": "无法保存密钥。",
    "ob.prov.vaultDesc": "{provider} API 密钥",

    "ob.prov.inc.title": "AI 能力已包含在你的方案中",
    "ob.prov.inc.lead":
      "你的 {plan} 方案已包含 Olano 积分，智能体一启动就能使用 Olano 托管的 AI。无需连接任何服务，也不用去申请 API 密钥。",
    "ob.prov.inc.leadNoPlan":
      "你的方案已包含 Olano 积分，智能体一启动就能使用 Olano 托管的 AI。无需连接任何服务，也不用去申请 API 密钥。",
    "ob.prov.inc.a11y": "方案已包含的能力",
    "ob.prov.inc.intelligence": "智能（语言模型）",
    "ob.prov.inc.intelligence.d": "用于对话、推理和实际工作的语言模型",
    "ob.prov.inc.image": "图像生成与编辑",
    "ob.prov.inc.image.d": "根据描述生成图像，或编辑已有图像",
    "ob.prov.inc.vision": "视觉理解",
    "ob.prov.inc.vision.d": "智能体可以读取照片、截图和扫描文档",
    "ob.prov.inc.search": "网络搜索",
    "ob.prov.inc.search.d": "来自最新网络的实时答案与信息来源",
    "ob.prov.inc.stt": "语音转文字",
    "ob.prov.inc.stt.d": "语音消息自动转为文字，智能体可直接处理",
    "ob.prov.inc.tts": "文字转语音",
    "ob.prov.inc.tts.d": "智能体用自然的声音朗读回复",
    "ob.prov.inc.credits": "约剩余 {n} 积分",
    "ob.prov.inc.byokUpsell":
      "想使用自己的模型服务商密钥？自带密钥（BYOK）——OpenAI、Anthropic、ChatGPT 订阅等——需要 Ultra 及以上方案。",
    "ob.prov.inc.byokUpgrade": "升级方案",
    "ob.prov.inc.byokHead": "使用自己的密钥",
    "ob.prov.inc.byokLead":
      "如果你希望某些模型计入自己的账户，可以添加服务商密钥。无论是否添加，上面这些能力都继续由 Olano 积分覆盖。",

    // 形态 C：自托管安装上的 Olano 积分卡片
    "ob.prov.olano.title": "使用 Olano 积分运行",
    "ob.prov.olano.titleDone": "Olano 积分已连接",
    "ob.prov.olano.lead":
      "无需任何模型服务商账户：在 cloud.olano.ai 创建 API 密钥并购买积分，然后把密钥粘贴到这里。你的智能体即可使用 Olano 托管模型，并包含图像生成、视觉理解、语音和网络搜索。",
    "ob.prov.olano.done": "你的智能体正使用 Olano 积分运行。下方能力均已包含。",
    "ob.prov.olano.zero": "此账户还没有积分，使用 Olano 层级的智能体在你于 cloud.olano.ai（账单）购买之前无法回答。你可以现在完成设置，稍后再购买。",
    "ob.prov.olano.byokHead": "或使用自己的密钥",
    "ob.prov.olano.byokLead":
      "已经有服务商账户？也可以粘贴它的 API 密钥，它会加密保存在保险库中。两种方式可以同时使用，现在或以后都行。",

    "ob.unlock.title": "保险库已锁定",
    "ob.unlock.lead": "输入保险库口令以保存此机密。",
    "ob.unlock.field": "保险库口令",
    "ob.unlock.submit": "解锁",
    "ob.unlock.err": "口令错误。",


    // rail step labels (new panels)
    "ob.nav.profile": "你的资料",
    "ob.nav.agentfather": "认识 Agent Father",
    "ob.welcome.lead":
      "只需两分钟：把这个账户变成你自己的，确认 AI 已就绪，然后认识 Agent Father——为你搭建整个团队的智能体。",

    // your profile
    "ob.profile.title": "把这个账户变成你的",
    "ob.profile.lead":
      "你当前登录的是创建此实例时自带的账户（{email}）——你的邮箱，加一个临时密码。填上你的姓名、设置自己的密码，并设置保护保险库的口令。",
    "ob.profile.leadCreate":
      "这个控制台还没有登录账户——任何能访问它的人都拥有完全控制权。请创建拥有它的管理员账户，并设置保护保险库的口令；此后控制台会要求使用此登录。",
    "ob.profile.leadVaultOnly": "账户已经是你的了。只剩一件事：设置保护保险库的口令。",
    "ob.profile.accountHead": "你的账户",
    "ob.profile.vaultHead": "保险库口令",
    "ob.profile.name": "你的姓名",
    "ob.profile.namePlaceholder": "Alex Rivera",
    "ob.profile.nameHint":
      "显示在你在控制台的所有操作旁，也是智能体称呼你的方式。自由文本，例如 “Alex Rivera”；以后可在个人资料中修改。下方的用户名由它生成。",
    "ob.profile.handle": "用户名",
    "ob.profile.handlePending": "输入姓名后显示",
    "ob.profile.handleHint":
      "你永久的 @用户名——小写字母、数字和连字符，由姓名生成。智能体和问题跟踪器都用它指代你。此处和以后都不可修改，只有姓名可以。仅当其他账户已占用时才会追加“-2”。",
    "ob.profile.email": "你的邮箱",
    "ob.profile.emailHint":
      "你在任何浏览器或设备上登录时使用的地址——一个有效的邮箱，例如 you@example.com。Olano 不会向它发送邮件；它就是你的登录名。",
    "ob.profile.password": "新密码",
    "ob.profile.confirm": "确认密码",
    "ob.profile.passwordHint":
      "替换欢迎邮件中的临时密码。至少 8 个字符，两次输入必须一致。保存后，其他仍以临时密码登录的浏览器都会被登出。",
    "ob.profile.passwordCreateHint": "至少 8 个字符，两次输入必须一致。你将在任何浏览器或设备上用邮箱和此密码登录。",
    "ob.profile.passphrase": "保险库口令",
    "ob.profile.passphraseConfirm": "确认保险库口令",
    "ob.profile.passphraseHint":
      "加密智能体使用的每个 API 密钥和秘密。至少 8 个字符，两次输入一致；请与登录密码不同。托管实例重启后会自动用新口令解锁保险库；自托管实例可能会要求你输入。",
    "ob.profile.submit": "保存并继续",
    "ob.profile.accountDone": "已以 {name}（@{handle}）登录。",
    "ob.profile.accountDoneGeneric": "账户已是你的——你已用它登录。",
    "ob.profile.createdDone": "管理员账户已创建——你已以 {email} 登录。",
    "ob.profile.vaultDone": "保险库口令已设置。",
    "ob.profile.err.name": "请输入你的姓名。",
    "ob.profile.err.email": "请输入用于登录的邮箱。",
    "ob.profile.err.short": "密码至少需要 8 个字符。",
    "ob.profile.err.mismatch": "两次密码不一致。",
    "ob.profile.err.exists": "这台机器上已存在账户。请用它登录，然后继续设置。",
    "ob.profile.err.generic": "无法保存你的账户。",

    // meet Agent Father
    "ob.father.title": "认识 Agent Father",
    "ob.father.lead":
      "Agent Father 是负责搭建并运行你其余团队的智能体。你不需要手动配置 Olano——用自然语言告诉 Agent Father 你需要什么，然后批准它的提议。",
    "ob.father.cap.create.t": "创建智能体",
    "ob.father.cap.create.d": "描述一个角色——“我 Shopify 店铺的客服智能体”——它会搭建好智能体：提示词、工具、记忆。",
    "ob.father.cap.reconfigure.t": "重新配置",
    "ob.father.cap.reconfigure.d": "按你的要求更改任意智能体的模型、工具、提示词或子智能体。",
    "ob.father.cap.connect.t": "连接渠道与服务",
    "ob.father.cap.connect.d": "Telegram、WhatsApp、Slack、邮件、日历以及几十种集成——需要登录时它会发给你一个链接。",
    "ob.father.cap.people.t": "邀请团队",
    "ob.father.cap.people.d": "添加成员、决定他们可以与哪些智能体对话，并设置权限。",
    "ob.father.cap.diagnose.t": "保持运行",
    "ob.father.cap.diagnose.d": "诊断卡住的智能体、重启它、调整部署设置。",
    "ob.father.cap.approve.t": "先问再做",
    "ob.father.cap.approve.d": "任何会更改部署的操作都会在聊天中等待你的批准卡片。",
    "ob.father.installing": "正在启动 Agent Father…",
    "ob.father.ready": "Agent Father 已上线，正在等你。",
    "ob.father.unavailable": "此部署的套餐不包含 Agent Father。",
    "ob.father.err": "无法启动 Agent Father。",
    "ob.father.retry": "重试",
    "ob.father.meet": "认识 Agent Father",
    "ob.father.goDashboard": "进入控制台",
    "ob.father.waking":
      "继续后，Agent Father 会启动并在聊天中向你打招呼——通常几秒钟内，在全新的机器上最多一分钟左右。",
    "ob.father.try.h": "适合先问的问题",
    "ob.father.try.1": "创建一个能回复客户邮件的智能体。",
    "ob.father.try.2": "我可以使用哪些模型提供商和连接器？",
    "ob.father.try.3": "更新销售智能体，让它也处理潜在客户。",
    "ob.father.tell.h": "介绍一下你自己——有了背景它会搭建得更好",
    "ob.father.tell.1": "这是你个人使用，还是用于业务。",
    "ob.father.tell.2": "你的业务是做什么的，或者你感兴趣的方向。",
    "ob.father.tell.3": "你已经在用的系统——CRM、邮件、即时通讯、电子表格。",
    "ob.father.tell.4": "你每天或每周重复做、希望交给它处理的事情。",
    "ob.finish.err": "无法完成设置。请确认运行时正在运行，然后重试。",
  },
  es: {
    "ob.skipAll": "Omitir la configuración por ahora",
    "ob.back": "Atrás",
    "ob.continue": "Continuar",
    "ob.saving": "Guardando…",
    "ob.skipStep": "Omitir por ahora",
    "ob.requiredStep": "Obligatorio en despliegues gestionados — completa este paso para continuar.",
    "ob.signout": "Cerrar sesión",
    "ob.optional": "Opcional",
    "ob.getKey": "Obtén tu clave API de {name}",

    "ob.nav.welcome": "Bienvenida",
    "ob.nav.provider": "Proveedor de IA",

    "ob.welcome.kicker": "Bienvenido a",
    "ob.welcome.title": "Tu equipo de IA, listo en minutos",
    "ob.welcome.mission":
      "Olano convierte los mejores modelos de IA en un equipo de agentes que trabajan para ti: conversan en tus canales, buscan en la web, recuerdan lo importante y automatizan trabajo real de forma segura.",
    "ob.welcome.f1.t": "Agentes",
    "ob.welcome.f1.d": "Crea especialistas y equipos con memoria, herramientas y su propio espacio de trabajo.",
    "ob.welcome.f2.t": "Canales",
    "ob.welcome.f2.d": "Habla con tus agentes por Telegram, Slack, WhatsApp, correo y más.",
    "ob.welcome.f3.t": "Herramientas",
    "ob.welcome.f3.d": "Búsqueda web, navegación, archivos, calendarios — actívalos por agente.",
    "ob.welcome.f4.t": "Vault",
    "ob.welcome.f4.d": "Cada clave cifrada en reposo, con overrides por agente.",
    "ob.welcome.start": "Vamos a configurarlo",
    "ob.welcome.skip": "Omitir el recorrido",

    "ob.cloud.vaultHint":
      "La frase del vault de este despliegue se generó al aprovisionarlo: está en tu correo de bienvenida y puedes copiarla desde tu despliegue en la consola cloud.",
    "ob.cloud.open": "Abrir tu despliegue",

    "ob.vault.hint":
      "Los despliegues gestionados de olano.ai generan su propia frase aleatoria para el vault: está en tu correo de bienvenida y puedes copiarla desde la tarjeta del despliegue en la consola cloud. Las instalaciones autoalojadas usan por defecto la contraseña inicial del administrador.",
    "ob.vault.setHint":
      "El vault está desbloqueado en este servidor, así que puedes definir tu propia frase directamente: no hace falta buscar la frase temporal con la que se aprovisionó este despliegue. Esa frase temporal deja de funcionar en cuanto guardes.",
    "ob.vault.envNote":
      "Si tu servidor define OLANO_VAULT_KEY, actualízala con la nueva frase para que el vault se desbloquee solo tras un reinicio.",
    "ob.vault.current": "Frase actual",
    "ob.vault.err.wrong": "La frase actual es incorrecta.",
    "ob.vault.err.short": "Usa al menos 8 caracteres.",
    "ob.vault.err.mismatch": "Las frases no coinciden.",
    "ob.vault.err.generic": "No se pudo cambiar la frase.",

    "ob.prov.title": "Conecta un proveedor de IA",
    "ob.prov.lead":
      "Los agentes piensan con un modelo de lenguaje. Elige tu proveedor y pega una clave API — se guarda cifrada en el vault.",
    "ob.prov.ready": "Listo",
    "ob.prov.loading": "Cargando los proveedores disponibles…",
    "ob.prov.loadingSlow":
      "Todavía cargando proveedores — la primera vez puede tardar unos segundos.",
    "ob.prov.loadErr": "No se pudo cargar la lista de proveedores.",
    "ob.prov.retry": "Reintentar",
    "ob.prov.codexBadge": "Suscripción",
    "ob.prov.codexLead":
      "Usa tu suscripción de ChatGPT Plus/Pro en lugar de una clave API — inicia sesión con OpenAI y tus modelos Codex quedan listos.",
    "ob.prov.codexConnect": "Conectar con ChatGPT",
    "ob.prov.codexDone": "Suscripción de ChatGPT conectada.",
    "ob.prov.more": "Más proveedores…",
    "ob.prov.keyLabel": "Clave API de {name}",
    "ob.prov.save": "Guardar clave",
    "ob.prov.saved": "Clave guardada en el vault.",
    "ob.prov.already": "Ya hay un proveedor configurado — todo listo.",
    "ob.prov.note": "Puedes añadir más proveedores cuando quieras — las claves viven en la vista Vault.",
    "ob.prov.byok": "El acceso a modelos lo gestiona tu plan de despliegue — puedes omitir este paso.",
    "ob.prov.err.generic": "No se pudo guardar la clave.",
    "ob.prov.vaultDesc": "Clave API de {provider}",

    "ob.prov.inc.title": "Tu IA ya viene incluida",
    "ob.prov.inc.lead":
      "Tu plan {plan} incluye Créditos Olano, así que tus agentes funcionan con la IA gestionada por Olano desde el primer momento. Nada que conectar, ninguna clave API que buscar.",
    "ob.prov.inc.leadNoPlan":
      "Tu plan incluye Créditos Olano, así que tus agentes funcionan con la IA gestionada por Olano desde el primer momento. Nada que conectar, ninguna clave API que buscar.",
    "ob.prov.inc.a11y": "Incluido en tu plan",
    "ob.prov.inc.intelligence": "Inteligencia",
    "ob.prov.inc.intelligence.d": "Modelos de lenguaje para conversar, razonar y trabajar de verdad",
    "ob.prov.inc.image": "Generación y edición de imágenes",
    "ob.prov.inc.image.d": "Crea imágenes a partir de una descripción, o edita las que ya tienes",
    "ob.prov.inc.vision": "Visión",
    "ob.prov.inc.vision.d": "Tus agentes leen fotos, capturas de pantalla y documentos escaneados",
    "ob.prov.inc.search": "Búsqueda web",
    "ob.prov.inc.search.d": "Respuestas y fuentes en vivo de la web de hoy",
    "ob.prov.inc.stt": "Voz a texto",
    "ob.prov.inc.stt.d": "Las notas de voz llegan como texto que tus agentes pueden usar",
    "ob.prov.inc.tts": "Texto a voz",
    "ob.prov.inc.tts.d": "Tus agentes responden en voz alta, con una voz natural",
    "ob.prov.inc.credits": "≈ {n} créditos disponibles",
    "ob.prov.inc.byokUpsell":
      "¿Prefieres usar tus propias claves? Traer tus claves (BYOK) — OpenAI, Anthropic, suscripciones de ChatGPT y demás — está incluido desde el plan Ultra.",
    "ob.prov.inc.byokUpgrade": "Mejorar tu plan",
    "ob.prov.inc.byokHead": "Usa tus propias claves",
    "ob.prov.inc.byokLead":
      "Añade una clave de proveedor si quieres que ciertos modelos se cobren a tu propia cuenta. Tus Créditos Olano siguen cubriendo todo lo de arriba igualmente.",

    // Forma C: la tarjeta de créditos Olano en una instalación autoalojada
    "ob.prov.olano.title": "Funcionar con créditos Olano",
    "ob.prov.olano.titleDone": "Créditos Olano conectados",
    "ob.prov.olano.lead":
      "Sin cuentas de proveedores: crea una clave API en cloud.olano.ai, compra créditos allí y pega la clave aquí. Tus agentes pasan a los modelos gestionados de Olano, con generación de imágenes, visión, voz y búsqueda web incluidas.",
    "ob.prov.olano.done": "Tus agentes funcionan con créditos Olano. Todo lo de abajo está incluido.",
    "ob.prov.olano.zero": "Esta cuenta aún no tiene créditos, así que los agentes en los niveles Olano no podrán responder hasta que compres algunos en cloud.olano.ai (Facturación). Puedes terminar la configuración ahora y comprar después.",
    "ob.prov.olano.byokHead": "O trae tus propias claves",
    "ob.prov.olano.byokLead":
      "¿Ya tienes una cuenta de proveedor? Pega su clave API en su lugar; se guarda cifrada en la bóveda. Puedes hacer ambas cosas, ahora o más tarde.",

    "ob.unlock.title": "El vault está bloqueado",
    "ob.unlock.lead": "Introduce la frase del vault para guardar este secreto.",
    "ob.unlock.field": "Frase del vault",
    "ob.unlock.submit": "Desbloquear",
    "ob.unlock.err": "Frase incorrecta.",


    // rail step labels (new panels)
    "ob.nav.profile": "Tu perfil",
    "ob.nav.agentfather": "Conoce a Agent Father",
    "ob.welcome.lead":
      "Dos minutos: haz tuya esta cuenta, confirma que tu IA está lista y conoce a Agent Father, el agente que construye el resto de tu equipo.",

    // your profile
    "ob.profile.title": "Haz tuya esta cuenta",
    "ob.profile.lead":
      "Has iniciado sesión con la cuenta con la que se creó esta instancia ({email}): tu correo, una contraseña temporal. Ponle tu nombre y una contraseña propia, y define la frase que protege tu bóveda.",
    "ob.profile.leadCreate":
      "Este panel aún no tiene inicio de sesión: cualquiera que llegue a él tiene control total. Crea la cuenta de administrador que lo posee y define la frase que protege tu bóveda; a partir de entonces el panel pedirá este acceso.",
    "ob.profile.leadVaultOnly": "Tu cuenta ya es tuya. Solo falta una cosa: la frase que protege tu bóveda.",
    "ob.profile.accountHead": "Tu cuenta",
    "ob.profile.vaultHead": "Frase de la bóveda",
    "ob.profile.name": "Tu nombre",
    "ob.profile.namePlaceholder": "Alex Rivera",
    "ob.profile.nameHint":
      "Se muestra junto a todo lo que haces en el panel y es como te llaman tus agentes. Texto libre, p. ej. “Alex Rivera”; puedes cambiarlo después desde tu perfil. Tu identificador de abajo se genera a partir de él.",
    "ob.profile.handle": "Identificador",
    "ob.profile.handlePending": "escribe tu nombre para verlo",
    "ob.profile.handleHint":
      "Tu @identificador permanente: minúsculas, dígitos y guiones, generado a partir de tu nombre. Los agentes y el gestor de incidencias te nombran con él. No se puede editar, ni aquí ni después; solo el nombre. Se añade “-2” únicamente si otra cuenta ya lo usa.",
    "ob.profile.email": "Tu correo",
    "ob.profile.emailHint":
      "La dirección con la que iniciarás sesión desde cualquier navegador o dispositivo: un correo válido como tu@ejemplo.com. Olano no le envía correo; es tu nombre de usuario.",
    "ob.profile.password": "Nueva contraseña",
    "ob.profile.confirm": "Confirmar contraseña",
    "ob.profile.passwordHint":
      "Sustituye la contraseña temporal de tu correo de bienvenida. Al menos 8 caracteres y ambos campos iguales. Cualquier otro navegador que siga conectado con la temporal se desconecta al guardar.",
    "ob.profile.passwordCreateHint": "Al menos 8 caracteres y ambos campos iguales. Iniciarás sesión con tu correo y esta contraseña desde cualquier navegador o dispositivo.",
    "ob.profile.passphrase": "Frase de la bóveda",
    "ob.profile.passphraseConfirm": "Confirmar frase de la bóveda",
    "ob.profile.passphraseHint":
      "Cifra cada clave de API y secreto que usan tus agentes. Al menos 8 caracteres, ambos campos iguales; que sea distinta de tu contraseña de acceso. Una instancia gestionada sigue desbloqueando la bóveda sola tras reiniciar con la nueva frase; una autoalojada puede pedírtela.",
    "ob.profile.submit": "Guardar y continuar",
    "ob.profile.accountDone": "Sesión iniciada como {name} (@{handle}).",
    "ob.profile.accountDoneGeneric": "Tu cuenta ya es tuya: has iniciado sesión con ella.",
    "ob.profile.createdDone": "Cuenta de administrador creada: has iniciado sesión como {email}.",
    "ob.profile.vaultDone": "Frase de la bóveda definida.",
    "ob.profile.err.name": "Escribe tu nombre.",
    "ob.profile.err.email": "Escribe el correo con el que iniciarás sesión.",
    "ob.profile.err.short": "Usa al menos 8 caracteres para la contraseña.",
    "ob.profile.err.mismatch": "Las contraseñas no coinciden.",
    "ob.profile.err.exists": "Ya existe una cuenta en esta máquina. Inicia sesión con ella y continúa la configuración.",
    "ob.profile.err.generic": "No se pudo guardar tu cuenta.",

    // meet Agent Father
    "ob.father.title": "Conoce a Agent Father",
    "ob.father.lead":
      "Agent Father es el agente que construye y dirige el resto de tu equipo. No configuras Olano a mano: le dices a Agent Father lo que necesitas, en lenguaje natural, y apruebas lo que propone.",
    "ob.father.cap.create.t": "Crea agentes",
    "ob.father.cap.create.d": "Describe un rol —“un agente de soporte para mi tienda Shopify”— y construye el agente: prompt, herramientas, memoria.",
    "ob.father.cap.reconfigure.t": "Los reconfigura",
    "ob.father.cap.reconfigure.d": "Cambia un modelo, herramienta, prompt o subagente de cualquier agente cuando se lo pides.",
    "ob.father.cap.connect.t": "Conecta canales y servicios",
    "ob.father.cap.connect.d": "Telegram, WhatsApp, Slack, correo, tu calendario y decenas de integraciones: te envía un enlace cuando hace falta iniciar sesión.",
    "ob.father.cap.people.t": "Invita a tu equipo",
    "ob.father.cap.people.d": "Añade compañeros, decide con qué agentes pueden hablar y define sus permisos.",
    "ob.father.cap.diagnose.t": "Mantiene todo en marcha",
    "ob.father.cap.diagnose.d": "Diagnostica un agente atascado, lo reinicia, ajusta la configuración del despliegue.",
    "ob.father.cap.approve.t": "Pregunta antes de actuar",
    "ob.father.cap.approve.d": "Todo lo que cambie tu despliegue espera tu tarjeta de aprobación en el chat.",
    "ob.father.installing": "Poniendo a Agent Father en línea…",
    "ob.father.ready": "Agent Father está en línea y te espera.",
    "ob.father.unavailable": "Agent Father no está incluido en el plan de este despliegue.",
    "ob.father.err": "No se pudo poner a Agent Father en línea.",
    "ob.father.retry": "Reintentar",
    "ob.father.meet": "Conoce a Agent Father",
    "ob.father.goDashboard": "Ir al panel",
    "ob.father.waking":
      "Al continuar, Agent Father se despierta y saluda en el chat: normalmente en segundos, hasta un minuto en una máquina recién creada.",
    "ob.father.try.h": "Buenas primeras preguntas",
    "ob.father.try.1": "Crea un agente que responda los correos de mis clientes.",
    "ob.father.try.2": "¿Qué proveedores y conectores puedo usar?",
    "ob.father.try.3": "Actualiza el agente de ventas para que también gestione leads.",
    "ob.father.tell.h": "Cuéntale sobre ti: construye mejor con contexto",
    "ob.father.tell.1": "Si es para uso personal o para un negocio.",
    "ob.father.tell.2": "A qué se dedica tu negocio, o qué te interesa.",
    "ob.father.tell.3": "Los sistemas que ya usas: CRM, correo, mensajería, hojas de cálculo.",
    "ob.father.tell.4": "Las tareas repetitivas de cada día o cada semana que te gustaría delegar.",
    "ob.finish.err": "No se pudo terminar la configuración. Comprueba que el runtime está en marcha y vuelve a intentarlo.",
  },
};
