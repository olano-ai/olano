/**
 * Copying text. WKWebView allows navigator.clipboard only from a user gesture
 * on a secure origin, which the bundled app is (capacitor://localhost); the
 * textarea fallback covers older WebViews.
 */

export const Clipboard = {
  async write(text: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Fall back below.
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
    } finally {
      area.remove();
    }
  },
};
