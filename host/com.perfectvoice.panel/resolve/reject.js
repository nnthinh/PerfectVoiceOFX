"use strict";

/**
 * Reject matrix (block before job).
 *
 * Official / README-confirmed: "File Path", GetSourceStartTime/EndTime,
 * GetLinkedItems, GetVoiceIsolationState, GetSourceAudioChannelMapping,
 * GetFusionCompCount, GetStart/GetDuration.
 *
 * Speed % / reverse / Elastic Wave keys were not in the 00b live dump.
 * Reject those only when a probed snapshot actually contains a matching
 * key or a duration heuristic fires. Otherwise warn — do not fake-reject.
 */

const FILE_PATH_KEY = "File Path";

const COPY = {
    offline: "Clip is offline or Fairlight-only with no file — relink or bounce.",
    nested: "Un-nest / generators are not supported.",
    speed: "Speed/retime ≠ 100%. Reset speed or bounce (v1.1+).",
    reverse: "Reversed clip — rejected.",
    elasticWave: "Turn off Elastic Wave or bounce.",
    voiceIsolation:
        "Turn off clip FX (including Voice Isolation) or bounce. v1 reads the source file, not FX.",
    slip: "Unusual slip/offset — rejected to avoid lip-sync drift.",
    multichannel: "Fold down to stereo in Fairlight, then run again.",
    proxy: "Relink the original (proxies are not processed).",
};

const UNCONFIRMED = {
    speed:
        "Clip speed % property name is still unconfirmed (README documents RetimeProcess, not speed). " +
        "Rejected only when a probed key or duration heuristic detects retime.",
    reverse:
        "Reverse flag property name is still unconfirmed; not rejected.",
    elasticWave:
        "Elastic Wave property name is still unconfirmed; not rejected.",
    clipFx:
        "Fairlight clip-FX stack API is still unconfirmed; only Voice Isolation is checked.",
    sampleRate:
        "GetClipProperty sample-rate key is still unconfirmed (design claims \"Sample Rate\").",
};

function isEnabledFlag(value) {
    if (value === true || value === 1 || value === "1") return true;
    if (typeof value === "string" && value.toLowerCase() === "true") return true;
    return false;
}

function finiteNumber(value) {
    const n = typeof value === "number" ? value : Number(value);
    return Number.isFinite(n) ? n : null;
}

function looksLikeAbsPath(val) {
    if (typeof val !== "string") return false;
    const s = val.trim();
    if (s.length < 2) return false;
    if (s.startsWith("/")) return true;
    if (/^[A-Za-z]:[\\/]/.test(s)) return true;
    return false;
}

function filePathFromDump(dump) {
    if (!dump || typeof dump !== "object") {
        return { path: null, key: null, probed: false };
    }
    const official = dump[FILE_PATH_KEY];
    if (typeof official === "string" && official.trim()) {
        return { path: official.trim(), key: FILE_PATH_KEY, probed: false };
    }
    // Probe dump keys — do not invent names; only accept values that look like paths.
    for (const [key, val] of Object.entries(dump)) {
        if (key === FILE_PATH_KEY) continue;
        if (looksLikeAbsPath(val)) {
            return { path: String(val).trim(), key, probed: true };
        }
    }
    return { path: null, key: null, probed: false };
}

function parseAudioMapping(raw) {
    if (raw == null) return null;
    let obj = raw;
    if (typeof raw === "string") {
        const text = raw.trim();
        if (!text) return null;
        try {
            obj = JSON.parse(text);
        } catch {
            return null;
        }
    }
    if (!obj || typeof obj !== "object") return null;
    return obj;
}

function detectMultichannel(mapping) {
    if (!mapping) return null;
    const types = [];
    if (mapping.track_mapping && typeof mapping.track_mapping === "object") {
        for (const entry of Object.values(mapping.track_mapping)) {
            if (entry && entry.type != null) types.push(String(entry.type));
        }
    }
    for (const typ of types) {
        const lower = typ.toLowerCase();
        if (lower.includes("5.1") || lower.includes("7.1")) {
            return { overTwo: true, reason: typ };
        }
        if (lower.includes("adaptive")) {
            const ch = finiteNumber(mapping.embedded_audio_channels);
            if (ch != null && ch > 2) return { overTwo: true, reason: typ };
        }
    }
    const embedded = finiteNumber(mapping.embedded_audio_channels);
    if (embedded != null && embedded > 2) {
        return { overTwo: true, reason: `embedded_audio_channels=${embedded}` };
    }
    if (mapping.linked_audio && typeof mapping.linked_audio === "object") {
        for (const entry of Object.values(mapping.linked_audio)) {
            const ch = finiteNumber(entry && entry.channels);
            if (ch != null && ch > 2) {
                return { overTwo: true, reason: `linked_audio.channels=${ch}` };
            }
        }
    }
    return { overTwo: false };
}

function parseSpeedValue(val) {
    if (val == null) return null;
    if (typeof val === "number" && Number.isFinite(val)) return val;
    const text = String(val).trim();
    if (!text) return null;
    const pct = text.endsWith("%");
    const n = Number(pct ? text.slice(0, -1) : text);
    if (!Number.isFinite(n)) return null;
    return n;
}

function isUnitySpeed(value) {
    if (value == null) return false;
    if (Math.abs(value - 1) < 0.02) return true;
    if (Math.abs(value - 100) < 0.5) return true;
    return false;
}

function scanDump(dump, predicate) {
    if (!dump || typeof dump !== "object") return [];
    const hits = [];
    for (const [key, val] of Object.entries(dump)) {
        if (predicate(key, val)) hits.push({ key, value: val });
    }
    return hits;
}

function speedFromDump(dump) {
    const hits = scanDump(dump, (key) => {
        if (/retimeprocess/i.test(key)) return false;
        return /speed|play\s*rate|retime/i.test(key);
    });
    for (const hit of hits) {
        const n = parseSpeedValue(hit.value);
        if (n != null) return { key: hit.key, value: n };
    }
    return null;
}

function reverseFromDump(dump) {
    const hits = scanDump(dump, (key) => {
        if (/flip[xy]/i.test(key)) return false;
        return /reverse|backwards/i.test(key);
    });
    return hits[0] || null;
}

function elasticFromDump(dump) {
    const hits = scanDump(dump, (key) => /elastic|audiowarp|audio\s*warp/i.test(key));
    return hits[0] || null;
}

function typeFromDump(dump) {
    const hits = scanDump(dump, (key) => /^(clip\s*)?type$/i.test(key) || /^media\s*type$/i.test(key));
    return hits[0] || null;
}

function linkedAudioOffset(mapping) {
    if (!mapping || !mapping.linked_audio || typeof mapping.linked_audio !== "object") {
        return null;
    }
    let maxAbs = 0;
    let any = false;
    for (const entry of Object.values(mapping.linked_audio)) {
        const off = finiteNumber(entry && entry.offset);
        if (off == null) continue;
        any = true;
        if (Math.abs(off) > Math.abs(maxAbs)) maxAbs = off;
    }
    return any ? maxAbs : null;
}

function speedHeuristic(clip, outFps) {
    const t0 = finiteNumber(clip.t0);
    const t1 = finiteNumber(clip.t1);
    const dur = finiteNumber(clip.durationFrames);
    if (t0 == null || t1 == null || dur == null || t1 <= t0) return null;
    if (!outFps || !(outFps.num > 0) || !(outFps.den > 0)) return null;
    const expected = (t1 - t0) * (outFps.num / outFps.den);
    if (!(expected > 0)) return null;
    const ratio = dur / expected;
    const notUnity = Math.abs(ratio - 1) > 0.03 && Math.abs(dur - expected) > 1.5;
    return { ratio, expected, durationFrames: dur, notUnity };
}

function evaluateReject(clip, ctx) {
    const reasons = [];
    const warnings = [];
    const combinedDump = {
        ...(clip.clipPropertyDump || {}),
        ...(clip.propertySnapshot || {}),
        ...(clip.itemPropertySnapshot || {}),
    };

    if (!clip.filePath) {
        reasons.push({ code: "offline", message: COPY.offline });
    }

    if (finiteNumber(clip.fusionCompCount) > 0) {
        reasons.push({ code: "fusion", message: COPY.nested });
    } else {
        const typ = typeFromDump(combinedDump);
        if (typ && /compound|nested|generator|fusion|solid|title|adjustment/i.test(String(typ.value))) {
            reasons.push({ code: "nested", message: COPY.nested, detail: `${typ.key}=${typ.value}` });
        }
    }

    const voice = clip.voiceIsolation;
    if (voice && isEnabledFlag(voice.isEnabled != null ? voice.isEnabled : voice.enabled)) {
        reasons.push({ code: "voice_isolation", message: COPY.voiceIsolation });
    }
    const trackVoice = clip.trackVoiceIsolation;
    if (
        trackVoice &&
        isEnabledFlag(trackVoice.isEnabled != null ? trackVoice.isEnabled : trackVoice.enabled)
    ) {
        if (!reasons.some((r) => r.code === "voice_isolation")) {
            reasons.push({ code: "voice_isolation", message: COPY.voiceIsolation });
        }
    }

    const mapping = parseAudioMapping(clip.audioMapping);
    const ch = detectMultichannel(mapping);
    if (ch && ch.overTwo) {
        reasons.push({ code: "multichannel", message: COPY.multichannel, detail: ch.reason });
    }

    const outFps = ctx && ctx.outFps;
    const speedHit = speedFromDump(combinedDump);
    const heuristic = speedHeuristic(clip, outFps);
    if (speedHit && !isUnitySpeed(speedHit.value)) {
        reasons.push({
            code: "speed",
            message: COPY.speed,
            detail: `${speedHit.key}=${speedHit.value}`,
        });
    } else if (heuristic && heuristic.notUnity) {
        reasons.push({
            code: "speed",
            message: COPY.speed,
            detail: `duration/source ratio=${heuristic.ratio.toFixed(4)}`,
        });
    } else if (!speedHit && !heuristic) {
        warnings.push({ code: "speed_unconfirmed", message: UNCONFIRMED.speed });
    }

    const rev = reverseFromDump(combinedDump);
    if (rev && isEnabledFlag(rev.value)) {
        reasons.push({ code: "reverse", message: COPY.reverse, detail: rev.key });
    } else if (rev && typeof rev.value === "number" && rev.value < 0) {
        reasons.push({ code: "reverse", message: COPY.reverse, detail: rev.key });
    } else if (!rev) {
        warnings.push({ code: "reverse_unconfirmed", message: UNCONFIRMED.reverse });
    }

    const ew = elasticFromDump(combinedDump);
    if (ew && isEnabledFlag(ew.value)) {
        reasons.push({ code: "elastic_wave", message: COPY.elasticWave, detail: ew.key });
    } else if (!ew) {
        warnings.push({ code: "elastic_wave_unconfirmed", message: UNCONFIRMED.elasticWave });
    }

    const speedRejected = reasons.some((r) => r.code === "speed");
    const offset = linkedAudioOffset(mapping);
    if (offset != null && Math.abs(offset) > 1) {
        reasons.push({
            code: "slip",
            message: COPY.slip,
            detail: `linked_audio.offset=${offset}`,
        });
    } else if (!speedRejected && heuristic && heuristic.notUnity === false) {
        // duration matches 1x — slip would show up as mapping offset, already checked
    }

    if (!voice && clip.voiceIsolationAvailable === false) {
        warnings.push({ code: "clip_fx_unconfirmed", message: UNCONFIRMED.clipFx });
    } else if (!reasons.some((r) => r.code === "voice_isolation")) {
        warnings.push({ code: "clip_fx_unconfirmed", message: UNCONFIRMED.clipFx });
    }

    return {
        rejected: reasons.length > 0,
        reasons,
        warnings,
    };
}

module.exports = {
    FILE_PATH_KEY,
    COPY,
    UNCONFIRMED,
    evaluateReject,
    filePathFromDump,
    parseAudioMapping,
    detectMultichannel,
    speedHeuristic,
    looksLikeAbsPath,
};
