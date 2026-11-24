import type { LocalDict } from "../../core/i18n";

// Thumbs up / down under a reply (ReplyFeedback.tsx) and the Insights count.
export const STR: LocalDict = {
  en: {
    "fb.up": "Good reply",
    "fb.down": "Bad reply",
    "fb.upTaken": "You marked this reply good. Click again to take it back.",
    "fb.downTaken": "You marked this reply bad. Click again to take it back.",
    "fb.prompt": "What went wrong? (optional)",
    "fb.placeholder": "The answer missed the point, used the wrong file, ...",
    "fb.help":
      "Your vote is kept on this deployment and counted in Insights. Only you see your own vote. When this agent's turns are traced to Langfuse, the vote is also sent there as a user_feedback score on this reply's trace; the comment is sent only when traces include full content. Up to 2000 characters.",
    "fb.send": "Send",
    "fb.skip": "Skip",
    "fb.failed": "Your vote could not be saved. Try again.",
    "fb.insights.title": "Reply feedback",
    "fb.insights.hint":
      "Thumbs people gave agents' replies in the chat during this period, per agent. A changed vote counts once, as it stands now; a vote taken back does not count. Kept on this deployment; Langfuse is not needed.",
    "fb.insights.empty": "No votes in this period.",
    "fb.insights.up": "Up",
    "fb.insights.down": "Down",
  },
  zh: {
    "fb.up": "回复很好",
    "fb.down": "回复不好",
    "fb.upTaken": "你已将此回复标记为好。再次点击可撤销。",
    "fb.downTaken": "你已将此回复标记为不好。再次点击可撤销。",
    "fb.prompt": "哪里有问题？（可选）",
    "fb.placeholder": "答案没抓住重点、用错了文件……",
    "fb.help":
      "你的投票保存在此部署中，并计入洞察。只有你能看到自己的投票。当此智能体的轮次被追踪到 Langfuse 时，投票也会作为 user_feedback 评分发送到该回复的追踪上；只有当追踪包含完整内容时才会发送评论。最多 2000 个字符。",
    "fb.send": "发送",
    "fb.skip": "跳过",
    "fb.failed": "无法保存你的投票，请重试。",
    "fb.insights.title": "回复反馈",
    "fb.insights.hint":
      "此期间人们在聊天中对各智能体回复的点赞与点踩，按智能体统计。修改过的投票按当前结果计一次；撤销的投票不计入。保存在此部署中，无需 Langfuse。",
    "fb.insights.empty": "此期间没有投票。",
    "fb.insights.up": "赞",
    "fb.insights.down": "踩",
  },
  es: {
    "fb.up": "Buena respuesta",
    "fb.down": "Mala respuesta",
    "fb.upTaken": "Marcaste esta respuesta como buena. Haz clic de nuevo para retirarlo.",
    "fb.downTaken": "Marcaste esta respuesta como mala. Haz clic de nuevo para retirarlo.",
    "fb.prompt": "¿Qué salió mal? (opcional)",
    "fb.placeholder": "La respuesta no fue al grano, usó el archivo equivocado, ...",
    "fb.help":
      "Tu voto se guarda en esta implementación y cuenta en Insights. Solo tú ves tu propio voto. Cuando los turnos de este agente se rastrean en Langfuse, el voto también se envía allí como una puntuación user_feedback sobre la traza de esta respuesta; el comentario se envía solo cuando las trazas incluyen el contenido completo. Hasta 2000 caracteres.",
    "fb.send": "Enviar",
    "fb.skip": "Omitir",
    "fb.failed": "No se pudo guardar tu voto. Inténtalo de nuevo.",
    "fb.insights.title": "Valoración de respuestas",
    "fb.insights.hint":
      "Pulgares que la gente dio a las respuestas de los agentes en el chat durante este periodo, por agente. Un voto cambiado cuenta una vez, como está ahora; un voto retirado no cuenta. Se guarda en esta implementación; no hace falta Langfuse.",
    "fb.insights.empty": "No hay votos en este periodo.",
    "fb.insights.up": "A favor",
    "fb.insights.down": "En contra",
  },
};
