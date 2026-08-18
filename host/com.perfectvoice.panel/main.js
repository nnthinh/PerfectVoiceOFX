// Production WI panel. Layout mirrors BMD SamplePlugin (manifest + main.js + .node).
// Security: https://www.electronjs.org/docs/tutorial/security
const { app, BrowserWindow, ipcMain } = require("electron");
const fs = require("fs");
const path = require("path");
const { startEngine, stopEngine, getPublicStatus } = require("./engine");

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
    const candidates = [
        path.join(__dirname, "WorkflowIntegration.node"),
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Workflow Integrations/Examples/SamplePlugin/WorkflowIntegration.node",
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
}

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 520,
        height: 520,
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
    initResolveInterface().catch((e) => debugLog(String(e)));
});

app.on("window-all-closed", () => {
    shutdown();
    if (process.platform !== "darwin") app.quit();
});

app.on("will-quit", () => {
    shutdown();
});
