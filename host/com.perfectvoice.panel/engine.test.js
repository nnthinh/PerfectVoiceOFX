"use strict";

const { describe, it, after } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");

const {
    defaultEnginePath,
    defaultRunDir,
    spawnChildEnv,
    __setPlatformForTests,
} = require("./engine");

after(() => {
    __setPlatformForTests(null);
});

describe("Windows §3.8 enginePath / run dir", () => {
    it("resolves engine to LOCALAPPDATA\\PerfectVoice\\engine\\perfectvoice-engine.exe", () => {
        const prev = process.env.LOCALAPPDATA;
        process.env.LOCALAPPDATA = "C:\\Users\\ed\\AppData\\Local";
        try {
            __setPlatformForTests("win32");
            const p = defaultEnginePath("win32");
            assert.equal(
                p,
                path.join(
                    "C:\\Users\\ed\\AppData\\Local",
                    "PerfectVoice",
                    "engine",
                    "perfectvoice-engine.exe",
                ),
            );
            assert.ok(!p.includes("Library"));
            assert.match(p, /perfectvoice-engine\.exe$/);
        } finally {
            if (prev === undefined) delete process.env.LOCALAPPDATA;
            else process.env.LOCALAPPDATA = prev;
            __setPlatformForTests(null);
        }
    });

    it("writes tokens under LOCALAPPDATA\\PerfectVoice\\run", () => {
        const prev = process.env.LOCALAPPDATA;
        process.env.LOCALAPPDATA = "C:\\Users\\ed\\AppData\\Local";
        try {
            const run = defaultRunDir("win32");
            assert.equal(
                run,
                path.join("C:\\Users\\ed\\AppData\\Local", "PerfectVoice", "run"),
            );
        } finally {
            if (prev === undefined) delete process.env.LOCALAPPDATA;
            else process.env.LOCALAPPDATA = prev;
        }
    });
});

describe("Win32 spawn env", () => {
    it("keeps SYSTEMROOT and LOCALAPPDATA with a minimal System32 PATH", () => {
        const env = spawnChildEnv("win32", {
            SYSTEMROOT: "C:\\Windows",
            SystemRoot: "C:\\Windows",
            LOCALAPPDATA: "C:\\Users\\ed\\AppData\\Local",
            USERPROFILE: "C:\\Users\\ed",
            TEMP: "C:\\Users\\ed\\AppData\\Local\\Temp",
            TMP: "C:\\Users\\ed\\AppData\\Local\\Temp",
        });
        assert.equal(env.SYSTEMROOT, "C:\\Windows");
        assert.equal(env.SystemRoot, "C:\\Windows");
        assert.equal(env.LOCALAPPDATA, "C:\\Users\\ed\\AppData\\Local");
        assert.equal(env.USERPROFILE, "C:\\Users\\ed");
        assert.ok(String(env.PATH).toLowerCase().includes("system32"));
        assert.ok(!String(env.PATH).toLowerCase().includes("windows\\system32;c:\\"));
    });

    it("does not leak a search PATH on macOS", () => {
        const env = spawnChildEnv("darwin", { HOME: "/Users/ed", TMPDIR: "/tmp" });
        assert.equal(env.PATH, "");
        assert.equal(env.HOME, "/Users/ed");
    });
});
