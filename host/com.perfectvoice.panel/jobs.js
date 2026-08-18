"use strict";

/**
 * Inspect → POST /v1/jobs → SSE → place with handles_*_actual.
 * Bearer stays in engine.js; this module never returns it.
 */

const fs = require("fs");
const {
    createJob,
    getJob,
    cancelJob,
    streamJobEvents,
    isAlive,
    getPublicStatus,
} = require("./engine");
const { inspectSelection } = require("./resolve/inspect");
const { muteOriginalClips, placeIsolated } = require("./resolve/place");
const { buildCreateJobRequest, placeParamsFromResult } = require("./resolve/job");

const ENGINE_UNHEALTHY = "Engine is not connected. Start the engine first.";
const JOB_RUNNING = "A job is already running.";
const NO_RESOLVE = "PerfectVoice requires DaVinci Resolve Studio and Workflow Integrations.";

let active = null;

function engineHealthy() {
    const s = getPublicStatus();
    if (!isAlive() || !s.connected) return false;
    const health = s.health;
    if (!health) return false;
    return health.ok === true || health.status === "ok";
}

function publicJob(job) {
    if (!job || typeof job !== "object") return job;
    const { token: _t, readyUrl: _u, Authorization: _a, ...rest } = job;
    return rest;
}

function emit(onEvent, payload) {
    if (typeof onEvent === "function") onEvent(payload);
}

async function waitForTerminal(jobId, onEvent) {
    const stream = streamJobEvents(jobId, (ev) => {
        emit(onEvent, { type: ev.event, jobId, data: ev.data || {} });
    });
    if (active && active.id === jobId) {
        active.stream = stream;
    }
    try {
        await stream.done;
    } catch (err) {
        if (active && active.cancelRequested) {
            return getJob(jobId).catch(() => ({
                id: jobId,
                status: "cancelled",
                error: "cancelled",
            }));
        }
        throw err;
    }
    return getJob(jobId);
}

function placeResults(resolve, inspect, origins, jobRecord) {
    const rows = [];
    const byId = new Map();
    const clips = (jobRecord && jobRecord.clips) || [];
    const manifests = origins || [];
    for (let i = 0; i < manifests.length; i += 1) {
        const origin = manifests[i].origin;
        const manifest = manifests[i].manifest;
        if (manifest && manifest.clip_id) byId.set(manifest.clip_id, origin);
    }
    for (const result of clips) {
        const origin = (result && result.clip_id && byId.get(result.clip_id)) || null;
        if (!origin) {
            rows.push({
                ok: false,
                error: `No inspect clip for job clip_id ${result && result.clip_id}`,
                clipId: result && result.clip_id,
            });
            continue;
        }
        try {
            const params = placeParamsFromResult(origin, result, inspect);
            const placed = placeIsolated(resolve, params);
            rows.push({
                ...placed,
                clipId: result.clip_id,
                clipName: origin.name || origin.display_name,
                outputPath: result.output_path,
                handlesLeftActual: result.handles_left_actual,
                cacheHit: result.cache_hit,
            });
        } catch (err) {
            rows.push({
                ok: false,
                error: err && err.message ? err.message : String(err),
                clipId: result.clip_id,
                clipName: origin.name,
            });
        }
    }
    return rows;
}

async function removeAccompaniment(resolve, options, onEvent) {
    const opts = options || {};
    if (active) {
        return { ok: false, error: JOB_RUNNING };
    }
    if (!engineHealthy()) {
        return { ok: false, error: ENGINE_UNHEALTHY, ...getPublicStatus() };
    }
    if (!resolve) {
        return { ok: false, error: NO_RESOLVE };
    }

    const inspect = inspectSelection(resolve, { handleS: opts.handleS });
    if (!inspect.ok) return inspect;

    const built = buildCreateJobRequest(inspect, opts);
    if (!built.ok) {
        return { ok: false, error: built.error, warnings: built.warnings, inspect };
    }

    try {
        fs.mkdirSync(built.body.output_dir, { recursive: true, mode: 0o700 });
    } catch (err) {
        return {
            ok: false,
            error: `Could not create output dir: ${err && err.message ? err.message : err}`,
            inspect,
        };
    }

    let accepted;
    try {
        accepted = await createJob(built.body);
    } catch (err) {
        return {
            ok: false,
            error: err && err.message ? err.message : String(err),
            inspect,
            warnings: built.warnings,
        };
    }

    const origins = built.body.clips.map((manifest, i) => ({
        manifest,
        origin: built.origins[i],
    }));

    active = {
        id: accepted.id,
        cancelRequested: false,
        stream: null,
    };
    emit(onEvent, { type: "queued", jobId: accepted.id, data: { id: accepted.id } });

    let record;
    try {
        record = await waitForTerminal(accepted.id, onEvent);
    } catch (err) {
        active = null;
        return {
            ok: false,
            error: err && err.message ? err.message : String(err),
            inspect,
            jobId: accepted.id,
            warnings: built.warnings,
        };
    }

    const status = record && record.status;
    if (status !== "done") {
        const cancelled = status === "cancelled" || (record && record.error === "cancelled");
        active = null;
        return {
            ok: false,
            cancelled,
            error:
                (record && record.error) ||
                (cancelled ? "Job cancelled." : "Job did not finish."),
            inspect,
            job: publicJob(record),
            jobId: accepted.id,
            warnings: built.warnings,
        };
    }

    const placed = placeResults(resolve, inspect, origins, record);
    const placeFailed = placed.filter((p) => !p.ok);
    let mute = null;
    if (opts.muteOriginal) {
        mute = muteOriginalClips(
            resolve,
            built.origins.map((c) => ({
                uniqueId: c.uniqueId,
                filePath: c.filePath,
                recordFrame: c.recordFrame,
                name: c.name,
            })),
        );
    }

    active = null;
    const warnings = [
        ...(built.warnings || []),
        ...(inspect.warnings || []),
        ...(mute && mute.warnings ? mute.warnings : []),
        ...placeFailed.map((p) => p.error).filter(Boolean),
    ];
    return {
        ok: placeFailed.length === 0,
        inspect,
        job: publicJob(record),
        jobId: accepted.id,
        placed,
        mute,
        warnings,
        error: placeFailed.length ? placeFailed[0].error : "",
    };
}

async function cancelActiveJob() {
    if (!active) {
        return { ok: false, error: "No job is running." };
    }
    active.cancelRequested = true;
    try {
        const body = await cancelJob(active.id);
        if (active.stream && typeof active.stream.abort === "function") {
            active.stream.abort();
        }
        return { ok: true, jobId: active.id, ...body };
    } catch (err) {
        if (active.stream && typeof active.stream.abort === "function") {
            active.stream.abort();
        }
        return {
            ok: false,
            jobId: active.id,
            error: err && err.message ? err.message : String(err),
        };
    }
}

function activeJobId() {
    return active ? active.id : null;
}

module.exports = {
    removeAccompaniment,
    cancelActiveJob,
    engineHealthy,
    activeJobId,
    ENGINE_UNHEALTHY,
};
