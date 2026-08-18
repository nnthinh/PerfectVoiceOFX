const { contextBridge, ipcRenderer } = require("electron/renderer");

// Renderer may request status / start; it never receives the Bearer token.
contextBridge.exposeInMainWorld("perfectvoice", {
    status: () => ipcRenderer.invoke("pv:status"),
    startEngine: () => ipcRenderer.invoke("pv:startEngine"),
});
