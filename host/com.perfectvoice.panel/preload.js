const { contextBridge, ipcRenderer } = require("electron/renderer");

// Renderer may request status / start / jobs; it never receives the Bearer token.
contextBridge.exposeInMainWorld("perfectvoice", {
    status: () => ipcRenderer.invoke("pv:status"),
    startEngine: () => ipcRenderer.invoke("pv:startEngine"),
    inspect: () => ipcRenderer.invoke("pv:inspect"),
    placeTestWav: (params) => ipcRenderer.invoke("pv:placeTestWav", params || {}),
    placeIsolated: (params) => ipcRenderer.invoke("pv:placeIsolated", params || {}),
    removeAccompaniment: (options) => ipcRenderer.invoke("pv:removeAccompaniment", options || {}),
    cancelJob: () => ipcRenderer.invoke("pv:cancelJob"),
    downloadModel: (name) => ipcRenderer.invoke("pv:downloadModel", name),
    onJobEvent: (cb) => {
        const listener = (_e, payload) => {
            if (typeof cb === "function") cb(payload);
        };
        ipcRenderer.on("pv:jobEvent", listener);
        return () => ipcRenderer.removeListener("pv:jobEvent", listener);
    },
});
