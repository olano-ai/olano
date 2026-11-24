// The Files view: one tree for every agent the signed-in person can reach.
//
//   AGENTS
//   ├─ agent1            ← from the roster (GET /api/agents, already
//   │   └─ workspace        filtered to what this user may access)
//   │       ├─ docs/     ← GET /api/files?agent=agent1&path=docs
//   │       └─ AGENTS.md
//   └─ agent2
//
// The view never reads the sidebar's global agent selection: it lists every
// accessible agent, collapsed, and each row is scoped to its own agent on the
// wire. State lives here; the tree MODEL (rows, sort, filter, the bounded
// listing cache, the request queue) is `tree.ts`, unit-tested on its own; the
// windowed ARIA tree that paints the rows is `FileTree.tsx`.
//
// Budgets this view is built to keep, whatever the fleet size:
// * zero listing requests on load — agents start collapsed;
// * one request per directory a person opens, at most LIST_CONCURRENCY in
//   flight (the runtime allows 240 a minute; a persisted expansion across
//   twenty agents used to re-hydrate as one burst);
// * at most LIST_LIMIT entries per listing and PREVIEW_MAX_BYTES per text
//   preview on the wire — the server says what it left out;
// * at most CACHE_CAP listings in memory (collapsed ones evicted first) and
//   PREFS_CAP per-file preferences; file CONTENT is never persisted across
//   tab switches, only which file was open.

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
} from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ArrowLeft, FileQuestion, RotateCw, Upload, X } from "lucide-react";
import { api, ApiError } from "../../core/api";
import { subscribeStream } from "../../core/agentStream";
import { peekPersistentState, pokePersistentState, usePersistentState } from "../../core/usePersistentState";
import { useCurrentAgent } from "../../core/currentAgent";
import { filesPath, parseRoute } from "../../core/routeUrls";
import { type UrlHistory } from "../../core/useUrlParam";
import { useAuth } from "../../core/auth";
import { useT, type LocalDict } from "../../core/i18n";
import { Markdown } from "../../ui/Markdown";
import { useToast } from "../../ui/toast";
import { useConfirm } from "../../ui/confirm";
import { ContextMenu, type MenuItem, type MenuState } from "../../ui/ContextMenu";
import { agentStatus, statusColor } from "../agents/helpers";
import { buildHtmlSrcdoc, isHtmlFile } from "./htmlPreview";
import { FILL_KINDS, TEXT_KINDS, categorize, extOf, looksBinary, type FileKind } from "./fileKind";
import { FileTree, decodeDrag, DND_MIME, type TreeHandlers, type TreeLabels } from "./FileTree";
import { SEARCH_MIN_LENGTH } from "./search";
import { useWorkspaceSearch } from "./useWorkspaceSearch";
import { batchPercent, partitionBySize, UPLOAD_MAX_BYTES, uploadFile } from "./upload";
import {
  FetchQueue,
  agentInfo,
  agentRowKey,
  ancestorsOf,
  countEntries,
  flattenTree,
  fmtSize,
  isAbortError,
  isHiddenPath,
  isSyntheticKey,
  isWithin,
  listingsToHydrate,
  lruSet,
  makeClock,
  makeCollator,
  nodeKey,
  parentOf,
  pruneNodes,
  splitKey,
  workspaceRowKey,
  type AgentInfo,
  type FileEntry,
  type Listing,
  type NodeKey,
  type NodeMap,
  type Row,
} from "./tree";

// ── budgets ──────────────────────────────────────────────────────────────
/** Entries asked of one listing; the server reports what it left out. */
const LIST_LIMIT = 2000;
/** Listing requests in flight at once (single-flight per directory). */
const LIST_CONCURRENCY = 6;
/** Bytes asked of one text preview; a longer file previews its head. */
const PREVIEW_MAX_BYTES = 1024 * 1024;
/** Listings kept in memory; expanded ones are pinned, the rest LRU. */
const CACHE_CAP = 300;
/** Per-file preferences (draft, Preview/Raw, scroll) remembered per session. */
const PREFS_CAP = 50;
/** Live-update coalescing window per agent. */
const SSE_DEBOUNCE_MS = 300;

// ── view-local translations ──────────────────────────────────────────────
const STR: LocalDict = {
  en: {
    "title": "Files",
    "subtitle.all": "Workspace files of every agent you can access",
    "subtitle.readonly": " · Read-only — writes are disabled for your account.",
    "filter.placeholder": "Filter…",
    "search.searching": "Searching all workspaces…",
    "search.truncated": "Showing the first {n} matches — keep typing to narrow",
    "tree.hit": "Found in {path} — open to reveal it in the tree",
    "filter.clear": "Clear filter",
    "btn.refresh": "Refresh",
    "btn.refresh.agent": "Refresh {agent}",
    "tree.agents": "Agents",
    "tree.workspace": "workspace",
    "tree.loading": "Loading…",
    "tree.retry": "Retry",
    "tree.empty": "Empty — right-click to create a file or folder",
    "tree.truncated": "Showing the first {shown} of {total} entries — use the filter to narrow",
    "tree.scanSize": "{size} in {files} files (from the daily disk scan)",
    "count.folders.one": "{n} folder",
    "count.folders.other": "{n} folders",
    "count.files.one": "{n} file",
    "count.files.other": "{n} files",
    "count.empty": "Empty",
    "count.truncated": "· first {shown} of {total}",
    "empty.noAgents.title": "No agents",
    "empty.noAgents.sub": "You don't have access to any agent's workspace yet.",
    "empty.noMatches": "No matches",
    "preview.selectFile": "Select a file to preview",
    "preview.hint": "Markdown, HTML, code, images, audio, video and PDFs render inline.",
    "preview.failed.title": "Could not read the file",
    "preview.binary.title": "No preview for this file",
    "preview.binary.sub": "It isn't text, an image, audio, video or a PDF, so it can't be shown here. Download it to open it in another app.",
    "preview.binary.subReadOnly": "It isn't text, an image, audio, video or a PDF, so it can't be shown here.",
    "preview.binary.noExt": "Binary file",
    "preview.truncated": "Showing the first {shown} of {total}. Download the file to read all of it; editing is off so a save can't drop the rest.",
    "seg.preview": "Preview",
    "seg.raw": "Raw",
    "btn.edit": "Edit",
    "btn.edit.title": "Edit (⌘S to save)",
    "btn.copy": "Copy",
    "btn.copy.title": "Copy to clipboard",
    "btn.download": "Download",
    "btn.download.title": "Download file",
    "btn.delete": "Delete",
    "btn.delete.title": "Delete file",
    "btn.close": "Close",
    "btn.close.title": "Close preview",
    "btn.back": "All files",
    "btn.back.title": "Back to the file tree",
    "btn.save": "Save",
    "btn.cancel": "Cancel",
    "status.editing": "editing…",
    "status.saving": "saving…",
    "menu.open": "Open",
    "menu.expand": "Expand",
    "menu.collapse": "Collapse",
    "menu.refresh": "Refresh",
    "menu.copyPath": "Copy relative path",
    "menu.copyFullPath": "Copy full path",
    "menu.rename": "Rename",
    "menu.duplicate": "Duplicate",
    "menu.delete": "Delete",
    "menu.newFile": "New file",
    "menu.newFolder": "New folder",
    "confirm.delete.title": "Delete {kind}",
    "confirm.delete.file": "file",
    "confirm.delete.folder": "folder",
    "confirm.delete.msg": "Delete “{name}”? This cannot be undone.",
    "confirm.delete.folderMsg": "Delete folder “{name}” and everything inside it? This cannot be undone.",
    "confirm.delete.ok": "Delete",
    "toast.loadDir": "Could not load directory.",
    "toast.openFailed": "open failed",
    "toast.cannotPreview": "Cannot preview: {detail}",
    "toast.copied": "Copied",
    "toast.copyFailed": "Copy failed.",
    "toast.pathCopied": "Path copied",
    "toast.saved": "Saved",
    "toast.writeError": "write error",
    "toast.saveFailed": "Save failed: {detail}",
    "toast.deleted": "Deleted",
    "toast.deleteFailed": "Delete failed: {detail}",
    "toast.duplicated": "Duplicated",
    "toast.duplicateFailed": "Duplicate failed: {detail}",
    "toast.renamed": "Renamed",
    "toast.renameFailed": "Rename failed: {detail}",
    "toast.renameSlash": "Name cannot contain “/”.",
    "toast.moved": "Moved to {dest}",
    "toast.movedRoot": "Moved to workspace root",
    "toast.moveFailed": "Move failed: {detail}",
    "toast.moveSelf": "Cannot move a folder into itself.",
    "toast.moveCrossAgent": "Files can't be moved between agents — download and upload instead.",
    "toast.unknownAgent": "Agent “{agent}” is not in your roster.",
    "toast.created": "Created",
    "toast.createFailed": "Create failed: {detail}",
    "btn.newFile.title": "New file",
    "btn.newFolder.title": "New folder",
    "btn.upload.title": "Upload files from this device into the selected folder",
    "menu.upload": "Upload files…",
    "upload.progress": "Uploading {name} ({i} of {n}) · {pct}%",
    "upload.to": "to {agent} / {dir}",
    "upload.toRoot": "to {agent}'s workspace root",
    "toast.uploaded.one": "Uploaded {name} {where}",
    "toast.uploaded.many": "Uploaded {n} files {where}",
    "toast.uploadFailed": "Upload of {name} failed: {detail}",
    "toast.uploadTooLarge": "Not uploaded, over the {mb} MB limit: {names}",
    "confirm.replace.title": "Replace file?",
    "confirm.replace.msg": "“{name}” already exists {where}. Replace it with the one you picked?",
    "confirm.replace.ok": "Replace",
    "confirm.replace.skip": "Keep existing",
    "btn.new.noTarget": "Select an agent, folder or file first to choose where it goes",
    "btn.collapseAll.title": "Collapse all",
    "btn.showHidden.title": "Show hidden files — entries whose name starts with a dot (.env, .olano/) are left out of the tree by default",
    "btn.hideHidden.title": "Hide hidden files — stop listing entries whose name starts with a dot (.env, .olano/)",
    "prompt.newFile.title": "New file",
    "prompt.newFile.label": "File name",
    "prompt.newFile.placeholder": "example.md",
    "prompt.newFolder.title": "New folder",
    "prompt.newFolder.label": "Folder name",
    "prompt.newFolder.placeholder": "my-folder",
    "prompt.renameFile.title": "Rename file",
    "prompt.renameFolder.title": "Rename folder",
    "prompt.rename.label": "New name",
    "prompt.rename": "Rename",
    "prompt.create": "Create",
    "prompt.in": "in {agent} / {dir}",
    "prompt.inRoot": "in {agent}'s workspace root",
    "resize.title": "Drag to resize · double-click to reset",
  },
  zh: {
    "title": "文件",
    "subtitle.all": "你可访问的所有智能体的工作区文件",
    "subtitle.readonly": " · 只读——你的账户已禁用写入。",
    "filter.placeholder": "筛选…",
    "search.searching": "正在搜索所有工作区…",
    "search.truncated": "仅显示前 {n} 个匹配——继续输入以缩小范围",
    "tree.hit": "位于 {path}——打开即可在目录树中定位",
    "filter.clear": "清除筛选",
    "btn.refresh": "刷新",
    "btn.refresh.agent": "刷新 {agent}",
    "tree.agents": "智能体",
    "tree.workspace": "workspace",
    "tree.loading": "加载中…",
    "tree.retry": "重试",
    "tree.empty": "空——右键以创建文件或文件夹",
    "tree.truncated": "仅显示前 {shown} 项（共 {total} 项）——请使用筛选缩小范围",
    "tree.scanSize": "{size}，共 {files} 个文件（来自每日磁盘扫描）",
    "count.folders.one": "{n} 个文件夹",
    "count.folders.other": "{n} 个文件夹",
    "count.files.one": "{n} 个文件",
    "count.files.other": "{n} 个文件",
    "count.empty": "空",
    "count.truncated": "· 前 {shown} 项，共 {total} 项",
    "empty.noAgents.title": "暂无智能体",
    "empty.noAgents.sub": "你尚无权访问任何智能体的工作区。",
    "empty.noMatches": "无匹配项",
    "preview.selectFile": "选择一个文件以预览",
    "preview.hint": "Markdown、HTML、代码、图片、音频、视频和 PDF 均可内嵌渲染。",
    "preview.failed.title": "无法读取文件",
    "preview.binary.title": "此文件无法预览",
    "preview.binary.sub": "它不是文本、图片、音频、视频或 PDF，无法在此显示。请下载后用其他应用打开。",
    "preview.binary.subReadOnly": "它不是文本、图片、音频、视频或 PDF，无法在此显示。",
    "preview.binary.noExt": "二进制文件",
    "preview.truncated": "仅显示前 {shown}（共 {total}）。下载文件以阅读全部内容；已关闭编辑，以免保存时丢失其余部分。",
    "seg.preview": "预览",
    "seg.raw": "源码",
    "btn.edit": "编辑",
    "btn.edit.title": "编辑（⌘S 保存）",
    "btn.copy": "复制",
    "btn.copy.title": "复制到剪贴板",
    "btn.download": "下载",
    "btn.download.title": "下载文件",
    "btn.delete": "删除",
    "btn.delete.title": "删除文件",
    "btn.close": "关闭",
    "btn.close.title": "关闭预览",
    "btn.back": "全部文件",
    "btn.back.title": "返回文件树",
    "btn.save": "保存",
    "btn.cancel": "取消",
    "status.editing": "编辑中…",
    "status.saving": "保存中…",
    "menu.open": "打开",
    "menu.expand": "展开",
    "menu.collapse": "折叠",
    "menu.refresh": "刷新",
    "menu.copyPath": "复制相对路径",
    "menu.copyFullPath": "复制完整路径",
    "menu.rename": "重命名",
    "menu.duplicate": "创建副本",
    "menu.delete": "删除",
    "menu.newFile": "新建文件",
    "menu.newFolder": "新建文件夹",
    "confirm.delete.title": "删除{kind}",
    "confirm.delete.file": "文件",
    "confirm.delete.folder": "文件夹",
    "confirm.delete.msg": "删除“{name}”？此操作无法撤销。",
    "confirm.delete.folderMsg": "删除文件夹“{name}”及其全部内容？此操作无法撤销。",
    "confirm.delete.ok": "删除",
    "toast.loadDir": "无法加载目录。",
    "toast.openFailed": "打开失败",
    "toast.cannotPreview": "无法预览：{detail}",
    "toast.copied": "已复制",
    "toast.copyFailed": "复制失败。",
    "toast.pathCopied": "已复制路径",
    "toast.saved": "已保存",
    "toast.writeError": "写入错误",
    "toast.saveFailed": "保存失败：{detail}",
    "toast.deleted": "已删除",
    "toast.deleteFailed": "删除失败：{detail}",
    "toast.duplicated": "已创建副本",
    "toast.duplicateFailed": "创建副本失败：{detail}",
    "toast.renamed": "已重命名",
    "toast.renameFailed": "重命名失败：{detail}",
    "toast.renameSlash": "名称不能包含“/”。",
    "toast.moved": "已移动到 {dest}",
    "toast.movedRoot": "已移动到工作区根目录",
    "toast.moveFailed": "移动失败：{detail}",
    "toast.moveSelf": "无法将文件夹移动到其自身内部。",
    "toast.moveCrossAgent": "文件不能在智能体之间移动——请改为下载后上传。",
    "toast.unknownAgent": "智能体“{agent}”不在你的名单中。",
    "toast.created": "已创建",
    "toast.createFailed": "创建失败：{detail}",
    "btn.newFile.title": "新建文件",
    "btn.newFolder.title": "新建文件夹",
    "btn.upload.title": "从此设备上传文件到所选文件夹",
    "menu.upload": "上传文件…",
    "upload.progress": "正在上传 {name}（第 {i} 个，共 {n} 个）· {pct}%",
    "upload.to": "到 {agent} / {dir}",
    "upload.toRoot": "到 {agent} 的工作区根目录",
    "toast.uploaded.one": "已上传 {name}{where}",
    "toast.uploaded.many": "已上传 {n} 个文件{where}",
    "toast.uploadFailed": "{name} 上传失败：{detail}",
    "toast.uploadTooLarge": "超过 {mb} MB 上限，未上传：{names}",
    "confirm.replace.title": "替换文件？",
    "confirm.replace.msg": "“{name}”已{where}存在。要用你选择的文件替换它吗？",
    "confirm.replace.ok": "替换",
    "confirm.replace.skip": "保留现有文件",
    "btn.new.noTarget": "请先选择一个智能体、文件夹或文件，以确定创建位置",
    "btn.collapseAll.title": "全部折叠",
    "btn.showHidden.title": "显示隐藏文件——默认不在树中列出以点开头的条目（.env、.olano/ 等）",
    "btn.hideHidden.title": "隐藏隐藏文件——不再列出以点开头的条目（.env、.olano/ 等）",
    "prompt.newFile.title": "新建文件",
    "prompt.newFile.label": "文件名",
    "prompt.newFile.placeholder": "example.md",
    "prompt.newFolder.title": "新建文件夹",
    "prompt.newFolder.label": "文件夹名称",
    "prompt.newFolder.placeholder": "my-folder",
    "prompt.renameFile.title": "重命名文件",
    "prompt.renameFolder.title": "重命名文件夹",
    "prompt.rename.label": "新名称",
    "prompt.rename": "重命名",
    "prompt.create": "创建",
    "prompt.in": "在 {agent} / {dir} 中",
    "prompt.inRoot": "在 {agent} 的工作区根目录",
    "resize.title": "拖动以调整宽度 · 双击重置",
  },
  es: {
    "title": "Archivos",
    "subtitle.all": "Archivos del espacio de trabajo de cada agente al que tienes acceso",
    "subtitle.readonly": " · Solo lectura: la escritura está deshabilitada para tu cuenta.",
    "filter.placeholder": "Filtrar…",
    "search.searching": "Buscando en todos los espacios de trabajo…",
    "search.truncated": "Mostrando las primeras {n} coincidencias — sigue escribiendo para acotar",
    "tree.hit": "Encontrado en {path} — ábrelo para localizarlo en el árbol",
    "filter.clear": "Borrar filtro",
    "btn.refresh": "Actualizar",
    "btn.refresh.agent": "Actualizar {agent}",
    "tree.agents": "Agentes",
    "tree.workspace": "workspace",
    "tree.loading": "Cargando…",
    "tree.retry": "Reintentar",
    "tree.empty": "Vacío: haz clic derecho para crear un archivo o carpeta",
    "tree.truncated": "Se muestran las primeras {shown} de {total} entradas; usa el filtro para acotar",
    "tree.scanSize": "{size} en {files} archivos (del escaneo diario de disco)",
    "count.folders.one": "{n} carpeta",
    "count.folders.other": "{n} carpetas",
    "count.files.one": "{n} archivo",
    "count.files.other": "{n} archivos",
    "count.empty": "Vacío",
    "count.truncated": "· primeras {shown} de {total}",
    "empty.noAgents.title": "Sin agentes",
    "empty.noAgents.sub": "Aún no tienes acceso al espacio de trabajo de ningún agente.",
    "empty.noMatches": "Sin coincidencias",
    "preview.selectFile": "Selecciona un archivo para previsualizar",
    "preview.hint": "Markdown, HTML, código, imágenes, audio, video y PDF se muestran en línea.",
    "preview.failed.title": "No se pudo leer el archivo",
    "preview.binary.title": "No hay vista previa para este archivo",
    "preview.binary.sub": "No es texto, imagen, audio, video ni PDF, así que no se puede mostrar aquí. Descárgalo para abrirlo en otra aplicación.",
    "preview.binary.subReadOnly": "No es texto, imagen, audio, video ni PDF, así que no se puede mostrar aquí.",
    "preview.binary.noExt": "Archivo binario",
    "preview.truncated": "Se muestran los primeros {shown} de {total}. Descarga el archivo para leerlo entero; la edición está desactivada para que un guardado no pierda el resto.",
    "seg.preview": "Vista previa",
    "seg.raw": "Original",
    "btn.edit": "Editar",
    "btn.edit.title": "Editar (⌘S para guardar)",
    "btn.copy": "Copiar",
    "btn.copy.title": "Copiar al portapapeles",
    "btn.download": "Descargar",
    "btn.download.title": "Descargar archivo",
    "btn.delete": "Eliminar",
    "btn.delete.title": "Eliminar archivo",
    "btn.close": "Cerrar",
    "btn.close.title": "Cerrar vista previa",
    "btn.back": "Todos los archivos",
    "btn.back.title": "Volver al árbol de archivos",
    "btn.save": "Guardar",
    "btn.cancel": "Cancelar",
    "status.editing": "editando…",
    "status.saving": "guardando…",
    "menu.open": "Abrir",
    "menu.expand": "Expandir",
    "menu.collapse": "Contraer",
    "menu.refresh": "Actualizar",
    "menu.copyPath": "Copiar ruta relativa",
    "menu.copyFullPath": "Copiar ruta completa",
    "menu.rename": "Cambiar nombre",
    "menu.duplicate": "Duplicar",
    "menu.delete": "Eliminar",
    "menu.newFile": "Nuevo archivo",
    "menu.newFolder": "Nueva carpeta",
    "confirm.delete.title": "Eliminar {kind}",
    "confirm.delete.file": "archivo",
    "confirm.delete.folder": "carpeta",
    "confirm.delete.msg": "¿Eliminar «{name}»? Esto no se puede deshacer.",
    "confirm.delete.folderMsg": "¿Eliminar la carpeta «{name}» y todo su contenido? Esto no se puede deshacer.",
    "confirm.delete.ok": "Eliminar",
    "toast.loadDir": "No se pudo cargar el directorio.",
    "toast.openFailed": "error al abrir",
    "toast.cannotPreview": "No se puede previsualizar: {detail}",
    "toast.copied": "Copiado",
    "toast.copyFailed": "Error al copiar.",
    "toast.pathCopied": "Ruta copiada",
    "toast.saved": "Guardado",
    "toast.writeError": "error de escritura",
    "toast.saveFailed": "Error al guardar: {detail}",
    "toast.deleted": "Eliminado",
    "toast.deleteFailed": "Error al eliminar: {detail}",
    "toast.duplicated": "Duplicado",
    "toast.duplicateFailed": "Error al duplicar: {detail}",
    "toast.renamed": "Nombre cambiado",
    "toast.renameFailed": "Error al cambiar el nombre: {detail}",
    "toast.renameSlash": "El nombre no puede contener «/».",
    "toast.moved": "Movido a {dest}",
    "toast.movedRoot": "Movido a la raíz del espacio de trabajo",
    "toast.moveFailed": "Error al mover: {detail}",
    "toast.moveSelf": "No se puede mover una carpeta dentro de sí misma.",
    "toast.moveCrossAgent": "Los archivos no se pueden mover entre agentes: descárgalo y súbelo de nuevo.",
    "toast.unknownAgent": "El agente «{agent}» no está en tu lista.",
    "toast.created": "Creado",
    "toast.createFailed": "Error al crear: {detail}",
    "btn.newFile.title": "Nuevo archivo",
    "btn.newFolder.title": "Nueva carpeta",
    "btn.upload.title": "Subir archivos desde este dispositivo a la carpeta seleccionada",
    "menu.upload": "Subir archivos…",
    "upload.progress": "Subiendo {name} ({i} de {n}) · {pct}%",
    "upload.to": "a {agent} / {dir}",
    "upload.toRoot": "a la raíz del espacio de trabajo de {agent}",
    "toast.uploaded.one": "{name} subido {where}",
    "toast.uploaded.many": "{n} archivos subidos {where}",
    "toast.uploadFailed": "No se pudo subir {name}: {detail}",
    "toast.uploadTooLarge": "No se subieron, superan el límite de {mb} MB: {names}",
    "confirm.replace.title": "¿Reemplazar archivo?",
    "confirm.replace.msg": "“{name}” ya existe {where}. ¿Reemplazarlo por el que elegiste?",
    "confirm.replace.ok": "Reemplazar",
    "confirm.replace.skip": "Conservar el existente",
    "btn.new.noTarget": "Selecciona primero un agente, carpeta o archivo para elegir dónde crearlo",
    "btn.collapseAll.title": "Contraer todo",
    "btn.showHidden.title": "Mostrar archivos ocultos: las entradas cuyo nombre empieza por punto (.env, .olano/) no aparecen en el árbol por defecto",
    "btn.hideHidden.title": "Ocultar archivos ocultos: dejar de listar las entradas cuyo nombre empieza por punto (.env, .olano/)",
    "prompt.newFile.title": "Nuevo archivo",
    "prompt.newFile.label": "Nombre del archivo",
    "prompt.newFile.placeholder": "ejemplo.md",
    "prompt.newFolder.title": "Nueva carpeta",
    "prompt.newFolder.label": "Nombre de la carpeta",
    "prompt.newFolder.placeholder": "mi-carpeta",
    "prompt.renameFile.title": "Cambiar nombre del archivo",
    "prompt.renameFolder.title": "Cambiar nombre de la carpeta",
    "prompt.rename.label": "Nuevo nombre",
    "prompt.rename": "Cambiar nombre",
    "prompt.create": "Crear",
    "prompt.in": "en {agent} / {dir}",
    "prompt.inRoot": "en la raíz del espacio de trabajo de {agent}",
    "resize.title": "Arrastra para ajustar · doble clic para restablecer",
  },
};

// ── types ──────────────────────────────────────────────────────────────
interface ListingEnvelope {
  entries: FileEntry[];
  truncated: boolean;
  total: number;
}

interface FileContentResponse {
  path: string;
  content: string;
  size: number;
  truncated?: boolean;
  total_size?: number;
  /** The head is not text; `content` is empty. */
  binary?: boolean;
}

interface FileWriteResponse {
  path: string;
  size: number;
}

/** The open file — identity only, so it can be remembered across tab
 *  switches without holding its bytes. Content lives in `FileContent`. */
interface SelectedRef {
  agent: string;
  path: string;
  name: string;
  absPath?: string;
}

interface FileContent {
  key: NodeKey;
  text: string;
  size: number;
  truncated: boolean;
  totalSize: number;
  /** Not text after all (sniffed, not named): the pane offers the download. */
  binary?: boolean;
  /** Set when the read failed: the pane shows this and a Retry instead of a
   *  spinner that never stops (the toast alone expires in seconds). */
  error?: string;
}

/** Per-file bits remembered per session (bounded, see PREFS_CAP). */
interface FilePrefs {
  editing?: boolean;
  mdRaw?: boolean;
  htmlRaw?: boolean;
  scroll?: number;
  draft?: string;
}

/** Live `file_updated` SSE event shape (see api.py:_push_event). */
interface FileUpdatedEvent {
  type: string;
  agent?: string | null;
  path?: string;
  size?: number;
}

/** Encode each path segment but keep the slashes for FastAPI's `{path:path}`. */
function encodePath(path: string): string {
  return path
    .split("/")
    .map((seg) => encodeURIComponent(seg))
    .join("/");
}

function agentQuery(agent: string): string {
  return `?agent=${encodeURIComponent(agent)}`;
}

function rawUrl(path: string, agent: string): string {
  return `/api/files-raw/${encodePath(path)}${agentQuery(agent)}`;
}

function errMessage(e: unknown, fallback: string): string {
  if (e instanceof ApiError) return e.detail;
  if (e instanceof Error) return e.message;
  return fallback;
}

// ── tree pane width (px) ─────────────────────────────────────────────────
// Persisted to localStorage so the user's chosen split survives reloads.
const TREE_W_KEY = "lc.files.treeWidth";
const TREE_W_MIN = 220;
const TREE_W_MAX = 640;
const TREE_W_DEFAULT = 320;
const PREVIEW_W_MIN = 360;

function clampTreeWidth(px: number): number {
  const max = Math.max(TREE_W_MIN, Math.min(TREE_W_MAX, window.innerWidth - PREVIEW_W_MIN));
  return Math.round(Math.min(max, Math.max(TREE_W_MIN, px)));
}

function readTreeWidth(): number {
  try {
    const raw = localStorage.getItem(TREE_W_KEY);
    if (raw) return clampTreeWidth(parseFloat(raw));
  } catch {
    // storage disabled — fall through to the default.
  }
  return TREE_W_DEFAULT;
}

function writeTreeWidth(px: number): void {
  try {
    localStorage.setItem(TREE_W_KEY, String(px));
  } catch {
    // storage disabled / quota — keep working in-memory only.
  }
}

// ── hidden entries (dotfiles) ────────────────────────────────────────────
// `GET /api/files` omits dot-entries unless asked, so this is a listing
// parameter, not a client-side filter: flipping it re-lists every cached
// directory. Persisted like the pane width.
const SHOW_HIDDEN_KEY = "lc.files.showHidden";

function readShowHidden(): boolean {
  try {
    return localStorage.getItem(SHOW_HIDDEN_KEY) === "1";
  } catch {
    return false;
  }
}

function writeShowHidden(on: boolean): void {
  try {
    localStorage.setItem(SHOW_HIDDEN_KEY, on ? "1" : "0");
  } catch {
    // storage disabled / quota — keep working in-memory only.
  }
}

// ── per-file preferences (bounded, session-scoped) ───────────────────────
// One insertion-ordered Map for every file ever opened this session, capped
// at PREFS_CAP. The previous view minted one persistent-state key per file
// per preference (draft, editing, mdRaw, htmlRaw, scroll) and never dropped
// any — a session that browsed a few hundred files kept a few thousand keys
// and every draft's full text alive until the tab closed.
const PREFS_KEY = "files.prefs.v2";

function prefsMap(): Map<NodeKey, FilePrefs> {
  let m = peekPersistentState<Map<NodeKey, FilePrefs>>(PREFS_KEY);
  if (!m) {
    m = new Map();
    pokePersistentState(PREFS_KEY, m);
  }
  return m;
}

function getPrefs(key: NodeKey): FilePrefs {
  return prefsMap().get(key) ?? {};
}

function setPrefs(key: NodeKey, patch: FilePrefs): void {
  const m = prefsMap();
  lruSet(m, key, { ...(m.get(key) ?? {}), ...patch }, PREFS_CAP);
}

/** Remembered tree scroll offset (one number, not one per agent). */
const TREE_SCROLL_KEY = "files.tree.scroll.v2";

// ── icons (inline SVG, no asset deps) ────────────────────────────────────
const folderIcon = (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth={2}>
    <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
  </svg>
);
const fileIcon = (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth={2}>
    <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
    <path d="M14 3v6h6" />
  </svg>
);
const searchIcon = (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth={2}>
    <circle cx="11" cy="11" r="7" />
    <path d="M21 21l-4.3-4.3" />
  </svg>
);
const editIcon = (
  <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
    <path d="M12 20h9" />
    <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z" />
  </svg>
);
const copyIcon = (
  <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M5 15V5a2 2 0 0 1 2-2h10" />
  </svg>
);
const downloadIcon = (
  <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
    <path d="M12 3v12" />
    <path d="M7 10l5 5 5-5" />
    <path d="M5 21h14" />
  </svg>
);
const trashIcon = (
  <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
    <path d="M3 6h18" />
    <path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" />
    <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
    <path d="M10 11v6" />
    <path d="M14 11v6" />
  </svg>
);
const duplicateIcon = (
  <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M5 15V5a2 2 0 0 1 2-2h10" />
  </svg>
);
// "Rename" — a text/title glyph (serif I), distinct from the Edit pencil.
const renameIcon = (
  <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
    <path d="M4 7V5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v2" />
    <path d="M12 4v16" />
    <path d="M9 20h6" />
  </svg>
);
const pathIcon = (
  <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
    <path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1" />
    <path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1" />
  </svg>
);
const openIcon = (
  <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
    <path d="M15 3h6v6" />
    <path d="M10 14L21 3" />
    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
  </svg>
);
const newFileIcon = (
  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth={1.9} aria-hidden>
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h6" />
    <path d="M14 3v5h5" />
    <path d="M18 14v6" />
    <path d="M15 17h6" />
  </svg>
);
const newFolderIcon = (
  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth={1.9} aria-hidden>
    <path d="M3 7a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v3" />
    <path d="M3 9v9a2 2 0 0 0 2 2h7" />
    <path d="M18 14v6" />
    <path d="M15 17h6" />
  </svg>
);
const uploadIcon = <Upload size={13} strokeWidth={2} aria-hidden />;
const collapseIcon = (
  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth={1.9} aria-hidden>
    <path d="M9 9H4" />
    <path d="M9 9V4" />
    <path d="M9 9 4 4" />
    <path d="M15 15h5" />
    <path d="M15 15v5" />
    <path d="m15 15 5 5" />
  </svg>
);
// Hidden-entry toggle: an open eye while dot-entries are listed, a struck-out
// one while they're filtered out (the state, not the action — the tooltip says
// what clicking does).
const eyeIcon = (
  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth={1.9} aria-hidden>
    <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7z" />
    <circle cx="12" cy="12" r="3" />
  </svg>
);
const eyeOffIcon = (
  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth={1.9} aria-hidden>
    <path d="M10.6 5.2A9.9 9.9 0 0 1 12 5c6.4 0 10 7 10 7a16.4 16.4 0 0 1-2.9 3.8" />
    <path d="M6.4 6.5A16.3 16.3 0 0 0 2 12s3.6 7 10 7a9.7 9.7 0 0 0 4.3-1" />
    <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
    <path d="M3 3l18 18" />
  </svg>
);
/** Tree disclosure caret — points right when collapsed, rotated when open. */
const treeCaret = (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
    <path d="M9 6l6 6-6 6" />
  </svg>
);


// ── name prompt modal (New File / New Folder) ────────────────────────────
interface PromptState {
  title: string;
  label: string;
  placeholder?: string;
  context?: string;
  /** Pre-filled value (e.g. the current name for a rename). */
  initialValue?: string;
  submitLabel: string;
  cancelLabel: string;
  onSubmit: (value: string) => void;
}

function PromptModal({ prompt, onClose }: { prompt: PromptState; onClose: () => void }) {
  const [value, setValue] = useState(prompt.initialValue ?? "");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.focus();
    // For a rename (pre-filled), select the basename without its extension so
    // the user can retype it immediately — standard rename affordance.
    if (prompt.initialValue) {
      const dot = prompt.initialValue.lastIndexOf(".");
      el.setSelectionRange(0, dot > 0 ? dot : prompt.initialValue.length);
    }
  }, [prompt.initialValue]);

  const trimmed = value.trim();
  const submit = () => {
    if (!trimmed) return;
    onClose();
    prompt.onSubmit(trimmed);
  };

  return (
    <div className="modal-overlay" onClick={onClose} style={{ zIndex: 80 }}>
      <div
        className="card"
        onClick={(e) => e.stopPropagation()}
        style={{ width: 420, maxWidth: "92vw", padding: 22, boxShadow: "var(--shadow-lg)" }}
        role="dialog"
        aria-modal="true"
      >
        <h2 style={{ margin: "0 0 4px", fontSize: 16 }}>{prompt.title}</h2>
        {prompt.context && (
          <p className="muted" style={{ margin: "0 0 14px", fontSize: 12.5 }}>
            {prompt.context}
          </p>
        )}
        <label className="muted" style={{ fontSize: 12, display: "block", marginBottom: 6 }}>
          {prompt.label}
        </label>
        <input
          ref={inputRef}
          className="input"
          value={value}
          placeholder={prompt.placeholder}
          spellCheck={false}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              submit();
            } else if (e.key === "Escape") {
              e.preventDefault();
              onClose();
            }
          }}
        />
        <div className="row" style={{ justifyContent: "flex-end", marginTop: 18, gap: 8 }}>
          <button className="btn" onClick={onClose}>
            {prompt.cancelLabel}
          </button>
          <button className="btn primary" disabled={!trimmed} onClick={submit}>
            {prompt.submitLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── component ──────────────────────────────────────────────────────────
export function FilesView() {
  const toast = useToast();
  const t = useT(STR);
  const confirm = useConfirm();
  const { agents, agent: currentAgent, loading: agentsLoading } = useCurrentAgent();
  const { user, multiUser } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  // Non-admin members in a multi-user install may browse + read only; the
  // server enforces this with 403s, so the UI just hides write affordances.
  const readOnly = multiUser && !!user && !user.is_admin;

  // ── state ──
  const [nodes, setNodes] = useState<NodeMap>({});
  const nodesRef = useRef<NodeMap>(nodes);
  nodesRef.current = nodes;
  const [expanded, setExpanded] = usePersistentState<NodeKey[]>("files.expanded.v2", []);
  const expandedSet = useMemo(() => new Set(expanded), [expanded]);
  const [selected, setSelected] = usePersistentState<SelectedRef | null>("files.selected.v2", null);
  const selectedRef = useRef<SelectedRef | null>(selected);
  selectedRef.current = selected;
  // How the next selection change reaches the URL: a click on a file is a Back
  // step ("push"); a selection the tree had to correct (the file was renamed,
  // moved or deleted under it) replaces, since the old address names nothing.
  const selectionHistoryRef = useRef<UrlHistory>("push");
  // The address: /files/<agent>?path=<workspace-relative> (core/routeUrls.ts).
  // Read here, once, for every effect below; the older /files?agent=<a>
  // spelling is rewritten to this form before the view mounts (RouteCanonical,
  // in both shells).
  const urlParts = parseRoute(location.pathname);
  const urlPath = (new URLSearchParams(location.search).get("path") ?? "").replace(/^\/+|\/+$/g, "");
  const urlAgent = urlParts.agent ?? "";
  const [content, setContent] = useState<FileContent | null>(null);
  const [focusedKey, setFocusedKey] = usePersistentState<NodeKey | null>("files.focused.v2", null);
  const [revealKey, setRevealKey] = useState<NodeKey | null>(null);
  const [search, setSearch] = usePersistentState("files.search", "");
  const [debouncedSearch, setDebouncedSearch] = useState(
    () => peekPersistentState<string>("files.search") ?? "",
  );
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [prompt, setPrompt] = useState<PromptState | null>(null);
  const [showHidden, setShowHidden] = useState<boolean>(readShowHidden);
  const showHiddenRef = useRef(showHidden);
  showHiddenRef.current = showHidden;

  const collator = useMemo(makeCollator, []);
  const clock = useMemo(makeClock, []);
  const queue = useMemo(() => new FetchQueue<Listing | null>(LIST_CONCURRENCY), []);
  // Leaving the view abandons whatever is still on its way. Deferred a tick
  // and re-checked: React's dev-mode StrictMode runs this cleanup once at
  // mount (a simulated unmount) and would otherwise abort the first batch
  // of real listings only to have the effect re-issue them.
  const mountedRef = useRef(false);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      window.setTimeout(() => {
        if (!mountedRef.current) queue.cancelAll();
      }, 0);
    };
  }, [queue]);

  // The roster as the tree sees it. Memoized on the roster identity so a
  // status flip re-renders the agent row and nothing else.
  const agentInfos = useMemo<AgentInfo[]>(
    () => agents.map((a) => agentInfo(a, agentStatus(a))),
    [agents],
  );
  const agentNames = useMemo(() => new Set(agentInfos.map((a) => a.name)), [agentInfos]);

  // Debounce the filter box (80ms).
  useEffect(() => {
    const id = window.setTimeout(() => setDebouncedSearch(search), 80);
    return () => window.clearTimeout(id);
  }, [search]);
  const query = debouncedSearch.trim().toLowerCase();
  // The network half of the filter box: matches in workspaces the tree has not
  // listed. Debounced and cancellable on its own terms (see search.ts) — the
  // 80ms local narrowing above never waits for it.
  const wsSearch = useWorkspaceSearch(search, showHidden);

  // ── split pane ──
  const [treeWidth, setTreeWidth] = useState<number>(readTreeWidth);
  const [resizing, setResizing] = useState(false);
  const gridRef = useRef<HTMLDivElement>(null);

  const startResize = useCallback(
    (e: MouseEvent) => {
      e.preventDefault();
      const startX = e.clientX;
      const startW = treeWidth;
      let latest = startW;
      setResizing(true);
      document.body.style.userSelect = "none";
      document.body.style.cursor = "col-resize";
      const onMove = (ev: globalThis.MouseEvent) => {
        latest = clampTreeWidth(startW + (ev.clientX - startX));
        if (gridRef.current) gridRef.current.style.gridTemplateColumns = `${latest}px 1fr`;
      };
      const onUp = () => {
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
        document.body.style.userSelect = "";
        document.body.style.cursor = "";
        setResizing(false);
        setTreeWidth(latest);
        writeTreeWidth(latest);
      };
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    },
    [treeWidth],
  );

  const resetResize = useCallback(() => {
    const w = clampTreeWidth(TREE_W_DEFAULT);
    setTreeWidth(w);
    writeTreeWidth(w);
  }, []);

  useEffect(() => {
    const onResize = () => setTreeWidth((w) => clampTreeWidth(w));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // ── listings ──
  /** Fetch one directory into the node map, through the bounded queue.
   *  `silent` keeps the current listing on screen while it reloads. */
  const loadListing = useCallback(
    (agent: string, path: string, opts?: { silent?: boolean }): Promise<Listing | null> => {
      const key = nodeKey(agent, path);
      if (!opts?.silent) {
        setNodes((prev) => ({
          ...prev,
          [key]: { ...prev[key], loading: true, error: undefined, touched: clock() },
        }));
      }
      return queue
        .enqueue(key, async (signal) => {
          const params = new URLSearchParams({ agent, limit: String(LIST_LIMIT) });
          if (path) params.set("path", path);
          if (showHiddenRef.current) params.set("hidden", "true");
          try {
            const res = await api.get<ListingEnvelope>(`/api/files?${params.toString()}`, { signal });
            const listing: Listing = { entries: res.entries, truncated: res.truncated, total: res.total };
            setNodes((prev) => ({ ...prev, [key]: { listing, touched: clock() } }));
            return listing;
          } catch (e) {
            if (isAbortError(e)) {
              // Collapsed or unmounted mid-flight: not an error, just no longer wanted.
              setNodes((prev) =>
                prev[key]?.loading ? { ...prev, [key]: { ...prev[key], loading: false } } : prev,
              );
              return null;
            }
            setNodes((prev) => ({
              ...prev,
              [key]: {
                ...prev[key],
                loading: false,
                error: errMessage(e, t("toast.loadDir")),
                touched: clock(),
              },
            }));
            return null;
          }
        })
        .catch(() => null);
    },
    [queue, clock, t],
  );

  // What the current expansion needs loaded to render. Recomputed per render;
  // read through a ref by the debounced SSE handler.
  const hydrateKeys = useMemo(
    () => listingsToHydrate(agentInfos, expandedSet),
    [agentInfos, expandedSet],
  );
  const hydrateRef = useRef<NodeKey[]>(hydrateKeys);
  hydrateRef.current = hydrateKeys;

  // Re-hydration: every listing the expansion needs and doesn't have goes to
  // the queue. `error` is a settled state — a directory that can't be listed
  // (a 403 on a `shared/*` projection, a 404 after a rename) is retried only
  // by a user gesture, never by this effect; the queue's single-flight guard
  // keeps a re-run from re-requesting a directory already on its way. A
  // listing a live update marked `stale` while collapsed reloads silently
  // the moment it is visible again (stale-while-revalidate).
  useEffect(() => {
    if (agentsLoading) return;
    for (const key of hydrateKeys) {
      const n = nodes[key];
      if (queue.has(key)) continue;
      const { agent, path } = splitKey(key);
      if (n?.listing?.stale) void loadListing(agent, path, { silent: true });
      else if (!n?.listing && !n?.loading && !n?.error) void loadListing(agent, path);
    }
  }, [agentsLoading, hydrateKeys, nodes, queue, loadListing]);

  // Bound the cache: everything the expansion needs stays, the rest is LRU.
  const pinned = useMemo(() => {
    const s = new Set<NodeKey>(hydrateKeys);
    if (selected) s.add(nodeKey(selected.agent, parentOf(selected.path)));
    return s;
  }, [hydrateKeys, selected]);
  useEffect(() => {
    setNodes((prev) => pruneNodes(prev, pinned, CACHE_CAP));
  }, [nodes, pinned]);

  // Agents that left the roster (deleted, access revoked) take their
  // listings, expansion and any pending requests with them. Never on an
  // EMPTY roster: the roster hook reports `[]` for a failed refetch too (a
  // 429 mid-burst, a reconnecting proxy), and that must not throw away the
  // person's open folders and file — with nothing listed the tree shows its
  // empty state and everything comes back when the roster does.
  useEffect(() => {
    if (agentsLoading || agentNames.size === 0) return;
    queue.cancelWhere((k) => !isSyntheticKey(k) && !agentNames.has(splitKey(k).agent));
    setExpanded((prev) => {
      const next = prev.filter((k) => {
        if (k.startsWith("agent:")) return agentNames.has(k.slice(6));
        if (k.startsWith("ws:")) return agentNames.has(k.slice(3));
        return agentNames.has(splitKey(k).agent);
      });
      return next.length === prev.length ? prev : next;
    });
    setNodes((prev) => {
      const keys = Object.keys(prev).filter((k) => !agentNames.has(splitKey(k).agent));
      if (!keys.length) return prev;
      const next = { ...prev };
      for (const k of keys) delete next[k];
      return next;
    });
    const cur = selectedRef.current;
    if (cur && !agentNames.has(cur.agent)) {
      selectionHistoryRef.current = "replace";
      setSelected(null);
    }
  }, [agentsLoading, agentNames, queue, setExpanded, setSelected]);

  /** Reload every VISIBLE listing (optionally one agent's), keeping what is
   *  on screen while it refreshes. Nothing collapsed is ever fetched. */
  const refreshVisible = useCallback(
    (agent?: string) => {
      for (const key of hydrateRef.current) {
        const s = splitKey(key);
        if (agent && s.agent !== agent) continue;
        void loadListing(s.agent, s.path, { silent: true });
      }
    },
    [loadListing],
  );

  /** Show / hide dot-entries: a listing parameter, so every visible listing
   *  goes back out with the new flag. Collapsed ones are dropped rather than
   *  refetched — they'd list with the wrong flag when opened otherwise. */
  const setHiddenVisible = useCallback(
    (next: boolean) => {
      if (showHiddenRef.current === next) return;
      showHiddenRef.current = next;
      writeShowHidden(next);
      setShowHidden(next);
      const visible = new Set(hydrateRef.current);
      setNodes((prev) => {
        const out: Record<NodeKey, NodeMap[string]> = {};
        for (const k of Object.keys(prev)) if (visible.has(k)) out[k] = prev[k];
        return out;
      });
      refreshVisible();
    },
    [refreshVisible],
  );

  // ── expansion ──
  const expandKeys = useCallback(
    (keys: NodeKey[]) => {
      setExpanded((prev) => {
        const have = new Set(prev);
        const add = keys.filter((k) => !have.has(k));
        return add.length ? [...prev, ...add] : prev;
      });
    },
    [setExpanded],
  );

  /** Collapse a row: drop it (and, for agent/workspace rows, every listing
   *  key beneath it) from the expansion, and cancel anything still queued
   *  for that subtree. Listings already loaded stay cached (LRU-bounded). */
  const collapseRow = useCallback(
    (row: Row) => {
      if (row.kind === "agent" || row.kind === "workspace") {
        const agent = row.agent;
        // Both rows close their whole subtree: the workspace row's listings
        // (an agent row also takes the workspace row itself with it).
        const drop = (k: NodeKey) =>
          k === row.key ||
          (row.kind === "agent" && k === workspaceRowKey(agent)) ||
          (!isSyntheticKey(k) && splitKey(k).agent === agent);
        setExpanded((prev) => prev.filter((k) => !drop(k)));
        queue.cancelWhere((k) => !isSyntheticKey(k) && splitKey(k).agent === agent);
        return;
      }
      if (row.kind === "entry") {
        const { agent, path } = splitKey(row.key);
        setExpanded((prev) =>
          prev.filter((k) => isSyntheticKey(k) || splitKey(k).agent !== agent || !isWithin(splitKey(k).path, path)),
        );
        queue.cancelWhere((k) => !isSyntheticKey(k) && splitKey(k).agent === agent && isWithin(splitKey(k).path, path));
      }
    },
    [setExpanded, queue],
  );

  const toggleRow = useCallback(
    (row: Row, open: boolean) => {
      if (!open) {
        collapseRow(row);
        return;
      }
      if (row.kind === "agent") expandKeys([row.key]);
      else if (row.kind === "workspace") expandKeys([agentRowKey(row.agent), row.key]);
      else if (row.kind === "entry" && row.entry.type === "directory") expandKeys([row.key]);
    },
    [collapseRow, expandKeys],
  );

  // ── preview ──
  // The file whose content is wanted, written the moment a preview starts.
  // "Latest request wins" has to be decided against this, not against the
  // rendered selection: a local read can resolve before React commits the
  // render that carries the new selection, and a guard on that saw the OLD
  // selection and dropped the response — a spinner that never ended.
  const previewKeyRef = useRef<NodeKey | null>(null);
  const previewFile = useCallback(
    async (agent: string, entry: Pick<FileEntry, "name" | "path" | "abs_path">) => {
      const cat = categorize(entry.name);
      const key = nodeKey(agent, entry.path);
      previewKeyRef.current = key;
      setSelected({ agent, path: entry.path, name: entry.name, absPath: entry.abs_path });
      setFocusedKey(key);
      setContent(null);
      // Media render straight from /api/files-raw. A known binary format asks
      // for one byte, which is all it takes to learn the file's size.
      const named = cat.kind === "binary";
      if (!named && !TEXT_KINDS.has(cat.kind)) return;
      try {
        const res = await api.get<FileContentResponse>(
          `/api/files/${encodePath(entry.path)}${agentQuery(agent)}&max_bytes=${named ? 1 : PREVIEW_MAX_BYTES}`,
        );
        if (previewKeyRef.current !== key) return; // moved on meanwhile
        const binary = named || !!res.binary || looksBinary(res.content);
        setContent({
          key,
          text: binary ? "" : res.content,
          size: binary ? 0 : res.size,
          truncated: !binary && !!res.truncated,
          totalSize: res.total_size ?? res.size,
          binary,
        });
      } catch (e) {
        const detail = errMessage(e, t("toast.openFailed"));
        toast.push(t("toast.cannotPreview", { detail }), "error");
        if (previewKeyRef.current === key) {
          setContent({ key, text: "", size: 0, truncated: false, totalSize: 0, error: detail });
        }
      }
    },
    [toast, t, setSelected, setFocusedKey],
  );

  // A remembered selection (tab switch back) has no content yet: fetch it,
  // once, when the roster is known. Only the remembered case — a fresh click
  // goes through previewFile itself, and re-running here on every selection
  // change would fetch each opened file twice.
  //
  // A URL that names a file of its own wins: the deep-link effect below opens
  // that one instead. So does one that names another agent's workspace
  // (/files/<agent>): the remembered file is not in it, and the deep-link
  // effect drops that selection.
  const restoredRef = useRef(false);
  useEffect(() => {
    if (agentsLoading || restoredRef.current) return;
    restoredRef.current = true;
    const cur = selectedRef.current;
    if (!cur || !agentNames.has(cur.agent)) return;
    if (urlPath && urlPath !== cur.path) return;
    if (urlAgent && urlAgent !== cur.agent) return;
    void previewFile(cur.agent, { name: cur.name, path: cur.path, abs_path: cur.absPath });
    // Arrival only: the address is read once, when the roster is known.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentsLoading, agentNames, previewFile]);

  // Expand the way to `target` in `agent`'s workspace, list its parent, then
  // preview it (a file) or open it (a folder) and scroll its row into view.
  // Shared by deep links and search hits — both name a path the tree may not
  // have loaded yet.
  const revealEntry = useCallback(
    async (agent: string, target: string) => {
      if (isHiddenPath(target)) setHiddenVisible(true);
      const ancestors = ancestorsOf(target);
      expandKeys([agentRowKey(agent), workspaceRowKey(agent), ...ancestors.map((p) => nodeKey(agent, p))]);
      const parent = parentOf(target);
      const name = target.split("/").pop() ?? target;
      // The queue single-flights this with the re-hydration effect's own
      // request for the same directory, so the reveal costs no extra listing.
      const listing = await loadListing(agent, parent, { silent: true });
      const entry = listing?.entries.find((e) => e.name === name);
      if (!entry) {
        toast.push(t("toast.cannotPreview", { detail: target }), "error");
        return;
      }
      const key = nodeKey(agent, entry.path);
      if (entry.type === "directory") expandKeys([key]);
      else void previewFile(agent, entry);
      setFocusedKey(key);
      setRevealKey(key);
    },
    [expandKeys, loadListing, previewFile, setFocusedKey, setHiddenVisible, toast, t],
  );

  // ── the address ──
  // The open file is the URL: /files/<agent>?path=<workspace-relative>
  // (core/routeUrls.ts). Other views link here that way (Cortex file lists,
  // skill panels, optimize reports, chat attachments, the runtime's media
  // cards), a reload or a copied link reopens the same file, and Back/Forward
  // step between the files opened. Landing on a path expands that agent →
  // workspace → the ancestor chain, previews the file (or opens the folder)
  // and scrolls its row into view. /files/<agent> alone opens that agent's
  // workspace. The sidebar selection is never touched: this view spans every
  // agent, and the segment only says which one the file is in.
  //
  // Older links (/files?agent=<a>&path=<p>, stored in notifications) are
  // rewritten to this form before the view mounts (RouteCanonical); a bare
  // /files?path=<p> still resolves against the selected agent.
  const handledLinkRef = useRef<string | null>(null);
  // The selection a link is about to produce. When it lands, the address is
  // already right (or only needs tidying), so it is recorded as a replace: a
  // link is one step, not two.
  const linkTargetRef = useRef<string | null>(null);
  useEffect(() => {
    if (agentsLoading) return; // roster still resolving — decide once it lands
    const linkAgent = urlAgent || (urlPath ? currentAgent || agentInfos[0]?.name || "" : "");
    if (!linkAgent) {
      // Back to a bare /files: back to nothing open. (Not on arrival: a bare
      // sidebar visit keeps the file remembered from last time.)
      const before = handledLinkRef.current;
      handledLinkRef.current = "";
      if (before && selectedRef.current) {
        selectionHistoryRef.current = "replace";
        setSelected(null);
      }
      return;
    }
    const linkKey = `${linkAgent}\n${urlPath}`;
    // Exactly once per address: a roster refresh re-runs this effect with the
    // same URL, which would fetch the file twice.
    if (handledLinkRef.current === linkKey) return;
    handledLinkRef.current = linkKey;
    // The address this view itself just wrote for a click: already on screen.
    const cur = selectedRef.current;
    if (urlPath && cur && cur.agent === linkAgent && cur.path === urlPath) return;
    if (!agentNames.has(linkAgent)) {
      toast.push(t("toast.unknownAgent", { agent: linkAgent }), "error");
      return;
    }
    if (!urlPath) {
      expandKeys([agentRowKey(linkAgent), workspaceRowKey(linkAgent)]);
      // /files/<agent> is that agent's workspace: a file remembered in another
      // agent's is not what the link opened, so it is dropped (the address
      // already says where we are, hence a replace, not a step).
      if (cur && cur.agent !== linkAgent) {
        selectionHistoryRef.current = "replace";
        setSelected(null);
      }
      return;
    }
    setSearch(""); // a persisted filter could hide the linked row
    linkTargetRef.current = linkKey;
    void revealEntry(linkAgent, urlPath);
  }, [agentsLoading, agentInfos, agentNames, urlAgent, urlPath, currentAgent, expandKeys, revealEntry, setSearch, setSelected, toast, t]);

  // The other direction: what is open becomes the address. Only on a change
  // of selection (a folder link leaves the selected file alone, and must not
  // be overwritten by it), and on arrival when a bare /files is showing the
  // file remembered from the last visit. An arrival that names a file, or
  // another agent's workspace, is the deep-link effect's to act on, not this
  // one's to overwrite.
  const reflectedSelRef = useRef<SelectedRef | null | undefined>(undefined);
  useEffect(() => {
    const prev = reflectedSelRef.current;
    reflectedSelRef.current = selected;
    const arrival = prev === undefined;
    if (!arrival && prev === selected) return;
    if (arrival && (!selected || urlPath || (urlAgent && urlAgent !== selected.agent))) return;
    // Nothing open under /files/<agent> is still that agent's workspace.
    const to = selected ? filesPath(selected.agent, selected.path) : filesPath(urlAgent || null);
    const here = location.pathname + location.search;
    const fromLink = !!selected && linkTargetRef.current === `${selected.agent}\n${selected.path}`;
    linkTargetRef.current = null;
    const history = arrival || fromLink ? "replace" : selectionHistoryRef.current;
    selectionHistoryRef.current = "push";
    if (to === here) return;
    // Mark it handled so the deep-link effect reads it as this view's echo.
    if (selected) handledLinkRef.current = `${selected.agent}\n${selected.path}`;
    navigate(to, { replace: history === "replace" });
    // Only a selection change drives this; the URL is read, not watched.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);

  // ── live updates ──
  // `file_updated` frames name the agent whose workspace changed. Per agent,
  // coalesce a burst into one pass SSE_DEBOUNCE_MS later: visible listings of
  // that agent reload silently; collapsed ones are marked stale and reload
  // when next shown; the open file re-reads if it is the one that changed.
  useEffect(() => {
    const timers = new Map<string, number>();
    const touched = new Map<string, Set<string>>();
    const flush = (agent: string) => {
      timers.delete(agent);
      const paths = touched.get(agent) ?? new Set<string>();
      touched.delete(agent);
      const visible = new Set(hydrateRef.current);
      const cached = Object.keys(nodesRef.current).filter((k) => splitKey(k).agent === agent);
      const stale: NodeKey[] = [];
      for (const key of cached) {
        if (visible.has(key)) void loadListing(agent, splitKey(key).path, { silent: true });
        else stale.push(key);
      }
      if (stale.length) {
        setNodes((prev) => {
          const next = { ...prev };
          for (const k of stale) {
            const n = next[k];
            if (n?.listing && !n.listing.stale) next[k] = { ...n, listing: { ...n.listing, stale: true } };
          }
          return next;
        });
      }
      const cur = selectedRef.current;
      if (cur && cur.agent === agent && paths.has(cur.path)) {
        void previewFile(agent, { name: cur.name, path: cur.path, abs_path: cur.absPath });
      }
    };
    const unsubscribe = subscribeStream((frame) => {
      const event = frame as FileUpdatedEvent;
      if (event.type !== "file_updated") return;
      const agent = event.agent || "";
      if (!agent) return;
      if (event.path) {
        if (!touched.has(agent)) touched.set(agent, new Set());
        touched.get(agent)!.add(event.path);
      }
      if (!timers.has(agent)) timers.set(agent, window.setTimeout(() => flush(agent), SSE_DEBOUNCE_MS));
    });
    return () => {
      unsubscribe();
      for (const id of timers.values()) window.clearTimeout(id);
    };
  }, [loadListing, previewFile]);

  // ── rows ──
  const rows = useMemo(
    () => flattenTree({ agents: agentInfos, nodes, expanded: expandedSet, query, collator, hits: wsSearch.hits }),
    [agentInfos, nodes, expandedSet, query, collator, wsSearch.hits],
  );
  const rowsByKey = useMemo(() => {
    const m = new Map<NodeKey, Row>();
    for (const r of rows) if (r.kind === "agent" || r.kind === "workspace" || r.kind === "entry") m.set(r.key, r);
    return m;
  }, [rows]);

  /** Where a toolbar "New file / New folder" lands: the focused row's
   *  directory (a folder itself, a file's parent, an agent/workspace's root),
   *  else the selected file's parent. */
  const createTarget = useMemo((): { agent: string; dir: string } | null => {
    const row = focusedKey ? rowsByKey.get(focusedKey) : undefined;
    if (row?.kind === "agent" || row?.kind === "workspace") return { agent: row.agent, dir: "" };
    if (row?.kind === "entry") {
      const { agent, path } = splitKey(row.key);
      return { agent, dir: row.entry.type === "directory" ? path : parentOf(path) };
    }
    if (selected) return { agent: selected.agent, dir: parentOf(selected.path) };
    return null;
  }, [focusedKey, rowsByKey, selected]);
  const focusedAgent = createTarget?.agent ?? null;

  // ── file operations ──
  const copyText = useCallback(
    async (text: string, msg: string) => {
      try {
        await navigator.clipboard.writeText(text);
        toast.push(msg);
      } catch {
        toast.push(t("toast.copyFailed"), "error");
      }
    },
    [toast, t],
  );

  const deleteEntry = useCallback(
    async (agent: string, entry: Pick<FileEntry, "name" | "path" | "type">) => {
      if (readOnly) return;
      const isDir = entry.type === "directory";
      const ok = await confirm({
        title: t("confirm.delete.title", { kind: t(isDir ? "confirm.delete.folder" : "confirm.delete.file") }),
        message: t(isDir ? "confirm.delete.folderMsg" : "confirm.delete.msg", { name: entry.name }),
        confirmLabel: t("confirm.delete.ok"),
        danger: true,
      });
      if (!ok) return;
      try {
        await api.del(`/api/files/${encodePath(entry.path)}${agentQuery(agent)}`);
        toast.push(t("toast.deleted"));
        // Only when the selection is inside what was deleted: the flag is
        // consumed by the next selection change, so setting it for an
        // untouched selection would turn the next click's step into a replace.
        const sel = selectedRef.current;
        if (sel && sel.agent === agent && isWithin(sel.path, entry.path)) {
          selectionHistoryRef.current = "replace";
          setSelected(null);
        }
        setExpanded((prev) =>
          prev.filter((k) => isSyntheticKey(k) || splitKey(k).agent !== agent || !isWithin(splitKey(k).path, entry.path)),
        );
        void loadListing(agent, parentOf(entry.path), { silent: true });
      } catch (e) {
        toast.push(t("toast.deleteFailed", { detail: errMessage(e, t("toast.openFailed")) }), "error");
      }
    },
    [confirm, toast, t, loadListing, setSelected, setExpanded, readOnly],
  );

  const duplicateEntry = useCallback(
    async (agent: string, entry: Pick<FileEntry, "path">) => {
      if (readOnly) return;
      try {
        await api.post<{ path: string }>(`/api/files/duplicate${agentQuery(agent)}`, { path: entry.path });
        toast.push(t("toast.duplicated"));
        void loadListing(agent, parentOf(entry.path), { silent: true });
      } catch (e) {
        toast.push(t("toast.duplicateFailed", { detail: errMessage(e, t("toast.openFailed")) }), "error");
      }
    },
    [toast, t, loadListing, readOnly],
  );

  /** Rename `entry` in place (same parent dir) via the move endpoint. */
  const renameEntry = useCallback(
    async (agent: string, entry: Pick<FileEntry, "name" | "path" | "type">, rawName: string) => {
      if (readOnly) return;
      const name = rawName.trim().replace(/^\/+|\/+$/g, "");
      if (!name || name === entry.name) return;
      if (name.includes("/")) {
        toast.push(t("toast.renameSlash"), "error");
        return;
      }
      const parent = parentOf(entry.path);
      const dest = parent ? `${parent}/${name}` : name;
      const oldPath = entry.path;
      try {
        await api.post(`/api/files/move${agentQuery(agent)}`, { source: oldPath, dest });
        toast.push(t("toast.renamed"));
        // Keep expanded descendants of a renamed folder open under the new path.
        setExpanded((prev) =>
          prev.map((k) => {
            if (isSyntheticKey(k)) return k;
            const s = splitKey(k);
            if (s.agent !== agent || !isWithin(s.path, oldPath)) return k;
            return nodeKey(agent, dest + s.path.slice(oldPath.length));
          }),
        );
        const cur = selectedRef.current;
        if (cur && cur.agent === agent && cur.path === oldPath && entry.type === "file") {
          selectionHistoryRef.current = "replace";
          void previewFile(agent, { name, path: dest });
        } else if (cur && cur.agent === agent && isWithin(cur.path, oldPath)) {
          selectionHistoryRef.current = "replace";
          setSelected({ ...cur, path: dest + cur.path.slice(oldPath.length) });
        }
        setFocusedKey(nodeKey(agent, dest));
        void loadListing(agent, parent, { silent: true });
      } catch (e) {
        toast.push(t("toast.renameFailed", { detail: errMessage(e, t("toast.openFailed")) }), "error");
      }
    },
    [toast, t, loadListing, previewFile, setSelected, setExpanded, setFocusedKey, readOnly],
  );

  /** Move `src` into the directory `dest.path` ("" = workspace root) of the
   *  same agent. The move endpoint is per-workspace, so a cross-agent drop is
   *  refused rather than half-done. */
  const moveEntry = useCallback(
    async (src: { agent: string; path: string }, dest: { agent: string; path: string }) => {
      if (readOnly) return;
      if (src.agent !== dest.agent) {
        toast.push(t("toast.moveCrossAgent"), "error");
        return;
      }
      const agent = src.agent;
      const base = src.path.split("/").pop() ?? src.path;
      const target = dest.path ? `${dest.path}/${base}` : base;
      if (target === src.path) return; // already there
      if (isWithin(dest.path, src.path) && dest.path !== "") {
        toast.push(t("toast.moveSelf"), "error");
        return;
      }
      try {
        await api.post(`/api/files/move${agentQuery(agent)}`, { source: src.path, dest: target });
        toast.push(dest.path ? t("toast.moved", { dest: dest.path }) : t("toast.movedRoot"));
        const sel = selectedRef.current;
        if (sel && sel.agent === agent && sel.path === src.path) {
          selectionHistoryRef.current = "replace";
          setSelected(null);
        }
        if (dest.path) expandKeys([nodeKey(agent, dest.path)]);
        void loadListing(agent, parentOf(src.path), { silent: true });
        void loadListing(agent, dest.path, { silent: true });
      } catch (e) {
        toast.push(t("toast.moveFailed", { detail: errMessage(e, t("toast.openFailed")) }), "error");
      }
    },
    [toast, t, loadListing, setSelected, expandKeys, readOnly],
  );

  const createEntry = useCallback(
    async (agent: string, type: "file" | "directory", parentDir: string, rawName: string) => {
      if (readOnly) return;
      const name = rawName.trim().replace(/^\/+|\/+$/g, "");
      if (!name) return;
      const path = parentDir ? `${parentDir}/${name}` : name;
      try {
        await api.post(`/api/files/create${agentQuery(agent)}`, { path, type });
        toast.push(t("toast.created"));
        const keys = [agentRowKey(agent), workspaceRowKey(agent)];
        if (parentDir) keys.push(nodeKey(agent, parentDir));
        if (type === "directory") keys.push(nodeKey(agent, path));
        expandKeys(keys);
        await loadListing(agent, parentDir, { silent: true });
        if (type === "file") void previewFile(agent, { name, path });
        else setFocusedKey(nodeKey(agent, path));
      } catch (e) {
        toast.push(t("toast.createFailed", { detail: errMessage(e, t("toast.openFailed")) }), "error");
      }
    },
    [toast, t, loadListing, previewFile, expandKeys, setFocusedKey, readOnly],
  );

  const promptNew = useCallback(
    (agent: string, type: "file" | "directory", parentDir: string) => {
      const isFile = type === "file";
      setPrompt({
        title: t(isFile ? "prompt.newFile.title" : "prompt.newFolder.title"),
        label: t(isFile ? "prompt.newFile.label" : "prompt.newFolder.label"),
        placeholder: t(isFile ? "prompt.newFile.placeholder" : "prompt.newFolder.placeholder"),
        context: parentDir ? t("prompt.in", { agent, dir: parentDir }) : t("prompt.inRoot", { agent }),
        submitLabel: t("prompt.create"),
        cancelLabel: t("btn.cancel"),
        onSubmit: (name) => void createEntry(agent, type, parentDir, name),
      });
    },
    [t, createEntry],
  );

  // ── upload from the device ──
  // One hidden <input type="file">, opened for whichever folder the button or
  // menu chose; the chosen folder rides a ref because the input's change event
  // arrives after the click handler has returned.
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const uploadTargetRef = useRef<{ agent: string; dir: string } | null>(null);
  const [upload, setUpload] = useState<{ name: string; i: number; n: number; pct: number } | null>(null);

  const uploadFiles = useCallback(
    async (agent: string, dir: string, picked: File[]) => {
      if (readOnly || !picked.length) return;
      const where = dir ? t("upload.to", { agent, dir }) : t("upload.toRoot", { agent });
      const location = dir ? t("prompt.in", { agent, dir }) : t("prompt.inRoot", { agent });
      const { ok: files, tooLarge } = partitionBySize(picked);
      if (tooLarge.length) {
        toast.push(
          t("toast.uploadTooLarge", {
            mb: String(UPLOAD_MAX_BYTES / (1024 * 1024)),
            names: tooLarge.map((f) => f.name).join(", "),
          }),
          "error",
        );
      }
      const sizes = files.map((f) => f.size);
      const uploaded: string[] = [];
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        const show = (loaded: number) =>
          setUpload({ name: file.name, i: i + 1, n: files.length, pct: batchPercent(sizes, i, loaded) });
        show(0);
        try {
          let res;
          try {
            res = await uploadFile(agent, dir, file, { onProgress: show });
          } catch (e) {
            // A file of that name exists: ask, then send again with overwrite.
            // A folder of that name (`folder_exists`) cannot be replaced.
            if (!(e instanceof ApiError && e.code === "file_exists")) throw e;
            setUpload(null); // nothing is moving while the question is open
            const replace = await confirm({
              title: t("confirm.replace.title"),
              message: t("confirm.replace.msg", { name: file.name, where: location }),
              confirmLabel: t("confirm.replace.ok"),
              cancelLabel: t("confirm.replace.skip"),
            });
            if (!replace) continue;
            show(0);
            res = await uploadFile(agent, dir, file, { overwrite: true, onProgress: show });
          }
          uploaded.push(res.path);
        } catch (e) {
          toast.push(t("toast.uploadFailed", { name: file.name, detail: errMessage(e, t("toast.openFailed")) }), "error");
        }
      }
      setUpload(null);
      if (!uploaded.length) return;
      const first = uploaded[0];
      const firstName = first.split("/").pop() ?? first;
      toast.push(
        uploaded.length === 1
          ? t("toast.uploaded.one", { name: firstName, where })
          : t("toast.uploaded.many", { n: String(uploaded.length), where }),
      );
      const keys = [agentRowKey(agent), workspaceRowKey(agent)];
      if (dir) keys.push(nodeKey(agent, dir));
      expandKeys(keys);
      await loadListing(agent, dir, { silent: true });
      // Land on what arrived: the single file opens, a batch is focused on
      // its first entry so the tree scrolls to the folder.
      if (uploaded.length === 1) void previewFile(agent, { name: firstName, path: first });
      else setFocusedKey(nodeKey(agent, first));
    },
    [readOnly, t, toast, confirm, expandKeys, loadListing, previewFile, setFocusedKey],
  );

  const startUpload = useCallback(
    (agent: string, dir: string) => {
      if (readOnly || upload) return;
      uploadTargetRef.current = { agent, dir };
      uploadInputRef.current?.click();
    },
    [readOnly, upload],
  );

  const promptRename = useCallback(
    (agent: string, entry: FileEntry) => {
      const isFile = entry.type === "file";
      setPrompt({
        title: t(isFile ? "prompt.renameFile.title" : "prompt.renameFolder.title"),
        label: t("prompt.rename.label"),
        initialValue: entry.name,
        submitLabel: t("prompt.rename"),
        cancelLabel: t("btn.cancel"),
        onSubmit: (name) => void renameEntry(agent, entry, name),
      });
    },
    [t, renameEntry],
  );

  // ── context menus ──
  const openRowMenu = useCallback(
    (e: MouseEvent, row: Row) => {
      e.preventDefault();
      const items: MenuItem[] = [];
      if (row.kind === "agent" || row.kind === "workspace") {
        const agent = row.agent;
        items.push({
          label: t(row.expanded ? "menu.collapse" : "menu.expand"),
          icon: openIcon,
          onClick: () => toggleRow(row, !row.expanded),
        });
        if (!readOnly) {
          items.push(
            { label: t("menu.newFile"), icon: newFileIcon, separatorBefore: true, onClick: () => promptNew(agent, "file", "") },
            { label: t("menu.newFolder"), icon: newFolderIcon, onClick: () => promptNew(agent, "directory", "") },
            { label: t("menu.upload"), icon: uploadIcon, disabled: !!upload, onClick: () => startUpload(agent, "") },
          );
        }
        items.push({
          label: t("menu.refresh"),
          icon: <RotateCw size={13} strokeWidth={1.75} />,
          separatorBefore: true,
          onClick: () => refreshVisible(agent),
        });
        setMenu({ x: e.clientX, y: e.clientY, items });
        return;
      }
      if (row.kind === "hit") {
        // A match the tree has not listed: Open reveals it (which loads the
        // real listing, where every other action lives) and Copy path needs
        // nothing more than the hit itself.
        const { agent: hitAgent, path } = splitKey(row.key);
        items.push(
          { label: t("menu.open"), icon: openIcon, onClick: () => void revealEntry(hitAgent, path) },
          {
            label: t("menu.copyPath"),
            icon: copyIcon,
            separatorBefore: true,
            onClick: () => void copyText(path, t("toast.pathCopied")),
          },
        );
        setMenu({ x: e.clientX, y: e.clientY, items });
        return;
      }
      if (row.kind !== "entry") return;
      const { agent } = splitKey(row.key);
      const entry = row.entry;
      const isDir = entry.type === "directory";
      items.push({
        label: t("menu.open"),
        icon: openIcon,
        onClick: () => (isDir ? toggleRow(row, true) : void previewFile(agent, entry)),
      });
      if (isDir && !readOnly) {
        items.push(
          { label: t("menu.newFile"), icon: newFileIcon, onClick: () => promptNew(agent, "file", entry.path) },
          { label: t("menu.newFolder"), icon: newFolderIcon, onClick: () => promptNew(agent, "directory", entry.path) },
          { label: t("menu.upload"), icon: uploadIcon, disabled: !!upload, onClick: () => startUpload(agent, entry.path) },
        );
      }
      items.push({
        label: t("menu.copyPath"),
        icon: copyIcon,
        separatorBefore: true,
        onClick: () => void copyText(entry.path, t("toast.pathCopied")),
      });
      if (entry.abs_path) {
        items.push({
          label: t("menu.copyFullPath"),
          icon: pathIcon,
          onClick: () => void copyText(entry.abs_path ?? entry.path, t("toast.pathCopied")),
        });
      }
      if (!readOnly) {
        items.push(
          { label: t("menu.rename"), icon: renameIcon, separatorBefore: true, onClick: () => promptRename(agent, entry) },
          { label: t("menu.duplicate"), icon: duplicateIcon, onClick: () => void duplicateEntry(agent, entry) },
          { label: t("menu.delete"), icon: trashIcon, danger: true, separatorBefore: true, onClick: () => void deleteEntry(agent, entry) },
        );
      }
      setMenu({ x: e.clientX, y: e.clientY, items });
    },
    [
      t,
      readOnly,
      toggleRow,
      promptNew,
      startUpload,
      upload,
      refreshVisible,
      previewFile,
      copyText,
      promptRename,
      duplicateEntry,
      deleteEntry,
      revealEntry,
    ],
  );

  // ── tree wiring ──
  const treeHandlers = useMemo<TreeHandlers>(
    () => ({
      onActivate: (row) => {
        if (row.kind === "hit") {
          void revealEntry(row.agent, row.hit.path);
          return;
        }
        if (row.kind === "entry" && row.entry.type === "file") {
          const { agent } = splitKey(row.key);
          void previewFile(agent, row.entry);
          return;
        }
        if (row.kind === "agent" || row.kind === "workspace" || row.kind === "entry") {
          toggleRow(row, !row.expanded);
          setFocusedKey(row.key);
        }
      },
      onToggle: (row, open) => {
        toggleRow(row, open);
        setFocusedKey(row.key);
      },
      onContextMenu: openRowMenu,
      onRename: (row) => {
        if (row.kind === "entry") promptRename(splitKey(row.key).agent, row.entry);
      },
      onDelete: (row) => {
        if (row.kind === "entry") void deleteEntry(splitKey(row.key).agent, row.entry);
      },
      onMove: (src, dest) => void moveEntry(src, dest),
      onRetry: (agent, path) => void loadListing(agent, path),
      onFocusChange: (key) => setFocusedKey(key),
    }),
    [previewFile, toggleRow, setFocusedKey, openRowMenu, promptRename, deleteEntry, moveEntry, loadListing, revealEntry],
  );

  const treeLabels = useMemo<TreeLabels>(
    () => ({
      workspace: t("tree.workspace"),
      loading: t("tree.loading"),
      retry: t("tree.retry"),
      empty: t("tree.empty"),
      truncated: (shown, total) => t("tree.truncated", { shown, total }),
      scanSize: (size, files) => t("tree.scanSize", { size, files }),
      hit: (path) => t("tree.hit", { path }),
    }),
    [t],
  );
  const treeIcons = useMemo(() => ({ folder: folderIcon, file: fileIcon, caret: treeCaret }), []);
  const treeScrollTop = useMemo(() => peekPersistentState<number>(TREE_SCROLL_KEY) ?? 0, []);
  const onTreeScroll = useCallback((top: number) => pokePersistentState(TREE_SCROLL_KEY, top), []);

  const selectedKey = selected ? nodeKey(selected.agent, selected.path) : null;
  const [rootDropping, setRootDropping] = useState(false);

  // Footer: the focused agent's root listing.
  const footer = useMemo(() => {
    if (!focusedAgent) return null;
    const listing = nodes[nodeKey(focusedAgent, "")]?.listing;
    if (!listing) return null;
    const { dirs, files } = countEntries(listing.entries);
    const parts: string[] = [];
    if (dirs) parts.push(t(dirs === 1 ? "count.folders.one" : "count.folders.other", { n: String(dirs) }));
    if (files) parts.push(t(files === 1 ? "count.files.one" : "count.files.other", { n: String(files) }));
    let text = parts.join(" · ") || t("count.empty");
    if (listing.truncated) {
      text += ` ${t("count.truncated", { shown: listing.entries.length.toLocaleString(), total: listing.total.toLocaleString() })}`;
    }
    return { agent: focusedAgent, text };
  }, [focusedAgent, nodes, t]);

  const anyExpanded = expanded.length > 0;
  const noAgents = !agentsLoading && agentInfos.length === 0;

  return (
    <>
      <div className="topbar">
        <div>
          <strong>{t("title")}</strong>
          <div className="muted" style={{ fontSize: 12 }}>
            {t("subtitle.all")}
            {readOnly && t("subtitle.readonly")}
          </div>
        </div>
        <div className="row">
          <div style={{ position: "relative", width: "min(220px, 45vw)" }}>
            <span
              aria-hidden
              style={{
                position: "absolute",
                left: 11,
                top: "50%",
                transform: "translateY(-50%)",
                color: "var(--text-4)",
                display: "flex",
                pointerEvents: "none",
              }}
            >
              {searchIcon}
            </span>
            <input
              className="input"
              style={{ paddingLeft: 32, paddingRight: search ? 30 : 12 }}
              placeholder={t("filter.placeholder")}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => {
                // Enter searches now instead of after the pause; Escape clears.
                if (e.key === "Enter") wsSearch.flush();
                else if (e.key === "Escape") setSearch("");
              }}
            />
            {search && (
              <button
                onClick={() => setSearch("")}
                title={t("filter.clear")}
                aria-label={t("filter.clear")}
                style={{
                  position: "absolute",
                  right: 6,
                  top: "50%",
                  transform: "translateY(-50%)",
                  width: 20,
                  height: 20,
                  display: "grid",
                  placeItems: "center",
                  border: "none",
                  borderRadius: "var(--r-pill)",
                  background: "var(--surface-3)",
                  color: "var(--text-3)",
                  cursor: "pointer",
                  lineHeight: 1,
                }}
              >
                <X size={13} strokeWidth={2} />
              </button>
            )}
          </div>
        </div>
      </div>

      {/* `files-preview-open` is a phone-only signal: there the two panes
          stack, and with a file open the preview takes the column while the
          header's Back button returns to the tree. */}
      <div
        ref={gridRef}
        className={`files-split${selected ? " files-preview-open" : ""}`}
        style={{
          display: "grid",
          gridTemplateColumns: `${treeWidth}px 1fr`,
          height: "calc(100vh - var(--topbar-h))",
        }}
      >
        {/* Left pane: toolbar + windowed tree */}
        <div
          className="files-tree-pane"
          style={{
            position: "relative",
            borderRight: "1px solid var(--border)",
            display: "flex",
            flexDirection: "column",
            minHeight: 0,
          }}
        >
          <div
            className="row"
            style={{ padding: "8px 10px 8px 12px", borderBottom: "1px solid var(--border)", gap: 4 }}
            onDragOver={(e) => {
              // Dropping on the header sends the entry to its own agent's root.
              if (!e.dataTransfer.types.includes(DND_MIME)) return;
              e.preventDefault();
              e.dataTransfer.dropEffect = "move";
              setRootDropping(true);
            }}
            onDragLeave={() => setRootDropping(false)}
            onDrop={(e: DragEvent) => {
              e.preventDefault();
              setRootDropping(false);
              const src = decodeDrag(e.dataTransfer.getData(DND_MIME));
              if (src) void moveEntry(src, { agent: src.agent, path: "" });
            }}
          >
            <span
              data-files-header
              style={{
                flex: 1,
                minWidth: 0,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                fontSize: 12,
                fontWeight: 700,
                letterSpacing: 0.4,
                textTransform: "uppercase",
                color: "var(--text-3)",
                padding: "3px 4px",
                borderRadius: "var(--r-xs)",
                background: rootDropping ? "var(--accent-soft)" : "transparent",
                outline: rootDropping ? "1px solid var(--accent)" : "none",
              }}
            >
              {t("tree.agents")}
              {!agentsLoading && agentInfos.length > 0 && (
                <span style={{ marginLeft: 6, fontWeight: 500, color: "var(--text-4)" }}>{agentInfos.length}</span>
              )}
            </span>
            {!readOnly && (
              <>
                <ToolButton
                  title={createTarget ? t("btn.newFile.title") : t("btn.new.noTarget")}
                  disabled={!createTarget}
                  onClick={() => createTarget && promptNew(createTarget.agent, "file", createTarget.dir)}
                >
                  {newFileIcon}
                </ToolButton>
                <ToolButton
                  title={createTarget ? t("btn.newFolder.title") : t("btn.new.noTarget")}
                  disabled={!createTarget}
                  onClick={() => createTarget && promptNew(createTarget.agent, "directory", createTarget.dir)}
                >
                  {newFolderIcon}
                </ToolButton>
                <ToolButton
                  title={createTarget ? t("btn.upload.title") : t("btn.new.noTarget")}
                  disabled={!createTarget || !!upload}
                  onClick={() => createTarget && startUpload(createTarget.agent, createTarget.dir)}
                >
                  <Upload size={15} strokeWidth={1.9} aria-hidden />
                </ToolButton>
                <input
                  ref={uploadInputRef}
                  type="file"
                  multiple
                  hidden
                  data-testid="files-upload-input"
                  onChange={(e) => {
                    const target = uploadTargetRef.current;
                    const files = Array.from(e.target.files ?? []);
                    e.target.value = "";
                    if (target && files.length) void uploadFiles(target.agent, target.dir, files);
                  }}
                />
              </>
            )}
            <ToolButton
              title={showHidden ? t("btn.hideHidden.title") : t("btn.showHidden.title")}
              active={showHidden}
              disabled={noAgents}
              onClick={() => setHiddenVisible(!showHidden)}
            >
              {showHidden ? eyeIcon : eyeOffIcon}
            </ToolButton>
            <ToolButton title={t("btn.collapseAll.title")} disabled={!anyExpanded} onClick={() => setExpanded([])}>
              {collapseIcon}
            </ToolButton>
            <ToolButton
              title={focusedAgent ? t("btn.refresh.agent", { agent: focusedAgent }) : t("btn.refresh")}
              disabled={hydrateKeys.length === 0}
              onClick={() => refreshVisible(focusedAgent ?? undefined)}
            >
              <RotateCw size={15} strokeWidth={1.75} />
            </ToolButton>
          </div>

          {upload && (
            <div className="muted files-upload-status" style={{ padding: "6px 12px", fontSize: 12 }} role="status">
              {t("upload.progress", { name: upload.name, i: String(upload.i), n: String(upload.n), pct: String(upload.pct) })}
            </div>
          )}
          {/* The network search's state. The local narrowing is instant; this
              line is the only sign that other workspaces are being asked. */}
          {query.length >= SEARCH_MIN_LENGTH && (wsSearch.searching || wsSearch.truncated) && (
            <div className="muted files-search-status" style={{ padding: "6px 12px", fontSize: 12 }} role="status">
              {wsSearch.searching
                ? t("search.searching")
                : t("search.truncated", { n: wsSearch.hits.length.toLocaleString() })}
            </div>
          )}
          {noAgents ? (
            <EmptyBlock title={t("empty.noAgents.title")} sub={t("empty.noAgents.sub")} />
          ) : query && rows.length === 0 ? (
            <EmptyBlock title={wsSearch.searching ? t("search.searching") : t("empty.noMatches")} />
          ) : (
            <FileTree
              className="files-tree"
              rows={rows}
              selectedKey={selectedKey}
              focusedKey={focusedKey}
              revealKey={revealKey}
              onRevealed={() => setRevealKey(null)}
              readOnly={readOnly}
              labels={treeLabels}
              handlers={treeHandlers}
              icons={treeIcons}
              statusColor={statusColor}
              initialScrollTop={treeScrollTop}
              onScrollTop={onTreeScroll}
            />
          )}

          {footer && (
            <div
              className="muted"
              style={{
                padding: "8px 14px",
                borderTop: "1px solid var(--border)",
                fontSize: 11.5,
                display: "flex",
                gap: 10,
                flexShrink: 0,
                minWidth: 0,
              }}
            >
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                <strong style={{ fontWeight: 600 }}>{footer.agent}</strong> · {footer.text}
              </span>
            </div>
          )}

          <ResizeHandle active={resizing} title={t("resize.title")} onMouseDown={startResize} onDoubleClick={resetResize} />
        </div>

        {/* Right pane: preview / editor */}
        <div
          className="files-preview-pane"
          style={{ minWidth: 0, minHeight: 0, overflow: "hidden", display: "flex", flexDirection: "column" }}
        >
          {selected ? (
            <PreviewPane
              key={selectedKey ?? ""}
              file={selected}
              content={content?.key === selectedKey ? content : null}
              readOnly={readOnly}
              onClose={() => setSelected(null)}
              onSaved={(text, size) =>
                setContent((cur) => (cur && cur.key === selectedKey ? { ...cur, text, size, totalSize: size } : cur))
              }
              onDelete={() => void deleteEntry(selected.agent, { name: selected.name, path: selected.path, type: "file" })}
              onRetry={() =>
                void previewFile(selected.agent, { name: selected.name, path: selected.path, abs_path: selected.absPath })
              }
            />
          ) : (
            <div style={{ display: "grid", placeItems: "center", height: "100%", textAlign: "center", padding: 24 }}>
              <div>
                <div style={{ color: "var(--text-5)", display: "flex", justifyContent: "center", marginBottom: 10 }}>
                  <svg viewBox="0 0 24 24" width="40" height="40" fill="none" stroke="currentColor" strokeWidth={1.5}>
                    <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
                    <path d="M14 3v6h6" />
                  </svg>
                </div>
                <p className="muted" style={{ margin: 0 }}>
                  {t("preview.selectFile")}
                </p>
                <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--text-4)" }}>{t("preview.hint")}</p>
              </div>
            </div>
          )}
        </div>
      </div>

      {menu && <ContextMenu menu={menu} onClose={() => setMenu(null)} />}
      {prompt && <PromptModal prompt={prompt} onClose={() => setPrompt(null)} />}
    </>
  );
}

// ── pane resize handle ───────────────────────────────────────────────────
// A thin vertical grab strip straddling the tree pane's right border. Local
// hover state lives here so hovering the handle never re-renders the file tree.
function ResizeHandle({
  active,
  title,
  onMouseDown,
  onDoubleClick,
}: {
  active: boolean;
  title: string;
  onMouseDown: (e: MouseEvent) => void;
  onDoubleClick: () => void;
}) {
  const [hover, setHover] = useState(false);
  const lit = active || hover;
  return (
    <div
      role="separator"
      aria-orientation="vertical"
      title={title}
      onMouseDown={onMouseDown}
      onDoubleClick={onDoubleClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        position: "absolute",
        top: 0,
        bottom: 0,
        right: -3,
        width: 7,
        zIndex: 6,
        cursor: "col-resize",
        display: "flex",
        justifyContent: "center",
        touchAction: "none",
      }}
    >
      {/* Visual line over the border — brightens while hovered/dragging. */}
      <span
        style={{
          width: 2,
          alignSelf: "stretch",
          background: lit ? "var(--accent)" : "transparent",
          transition: "background var(--t-fast)",
        }}
      />
    </div>
  );
}

// ── toolbar icon button ──────────────────────────────────────────────────
// `active` marks a toggle that is currently on (the hidden-files eye): it lights
// the button and exposes the state to assistive tech, so the toolbar isn't a row
// of identical-looking icons with one of them silently changing what the tree
// lists. Plain actions leave it undefined and get no aria-pressed at all.
function ToolButton({
  title,
  onClick,
  disabled,
  active,
  children,
}: {
  title: string;
  onClick: () => void;
  disabled?: boolean;
  active?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      className="btn ghost"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={title}
      aria-pressed={active}
      style={{
        padding: "5px 7px",
        color: active ? "var(--accent)" : "var(--text-3)",
        background: active ? "var(--accent-soft)" : undefined,
      }}
    >
      {children}
    </button>
  );
}

function EmptyBlock({ title, sub }: { title: string; sub?: string }) {
  return (
    <div style={{ padding: 16, textAlign: "center" }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>{title}</div>
      {sub && (
        <div className="muted" style={{ fontSize: 12 }}>
          {sub}
        </div>
      )}
    </div>
  );
}

// ── segmented control (Preview / Raw toggle) ─────────────────────────────
interface SegmentedOption {
  value: string;
  label: string;
}

function Segmented({
  value,
  options,
  onChange,
}: {
  value: string;
  options: SegmentedOption[];
  onChange: (value: string) => void;
}) {
  return (
    <div
      style={{
        display: "inline-flex",
        padding: 2,
        gap: 2,
        background: "var(--surface-2)",
        borderRadius: "var(--r-sm)",
      }}
    >
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            onClick={() => onChange(o.value)}
            aria-pressed={active}
            style={{
              padding: "4px 12px",
              border: "none",
              borderRadius: "var(--r-xs)",
              cursor: "pointer",
              fontSize: 12,
              fontWeight: active ? 600 : 500,
              background: active ? "var(--accent-gradient-soft)" : "transparent",
              color: active ? "var(--text)" : "var(--text-3)",
              transition: "background var(--t-fast), color var(--t-fast)",
            }}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

// ── preview pane ───────────────────────────────────────────────────────
interface PreviewPaneProps {
  file: SelectedRef;
  /** Loaded text for text kinds; null while loading (or for binary kinds). */
  content: FileContent | null;
  /** Non-admin members get a read-only preview — no edit/download/delete. */
  readOnly: boolean;
  onClose: () => void;
  onSaved: (text: string, size: number) => void;
  onDelete: () => void;
  /** Re-read the file after a failed fetch. */
  onRetry: () => void;
}

function PreviewPane({ file, content, readOnly, onClose, onSaved, onDelete, onRetry }: PreviewPaneProps) {
  const toast = useToast();
  const t = useT(STR);
  const key = nodeKey(file.agent, file.path);
  const cat = useMemo(() => categorize(file.name), [file.name]);
  // Editing mode + Preview/Raw choice persist per file (bounded, see PREFS_CAP).
  const [prefs, setPrefsState] = useState<FilePrefs>(() => getPrefs(key));
  const updatePrefs = useCallback(
    (patch: FilePrefs) => {
      setPrefs(key, patch);
      setPrefsState((p) => ({ ...p, ...patch }));
    },
    [key],
  );
  const isHtml = cat.kind === "code" && isHtmlFile(file.name);
  const isBinary = cat.kind === "binary" || !!content?.binary;
  const isText = TEXT_KINDS.has(cat.kind) && !isBinary;
  const loadingText = (isText || cat.kind === "binary") && content === null;
  const failed = !!content?.error;
  const truncated = !!content?.truncated;
  // Read-only members may view but never edit; neither may anyone edit a head
  // — saving it would write the head back over the whole file.
  const canEdit = !readOnly && isText && !loadingText && !truncated && !failed;
  const editing = !!prefs.editing && canEdit;

  // Restore the preview's scroll position per file, across tab switches.
  const bodyRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = bodyRef.current;
    const saved = prefs.scroll;
    if (el && saved && saved > 0 && !loadingText && Math.abs(el.scrollTop - saved) > 1) el.scrollTop = saved;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadingText, content?.text]);
  const onBodyScroll = useCallback(() => {
    const el = bodyRef.current;
    if (el) setPrefs(key, { scroll: el.scrollTop });
  }, [key]);

  if (editing && content) {
    return (
      <EditorPane
        file={file}
        content={content}
        draft={prefs.draft}
        onDraft={(draft) => setPrefs(key, { draft })}
        onCancel={() => updatePrefs({ editing: false, draft: undefined })}
        onSaved={(text, size) => {
          onSaved(text, size);
          updatePrefs({ editing: false, draft: undefined });
        }}
      />
    );
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(content?.text ?? "");
      toast.push(t("toast.copied"));
    } catch {
      toast.push(t("toast.copyFailed"), "error");
    }
  }

  const sizeLabel = content && !failed ? fmtSize(content.totalSize) : isText || isBinary ? "" : cat.kind;
  const raw = isHtml ? !!prefs.htmlRaw : !!prefs.mdRaw;

  return (
    <>
      <div className="files-pane-head">
        {/* Phone-only: the preview owns the whole column there, so it needs a
            way back — the same action as the trailing ✕, which hides at that
            width rather than offering two controls for one thing. */}
        <button className="btn files-pane-back" onClick={onClose} title={t("btn.back.title")}>
          <ArrowLeft size={13} strokeWidth={1.75} />
          {t("btn.back")}
        </button>
        <div className="files-pane-title">
          <strong style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{file.name}</strong>
          <span className="muted" style={{ fontSize: 12, flexShrink: 0 }}>
            {file.agent}
            {sizeLabel ? ` · ${sizeLabel}` : ""}
            {cat.kind === "code" && cat.lang ? ` · ${cat.lang}` : ""}
          </span>
        </div>
        <div className="files-pane-actions">
          {(cat.kind === "markdown" || isHtml) && !loadingText && !isBinary && (
            <span className="files-pane-seg">
              <Segmented
                value={raw ? "raw" : "preview"}
                options={[
                  { value: "preview", label: t("seg.preview") },
                  { value: "raw", label: t("seg.raw") },
                ]}
                onChange={(v) => updatePrefs(isHtml ? { htmlRaw: v === "raw" } : { mdRaw: v === "raw" })}
              />
            </span>
          )}
          {canEdit && (
            <button
              className="btn ghost icon"
              onClick={() => updatePrefs({ editing: true })}
              title={t("btn.edit.title")}
              aria-label={t("btn.edit")}
            >
              {editIcon}
            </button>
          )}
          {isText && !loadingText && !failed && (
            <button className="btn ghost icon" onClick={() => void copy()} title={t("btn.copy.title")} aria-label={t("btn.copy")}>
              {copyIcon}
            </button>
          )}
          {!readOnly && (
            <a
              className="btn ghost icon"
              href={rawUrl(file.path, file.agent)}
              download={file.name}
              title={t("btn.download.title")}
              aria-label={t("btn.download")}
            >
              {downloadIcon}
            </a>
          )}
          {!readOnly && (
            <button className="btn ghost danger icon" onClick={onDelete} title={t("btn.delete.title")} aria-label={t("btn.delete")}>
              {trashIcon}
            </button>
          )}
          <button className="btn ghost icon files-pane-close" onClick={onClose} title={t("btn.close.title")} aria-label={t("btn.close")}>
            <X size={15} strokeWidth={2} />
          </button>
        </div>
      </div>

      {truncated && content && (
        <div
          role="status"
          data-preview-truncated
          style={{
            padding: "8px 18px",
            fontSize: 12,
            borderBottom: "1px solid var(--border)",
            background: "var(--warn-soft, var(--surface-2))",
            color: "var(--text-2, var(--text))",
          }}
        >
          {t("preview.truncated", { shown: fmtSize(content.text.length), total: fmtSize(content.totalSize) })}
        </div>
      )}

      <div
        ref={bodyRef}
        onScroll={onBodyScroll}
        className="files-preview-body"
        data-fill={FILL_KINDS.has(cat.kind) || (isHtml && !raw) ? "" : undefined}
      >
        {failed && content ? (
          <div style={{ padding: 24, textAlign: "center" }}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>{t("preview.failed.title")}</div>
            <div className="muted" style={{ fontSize: 12, marginBottom: 12, overflowWrap: "anywhere" }}>
              {content.error}
            </div>
            <button className="btn" onClick={onRetry}>
              {t("tree.retry")}
            </button>
          </div>
        ) : (
          <PreviewBody
            file={file}
            kind={isBinary ? "binary" : cat.kind}
            content={content}
            loadingText={loadingText}
            raw={raw}
            readOnly={readOnly}
          />
        )}
      </div>
    </>
  );
}

interface PreviewBodyProps {
  file: SelectedRef;
  kind: FileKind;
  content: FileContent | null;
  loadingText: boolean;
  raw: boolean;
  readOnly: boolean;
}

function PreviewBody({ file, kind, content, loadingText, raw, readOnly }: PreviewBodyProps) {
  const src = rawUrl(file.path, file.agent);
  // An image or video the browser cannot decode (a HEIC on Chrome, an exotic
  // codec) gets the same card as any other binary, not a broken-image glyph.
  const [undecodable, setUndecodable] = useState(false);

  if (kind === "binary" || undecodable) {
    if (kind === "binary" && loadingText) return <div className="spinner" style={{ margin: 24 }} />;
    return <BinaryCard file={file} totalSize={content?.totalSize} readOnly={readOnly} src={src} />;
  }

  if (kind === "image") {
    return (
      <div style={{ display: "grid", placeItems: "center", padding: 24, height: "100%" }}>
        <img
          src={src}
          alt={file.name}
          onError={() => setUndecodable(true)}
          style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain" }}
        />
      </div>
    );
  }
  if (kind === "pdf") {
    return <iframe src={src} title={file.name} style={{ width: "100%", height: "100%", border: "none" }} />;
  }
  if (kind === "video") {
    return (
      <div style={{ display: "grid", placeItems: "center", padding: 24, height: "100%" }}>
        <video
          src={src}
          controls
          playsInline
          preload="metadata"
          onError={() => setUndecodable(true)}
          style={{ maxWidth: "100%", maxHeight: "100%" }}
        />
      </div>
    );
  }
  if (kind === "audio") {
    return (
      <div style={{ display: "grid", placeItems: "center", padding: 24, height: "100%" }}>
        <audio src={src} controls style={{ width: "min(480px, 90%)" }} />
      </div>
    );
  }

  if (loadingText || !content) {
    return <div className="spinner" style={{ margin: 24 }} />;
  }

  if (kind === "markdown" && !raw) {
    return (
      <div style={{ padding: 18, maxWidth: 820, lineHeight: 1.6 }}>
        <Markdown text={content.text} />
      </div>
    );
  }

  if (kind === "code" && isHtmlFile(file.name) && !raw) {
    return <HtmlPreviewFrame file={file} html={content.text} />;
  }

  return (
    <pre
      style={{
        margin: 0,
        padding: 18,
        fontFamily: "var(--font-mono)",
        fontSize: 13,
        lineHeight: 1.55,
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
        background: kind === "code" ? "var(--code-bg)" : "transparent",
      }}
    >
      {content.text}
    </pre>
  );
}

/** What a file with no inline renderer shows instead of its bytes. */
function BinaryCard({
  file,
  totalSize,
  readOnly,
  src,
}: {
  file: SelectedRef;
  totalSize?: number;
  readOnly: boolean;
  src: string;
}) {
  const t = useT(STR);
  const ext = extOf(file.name);
  const meta = [ext ? ext.toUpperCase() : t("preview.binary.noExt"), totalSize != null ? fmtSize(totalSize) : ""]
    .filter(Boolean)
    .join(" · ");
  return (
    <div
      data-preview-binary
      style={{ display: "grid", placeItems: "center", alignContent: "center", gap: 6, padding: 24, height: "100%", textAlign: "center" }}
    >
      <FileQuestion size={40} strokeWidth={1.25} className="muted" aria-hidden />
      <div style={{ fontWeight: 600, marginTop: 6 }}>{t("preview.binary.title")}</div>
      <div className="muted" style={{ fontSize: 12 }}>
        {meta}
      </div>
      <div className="muted" style={{ fontSize: 12, maxWidth: 360 }}>
        {t(readOnly ? "preview.binary.subReadOnly" : "preview.binary.sub")}
      </div>
      {!readOnly && (
        <a className="btn" href={src} download={file.name} style={{ marginTop: 10 }}>
          {downloadIcon}
          {t("btn.download")}
        </a>
      )}
    </div>
  );
}

/**
 * Live HTML preview. The srcdoc document runs on an opaque origin — `sandbox`
 * must NEVER include `allow-same-origin`: with it, agent-authored HTML would
 * execute on the dashboard origin with the session cookie and credentialed
 * /api access. buildHtmlSrcdoc pre-inlines relative CSS/JS/img references,
 * which the opaque origin could not fetch itself (no cookie, no ?agent=).
 */
function HtmlPreviewFrame({ file, html }: { file: SelectedRef; html: string }) {
  const [doc, setDoc] = useState<string | null>(null);

  useEffect(() => {
    let stale = false;
    setDoc(null);
    void buildHtmlSrcdoc(html, file.path, file.agent).then((d) => {
      if (!stale) setDoc(d);
    });
    return () => {
      stale = true;
    };
  }, [html, file.path, file.agent]);

  if (doc === null) return <div className="spinner" style={{ margin: 24 }} />;
  return (
    <iframe
      srcDoc={doc}
      sandbox="allow-scripts allow-forms allow-modals"
      title={file.name}
      style={{ width: "100%", height: "100%", border: "none", background: "#fff", colorScheme: "light" }}
    />
  );
}

// ── editor pane ────────────────────────────────────────────────────────
interface EditorPaneProps {
  file: SelectedRef;
  content: FileContent;
  /** An unsaved draft from an earlier visit, if any. */
  draft: string | undefined;
  onDraft: (draft: string) => void;
  onCancel: () => void;
  onSaved: (text: string, size: number) => void;
}

function EditorPane({ file, content, draft, onDraft, onCancel, onSaved }: EditorPaneProps) {
  const toast = useToast();
  const t = useT(STR);
  const [value, setValue] = useState<string>(draft ?? content.text);
  const [status, setStatus] = useState<"editing" | "saving">("editing");
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    taRef.current?.focus();
  }, []);

  const dirty = value !== content.text;

  const save = useCallback(async () => {
    setStatus("saving");
    try {
      const res = await api.put<FileWriteResponse>(
        `/api/files/${encodePath(file.path)}${agentQuery(file.agent)}`,
        { content: value },
      );
      toast.push(t("toast.saved"));
      onSaved(value, res.size);
    } catch (e) {
      toast.push(t("toast.saveFailed", { detail: errMessage(e, t("toast.writeError")) }), "error");
      setStatus("editing");
    }
  }, [file.agent, file.path, value, toast, onSaved, t]);

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      void save();
    } else if (e.key === "Escape") {
      e.preventDefault();
      onCancel();
    }
  }

  const saving = status === "saving";

  return (
    <>
      <div className="files-pane-head">
        <div className="files-pane-title">
          <strong style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{file.name}</strong>
          <span className="muted" style={{ fontSize: 12, display: "inline-flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
            {dirty && !saving && (
              <span aria-hidden style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--accent)" }} />
            )}
            {saving ? t("status.saving") : t("status.editing")}
          </span>
        </div>
        <div className="files-pane-actions">
          <button className="btn primary" disabled={saving} onClick={() => void save()}>
            {t("btn.save")}
          </button>
          <button className="btn" disabled={saving} onClick={onCancel}>
            {t("btn.cancel")}
          </button>
        </div>
      </div>
      <textarea
        ref={taRef}
        className="input"
        value={value}
        onChange={(e) => {
          setValue(e.target.value);
          onDraft(e.target.value);
        }}
        onKeyDown={onKeyDown}
        spellCheck={false}
        style={{
          flex: 1,
          minHeight: 0,
          border: "none",
          borderRadius: 0,
          resize: "none",
          fontFamily: "var(--font-mono)",
          fontSize: 13,
          lineHeight: 1.55,
          padding: 18,
        }}
      />
    </>
  );
}
