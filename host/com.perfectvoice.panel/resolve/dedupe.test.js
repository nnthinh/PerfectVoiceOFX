"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { dedupeLinkedGroup, groupAndDedupe } = require("./dedupe");
const { collectSelected, groupSelectedItems } = require("./selection");
const fs = require("fs");
const path = require("path");

function member(name, trackType, filePath, uid, index) {
    return {
        name,
        uniqueId: uid || name,
        unique_id: uid || name,
        trackType,
        track_type: trackType,
        trackIndex: index == null ? 1 : index,
        track_index: index == null ? 1 : index,
        filePath,
        file_path: filePath,
    };
}

describe("dedupeLinkedGroup", () => {
    it("prefers audio over linked video", () => {
        const rows = dedupeLinkedGroup([
            member("A001", "video", "/media/A001.mov", "v", 1),
            member("A001", "audio", "/media/A001.wav", "a", 1),
        ]);
        assert.equal(rows.length, 1);
        assert.equal(rows[0].filePath, "/media/A001.wav");
        assert.equal(rows[0].preferredAudioSibling, true);
        assert.equal(rows[0].suppressedDuplicate, false);
    });

    it("keeps two distinct audio paths as two jobs", () => {
        const rows = dedupeLinkedGroup([
            member("cam", "video", "/media/cam.mov"),
            member("boom", "audio", "/media/boom.wav", "boom", 1),
            member("mix", "audio", "/media/mix.wav", "mix", 2),
        ]);
        const paths = rows.filter((r) => !r.suppressedDuplicate).map((r) => r.filePath);
        assert.deepEqual(paths, ["/media/boom.wav", "/media/mix.wav"]);
        assert.ok(rows.every((r) => r.preferredAudioSibling));
    });

    it("flags the same audio path as suppressed_duplicate", () => {
        const rows = dedupeLinkedGroup([
            member("A1", "audio", "/media/same.wav", "id-a", 1),
            member("A2", "audio", "/media/same.wav", "id-b", 2),
        ]);
        assert.equal(rows.length, 2);
        assert.equal(rows[0].suppressedDuplicate, false);
        assert.equal(rows[1].suppressedDuplicate, true);
        assert.equal(rows[1].filePath, "/media/same.wav");
        assert.equal(rows[1].duplicateOf, "id-a");
    });

    it("uses video only when the group has no audio", () => {
        const rows = dedupeLinkedGroup([member("V", "video", "/media/v.mov")]);
        assert.equal(rows.length, 1);
        assert.equal(rows[0].filePath, "/media/v.mov");
        assert.equal(rows[0].preferredAudioSibling, false);
        assert.equal(rows[0].suppressedDuplicate, false);
    });
});

describe("groupAndDedupe", () => {
    it("does not merge two unlinked groups", () => {
        const rows = groupAndDedupe(
            ["g1", "g2"],
            (id) => ({
                members:
                    id === "g1"
                        ? [member("A", "audio", "/a.wav", "a")]
                        : [member("B", "audio", "/b.wav", "b")],
            }),
        );
        assert.equal(rows.length, 2);
        assert.deepEqual(
            rows.map((r) => r.filePath),
            ["/a.wav", "/b.wav"],
        );
    });
});

function fakeItem(spec) {
    const item = {
        GetName: () => spec.name,
        GetUniqueId: () => spec.uid || spec.name,
        GetTrackTypeAndIndex: () => [spec.trackType, spec.trackIndex == null ? 1 : spec.trackIndex],
        GetLinkedItems: () => spec.linked || [],
        GetMediaPoolItem: () => spec.mp || null,
        GetStart: () => spec.start,
        GetDuration: () => spec.duration,
        GetSourceStartTime: () => spec.t0,
        GetSourceEndTime: () => spec.t1,
        GetFusionCompCount: () => spec.fusion || 0,
        GetVoiceIsolationState: () => spec.voice,
        GetSourceAudioChannelMapping: () => spec.mapping,
        GetProperty: () => spec.props || {},
    };
    return item;
}

function fakeMp(filePath, extra) {
    const dump = { "File Path": filePath, ...(extra || {}) };
    return {
        GetClipProperty: (key) => {
            if (key == null || key === "") return dump;
            return dump[key];
        },
    };
}

describe("collectSelected", () => {
    it("uses GetSelectedClips when it returns items", () => {
        const a = fakeItem({
            name: "A",
            trackType: "audio",
            t0: 0.2,
            t1: 1.2,
            mp: fakeMp("/a.wav"),
        });
        const timeline = {
            GetSelectedClips: () => [a],
            GetItemListInTrack() {
                throw new Error("must not iterate the timeline");
            },
        };
        const got = collectSelected(timeline);
        assert.equal(got.source, "GetSelectedClips");
        assert.equal(got.items.length, 1);
    });

    it("falls back to playhead + linked when GetSelectedClips is missing", () => {
        const audio = fakeItem({ name: "aud", trackType: "audio", mp: fakeMp("/a.wav") });
        const video = fakeItem({
            name: "vid",
            trackType: "video",
            linked: [audio],
            mp: fakeMp("/v.mov"),
        });
        const timeline = {
            GetCurrentVideoItem: () => video,
            GetItemListInTrack() {
                throw new Error("must not iterate the timeline");
            },
        };
        const got = collectSelected(timeline);
        assert.equal(got.source, "playhead_plus_linked");
        assert.equal(got.items.length, 2);
        const rows = groupSelectedItems(got.items);
        assert.equal(rows.filter((r) => !r.suppressedDuplicate).length, 1);
        assert.equal(rows[0].filePath, "/a.wav");
    });

    it("never calls GetItemListInTrack", () => {
        const src = fs.readFileSync(path.join(__dirname, "selection.js"), "utf8");
        assert.equal(src.includes("GetItemListInTrack"), false);
        assert.equal(src.includes("GetItemList("), false);
    });
});
