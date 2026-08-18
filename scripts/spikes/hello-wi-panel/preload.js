const { contextBridge, ipcRenderer } = require("electron/renderer");

contextBridge.exposeInMainWorld("pvSpike", {
    spawnHello: () => ipcRenderer.invoke("pv:spawnHello"),
    enginePath: () => ipcRenderer.invoke("pv:enginePath"),
});
