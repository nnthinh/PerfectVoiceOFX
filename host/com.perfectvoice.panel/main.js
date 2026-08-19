// Production WI panel. Layout mirrors BMD SamplePlugin (manifest + main.js + .node).
// Security: https://www.electronjs.org/docs/tutorial/security
const { app, BrowserWindow, ipcMain } = require("electron");
const fs = require("fs");
const path = require("path");
const { startEngine, stopEngine, getPublicStatus, downloadModel } = require("./engine");
const { inspectSelection, placeIsolated, placeTestWav } = require("./resolve");
const { removeAccompaniment, cancelActiveJob } = require("./jobs");

const PLUGIN_ID = "com.perfectvoice.panel";
const STUDIO_REQUIRED =
    "PerfectVoice requires DaVinci Resolve Studio and Workflow Integrations.";

let mainWindow = null;
let WorkflowIntegration = null;
let resolveReady = false;
let resolveError = STUDIO_REQUIRED;

function existsFile(p) {
    try {
        return fs.statSync(p).isFile();
    } catch {
        return false;
    }
}

function debugLog(message) {
    const text = String(message);
    console.log(`[perfectvoice] ${text}`);
    try {
        if (mainWindow && !mainWindow.isDestroyed()) {
            const safe = text.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
            mainWindow.webContents.executeJavaScript(
                `console.log('%c[perfectvoice]', 'color:#3db8e8', '${safe}');`,
            );
        }
    } catch {
        // ignore
    }
}

function loadWorkflowIntegrationNode() {
    const programData =
        process.env.PROGRAMDATA || process.env.ProgramData || "C:\\ProgramData";
    const programFiles = process.env.ProgramFiles || "C:\\Program Files";
    const candidates = [
        path.join(__dirname, "WorkflowIntegration.node"),
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Workflow Integrations/Examples/SamplePlugin/WorkflowIntegration.node",
        path.join(
            programData,
            "Blackmagic Design/DaVinci Resolve/Support/Developer/Workflow Integrations/Examples/SamplePlugin/WorkflowIntegration.node",
        ),
        path.join(
            programFiles,
            "Blackmagic Design/DaVinci Resolve/Developer/Workflow Integrations/Examples/SamplePlugin/WorkflowIntegration.node",
        ),
    ];
    for (const c of candidates) {
        if (!existsFile(c)) continue;
        try {
            return require(c);
        } catch (e) {
            debugLog(`Failed to require ${c}: ${e.message}`);
        }
    }
    return null;
}

let initPromise = null;

async function initResolveInterface() {
    if (!WorkflowIntegration) {
        WorkflowIntegration = loadWorkflowIntegrationNode();
    }
    if (!WorkflowIntegration) {
        resolveReady = false;
        resolveError = STUDIO_REQUIRED;
        debugLog("WorkflowIntegration.node not found — Resolve bridge offline (spawn still works)");
        return null;
    }
    const isSuccess = await WorkflowIntegration.Initialize(PLUGIN_ID);
    if (!isSuccess) {
        resolveReady = false;
        resolveError = STUDIO_REQUIRED;
        debugLog("WorkflowIntegration.Initialize failed");
        return null;
    }
    resolveReady = true;
    resolveError = null;
    return WorkflowIntegration.GetResolve();
}

function ensureResolveInit() {
    if (!initPromise) initPromise = initResolveInterface();
    return initPromise;
}

function panelStatus() {
    return {
        ...getPublicStatus(),
        resolveReady,
        resolveError,
    };
}

function registerIpc() {
    ipcMain.handle("pv:status", async () => panelStatus());
    ipcMain.handle("pv:startEngine", async () => {
        try {
            const info = await startEngine();
            return {
                ok: true,
                ...panelStatus(),
                health: info.health,
            };
        } catch (err) {
            return {
                ok: false,
                ...panelStatus(),
                error: err && err.message ? err.message : String(err),
            };
        }
    });
    ipcMain.handle("pv:inspect", async () => {
        try {
            const resolve = await ensureResolveInit();
            if (!resolve) {
                return { ok: false, error: resolveError || STUDIO_REQUIRED };
            }
            return inspectSelection(resolve);
        } catch (err) {
            return { ok: false, error: err && err.message ? err.message : String(err) };
        }
    });
    ipcMain.handle("pv:placeTestWav", async (_e, params) => {
        try {
            const resolve = await ensureResolveInit();
            if (!resolve) {
                return { ok: false, error: resolveError || STUDIO_REQUIRED };
            }
            return placeTestWav(resolve, params || {});
        } catch (err) {
            return { ok: false, error: err && err.message ? err.message : String(err) };
        }
    });
    ipcMain.handle("pv:placeIsolated", async (_e, params) => {
        try {
            const resolve = await ensureResolveInit();
            if (!resolve) {
                return { ok: false, error: resolveError || STUDIO_REQUIRED };
            }
            return placeIsolated(resolve, params || {});
        } catch (err) {
            return { ok: false, error: err && err.message ? err.message : String(err) };
        }
    });
    ipcMain.handle("pv:removeAccompaniment", async (_e, options) => {
        try {
            const resolve = await ensureResolveInit();
            if (!resolve) {
                return { ok: false, error: resolveError || STUDIO_REQUIRED };
            }
            return await removeAccompaniment(resolve, options || {}, (payload) => {
                try {
                    if (mainWindow && !mainWindow.isDestroyed()) {
                        mainWindow.webContents.send("pv:jobEvent", payload);
                    }
                } catch {
                    // ignore
                }
            });
        } catch (err) {
            return { ok: false, error: err && err.message ? err.message : String(err) };
        }
    });
    ipcMain.handle("pv:cancelJob", async () => {
        try {
            return await cancelActiveJob();
        } catch (err) {
            return { ok: false, error: err && err.message ? err.message : String(err) };
        }
    });
    ipcMain.handle("pv:downloadModel", async (_e, name) => {
        try {
            return await downloadModel(name, (data) => {
                try {
                    if (mainWindow && !mainWindow.isDestroyed()) {
                        mainWindow.webContents.send("pv:downloadEvent", {
                            type: "progress",
                            data: data || {},
                        });
                    }
                } catch {
                    // ignore
                }
            });
        } catch (err) {
            return { ok: false, error: err && err.message ? err.message : String(err) };
        }
    });
}

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 560,
        height: 900,
        useContentSize: true,
        webPreferences: {
            preload: path.join(__dirname, "preload.js"),
            contextIsolation: true,
            nodeIntegration: false,
        },
    });
    mainWindow.on("close", () => app.quit());
    mainWindow.loadFile("index.html");
}

let shuttingDown = false;

function shutdown() {
    if (shuttingDown) return;
    shuttingDown = true;
    stopEngine().catch(() => {});
    if (WorkflowIntegration && typeof WorkflowIntegration.CleanUp === "function") {
        WorkflowIntegration.CleanUp();
    }
}

app.whenReady().then(() => {
    registerIpc();
    createWindow();
    ensureResolveInit().catch((e) => debugLog(String(e)));
});

app.on("window-all-closed", () => {
    shutdown();
    if (process.platform !== "darwin") app.quit();
});

app.on("will-quit", () => {
    shutdown();
});
