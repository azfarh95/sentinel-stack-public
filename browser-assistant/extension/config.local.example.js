// Browser Assistant side panel — local config (TEMPLATE).
//
// Copy this to `config.local.js` (gitignored) and set the token to match
// COMET_BRIDGE_TOKEN in metamcp-local/.env.local. The panel sends it as the
// `X-Comet-Token` header so the :8108 surface accepts /run, /events, /approve.
// If config.local.js is absent the panel sends no token and the surface
// (which has a token set) returns 401.
window.COMET_BRIDGE_TOKEN = "PASTE_TOKEN_HERE";
