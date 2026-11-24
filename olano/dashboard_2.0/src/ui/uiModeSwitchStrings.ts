import type { LocalDict } from "../core/i18n";

/**
 * Copy for the shell switch. The row is labelled with the STATE the switch
 * controls ("Simple view"); the cover strings name the destination while the
 * swap happens. Every string says "view", never "mode" or "access" — see the
 * note in UiModeSwitch.tsx.
 */
export const STR: LocalDict = {
  en: {
    "switch.simpleView": "Simple view",
    "switch.help":
      "Changes which screen you land on: the full dashboard (agents, config, tools, logs) "
      + "or the simple view (just your agents and your conversations). It changes what you see "
      + "and nothing about what you are allowed to do — you keep every permission either way, "
      + "and you can switch back from the same menu in the other view. Only admins can switch "
      + "themselves; everyone else is set by an admin in Admin → Users.",
    "cover.toSimple": "Opening the simple view…",
    "cover.toAdvanced": "Opening the full dashboard…",
  },
  zh: {
    "switch.simpleView": "简易视图",
    "switch.help":
      "更改您登录后看到的界面：完整仪表板（智能体、配置、工具、日志）或简易视图"
      + "（仅显示您的智能体和对话）。这只改变您看到的内容，不改变您的权限 —— "
      + "两种视图下权限完全相同，并且可以从另一视图的相同菜单切换回来。"
      + "只有管理员可以自行切换；其他人由管理员在“管理 → 用户”中设置。",
    "cover.toSimple": "正在打开简易视图…",
    "cover.toAdvanced": "正在打开完整仪表板…",
  },
  es: {
    "switch.simpleView": "Vista simple",
    "switch.help":
      "Cambia la pantalla en la que aterrizas: el panel completo (agentes, configuración, "
      + "herramientas, registros) o la vista simple (solo tus agentes y tus conversaciones). "
      + "Cambia lo que ves y nada de lo que puedes hacer: conservas todos tus permisos en "
      + "ambas, y puedes volver desde el mismo menú en la otra vista. Solo los administradores "
      + "pueden cambiar por sí mismos; al resto lo define un administrador en Admin → Usuarios.",
    "cover.toSimple": "Abriendo la vista simple…",
    "cover.toAdvanced": "Abriendo el panel completo…",
  },
};
