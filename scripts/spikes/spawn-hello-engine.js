#!/usr/bin/env node
/**
 * Spawn hello-engine the way the Workflow Integration panel will.
 *
 * Contract §3.8:
 *   spawn(absEnginePath, ["serve", "--bind", "127.0.0.1", "--port", "0",
 *                         "--token-file", tokenPath], { cwd: engineDir })
 *
 * Absolute path only. No PATH. No relative. Fail closed on EPERM/EACCES.
 *
 * This is the Electron child_process.spawn shape (copied from SamplePlugin /
 * Audiio: require("child_process").spawn). Node and Resolve's Electron share
 * that API; run under Resolve Electron with ELECTRON_RUN_AS_NODE=1 to exercise
 * the Hardened Runtime parent (see docs/spikes/notarize.md).
 */
"use strict";

const { spawn } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");

const SPIKE_DIR = __dirname;
const READY_RE = /^READY (http:\/\/127\.0\.0\.1:\d+)\s*$/;
const FAIL_CLOSED =
  "Cannot start engine (spawn blocked or not installed). Need Studio standalone + a codesigned engine.";

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
    if (!path.isAbsolute(env)) {
      throw new Error("PERFECTVOICE_ENGINE must be an absolute path");
    }
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

  const spikeBin = path.join(SPIKE_DIR, "hello-engine");
  if (existsFile(spikeBin)) return spikeBin;
  const spikePy = path.join(SPIKE_DIR, "hello-engine.py");
  if (existsFile(spikePy)) return spikePy;
  return null;
}

function assertAbsolute(p, label) {
  if (!p || !path.isAbsolute(p)) {
    throw new Error(`${label} must be an absolute path (got ${p})`);
  }
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

function failClosed(err) {
  const code = err && err.code;
  console.error(FAIL_CLOSED);
  console.error(`spawn error: ${code || ""} ${err && err.message ? err.message : err}`);
  process.exit(2);
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
        res.on("end", () => {
          resolve({ status: res.statusCode, body });
        });
      },
    );
    req.on("error", reject);
    req.setTimeout(5000, () => {
      req.destroy(new Error("health timeout"));
    });
  });
}

function spawnEngine(absEnginePath, tokenPath) {
  assertAbsolute(absEnginePath, "enginePath");
  assertAbsolute(tokenPath, "tokenPath");

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

  let cmd = absEnginePath;
  let cmdArgs = args;
  if (absEnginePath.endsWith(".py")) {
    // Interpreter must also be absolute — still no PATH lookup.
    const py =
      process.env.PERFECTVOICE_PYTHON ||
      "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3";
    assertAbsolute(py, "python");
    if (!existsFile(py)) {
      throw new Error(`python interpreter not found: ${py}`);
    }
    cmd = py;
    cmdArgs = [absEnginePath, ...args];
  }

  console.error(`spawn ${cmd} ${cmdArgs.join(" ")}`);
  console.error(`cwd    ${engineDir}`);

  const child = spawn(cmd, cmdArgs, {
    cwd: engineDir,
    env: {
      PATH: "",
      HOME: process.env.HOME || "",
      TMPDIR: process.env.TMPDIR || "",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  return child;
}

async function main() {
  const absEnginePath = resolveEnginePath();
  if (!absEnginePath) {
    console.error("engine not found. Build scripts/spikes/hello-engine or set PERFECTVOICE_ENGINE.");
    process.exit(1);
  }
  const { tokenPath, token } = writeTokenFile();
  let child;
  try {
    child = spawnEngine(absEnginePath, tokenPath);
  } catch (err) {
    if (err && (err.code === "EPERM" || err.code === "EACCES")) {
      failClosed(err);
    }
    throw err;
  }

  child.on("error", (err) => {
    if (err && (err.code === "EPERM" || err.code === "EACCES")) {
      failClosed(err);
    }
    console.error(`spawn error: ${err.message}`);
    process.exit(1);
  });

  let stderrBuf = "";
  child.stderr.on("data", (c) => {
    stderrBuf += c.toString("utf8");
    process.stderr.write(c);
  });

  const readyUrl = await new Promise((resolve, reject) => {
    let buf = "";
    const timer = setTimeout(() => {
      reject(new Error(`timeout waiting for READY\nstderr:\n${stderrBuf}`));
    }, 8000);
    child.stdout.on("data", (chunk) => {
      buf += chunk.toString("utf8");
      const lines = buf.split(/\n/);
      buf = lines.pop() || "";
      for (const line of lines) {
        const m = READY_RE.exec(line);
        if (m) {
          clearTimeout(timer);
          resolve(m[1]);
        }
      }
    });
    child.on("exit", (code, signal) => {
      clearTimeout(timer);
      reject(new Error(`engine exited ${code} signal=${signal}\nstderr:\n${stderrBuf}`));
    });
  });

  console.log(`ready ${readyUrl}`);
  const health = await getHealth(readyUrl, token);
  console.log(`health ${health.status} ${health.body}`);
  if (health.status !== 200) {
    child.kill("SIGTERM");
    process.exit(1);
  }
  const parsed = JSON.parse(health.body);
  if (parsed.ok !== true || parsed.protocol_version !== 1) {
    child.kill("SIGTERM");
    console.error("unexpected health payload");
    process.exit(1);
  }
  if (existsFile(tokenPath)) {
    console.error("token file still present after READY (engine should unlink)");
    child.kill("SIGTERM");
    process.exit(1);
  }
  try {
    child.kill("SIGTERM");
  } catch {
    // already gone
  }
  const exited = await Promise.race([
    new Promise((r) => child.once("exit", () => r(true))),
    new Promise((r) => setTimeout(() => r(false), 1000)),
  ]);
  if (!exited) {
    try {
      child.kill("SIGKILL");
    } catch {
      // ignore
    }
  }
  console.log("ok unsigned-or-local spawn + /v1/health");
  process.exit(0);
}

main().catch((err) => {
  if (err && (err.code === "EPERM" || err.code === "EACCES")) {
    failClosed(err);
  }
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
