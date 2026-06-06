// Browser-portable replacements for window.confirm() / prompt() / alert()
// using the native <dialog> element. We need these because WKWebView (and
// therefore the Tauri shell) silently no-ops all three without showing any
// UI — so the kill / send-bulk / new-session / API-error paths appeared to
// do nothing in the .app. The HTML <dialog> path renders identically in
// Chrome, Safari, and the Tauri webview.
//
// Ported from static/dialog.js. The imperative document.createElement path
// is replaced by a signal-backed <DialogHost> rendered once in <App>; the
// Promise-returning public API and return contracts are preserved exactly:
//   confirmDialog → boolean (false on cancel / backdrop / Esc)
//   promptDialog  → string | null (null on cancel)
// All CSS classes (.confirm-dialog, .prompt-dialog, .confirm-dialog-msg,
// .confirm-dialog-actions, .confirm-dialog-cancel, .confirm-dialog-ok,
// .is-danger, .prompt-dialog-input) carry over unchanged. Escape-to-cancel
// goes through the shared LIFO useEscape hook rather than <dialog>'s native
// close, so a dialog opened over another overlay closes in LIFO order.
import { signal } from "@preact/signals";
import { useRef, useEffect } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";

// Queue of active dialog requests. Each carries its config + a resolve fn.
const dialogs = signal([]);
let nextId = 1;

function open(req) {
  return new Promise((resolve) => {
    dialogs.value = [...dialogs.value, { ...req, id: nextId++, resolve }];
  });
}

function settle(id, result) {
  const req = dialogs.value.find((d) => d.id === id);
  dialogs.value = dialogs.value.filter((d) => d.id !== id);
  if (req) req.resolve(result);
}

export function confirmDialog(message, opts = {}) {
  return open({
    type: "confirm",
    message,
    okLabel: opts.okLabel || "OK",
    cancelLabel: opts.cancelLabel || "Cancel",
    // `danger: true` paints the OK button red and moves default focus to
    // Cancel — used for destructive ops like kill.
    danger: !!opts.danger,
  });
}

export function promptDialog(label, opts = {}) {
  return open({
    type: "prompt",
    message: label,
    defaultValue: opts.defaultValue || "",
    placeholder: opts.placeholder || "",
    okLabel: opts.okLabel || "OK",
    cancelLabel: opts.cancelLabel || "Cancel",
  });
}

// Render newline-separated message text as <br>-joined lines (the vanilla
// version did `escapeHtml(message).replace(/\n/g, "<br>")`; JSX gives us
// auto-escaping for free, so we just split + interleave <br>).
function messageLines(message) {
  const parts = String(message ?? "").split("\n");
  return parts.map((line, i) => (
    <>
      {i > 0 ? <br /> : null}
      {line}
    </>
  ));
}

function OneDialog({ req }) {
  const ref = useRef(null);
  const inputRef = useRef(null);
  const danger = req.type === "confirm" && req.danger;

  // The cancel result varies by type: confirm → false, prompt → null,
  // alert → undefined (void).
  const cancelResult = req.type === "confirm" ? false : req.type === "prompt" ? null : undefined;

  // Escape cancels, via the shared LIFO stack (not <dialog>'s native close).
  useEscape(() => settle(req.id, cancelResult));

  useEffect(() => {
    const dlg = ref.current;
    if (dlg && !dlg.open) dlg.showModal();
    // For destructive prompts, focus Cancel so an accidental Enter/Space
    // doesn't trigger the action (matches macOS NSAlert defaults). For
    // prompt, focus + select the input; otherwise focus OK.
    if (req.type === "prompt") {
      const inp = inputRef.current;
      if (inp) { inp.focus(); inp.select(); }
    } else if (danger) {
      dlg?.querySelector(".confirm-dialog-cancel")?.focus();
    } else {
      dlg?.querySelector(".confirm-dialog-ok")?.focus();
    }
  }, []);

  const onOk = () => {
    if (req.type === "prompt") settle(req.id, inputRef.current?.value ?? "");
    else if (req.type === "alert") settle(req.id, undefined);
    else settle(req.id, true);
  };
  const onCancel = () => settle(req.id, cancelResult);

  // Backdrop click (the <dialog> element itself, outside its content box)
  // cancels — same affordance as native modals.
  const onDialogClick = (e) => {
    if (e.target === ref.current) onCancel();
  };

  const className = req.type === "prompt" ? "confirm-dialog prompt-dialog" : "confirm-dialog";

  return (
    <dialog ref={ref} class={className} onClick={onDialogClick}>
      <p class="confirm-dialog-msg">{messageLines(req.message)}</p>
      {req.type === "prompt" && (
        <input
          ref={inputRef}
          type="text"
          class="prompt-dialog-input"
          autocomplete="off"
          spellcheck={false}
          value={req.defaultValue}
          placeholder={req.placeholder || undefined}
          onKeyDown={(e) => {
            // Enter submits the current value.
            if (e.key === "Enter") { e.preventDefault(); onOk(); }
          }}
        />
      )}
      <div class="confirm-dialog-actions">
        {req.type !== "alert" && (
          <button type="button" class="confirm-dialog-cancel" onClick={onCancel}>
            {req.cancelLabel}
          </button>
        )}
        <button
          type="button"
          class={`confirm-dialog-ok${danger ? " is-danger" : ""}`}
          onClick={onOk}
        >
          {req.okLabel}
        </button>
      </div>
    </dialog>
  );
}

export function DialogHost() {
  const list = dialogs.value;
  return (
    <>
      {list.map((req) => (
        <OneDialog key={req.id} req={req} />
      ))}
    </>
  );
}
