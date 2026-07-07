// Service worker: make the toolbar button open the side panel.
// All the UI + logic lives in sidepanel.html / sidepanel.js.

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: true })
    .catch((err) => console.error("setPanelBehavior failed", err));
});
