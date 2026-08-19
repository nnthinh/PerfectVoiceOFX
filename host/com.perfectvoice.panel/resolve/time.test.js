"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
    FPS_23_976,
    FPS_24,
    FPS_25,
    FPS_29_97,
    FPS_59_94,
    appendClipInfo,
    asFps,
    actualHandles,
    expectedOutputSampleCount,
    extractSampleRange,
    fileRelativeTimes,
    parseTimelineFrameRate,
    placeFrames,
    roundHalfUp,
} = require("./time");

describe("roundHalfUp", () => {
    it("is half-away-from-zero, not banker", () => {
        assert.equal(roundHalfUp(0.5), 1);
        assert.equal(roundHalfUp(1.5), 2);
        assert.equal(roundHalfUp(2.5), 3);
    });

    it("handles typical and negative values", () => {
        assert.equal(roundHalfUp(4.795204795204795), 5);
        assert.equal(roundHalfUp(-0.5), -1);
        assert.equal(roundHalfUp(-1.5), -2);
    });

    it("rejects NaN/Inf", () => {
        assert.throws(() => roundHalfUp(Number.NaN), /finite/);
        assert.throws(() => roundHalfUp(Number.POSITIVE_INFINITY), /finite/);
    });
});

describe("fileRelativeTimes", () => {
    it("leaves file-relative in/out alone", () => {
        const r = fileRelativeTimes(0.2, 1.2, 10);
        assert.equal(r.shifted, false);
        assert.ok(Math.abs(r.t0 - 0.2) < 1e-12);
        assert.ok(Math.abs(r.t1 - 1.2) < 1e-12);
    });

    it("maps 21:05:40 TOD onto a 37.42s file", () => {
        const r = fileRelativeTimes(75940.36, 75977.78, 37.42);
        assert.equal(r.shifted, true);
        assert.equal(r.t0, 0);
        assert.ok(Math.abs(r.t1 - 37.42) < 1e-9);
    });

    it("does not keep a 0.36s phantom in-point when span ≈ file duration", () => {
        const r = fileRelativeTimes(75940.36, 75977.78, 37.44, { frameSeconds: 1 / 30 });
        assert.equal(r.t0, 0);
        assert.ok(Math.abs(r.t1 - 37.44) < 1e-9);
    });

    it("subtracts Start TC when present", () => {
        const r = fileRelativeTimes(75945.36, 75970.36, 37.42, { startTc: 75940.36 });
        assert.equal(r.shifted, true);
        assert.ok(Math.abs(r.t0 - 5) < 1e-9);
        assert.ok(Math.abs(r.t1 - 30) < 1e-9);
    });
});

describe("actualHandles / extract", () => {
    it("clamps left when t0 < H (t0=0.2 H=0.5 → 0.2)", () => {
        const h = actualHandles(0.2, 1.2, 10.0, 0.5);
        assert.ok(Math.abs(h.hLeftActual - 0.2) < 1e-12);
        assert.ok(Math.abs(h.hRightActual - 0.5) < 1e-12);
        const ext = extractSampleRange(0.2, 1.2, 10.0, 48000, 0.5);
        assert.ok(Math.abs(ext.hLeftActual - 0.2) < 1e-12);
        assert.ok(Math.abs(ext.hRightActual - 0.5) < 1e-12);
        assert.equal(ext.srcInSample, 0);
        assert.equal(ext.srcOutSample, roundHalfUp((1.2 + 0.5) * 48000));
    });

    it("keeps full handles for an interior clip", () => {
        const ext = extractSampleRange(1.0, 2.0, 10.0, 48000, 0.5);
        assert.ok(Math.abs(ext.hLeftActual - 0.5) < 1e-12);
        assert.ok(Math.abs(ext.hRightActual - 0.5) < 1e-12);
        assert.equal(ext.srcInSample, 24000);
        assert.equal(ext.srcOutSample, 120000);
    });

    it("clamps right at EOF", () => {
        const ext = extractSampleRange(1.0, 1.8, 2.0, 48000, 0.5);
        assert.ok(Math.abs(ext.hLeftActual - 0.5) < 1e-12);
        assert.ok(Math.abs(ext.hRightActual - 0.2) < 1e-9);
        assert.equal(ext.srcOutSample, roundHalfUp(2.0 * 48000));
    });

    it("gives no left handle at t0=0", () => {
        const ext = extractSampleRange(0.0, 1.0, 5.0, 44100, 0.5);
        assert.equal(ext.hLeftActual, 0);
        assert.equal(ext.srcInSample, 0);
    });
});

describe("placeFrames", () => {
    it("clamps SOF on required fps fixtures", () => {
        for (const fps of [FPS_23_976, FPS_24, FPS_25, FPS_29_97]) {
            const place = placeFrames(0.2, 1.2, 10.0, fps, 0.5);
            const fpsF = fps[0] / fps[1];
            assert.equal(place.handleStartFrame, roundHalfUp(0.2 * fpsF));
            assert.equal(place.handleEndFrameExclusive, roundHalfUp((0.2 + 1.0) * fpsF));
            assert.equal(place.handleEndFrame, place.handleEndFrameExclusive - 1);
            assert.ok(place.bodyFrameCount > 0);
        }
    });

    it("is exact at 24 fps interior", () => {
        const place = placeFrames(1.0, 2.0, 10.0, FPS_24, 0.5);
        assert.equal(place.handleStartFrame, 12);
        assert.equal(place.handleEndFrameExclusive, 36);
        assert.equal(place.handleEndFrame, 35);
        assert.equal(place.bodyFrameCount, 24);
    });

    it("matches 24000/1001 fraction", () => {
        const place = placeFrames(0.2, 1.2, 10.0, FPS_23_976, 0.5);
        assert.equal(place.outFpsNum, 24000);
        assert.equal(place.outFpsDen, 1001);
        assert.equal(place.handleStartFrame, roundHalfUp(0.2 * (24000 / 1001)));
        assert.equal(place.handleStartFrame, 5);
        assert.equal(place.handleEndFrameExclusive, 29);
        assert.equal(place.handleEndFrame, 28);
    });

    it("treats drop-frame as display-only (same 30000/1001)", () => {
        const ndf = placeFrames(0.2, 1.2, 10.0, FPS_29_97);
        const parsed = placeFrames(0.2, 1.2, 10.0, parseTimelineFrameRate("29.97 DF"));
        assert.equal(ndf.handleStartFrame, parsed.handleStartFrame);
        assert.equal(ndf.handleEndFrame, parsed.handleEndFrame);
    });

    it("never uses src_in_sample as startFrame", () => {
        const ext = extractSampleRange(10.0, 12.0, 60.0, 48000, 0.5);
        const place = placeFrames(10.0, 12.0, 60.0, FPS_24, 0.5);
        assert.equal(ext.srcInSample, 456000);
        assert.equal(place.handleStartFrame, 12);
        assert.notEqual(place.handleStartFrame, ext.srcInSample);
        const clipInfo = appendClipInfo("dummy", place, 1001, 1);
        assert.equal(clipInfo.startFrame, place.handleStartFrame);
        assert.equal(clipInfo.endFrame, place.handleEndFrame);
        assert.notEqual(clipInfo.startFrame, ext.srcInSample);
        assert.equal(clipInfo.mediaType, 2);
        assert.equal(clipInfo.recordFrame, 1001);
    });

    it("uses inclusive endFrame matching official 0..23 = 24 frames", () => {
        const place = placeFrames(0.5, 1.5, 10.0, FPS_24, 0.5);
        assert.equal(place.handleStartFrame, 12);
        assert.equal(place.handleEndFrame, 35);
        assert.equal(place.handleEndFrame - place.handleStartFrame + 1, 24);
    });

    it("honors engine handles_left_actual instead of recomputing H", () => {
        const place = placeFrames(0.2, 1.2, 10.0, FPS_24, 0.5, 0.2);
        assert.equal(place.hLeftActual, 0.2);
        assert.equal(place.handleStartFrame, 5);
        const naive = placeFrames(0.2, 1.2, 10.0, FPS_24, 0.5, 0.5);
        assert.notEqual(place.handleStartFrame, naive.handleStartFrame);
    });
});

describe("expectedOutputSampleCount", () => {
    it("is within one sample of clamped duration", () => {
        const nOut = expectedOutputSampleCount(0.2, 1.2, 10.0, 48000, 0.5);
        const expectedSeconds = 1.2 - 0.2 + 0.2 + 0.5;
        assert.ok(Math.abs(nOut - Math.round(expectedSeconds * 48000)) <= 1);
        assert.equal(nOut, 81600);
        const naive = roundHalfUp((1.2 - 0.2 + 0.5 + 0.5) * 48000);
        assert.notEqual(nOut, naive);
        assert.ok(nOut < naive);
    });
});

describe("parseTimelineFrameRate", () => {
    it("maps README strings", () => {
        assert.deepEqual(parseTimelineFrameRate("23.976"), asFps(FPS_23_976));
        assert.deepEqual(parseTimelineFrameRate("24"), asFps(FPS_24));
        assert.deepEqual(parseTimelineFrameRate("25"), asFps(FPS_25));
        assert.deepEqual(parseTimelineFrameRate("29.97"), asFps(FPS_29_97));
        assert.deepEqual(parseTimelineFrameRate("29.97 DF"), asFps(FPS_29_97));
        assert.deepEqual(parseTimelineFrameRate("59.94"), asFps(FPS_59_94));
        assert.deepEqual(parseTimelineFrameRate(24), asFps([24, 1]));
    });

    it("rejects empty", () => {
        assert.throws(() => parseTimelineFrameRate(""), /empty/);
        assert.throws(() => parseTimelineFrameRate(null), /empty/);
    });

    it("rejects non-positive asFps", () => {
        assert.throws(() => asFps([0, 1]), /positive/);
        assert.throws(() => asFps(-24), /positive/);
    });
});
