// Task 8 aggregator: the secondary modals + alerts feed + the "+ session"
// prompt flow + the Tauri external-link interceptor + the service-worker
// registration. Mounted once (behind the `overlays` surface flag) so all the
// low-frequency surfaces come up together. Each child modal is self-gating
// (renders null until its own open signal flips) and registers its opener via
// the existing window-bridge / header-button-by-id conventions, so this
// component is just composition + the three imperative inits.
import { useEffect } from "preact/hooks";
import { promptDialog } from "./Dialog.jsx";
import { apiCall } from "../util.js";
import { poll } from "../grid/poll.js";
import { initExternalLinks } from "../tauri.js";
import { Alerts } from "./Alerts.jsx";
import { CommandsModal } from "./CommandsModal.jsx";
import { NewProjectModal } from "./NewProjectModal.jsx";
import { ReviewPrModal } from "./ReviewPrModal.jsx";
import { CleanupModal } from "./CleanupModal.jsx";
import { SettingsModal } from "./SettingsModal.jsx";
import { OpenPickerModal } from "./OpenPickerModal.jsx";
import { LauncherModal } from "./LauncherModal.jsx";

export function Overlays() {
  useEffect(() => {
    // "+ session" — a prompt, not a rendered modal. Wired to the Preact
    // <Header>'s #new-session button by id (the header may be Preact or, while
    // only overlays is claimed, vanilla — either way the id exists). Ported
    // from app.js's new-session click handler.
    const newSessionBtn = document.getElementById("new-session");
    async function onNewSession() {
      const name = await promptDialog("session name:", { placeholder: "e.g. tc/feature" });
      if (!name) return;
      await apiCall("new session", "/api/session/new", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim() }),
      });
      poll();
    }
    if (newSessionBtn) newSessionBtn.addEventListener("click", onNewSession);

    // Tauri external-link interceptor (no-op in a real browser).
    initExternalLinks();

    // Service worker — PWA installability gate (no-op caching). Ported from
    // app.js's registration so the Preact path keeps the install affordance.
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
    }

    return () => {
      if (newSessionBtn) newSessionBtn.removeEventListener("click", onNewSession);
    };
  }, []);

  return (
    <>
      <Alerts />
      <CommandsModal />
      <NewProjectModal />
      <ReviewPrModal />
      <CleanupModal />
      <SettingsModal />
      <OpenPickerModal />
      <LauncherModal />
    </>
  );
}
