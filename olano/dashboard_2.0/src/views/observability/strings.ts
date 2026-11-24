import type { LocalDict } from "../../core/i18n";

// Langfuse tracing: the Config → Observability section, the agent's
// Observability card (Overview tab) and the "Open in Langfuse" links.
export const STR: LocalDict = {
  en: {
    "ob.section": "Observability (Langfuse)",
    "ob.section.hint":
      "Send each agent turn to a Langfuse project as one trace: the turn, every model call and every tool call under it, with timings, token counts and credits, grouped by conversation. Off by default. Langfuse runs outside this deployment (Langfuse Cloud or your own server); nothing extra is installed here. Agents cannot change these settings: they decide whether conversations leave this deployment.",
    "ob.enabled": "Trace agent turns",
    "ob.enabled.hint":
      "On: every agent's turns are traced, unless an agent's own Observability setting says otherwise. Off (default): no agent is traced unless its own setting turns tracing on. Tracing also needs Langfuse keys below; without them nothing is sent, whatever this switch says. Applies from the next turn, no restart.",
    "ob.host": "Langfuse address",
    "ob.host.hint":
      "The Langfuse server the traces go to: https://cloud.langfuse.com (Langfuse Cloud, EU), https://us.cloud.langfuse.com (US), https://jp.cloud.langfuse.com (Japan), or your own server such as https://langfuse.example.com. Langfuse Cloud keys only work in the region where they were created. Leave empty to use the address saved with the keys, or the LANGFUSE_BASE_URL environment variable of this server, then the EU region. Must start with https:// (plain http:// is accepted only for a server on this machine).",
    "ob.capture": "What each trace contains",
    "ob.capture.hint":
      "Metadata (default): the structure of each turn, timings, token counts, credits, the model and tool names, and whether a step failed; no message, tool argument, tool result or error text, and the person's id is replaced with a stable pseudonym. Full: also the messages, tool arguments and results and error text, so a trace shows exactly what the agent saw and said. In both modes every secret the agent could see (vault values, keys in its settings, text shaped like a credential) is replaced with [secret], images and files are never sent, and a managed Olano model is shown by its tier name. An agent can override this in its own Observability setting.",
    "ob.capture.metadata": "Metadata only (default)",
    "ob.capture.full": "Full content, secrets masked",
    "ob.fullWarning":
      "Full capture stores conversations, tool results and the contents of files the agents read in Langfuse. Anyone with access to that Langfuse project can read them. Use it on a project only the right people can open.",
    "ob.sample": "Share of turns traced",
    "ob.sample.hint":
      "From 0 (none) to 1 (every turn, the default). 0.25 traces about one turn in four. Each turn is traced whole or not at all, and the choice is made from the turn itself, so a turn is never half-traced. An agent can set its own rate.",
    "ob.environment": "Environment name",
    "ob.environment.hint":
      "An optional label shown on every trace in Langfuse, to tell deployments apart in one project: for example production or staging. Lowercase letters, digits, - and _, at most 40 characters, not starting with langfuse. Leave empty to send none.",
    "ob.background": "Trace background work",
    "ob.background.hint":
      "On (default): Cortex runs (background thinking, idea work, memory upkeep) are traced too. Off: only conversations and scheduled jobs are traced, which keeps a busy Cortex from filling the project.",
    "ob.keys": "Langfuse keys",
    "ob.keys.hint":
      "The public key (pk-lf-…) and secret key (sk-lf-…) of the Langfuse project, from Langfuse → Settings → API keys. They are kept in the vault, hidden from agents and from the Vault page, and never shown again here. If none are saved, this server's LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY environment variables are used. Saving keys does not turn tracing on.",
    "ob.publicKey": "Public key",
    "ob.secretKey": "Secret key",
    "ob.keysSource.vault": "Keys saved here are in use ({key}).",
    "ob.keysSource.env": "This server's environment variables are in use ({key}).",
    "ob.keysSource.none": "No keys yet: nothing is sent until keys are saved here or set on the server.",
    "ob.saveKeys": "Save keys",
    "ob.clearKeys": "Remove saved keys",
    "ob.keysSaved": "Langfuse keys saved",
    "ob.keysCleared": "Saved Langfuse keys removed",
    "ob.test": "Test connection",
    "ob.testing": "Testing…",
    "ob.test.hint":
      "Checks the keys against the Langfuse address and remembers the project, so conversations can link to their traces. Uses the keys typed above when both are filled in, otherwise the saved ones.",
    "ob.test.ok": "Connected to the project “{project}”.",
    "ob.test.fail": "Not connected: {message}",
    "ob.openProject": "Open the project in Langfuse",
    "ob.status": "Status",
    "ob.status.none": "Nothing has been sent since the last restart.",
    "ob.status.ok": "{spans} steps sent since the last restart.",
    "ob.status.error": "Last problem ({when}): {error}",
    "ob.overrides": "Agents with their own setting",
    "ob.overrides.none": "None: every agent follows this setting.",
    "ob.override.on": "traced",
    "ob.override.off": "not traced",
    "ob.unsavedNote": "The switches above are saved with the page's Save button; keys and the test act at once.",
    "ob.agent.title": "Observability (Langfuse)",
    "ob.agent.intro":
      "Whether this agent's turns are traced to Langfuse, and how much each trace contains. Empty follows Config → Observability. Only an admin can change this.",
    "ob.agent.enabled": "Trace this agent",
    "ob.agent.enabled.hint":
      "Follow Config (default): use the deployment's switch. On: trace this agent even if the deployment does not. Off: never trace this agent, even if the deployment does. Tracing still needs Langfuse keys.",
    "ob.agent.inherit": "Follow Config",
    "ob.agent.on": "On",
    "ob.agent.off": "Off",
    "ob.agent.capture.hint":
      "Follow Config (default): use the deployment's choice. Metadata: structure, timings, token counts and names only, no text. Full: also messages, tool arguments, results and error text, with secrets masked. Images and files are never sent.",
    "ob.agent.sample.hint":
      "A number from 0 to 1, for example 0.2 for one turn in five. Leave empty to follow Config.",
    "ob.agent.effective": "In effect now: {state}",
    "ob.agent.effective.on": "traced ({capture}, {rate} of turns), keys from {source}",
    "ob.agent.effective.off": "not traced",
    "ob.agent.source.vault": "Config",
    "ob.agent.source.agentVault": "this agent's own keys",
    "ob.agent.source.env": "the server's environment",
    "ob.agent.save": "Save",
    "ob.agent.saved": "Observability setting saved",
    "ob.link.trace": "Langfuse",
    "ob.link.traceTitle": "Open this reply's trace in Langfuse",
    "ob.link.session": "Open in Langfuse",
    "ob.link.sessionTitle": "Open this conversation's traces in Langfuse",
  },
  zh: {
    "ob.section": "可观测性（Langfuse）",
    "ob.section.hint":
      "把每个智能体回合作为一条追踪发送到 Langfuse 项目：回合本身，以及其中每次模型调用和工具调用，附带耗时、令牌数和积分，按对话分组。默认关闭。Langfuse 运行在本部署之外（Langfuse Cloud 或你自己的服务器），这里不会额外安装任何东西。智能体无法更改这些设置：它们决定对话是否离开本部署。",
    "ob.enabled": "追踪智能体回合",
    "ob.enabled.hint":
      "开启：追踪所有智能体的回合，除非某个智能体自己的可观测性设置另有规定。关闭（默认）：除非智能体自己的设置开启追踪，否则不追踪任何智能体。追踪还需要下方的 Langfuse 密钥；没有密钥时不会发送任何内容。从下一个回合起生效，无需重启。",
    "ob.host": "Langfuse 地址",
    "ob.host.hint":
      "追踪发送到的 Langfuse 服务器：https://cloud.langfuse.com（欧盟）、https://us.cloud.langfuse.com（美国）、https://jp.cloud.langfuse.com（日本），或你自己的服务器，例如 https://langfuse.example.com。Langfuse Cloud 的密钥只在创建它们的区域有效。留空则使用与密钥一起保存的地址，或本服务器的 LANGFUSE_BASE_URL 环境变量，再不行则使用欧盟区域。必须以 https:// 开头（仅本机服务器可用 http://）。",
    "ob.capture": "每条追踪包含的内容",
    "ob.capture.hint":
      "仅元数据（默认）：回合结构、耗时、令牌数、积分、模型和工具名称，以及某一步是否失败；不含消息、工具参数、工具结果或错误文本，用户 ID 会替换为稳定的化名。完整：还包括消息、工具参数和结果以及错误文本，追踪会显示智能体看到和说出的全部内容。两种模式下，智能体可见的所有机密（保险库值、设置中的密钥、形似凭据的文本）都会替换为 [secret]，图片和文件绝不会发送，托管的 Olano 模型以其层级名称显示。智能体可以在自己的可观测性设置中覆盖此项。",
    "ob.capture.metadata": "仅元数据（默认）",
    "ob.capture.full": "完整内容，屏蔽机密",
    "ob.fullWarning":
      "完整采集会把对话、工具结果以及智能体读取的文件内容存储到 Langfuse。任何能访问该 Langfuse 项目的人都能读取。请只在合适的人才能打开的项目中使用。",
    "ob.sample": "追踪的回合比例",
    "ob.sample.hint":
      "从 0（不追踪）到 1（每个回合，默认）。0.25 约追踪四分之一的回合。每个回合要么完整追踪，要么完全不追踪。智能体可以设置自己的比例。",
    "ob.environment": "环境名称",
    "ob.environment.hint":
      "可选标签，显示在 Langfuse 的每条追踪上，用于在同一项目中区分部署，例如 production 或 staging。小写字母、数字、- 和 _，最多 40 个字符，不能以 langfuse 开头。留空则不发送。",
    "ob.background": "追踪后台工作",
    "ob.background.hint":
      "开启（默认）：Cortex 运行（后台思考、创意工作、记忆整理）也会被追踪。关闭：只追踪对话和定时任务，避免繁忙的 Cortex 填满项目。",
    "ob.keys": "Langfuse 密钥",
    "ob.keys.hint":
      "Langfuse 项目的公钥（pk-lf-…）和私钥（sk-lf-…），在 Langfuse → Settings → API keys 中获取。它们保存在保险库中，对智能体和保险库页面隐藏，此处也不会再次显示。如果未保存，则使用本服务器的 LANGFUSE_PUBLIC_KEY 和 LANGFUSE_SECRET_KEY 环境变量。保存密钥不会开启追踪。",
    "ob.publicKey": "公钥",
    "ob.secretKey": "私钥",
    "ob.keysSource.vault": "正在使用此处保存的密钥（{key}）。",
    "ob.keysSource.env": "正在使用本服务器的环境变量（{key}）。",
    "ob.keysSource.none": "尚无密钥：在此保存或在服务器上设置密钥之前不会发送任何内容。",
    "ob.saveKeys": "保存密钥",
    "ob.clearKeys": "删除已保存的密钥",
    "ob.keysSaved": "Langfuse 密钥已保存",
    "ob.keysCleared": "已删除保存的 Langfuse 密钥",
    "ob.test": "测试连接",
    "ob.testing": "测试中…",
    "ob.test.hint":
      "用 Langfuse 地址验证密钥并记住项目，以便对话可以链接到它们的追踪。两个密钥都已填写时使用上面输入的密钥，否则使用已保存的密钥。",
    "ob.test.ok": "已连接到项目“{project}”。",
    "ob.test.fail": "未连接：{message}",
    "ob.openProject": "在 Langfuse 中打开项目",
    "ob.status": "状态",
    "ob.status.none": "自上次重启以来尚未发送任何内容。",
    "ob.status.ok": "自上次重启以来已发送 {spans} 个步骤。",
    "ob.status.error": "最近的问题（{when}）：{error}",
    "ob.overrides": "有自己设置的智能体",
    "ob.overrides.none": "无：所有智能体都遵循此设置。",
    "ob.override.on": "追踪",
    "ob.override.off": "不追踪",
    "ob.unsavedNote": "上面的开关通过页面的保存按钮保存；密钥和测试会立即生效。",
    "ob.agent.title": "可观测性（Langfuse）",
    "ob.agent.intro": "此智能体的回合是否追踪到 Langfuse，以及每条追踪包含多少内容。留空则遵循 配置 → 可观测性。只有管理员可以更改。",
    "ob.agent.enabled": "追踪此智能体",
    "ob.agent.enabled.hint":
      "遵循配置（默认）：使用部署的开关。开启：即使部署未开启也追踪此智能体。关闭：即使部署开启也从不追踪此智能体。追踪仍需要 Langfuse 密钥。",
    "ob.agent.inherit": "遵循配置",
    "ob.agent.on": "开启",
    "ob.agent.off": "关闭",
    "ob.agent.capture.hint":
      "遵循配置（默认）：使用部署的选择。元数据：仅结构、耗时、令牌数和名称，不含文本。完整：还包括消息、工具参数、结果和错误文本，并屏蔽机密。图片和文件绝不会发送。",
    "ob.agent.sample.hint": "0 到 1 之间的数字，例如 0.2 表示五个回合追踪一个。留空则遵循配置。",
    "ob.agent.effective": "当前生效：{state}",
    "ob.agent.effective.on": "追踪（{capture}，{rate} 的回合），密钥来自{source}",
    "ob.agent.effective.off": "不追踪",
    "ob.agent.source.vault": "配置",
    "ob.agent.source.agentVault": "此智能体自己的密钥",
    "ob.agent.source.env": "服务器环境变量",
    "ob.agent.save": "保存",
    "ob.agent.saved": "可观测性设置已保存",
    "ob.link.trace": "Langfuse",
    "ob.link.traceTitle": "在 Langfuse 中打开此回复的追踪",
    "ob.link.session": "在 Langfuse 中打开",
    "ob.link.sessionTitle": "在 Langfuse 中打开此对话的追踪",
  },
  es: {
    "ob.section": "Observabilidad (Langfuse)",
    "ob.section.hint":
      "Envía cada turno de un agente a un proyecto de Langfuse como una traza: el turno, cada llamada al modelo y cada llamada a herramienta, con tiempos, tokens y créditos, agrupados por conversación. Desactivado por defecto. Langfuse se ejecuta fuera de este despliegue (Langfuse Cloud o tu propio servidor); aquí no se instala nada. Los agentes no pueden cambiar estos ajustes: deciden si las conversaciones salen de este despliegue.",
    "ob.enabled": "Trazar los turnos de los agentes",
    "ob.enabled.hint":
      "Activado: se trazan los turnos de todos los agentes, salvo que el ajuste de Observabilidad del propio agente diga otra cosa. Desactivado (por defecto): no se traza ningún agente salvo que su propio ajuste lo active. Además hacen falta las claves de Langfuse de abajo; sin ellas no se envía nada. Se aplica desde el siguiente turno, sin reiniciar.",
    "ob.host": "Dirección de Langfuse",
    "ob.host.hint":
      "El servidor de Langfuse al que van las trazas: https://cloud.langfuse.com (UE), https://us.cloud.langfuse.com (EE. UU.), https://jp.cloud.langfuse.com (Japón) o tu propio servidor, por ejemplo https://langfuse.example.com. Las claves de Langfuse Cloud solo funcionan en la región donde se crearon. Vacío: se usa la dirección guardada con las claves, o la variable de entorno LANGFUSE_BASE_URL de este servidor, y si no, la región de la UE. Debe empezar por https:// (http:// solo para un servidor en esta máquina).",
    "ob.capture": "Qué contiene cada traza",
    "ob.capture.hint":
      "Solo metadatos (por defecto): la estructura del turno, tiempos, tokens, créditos, los nombres de modelo y herramientas y si un paso falló; ningún texto de mensajes, argumentos o resultados de herramientas ni de errores, y el identificador de la persona se sustituye por un seudónimo estable. Completo: también los mensajes, los argumentos y resultados de herramientas y el texto de los errores. En ambos modos todo secreto que el agente pudiera ver (valores del almacén, claves de sus ajustes, texto con forma de credencial) se sustituye por [secret], nunca se envían imágenes ni archivos y un modelo gestionado de Olano aparece con el nombre de su nivel. Un agente puede cambiarlo en su propio ajuste.",
    "ob.capture.metadata": "Solo metadatos (por defecto)",
    "ob.capture.full": "Contenido completo, secretos ocultos",
    "ob.fullWarning":
      "La captura completa guarda en Langfuse las conversaciones, los resultados de herramientas y el contenido de los archivos que leen los agentes. Cualquiera con acceso a ese proyecto puede leerlos. Úsala solo en un proyecto que abran las personas adecuadas.",
    "ob.sample": "Proporción de turnos trazados",
    "ob.sample.hint":
      "De 0 (ninguno) a 1 (todos, por defecto). 0.25 traza aproximadamente uno de cada cuatro. Cada turno se traza entero o no se traza. Un agente puede fijar su propia proporción.",
    "ob.environment": "Nombre del entorno",
    "ob.environment.hint":
      "Etiqueta opcional que aparece en cada traza para distinguir despliegues en un mismo proyecto, por ejemplo production o staging. Minúsculas, dígitos, - y _, como máximo 40 caracteres, sin empezar por langfuse. Vacío: no se envía ninguna.",
    "ob.background": "Trazar el trabajo en segundo plano",
    "ob.background.hint":
      "Activado (por defecto): también se trazan las ejecuciones de Cortex (pensamiento en segundo plano, ideas, mantenimiento de la memoria). Desactivado: solo conversaciones y tareas programadas, para que un Cortex muy activo no llene el proyecto.",
    "ob.keys": "Claves de Langfuse",
    "ob.keys.hint":
      "La clave pública (pk-lf-…) y la secreta (sk-lf-…) del proyecto, en Langfuse → Settings → API keys. Se guardan en el almacén, ocultas para los agentes y para la página del almacén, y no se vuelven a mostrar aquí. Si no hay ninguna guardada, se usan las variables de entorno LANGFUSE_PUBLIC_KEY y LANGFUSE_SECRET_KEY de este servidor. Guardar las claves no activa el trazado.",
    "ob.publicKey": "Clave pública",
    "ob.secretKey": "Clave secreta",
    "ob.keysSource.vault": "Se usan las claves guardadas aquí ({key}).",
    "ob.keysSource.env": "Se usan las variables de entorno de este servidor ({key}).",
    "ob.keysSource.none": "Aún no hay claves: no se envía nada hasta guardarlas aquí o definirlas en el servidor.",
    "ob.saveKeys": "Guardar claves",
    "ob.clearKeys": "Quitar las claves guardadas",
    "ob.keysSaved": "Claves de Langfuse guardadas",
    "ob.keysCleared": "Claves de Langfuse quitadas",
    "ob.test": "Probar la conexión",
    "ob.testing": "Probando…",
    "ob.test.hint":
      "Comprueba las claves contra la dirección de Langfuse y recuerda el proyecto, para que las conversaciones enlacen con sus trazas. Usa las claves escritas arriba si están las dos, si no las guardadas.",
    "ob.test.ok": "Conectado al proyecto «{project}».",
    "ob.test.fail": "Sin conexión: {message}",
    "ob.openProject": "Abrir el proyecto en Langfuse",
    "ob.status": "Estado",
    "ob.status.none": "No se ha enviado nada desde el último reinicio.",
    "ob.status.ok": "{spans} pasos enviados desde el último reinicio.",
    "ob.status.error": "Último problema ({when}): {error}",
    "ob.overrides": "Agentes con ajuste propio",
    "ob.overrides.none": "Ninguno: todos los agentes siguen este ajuste.",
    "ob.override.on": "con trazas",
    "ob.override.off": "sin trazas",
    "ob.unsavedNote": "Los ajustes de arriba se guardan con el botón Guardar de la página; las claves y la prueba actúan al momento.",
    "ob.agent.title": "Observabilidad (Langfuse)",
    "ob.agent.intro":
      "Si los turnos de este agente se trazan en Langfuse y cuánto contiene cada traza. Vacío sigue a Configuración → Observabilidad. Solo un administrador puede cambiarlo.",
    "ob.agent.enabled": "Trazar este agente",
    "ob.agent.enabled.hint":
      "Seguir la configuración (por defecto): usa el interruptor del despliegue. Activado: traza este agente aunque el despliegue no lo haga. Desactivado: nunca traza este agente. Siguen haciendo falta claves de Langfuse.",
    "ob.agent.inherit": "Seguir la configuración",
    "ob.agent.on": "Activado",
    "ob.agent.off": "Desactivado",
    "ob.agent.capture.hint":
      "Seguir la configuración (por defecto): la elección del despliegue. Metadatos: solo estructura, tiempos, tokens y nombres, sin texto. Completo: también mensajes, argumentos, resultados y errores, con los secretos ocultos. Nunca se envían imágenes ni archivos.",
    "ob.agent.sample.hint": "Un número de 0 a 1, por ejemplo 0.2 para uno de cada cinco turnos. Vacío sigue a la configuración.",
    "ob.agent.effective": "En vigor ahora: {state}",
    "ob.agent.effective.on": "con trazas ({capture}, {rate} de los turnos), claves de {source}",
    "ob.agent.effective.off": "sin trazas",
    "ob.agent.source.vault": "Configuración",
    "ob.agent.source.agentVault": "las claves propias de este agente",
    "ob.agent.source.env": "el entorno del servidor",
    "ob.agent.save": "Guardar",
    "ob.agent.saved": "Ajuste de observabilidad guardado",
    "ob.link.trace": "Langfuse",
    "ob.link.traceTitle": "Abrir la traza de esta respuesta en Langfuse",
    "ob.link.session": "Abrir en Langfuse",
    "ob.link.sessionTitle": "Abrir las trazas de esta conversación en Langfuse",
  },
};
