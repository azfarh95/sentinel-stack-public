// Comet side-panel — local config (TEMPLATE).
//
// Copy this to `config.local.js` (gitignored) and set the token to match
// COMET_BRIDGE_TOKEN in metamcp-local/.env.local. The side panel sends it as the
// `X-Comet-Token` header so the bridge (127.0.0.1:8101) accepts /chat — this is
// what closes the unauthenticated "S1 hole". If config.local.js is absent the
// panel sends no token and the bridge (if it has a token set) returns 401.
window.COMET_BRIDGE_TOKEN = "PASTE_TOKEN_HERE";
