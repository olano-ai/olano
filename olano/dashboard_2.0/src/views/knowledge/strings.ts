import type { LocalDict } from "../../core/i18n";

// Per-view translations for the Knowledge base view. Shared keys (nav.*,
// common.*) still resolve through the shared dictionary via useT's fallback.
export const STR: LocalDict = {
  en: {
    "title": "Knowledge",
    "running": "{n} running",
    "agents.none": "No agents",
    "reroute.notice": "{selected} is offline, so knowledge-base calls are being answered by {target}. Start {selected} — or pick it again once it is running — to route them there.",
    "page.title": "Knowledge base",
    "page.subtitle":
      "Vector-indexed sources your agents can semantically search. Routed via the selected running agent's kb_* tools.",

    // tabs
    "tab.search": "Search",
    "tab.sources": "Sources",
    "tab.add": "Add source",

    // search tab
    "search.heading": "Search",
    "search.subtitle": "Semantic search across a store via the running agent's kb_search.",
    "search.placeholder": "Search across the knowledge base…",
    "search.btn": "Search",
    "search.field.store": "Store",
    "search.field.k": "Results (k)",
    "search.field.mode": "Mode",
    "search.mode.similarity": "similarity",
    "search.mode.mmr": "mmr (diverse)",
    "search.searching": "Searching…",
    "search.prompt": "Enter a query above to search the knowledge base.",
    "search.prompt.blocked":
      "Search is unavailable: the selected agent has no kb_search tool. See the notice above.",

    // search results
    "results.title": "Results",
    "results.hit": "hit",
    "results.hits": "hits",
    "results.via": "via {agent}",
    "results.copy": "Copy",
    "results.download": "Download",
    "results.none": "No results found for “{query}”.",

    // toasts (search)
    "toast.copied": "Copied results to clipboard.",
    "toast.copyFailed": "Copy failed.",
    "err.noAgent.search": "No running agent to dispatch the search.",
    "err.search": "Search failed.",

    // missing kb_* tools — the page routes every action through the selected
    // agent's tools, so an agent without them cannot serve this page at all
    "missing.title": "{agent} can't run the knowledge base tools",
    "missing.why":
      "This page has no backend of its own — every search, listing and ingest is a kb_* tool call sent to the selected agent as a chat message. The kb_* tools are not granted by default: an agent has them only when its own tools: list names them. Without them the agent just answers the call in prose, which is not a search result.",
    "missing.list": "Missing on this agent:",
    "missing.fix.add":
      "Add the missing tools under Agents → {agent} → Tools (or the tools: list in its agent.json5), then restart the agent.",
    "missing.fix.global":
      "If a tool is already listed there, check Config → Tools: a home-wide switch removes a tool from every agent at boot without changing any config file.",
    "missing.fix.other":
      "Or pick another agent in the selector above — this page routes to whichever agent is selected, so any agent whose tools include the kb_* names will work.",
    "err.missingTool":
      "{agent} has no {tool} tool. Add it to the agent's tools and restart it, or pick another agent above.",
    "disabled.tool": "The selected agent has no {tool} tool.",

    // sources tab
    "sources.heading": "Indexed sources",
    "sources.refresh": "Refresh",
    "sources.stat": "{store}: {sources} sources · {chunks} chunks",
    "sources.loading": "Loading sources…",
    "sources.empty.title": "No sources indexed",
    "sources.empty.body":
      "Add a URL or file from the Add source tab, or have an agent run kb_add_file / kb_add_url.",
    "col.source": "Source",
    "col.store": "Store",
    "col.type": "Type",
    "col.actions": "Actions",
    "type.url": "url",
    "type.file": "file",
    "sources.remove": "Remove",
    "err.noAgent.list": "No running agent to list the knowledge base.",
    "err.list": "Could not list sources.",

    // add tab
    "add.target.heading": "Target store",
    "add.target.subtitle": "Where URL / file ingestion and sync write to.",
    "add.sync.busy": "Syncing…",
    "add.sync.btn": "Sync store",
    "add.sync.hint": "Re-index new / changed files in the store directory.",
    "add.url.heading": "Ingest URL",
    "add.url.placeholder": "https://example.com/docs",
    "add.url.hint": "Scraped to markdown (Firecrawl if available, else httpx).",
    "add.file.heading": "Ingest file",
    "add.file.placeholder": "report.pdf or /abs/path/to/file.md",
    "add.file.hint":
      "Workspace-relative or absolute path. Text, Markdown, code, JSON, CSV, HTML (PDF/DOCX if extras installed).",
    "add.ingest": "Ingest",
    "add.store.heading": "Create store from directory",
    "add.store.name": "store name (e.g. project_docs)",
    "add.store.dir": "directory (e.g. docs/ or /abs/path)",
    "add.store.glob": "glob (default **/*)",
    "add.store.creating": "Creating…",
    "add.store.create": "Create store",

    // add toasts
    "toast.urlIngested": "URL ingested via {agent}.",
    "toast.fileIngested": "File ingested via {agent}.",
    "toast.storeCreated": "Store '{name}' created.",
    "toast.synced": "Knowledge base synced.",
    "toast.sourceRemoved": "Source removed.",
    "err.noAgent.url": "No running agent to ingest the URL.",
    "err.noAgent.file": "No running agent to ingest the file.",
    "err.noAgent.store": "No running agent to create the store.",
    "err.noAgent.sync": "No running agent to sync.",
    "err.noAgent.delete": "No running agent to delete the source.",
    "err.ingest": "Ingest failed.",
    "err.createStore": "Create store failed.",
    "err.sync": "Sync failed.",
    "err.delete": "Delete failed.",

    // delete modal
    "modal.title": "Remove source?",
    "modal.deindex": "De-index this source from store",
    "modal.disk": "Also delete the file from disk (kb_remove_file)",
    "modal.urlOnly": "URLs are only removed from the index — nothing is deleted from disk.",
    "modal.cancel": "Cancel",
    "modal.removeDelete": "Remove + delete file",
    "modal.removeIndex": "Remove from index",
  },

  zh: {
    "title": "知识库",
    "running": "{n} 个运行中",
    "agents.none": "无智能体",
    "reroute.notice": "{selected} 已离线，知识库调用暂由 {target} 回答。启动 {selected}（或在其运行后重新选择）即可改回由它处理。",
    "page.title": "知识库",
    "page.subtitle": "可供智能体进行语义检索的向量化索引来源。通过所选运行中智能体的 kb_* 工具调度。",

    // tabs
    "tab.search": "搜索",
    "tab.sources": "来源",
    "tab.add": "添加来源",

    // search tab
    "search.heading": "搜索",
    "search.subtitle": "通过运行中智能体的 kb_search 在指定存储库中进行语义检索。",
    "search.placeholder": "在知识库中搜索…",
    "search.btn": "搜索",
    "search.field.store": "存储库",
    "search.field.k": "结果数（k）",
    "search.field.mode": "模式",
    "search.mode.similarity": "相似度",
    "search.mode.mmr": "mmr（多样化）",
    "search.searching": "搜索中…",
    "search.prompt": "在上方输入查询以搜索知识库。",
    "search.prompt.blocked": "无法搜索：所选智能体没有 kb_search 工具。请参见上方提示。",

    // search results
    "results.title": "搜索结果",
    "results.hit": "条结果",
    "results.hits": "条结果",
    "results.via": "经由 {agent}",
    "results.copy": "复制",
    "results.download": "下载",
    "results.none": "未找到与“{query}”相关的结果。",

    // toasts (search)
    "toast.copied": "已将结果复制到剪贴板。",
    "toast.copyFailed": "复制失败。",
    "err.noAgent.search": "没有运行中的智能体可执行搜索。",
    "err.search": "搜索失败。",

    // 缺少 kb_* 工具
    "missing.title": "{agent} 无法运行知识库工具",
    "missing.why":
      "本页面没有独立的后端——每一次搜索、列表和导入都是以聊天消息的形式发送给所选智能体的 kb_* 工具调用。kb_* 工具并非默认授予：只有当智能体自身的 tools: 列表中列出它们时才会绑定。缺少这些工具时，智能体只会用普通文字回复该调用，而那并不是搜索结果。",
    "missing.list": "该智能体缺少：",
    "missing.fix.add":
      "在「智能体 → {agent} → 工具」中（或在其 agent.json5 的 tools: 列表里）添加缺少的工具，然后重启该智能体。",
    "missing.fix.global":
      "如果工具已在其中列出，请检查「配置 → 工具」：全局开关会在启动时从每个智能体中移除该工具，且不会修改任何配置文件。",
    "missing.fix.other":
      "也可以在上方选择器中改选其他智能体——本页面会路由到所选的智能体，因此任何工具列表包含 kb_* 名称的智能体都可以使用。",
    "err.missingTool": "{agent} 没有 {tool} 工具。请将其添加到该智能体的工具中并重启，或在上方改选其他智能体。",
    "disabled.tool": "所选智能体没有 {tool} 工具。",

    // sources tab
    "sources.heading": "已索引来源",
    "sources.refresh": "刷新",
    "sources.stat": "{store}：{sources} 个来源 · {chunks} 个块",
    "sources.loading": "正在加载来源…",
    "sources.empty.title": "尚无已索引来源",
    "sources.empty.body": "从“添加来源”标签页添加网址或文件，或让智能体运行 kb_add_file / kb_add_url。",
    "col.source": "来源",
    "col.store": "存储库",
    "col.type": "类型",
    "col.actions": "操作",
    "type.url": "网址",
    "type.file": "文件",
    "sources.remove": "移除",
    "err.noAgent.list": "没有运行中的智能体可列出知识库。",
    "err.list": "无法列出来源。",

    // add tab
    "add.target.heading": "目标存储库",
    "add.target.subtitle": "网址 / 文件导入和同步将写入此处。",
    "add.sync.busy": "同步中…",
    "add.sync.btn": "同步存储库",
    "add.sync.hint": "重新索引存储库目录中新增 / 变更的文件。",
    "add.url.heading": "导入网址",
    "add.url.placeholder": "https://example.com/docs",
    "add.url.hint": "抓取并转为 markdown（可用时使用 Firecrawl，否则使用 httpx）。",
    "add.file.heading": "导入文件",
    "add.file.placeholder": "report.pdf 或 /abs/path/to/file.md",
    "add.file.hint":
      "工作区相对路径或绝对路径。支持文本、Markdown、代码、JSON、CSV、HTML（安装相应扩展后支持 PDF/DOCX）。",
    "add.ingest": "导入",
    "add.store.heading": "从目录创建存储库",
    "add.store.name": "存储库名称（例如 project_docs）",
    "add.store.dir": "目录（例如 docs/ 或 /abs/path）",
    "add.store.glob": "通配符（默认 **/*）",
    "add.store.creating": "创建中…",
    "add.store.create": "创建存储库",

    // add toasts
    "toast.urlIngested": "已经由 {agent} 导入网址。",
    "toast.fileIngested": "已经由 {agent} 导入文件。",
    "toast.storeCreated": "已创建存储库“{name}”。",
    "toast.synced": "知识库已同步。",
    "toast.sourceRemoved": "已移除来源。",
    "err.noAgent.url": "没有运行中的智能体可导入网址。",
    "err.noAgent.file": "没有运行中的智能体可导入文件。",
    "err.noAgent.store": "没有运行中的智能体可创建存储库。",
    "err.noAgent.sync": "没有运行中的智能体可同步。",
    "err.noAgent.delete": "没有运行中的智能体可删除来源。",
    "err.ingest": "导入失败。",
    "err.createStore": "创建存储库失败。",
    "err.sync": "同步失败。",
    "err.delete": "删除失败。",

    // delete modal
    "modal.title": "移除来源？",
    "modal.deindex": "从此存储库中移除该来源的索引",
    "modal.disk": "同时从磁盘删除该文件（kb_remove_file）",
    "modal.urlOnly": "网址仅从索引中移除——不会从磁盘删除任何内容。",
    "modal.cancel": "取消",
    "modal.removeDelete": "移除并删除文件",
    "modal.removeIndex": "从索引中移除",
  },

  es: {
    "title": "Conocimiento",
    "running": "{n} en ejecución",
    "agents.none": "Sin agentes",
    "reroute.notice": "{selected} está apagado, así que las consultas a la base de conocimiento las responde {target}. Inicia {selected} —o vuelve a elegirlo cuando esté en marcha— para dirigirlas ahí.",
    "page.title": "Base de conocimiento",
    "page.subtitle":
      "Fuentes indexadas como vectores que tus agentes pueden buscar semánticamente. Se enrutan mediante las herramientas kb_* del agente en ejecución seleccionado.",

    // tabs
    "tab.search": "Buscar",
    "tab.sources": "Fuentes",
    "tab.add": "Añadir fuente",

    // search tab
    "search.heading": "Buscar",
    "search.subtitle":
      "Búsqueda semántica en un almacén mediante el kb_search del agente en ejecución.",
    "search.placeholder": "Buscar en la base de conocimiento…",
    "search.btn": "Buscar",
    "search.field.store": "Almacén",
    "search.field.k": "Resultados (k)",
    "search.field.mode": "Modo",
    "search.mode.similarity": "similitud",
    "search.mode.mmr": "mmr (diverso)",
    "search.searching": "Buscando…",
    "search.prompt": "Escribe una consulta arriba para buscar en la base de conocimiento.",
    "search.prompt.blocked":
      "La búsqueda no está disponible: el agente seleccionado no tiene la herramienta kb_search. Consulta el aviso de arriba.",

    // search results
    "results.title": "Resultados",
    "results.hit": "coincidencia",
    "results.hits": "coincidencias",
    "results.via": "vía {agent}",
    "results.copy": "Copiar",
    "results.download": "Descargar",
    "results.none": "No se encontraron resultados para «{query}».",

    // toasts (search)
    "toast.copied": "Resultados copiados al portapapeles.",
    "toast.copyFailed": "Error al copiar.",
    "err.noAgent.search": "No hay ningún agente en ejecución para realizar la búsqueda.",
    "err.search": "La búsqueda falló.",

    // faltan herramientas kb_*
    "missing.title": "{agent} no puede ejecutar las herramientas de la base de conocimiento",
    "missing.why":
      "Esta página no tiene un backend propio: cada búsqueda, listado e ingesta es una llamada a una herramienta kb_* enviada al agente seleccionado como mensaje de chat. Las herramientas kb_* no se conceden por defecto: un agente solo las tiene cuando su propia lista tools: las nombra. Sin ellas, el agente responde a la llamada con texto normal, que no es un resultado de búsqueda.",
    "missing.list": "Faltan en este agente:",
    "missing.fix.add":
      "Añade las herramientas que faltan en Agentes → {agent} → Herramientas (o en la lista tools: de su agent.json5) y reinicia el agente.",
    "missing.fix.global":
      "Si una herramienta ya aparece ahí, revisa Configuración → Herramientas: un interruptor global la quita de todos los agentes al arrancar sin modificar ningún archivo de configuración.",
    "missing.fix.other":
      "O elige otro agente en el selector de arriba: esta página se enruta al agente seleccionado, así que servirá cualquiera cuyas herramientas incluyan los nombres kb_*.",
    "err.missingTool":
      "{agent} no tiene la herramienta {tool}. Añádela a las herramientas del agente y reinícialo, o elige otro agente arriba.",
    "disabled.tool": "El agente seleccionado no tiene la herramienta {tool}.",

    // sources tab
    "sources.heading": "Fuentes indexadas",
    "sources.refresh": "Actualizar",
    "sources.stat": "{store}: {sources} fuentes · {chunks} fragmentos",
    "sources.loading": "Cargando fuentes…",
    "sources.empty.title": "No hay fuentes indexadas",
    "sources.empty.body":
      "Añade una URL o un archivo desde la pestaña Añadir fuente, o haz que un agente ejecute kb_add_file / kb_add_url.",
    "col.source": "Fuente",
    "col.store": "Almacén",
    "col.type": "Tipo",
    "col.actions": "Acciones",
    "type.url": "url",
    "type.file": "archivo",
    "sources.remove": "Eliminar",
    "err.noAgent.list": "No hay ningún agente en ejecución para listar la base de conocimiento.",
    "err.list": "No se pudieron listar las fuentes.",

    // add tab
    "add.target.heading": "Almacén de destino",
    "add.target.subtitle": "Adónde escriben la ingesta de URL / archivos y la sincronización.",
    "add.sync.busy": "Sincronizando…",
    "add.sync.btn": "Sincronizar almacén",
    "add.sync.hint": "Reindexa los archivos nuevos / modificados del directorio del almacén.",
    "add.url.heading": "Ingerir URL",
    "add.url.placeholder": "https://example.com/docs",
    "add.url.hint": "Extraído a markdown (Firecrawl si está disponible, si no httpx).",
    "add.file.heading": "Ingerir archivo",
    "add.file.placeholder": "report.pdf o /abs/path/to/file.md",
    "add.file.hint":
      "Ruta relativa al espacio de trabajo o absoluta. Texto, Markdown, código, JSON, CSV, HTML (PDF/DOCX si se instalan los extras).",
    "add.ingest": "Ingerir",
    "add.store.heading": "Crear almacén desde un directorio",
    "add.store.name": "nombre del almacén (p. ej. project_docs)",
    "add.store.dir": "directorio (p. ej. docs/ o /abs/path)",
    "add.store.glob": "glob (predeterminado **/*)",
    "add.store.creating": "Creando…",
    "add.store.create": "Crear almacén",

    // add toasts
    "toast.urlIngested": "URL ingerida mediante {agent}.",
    "toast.fileIngested": "Archivo ingerido mediante {agent}.",
    "toast.storeCreated": "Almacén «{name}» creado.",
    "toast.synced": "Base de conocimiento sincronizada.",
    "toast.sourceRemoved": "Fuente eliminada.",
    "err.noAgent.url": "No hay ningún agente en ejecución para ingerir la URL.",
    "err.noAgent.file": "No hay ningún agente en ejecución para ingerir el archivo.",
    "err.noAgent.store": "No hay ningún agente en ejecución para crear el almacén.",
    "err.noAgent.sync": "No hay ningún agente en ejecución para sincronizar.",
    "err.noAgent.delete": "No hay ningún agente en ejecución para eliminar la fuente.",
    "err.ingest": "La ingesta falló.",
    "err.createStore": "No se pudo crear el almacén.",
    "err.sync": "La sincronización falló.",
    "err.delete": "La eliminación falló.",

    // delete modal
    "modal.title": "¿Eliminar fuente?",
    "modal.deindex": "Quitar del índice esta fuente del almacén",
    "modal.disk": "Eliminar también el archivo del disco (kb_remove_file)",
    "modal.urlOnly": "Las URL solo se quitan del índice; no se elimina nada del disco.",
    "modal.cancel": "Cancelar",
    "modal.removeDelete": "Eliminar + borrar archivo",
    "modal.removeIndex": "Quitar del índice",
  },
};
