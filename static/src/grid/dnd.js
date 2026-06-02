// Shared drag MIME types for the grid. Two coexisting HTML5 DnD channels
// (coupling #6 — must stay distinct):
//   text/plain                  → session-header reorder
//   application/periscope-card  → move a card into another session
// Card drags deliberately set ONLY CARD_MIME so the header-reorder branch
// (which keys on text/plain) ignores them, and vice versa. Lives in its own
// module so Card.jsx and Grid.jsx share the exact constant without a cycle.
export const CARD_MIME = "application/periscope-card";
