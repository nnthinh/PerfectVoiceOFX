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

let mainWindow = null;
let WorkflowIntegration = null;

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
    const spikeBin = path.join(__dirname, "..", "hello-engine");
    if (existsFile(spikeBin)) return spikeBin;
    const spikePy = path.join(__dirname, "..", "hello-engine.py");
    if (existsFile(spikePy)) return spikePy;
    return null;
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

    const { tokenPath, token } = writeTokenFile();
    const engineDir = path.dirname(absEnginePath);
    const args = ["serve", "--bind", "127.0.0.1", "--port", "0", "--token-file", tokenPath];

    let cmd = absEnginePath;
    let cmdArgs = args;
    if (absEnginePath.endsWith(".py")) {
        const py =
            process.env.PERFECTVOICE_PYTHON ||
            "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3";
        if (!path.isAbsolute(py) || !existsFile(py)) {
            return Promise.reject(new Error(`python interpreter not found: ${py}`));
        }
        cmd = py;
        cmdArgs = [absEnginePath, ...args];
    }

    debugLog(`spawn ${cmd}`);
    const child = spawn(cmd, cmdArgs, {
        cwd: engineDir,
        env: { PATH: "", HOME: process.env.HOME || "", TMPDIR: process.env.TMPDIR || "" },
        stdio: ["ignore", "pipe", "pipe"],
    });

    return new Promise((resolve, reject) => {
        child.on("error", (err) => {
            if (err && (err.code === "EPERM" || err.code === "EACCES")) {
                const closed = new Error(FAIL_CLOSED);
                closed.code = err.code;
                reject(closed);
                return;
            }
            reject(err);
        });

        let stderrBuf = "";
        child.stderr.on("data", (c) => {
            stderrBuf += c.toString("utf8");
        });

        let buf = "";
        const timer = setTimeout(() => {
            child.kill("SIGTERM");
            reject(new Error(`timeout waiting for READY\n${stderrBuf}`));
        }, 8000);

        child.stdout.on("data", (chunk) => {
            buf += chunk.toString("utf8");
            const lines = buf.split(/\n/);
            buf = lines.pop() || "";
            for (const line of lines) {
                const m = READY_RE.exec(line);
                if (!m) continue;
                clearTimeout(timer);
                const readyUrl = m[1];
                getHealth(readyUrl, token)
                    .then((health) => {
                        child.kill("SIGTERM");
                        resolve({
                            enginePath: absEnginePath,
                            readyUrl,
                            healthStatus: health.status,
                            healthBody: health.body,
                            tokenUnlinked: !existsFile(tokenPath),
                        });
                    })
                    .catch((err) => {
                        child.kill("SIGTERM");
                        reject(err);
                    });
            }
        });
    });
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
    if (WorkflowIntegration && typeof WorkflowIntegration.CleanUp === "function") {
        WorkflowIntegration.CleanUp();
    }
    if (process.platform !== "darwin") app.quit();
});
