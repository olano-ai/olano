import type { LocalDict } from "../../core/i18n";
import type { Presence, PresenceInput } from "./presence";

// What each presence state means, in words, for both shells' agent lists
// (presence.ts holds the rules). One table so the dashboard sidebar and the
// simple rail describe the same agent the same way.
//
// Every state says what it means for the person reading it: whether the agent
// will answer, and what has to happen before it does.
export const PRESENCE_STR: LocalDict = {
  en: {
    "presence.online": "Online: answers right away.",
    "presence.starting": "Starting up: it will answer in a moment.",
    "presence.stopping": "Stopping: it will stop answering in a moment.",
    "presence.error": "Failed to start, so it won't answer until it is restarted: {reason}",
    "presence.errorNoReason": "Failed to start, so it won't answer until it is restarted.",
    "presence.blocked": "Not available on this deployment, so it won't answer: {reason}",
    "presence.blockedNoReason": "Not available on this deployment, so it won't answer.",
    "presence.paused": "Paused: someone stopped it on purpose, and it won't answer until it is started again.",
    "presence.offline": "Offline: it isn't running right now, so it won't answer until it is started.",
    "presence.asleepCount": "{n} offline",
    "presence.asleepShow": "Show the {n} offline agents",
    "presence.asleepHide": "Hide the {n} offline agents",
    "presence.asleepHelp":
      "Agents that are paused or not running, tucked away so the ones that can answer stay on top. Click to show or hide them; the choice is remembered on this device. Pinned agents, the agent you have open and agents with unread replies always show, whatever their state. Searching always shows every match.",
    "presence.asleepHelpGroups":
      "Agents that are paused or not running, tucked away so the ones that can answer stay on top. Click to show or hide them; the choice is remembered on this device. Pinned agents, agents in your own groups, the agent you have open and agents with unread replies always show, whatever their state.",
  },
  zh: {
    "presence.online": "在线：会立即回复。",
    "presence.starting": "正在启动：稍后即可回复。",
    "presence.stopping": "正在停止：稍后将不再回复。",
    "presence.error": "启动失败，重新启动前不会回复：{reason}",
    "presence.errorNoReason": "启动失败，重新启动前不会回复。",
    "presence.blocked": "在此部署上不可用，因此不会回复：{reason}",
    "presence.blockedNoReason": "在此部署上不可用，因此不会回复。",
    "presence.paused": "已暂停：有人特意停止了它，再次启动前不会回复。",
    "presence.offline": "离线：当前未运行，启动前不会回复。",
    "presence.asleepCount": "{n} 个离线",
    "presence.asleepShow": "显示 {n} 个离线智能体",
    "presence.asleepHide": "隐藏 {n} 个离线智能体",
    "presence.asleepHelp":
      "已暂停或未运行的智能体收在这里，让能回复的智能体留在上方。点击可显示或隐藏；此选择保存在本设备上。已置顶的智能体、当前打开的智能体以及有未读回复的智能体始终显示，不论其状态。搜索时始终显示所有匹配项。",
    "presence.asleepHelpGroups":
      "已暂停或未运行的智能体收在这里，让能回复的智能体留在上方。点击可显示或隐藏；此选择保存在本设备上。已置顶的智能体、你自建分组中的智能体、当前打开的智能体以及有未读回复的智能体始终显示，不论其状态。",
  },
  es: {
    "presence.online": "En línea: responde enseguida.",
    "presence.starting": "Iniciándose: responderá en un momento.",
    "presence.stopping": "Deteniéndose: dejará de responder en un momento.",
    "presence.error": "No pudo iniciarse, así que no responderá hasta que se reinicie: {reason}",
    "presence.errorNoReason": "No pudo iniciarse, así que no responderá hasta que se reinicie.",
    "presence.blocked": "No está disponible en este despliegue, así que no responderá: {reason}",
    "presence.blockedNoReason": "No está disponible en este despliegue, así que no responderá.",
    "presence.paused": "En pausa: alguien lo detuvo a propósito y no responderá hasta que se vuelva a iniciar.",
    "presence.offline": "Sin conexión: no se está ejecutando, así que no responderá hasta que se inicie.",
    "presence.asleepCount": "{n} sin conexión",
    "presence.asleepShow": "Mostrar los {n} agentes sin conexión",
    "presence.asleepHide": "Ocultar los {n} agentes sin conexión",
    "presence.asleepHelp":
      "Agentes en pausa o que no se están ejecutando, recogidos aquí para que los que pueden responder queden arriba. Haz clic para mostrarlos u ocultarlos; la elección se recuerda en este dispositivo. Los agentes fijados, el agente que tienes abierto y los que tienen respuestas sin leer siempre se muestran, sea cual sea su estado. Al buscar se muestran siempre todas las coincidencias.",
    "presence.asleepHelpGroups":
      "Agentes en pausa o que no se están ejecutando, recogidos aquí para que los que pueden responder queden arriba. Haz clic para mostrarlos u ocultarlos; la elección se recuerda en este dispositivo. Los agentes fijados, los de tus propios grupos, el agente que tienes abierto y los que tienen respuestas sin leer siempre se muestran, sea cual sea su estado.",
  },
};

type T = (key: string, vars?: Record<string, string>) => string;

/** A reason the roster sent, or "" for anything that is not text. */
function reasonOf(v: unknown): string {
  return typeof v === "string" ? v.trim() : "";
}

/** The one-line explanation of an agent's state, for a row's tooltip. */
export function presenceText(t: T, p: Presence, a: PresenceInput): string {
  if (p === "error") {
    const reason = reasonOf(a.error);
    return reason ? t("presence.error", { reason }) : t("presence.errorNoReason");
  }
  if (p === "blocked") {
    const reason = reasonOf(a.blocked_reason);
    return reason ? t("presence.blocked", { reason }) : t("presence.blockedNoReason");
  }
  return t(`presence.${p}`);
}
