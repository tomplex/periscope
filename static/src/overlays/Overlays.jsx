// Task 8 aggregator: the secondary modals + alerts feed + the Tauri
// external-link interceptor + the service-worker registration. Mounted once
// (behind the `overlays` surface flag) so all the low-frequency surfaces come
// up together. Each child modal is self-gating (renders null until its own
// open signal flips) and registers its opener via the existing window-bridge /
// header-button-by-id conventions, so this component is just composition +
// the two imperative inits.
import { useEffect } from "preact/hooks";
import { initExternalLinks } from "../tauri.js";
import { CleanupModal } from "./CleanupModal.jsx";
import { CommandsModal } from "./CommandsModal.jsx";
import { LauncherModal } from "./LauncherModal.jsx";
import { OpenOmnibox } from "./OpenOmnibox.jsx";
import { SettingsModal } from "./SettingsModal.jsx";

export function Overlays() {
  useEffect(() => {
    // Tauri external-link interceptor (no-op in a real browser).
    initExternalLinks();

    // Service worker — PWA installability gate (no-op caching). Ported from
    // app.js's registration so the Preact path keeps the install affordance.
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
    }
  }, []);

  return (
    <>
      <CommandsModal />
      <CleanupModal />
      <SettingsModal />
      <LauncherModal />
      <OpenOmnibox />
    </>
  );
}
