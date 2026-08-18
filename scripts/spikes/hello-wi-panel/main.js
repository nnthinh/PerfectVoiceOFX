// Spike WI panel. Layout mirrors BMD SamplePlugin (manifest + main.js + .node).
// Security: https://www.electronjs.org/docs/tutorial/security
const { app, BrowserWindow, ipcMain } = require("electron");
const { spawn } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");

const PLUGIN_ID = "com.perfectvoice.hello.spike";
const READY_RE = /^READY (http:\/\/127\.0\.0\.1:\d+)\s*$/;
const FAIL_CLOSED =
    "Cannot start engine (spawn blocked or not installed). Need Studio standalone + a codesigned engine.";

// Absolute only — never PATH. PERFECTVOICE_PYTHON wins when set.
const PYTHON_CANDIDATES = [
    "/usr/bin/python3",
    "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3",
    "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
    "/opt/homebrew/bin/python3",
];

let mainWindow = null;
let WorkflowIntegration = null;
let engineChild = null;

function debugLog(message) {
    const text = String(message);
    console.log(`[pv-hello] ${text}`);
    try {
        if (mainWindow && !mainWindow.isDestroyed()) {
            const safe = text.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
            mainWindow.webContents.executeJavaScript(
                `console.log('%c[pv-hello]', 'color:#3db8e8', '${safe}');`,
            );
        }
    } catch {
        // ignore
    }
}

function existsFile(p) {
    try {
        return fs.statSync(p).isFile();
    } catch {
        return false;
    }
}

function unlinkIfPresent(p) {
    if (!p) return;
    try {
        fs.unlinkSync(p);
    } catch (err) {
        if (!err || err.code !== "ENOENT") throw err;
    }
}

function stopChild(child) {
    if (!child || child.exitCode !== null) return;
    try {
        child.kill("SIGTERM");
    } catch {
        // already gone
    }
    setTimeout(() => {
        if (child.exitCode === null) {
            try {
                child.kill("SIGKILL");
            } catch {
                // ignore
            }
        }
    }, 1000);
}

function stopEngineChild() {
    if (!engineChild) return;
    stopChild(engineChild);
    engineChild = null;
}

function resolvePython() {
    const env = process.env.PERFECTVOICE_PYTHON;
    if (env) {
        if (!path.isAbsolute(env)) {
            throw new Error("PERFECTVOICE_PYTHON must be an absolute path");
        }
        if (!existsFile(env)) {
            throw new Error(`PERFECTVOICE_PYTHON not found: ${env}`);
        }
        return env;
    }
    for (const c of PYTHON_CANDIDATES) {
        if (existsFile(c)) return c;
    }
    throw new Error("no absolute python3 found; set PERFECTVOICE_PYTHON");
}

function resolveDevSibling() {
    // Only when this file still lives next to scripts/spikes/hello-engine.
    // Installed copies must use §3.8 enginePath / PERFECTVOICE_ENGINE.
    if (path.basename(__dirname) !== "hello-wi-panel") return null;
    if (path.basename(path.dirname(__dirname)) !== "spikes") return null;
    const spikeBin = path.join(__dirname, "..", "hello-engine");
    if (existsFile(spikeBin)) return spikeBin;
    const spikePy = path.join(__dirname, "..", "hello-engine.py");
    if (existsFile(spikePy)) return spikePy;
    return null;
}

function resolveEnginePath() {
    const env = process.env.PERFECTVOICE_ENGINE;
    if (env) {
        if (!path.isAbsolute(env)) return null;
        if (existsFile(env)) return env;
    }
    const home = os.homedir();
    const user = path.join(
        home,
        "Library/Application Support/PerfectVoice/engine/perfectvoice-engine",
    );
    if (existsFile(user)) return user;
    const system = "/Library/Application Support/PerfectVoice/engine/perfectvoice-engine";
    if (existsFile(system)) return system;
    return resolveDevSibling();
}

function writeTokenFile() {
    const runDir = path.join(
        os.homedir(),
        "Library/Application Support/PerfectVoice/run",
    );
    fs.mkdirSync(runDir, { recursive: true, mode: 0o700 });
    const tokenPath = path.join(runDir, `${crypto.randomUUID()}.token`);
    const token = crypto.randomBytes(32).toString("hex");
    fs.writeFileSync(tokenPath, token, { encoding: "utf8", mode: 0o600 });
    return { tokenPath, token };
}

function getHealth(url, token) {
    return new Promise((resolve, reject) => {
        const req = http.get(
            `${url}/v1/health`,
            { headers: { Authorization: `Bearer ${token}` } },
            (res) => {
                let body = "";
                res.setEncoding("utf8");
                res.on("data", (c) => {
                    body += c;
                });
                res.on("end", () => resolve({ status: res.statusCode, body }));
            },
        );
        req.on("error", reject);
        req.setTimeout(5000, () => req.destroy(new Error("health timeout")));
    });
}

function spawnHelloEngine() {
    const absEnginePath = resolveEnginePath();
    if (!absEnginePath) {
        const err = new Error("engine not found");
        err.code = "ENOENT";
        return Promise.reject(err);
    }
    if (!path.isAbsolute(absEnginePath)) {
        return Promise.reject(new Error("enginePath must be absolute"));
    }

    let tokenPath = null;
    let written;
    try {
        written = writeTokenFile();
        tokenPath = written.tokenPath;
    } catch (err) {
        return Promise.reject(err);
    }

    let child = null;
    let timer = null;
    let settled = false;
    let resolve_;
    let reject_;
    const done = new Promise((resolve, reject) => {
        resolve_ = resolve;
        reject_ = reject;
    });

    const finish = (err, value) => {
        if (settled) return;
        settled = true;
        if (timer) clearTimeout(timer);
        if (child) {
            child.removeAllListeners("exit");
            child.removeAllListeners("error");
        }
        stopChild(child);
        if (engineChild === child) engineChild = null;
        if (err) {
            unlinkIfPresent(tokenPath);
            reject_(err);
            return;
        }
        resolve_(value);
    };

    const engineDir = path.dirname(absEnginePath);
    const args = ["serve", "--bind", "127.0.0.1", "--port", "0", "--token-file", tokenPath];

    let cmd = absEnginePath;
    let cmdArgs = args;
    try {
        if (absEnginePath.endsWith(".py")) {
            cmd = resolvePython();
            cmdArgs = [absEnginePath, ...args];
        }
        debugLog(`spawn ${cmd}`);
        child = spawn(cmd, cmdArgs, {
            cwd: engineDir,
            env: { PATH: "", HOME: process.env.HOME || "", TMPDIR: process.env.TMPDIR || "" },
            stdio: ["ignore", "pipe", "pipe"],
        });
    } catch (err) {
        if (err && (err.code === "EPERM" || err.code === "EACCES")) {
            const closed = new Error(FAIL_CLOSED);
            closed.code = err.code;
            finish(closed);
        } else {
            finish(err);
        }
        return done;
    }
    engineChild = child;

    let stderrBuf = "";
    child.stderr.on("data", (c) => {
        stderrBuf += c.toString("utf8");
    });

    timer = setTimeout(() => {
        finish(new Error(`timeout waiting for READY\n${stderrBuf}`));
    }, 8000);

    child.on("error", (err) => {
        if (err && (err.code === "EPERM" || err.code === "EACCES")) {
            const closed = new Error(FAIL_CLOSED);
            closed.code = err.code;
            finish(closed);
            return;
        }
        finish(err);
    });

    child.on("exit", (code, signal) => {
        finish(new Error(`engine exited ${code} signal=${signal}\n${stderrBuf}`));
    });

    let buf = "";
    child.stdout.on("data", (chunk) => {
        buf += chunk.toString("utf8");
        const lines = buf.split(/\n/);
        buf = lines.pop() || "";
        for (const line of lines) {
            const m = READY_RE.exec(line);
            if (!m) continue;
            const readyUrl = m[1];
            getHealth(readyUrl, written.token)
                .then((health) => {
                    if (health.status !== 200) {
                        throw new Error(`health status ${health.status}: ${health.body}`);
                    }
                    const parsed = JSON.parse(health.body);
                    if (parsed.ok !== true || parsed.protocol_version !== 1) {
                        throw new Error("unexpected health payload");
                    }
                    if (existsFile(tokenPath)) {
                        throw new Error("token file still present after READY (engine should unlink)");
                    }
                    const consumed = tokenPath;
                    tokenPath = null;
                    finish(null, {
                        enginePath: absEnginePath,
                        readyUrl,
                        healthStatus: health.status,
                        healthBody: health.body,
                        tokenUnlinked: !existsFile(consumed),
                    });
                })
                .catch((err) => finish(err));
        }
    });

    return done;
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
        debugLog("WorkflowIntegration.node not found — Resolve bridge offline (spawn still works)");
        return null;
    }
    const isSuccess = await WorkflowIntegration.Initialize(PLUGIN_ID);
    if (!isSuccess) {
        debugLog("WorkflowIntegration.Initialize failed");
        return null;
    }
    return WorkflowIntegration.GetResolve();
}

function registerIpc() {
    ipcMain.handle("pv:enginePath", async () => resolveEnginePath());
    ipcMain.handle("pv:spawnHello", async () => spawnHelloEngine());
}

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 640,
        height: 420,
        useContentSize: true,
        webPreferences: {
            preload: path.join(__dirname, "preload.js"),
        },
    });
    mainWindow.on("close", () => app.quit());
    mainWindow.loadFile("index.html");
}

app.whenReady().then(() => {
    registerIpc();
    createWindow();
    initResolveInterface().catch((e) => debugLog(String(e)));
});

app.on("window-all-closed", () => {
    stopEngineChild();
    if (WorkflowIntegration && typeof WorkflowIntegration.CleanUp === "function") {
        WorkflowIntegration.CleanUp();
    }
    if (process.platform !== "darwin") app.quit();
});

app.on("will-quit", () => {
    stopEngineChild();
});
