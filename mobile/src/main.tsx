import { setupIonicReact } from "@ionic/react";
import { createRoot } from "react-dom/client";

/* Ionic's base styles, then the dark palette, switched by a class so the app
   can follow the phone or a choice made in Settings (native/theme.ts). */
import "@ionic/react/css/core.css";
import "@ionic/react/css/normalize.css";
import "@ionic/react/css/structure.css";
import "@ionic/react/css/typography.css";
import "@ionic/react/css/padding.css";
import "@ionic/react/css/text-alignment.css";
import "@ionic/react/css/flex-utils.css";
import "@ionic/react/css/display.css";
import "@ionic/react/css/palettes/dark.class.css";

import "./theme/variables.css";
import "./theme/app.css";

import { App } from "./App.tsx";
import { initTheme } from "./native/theme.ts";
import { CrashBoundary } from "./ui/CrashScreen.tsx";

setupIonicReact({
  // The iOS back swipe and the platform's own page transitions.
  swipeBackEnabled: true,
  // Android's back button walks back through the page stack.
  hardwareBackButton: true,
});

// Before the first paint, so the app never flashes the other theme.
initTheme();

createRoot(document.getElementById("root") as HTMLElement).render(
  <CrashBoundary>
    <App />
  </CrashBoundary>,
);
