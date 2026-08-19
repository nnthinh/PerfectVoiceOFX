// Production WI panel. Layout mirrors BMD SamplePlugin (manifest + main.js + .node).
// Security: https://www.electronjs.org/docs/tutorial/security
const { app, BrowserWindow, ipcMain } = require("electron");
const fs = require("fs");
const path = require("path");
const {
    startEngine,
    stopEngine,
    getPublicStatus,
    refreshSession,
    downloadModel,
    getSpeakers,
    enrollSpeaker,
    deleteSpeaker,
    defaultRunDir,
} = require("./engine");
const { inspectSelection, placeIsolated, placeTestWav } = require("./resolve");
const { removeAccompaniment, cancelActiveJob } = require("./jobs");

const PLUGIN_ID = "com.perfectvoice.panel";
const STUDIO_REQUIRED =
    "PerfectVoice requires DaVinci Resolve Studio and Workflow Integrations.";

let mainWindow = null;
let WorkflowIntegration = null;
let resolveReady = false;
let resolveError = STUDIO_REQUIRED;

const ALLOWED_MODELS = new Set(["htdemucs", "htdemucs_ft"]);
const DEFAULT_UI_PREFS = {
    model: "htdemucs",
    dfn: false,
    muteOriginal: false,
    useCache: true,
    wet: 0.85,
    shifts: 1,
    overlap: 0.25,
};

function sanitizeWet(val) {
    const n = Number(val);
    if (!Number.isFinite(n) || n < 0 || n > 1) return DEFAULT_UI_PREFS.wet;
    return Math.round(n * 100) / 100;
}

function sanitizeShifts(val) {
    const n = parseInt(val, 10);
    if (!Number.isFinite(n) || n < 1 || n > 16) return DEFAULT_UI_PREFS.shifts;
    return n;
}

function sanitizeOverlap(val) {
    const n = Number(val);
    if (!Number.isFinite(n) || n < 0 || n >= 1) return DEFAULT_UI_PREFS.overlap;
    return Math.round(n * 100) / 100;
}

function uiPrefsPath() {
    return path.join(path.dirname(defaultRunDir()), "ui.json");
}

function loadUiPrefs() {
    const dest = uiPrefsPath();
    try {
        const raw = JSON.parse(fs.readFileSync(dest, "utf8"));
        if (!raw || typeof raw !== "object") return { ...DEFAULT_UI_PREFS };
        const model = ALLOWED_MODELS.has(raw.model) ? raw.model : DEFAULT_UI_PREFS.model;
        return {
            model,
            dfn: raw.dfn === true,
            muteOriginal: raw.muteOriginal === true,
            useCache: raw.useCache !== false,
            wet: sanitizeWet(raw.wet),
            shifts: sanitizeShifts(raw.shifts),
            overlap: sanitizeOverlap(raw.overlap),
        };
    } catch {
        return { ...DEFAULT_UI_PREFS };
    }
}

function saveUiPrefs(next) {
    const prefs = {
        ...DEFAULT_UI_PREFS,
        ...(next && typeof next === "object" ? next : {}),
    };
    if (!ALLOWED_MODELS.has(prefs.model)) prefs.model = DEFAULT_UI_PREFS.model;
    prefs.dfn = prefs.dfn === true;
    prefs.muteOriginal = prefs.muteOriginal === true;
    prefs.useCache = prefs.useCache !== false;
    prefs.wet = sanitizeWet(prefs.wet);
    prefs.shifts = sanitizeShifts(prefs.shifts);
    prefs.overlap = sanitizeOverlap(prefs.overlap);
    const dest = uiPrefsPath();
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.writeFileSync(dest, `${JSON.stringify(prefs, null, 2)}\n`, "utf8");
    return prefs;
}

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
    ipcMain.handle("pv:getUiPrefs", async () => loadUiPrefs());
    ipcMain.handle("pv:setUiPrefs", async (_e, next) => saveUiPrefs(next || {}));
    ipcMain.handle("pv:status", async () => {
        try {
            await refreshSession();
        } catch {
            // keep last known health if the sidecar is mid-restart
        }
        return panelStatus();
    });
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
            const result = await downloadModel(name, (data) => {
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
            try {
                await refreshSession();
            } catch {
                // ignore
            }
            return { ...panelStatus(), ...result };
        } catch (err) {
            return { ok: false, error: err && err.message ? err.message : String(err) };
        }
    });
    ipcMain.handle("pv:getSpeakers", async () => {
        try {
            return await getSpeakers();
        } catch (err) {
            return { ok: false, error: err && err.message ? err.message : String(err) };
        }
    });
    ipcMain.handle("pv:enrollSpeaker", async (_e, params) => {
        try {
            let audioPath = params && params.audio_path;
            let t0 = (params && params.t0) || 0;
            let t1 = (params && params.t1) || 0;
            let name = (params && params.name) || "Speaker";
            if (!audioPath) {
                const resolve = await ensureResolveInit();
                if (!resolve) return { ok: false, error: resolveError || STUDIO_REQUIRED };
                const insp = inspectSelection(resolve);
                if (!insp || !insp.clips || !insp.clips.length) {
                    return { ok: false, error: "Select a clip on the timeline first to enroll voice." };
                }
                const first = insp.clips[0];
                audioPath = first.source_path || first.path;
                const sr = first.source_sample_rate || 44100;
                t0 = first.source_in_sample != null ? first.source_in_sample / sr : 0;
                t1 = first.source_out_sample != null ? first.source_out_sample / sr : t0 + 3.0;
                name = first.clip_name || first.name || name;
            }
            return await enrollSpeaker({ audio_path: audioPath, name, t0, t1 });
        } catch (err) {
            return { ok: false, error: err && err.message ? err.message : String(err) };
        }
    });
    ipcMain.handle("pv:deleteSpeaker", async (_e, speakerId) => {
        try {
            return await deleteSpeaker(speakerId);
        } catch (err) {
            return { ok: false, error: err && err.message ? err.message : String(err) };
        }
    });
}

function loadWindowState() {
    try {
        const dest = uiPrefsPath();
        const raw = JSON.parse(fs.readFileSync(dest, "utf8"));
        if (raw && typeof raw === "object" && raw.windowState) {
            const ws = raw.windowState;
            const w = Number(ws.width);
            const h = Number(ws.height);
            return {
                width: Number.isFinite(w) && w >= 400 ? w : 680,
                height: Number.isFinite(h) && h >= 500 ? h : 1020,
                x: Number.isFinite(Number(ws.x)) ? Number(ws.x) : undefined,
                y: Number.isFinite(Number(ws.y)) ? Number(ws.y) : undefined,
            };
        }
    } catch {
        // ignore
    }
    return { width: 680, height: 1020 };
}

function saveWindowState(win) {
    if (!win || win.isDestroyed()) return;
    try {
        const bounds = win.getBounds();
        const dest = uiPrefsPath();
        let existing = {};
        try {
            existing = JSON.parse(fs.readFileSync(dest, "utf8")) || {};
        } catch {}
        existing.windowState = {
            width: bounds.width,
            height: bounds.height,
            x: bounds.x,
            y: bounds.y,
        };
        fs.mkdirSync(path.dirname(dest), { recursive: true });
        fs.writeFileSync(dest, `${JSON.stringify(existing, null, 2)}\n`, "utf8");
    } catch (err) {
        debugLog(`saveWindowState error: ${err}`);
    }
}

function createWindow() {
    const ws = loadWindowState();
    const options = {
        width: ws.width || 680,
        height: ws.height || 1020,
        minWidth: 540,
        minHeight: 650,
        useContentSize: true,
        webPreferences: {
            preload: path.join(__dirname, "preload.js"),
            contextIsolation: true,
            nodeIntegration: false,
        },
    };
    if (ws.x != null && ws.y != null) {
        options.x = ws.x;
        options.y = ws.y;
    }
    mainWindow = new BrowserWindow(options);

    mainWindow.once("ready-to-show", () => {
        try {
            if (ws.width && ws.height) {
                mainWindow.setContentSize(ws.width, ws.height);
            }
            if (ws.x != null && ws.y != null) {
                mainWindow.setPosition(ws.x, ws.y);
            }
        } catch {
            // ignore
        }
        mainWindow.show();
    });

    let saveTimer = null;
    const debouncedSave = () => {
        if (saveTimer) clearTimeout(saveTimer);
        saveTimer = setTimeout(() => {
            saveWindowState(mainWindow);
        }, 400);
    };
    mainWindow.on("resize", debouncedSave);
    mainWindow.on("move", debouncedSave);
    mainWindow.on("close", () => {
        saveWindowState(mainWindow);
        app.quit();
    });
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
