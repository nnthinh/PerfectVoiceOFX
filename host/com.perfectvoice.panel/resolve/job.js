"use strict";

/**
 * Panel → engine job request. source_in/out are content t0/t1 in samples;
 * the engine applies Appendix A once (do not pre-add H).
 */

const crypto = require("crypto");
const os = require("os");
const path = require("path");

const { asFps, DEFAULT_HANDLE_S, roundHalfUp } = require("./time");
const { detectMultichannel, parseAudioMapping } = require("./reject");

const DEFAULT_SR = 48000;
const DEFAULT_WET = 0.85;
const DEFAULT_SEGMENT = 7.8;
const DEFAULT_OVERLAP = 0.25;
const DEFAULT_SHIFTS = 1;
const OUTPUT_FOLDER = "PerfectVoice";

function finiteNumber(value) {
    const n = typeof value === "number" ? value : Number(value);
    return Number.isFinite(n) ? n : null;
}

function defaultOutputDir() {
    if (process.platform === "win32") {
        const root =
            process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
        return path.join(root, "PerfectVoice", "output", crypto.randomUUID());
    }
    return path.join(
        os.homedir(),
        "Library/Application Support/PerfectVoice/output",
        crypto.randomUUID(),
    );
}

/** True when writing here would put PerfectVoice next to a volume or drive root. */
function isShallowBase(dir) {
    const resolved = path.resolve(String(dir || ""));
    const root = path.parse(resolved).root;
    if (!resolved || resolved === root) return true;
    // /Volumes, /Users, /mnt, C:\Media — one level under the filesystem root.
    return path.dirname(resolved) === root;
}

/** Sibling of the folder that contains the clip: …/SHOW/Source/a.mp4 → …/SHOW/PerfectVoice */
function sidecarDirForSource(sourcePath) {
    const file = path.resolve(String(sourcePath));
    const clipDir = path.dirname(file);
    const parent = path.dirname(clipDir);
    const base = isShallowBase(parent) ? clipDir : parent;
    return path.join(base, OUTPUT_FOLDER);
}

function outputDirForSources(sourcePaths) {
    const dirs = [];
    const seen = new Set();
    for (const raw of sourcePaths) {
        if (!raw) continue;
        const dir = sidecarDirForSource(raw);
        if (seen.has(dir)) continue;
        seen.add(dir);
        dirs.push(dir);
    }
    return dirs;
}

function uniqueRoots(paths) {
    const roots = [];
    const seen = new Set();
    for (const raw of paths) {
        if (!raw) continue;
        const resolved = path.resolve(String(raw));
        if (seen.has(resolved)) continue;
        // Engine rejects filesystem roots ("/" / "C:\\").
        const parsed = path.parse(resolved);
        if (resolved === parsed.root) continue;
        seen.add(resolved);
        roots.push(resolved);
    }
    return roots;
}

function allowedRootsFor(sourcePaths, outputDir) {
    const dirs = sourcePaths.map((p) => path.dirname(path.resolve(p)));
    dirs.push(path.resolve(outputDir));
    dirs.push(path.dirname(path.resolve(outputDir)));
    const roots = uniqueRoots(dirs);
    if (!roots.length) {
        throw new Error("could not build allowed_roots (paths resolved to filesystem root)");
    }
    return roots;
}

function fpsPair(fps, fallback) {
    const src = fps != null ? fps : fallback;
    const frac = asFps(src || { num: 24, den: 1 });
    return { num: frac.num, den: frac.den };
}

function sourceChannelsAndMap(clip) {
    const mapping = parseAudioMapping(clip && clip.audioMapping);
    const multi = detectMultichannel(mapping);
    if (multi && multi.overTwo) {
        throw new Error("source has more than 2 channels");
    }
    if (mapping) {
        const embedded = finiteNumber(mapping.embedded_audio_channels);
        if (embedded === 1) return { channels: 1, channelMap: [0] };
        if (embedded === 2) return { channels: 2, channelMap: [0, 1] };
    }
    return { channels: 2, channelMap: [0, 1] };
}

function contentSample(tSeconds, sampleRate) {
    // Content t0/t1 only — not t0-H. Engine is source of truth for handles.
    return roundHalfUp(Number(tSeconds) * Number(sampleRate));
}

function displayName(clip) {
    const name = clip && clip.name != null ? String(clip.name).trim() : "";
    if (name) return name;
    if (clip && clip.filePath) {
        const base = path.basename(clip.filePath);
        if (base) return base;
    }
    return "clip";
}

function jobableClips(inspect) {
    return ((inspect && inspect.clips) || []).filter(
        (c) => c && !c.suppressedDuplicate && !c.rejected,
    );
}

function clipToManifest(clip, inspect, options) {
    const opts = options || {};
    const t0 = finiteNumber(clip.t0);
    const t1 = finiteNumber(clip.t1);
    if (t0 == null || t1 == null || !(t1 > t0)) {
        throw new Error("missing source times");
    }
    const srcSr = finiteNumber(clip.sampleRate);
    if (srcSr == null || !(srcSr > 0)) {
        throw new Error("source sample rate is unknown");
    }
    const filePath = clip.filePath && String(clip.filePath).trim();
    if (!filePath) {
        throw new Error("no File Path");
    }
    const recordFrame = finiteNumber(clip.recordFrame);
    if (recordFrame == null) {
        throw new Error("missing recordFrame");
    }
    const fileDur = finiteNumber(clip.fileDur);
    if (fileDur == null || !(fileDur > 0)) {
        throw new Error("missing file duration");
    }
    const handleS =
        opts.handleS != null
            ? Number(opts.handleS)
            : clip.handleS != null
              ? Number(clip.handleS)
              : inspect && inspect.handleS != null
                ? Number(inspect.handleS)
                : DEFAULT_HANDLE_S;
    if (!Number.isFinite(handleS) || handleS < 0) {
        throw new Error("invalid handles_seconds");
    }

    const sourceIn = contentSample(t0, srcSr);
    const sourceOut = contentSample(t1, srcSr);
    if (!(sourceOut > sourceIn)) {
        throw new Error("empty content window");
    }

    const ch = sourceChannelsAndMap(clip);
    const outFps = fpsPair(inspect && inspect.outFps, { num: 24, den: 1 });
    const srcFps = fpsPair(clip.srcFps, outFps);
    const projSr =
        finiteNumber(opts.projectSampleRate) ||
        finiteNumber(inspect && inspect.projectSampleRate) ||
        srcSr ||
        DEFAULT_SR;
    const durationFrames = finiteNumber(clip.durationFrames);
    const timelineEnd =
        durationFrames != null ? recordFrame + durationFrames : recordFrame;

    return {
        schema: "perfectvoice.clip.v1",
        clip_id: crypto.randomUUID(),
        display_name: displayName(clip),
        source_path: filePath,
        source_in_sample: sourceIn,
        source_out_sample: sourceOut,
        source_sample_rate: Math.round(srcSr),
        source_channels: ch.channels,
        audio_stream_index: 0,
        channel_map: ch.channelMap,
        timeline_start_frame: Math.round(recordFrame),
        timeline_end_frame: Math.round(timelineEnd),
        timeline_fps: outFps,
        source_fps: srcFps,
        output_media_fps: outFps,
        project_sample_rate: Math.round(projSr),
        handles_seconds: handleS,
        file_duration_seconds: fileDur,
    };
}

function resolveModel(options) {
    const opts = options || {};
    if (opts.model === "htdemucs_ft" || opts.preset === "quality") return "htdemucs_ft";
    return "htdemucs";
}

function resolveEnhancer(options) {
    const opts = options || {};
    if (opts.enhancer === "deepfilternet3" || opts.dfn === true) return "deepfilternet3";
    return "none";
}

function validNumber(val, min, max, fallback) {
    const n = finiteNumber(val);
    if (n == null || n < min || n > max) return fallback;
    return n;
}

function validInteger(val, min, max, fallback) {
    const n = finiteNumber(val);
    if (n == null || n < min || n > max || !Number.isInteger(n)) return fallback;
    return n;
}

function buildParams(outputDir, roots, options) {
    const opts = options || {};
    const params = {
        schema: "perfectvoice.params.v1",
        model: resolveModel(opts),
        device: opts.device || "auto",
        segment: validNumber(opts.segment, 0.1, 7.8, DEFAULT_SEGMENT),
        overlap: validNumber(opts.overlap, 0, 0.99, DEFAULT_OVERLAP),
        shifts: validInteger(opts.shifts, 1, 16, DEFAULT_SHIFTS),
        vocals_only_bag: false,
        wet: validNumber(opts.wet, 0, 1, DEFAULT_WET),
        output_gain_db: opts.outputGainDb != null ? Number(opts.outputGainDb) : 0,
        mono: Boolean(opts.mono),
        sample_format: opts.sampleFormat || "pcm24",
        enhancer: resolveEnhancer(opts),
        resampler_id: "soxr_hq_v1",
        clip_policy: "no_demucs_rescale",
        output_dir: outputDir,
        allowed_roots: roots,
        use_cache: opts.useCache !== false,
    };
    if (opts.mode) params.mode = opts.mode;
    if (opts.speaker_id) params.speaker_id = opts.speaker_id;
    if (opts.ref_sample_t0 != null) params.ref_sample_t0 = Number(opts.ref_sample_t0);
    if (opts.ref_sample_t1 != null) params.ref_sample_t1 = Number(opts.ref_sample_t1);
    return params;
}

function buildCreateJobRequest(inspect, options) {
    const opts = options || {};
    const warnings = [];
    const manifests = [];
    const origins = [];
    for (const clip of jobableClips(inspect)) {
        try {
            const manifest = clipToManifest(clip, inspect, opts);
            manifests.push(manifest);
            origins.push(clip);
        } catch (err) {
            const label = displayName(clip);
            warnings.push(`${label}: ${err && err.message ? err.message : err}`);
        }
    }
    if (!manifests.length) {
        return {
            ok: false,
            error:
                "No accepted clips with a file path, source times, and sample rate.",
            warnings,
        };
    }
    const sources = manifests.map((c) => c.source_path);
    const sidecarDirs = outputDirForSources(sources);
    const outputDir = opts.outputDir || sidecarDirs[0] || defaultOutputDir();
    if (!opts.outputDir && sidecarDirs.length > 1) {
        warnings.push(
            `Clips live under ${sidecarDirs.length} source folders; writing WAVs to ${outputDir}`,
        );
    }
    const roots = allowedRootsFor(sources, outputDir);
    const params = buildParams(outputDir, roots, opts);
    return {
        ok: true,
        body: {
            clips: manifests,
            params,
            allowed_roots: roots,
            output_dir: outputDir,
        },
        origins,
        warnings,
    };
}

function placeParamsFromResult(inspectClip, jobClip, inspect) {
    if (!jobClip || !jobClip.output_path) {
        throw new Error("job result is missing output_path");
    }
    if (jobClip.handles_left_actual == null) {
        throw new Error("job result is missing handles_left_actual");
    }
    return {
        wavPath: jobClip.output_path,
        recordFrame: inspectClip.recordFrame,
        t0: inspectClip.t0,
        t1: inspectClip.t1,
        fileDur: inspectClip.fileDur,
        handleS: inspectClip.handleS != null ? inspectClip.handleS : DEFAULT_HANDLE_S,
        handles_left_actual: jobClip.handles_left_actual,
        handles_right_actual: jobClip.handles_right_actual,
        outFps: inspect && inspect.outFps,
    };
}

module.exports = {
    DEFAULT_SR,
    DEFAULT_WET,
    contentSample,
    clipToManifest,
    buildParams,
    buildCreateJobRequest,
    placeParamsFromResult,
    allowedRootsFor,
    defaultOutputDir,
    sidecarDirForSource,
    outputDirForSources,
    isShallowBase,
    OUTPUT_FOLDER,
    jobableClips,
    resolveModel,
    resolveEnhancer,
};
