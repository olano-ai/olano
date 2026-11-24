import type { LocalDict } from "../../core/i18n";

// The "New agent" dialog's own strings. They lived in the simple shell's
// dictionary until the dashboard's sidebar grew the same dialog; one copy
// here, and the simple shell spreads it back into its own dictionary so the
// two shells can never word the same question differently.
//
// Every control carries its own explanation (the `*Help` keys): what it
// changes, the format it accepts with an example, what happens when it is left
// empty, and what each choice means. A field without one is a defect.
export const NEWBOT_STR: LocalDict = {
  en: {
    "newbot.title": "New agent",
    "newbot.subtitle": "Give it a name and pick what it should be good at.",
    "newbot.name": "Name",
    "newbot.namePlaceholder": "Hillary",
    "newbot.nameRequired": "Give the agent a name first.",
    "newbot.handle": "Handle",
    "newbot.handlePending": "generated from the name once you type one",
    "newbot.handlePendingArchetype": "{slug} — the template's own, until you type a name",
    "newbot.handleHelp":
      "The agent's permanent identifier, generated from its name (lowercase, e.g. `hillary` from `Hillary`). It is how other agents address this one and where its files live, and it can never be changed — so put anything you may want to change later in the name. If it is already taken, a number is added (`hillary-2`).",
    "newbot.nameHelp":
      "Lowercase letters, numbers, dashes and underscores only, starting with a letter or a number — e.g. sales-helper. This name is permanent, and it is how you and other agents refer to this one.",
    "newbot.kind": "What should it be good at?",
    "newbot.kindHelp":
      "Pick a ready-made template, or start from scratch. “Start from scratch” creates a general-purpose agent you describe yourself. An “Expert” arrives already set up for one job (its description says which). A “Team” is a lead agent that has its own specialists working under it — pick one when the job has several parts. Everything else — which model it uses, what it may do — is chosen for you and can be changed later by whoever owns the agent.",
    "newbot.blank": "Start from scratch",
    "newbot.blankDesc": "A general-purpose agent. You describe what it should do.",
    "newbot.searchPlaceholder": "Search templates…",
    "newbot.searchLabel": "Search templates",
    "newbot.experts": "Experts",
    "newbot.teams": "Teams",
    "newbot.loading": "Loading templates…",
    "newbot.catalogFailed": "Couldn't load the templates.",
    "newbot.catalogEmpty": "No templates are available on this deployment.",
    "newbot.noMatches": "No templates match your search.",
    "newbot.retry": "Try again",
    "newbot.description": "What should it do?",
    "newbot.descriptionPlaceholder": "Answers questions about our product and drafts replies to customers.",
    "newbot.descriptionHelp":
      "One or two sentences describing the agent's job. It shows in your agent list and helps the agent understand what it is for. Leave it empty to add one later from the agent's profile. (Templates come with their own description, so this only appears when you start from scratch.)",
    "newbot.model": "Model",
    "newbot.modelHelp":
      "Which model answers for this agent. Only models this deployment can run right now are listed: the deployment defaults (Default · Turbo / Pro / Max — what each points at is set in Config → Default models), the Olano managed tiers (billed in credits, no key needed) and any provider you have connected with your own key. Default · Turbo, the fast everyday tier, is pre-selected whenever this deployment offers it (otherwise the first model that can run is). With a template picked, “Template's own model” keeps the model the template was designed for. The model can be changed at any time from the agent's page.",
    "newbot.modelTemplateOwn": "Template's own model",
    "newbot.modelLoading": "Loading models…",
    "newbot.modelNone": "No connected models — the deployment default will be used",
    "newbot.create": "Create agent",
    "newbot.creating": "Creating…",
    "newbot.created": "{name} is ready.",
    "newbot.failed": "Couldn't create the agent.",
    "newbot.cancel": "Cancel",
  },
  zh: {
    "newbot.title": "新建智能体",
    "newbot.subtitle": "为它起个名字，并选择它擅长什么。",
    "newbot.name": "名称",
    "newbot.namePlaceholder": "Hillary",
    "newbot.nameRequired": "请先为智能体起一个名称。",
    "newbot.handle": "标识符",
    "newbot.handlePending": "输入名称后自动生成",
    "newbot.handlePendingArchetype": "{slug}——在你输入名称之前，使用模板自带的标识符",
    "newbot.handleHelp":
      "智能体的永久标识符，由名称自动生成（小写，例如由 `Hillary` 生成 `hillary`）。其他智能体用它来称呼这个智能体，它的文件也存放于此，且永远无法更改——因此以后可能想改的内容都请填在名称里。若已被占用，会自动追加数字（`hillary-2`）。",
    "newbot.nameHelp":
      "只能使用小写字母、数字、连字符和下划线，且以字母或数字开头——例如 sales-helper。该名称是永久的，你和其他智能体都用它来指代这个智能体。",
    "newbot.kind": "它应该擅长什么？",
    "newbot.kindHelp":
      "选择一个现成模板，或从零开始。“从零开始”会创建一个由你自己描述用途的通用智能体。“专家”预先针对某一项工作配置好（其简介会说明是哪一项）。“团队”是一个带领若干专家的主管智能体——工作包含多个部分时选它。其余设置（使用哪个模型、允许做什么）都已为你选好，之后可由智能体拥有者修改。",
    "newbot.blank": "从零开始",
    "newbot.blankDesc": "通用智能体，由你描述它该做什么。",
    "newbot.searchPlaceholder": "搜索模板…",
    "newbot.searchLabel": "搜索模板",
    "newbot.experts": "专家",
    "newbot.teams": "团队",
    "newbot.loading": "正在加载模板…",
    "newbot.catalogFailed": "无法加载模板。",
    "newbot.catalogEmpty": "此部署没有可用模板。",
    "newbot.noMatches": "没有匹配的模板。",
    "newbot.retry": "重试",
    "newbot.description": "它该做什么？",
    "newbot.descriptionPlaceholder": "回答产品相关问题并起草给客户的回复。",
    "newbot.descriptionHelp":
      "用一两句话描述智能体的工作。它会显示在你的智能体列表中，也帮助智能体理解自己的用途。留空可稍后在智能体资料中补充。（模板自带简介，因此仅在从零开始时显示此项。）",
    "newbot.model": "模型",
    "newbot.modelHelp":
      "为此智能体作答的模型。只列出此部署当前可以运行的模型：部署默认（默认 · Turbo / Pro / Max——各自指向哪个模型在 配置 → 默认模型 中设置）、Olano 托管档位（按积分计费，无需密钥）以及你用自己的密钥接入的服务商。只要此部署提供“默认 · Turbo”（快速的日常档位），就会预选它；否则预选第一个可运行的模型。选了模板时，“模板自带模型”会保留模板设计时使用的模型。之后随时可在智能体页面更改。",
    "newbot.modelTemplateOwn": "模板自带模型",
    "newbot.modelLoading": "正在加载模型…",
    "newbot.modelNone": "没有已接入的模型——将使用部署默认值",
    "newbot.create": "创建智能体",
    "newbot.creating": "创建中…",
    "newbot.created": "{name} 已就绪。",
    "newbot.failed": "无法创建智能体。",
    "newbot.cancel": "取消",
  },
  es: {
    "newbot.title": "Nuevo agente",
    "newbot.subtitle": "Ponle un nombre y elige en qué debe ser bueno.",
    "newbot.name": "Nombre",
    "newbot.namePlaceholder": "Hillary",
    "newbot.nameRequired": "Ponle un nombre al agente primero.",
    "newbot.handle": "Identificador",
    "newbot.handlePending": "se genera a partir del nombre en cuanto escribas uno",
    "newbot.handlePendingArchetype": "{slug} — el de la plantilla, hasta que escribas un nombre",
    "newbot.handleHelp":
      "El identificador permanente del agente, generado a partir de su nombre (en minúsculas, p. ej. `hillary` a partir de `Hillary`). Es como lo nombran otros agentes y donde viven sus archivos, y no se puede cambiar nunca — así que pon en el nombre todo lo que quizá quieras cambiar después. Si ya está en uso se añade un número (`hillary-2`).",
    "newbot.nameHelp":
      "Solo minúsculas, números, guiones y guiones bajos, empezando por una letra o un número — por ejemplo ventas-ayuda. Este nombre es permanente y es como tú y otros agentes se refieren a él.",
    "newbot.kind": "¿En qué debe ser bueno?",
    "newbot.kindHelp":
      "Elige una plantilla lista o empieza desde cero. «Empezar desde cero» crea un agente de uso general que describes tú. Un «Experto» viene ya preparado para un trabajo concreto (su descripción dice cuál). Un «Equipo» es un agente responsable con sus propios especialistas debajo — elígelo cuando el trabajo tenga varias partes. Todo lo demás — qué modelo usa, qué puede hacer — se elige por ti y quien posea el agente puede cambiarlo después.",
    "newbot.blank": "Empezar desde cero",
    "newbot.blankDesc": "Un agente de uso general. Tú describes qué debe hacer.",
    "newbot.searchPlaceholder": "Buscar plantillas…",
    "newbot.searchLabel": "Buscar plantillas",
    "newbot.experts": "Expertos",
    "newbot.teams": "Equipos",
    "newbot.loading": "Cargando plantillas…",
    "newbot.catalogFailed": "No se pudieron cargar las plantillas.",
    "newbot.catalogEmpty": "Esta instalación no tiene plantillas disponibles.",
    "newbot.noMatches": "Ninguna plantilla coincide con tu búsqueda.",
    "newbot.retry": "Reintentar",
    "newbot.description": "¿Qué debe hacer?",
    "newbot.descriptionPlaceholder": "Responde preguntas sobre nuestro producto y redacta respuestas a clientes.",
    "newbot.descriptionHelp":
      "Una o dos frases que describan el trabajo del agente. Aparece en tu lista de agentes y ayuda al propio agente a entender para qué está. Déjalo vacío para añadirlo luego desde su perfil. (Las plantillas traen su propia descripción, por eso esto solo aparece al empezar desde cero.)",
    "newbot.model": "Modelo",
    "newbot.modelHelp":
      "El modelo que responde por este agente. Solo se listan los modelos que esta instalación puede ejecutar ahora mismo: los predeterminados del despliegue (Default · Turbo / Pro / Max; a qué apunta cada uno se fija en Configuración → Modelos predeterminados), los niveles gestionados de Olano (facturados en créditos, sin clave) y cualquier proveedor que hayas conectado con tu propia clave. Viene preseleccionado Default · Turbo, el nivel rápido para el día a día, siempre que esta instalación lo ofrezca (si no, el primer modelo que pueda ejecutarse). Con una plantilla elegida, «Modelo propio de la plantilla» conserva el modelo para el que se diseñó. Se puede cambiar en cualquier momento desde la página del agente.",
    "newbot.modelTemplateOwn": "Modelo propio de la plantilla",
    "newbot.modelLoading": "Cargando modelos…",
    "newbot.modelNone": "No hay modelos conectados: se usará el predeterminado del despliegue",
    "newbot.create": "Crear agente",
    "newbot.creating": "Creando…",
    "newbot.created": "{name} está listo.",
    "newbot.failed": "No se pudo crear el agente.",
    "newbot.cancel": "Cancelar",
  },
};
