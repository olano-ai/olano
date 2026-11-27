import { type ReactNode } from "react";
import { THEMES, setTheme, type Theme } from "../core/theme";
import { useTheme } from "../core/useTheme";
import { LANGS, useI18n, type Lang } from "../core/i18n";
import { SunIcon, LanguageIcon } from "./icons";

// Theme and language as one compact row each, instead of one clickable row per
// value. The profile/account menu carries three themes and ten languages: as
// lists that is thirteen rows, tall enough to push the menu past the viewport
// (the simple view had to cap it at 60vh and scroll it) and to bury the actions
// underneath. A native <select> renders its own popup outside the menu box, so
// the menu stays short and nothing it opens can be clipped by an ancestor.
//
// Both preferences are per-browser (localStorage), so neither is wired to a
// server-side form — the change applies on selection, with no Save step.

function PrefRow({ icon, label, children }: { icon: ReactNode; label: string; children: ReactNode }) {
  return (
    <label className="menu-select-row">
      <span className="menu-select-label">
        {icon} {label}
      </span>
      {children}
    </label>
  );
}

export function ThemeSelect({ label }: { label: string }) {
  const { t } = useI18n();
  const theme = useTheme();
  return (
    <PrefRow icon={<SunIcon size={15} strokeWidth={1.75} />} label={label}>
      <select
        className="input menu-select"
        aria-label={label}
        value={theme}
        onChange={(e) => {
          setTheme(e.target.value as Theme);
        }}
      >
        {THEMES.map((id) => (
          <option key={id} value={id}>
            {t(`theme.${id}`)}
          </option>
        ))}
      </select>
    </PrefRow>
  );
}

export function LanguageSelect({ label }: { label: string }) {
  const { lang, setLang } = useI18n();
  return (
    <PrefRow icon={<LanguageIcon size={15} strokeWidth={1.75} />} label={label}>
      <select
        className="input menu-select"
        aria-label={label}
        value={lang}
        onChange={(e) => setLang(e.target.value as Lang)}
      >
        {LANGS.map((l) => (
          <option key={l.id} value={l.id}>
            {l.label}
          </option>
        ))}
      </select>
    </PrefRow>
  );
}
