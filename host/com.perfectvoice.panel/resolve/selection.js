"use strict";

/**
 * Timeline selection. Probe GetSelectedClips at runtime; 20.x falls back
 * to playhead + GetLinkedItems. Never walk every audio item on the timeline.
 */

const { dedupeLinkedGroup, itemKey } = require("./dedupe");
const { filePathFromDump, FILE_PATH_KEY } = require("./reject");

function isCallable(obj, name) {
    return !!(obj && typeof obj[name] === "function");
}

function call(obj, name, ...args) {
    if (!isCallable(obj, name)) return undefined;
    try {
        return obj[name](...args);
    } catch (err) {
        return {
            _error: `${err && err.name ? err.name : "Error"}: ${err && err.message ? err.message : err}`,
        };
    }
}

function callValue(obj, name, ...args) {
    const v = call(obj, name, ...args);
    if (v && typeof v === "object" && Object.prototype.hasOwnProperty.call(v, "_error")) {
        return undefined;
    }
    return v;
}

function clipPropertyDump(mp) {
    if (!isCallable(mp, "GetClipProperty")) return {};
    const attempts = [() => mp.GetClipProperty(), () => mp.GetClipProperty(""), () => mp.GetClipProperty(null)];
    for (const fn of attempts) {
        try {
            const snap = fn();
            if (snap && typeof snap === "object" && !Array.isArray(snap) && !("_error" in snap)) {
                return snap;
            }
        } catch {
            // try next calling convention
        }
    }
    return {};
}

function mediaFilePath(mp) {
    if (!mp) return { path: null, key: null, probed: false, dump: {} };
    const dump = clipPropertyDump(mp);
    const keyed = callValue(mp, "GetClipProperty", FILE_PATH_KEY);
    if (typeof keyed === "string" && keyed.trim()) {
        return { path: keyed.trim(), key: FILE_PATH_KEY, probed: false, dump };
    }
    const fromDump = filePathFromDump(dump);
    return { ...fromDump, dump };
}

function trackTypeAndIndex(item) {
    const track = callValue(item, "GetTrackTypeAndIndex");
    if (Array.isArray(track) && track.length) {
        return { trackType: track[0], trackIndex: track.length > 1 ? track[1] : null };
    }
    return { trackType: null, trackIndex: null };
}

function linkedItems(item) {
    const raw = callValue(item, "GetLinkedItems");
    return Array.isArray(raw) ? raw : [];
}

function memberFromItem(item) {
    const { trackType, trackIndex } = trackTypeAndIndex(item);
    const mp = callValue(item, "GetMediaPoolItem");
    const file = mediaFilePath(mp && typeof mp === "object" && !mp._error ? mp : null);
    return {
        item,
        name: callValue(item, "GetName"),
        uniqueId: callValue(item, "GetUniqueId"),
        trackType,
        trackIndex,
        filePath: file.path,
        filePathKey: file.key,
        filePathProbed: file.probed,
        mediaPoolItem: mp && typeof mp === "object" && !mp._error ? mp : null,
        clipPropertyDump: file.dump,
    };
}

function collectSelected(timeline) {
    const warnings = [];
    if (!timeline) {
        return { items: [], source: "none", warnings: ["no current timeline"] };
    }

    if (isCallable(timeline, "GetSelectedClips")) {
        const raw = callValue(timeline, "GetSelectedClips");
        const selected = Array.isArray(raw) ? raw : [];
        if (selected.length) {
            return { items: selected, source: "GetSelectedClips", warnings };
        }
        warnings.push("GetSelectedClips returned empty");
    } else {
        warnings.push(
            "timeline.GetSelectedClips is not callable; using 20.x playhead + GetLinkedItems fallback",
        );
    }

    const current = callValue(timeline, "GetCurrentVideoItem");
    if (current) {
        const pool = [current, ...linkedItems(current)];
        return { items: pool, source: "playhead_plus_linked", warnings };
    }

    warnings.push("no selection and no current video item");
    return { items: [], source: "none", warnings };
}

function groupSelectedItems(items) {
    const list = Array.isArray(items) ? items : [];
    const parent = new Map();

    function find(k) {
        if (!parent.has(k)) parent.set(k, k);
        if (parent.get(k) !== k) parent.set(k, find(parent.get(k)));
        return parent.get(k);
    }
    function union(a, b) {
        const ra = find(a);
        const rb = find(b);
        if (ra !== rb) parent.set(ra, rb);
    }

    const byKey = new Map();
    for (const item of list) {
        const k = itemKey(item, callValue);
        byKey.set(k, item);
        find(k);
        for (const sib of linkedItems(item)) {
            const sk = itemKey(sib, callValue);
            byKey.set(sk, sib);
            union(k, sk);
        }
    }

    const buckets = new Map();
    for (const [k, item] of byKey) {
        const root = find(k);
        if (!buckets.has(root)) buckets.set(root, []);
        buckets.get(root).push(item);
    }

    const out = [];
    for (const groupItems of buckets.values()) {
        out.push(...dedupeLinkedGroup(groupItems.map(memberFromItem)));
    }
    return out;
}

function parseVersionFields(fields) {
    if (!fields) return null;
    const seq = Array.isArray(fields) ? fields : typeof fields === "object" ? Object.values(fields) : [];
    if (seq.length < 3) return null;
    const major = Number(seq[0]);
    const minor = Number(seq[1]);
    const patch = Number(seq[2]);
    if (![major, minor, patch].every(Number.isFinite)) return null;
    return { major, minor, patch };
}

function selectionStrategy(version) {
    if (!version) return "unknown";
    if (version.major > 21 || (version.major === 21 && (version.minor > 0 || version.patch >= 4))) {
        return "get_selected_clips_then_dedupe";
    }
    if (version.major >= 20) return "playhead_plus_linked";
    return "unsupported";
}

module.exports = {
    isCallable,
    call,
    callValue,
    clipPropertyDump,
    mediaFilePath,
    collectSelected,
    groupSelectedItems,
    memberFromItem,
    linkedItems,
    parseVersionFields,
    selectionStrategy,
};
