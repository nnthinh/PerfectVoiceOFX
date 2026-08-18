"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
    evaluateReject,
    filePathFromDump,
    UNCONFIRMED,
    COPY,
} = require("./reject");
const { inspectSelection } = require("./inspect");
const { computePlace, writeSilenceWav, readWavStats, wavCoversPlace, pickPlaceClip } = require("./place");
const { FPS_24, placeFrames } = require("./time");
const fs = require("fs");
const os = require("os");
const path = require("path");

const FPS24 = { num: 24, den: 1 };

function baseClip(over) {
    return {
        filePath: "/media/a.wav",
        t0: 1,
        t1: 2,
        durationFrames: 24,
        fusionCompCount: 0,
        voiceIsolation: { isEnabled: false, amount: 0 },
        audioMapping: { embedded_audio_channels: 2, track_mapping: { "1": { type: "Stereo" } } },
        propertySnapshot: {},
        clipPropertyDump: { "File Path": "/media/a.wav" },
        ...over,
    };
}

describe("evaluateReject", () => {
    it("accepts a clean stereo clip", () => {
        const v = evaluateReject(baseClip(), { outFps: FPS24 });
        assert.equal(v.rejected, false);
        assert.equal(v.reasons.length, 0);
        assert.ok(v.warnings.some((w) => w.code === "reverse_unconfirmed"));
        assert.ok(v.warnings.some((w) => w.code === "elastic_wave_unconfirmed"));
        assert.ok(v.warnings.some((w) => w.message === UNCONFIRMED.reverse));
    });

    it("rejects missing File Path", () => {
        const v = evaluateReject(baseClip({ filePath: null }), { outFps: FPS24 });
        assert.equal(v.rejected, true);
        assert.ok(v.reasons.some((r) => r.code === "offline" && r.message === COPY.offline));
    });

    it("rejects Fusion / nested", () => {
        const v = evaluateReject(baseClip({ fusionCompCount: 2 }), { outFps: FPS24 });
        assert.ok(v.reasons.some((r) => r.code === "fusion"));
    });

    it("rejects Voice Isolation enabled", () => {
        const v = evaluateReject(
            baseClip({ voiceIsolation: { isEnabled: true, amount: 80 } }),
            { outFps: FPS24 },
        );
        assert.ok(v.reasons.some((r) => r.code === "voice_isolation"));
    });

    it("rejects 5.1 mapping", () => {
        const v = evaluateReject(
            baseClip({
                audioMapping: {
                    embedded_audio_channels: 6,
                    track_mapping: { "1": { type: "5.1" } },
                },
            }),
            { outFps: FPS24 },
        );
        assert.ok(v.reasons.some((r) => r.code === "multichannel"));
    });

    it("rejects speed via duration heuristic", () => {
        const v = evaluateReject(baseClip({ durationFrames: 48 }), { outFps: FPS24 });
        assert.ok(v.reasons.some((r) => r.code === "speed"));
    });

    it("rejects speed when a probed dump key is not 100%", () => {
        const v = evaluateReject(
            baseClip({
                durationFrames: 24,
                itemPropertySnapshot: { ClipSpeed: 50 },
            }),
            { outFps: FPS24 },
        );
        assert.ok(v.reasons.some((r) => r.code === "speed"));
    });

    it("does not fake-reject reverse or Elastic Wave without a dump key", () => {
        const v = evaluateReject(baseClip(), { outFps: FPS24 });
        assert.equal(v.reasons.some((r) => r.code === "reverse"), false);
        assert.equal(v.reasons.some((r) => r.code === "elastic_wave"), false);
    });

    it("rejects reverse only when a probed key is enabled", () => {
        const v = evaluateReject(
            baseClip({ itemPropertySnapshot: { Reverse: true } }),
            { outFps: FPS24 },
        );
        assert.ok(v.reasons.some((r) => r.code === "reverse"));
    });

    it("rejects slip from linked_audio.offset", () => {
        const v = evaluateReject(
            baseClip({
                audioMapping: {
                    embedded_audio_channels: 2,
                    linked_audio: { "1": { channels: 2, offset: 4800, path: "/media/a.wav" } },
                    track_mapping: { "1": { type: "Stereo" } },
                },
            }),
            { outFps: FPS24 },
        );
        assert.ok(v.reasons.some((r) => r.code === "slip"));
    });

    it("does not treat FlipX as reverse", () => {
        const v = evaluateReject(
            baseClip({ itemPropertySnapshot: { FlipX: true, FlipY: false } }),
            { outFps: FPS24 },
        );
        assert.equal(v.reasons.some((r) => r.code === "reverse"), false);
    });
});

describe("filePathFromDump", () => {
    it("uses official File Path first", () => {
        const got = filePathFromDump({ "File Path": "/media/a.wav", Other: "/tmp/x" });
        assert.equal(got.path, "/media/a.wav");
        assert.equal(got.key, "File Path");
        assert.equal(got.probed, false);
    });

    it("probes dump values that look like paths without inventing a key", () => {
        const got = filePathFromDump({ "Mystery Path Key": "/Volumes/media/a.wav" });
        assert.equal(got.path, "/Volumes/media/a.wav");
        assert.equal(got.key, "Mystery Path Key");
        assert.equal(got.probed, true);
    });
});

describe("inspectSelection (mock Resolve)", () => {
    it("dedupes linked A/V and reports reject reasons", () => {
        const mpA = {
            GetClipProperty: (key) => {
                const dump = { "File Path": "/media/A001.wav" };
                if (key == null || key === "") return dump;
                return dump[key];
            },
        };
        const audio = {
            GetName: () => "A001",
            GetUniqueId: () => "aud",
            GetTrackTypeAndIndex: () => ["audio", 1],
            GetLinkedItems: () => [video],
            GetMediaPoolItem: () => mpA,
            GetStart: () => 86400,
            GetDuration: () => 24,
            GetSourceStartTime: () => 0.2,
            GetSourceEndTime: () => 1.2,
            GetFusionCompCount: () => 0,
            GetVoiceIsolationState: () => ({ isEnabled: false }),
            GetSourceAudioChannelMapping: () =>
                JSON.stringify({ embedded_audio_channels: 2, track_mapping: { "1": { type: "Stereo" } } }),
            GetProperty: () => ({}),
        };
        const video = {
            GetName: () => "A001",
            GetUniqueId: () => "vid",
            GetTrackTypeAndIndex: () => ["video", 1],
            GetLinkedItems: () => [audio],
            GetMediaPoolItem: () => ({
                GetClipProperty: (key) => (key === "File Path" || !key ? "/media/A001.mov" : undefined),
            }),
            GetStart: () => 86400,
            GetDuration: () => 24,
            GetSourceStartTime: () => 0.2,
            GetSourceEndTime: () => 1.2,
            GetFusionCompCount: () => 0,
            GetProperty: () => ({}),
        };
        const timeline = {
            GetName: () => "TL",
            GetSelectedClips: () => [video, audio],
            GetItemListInTrack() {
                throw new Error("must not iterate the timeline");
            },
        };
        const resolve = {
            GetVersion: () => [21, 0, 4, ""],
            GetVersionString: () => "21.0.4",
            GetProductName: () => "DaVinci Resolve Studio",
            GetProjectManager: () => ({
                GetCurrentProject: () => ({
                    GetCurrentTimeline: () => timeline,
                    GetSetting: (key) => (key === "timelineFrameRate" ? "24" : undefined),
                    GetMediaPool: () => ({}),
                }),
            }),
        };
        const result = inspectSelection(resolve);
        assert.equal(result.ok, true);
        assert.equal(result.source, "GetSelectedClips");
        assert.equal(result.jobCount, 1);
        assert.equal(result.clips[0].filePath, "/media/A001.wav");
        assert.ok(Math.abs(result.clips[0].hLeftActual - 0.2) < 1e-12);
        assert.equal(result.clips[0].recordFrame, 86400);
        assert.equal(result.acceptedCount, 1);
    });
});

describe("place compute", () => {
    it("builds inclusive WAV-grid frames from handles_left_actual", () => {
        const { place } = computePlace({
            t0: 0.2,
            t1: 1.2,
            fileDur: 10,
            handleS: 0.5,
            handlesLeftActual: 0.2,
            outFps: FPS_24,
        });
        assert.equal(place.handleStartFrame, 5);
        assert.equal(place.handleEndFrame, 28);
        const infoStart = placeFrames(0.2, 1.2, 10, FPS_24, 0.5, 0.2);
        assert.equal(place.handleStartFrame, infoStart.handleStartFrame);
    });

    it("writes a WAV that covers the place window", () => {
        const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pv-place-"));
        const wav = path.join(dir, "t.wav");
        writeSilenceWav(wav, 48000, 81600, 2);
        const stats = readWavStats(wav);
        assert.equal(stats.nFrames, 81600);
        const place = placeFrames(0.2, 1.2, 10, FPS_24, 0.5);
        const cover = wavCoversPlace(stats, place, { num: 24, den: 1 });
        assert.equal(cover.ok, true);
        fs.rmSync(dir, { recursive: true, force: true });
    });

    it("pickPlaceClip prefers an accepted job", () => {
        const clip = pickPlaceClip({
            clips: [
                { suppressedDuplicate: true, t0: 0, t1: 1, recordFrame: 1 },
                { rejected: true, t0: 0.2, t1: 1.2, recordFrame: 10, name: "bad" },
                { rejected: false, t0: 0.2, t1: 1.2, recordFrame: 20, name: "good" },
            ],
        });
        assert.equal(clip.name, "good");
    });
});
