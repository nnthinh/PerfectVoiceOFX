"use strict";

/**
 * Dedupe linked A/V selection — one job per distinct audio File Path (§3.3).
 * Pure: operates on member dicts, no Resolve required.
 */

function field(member, camel, snake) {
    if (member[camel] !== undefined) return member[camel];
    if (snake && member[snake] !== undefined) return member[snake];
    return undefined;
}

function normalizePath(raw) {
    if (raw && typeof raw === "object" && "_error" in raw) return null;
    if (typeof raw !== "string") return raw == null ? null : String(raw);
    const trimmed = raw.trim();
    return trimmed === "" ? null : trimmed;
}

function dedupeLinkedGroup(members) {
    const list = Array.isArray(members) ? members : [];
    const audio = list.filter((m) => field(m, "trackType", "track_type") === "audio");
    const video = list.filter((m) => field(m, "trackType", "track_type") === "video");
    const other = list.filter((m) => {
        const kind = field(m, "trackType", "track_type");
        return kind !== "audio" && kind !== "video";
    });
    const groupSize = list.length;

    function row(member, preferredAudio, suppressed, duplicateOf) {
        const trackType = field(member, "trackType", "track_type");
        const trackIndex = field(member, "trackIndex", "track_index");
        return {
            chosenName: field(member, "name") || field(member, "chosenName", "chosen_name"),
            chosenTrack: [trackType, trackIndex],
            filePath: normalizePath(field(member, "filePath", "file_path")),
            preferredAudioSibling: preferredAudio,
            groupSize,
            suppressedDuplicate: suppressed,
            duplicateOf,
            uniqueId: field(member, "uniqueId", "unique_id"),
            trackType,
            trackIndex,
            item: member.item,
            // Needed by inspect/job: dump holds Sample Rate / Duration.
            mediaPoolItem: member.mediaPoolItem,
            clipPropertyDump: member.clipPropertyDump,
            filePathKey: member.filePathKey,
            filePathProbed: member.filePathProbed,
        };
    }

    if (audio.length) {
        const seenPaths = Object.create(null);
        const rows = [];
        for (const member of audio) {
            const path = normalizePath(field(member, "filePath", "file_path"));
            const pathKey = typeof path === "string" ? path : "";
            const firstId = pathKey ? seenPaths[pathKey] : undefined;
            const suppressed = Boolean(pathKey && firstId !== undefined);
            rows.push(
                row(
                    member,
                    true,
                    suppressed,
                    suppressed ? firstId : null,
                ),
            );
            if (pathKey && !suppressed) {
                seenPaths[pathKey] =
                    field(member, "uniqueId", "unique_id") || field(member, "name");
            }
        }
        return rows;
    }

    const chosen = (video[0] || other[0] || list[0] || {});
    return [row(chosen, false, false, null)];
}

const identityKeys = new WeakMap();
let nextIdentity = 1;

function objectIdentityKey(item) {
    if (item && (typeof item === "object" || typeof item === "function")) {
        let key = identityKeys.get(item);
        if (!key) {
            key = `obj:${nextIdentity}`;
            nextIdentity += 1;
            identityKeys.set(item, key);
        }
        return key;
    }
    return String(item);
}

function itemKey(item, callFn) {
    if (item && item.uniqueId) return String(item.uniqueId);
    if (item && item.unique_id) return String(item.unique_id);
    if (typeof callFn === "function") {
        const uid = callFn(item, "GetUniqueId");
        if (typeof uid === "string" && uid) return uid;
        // Do not fall back to GetName — linked A/V often share a name.
    }
    return objectIdentityKey(item);
}

function groupAndDedupe(items, memberOf) {
    const seen = new Set();
    const out = [];
    const list = Array.isArray(items) ? items : [];
    for (const item of list) {
        const built = typeof memberOf === "function" ? memberOf(item) : null;
        const members = built && Array.isArray(built.members) ? built.members : [item];
        const keys = members
            .map((m) => (m && (m.uniqueId || m.unique_id)) || itemKey(m && m.item ? m.item : m))
            .sort();
        const groupId = keys.join("\0");
        if (seen.has(groupId)) continue;
        seen.add(groupId);
        const rows = dedupeLinkedGroup(members);
        out.push(...rows);
    }
    return out;
}

module.exports = {
    dedupeLinkedGroup,
    groupAndDedupe,
    itemKey,
    normalizePath,
};
