const { contextBridge, ipcRenderer } = require("electron/renderer");

// Renderer may request status / start; it never receives the Bearer token.
contextBridge.exposeInMainWorld("perfectvoice", {
    status: () => ipcRenderer.invoke("pv:status"),
    startEngine: () => ipcRenderer.invoke("pv:startEngine"),
    inspect: () => ipcRenderer.invoke("pv:inspect"),
    placeTestWav: (params) => ipcRenderer.invoke("pv:placeTestWav", params || {}),
    placeIsolated: (params) => ipcRenderer.invoke("pv:placeIsolated", params || {}),
});
