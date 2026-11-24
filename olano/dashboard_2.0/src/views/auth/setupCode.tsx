import { useEffect, useState } from "react";
import { api } from "../../core/api";
import { useT, type LocalDict } from "../../core/i18n";

/**
 * The one-time setup code a self-hosted box asks for before it creates its
 * first admin (olano/runtime/setup_code.py). Shared by the two screens that
 * create that account: the sign-in gate's BootstrapView (sign-in required, no
 * accounts yet) and the onboarding wizard's profile step (single-user box).
 *
 * The server decides whether a code is needed; GET /api/auth/setup-status
 * answers before anyone is signed in. A failed probe reads as "needed": the
 * field is harmless when the server ignores it, and hiding it when it is
 * required would leave the owner with an error they cannot act on.
 */

const STR: LocalDict = {
  en: {
    "setupCode.label": "Setup code",
    "setupCode.placeholder": "ABCD-EFGH-JKMN",
    "setupCode.hint":
      "A one-time code that proves you installed this box, so nobody else who reaches this address first can claim it. It is three groups of four letters and digits, like ABCD-EFGH-JKMN; case, spaces and dashes don't matter. Find it in the terminal output of setup.sh (printed next to the address), in the OLANO_SETUP_CODE line of the .env file next to docker-compose.yml, or in the runtime's log (docker compose logs olano, look for FIRST-RUN SETUP CODE). It stops working once this account exists.",
    "setupCode.err.required": "Enter the setup code. It is printed by setup.sh and in the runtime's log.",
    "setupCode.err.invalid":
      "That setup code is not right. Check the setup.sh output, the OLANO_SETUP_CODE line in .env, or docker compose logs olano.",
    "setupCode.err.limited":
      "Too many wrong setup codes. Wait {minutes} min, then try again with the code from setup.sh or the runtime's log.",
  },
  zh: {
    "setupCode.label": "设置码",
    "setupCode.placeholder": "ABCD-EFGH-JKMN",
    "setupCode.hint":
      "一次性代码，用来证明这台服务器是你安装的，防止其他先访问到此地址的人抢先占用。格式为三组各四个字母或数字，例如 ABCD-EFGH-JKMN；大小写、空格和短横线均不影响。可在 setup.sh 的终端输出（打印在地址旁边）、docker-compose.yml 旁边 .env 文件中的 OLANO_SETUP_CODE 一行，或运行时日志（docker compose logs olano，查找 FIRST-RUN SETUP CODE）中找到。账户创建后即失效。",
    "setupCode.err.required": "请输入设置码。它由 setup.sh 打印，也出现在运行时日志中。",
    "setupCode.err.invalid":
      "设置码不正确。请检查 setup.sh 的输出、.env 中的 OLANO_SETUP_CODE 一行，或 docker compose logs olano。",
    "setupCode.err.limited": "错误的设置码过多。请等待 {minutes} 分钟后，再用 setup.sh 或运行时日志中的代码重试。",
  },
  es: {
    "setupCode.label": "Código de configuración",
    "setupCode.placeholder": "ABCD-EFGH-JKMN",
    "setupCode.hint":
      "Un código de un solo uso que demuestra que instalaste este servidor, para que nadie que llegue antes a esta dirección pueda reclamarlo. Son tres grupos de cuatro letras y cifras, como ABCD-EFGH-JKMN; mayúsculas, espacios y guiones dan igual. Está en la salida de setup.sh en la terminal (junto a la dirección), en la línea OLANO_SETUP_CODE del archivo .env junto a docker-compose.yml, o en el registro del runtime (docker compose logs olano, busca FIRST-RUN SETUP CODE). Deja de funcionar en cuanto existe esta cuenta.",
    "setupCode.err.required":
      "Introduce el código de configuración. Lo imprime setup.sh y aparece en el registro del runtime.",
    "setupCode.err.invalid":
      "Ese código de configuración no es correcto. Revisa la salida de setup.sh, la línea OLANO_SETUP_CODE de .env o docker compose logs olano.",
    "setupCode.err.limited":
      "Demasiados códigos incorrectos. Espera {minutes} min y vuelve a intentarlo con el código de setup.sh o del registro.",
  },
};

/** Whether the server wants a setup code. undefined while asking. */
export function useSetupCodeRequired(enabled = true): boolean | undefined {
  const [required, setRequired] = useState<boolean | undefined>(undefined);
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    api
      .get<{ setup_code_required?: boolean }>("/api/auth/setup-status")
      .then((s) => live && setRequired(s.setup_code_required !== false))
      .catch(() => live && setRequired(true));
    return () => {
      live = false;
    };
  }, [enabled]);
  return enabled ? required : false;
}

/** A user-facing message for a bootstrap error detail, or null if not ours. */
export function useSetupCodeError(): (detail: string, retryAfter?: number) => string | null {
  const t = useT(STR);
  return (detail: string, retryAfter?: number) => {
    if (detail === "setup_code_required") return t("setupCode.err.required");
    if (detail === "setup_code_invalid") return t("setupCode.err.invalid");
    if (detail === "setup_code_rate_limited") {
      const minutes = String(Math.max(1, Math.ceil((retryAfter ?? 900) / 60)));
      return t("setupCode.err.limited", { minutes });
    }
    return null;
  };
}

export function SetupCodeField({
  value,
  onChange,
  hintClassName,
  autoFocus,
}: {
  value: string;
  onChange: (v: string) => void;
  /** The hint's class in the host form ("ob-hint" in the wizard). */
  hintClassName?: string;
  autoFocus?: boolean;
}) {
  const t = useT(STR);
  return (
    <div className="field">
      <label htmlFor="olano-setup-code">{t("setupCode.label")}</label>
      <input
        id="olano-setup-code"
        className="input"
        name="setup-code"
        data-testid="setup-code"
        // Not a password manager's business: a one-time value, never reused.
        autoComplete="one-time-code"
        autoCapitalize="characters"
        spellCheck={false}
        placeholder={t("setupCode.placeholder")}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoFocus={autoFocus}
      />
      {hintClassName ? (
        <span className={hintClassName}>{t("setupCode.hint")}</span>
      ) : (
        <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
          {t("setupCode.hint")}
        </p>
      )}
    </div>
  );
}
