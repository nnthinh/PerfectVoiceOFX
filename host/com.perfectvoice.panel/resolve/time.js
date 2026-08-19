"use strict";

/**
 * Appendix A — sample / frame conversion (pure, no Resolve).
 *
 * Must match shared/perfectvoice_time.py. Engine is source of truth if a
 * later port drifts. src_in_sample is an extract index and must never be
 * passed as AppendToTimeline startFrame.
 */

const FPS_23_976 = [24000, 1001];
const FPS_24 = [24, 1];
const FPS_25 = [25, 1];
const FPS_29_97 = [30000, 1001];
const FPS_59_94 = [60000, 1001];

const NTSC_RATE_STRINGS = {
    "23.976": FPS_23_976,
    "29.97": FPS_29_97,
    "59.94": FPS_59_94,
};

const DEFAULT_HANDLE_S = 0.5;

function gcd(a, b) {
    a = Math.abs(Math.trunc(a));
    b = Math.abs(Math.trunc(b));
    while (b) {
        const t = b;
        b = a % b;
        a = t;
    }
    return a || 1;
}

function reduce(num, den) {
    const g = gcd(num, den);
    let n = Math.trunc(num / g);
    let d = Math.trunc(den / g);
    if (d < 0) {
        n = -n;
        d = -d;
    }
    return { num: n, den: d };
}

function roundHalfUp(value) {
    // Half away from zero — not JS/Python-3 banker's round (2.5 → 2).
    if (!Number.isFinite(value)) {
        throw new Error(`roundHalfUp expects a finite number, got ${value}`);
    }
    if (value >= 0) return Math.floor(value + 0.5);
    return Math.ceil(value - 0.5);
}

function floatToRatio(value) {
    if (value === 0) return { num: 0, den: 1 };
    const buf = new ArrayBuffer(8);
    const view = new DataView(buf);
    view.setFloat64(0, value);
    const bits = view.getBigUint64(0);
    const sign = bits >> 63n === 0n ? 1n : -1n;
    const expBits = Number((bits >> 52n) & 0x7ffn);
    const fracBits = bits & 0xfffffffffffffn;
    let exp;
    let mant;
    if (expBits === 0) {
        exp = -1022 - 52;
        mant = fracBits;
    } else {
        exp = expBits - 1023 - 52;
        mant = fracBits | (1n << 52n);
    }
    let num = sign * mant;
    let den = 1n;
    if (exp >= 0) {
        num <<= BigInt(exp);
    } else {
        den <<= BigInt(-exp);
    }
    const g = bigGcd(num < 0n ? -num : num, den);
    return { num: Number(num / g), den: Number(den / g) };
}

function bigGcd(a, b) {
    while (b !== 0n) {
        const t = b;
        b = a % b;
        a = t;
    }
    return a === 0n ? 1n : a;
}

function limitDenominator(num, den, maxDen) {
    if (den <= maxDen) return reduce(num, den);
    let p0 = 0;
    let q0 = 1;
    let p1 = 1;
    let q1 = 0;
    let n = num;
    let d = den;
    while (true) {
        const a = Math.floor(n / d);
        const q2 = q0 + a * q1;
        if (q2 > maxDen) break;
        const p2 = p0 + a * p1;
        p0 = p1;
        q0 = q1;
        p1 = p2;
        q1 = q2;
        const nextN = d;
        d = n - a * d;
        n = nextN;
        if (d === 0) break;
    }
    const k = Math.floor((maxDen - q0) / q1);
    const b1n = p0 + k * p1;
    const b1d = q0 + k * q1;
    const self = num / den;
    const err1 = Math.abs(b1n / b1d - self);
    const err2 = Math.abs(p1 / q1 - self);
    if (err2 <= err1) return reduce(p1, q1);
    return reduce(b1n, b1d);
}

function asFps(fps) {
    if (fps && typeof fps === "object" && !Array.isArray(fps)) {
        const num = fps.num != null ? fps.num : fps.numerator;
        const den = fps.den != null ? fps.den : fps.denominator;
        if (num != null && den != null) {
            if (!(Number(num) / Number(den) > 0)) {
                throw new Error(`fps must be positive, got ${num}/${den}`);
            }
            return reduce(Number(num), Number(den));
        }
    }
    if (Array.isArray(fps)) {
        if (fps.length !== 2) {
            throw new Error(`fps tuple must be (num, den), got ${fps}`);
        }
        const frac = reduce(Number(fps[0]), Number(fps[1]));
        if (!(frac.num / frac.den > 0)) {
            throw new Error(`fps must be positive, got ${fps}`);
        }
        return frac;
    }
    const value = Number(fps);
    if (!Number.isFinite(value) || value <= 0) {
        throw new Error(`fps must be positive, got ${fps}`);
    }
    const exact = floatToRatio(value);
    return limitDenominator(exact.num, exact.den, 1001);
}

function parseTimelineFrameRate(raw) {
    if (raw == null) {
        throw new Error("timelineFrameRate is empty");
    }
    let text = String(raw).trim();
    if (!text) {
        throw new Error("timelineFrameRate is empty");
    }
    if (text.toUpperCase().endsWith("DF")) {
        text = text.slice(0, -2).trim();
    }
    if (Object.prototype.hasOwnProperty.call(NTSC_RATE_STRINGS, text)) {
        return asFps(NTSC_RATE_STRINGS[text]);
    }
    const value = Number(text);
    if (!Number.isFinite(value)) {
        throw new Error(`unrecognized timelineFrameRate: ${raw}`);
    }
    if (value <= 0) {
        throw new Error(`timelineFrameRate must be positive, got ${raw}`);
    }
    for (const [label, rational] of Object.entries(NTSC_RATE_STRINGS)) {
        if (Math.abs(value - Number(label)) < 0.001) {
            return asFps(rational);
        }
    }
    if (Math.abs(value - Math.round(value)) < 1e-6) {
        return asFps([Math.round(value), 1]);
    }
    return asFps(value);
}

function parseMediaStartSeconds(raw) {
    if (raw == null) return null;
    if (typeof raw === "number" && Number.isFinite(raw)) return raw;
    const text = String(raw).trim();
    if (!text) return null;
    if (!text.includes(":")) {
        const n = Number(text);
        return Number.isFinite(n) ? n : null;
    }
    const m = text.match(/^(\d+):(\d{2}):(\d{2})(?:[:;.](\d+))?/);
    if (!m) return null;
    return Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]);
}

function startTcFromDump(dump) {
    if (!dump || typeof dump !== "object") return null;
    for (const [key, val] of Object.entries(dump)) {
        if (!/start\s*(tc|timecode)/i.test(key)) continue;
        const n = parseMediaStartSeconds(val);
        if (n != null) return n;
    }
    return null;
}

/**
 * GetSourceStartTime() is often reel / time-of-day TC (21:05:40),
 * not seconds from byte 0 of the file. Map onto [0, fileDur].
 */
function fileRelativeTimes(t0, t1, fileDur, opts) {
    const options = opts || {};
    if (!(fileDur > 0) || !(t1 > t0) || !Number.isFinite(t0) || !Number.isFinite(t1)) {
        return { t0, t1, shifted: false, origin: 0 };
    }
    const eps = 1e-3;
    if (t0 >= -eps && t1 <= fileDur + 1) {
        return {
            t0: Math.max(0, t0),
            t1: Math.min(t1, fileDur),
            shifted: false,
            origin: 0,
        };
    }
    const startTc = options.startTc;
    if (startTc != null && Number.isFinite(startTc)) {
        const a = t0 - startTc;
        const b = t1 - startTc;
        if (b > a && a < fileDur && b > 0) {
            return {
                t0: Math.max(0, a),
                t1: Math.min(b, fileDur),
                shifted: true,
                origin: startTc,
            };
        }
    }
    const leftS = options.leftOffsetSeconds;
    if (leftS != null && Number.isFinite(leftS) && leftS >= 0 && leftS < fileDur) {
        const span = t1 - t0;
        const a = leftS;
        const b = Math.min(leftS + span, fileDur);
        if (b > a) {
            return { t0: a, t1: b, shifted: true, origin: t0 - a };
        }
    }
    if (t0 >= fileDur - eps) {
        const span = t1 - t0;
        return { t0: 0, t1: Math.min(span, fileDur), shifted: true, origin: t0 };
    }
    return { t0, t1, shifted: false, origin: 0 };
}

function actualHandles(t0, t1, fileDur, handleS = DEFAULT_HANDLE_S) {
    // Fixture: t0=0.2, H=0.5 → H_left_actual=0.2 (not 0.5).
    if (handleS < 0) {
        throw new Error(`handle_s must be >= 0, got ${handleS}`);
    }
    const hLeft = Math.min(handleS, Math.max(0, t0));
    const hRight = Math.min(handleS, Math.max(0, fileDur - t1));
    return { hLeftActual: hLeft, hRightActual: hRight };
}

function extractSampleRange(t0, t1, fileDur, srcSr, handleS = DEFAULT_HANDLE_S) {
    if (!(srcSr > 0)) {
        throw new Error(`src_sr must be positive, got ${srcSr}`);
    }
    const { hLeftActual, hRightActual } = actualHandles(t0, t1, fileDur, handleS);
    const srcIn = roundHalfUp((t0 - hLeftActual) * srcSr);
    const srcOut = roundHalfUp((t1 + hRightActual) * srcSr);
    if (srcOut < srcIn) {
        throw new Error(
            `empty extract window: src_in=${srcIn} src_out=${srcOut} ` +
                `(t0=${t0} t1=${t1} file_dur=${fileDur})`,
        );
    }
    return {
        hLeftActual,
        hRightActual,
        srcInSample: srcIn,
        srcOutSample: srcOut,
        srcSr,
        srcSampleCount: srcOut - srcIn,
    };
}

function placeFrames(t0, t1, fileDur, outFps, handleS = DEFAULT_HANDLE_S, handlesLeftActual) {
    const fps = asFps(outFps);
    const hLeft =
        handlesLeftActual != null
            ? Number(handlesLeftActual)
            : actualHandles(t0, t1, fileDur, handleS).hLeftActual;
    if (!Number.isFinite(hLeft) || hLeft < 0) {
        throw new Error(`handles_left_actual must be >= 0, got ${handlesLeftActual}`);
    }
    const fpsF = fps.num / fps.den;
    const start = roundHalfUp(hLeft * fpsF);
    const endExcl = roundHalfUp((hLeft + (t1 - t0)) * fpsF);
    if (endExcl <= start) {
        throw new Error(
            `empty place window: start=${start} end_excl=${endExcl} ` +
                `(t0=${t0} t1=${t1} out_fps=${fps.num}/${fps.den})`,
        );
    }
    return {
        handleStartFrame: start,
        handleEndFrameExclusive: endExcl,
        // Official 7_add_subclips_to_timeline.py: 0..23 = 24 frames.
        handleEndFrame: endExcl - 1,
        outFpsNum: fps.num,
        outFpsDen: fps.den,
        bodyFrameCount: endExcl - start,
        hLeftActual: hLeft,
    };
}

function expectedOutputSampleCount(t0, t1, fileDur, projSr, handleS = DEFAULT_HANDLE_S) {
    if (!(projSr > 0)) {
        throw new Error(`proj_sr must be positive, got ${projSr}`);
    }
    const { hLeftActual, hRightActual } = actualHandles(t0, t1, fileDur, handleS);
    return roundHalfUp((t1 - t0 + hLeftActual + hRightActual) * projSr);
}

function appendClipInfo(mediaPoolItem, place, recordFrame, trackIndex, mediaType = 2) {
    return {
        mediaPoolItem,
        startFrame: place.handleStartFrame,
        endFrame: place.handleEndFrame,
        mediaType,
        trackIndex,
        recordFrame,
    };
}

function fpsFloat(fps) {
    const f = asFps(fps);
    return f.num / f.den;
}

module.exports = {
    FPS_23_976,
    FPS_24,
    FPS_25,
    FPS_29_97,
    FPS_59_94,
    DEFAULT_HANDLE_S,
    roundHalfUp,
    asFps,
    parseTimelineFrameRate,
    actualHandles,
    fileRelativeTimes,
    parseMediaStartSeconds,
    startTcFromDump,
    extractSampleRange,
    placeFrames,
    expectedOutputSampleCount,
    appendClipInfo,
    fpsFloat,
};
