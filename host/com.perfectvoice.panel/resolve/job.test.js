"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");

const { extractSampleRange, FPS_24 } = require("./time");
const {
    buildCreateJobRequest,
    clipToManifest,
    contentSample,
    placeParamsFromResult,
} = require("./job");
const http = require("http");
const { computePlace, muteOriginalClips } = require("./place");
const {
    parseSseBuffer,
    createJob,
    getJob,
    cancelJob,
    downloadModel,
    __setSessionForTests,
} = require("../engine");

function sampleInspect(overrides) {
    const clip = {
        name: "A001",
        uniqueId: "aud",
        filePath: "/Volumes/Media/A001.wav",
        t0: 0.2,
        t1: 1.2,
        fileDur: 2.0,
        sampleRate: 48000,
        recordFrame: 86400,
        durationFrames: 24,
        srcFps: { num: 24, den: 1 },
        rejected: false,
        suppressedDuplicate: false,
        audioMapping: { embedded_audio_channels: 2 },
        handleS: 0.5,
        ...(overrides || {}),
    };
    return {
        ok: true,
        outFps: { num: 24, den: 1 },
        projectSampleRate: 48000,
        handleS: 0.5,
        clips: [clip],
        acceptedCount: clip.rejected || clip.suppressedDuplicate ? 0 : 1,
    };
}

describe("contentSample / clip.v1 window", () => {
    it("is content t0 in samples, not the extract window", () => {
        assert.equal(contentSample(0.2, 48000), 9600);
        assert.equal(contentSample(1.2, 48000), 57600);
        const extract = extractSampleRange(0.2, 1.2, 2.0, 48000, 0.5);
        assert.equal(extract.srcInSample, 0);
        assert.equal(extract.hLeftActual, 0.2);
        assert.notEqual(contentSample(0.2, 48000), extract.srcInSample);
    });
});

describe("buildCreateJobRequest", () => {
    it("posts content samples and omits result-only fields", () => {
        const built = buildCreateJobRequest(sampleInspect());
        assert.equal(built.ok, true);
        const clip = built.body.clips[0];
        assert.equal(clip.schema, "perfectvoice.clip.v1");
        assert.equal(clip.source_in_sample, 9600);
        assert.equal(clip.source_out_sample, 57600);
        assert.equal(clip.handles_seconds, 0.5);
        assert.equal(clip.source_sample_rate, 48000);
        assert.equal(clip.source_channels, 2);
        assert.deepEqual(clip.channel_map, [0, 1]);
        assert.match(clip.clip_id, /^[0-9a-f-]{36}$/i);
        assert.equal(Object.prototype.hasOwnProperty.call(clip, "handles_left_actual"), false);
        assert.equal(Object.prototype.hasOwnProperty.call(clip, "wet_dry_sample_rate"), false);

        const params = built.body.params;
        assert.equal(params.schema, "perfectvoice.params.v1");
        assert.equal(params.model, "htdemucs");
        assert.equal(params.enhancer, "none");
        assert.equal(params.use_cache, true);
        assert.equal(params.wet, 0.85);
        assert.equal(Object.prototype.hasOwnProperty.call(params, "wet_dry_sample_rate"), false);
        assert.deepEqual(built.body.allowed_roots, params.allowed_roots);
        assert.equal(built.body.output_dir, params.output_dir);
        assert.ok(built.body.allowed_roots.includes(path.resolve("/Volumes/Media")));
    });

    it("does not pre-add H (t0=0.2, H=0.5 stays source_in=9600)", () => {
        const built = buildCreateJobRequest(sampleInspect());
        const extract = extractSampleRange(0.2, 1.2, 2.0, 48000, 0.5);
        assert.equal(built.body.clips[0].source_in_sample, 9600);
        assert.equal(built.body.clips[0].source_out_sample, 57600);
        assert.notEqual(built.body.clips[0].source_out_sample, extract.srcOutSample);
    });

    it("sets enhancer=deepfilternet3 when DFN is requested", () => {
        const built = buildCreateJobRequest(sampleInspect(), { dfn: true });
        assert.equal(built.body.params.enhancer, "deepfilternet3");
        const quality = buildCreateJobRequest(sampleInspect(), { preset: "quality" });
        assert.equal(quality.body.params.model, "htdemucs_ft");
    });

    it("skips rejected clips and fails when none remain", () => {
        const inspect = sampleInspect({ rejected: true });
        const built = buildCreateJobRequest(inspect);
        assert.equal(built.ok, false);
        assert.match(built.error, /No accepted clips/);
    });

    it("warns and skips a clip missing sample rate", () => {
        const inspect = sampleInspect({ sampleRate: null });
        inspect.clips.push({
            name: "ok",
            filePath: "/Volumes/Media/B.wav",
            t0: 0.2,
            t1: 1.2,
            fileDur: 2,
            sampleRate: 48000,
            recordFrame: 10,
            rejected: false,
        });
        const built = buildCreateJobRequest(inspect);
        assert.equal(built.ok, true);
        assert.equal(built.body.clips.length, 1);
        assert.equal(built.body.clips[0].display_name, "ok");
        assert.ok(built.warnings.some((w) => /sample rate/i.test(w)));
    });
});

describe("placeParamsFromResult", () => {
    it("uses engine handles_left_actual, not requested H", () => {
        const inspect = sampleInspect();
        const params = placeParamsFromResult(
            inspect.clips[0],
            {
                output_path: "/tmp/isolated.wav",
                handles_left_actual: 0.2,
                handles_right_actual: 0.5,
            },
            inspect,
        );
        assert.equal(params.handles_left_actual, 0.2);
        assert.equal(params.wavPath, "/tmp/isolated.wav");
        const { hLeft, place } = computePlace({ ...params, outFps: FPS_24 });
        assert.equal(hLeft, 0.2);
        assert.equal(place.handleStartFrame, 5);
        const naive = computePlace({
            t0: 0.2,
            t1: 1.2,
            fileDur: 2,
            handleS: 0.5,
            outFps: FPS_24,
        });
        assert.equal(naive.hLeft, 0.2);
        const unclamped = computePlace({
            t0: 1.0,
            t1: 2.0,
            fileDur: 10,
            handleS: 0.5,
            outFps: FPS_24,
        });
        assert.equal(unclamped.hLeft, 0.5);
        const fromEngine = computePlace({
            t0: 1.0,
            t1: 2.0,
            fileDur: 10,
            handleS: 0.5,
            handles_left_actual: 0.25,
            outFps: FPS_24,
        });
        assert.equal(fromEngine.hLeft, 0.25);
    });

    it("refuses a result without handles_left_actual", () => {
        assert.throws(
            () =>
                placeParamsFromResult(
                    sampleInspect().clips[0],
                    { output_path: "/tmp/x.wav" },
                    sampleInspect(),
                ),
            /handles_left_actual/,
        );
    });
});

describe("clipToManifest uuid / fps", () => {
    it("uses inspect outFps and project sample rate", () => {
        const inspect = sampleInspect();
        inspect.outFps = { num: 24000, den: 1001 };
        inspect.projectSampleRate = 96000;
        const clip = clipToManifest(inspect.clips[0], inspect, {});
        assert.deepEqual(clip.timeline_fps, { num: 24000, den: 1001 });
        assert.equal(clip.project_sample_rate, 96000);
    });
});

describe("muteOriginalClips", () => {
    it("calls SetClipEnabled(false) on the matching audio item", () => {
        const calls = [];
        const audio = {
            GetName: () => "A001",
            GetUniqueId: () => "aud",
            GetTrackTypeAndIndex: () => ["audio", 1],
            GetLinkedItems: () => [],
            GetMediaPoolItem: () => ({
                GetClipProperty: (key) => {
                    const dump = { "File Path": "/media/A001.wav" };
                    if (key == null || key === "") return dump;
                    return dump[key];
                },
            }),
            GetStart: () => 86400,
            SetClipEnabled: (v) => {
                calls.push(v);
                return true;
            },
        };
        const timeline = {
            GetSelectedClips: () => [audio],
        };
        const resolve = {
            GetProjectManager: () => ({
                GetCurrentProject: () => ({
                    GetCurrentTimeline: () => timeline,
                }),
            }),
        };
        const result = muteOriginalClips(resolve, [
            { uniqueId: "aud", filePath: "/media/A001.wav", recordFrame: 86400 },
        ]);
        assert.equal(result.ok, true);
        assert.equal(result.muted, 1);
        assert.deepEqual(calls, [false]);
    });
});

describe("engine HTTP (mock sidecar)", () => {
    it("POSTs jobs, streams SSE, cancels, and stubs model download", async () => {
        const token = "a".repeat(64);
        let sawBearer = false;
        let createdBody = null;
        const server = http.createServer((req, res) => {
            if (req.headers.authorization === `Bearer ${token}`) sawBearer = true;
            if (req.method === "POST" && req.url === "/v1/jobs") {
                let raw = "";
                req.on("data", (c) => {
                    raw += c;
                });
                req.on("end", () => {
                    createdBody = JSON.parse(raw);
                    res.writeHead(202, { "Content-Type": "application/json" });
                    res.end(JSON.stringify({ id: "11111111-1111-4111-8111-111111111111", status: "queued" }));
                });
                return;
            }
            if (req.method === "GET" && req.url === "/v1/jobs/11111111-1111-4111-8111-111111111111") {
                res.writeHead(200, { "Content-Type": "application/json" });
                res.end(
                    JSON.stringify({
                        id: "11111111-1111-4111-8111-111111111111",
                        status: "done",
                        clips: [
                            {
                                clip_id: createdBody.clips[0].clip_id,
                                output_path: "/tmp/out.wav",
                                handles_left_actual: 0.2,
                                handles_right_actual: 0.5,
                            },
                        ],
                    }),
                );
                return;
            }
            if (req.method === "GET" && req.url.endsWith("/events")) {
                res.writeHead(200, { "Content-Type": "text/event-stream" });
                res.write('event: progress\ndata: {"clip_id":"abc","segment_offset":1,"audio_length":2}\n\n');
                res.write('event: done\ndata: {"id":"11111111-1111-4111-8111-111111111111"}\n\n');
                res.end();
                return;
            }
            if (req.method === "POST" && req.url.endsWith("/cancel")) {
                res.writeHead(202, { "Content-Type": "application/json" });
                res.end(JSON.stringify({ id: "11111111-1111-4111-8111-111111111111", status: "cancelled" }));
                return;
            }
            if (req.method === "POST" && req.url === "/v1/models/download") {
                res.writeHead(404, { "Content-Type": "application/json" });
                res.end(JSON.stringify({ error: "not_found" }));
                return;
            }
            res.writeHead(404);
            res.end();
        });
        await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
        const { port } = server.address();
        const child = { exitCode: null };
        __setSessionForTests({
            child,
            token,
            readyUrl: `http://127.0.0.1:${port}`,
            enginePath: "/tmp/fake-engine",
            health: { ok: true, protocol_version: 1 },
        });
        try {
            const built = buildCreateJobRequest(sampleInspect());
            const accepted = await createJob(built.body);
            assert.equal(accepted.status, "queued");
            assert.equal(createdBody.clips[0].source_in_sample, 9600);
            const events = [];
            const stream = require("../engine").streamJobEvents(accepted.id, (ev) => events.push(ev));
            await stream.done;
            assert.equal(events[0].event, "progress");
            assert.equal(events[1].event, "done");
            const record = await getJob(accepted.id);
            assert.equal(record.status, "done");
            assert.equal(record.clips[0].handles_left_actual, 0.2);
            const cancelled = await cancelJob(accepted.id);
            assert.equal(cancelled.status, "cancelled");
            const dl = await downloadModel("htdemucs");
            assert.equal(dl.ok, false);
            assert.equal(dl.notImplemented, true);
            assert.equal(sawBearer, true);
            const publicStatus = require("../engine").getPublicStatus();
            assert.equal(Object.prototype.hasOwnProperty.call(publicStatus, "token"), false);
            assert.equal(Object.prototype.hasOwnProperty.call(publicStatus, "readyUrl"), false);
        } finally {
            __setSessionForTests(null);
            await new Promise((resolve) => server.close(resolve));
        }
    });
});

describe("parseSseBuffer", () => {
    it("parses progress and done blocks and ignores keep-alives", () => {
        const raw =
            ": ka\n\n" +
            "event: progress\n" +
            'data: {"clip_id":"abc","segment_offset":1,"audio_length":4}\n\n' +
            "event: done\n" +
            'data: {"id":"job-1"}\n\n' +
            "event: progress\n" +
            "data: {";
        const { events, rest } = parseSseBuffer(raw);
        assert.equal(events.length, 2);
        assert.equal(events[0].event, "progress");
        assert.equal(events[0].data.segment_offset, 1);
        assert.equal(events[1].event, "done");
        assert.equal(events[1].data.id, "job-1");
        assert.equal(rest.startsWith("event: progress"), true);
    });
});
