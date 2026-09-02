# LGTM integration

`# --- LGTM integration ---` block in `server.py`. Periscope mirrors
LGTM's session list onto pane cards (a `👁 review` chip on cards whose
cwd matches a registered LGTM repo) and embeds LGTM's UI in the modal's
Review tab via an iframe. Discovery is all over HTTP against LGTM's
existing API:

- `GET http://localhost:9900/projects` — full session list, polled
  every `LGTM_REFRESH_S` (default 30s).
- `GET http://localhost:9900/project/:slug/events` — SSE stream per
  session; any event triggers a refresh.
- `POST http://localhost:9900/projects` — invoked by `/api/lgtm/start`
  when the user clicks "Start review" from the Review tab.

Override the base URL with `PERISCOPE_LGTM_URL`. Everything degrades
silently when LGTM isn't running — no log spam, the cache just stays
empty and the chips never appear.

LGTM is intentionally unaware of periscope. Don't add cross-imports or
shared types — the contract is the HTTP/SSE shape above.

## Debugging a blank Review tab

If the iframe mounts but renders blank, the failure is almost always on
LGTM's side, not in periscope's plumbing. The fast path:

1. Open the iframe URL directly in a browser tab —
   `http://127.0.0.1:9900/project/<slug>/`. If it's blank there too,
   the integration is fine and LGTM is broken.
2. View source on that page. The HTML head references a content-hashed
   JS bundle: `<script src="/assets/index-<hash>.js">`.
3. `curl -I http://127.0.0.1:9900/assets/<hash>.js` — if it 404s,
   LGTM's `frontend/dist/` is missing the bundle (common after an
   interrupted `npm run build:frontend` or a partial dev/prod toggle).
4. Fix: `cd ~/dev/claude-review && npm run build:frontend`.

Symptom is "Review tab is blank" because the SPA never bootstraps —
`<div id="root">` stays empty, the iframe shows nothing. Easy to
mistake for a periscope iframe sizing bug; it isn't.
