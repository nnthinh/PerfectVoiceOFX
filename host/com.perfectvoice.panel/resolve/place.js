"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

const {
    DEFAULT_HANDLE_S,
    actualHandles,
    appendClipInfo,
    asFps,
    expectedOutputSampleCount,
    placeFrames,
    parseTimelineFrameRate,
    roundHalfUp,
} = require("./time");
const { callValue, isCallable } = require("./selection");
const { inspectSelection } = require("./inspect");

const ISOLATED_TRACK = "PV Isolated Voice";
const MEDIA_BIN = "PerfectVoice";
const DEFAULT_SR = 48000;

function existsFile(p) {
    try {
        return fs.statSync(p).isFile();
    } catch {
        return false;
    }
}

function writeSilenceWav(filePath, sampleRate, nFrames, channels = 2) {
    const bytesPerSample = 2;
    const blockAlign = channels * bytesPerSample;
    const dataSize = nFrames * blockAlign;
    const buf = Buffer.alloc(44 + dataSize);
    buf.write("RIFF", 0);
    buf.writeUInt32LE(36 + dataSize, 4);
    buf.write("WAVE", 8);
    buf.write("fmt ", 12);
    buf.writeUInt32LE(16, 16);
    buf.writeUInt16LE(1, 20);
    buf.writeUInt16LE(channels, 22);
    buf.writeUInt32LE(sampleRate, 24);
    buf.writeUInt32LE(sampleRate * blockAlign, 28);
    buf.writeUInt16LE(blockAlign, 32);
    buf.writeUInt16LE(16, 34);
    buf.write("data", 36);
    buf.writeUInt32LE(dataSize, 40);
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.writeFileSync(filePath, buf);
    return filePath;
}

function readWavStats(filePath) {
    const fd = fs.openSync(filePath, "r");
    const header = Buffer.alloc(44);
    const n = fs.readSync(fd, header, 0, 44, 0);
    fs.closeSync(fd);
    if (n < 44 || header.toString("ascii", 0, 4) !== "RIFF" || header.toString("ascii", 8, 12) !== "WAVE") {
        throw new Error(`not a RIFF/WAVE file: ${filePath}`);
    }
    const channels = header.readUInt16LE(22);
    const sampleRate = header.readUInt32LE(24);
    const bits = header.readUInt16LE(34);
    const dataSize = header.readUInt32LE(40);
    const bytesPerSample = bits / 8;
    const nFrames =
        channels && bytesPerSample ? Math.floor(dataSize / (channels * bytesPerSample)) : 0;
    const durationS = sampleRate ? nFrames / sampleRate : 0;
    return { path: filePath, sampleRate, nFrames, channels, bits, durationS };
}

function wavCoversPlace(stats, place, fps) {
    const fpsF = fps.num / fps.den;
    const wavEndExcl = roundHalfUp(stats.durationS * fpsF);
    const endExcl = place.handleEndFrameExclusive;
    if (endExcl > wavEndExcl) {
        return {
            ok: false,
            note:
                `endFrame inclusive=${place.handleEndFrame} (exclusive=${endExcl}) ` +
                `past WAV grid exclusive=${wavEndExcl} ` +
                `(${stats.durationS.toFixed(4)}s @ ${fps.num}/${fps.den})`,
        };
    }
    return {
        ok: true,
        note: `WAV ${stats.nFrames} samples @ ${stats.sampleRate} Hz covers exclusive end ${endExcl}`,
    };
}

function ensureBin(mediaPool, name) {
    const root = callValue(mediaPool, "GetRootFolder");
    if (!root) return null;
    const subs = callValue(root, "GetSubFolderList") || [];
    if (Array.isArray(subs)) {
        for (const folder of subs) {
            if (callValue(folder, "GetName") === name) {
                if (isCallable(mediaPool, "SetCurrentFolder")) mediaPool.SetCurrentFolder(folder);
                return folder;
            }
        }
    }
    const created = callValue(mediaPool, "AddSubFolder", root, name);
    if (created && isCallable(mediaPool, "SetCurrentFolder")) {
        mediaPool.SetCurrentFolder(created);
    }
    return created || null;
}

function ensureIsolatedTrack(timeline, name) {
    const count = Number(callValue(timeline, "GetTrackCount", "audio")) || 0;
    for (let idx = 1; idx <= count; idx += 1) {
        if (callValue(timeline, "GetTrackName", "audio", idx) === name) {
            return idx;
        }
    }
    const ok = callValue(timeline, "AddTrack", "audio", "stereo");
    if (!ok) return null;
    const newIdx = Number(callValue(timeline, "GetTrackCount", "audio"));
    if (!Number.isFinite(newIdx) || newIdx < 1) return null;
    if (isCallable(timeline, "SetTrackName")) {
        try {
            timeline.SetTrackName("audio", newIdx, name);
        } catch {
            // track still usable unnamed
        }
    }
    return newIdx;
}

function resolveOutFps(resolve, params) {
    if (params && params.outFps != null) {
        return { fps: asFps(params.outFps), source: "params.outFps" };
    }
    const pm = callValue(resolve, "GetProjectManager");
    const project = pm ? callValue(pm, "GetCurrentProject") : null;
    if (project && isCallable(project, "GetSetting")) {
        const raw = callValue(project, "GetSetting", "timelineFrameRate");
        if (raw != null) {
            try {
                return {
                    fps: parseTimelineFrameRate(raw),
                    source: `project.GetSetting('timelineFrameRate')=${JSON.stringify(raw)}`,
                };
            } catch {
                // fall through
            }
        }
    }
    return { fps: asFps([24, 1]), source: "default_24/1" };
}

function computePlace(params) {
    const t0 = Number(params.t0);
    const t1 = Number(params.t1);
    if (!Number.isFinite(t0) || !Number.isFinite(t1) || t1 <= t0) {
        throw new Error("place requires t0 < t1");
    }
    const handleS = params.handleS != null ? Number(params.handleS) : DEFAULT_HANDLE_S;
    const fileDur = params.fileDur != null ? Number(params.fileDur) : t1 + 1e6;
    const hLeft =
        params.handlesLeftActual != null
            ? Number(params.handlesLeftActual)
            : actualHandles(t0, t1, fileDur, handleS).hLeftActual;
    const fps = asFps(params.outFps);
    const place = placeFrames(t0, t1, fileDur, fps, handleS, hLeft);
    return { place, t0, t1, handleS, fileDur, hLeft, fps };
}

function defaultTestWavPath() {
    return path.join(
        os.homedir(),
        "Library/Application Support/PerfectVoice/tmp/place-test.wav",
    );
}

function placeIsolated(resolve, params) {
    const p = params || {};
    if (!resolve) {
        return { ok: false, error: "Resolve is not connected." };
    }
    if (!p.wavPath) {
        return { ok: false, error: "wavPath is required." };
    }
    if (!existsFile(p.wavPath)) {
        return { ok: false, error: `WAV not found: ${p.wavPath}` };
    }
    if (p.recordFrame == null || !Number.isFinite(Number(p.recordFrame))) {
        return { ok: false, error: "recordFrame is required (original TimelineItem.GetStart())." };
    }

    let computed;
    try {
        const fpsInfo = p.outFps != null ? { fps: asFps(p.outFps) } : resolveOutFps(resolve, p);
        computed = computePlace({ ...p, outFps: fpsInfo.fps });
    } catch (err) {
        return { ok: false, error: err && err.message ? err.message : String(err) };
    }

    let stats;
    try {
        stats = readWavStats(p.wavPath);
    } catch (err) {
        stats = null;
        computed.wavStatError = err && err.message ? err.message : String(err);
    }
    if (stats) {
        const cover = wavCoversPlace(stats, computed.place, computed.fps);
        if (!cover.ok) {
            return { ok: false, error: cover.note, wav: stats, place: computed.place };
        }
        computed.coverNote = cover.note;
    }

    const pm = callValue(resolve, "GetProjectManager");
    const project = pm ? callValue(pm, "GetCurrentProject") : null;
    if (!project) return { ok: false, error: "No current project." };
    const timeline = callValue(project, "GetCurrentTimeline");
    const mediaPool = callValue(project, "GetMediaPool");
    if (!timeline || !mediaPool) {
        return { ok: false, error: "Need a current timeline and media pool." };
    }

    ensureBin(mediaPool, p.binName || MEDIA_BIN);
    const imported = callValue(mediaPool, "ImportMedia", [path.resolve(p.wavPath)]);
    if (!Array.isArray(imported) || !imported.length) {
        return { ok: false, error: `ImportMedia failed for ${p.wavPath}` };
    }
    const mediaPoolItem = imported[0];

    let trackIndex = p.trackIndex != null ? Number(p.trackIndex) : 0;
    if (!trackIndex) {
        trackIndex = ensureIsolatedTrack(timeline, p.trackName || ISOLATED_TRACK);
        if (trackIndex == null) {
            return { ok: false, error: 'AddTrack("audio", "stereo") failed' };
        }
    }

    const clipInfo = appendClipInfo(
        mediaPoolItem,
        computed.place,
        Number(p.recordFrame),
        trackIndex,
        2,
    );
    const placed = callValue(mediaPool, "AppendToTimeline", [clipInfo]);
    return {
        ok: Boolean(placed),
        trackIndex,
        trackName: p.trackName || ISOLATED_TRACK,
        recordFrame: Number(p.recordFrame),
        clipInfo: {
            startFrame: clipInfo.startFrame,
            endFrame: clipInfo.endFrame,
            mediaType: clipInfo.mediaType,
            trackIndex: clipInfo.trackIndex,
            recordFrame: clipInfo.recordFrame,
        },
        place: computed.place,
        handlesLeftActual: computed.hLeft,
        wav: stats || { path: p.wavPath },
        coverNote: computed.coverNote || null,
        placedCount: Array.isArray(placed) ? placed.length : placed ? 1 : 0,
    };
}

function pickPlaceClip(inspect) {
    if (!inspect || !Array.isArray(inspect.clips)) return null;
    const jobs = inspect.clips.filter((c) => !c.suppressedDuplicate);
    const accepted = jobs.filter((c) => !c.rejected && c.t0 != null && c.t1 != null && c.recordFrame != null);
    if (accepted.length) return accepted[0];
    return jobs.find((c) => c.t0 != null && c.t1 != null && c.recordFrame != null) || null;
}

function placeTestWav(resolve, params) {
    const p = params || {};
    const inspect = inspectSelection(resolve, { handleS: p.handleS });
    if (!inspect.ok) return inspect;
    const clip = pickPlaceClip(inspect);
    if (!clip) {
        return {
            ok: false,
            error: "No selected clip with source times and recordFrame. Select a clip first.",
            inspect,
        };
    }

    const handleS = p.handleS != null ? p.handleS : clip.handleS || DEFAULT_HANDLE_S;
    const fileDur = clip.fileDur != null ? clip.fileDur : clip.t1 + 1e6;
    const hLeft = clip.hLeftActual != null ? clip.hLeftActual : actualHandles(clip.t0, clip.t1, fileDur, handleS).hLeftActual;
    const projSr = p.sampleRate || clip.sampleRate || DEFAULT_SR;
    const nOut = expectedOutputSampleCount(clip.t0, clip.t1, fileDur, projSr, handleS);
    const wavPath = p.wavPath || defaultTestWavPath();
    let generated = false;
    if (!p.wavPath || !existsFile(p.wavPath)) {
        writeSilenceWav(wavPath, projSr, nOut, 2);
        generated = true;
    }

    const result = placeIsolated(resolve, {
        wavPath,
        recordFrame: clip.recordFrame,
        t0: clip.t0,
        t1: clip.t1,
        fileDur,
        handleS,
        handlesLeftActual: hLeft,
        outFps: inspect.outFps,
        trackName: p.trackName || ISOLATED_TRACK,
        binName: p.binName || MEDIA_BIN,
    });
    return {
        ...result,
        generated,
        nOut,
        clipName: clip.name,
        clipRejected: Boolean(clip.rejected),
        inspectSource: inspect.source,
        warnings: [
            ...(inspect.warnings || []),
            ...(clip.rejected
                ? ["Clip is rejected for jobs; place test WAV still ran (does not POST /v1/jobs)."]
                : []),
        ],
    };
}

module.exports = {
    ISOLATED_TRACK,
    MEDIA_BIN,
    writeSilenceWav,
    readWavStats,
    wavCoversPlace,
    ensureIsolatedTrack,
    ensureBin,
    computePlace,
    placeIsolated,
    placeTestWav,
    pickPlaceClip,
    defaultTestWavPath,
};
