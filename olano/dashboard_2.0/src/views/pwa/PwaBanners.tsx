// The two moments the PWA needs a word with the user: a build is waiting, and
// the app can be put on the home screen.
//
// Both are banners rather than toasts because both need a decision. A toast
// auto-dismisses, and an update that vanishes before it is read leaves the tab
// on the old build with no second chance until a reload it has no reason to do.

import { useState } from "react";
import { ArrowUpCircle, Plus, Share, Smartphone, X } from "lucide-react";
import { useT, type LocalDict } from "../../core/i18n";
import { isIOS, isStandalone, usePwaInstall, usePwaUpdate } from "../../core/pwa";
import { MOBILE_QUERY, useMediaQuery } from "../../core/useMediaQuery";

const dict: LocalDict = {
  en: {
    "pwa.update.title": "A new version of Olano is ready.",
    "pwa.update.action": "Reload",
    "pwa.update.dismiss": "Not now",
    "pwa.install.title": "Add Olano to your home screen",
    "pwa.install.body": "Opens full screen, without the browser bar, and keeps you signed in.",
    "pwa.install.action": "Install",
    "pwa.install.dismiss": "Dismiss",
    "pwa.install.ios.step1": "Tap the Share button in the browser toolbar.",
    "pwa.install.ios.step2": "Scroll down and choose “Add to Home Screen”.",
    "pwa.install.ios.step3": "Tap “Add”. Olano appears with your other apps.",
    "pwa.install.ios.how": "How",
  },
  es: {
    "pwa.update.title": "Hay una nueva versión de Olano.",
    "pwa.update.action": "Recargar",
    "pwa.update.dismiss": "Ahora no",
    "pwa.install.title": "Añade Olano a tu pantalla de inicio",
    "pwa.install.body":
      "Se abre a pantalla completa, sin la barra del navegador, y mantiene tu sesión.",
    "pwa.install.action": "Instalar",
    "pwa.install.dismiss": "Descartar",
    "pwa.install.ios.step1": "Toca el botón Compartir en la barra del navegador.",
    "pwa.install.ios.step2": "Baja y elige «Añadir a pantalla de inicio».",
    "pwa.install.ios.step3": "Toca «Añadir». Olano aparecerá con tus otras apps.",
    "pwa.install.ios.how": "Cómo",
  },
};

/** "A new build is waiting" — shown on every viewport, never auto-dismissed. */
function UpdateBanner() {
  const t = useT(dict);
  const { ready, applyUpdate } = usePwaUpdate();
  const [hidden, setHidden] = useState(false);

  if (!ready || hidden) return null;
  return (
    <div className="pwa-banner" role="status">
      <ArrowUpCircle size={17} strokeWidth={1.75} />
      <span className="pwa-banner-text">{t("pwa.update.title")}</span>
      <button className="btn primary sm" onClick={applyUpdate}>
        {t("pwa.update.action")}
      </button>
      <button
        className="btn ghost icon"
        onClick={() => setHidden(true)}
        aria-label={t("pwa.update.dismiss")}
      >
        <X size={15} strokeWidth={2} />
      </button>
    </div>
  );
}

/** "Put this on your home screen" — phones only, and never once installed. */
function InstallBanner() {
  const t = useT(dict);
  const isMobile = useMediaQuery(MOBILE_QUERY);
  const { available, dismissed, dismiss, promptInstall } = usePwaInstall();
  const [showSteps, setShowSteps] = useState(false);
  const ios = isIOS();

  // Already installed → nothing to offer. Desktop is excluded deliberately:
  // the browser is a perfectly good home for the dashboard there, and an
  // install nag on a machine that does not need one is pure noise.
  if (isStandalone() || dismissed || !isMobile) return null;
  // Android and desktop Chromium fire `beforeinstallprompt`, which is the only
  // way to install programmatically. iOS never fires it and installs only via
  // the Share sheet, so it gets instructions instead. Anything else (Firefox
  // on Android, a browser with the prompt suppressed) gets nothing rather than
  // a button that cannot work.
  if (!available && !ios) return null;

  return (
    <div className="pwa-banner install" role="region" aria-label={t("pwa.install.title")}>
      <div className="row" style={{ gap: 10, alignItems: "flex-start" }}>
        <Smartphone size={17} strokeWidth={1.75} style={{ flex: "0 0 auto", marginTop: 2 }} />
        <div style={{ minWidth: 0 }}>
          <div className="pwa-banner-text">{t("pwa.install.title")}</div>
          <div className="muted pwa-banner-sub">{t("pwa.install.body")}</div>
          {showSteps && (
            <ol className="pwa-install-steps">
              <li>
                <Share size={13} strokeWidth={2} /> {t("pwa.install.ios.step1")}
              </li>
              <li>
                <Plus size={13} strokeWidth={2} /> {t("pwa.install.ios.step2")}
              </li>
              <li>{t("pwa.install.ios.step3")}</li>
            </ol>
          )}
        </div>
        <button
          className="btn ghost icon"
          style={{ marginLeft: "auto", flex: "0 0 auto" }}
          onClick={dismiss}
          aria-label={t("pwa.install.dismiss")}
        >
          <X size={15} strokeWidth={2} />
        </button>
      </div>
      <div className="row" style={{ gap: 8, justifyContent: "flex-end" }}>
        {ios ? (
          <button
            className="btn primary sm"
            onClick={() => setShowSteps((s) => !s)}
            aria-expanded={showSteps}
          >
            {t("pwa.install.ios.how")}
          </button>
        ) : (
          <button
            className="btn primary sm"
            onClick={async () => {
              // Either outcome retires the banner: an accept installs, and a
              // decline is an answer we should not ask again this session.
              await promptInstall();
              dismiss();
            }}
          >
            {t("pwa.install.action")}
          </button>
        )}
      </div>
    </div>
  );
}

/** Mounted once by Layout, below the routed content. */
export function PwaBanners() {
  return (
    <>
      <UpdateBanner />
      <InstallBanner />
    </>
  );
}
