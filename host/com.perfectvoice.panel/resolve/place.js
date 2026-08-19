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
const { callValue, isCallable, collectSelected, groupSelectedItems } = require("./selection");
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
    const buf = fs.readFileSync(filePath);
    if (buf.length < 12 || buf.toString("ascii", 0, 4) !== "RIFF" || buf.toString("ascii", 8, 12) !== "WAVE") {
        throw new Error(`not a RIFF/WAVE file: ${filePath}`);
    }
    let offset = 12;
    let fmt = null;
    let dataSize = null;
    while (offset + 8 <= buf.length) {
        const id = buf.toString("ascii", offset, offset + 4);
        const size = buf.readUInt32LE(offset + 4);
        const start = offset + 8;
        if (id === "fmt " && size >= 16 && start + 16 <= buf.length) {
            fmt = {
                channels: buf.readUInt16LE(start + 2),
                sampleRate: buf.readUInt32LE(start + 4),
                bits: buf.readUInt16LE(start + 14),
            };
        } else if (id === "data") {
            dataSize = size;
            if (fmt) break;
        }
        offset = start + size + (size % 2);
    }
    if (!fmt || dataSize == null || !(fmt.channels > 0) || !(fmt.bits > 0) || !(fmt.sampleRate > 0)) {
        return {
            path: filePath,
            sampleRate: fmt && fmt.sampleRate,
            nFrames: null,
            channels: fmt && fmt.channels,
            bits: fmt && fmt.bits,
            durationS: null,
            canonical: false,
        };
    }
    const bytesPerSample = fmt.bits / 8;
    const nFrames = Math.floor(dataSize / (fmt.channels * bytesPerSample));
    const durationS = nFrames / fmt.sampleRate;
    return {
        path: filePath,
        sampleRate: fmt.sampleRate,
        nFrames,
        channels: fmt.channels,
        bits: fmt.bits,
        durationS,
        canonical: true,
    };
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

function appendSucceeded(placed) {
    if (Array.isArray(placed)) return placed.length > 0;
    return Boolean(placed);
}

function pickHandlesLeftActual(params) {
    // Engine job result field is handles_left_actual (Appendix A). Place must
    // use that, not default H, or startFrame drifts when t0 > requested H.
    if (!params) return null;
    if (params.handles_left_actual != null) return Number(params.handles_left_actual);
    if (params.handlesLeftActual != null) return Number(params.handlesLeftActual);
    return null;
}

function computePlace(params) {
    const t0 = Number(params.t0);
    const t1 = Number(params.t1);
    if (!Number.isFinite(t0) || !Number.isFinite(t1) || t1 <= t0) {
        throw new Error("place requires t0 < t1");
    }
    const handleS = params.handleS != null ? Number(params.handleS) : DEFAULT_HANDLE_S;
    const fileDur = params.fileDur != null ? Number(params.fileDur) : t1 + 1e6;
    const fromEngine = pickHandlesLeftActual(params);
    const hLeft =
        fromEngine != null && Number.isFinite(fromEngine)
            ? fromEngine
            : actualHandles(t0, t1, fileDur, handleS).hLeftActual;
    const fps = asFps(params.outFps);
    const place = placeFrames(t0, t1, fileDur, fps, handleS, hLeft);
    return { place, t0, t1, handleS, fileDur, hLeft, fps };
}

function defaultTestWavPath() {
    if (process.platform === "win32") {
        const root =
            process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
        return path.join(root, "PerfectVoice", "tmp", "place-test.wav");
    }
    return path.join(
        os.homedir(),
        "Library/Application Support/PerfectVoice/tmp/place-test.wav",
    );
}

function removeExistingTimelineClips(timeline, trackIndex, startRec, endRec) {
    if (!timeline || !trackIndex || !isCallable(timeline, "GetItemListInTrack")) return;
    try {
        const existingClips = timeline.GetItemListInTrack("audio", trackIndex);
        if (Array.isArray(existingClips) && existingClips.length && isCallable(timeline, "DeleteClips")) {
            const toDelete = [];
            for (const item of existingClips) {
                if (!item) continue;
                const itemStart = isCallable(item, "GetStart") ? Number(item.GetStart()) : null;
                const itemEnd = isCallable(item, "GetEnd") ? Number(item.GetEnd()) : null;
                if (itemStart != null && itemEnd != null) {
                    if (itemStart < endRec && itemEnd > startRec) {
                        toDelete.push(item);
                    }
                }
            }
            if (toDelete.length) {
                timeline.DeleteClips(toDelete);
            }
        }
    } catch {
        // non-fatal if track cleanup is not supported
    }
}

function removeExistingMediaPoolClip(mediaPool, bin, wavPath) {
    if (!mediaPool || !bin || !wavPath) return;
    try {
        const fullPath = path.resolve(wavPath);
        const baseName = path.basename(wavPath);
        const clipList = callValue(bin, "GetClipList") || [];
        if (Array.isArray(clipList) && clipList.length) {
            const toDelete = [];
            for (const item of clipList) {
                if (!item) continue;
                let match = false;
                if (isCallable(item, "GetClipProperty")) {
                    const itemPath = item.GetClipProperty("File Path");
                    if (itemPath && path.resolve(itemPath) === fullPath) {
                        match = true;
                    }
                }
                if (!match && isCallable(item, "GetName")) {
                    const itemName = item.GetName();
                    if (itemName === baseName || itemName === path.parse(baseName).name) {
                        match = true;
                    }
                }
                if (match) {
                    toDelete.push(item);
                }
            }
            if (toDelete.length && isCallable(mediaPool, "DeleteClips")) {
                mediaPool.DeleteClips(toDelete);
            }
        }
    } catch {
        // non-fatal
    }
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
    if (stats && stats.canonical && Number.isFinite(stats.durationS)) {
        const cover = wavCoversPlace(stats, computed.place, computed.fps);
        if (!cover.ok) {
            return { ok: false, error: cover.note, wav: stats, place: computed.place };
        }
        computed.coverNote = cover.note;
    } else if (stats && !stats.canonical) {
        computed.coverNote = "WAV data chunk was not found; skipped cover check";
    }

    const pm = callValue(resolve, "GetProjectManager");
    const project = pm ? callValue(pm, "GetCurrentProject") : null;
    if (!project) return { ok: false, error: "No current project." };
    const timeline = callValue(project, "GetCurrentTimeline");
    const mediaPool = callValue(project, "GetMediaPool");
    if (!timeline || !mediaPool) {
        return { ok: false, error: "Need a current timeline and media pool." };
    }

    // 1. Ensure isolated audio track exists
    let trackIndex = p.trackIndex != null ? Number(p.trackIndex) : 0;
    if (!trackIndex) {
        trackIndex = ensureIsolatedTrack(timeline, p.trackName || ISOLATED_TRACK);
        if (trackIndex == null) {
            return { ok: false, error: 'AddTrack("audio", "stereo") failed' };
        }
    }

    // 2. STEP 1: Delete old audio clip on the timeline in this frame range
    const startRec = Number(p.recordFrame);
    const endRec = startRec + (Number(computed.place.endFrame) - Number(computed.place.startFrame));
    removeExistingTimelineClips(timeline, trackIndex, startRec, endRec);

    // 3. STEP 2: Delete old MediaPoolItem from Media Pool bin (so Resolve re-reads fresh audio from disk)
    const bin = ensureBin(mediaPool, p.binName || MEDIA_BIN);
    if (bin) {
        removeExistingMediaPoolClip(mediaPool, bin, p.wavPath);
    }

    // 4. STEP 3: Import fresh WAV into Media Pool
    const imported = callValue(mediaPool, "ImportMedia", [path.resolve(p.wavPath)]);
    if (!Array.isArray(imported) || !imported.length) {
        return { ok: false, error: `ImportMedia failed for ${p.wavPath}` };
    }
    const mediaPoolItem = imported[0];
    if (mediaPoolItem) {
        if (isCallable(mediaPoolItem, "SetClipProperty")) {
            try {
                mediaPoolItem.SetClipProperty("Start TC", "00:00:00:00");
            } catch {
                // property name may differ; recordFrame is still applied below
            }
        }
        if (isCallable(mediaPoolItem, "SetClipMarkInOut")) {
            try {
                mediaPoolItem.SetClipMarkInOut(
                    Number(computed.place.startFrame),
                    Number(computed.place.endFrame),
                );
            } catch {
                // optional
            }
        }
    }

    // 5. STEP 4: Append fresh clip to Timeline
    const clipInfo = appendClipInfo(
        mediaPoolItem,
        computed.place,
        Number(p.recordFrame),
        trackIndex,
        2,
    );
    const placed = callValue(mediaPool, "AppendToTimeline", [clipInfo]);
    const ok = appendSucceeded(placed);
    const placedCount = Array.isArray(placed) ? placed.length : placed ? 1 : 0;
    return {
        ok,
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
        placedCount,
    };
}

function clipMatchesMuteTarget(row, target, recordFrame) {
    if (!target) return false;
    if (target.uniqueId && row.uniqueId && target.uniqueId === row.uniqueId) return true;
    if (target.filePath && row.filePath && target.filePath === row.filePath) {
        if (target.recordFrame == null || recordFrame == null) return true;
        return Number(target.recordFrame) === Number(recordFrame);
    }
    return false;
}

function muteOriginalClips(resolve, targets) {
    const list = Array.isArray(targets) ? targets.filter(Boolean) : [];
    if (!list.length) {
        return { ok: true, muted: 0, warnings: [] };
    }
    if (!resolve) {
        return { ok: false, error: "Resolve is not connected.", muted: 0, warnings: [] };
    }
    const pm = callValue(resolve, "GetProjectManager");
    const project = pm ? callValue(pm, "GetCurrentProject") : null;
    const timeline = project ? callValue(project, "GetCurrentTimeline") : null;
    if (!timeline) {
        return { ok: false, error: "Need a current timeline to mute originals.", muted: 0, warnings: [] };
    }
    const selected = collectSelected(timeline);
    const rows = groupSelectedItems(selected.items);
    let muted = 0;
    const warnings = [];
    const seen = new Set();
    for (const row of rows) {
        if (row.suppressedDuplicate) continue;
        const recordFrame = row.item ? Number(callValue(row.item, "GetStart")) : null;
        const match = list.find((t) => clipMatchesMuteTarget(row, t, recordFrame));
        if (!match) continue;
        const key = row.uniqueId || `${row.filePath}:${recordFrame}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const item = row.item;
        const label = row.name || match.name || match.display_name || "clip";
        if (!item || !isCallable(item, "SetClipEnabled")) {
            warnings.push(`Cannot mute “${label}”: SetClipEnabled is unavailable.`);
            continue;
        }
        const ok = callValue(item, "SetClipEnabled", false);
        if (ok === false) {
            warnings.push(`SetClipEnabled failed for “${label}”.`);
        } else {
            muted += 1;
        }
    }
    if (!muted && !warnings.length) {
        warnings.push("No matching original clips were muted. Select the same clips and try again.");
    }
    return { ok: true, muted, warnings };
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
    pickHandlesLeftActual,
    appendSucceeded,
    placeIsolated,
    placeTestWav,
    muteOriginalClips,
    clipMatchesMuteTarget,
    pickPlaceClip,
    defaultTestWavPath,
};
