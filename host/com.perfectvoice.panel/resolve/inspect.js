"use strict";

const {
    DEFAULT_HANDLE_S,
    actualHandles,
    fileRelativeTimes,
    startTcFromDump,
    parseTimelineFrameRate,
    asFps,
} = require("./time");
const { evaluateReject, parseAudioMapping, UNCONFIRMED } = require("./reject");
const {
    call,
    callValue,
    isCallable,
    clipPropertyDump,
    collectSelected,
    groupSelectedItems,
    parseVersionFields,
    selectionStrategy,
} = require("./selection");

function finiteNumber(value) {
    const n = typeof value === "number" ? value : Number(value);
    return Number.isFinite(n) ? n : null;
}

function timelineStartFrame(item) {
    // README: GetStart([subframe_precision]) — prefer subframe when the host accepts it.
    const sub = finiteNumber(callValue(item, "GetStart", true));
    if (sub != null) return sub;
    return finiteNumber(callValue(item, "GetStart"));
}

function sourceTimes(item) {
    const t0 = finiteNumber(callValue(item, "GetSourceStartTime"));
    const t1 = finiteNumber(callValue(item, "GetSourceEndTime"));
    if (t0 != null && t1 != null) {
        return { t0, t1, source: "GetSourceStartTime/GetSourceEndTime" };
    }
    const f0 = finiteNumber(callValue(item, "GetSourceStartFrame"));
    const f1 = finiteNumber(callValue(item, "GetSourceEndFrame"));
    if (f0 != null && f1 != null) {
        return { t0: null, t1: null, sourceStartFrame: f0, sourceEndFrame: f1, source: "GetSourceStartFrame/GetSourceEndFrame" };
    }
    return { t0: null, t1: null, source: null };
}

function fpsFromDump(dump) {
    if (!dump || typeof dump !== "object") return null;
    for (const [key, val] of Object.entries(dump)) {
        if (!/fps|frame\s*rate/i.test(key)) continue;
        try {
            return { fps: parseTimelineFrameRate(val), key };
        } catch {
            // not a usable rate
        }
    }
    return null;
}

function fileDuration(dump, t1, item, srcFps) {
    if (dump && typeof dump === "object") {
        for (const [key, val] of Object.entries(dump)) {
            if (!/^duration/i.test(key)) continue;
            const n = finiteNumber(val);
            if (n != null && n > 0) return { fileDur: n, source: key, assumed: false };
        }
        const frames = finiteNumber(dump.Frames);
        if (frames != null && srcFps && srcFps.num > 0) {
            return {
                fileDur: frames / (srcFps.num / srcFps.den),
                source: "Frames",
                assumed: false,
            };
        }
    }
    const right = finiteNumber(callValue(item, "GetRightOffset"));
    if (t1 != null && right != null && srcFps && srcFps.num > 0) {
        return {
            fileDur: t1 + right / (srcFps.num / srcFps.den),
            source: "t1+GetRightOffset",
            assumed: false,
        };
    }
    if (t1 != null) {
        return { fileDur: t1 + 1e6, source: "unknown_assume_unclamped", assumed: true };
    }
    return { fileDur: null, source: null, assumed: true };
}

function liveTimelineFps(project) {
    if (!isCallable(project, "GetSetting")) return { fps: asFps([24, 1]), raw: null, source: "default_24/1" };
    const raw = callValue(project, "GetSetting", "timelineFrameRate");
    if (raw == null) return { fps: asFps([24, 1]), raw, source: "default_24/1" };
    try {
        return {
            fps: parseTimelineFrameRate(raw),
            raw,
            source: `project.GetSetting('timelineFrameRate')=${JSON.stringify(raw)}`,
        };
    } catch {
        return { fps: asFps([24, 1]), raw, source: `default_24/1 (unparsed timelineFrameRate=${raw})` };
    }
}

function parseSampleRate(raw) {
    if (raw == null) return null;
    if (typeof raw === "number" && raw > 0) return Math.round(raw);
    const text = String(raw).trim();
    const m = text.match(/(\d+(?:\.\d+)?)/);
    if (!m) return null;
    const n = Number(m[1]);
    if (!Number.isFinite(n) || n <= 0) return null;
    if (n < 1000) return Math.round(n * 1000);
    return Math.round(n);
}

function liveProjectSampleRate(project) {
    // README names timelineSampleRate; do not invent other keys as primary.
    if (project && isCallable(project, "GetSetting")) {
        for (const key of ["timelineSampleRate", "audioSampleRate"]) {
            const raw = callValue(project, "GetSetting", key);
            const n = parseSampleRate(raw);
            if (n) {
                return {
                    sampleRate: n,
                    source: `project.GetSetting(${JSON.stringify(key)})=${JSON.stringify(raw)}`,
                };
            }
        }
    }
    return { sampleRate: 48000, source: "default_48000" };
}

function inspectClip(row, ctx) {
    const item = row.item;
    const mp =
        row.mediaPoolItem ||
        (item ? callValue(item, "GetMediaPoolItem") : null);
    const dumped = row.clipPropertyDump;
    const dump =
        dumped && typeof dumped === "object" && Object.keys(dumped).length
            ? dumped
            : mp
              ? clipPropertyDump(mp)
              : {};
    const times = item ? sourceTimes(item) : { t0: null, t1: null, source: null };
    const srcFpsHit = fpsFromDump(dump);
    const srcFps = srcFpsHit ? srcFpsHit.fps : null;
    let t0 = times.t0;
    let t1 = times.t1;
    if ((t0 == null || t1 == null) && times.sourceStartFrame != null && srcFps) {
        const f = srcFps.num / srcFps.den;
        t0 = times.sourceStartFrame / f;
        t1 = times.sourceEndFrame / f;
    }

    const recordFrame = item ? timelineStartFrame(item) : null;
    const durationFrames = item ? finiteNumber(callValue(item, "GetDuration")) : null;
    const fusionCompCount = item ? finiteNumber(callValue(item, "GetFusionCompCount")) : null;
    const voiceIsolationAvailable = item ? isCallable(item, "GetVoiceIsolationState") : false;
    const voiceIsolation = voiceIsolationAvailable ? callValue(item, "GetVoiceIsolationState") : null;
    let trackVoiceIsolation = null;
    if (
        ctx.timeline &&
        isCallable(ctx.timeline, "GetVoiceIsolationState") &&
        row.trackType === "audio" &&
        row.trackIndex != null
    ) {
        trackVoiceIsolation = callValue(ctx.timeline, "GetVoiceIsolationState", row.trackIndex);
    }

    let audioMapping = item ? callValue(item, "GetSourceAudioChannelMapping") : null;
    if (audioMapping == null && mp && isCallable(mp, "GetAudioMapping")) {
        audioMapping = callValue(mp, "GetAudioMapping");
    }
    audioMapping = parseAudioMapping(audioMapping);

    const itemPropertySnapshot = item && isCallable(item, "GetProperty") ? callValue(item, "GetProperty") : {};
    const durInfo = fileDuration(dump, t1, item, srcFps);
    const handleS = ctx.handleS != null ? ctx.handleS : DEFAULT_HANDLE_S;
    let timeShifted = false;
    if (t0 != null && t1 != null && durInfo.fileDur != null) {
        const fpsF = srcFps && srcFps.num > 0 ? srcFps.num / srcFps.den : null;
        const leftFrames = item ? finiteNumber(callValue(item, "GetLeftOffset")) : null;
        const rel = fileRelativeTimes(t0, t1, durInfo.fileDur, {
            startTc: startTcFromDump(dump, fpsF),
            leftOffsetSeconds: leftFrames != null && fpsF ? leftFrames / fpsF : null,
            frameSeconds: fpsF ? 1 / fpsF : 1 / 30,
        });
        timeShifted = rel.shifted;
        t0 = rel.t0;
        t1 = rel.t1;
    }
    let handles = null;
    if (t0 != null && t1 != null && durInfo.fileDur != null) {
        handles = actualHandles(t0, t1, durInfo.fileDur, handleS);
    }

    const sampleRateRaw = dump["Sample Rate"];
    const sampleRate = parseSampleRate(sampleRateRaw);

    const clip = {
        name: row.chosenName,
        uniqueId: row.uniqueId,
        trackType: row.trackType,
        trackIndex: row.trackIndex,
        filePath: row.filePath,
        filePathKey: row.filePathKey,
        filePathProbed: Boolean(row.filePathProbed),
        preferredAudioSibling: row.preferredAudioSibling,
        suppressedDuplicate: row.suppressedDuplicate,
        duplicateOf: row.duplicateOf,
        groupSize: row.groupSize,
        t0,
        t1,
        sourceTimeOrigin: times.source,
        sourceTimeShifted: timeShifted,
        recordFrame,
        durationFrames,
        fusionCompCount,
        voiceIsolation,
        voiceIsolationAvailable,
        trackVoiceIsolation,
        audioMapping,
        propertySnapshot: itemPropertySnapshot && typeof itemPropertySnapshot === "object" ? itemPropertySnapshot : {},
        clipPropertyDump: dump,
        clipPropertyKeys: Object.keys(dump || {}),
        fileDur: durInfo.fileDur,
        fileDurSource: durInfo.source,
        fileDurAssumed: durInfo.assumed,
        sampleRate,
        srcFps: srcFps ? { num: srcFps.num, den: srcFps.den } : null,
        hLeftActual: handles ? handles.hLeftActual : null,
        hRightActual: handles ? handles.hRightActual : null,
        handleS,
    };

    if (row.suppressedDuplicate) {
        clip.rejected = false;
        clip.reasons = [];
        clip.warnings = [
            {
                code: "duplicate_path",
                message: "Same audio File Path as another selected item — one job per path.",
            },
        ];
        return clip;
    }

    const verdict = evaluateReject(clip, { outFps: ctx.outFps });
    clip.rejected = verdict.rejected;
    clip.reasons = verdict.reasons;
    clip.warnings = verdict.warnings;
    if (timeShifted) {
        clip.warnings.push({
            code: "source_tc_normalized",
            message: "Source start looked like reel/TOD timecode; mapped onto the file.",
        });
    }
    if (durInfo.assumed && t0 != null) {
        clip.warnings.push({
            code: "file_dur_unconfirmed",
            message: "Source file duration was not in the clip-property dump; right handle is unclamped.",
        });
    }
    if (sampleRateRaw == null) {
        clip.warnings.push({ code: "sample_rate_unconfirmed", message: UNCONFIRMED.sampleRate });
    }
    return clip;
}

function jsonSafe(value) {
    // Electron IPC cannot clone native Resolve objects.
    try {
        return JSON.parse(
            JSON.stringify(value, (_k, v) => {
                if (typeof v === "bigint") return Number(v);
                if (typeof v === "function") return undefined;
                return v;
            }),
        );
    } catch {
        return null;
    }
}

function publicClip(clip) {
    const {
        clipPropertyDump: _dump,
        propertySnapshot: _props,
        mediaPoolItem: _mp,
        item: _item,
        ...rest
    } = clip;
    return jsonSafe(rest) || rest;
}

function inspectSelection(resolve, options) {
    const opts = options || {};
    if (!resolve) {
        return { ok: false, error: "Resolve is not connected." };
    }

    const versionFields = call(resolve, "GetVersion");
    const version = parseVersionFields(
        versionFields && !versionFields._error ? versionFields : callValue(resolve, "GetVersion"),
    );
    const versionString = callValue(resolve, "GetVersionString");
    const product = callValue(resolve, "GetProductName");
    const strategy = selectionStrategy(version);
    if (strategy === "unsupported") {
        return {
            ok: false,
            error: "PerfectVoice requires DaVinci Resolve Studio 20.0 or later.",
            product,
            versionString,
        };
    }

    const pm = callValue(resolve, "GetProjectManager");
    const project = pm ? callValue(pm, "GetCurrentProject") : null;
    if (!project) {
        return { ok: false, error: "No current project.", product, versionString };
    }
    const timeline = callValue(project, "GetCurrentTimeline");
    if (!timeline) {
        return { ok: false, error: "No current timeline.", product, versionString };
    }

    const fpsInfo = liveTimelineFps(project);
    const selected = collectSelected(timeline);
    const rows = groupSelectedItems(selected.items);
    const handleS = opts.handleS != null ? opts.handleS : DEFAULT_HANDLE_S;
    const ctx = {
        timeline,
        outFps: fpsInfo.fps,
        handleS,
    };
    const clips = rows.map((row) => publicClip(inspectClip(row, ctx)));
    const jobClips = clips.filter((c) => !c.suppressedDuplicate);
    const accepted = jobClips.filter((c) => !c.rejected);

    const warnings = [...selected.warnings];
    if (selected.source === "playhead_plus_linked") {
        warnings.push("Used playhead + GetLinkedItems (20.x fallback or empty GetSelectedClips).");
    }
    if (!clips.length) {
        warnings.push("No clips selected. Select a clip on the Edit or Fairlight page.");
    }

    let currentTc = null;
    let startTc = null;
    try {
        currentTc = isCallable(timeline, "GetCurrentTimecode") ? callValue(timeline, "GetCurrentTimecode") : null;
        startTc = isCallable(timeline, "GetStartTimecode") ? callValue(timeline, "GetStartTimecode") : null;
    } catch {}

    const srInfo = liveProjectSampleRate(project);
    const payload = {
        ok: true,
        source: selected.source,
        selectionStrategy: strategy,
        product: product || null,
        versionString: versionString || null,
        timelineName: callValue(timeline, "GetName") || null,
        currentTimecode: currentTc,
        startTimecode: startTc,
        outFps: { num: fpsInfo.fps.num, den: fpsInfo.fps.den },
        outFpsSource: fpsInfo.source,
        projectSampleRate: srInfo.sampleRate,
        projectSampleRateSource: srInfo.source,
        handleS,
        clips,
        jobCount: jobClips.length,
        acceptedCount: accepted.length,
        rejectedCount: jobClips.length - accepted.length,
        warnings,
    };
    return jsonSafe(payload) || { ok: false, error: "Inspect result could not be serialized." };
}

module.exports = {
    inspectSelection,
    inspectClip,
    liveTimelineFps,
    liveProjectSampleRate,
    parseSampleRate,
    sourceTimes,
};
