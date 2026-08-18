"use strict";

/**
 * §3.8 sidecar spawn. Token and Bearer stay in this process — never return them.
 */

const { spawn } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");

const READY_RE = /^READY (http:\/\/127\.0\.0\.1:\d+)\s*$/;
const FAIL_CLOSED =
    "Cannot start engine (spawn blocked or not installed). Need Studio standalone + a codesigned engine.";
const ENGINE_NOT_FOUND =
    "Engine not found. Set PERFECTVOICE_ENGINE or install to ~/Library/Application Support/PerfectVoice/engine/perfectvoice-engine.";
const UPDATE_ENGINE = "Update PerfectVoice engine";
const PROTOCOL_VERSION = 1;

let session = null;
let startPromise = null;

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

function failClosedError(err) {
    const closed = new Error(FAIL_CLOSED);
    closed.code = err && err.code;
    return closed;
}

function isEperm(err) {
    return !!(err && (err.code === "EPERM" || err.code === "EACCES"));
}

function resolveEnginePath() {
    const env = process.env.PERFECTVOICE_ENGINE;
    if (env) {
        if (!path.isAbsolute(env)) {
            throw new Error("PERFECTVOICE_ENGINE must be an absolute path");
        }
        if (existsFile(env)) return env;
    }
    const user = path.join(
        os.homedir(),
        "Library/Application Support/PerfectVoice/engine/perfectvoice-engine",
    );
    if (existsFile(user)) return user;
    // Frozen §3.8 fallback (pkg / admin install).
    const system = "/Library/Application Support/PerfectVoice/engine/perfectvoice-engine";
    if (existsFile(system)) return system;
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

function parseHealth(health) {
    if (health.status !== 200) {
        throw new Error(`health status ${health.status}: ${health.body}`);
    }
    let parsed;
    try {
        parsed = JSON.parse(health.body);
    } catch {
        throw new Error("health response is not JSON");
    }
    if (parsed.ok !== true || parsed.protocol_version !== PROTOCOL_VERSION) {
        throw new Error(UPDATE_ENGINE);
    }
    return parsed;
}

function killChild(child) {
    if (!child || child.exitCode !== null) return;
    try {
        child.kill("SIGTERM");
    } catch {
        // already gone
    }
}

function killChildHard(child) {
    killChild(child);
    if (!child || child.exitCode !== null) return;
    try {
        child.kill("SIGKILL");
    } catch {
        // ignore
    }
}

function stopChild(child) {
    if (!child || child.exitCode !== null) return Promise.resolve();
    return new Promise((resolve) => {
        let done = false;
        const finish = () => {
            if (done) return;
            done = true;
            resolve();
        };
        child.once("exit", finish);
        killChild(child);
        setTimeout(() => {
            killChildHard(child);
            setTimeout(finish, 250);
        }, 1000);
    });
}

function isAlive() {
    return !!(session && session.child && session.child.exitCode === null);
}

function getPublicStatus() {
    const enginePath = (() => {
        try {
            return (session && session.enginePath) || resolveEnginePath();
        } catch {
            return null;
        }
    })();
    const caps = session && session.capabilities;
    return {
        connected: isAlive(),
        enginePath: enginePath || null,
        health: session && session.health ? session.health : null,
        modelsReady: caps && caps.models_ready != null ? caps.models_ready : null,
        devices: caps && caps.devices ? caps.devices : null,
    };
}

function engineErrorMessage(body, fallback) {
    if (body && typeof body === "object") {
        if (typeof body.detail === "string" && body.detail) return body.detail;
        if (typeof body.error === "string" && body.error) return body.error;
        if (typeof body.message === "string" && body.message) return body.message;
    }
    return fallback;
}

function parseSseBuffer(buf) {
    const events = [];
    let rest = String(buf || "");
    while (true) {
        const idx = rest.indexOf("\n\n");
        if (idx < 0) break;
        const block = rest.slice(0, idx);
        rest = rest.slice(idx + 2);
        let event = "message";
        const dataLines = [];
        for (const line of block.split("\n")) {
            if (!line || line.startsWith(":")) continue;
            if (line.startsWith("event:")) {
                event = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
                dataLines.push(line.slice(5).trim());
            }
        }
        if (!dataLines.length) continue;
        const raw = dataLines.join("\n");
        let data = raw;
        try {
            data = JSON.parse(raw);
        } catch {
            // keep raw string
        }
        events.push({ event, data });
    }
    return { events, rest };
}

function requestJson(method, pathname, body) {
    return new Promise((resolve, reject) => {
        if (!isAlive()) {
            reject(new Error("Engine is not connected."));
            return;
        }
        const base = String(session.readyUrl).replace(/\/$/, "");
        const url = new URL(pathname, `${base}/`);
        const payload = body === undefined ? null : JSON.stringify(body);
        const headers = {
            Authorization: `Bearer ${session.token}`,
            Accept: "application/json",
        };
        if (payload != null) {
            headers["Content-Type"] = "application/json";
            headers["Content-Length"] = Buffer.byteLength(payload);
        }
        const req = http.request(
            {
                hostname: url.hostname,
                port: url.port,
                path: `${url.pathname}${url.search}`,
                method,
                headers,
            },
            (res) => {
                let data = "";
                res.setEncoding("utf8");
                res.on("data", (c) => {
                    data += c;
                });
                res.on("end", () => {
                    let parsed = null;
                    if (data) {
                        try {
                            parsed = JSON.parse(data);
                        } catch {
                            parsed = { error: "invalid_json" };
                        }
                    }
                    resolve({ status: res.statusCode, body: parsed });
                });
            },
        );
        req.on("error", reject);
        req.setTimeout(30000, () => req.destroy(new Error("engine request timeout")));
        if (payload != null) req.write(payload);
        req.end();
    });
}

function streamJobEvents(jobId, onEvent) {
    if (!isAlive()) {
        return {
            abort() {},
            done: Promise.reject(new Error("Engine is not connected.")),
        };
    }
    const base = String(session.readyUrl).replace(/\/$/, "");
    const url = new URL(`/v1/jobs/${jobId}/events`, `${base}/`);
    const handle = { req: null, abort() {} };
    handle.done = new Promise((resolve, reject) => {
        let settled = false;
        const finish = (err, ev) => {
            if (settled) return;
            settled = true;
            if (err) reject(err);
            else resolve(ev);
        };
        const req = http.get(
            {
                hostname: url.hostname,
                port: url.port,
                path: url.pathname,
                headers: {
                    Authorization: `Bearer ${session.token}`,
                    Accept: "text/event-stream",
                },
            },
            (res) => {
                if (res.statusCode !== 200) {
                    let data = "";
                    res.setEncoding("utf8");
                    res.on("data", (c) => {
                        data += c;
                    });
                    res.on("end", () => {
                        let parsed = null;
                        try {
                            parsed = data ? JSON.parse(data) : null;
                        } catch {
                            parsed = null;
                        }
                        finish(
                            new Error(
                                engineErrorMessage(parsed, `events status ${res.statusCode}`),
                            ),
                        );
                    });
                    return;
                }
                let buf = "";
                res.setEncoding("utf8");
                res.on("data", (chunk) => {
                    buf += chunk;
                    const parsed = parseSseBuffer(buf);
                    buf = parsed.rest;
                    for (const ev of parsed.events) {
                        if (typeof onEvent === "function") onEvent(ev);
                        if (ev.event === "done" || ev.event === "error") {
                            res.destroy();
                            finish(null, ev);
                            return;
                        }
                    }
                });
                res.on("end", () => finish(null, null));
                res.on("error", (err) => finish(err));
            },
        );
        handle.req = req;
        handle.abort = () => {
            try {
                req.destroy();
            } catch {
                // ignore
            }
        };
        req.on("error", (err) => finish(err));
    });
    return handle;
}

async function refreshCapabilities() {
    if (!isAlive()) return null;
    const res = await requestJson("GET", "/v1/capabilities");
    if (res.status === 200 && res.body && typeof res.body === "object") {
        session.capabilities = res.body;
        return res.body;
    }
    return null;
}

async function createJob(body) {
    const res = await requestJson("POST", "/v1/jobs", body);
    if (res.status !== 202) {
        const err = new Error(engineErrorMessage(res.body, `create job status ${res.status}`));
        err.status = res.status;
        throw err;
    }
    return res.body;
}

async function getJob(jobId) {
    const res = await requestJson("GET", `/v1/jobs/${jobId}`);
    if (res.status !== 200) {
        const err = new Error(engineErrorMessage(res.body, `job status ${res.status}`));
        err.status = res.status;
        throw err;
    }
    return res.body;
}

async function cancelJob(jobId) {
    const res = await requestJson("POST", `/v1/jobs/${jobId}/cancel`);
    if (res.status !== 202) {
        const err = new Error(engineErrorMessage(res.body, `cancel status ${res.status}`));
        err.status = res.status;
        throw err;
    }
    return res.body;
}

async function downloadModel(name) {
    const model = name || "htdemucs";
    if (!isAlive()) {
        return {
            ok: false,
            notImplemented: true,
            error: "Download model is not implemented in this release.",
        };
    }
    const res = await requestJson("POST", "/v1/models/download", { name: model });
    if (res.status === 404) {
        return {
            ok: false,
            notImplemented: true,
            error: "Download model is not implemented in this release.",
        };
    }
    if (res.status >= 200 && res.status < 300) {
        return { ok: true, status: res.status, body: res.body };
    }
    return {
        ok: false,
        error: engineErrorMessage(res.body, `download status ${res.status}`),
        status: res.status,
    };
}

function waitReady(child, stderrRef) {
    return new Promise((resolve, reject) => {
        let buf = "";
        const timer = setTimeout(() => {
            reject(new Error(`timeout waiting for READY\n${stderrRef.buf}`));
        }, 8000);
        const onExit = (code, signal) => {
            clearTimeout(timer);
            reject(new Error(`engine exited ${code} signal=${signal}\n${stderrRef.buf}`));
        };
        child.stdout.on("data", (chunk) => {
            buf += chunk.toString("utf8");
            const lines = buf.split(/\n/);
            buf = lines.pop() || "";
            for (const line of lines) {
                const m = READY_RE.exec(line);
                if (m) {
                    clearTimeout(timer);
                    child.removeListener("exit", onExit);
                    resolve(m[1]);
                }
            }
        });
        child.on("exit", onExit);
    });
}

function spawnEngine(absEnginePath, tokenPath) {
    if (!path.isAbsolute(absEnginePath)) {
        throw new Error("enginePath must be absolute");
    }
    if (!path.isAbsolute(tokenPath)) {
        throw new Error("tokenPath must be absolute");
    }
    const engineDir = path.dirname(absEnginePath);
    const args = [
        "serve",
        "--bind",
        "127.0.0.1",
        "--port",
        "0",
        "--token-file",
        tokenPath,
    ];
    // Empty PATH: never resolve the binary via PATH.
    return spawn(absEnginePath, args, {
        cwd: engineDir,
        env: {
            PATH: "",
            HOME: process.env.HOME || "",
            TMPDIR: process.env.TMPDIR || "",
        },
        stdio: ["ignore", "pipe", "pipe"],
    });
}

function clearSessionIf(child) {
    if (session && session.child === child) {
        session = null;
    }
}

async function doStart() {
    const absEnginePath = resolveEnginePath();
    if (!absEnginePath) {
        const err = new Error(ENGINE_NOT_FOUND);
        err.code = "ENOENT";
        throw err;
    }

    let tokenPath = null;
    let child = null;
    try {
        const written = writeTokenFile();
        tokenPath = written.tokenPath;

        try {
            child = spawnEngine(absEnginePath, tokenPath);
        } catch (err) {
            if (isEperm(err)) throw failClosedError(err);
            throw err;
        }

        const spawnFailed = new Promise((_, reject) => {
            child.on("error", (err) => {
                if (isEperm(err)) {
                    reject(failClosedError(err));
                    return;
                }
                reject(err);
            });
        });

        const stderrRef = { buf: "" };
        child.stderr.on("data", (c) => {
            stderrRef.buf += c.toString("utf8");
        });

        const readyUrl = await Promise.race([waitReady(child, stderrRef), spawnFailed]);
        const health = parseHealth(await getHealth(readyUrl, written.token));
        if (existsFile(tokenPath)) {
            throw new Error("token file still present after READY (engine should unlink)");
        }
        tokenPath = null;

        child.removeAllListeners("exit");
        child.on("exit", () => {
            clearSessionIf(child);
        });

        session = {
            child,
            token: written.token,
            readyUrl,
            enginePath: absEnginePath,
            health,
            capabilities: null,
        };
        try {
            await refreshCapabilities();
        } catch {
            // health already proved the sidecar; capabilities are optional
        }
        return { readyUrl, health, enginePath: absEnginePath };
    } catch (err) {
        unlinkIfPresent(tokenPath);
        await stopChild(child);
        clearSessionIf(child);
        throw err;
    }
}

function startEngine() {
    if (startPromise) return startPromise;
    if (isAlive()) {
        startPromise = getHealth(session.readyUrl, session.token)
            .then(async (raw) => {
                const health = parseHealth(raw);
                session.health = health;
                try {
                    await refreshCapabilities();
                } catch {
                    // ignore
                }
                return {
                    readyUrl: session.readyUrl,
                    health,
                    enginePath: session.enginePath,
                };
            })
            .finally(() => {
                startPromise = null;
            });
        return startPromise;
    }
    startPromise = doStart().finally(() => {
        startPromise = null;
    });
    return startPromise;
}

async function stopEngine() {
    const current = session;
    session = null;
    if (!current) return;
    await stopChild(current.child);
}

function __setSessionForTests(next) {
    session = next;
}

module.exports = {
    FAIL_CLOSED,
    ENGINE_NOT_FOUND,
    UPDATE_ENGINE,
    resolveEnginePath,
    startEngine,
    stopEngine,
    getPublicStatus,
    isAlive,
    parseSseBuffer,
    requestJson,
    streamJobEvents,
    createJob,
    getJob,
    cancelJob,
    downloadModel,
    refreshCapabilities,
    __setSessionForTests,
};

if (require.main === module) {
    startEngine()
        .then((info) => {
            console.log(`ready ${info.readyUrl}`);
            console.log(`health ${JSON.stringify(info.health)}`);
            console.log("ok spawn + /v1/health");
        })
        .catch((err) => {
            if (isEperm(err)) {
                console.error(FAIL_CLOSED);
            }
            console.error(err && err.stack ? err.stack : err);
            process.exitCode = err && isEperm(err) ? 2 : 1;
        })
        .finally(() => stopEngine());
}
