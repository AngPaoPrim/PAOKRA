const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("paokra", {
  selectVideo: () => ipcRenderer.invoke("select-video"),
  selectVideos: (options) => ipcRenderer.invoke("select-videos", options || {}),
  openTikTokDashboard: () => ipcRenderer.invoke("open-tiktok-dashboard"),
  openExternal: (url) => ipcRenderer.invoke("open-external", String(url || "")),
  getTikTokDashboardStatus: () => ipcRenderer.invoke("tiktok-dashboard-status"),
  openAIStudio: () => ipcRenderer.invoke("open-ai-studio"),
  checkForUpdates: () => ipcRenderer.invoke("check-for-updates"),
  openSystemUpdateFix: () => ipcRenderer.invoke("open-system-update-fix"),
  updateActivity: () => ipcRenderer.send("update-user-activity")
});
