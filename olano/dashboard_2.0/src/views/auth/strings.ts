import type { LocalDict } from "../../core/i18n";

export const STR: LocalDict = {
  en: {
    // shared field labels
    "field.email": "Email",
    "field.password": "Password",

    // login — sign in
    "signin.markAlt": "Olano",
    "signin.title": "Sign in",
    "signin.subtitle": "Welcome back to Olano.",
    "signin.titleSimple": "Welcome back",
    "signin.subtitleSimple": "Sign in to chat with your bots.",
    "signin.submit": "Sign in",
    "signin.submitting": "Signing in…",
    "signin.forgot": "Forgot password?",
    "signin.err.credentials": "Wrong email or password.",
    "signin.err.generic": "Sign in failed. Try again.",
    "signin.err.offline": "Can't reach the server. It may be starting up — please wait a moment.",
    "signin.starting": "Waiting for server…",
    "signin.waiting": "The server is still starting up — sign in will be enabled once it's online.",
    "status.online": "Online",
    "status.checking": "Checking…",
    "status.starting": "Server starting…",
    // login: first sign-in on a just-deployed managed instance, while the
    // provisioned account still has the temporary password the cloud generated
    "signin.first.subtitle": "Welcome to your new Olano instance.",
    "signin.first.title": "First sign-in",
    "signin.first.body":
      "Use the email address of your Olano Cloud account and the temporary password. You can get the temporary password from the email we just sent you, or from your deployment's Security tab:",
    "signin.first.linkTitle": "Opens your deployment's Security tab in Olano Cloud in a new tab",
    "forgot.first.body":
      "This instance still uses the temporary password it was created with. You can get it from the email we just sent you, or from your deployment's Security tab:",

    // login — forgot password
    "forgot.title": "Forgot password?",
    "forgot.body":
      "Password resets are handled by your admin. Ask them to send you a one-time reset link (and QR) — you'll open it and choose a new password.",
    "forgot.cliHint": "Admins: reset your own password from the CLI with",
    "forgot.back": "Back to sign in",

    // bootstrap — create owner account
    "bootstrap.title": "Create your account",
    "bootstrap.subtitle":
      "Welcome to Olano. This first account is the owner/admin — it owns every existing agent and manages users. Everyone else joins by invite.",
    "bootstrap.name": "Your name",
    "bootstrap.submit": "Create owner account",
    "bootstrap.submitting": "Creating…",
    "bootstrap.err": "Could not create the owner account.",
    "bootstrap.err.usernameTaken": "That handle is already taken — pick another.",
    "field.handle": "Handle",
    "field.handleOptional": "optional",
    "field.handlePlaceholder": "e.g. carlos-m",
    "field.handleHelp":
      "Your permanent public handle: lowercase letters, digits and dashes. It identifies you " +
      "in the issue tracker and cannot be changed later. Leave empty to generate one from " +
      "your name. Not a login — you sign in with your email.",
    "handle.available": "Available",
    "handle.taken": "Already taken — try {suggestion}",
    "handle.savedAs": "Will be saved as {normalized}",
    "handle.checking": "Checking…",
    "invite.handleLine": "Your handle: @{username} (permanent — it identifies you in issues)",

    // accept invite
    "invite.invalid.title": "Invite not valid",
    "invite.invalid.body":
      "This invite link is invalid, already used, or expired. Ask your admin to send a new one.",
    "invite.title": "Set your password",
    "invite.welcome": "Welcome{name}! Choose a password for ",
    "invite.newPassword": "New password",
    "invite.confirmPassword": "Confirm password",
    "invite.submit": "Set password & sign in",
    "invite.submitting": "Setting up…",
    "invite.err.mismatch": "Passwords don't match.",
    "invite.err.invalid": "This invite is invalid or expired.",
    "invite.err.generic": "Something went wrong.",
  },
  zh: {
    "field.email": "邮箱",
    "field.password": "密码",

    "signin.markAlt": "Olano",
    "signin.title": "登录",
    "signin.subtitle": "欢迎回到 Olano。",
    "signin.titleSimple": "欢迎回来",
    "signin.subtitleSimple": "登录后即可与您的机器人聊天。",
    "signin.submit": "登录",
    "signin.submitting": "正在登录…",
    "signin.forgot": "忘记密码？",
    "signin.err.credentials": "邮箱或密码错误。",
    "signin.err.generic": "登录失败，请重试。",
    "signin.err.offline": "无法连接服务器，可能正在启动，请稍候。",
    "signin.starting": "等待服务器…",
    "signin.waiting": "服务器正在启动——上线后即可登录。",
    "status.online": "在线",
    "status.checking": "检查中…",
    "status.starting": "服务器启动中…",
    "signin.first.subtitle": "欢迎使用你的新 Olano 实例。",
    "signin.first.title": "首次登录",
    "signin.first.body":
      "请使用你的 Olano Cloud 账户邮箱和临时密码登录。临时密码可以在我们刚刚发给你的邮件中找到，也可以在该部署的“安全”标签页中获取：",
    "signin.first.linkTitle": "在新标签页中打开该部署在 Olano Cloud 中的“安全”标签页",
    "forgot.first.body":
      "此实例仍在使用创建时生成的临时密码。你可以在我们刚刚发给你的邮件中找到它，也可以在该部署的“安全”标签页中获取：",

    "forgot.title": "忘记密码？",
    "forgot.body":
      "密码重置由管理员处理。请管理员向你发送一次性重置链接（及二维码）——打开后即可设置新密码。",
    "forgot.cliHint": "管理员：可在命令行中重置自己的密码：",
    "forgot.back": "返回登录",

    "bootstrap.title": "创建账户",
    "bootstrap.subtitle":
      "欢迎使用 Olano。首个账户即为所有者/管理员——它拥有所有现有智能体并负责管理用户。其他人需通过邀请加入。",
    "bootstrap.name": "你的姓名",
    "bootstrap.submit": "创建所有者账户",
    "bootstrap.submitting": "正在创建…",
    "bootstrap.err": "无法创建所有者账户。",
    "bootstrap.err.usernameTaken": "该标识符已被占用——请换一个。",
    "field.handle": "标识符",
    "field.handleOptional": "可选",
    "field.handlePlaceholder": "例如 carlos-m",
    "field.handleHelp":
      "你的永久公开标识符：仅限小写字母、数字和短横线。它用于在问题追踪器中标识你，创建后无法更改。" +
      "留空则根据姓名自动生成。它不是登录名——登录仍使用邮箱。",
    "handle.available": "可用",
    "handle.taken": "已被占用——试试 {suggestion}",
    "handle.savedAs": "将保存为 {normalized}",
    "handle.checking": "检查中…",
    "invite.handleLine": "你的标识符：@{username}（永久——用于在问题中标识你）",

    "invite.invalid.title": "邀请无效",
    "invite.invalid.body": "该邀请链接无效、已被使用或已过期。请联系管理员重新发送。",
    "invite.title": "设置密码",
    "invite.welcome": "欢迎{name}！请为以下账户设置密码：",
    "invite.newPassword": "新密码",
    "invite.confirmPassword": "确认密码",
    "invite.submit": "设置密码并登录",
    "invite.submitting": "正在设置…",
    "invite.err.mismatch": "两次输入的密码不一致。",
    "invite.err.invalid": "该邀请无效或已过期。",
    "invite.err.generic": "出现了一些问题。",
  },
  es: {
    "field.email": "Correo electrónico",
    "field.password": "Contraseña",

    "signin.markAlt": "Olano",
    "signin.title": "Iniciar sesión",
    "signin.subtitle": "Bienvenido de nuevo a Olano.",
    "signin.titleSimple": "Hola de nuevo",
    "signin.subtitleSimple": "Inicia sesión para chatear con tus bots.",
    "signin.submit": "Iniciar sesión",
    "signin.submitting": "Iniciando sesión…",
    "signin.forgot": "¿Olvidaste tu contraseña?",
    "signin.err.credentials": "Correo o contraseña incorrectos.",
    "signin.err.generic": "No se pudo iniciar sesión. Inténtalo de nuevo.",
    "signin.err.offline": "No se puede conectar con el servidor. Puede estar iniciándose; espera un momento.",
    "signin.starting": "Esperando al servidor…",
    "signin.waiting": "El servidor se está iniciando; podrás iniciar sesión cuando esté en línea.",
    "status.online": "En línea",
    "status.checking": "Comprobando…",
    "status.starting": "Iniciando servidor…",
    "signin.first.subtitle": "Te damos la bienvenida a tu nueva instancia de Olano.",
    "signin.first.title": "Primer inicio de sesión",
    "signin.first.body":
      "Usa el correo de tu cuenta de Olano Cloud y la contraseña temporal. Puedes obtener la contraseña temporal en el correo que te acabamos de enviar o en la pestaña Seguridad de tu despliegue:",
    "signin.first.linkTitle": "Abre la pestaña Seguridad de tu despliegue en Olano Cloud en una pestaña nueva",
    "forgot.first.body":
      "Esta instancia todavía usa la contraseña temporal con la que se creó. Puedes obtenerla en el correo que te acabamos de enviar o en la pestaña Seguridad de tu despliegue:",

    "forgot.title": "¿Olvidaste tu contraseña?",
    "forgot.body":
      "El restablecimiento de contraseñas lo gestiona tu administrador. Pídele que te envíe un enlace de restablecimiento de un solo uso (y un código QR); lo abrirás y elegirás una nueva contraseña.",
    "forgot.cliHint":
      "Administradores: restablezcan su propia contraseña desde la CLI con",
    "forgot.back": "Volver a iniciar sesión",

    "bootstrap.title": "Crea tu cuenta",
    "bootstrap.subtitle":
      "Bienvenido a Olano. Esta primera cuenta es la del propietario/administrador: es dueña de todos los agentes existentes y gestiona a los usuarios. Los demás se unen por invitación.",
    "bootstrap.name": "Tu nombre",
    "bootstrap.submit": "Crear cuenta de propietario",
    "bootstrap.submitting": "Creando…",
    "bootstrap.err": "No se pudo crear la cuenta de propietario.",
    "bootstrap.err.usernameTaken": "Ese identificador ya está en uso — elige otro.",
    "field.handle": "Identificador",
    "field.handleOptional": "opcional",
    "field.handlePlaceholder": "p. ej. carlos-m",
    "field.handleHelp":
      "Tu identificador público permanente: solo minúsculas, dígitos y guiones. Te identifica " +
      "en el gestor de incidencias y no se puede cambiar después. Déjalo vacío para generarlo " +
      "a partir de tu nombre. No es un usuario de acceso: se entra con el correo.",
    "handle.available": "Disponible",
    "handle.taken": "Ya está en uso — prueba {suggestion}",
    "handle.savedAs": "Se guardará como {normalized}",
    "handle.checking": "Comprobando…",
    "invite.handleLine": "Tu identificador: @{username} (permanente — te identifica en incidencias)",

    "invite.invalid.title": "Invitación no válida",
    "invite.invalid.body":
      "Este enlace de invitación no es válido, ya se usó o ha caducado. Pide a tu administrador que envíe uno nuevo.",
    "invite.title": "Establece tu contraseña",
    "invite.welcome": "¡Bienvenido{name}! Elige una contraseña para ",
    "invite.newPassword": "Nueva contraseña",
    "invite.confirmPassword": "Confirmar contraseña",
    "invite.submit": "Establecer contraseña e iniciar sesión",
    "invite.submitting": "Configurando…",
    "invite.err.mismatch": "Las contraseñas no coinciden.",
    "invite.err.invalid": "Esta invitación no es válida o ha caducado.",
    "invite.err.generic": "Algo salió mal.",
  },
};
