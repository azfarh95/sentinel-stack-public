// Service worker: make the toolbar button open the side panel.
// The chat lives entirely in sidepanel.html / sidepanel.js.

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: true })
    .catch((err) => console.error("setPanelBehavior failed", err));
});

// Optional: keep the panel pinned to all tabs so it doesn't disappear when switching.
chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  try {
    await chrome.sidePanel.setOptions({
      tabId,
      path: "sidepanel.html",
      enabled: true,
    });
  } catch (e) {
    // tab may be a chrome:// URL where the panel can't attach — silent.
  }
});
