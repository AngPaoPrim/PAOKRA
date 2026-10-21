const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("paokraAI", {
  chooseVideo: () => ipcRenderer.invoke("choose-video"),
  chooseOutput: () => ipcRenderer.invoke("choose-output"),
  enhance: (options) => ipcRenderer.invoke("enhance-video", options),
  cancel: () => ipcRenderer.invoke("cancel-job"),
  openOutput: (filePath) => ipcRenderer.invoke("open-output", filePath),
  onProgress: (fn) => ipcRenderer.on("progress", (_event, data) => fn(data))
});