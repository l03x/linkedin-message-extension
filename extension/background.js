// The only background code: make the toolbar icon open the side panel.
// No listeners on tabs, pages or network; nothing runs on LinkedIn from here.
chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});
