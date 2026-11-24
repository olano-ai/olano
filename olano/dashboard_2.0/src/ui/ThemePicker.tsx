import { useState } from "react";
import { THEMES, getTheme, setTheme, type Theme } from "../core/theme";
import { useI18n } from "../core/i18n";
import { SunIcon, MoonIcon, ContrastIcon, PaletteIcon, ZapIcon, CheckIcon } from "./icons";

// One glyph per theme, shared with the sidebar's cycle toggle so the same theme
// never wears two icons. The three originals keep the sun / moon / contrast
// trio, volt gets the bolt its name promises; the other coloured themes share
// a palette, because what distinguishes them is a colour a monochrome icon
// cannot carry — the label does that.
export function themeIcon(id: Theme, size = 15) {
  if (id === "light") return <MoonIcon size={size} />;
  if (id === "noir") return <ContrastIcon size={size} />;
  if (id === "volt") return <ZapIcon size={size} />;
  if (id === "dark") return <SunIcon size={size} />;
  return <PaletteIcon size={size} />;
}

// Reusable theme selector — one row per theme with active check + icon + label,
// mirroring the language picker in the profile menu. Writes straight through to
// setTheme (localStorage + <html data-theme>). Theme is a local, per-browser
// preference, so this is intentionally NOT wired to any server-side form.
export function ThemePicker({ onChange }: { onChange?: (theme: Theme) => void }) {
  const { t } = useI18n();
  const [theme, setThemeState] = useState<Theme>(getTheme());
  return (
    <>
      {THEMES.map((id) => (
        <button
          key={id}
          type="button"
          className="menu-item"
          onClick={() => {
            setTheme(id);
            setThemeState(id);
            onChange?.(id);
          }}
        >
          <span style={{ width: 15, display: "inline-flex" }}>{theme === id && <CheckIcon size={14} />}</span>
          {themeIcon(id)}
          {t(`theme.${id}`)}
        </button>
      ))}
    </>
  );
}
