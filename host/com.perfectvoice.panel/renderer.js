window.addEventListener("DOMContentLoaded", async () => {
    const statusEl = document.getElementById("status");
    const pathEl = document.getElementById("enginePath");
    const healthEl = document.getElementById("health");
    const errorEl = document.getElementById("error");
    const resolveEl = document.getElementById("resolveNote");
    const startBtn = document.getElementById("startBtn");
    const inspectBtn = document.getElementById("inspectBtn");
    const placeBtn = document.getElementById("placeBtn");
    const removeBtn = document.getElementById("removeBtn");
    const cancelBtn = document.getElementById("cancelBtn");
    const downloadBtn = document.getElementById("downloadBtn");
    const dfnCheck = document.getElementById("dfnCheck");
    const muteCheck = document.getElementById("muteCheck");
    const cacheCheck = document.getElementById("cacheCheck");
    const modelSelect = document.getElementById("modelSelect");
    const jobProgress = document.getElementById("jobProgress");
    const jobError = document.getElementById("jobError");
    const clipList = document.getElementById("clipList");
    const inspectMeta = document.getElementById("inspectMeta");
    const inspectError = document.getElementById("inspectError");

    let lastStatus = null;
    let lastInspect = null;
    let jobRunning = false;
    let cancelTimer = null;

    function paint(s) {
        const connected = !!(s && s.connected);
        statusEl.textContent = connected ? "Engine connected" : "Engine not connected";
        statusEl.className = connected ? "ok" : "off";
        pathEl.textContent = (s && s.enginePath) || "not found";
        if (s && s.health) {
            healthEl.textContent = JSON.stringify(s.health, null, 2);
        } else if (!healthEl.textContent) {
            healthEl.textContent = "—";
        }
        if (s && s.resolveReady) {
            resolveEl.textContent = "";
        } else if (s && s.resolveError) {
            resolveEl.textContent = s.resolveError;
        }
        errorEl.textContent = (s && s.error) || "";
        lastStatus = s;
        syncRunButtons();
    }

    function engineHealthy(s) {
        if (!s || !s.connected || !s.health) return false;
        return s.health.ok === true || s.health.status === "ok";
    }

    function syncRunButtons() {
        const accepted = !!(lastInspect && lastInspect.ok && lastInspect.acceptedCount > 0);
        const healthy = engineHealthy(lastStatus);
        removeBtn.disabled = jobRunning || !healthy || !accepted;
        if (!jobRunning) {
            cancelBtn.disabled = true;
        }
        startBtn.disabled = jobRunning;
        inspectBtn.disabled = jobRunning;
        placeBtn.disabled = jobRunning;
        downloadBtn.disabled = jobRunning || !healthy;
        if (jobRunning) return;
        const current = jobProgress.textContent || "";
        const isHint =
            !current ||
            current === "—" ||
            /inspect a selection|start the engine|ready\. click remove/i.test(current);
        if (!isHint) return;
        if (!healthy) {
            jobProgress.textContent = accepted
                ? "Start the engine to enable Remove musical accompaniment."
                : "Inspect a selection and start the engine to enable Remove.";
        } else if (!accepted) {
            jobProgress.textContent = "Inspect a selection with at least one accepted clip.";
        } else {
            jobProgress.textContent = "Ready. Click Remove musical accompaniment.";
        }
    }

    function fmtNum(n, digits) {
        if (n == null || !Number.isFinite(Number(n))) return "—";
        return Number(n).toFixed(digits);
    }

    function paintInspect(result) {
        inspectError.textContent = "";
        clipList.replaceChildren();
        lastInspect = result && result.ok ? result : null;
        syncRunButtons();
        if (!result || !result.ok) {
            inspectMeta.textContent = "";
            inspectError.textContent = (result && result.error) || "Inspect failed.";
            return;
        }
        const fps = result.outFps
            ? `${result.outFps.num}/${result.outFps.den}`
            : "—";
        inspectMeta.textContent =
            `Source: ${result.source || "—"}  ·  ${result.jobCount || 0} job(s), ` +
            `${result.acceptedCount || 0} accepted, ${result.rejectedCount || 0} rejected  ·  fps ${fps}` +
            (result.warnings && result.warnings.length ? `\n${result.warnings.join("\n")}` : "");

        for (const clip of result.clips || []) {
            const li = document.createElement("li");
            const title = document.createElement("div");
            title.className = "name";
            const track =
                clip.trackType != null
                    ? `${clip.trackType} ${clip.trackIndex != null ? clip.trackIndex : ""}`.trim()
                    : "";
            title.textContent = `${clip.name || "(unnamed)"}  ${track}`;
            li.appendChild(title);

            const pathLine = document.createElement("div");
            pathLine.className = "path";
            pathLine.textContent = clip.filePath || "(no File Path)";
            li.appendChild(pathLine);

            const meta = document.createElement("div");
            meta.className = "meta";
            meta.textContent =
                `t0=${fmtNum(clip.t0, 3)}  t1=${fmtNum(clip.t1, 3)}  ` +
                `recordFrame=${clip.recordFrame != null ? clip.recordFrame : "—"}  ` +
                `H_left=${fmtNum(clip.hLeftActual, 3)}`;
            li.appendChild(meta);

            if (clip.suppressedDuplicate) {
                const dup = document.createElement("div");
                dup.className = "dup";
                dup.textContent = "Duplicate (same audio path — skipped)";
                li.appendChild(dup);
            } else if (clip.rejected) {
                for (const reason of clip.reasons || []) {
                    const line = document.createElement("div");
                    line.className = "rej";
                    line.textContent = `Rejected: ${reason.message}`;
                    li.appendChild(line);
                }
            } else {
                const ok = document.createElement("div");
                ok.className = "okc";
                ok.textContent = "Accepted";
                li.appendChild(ok);
            }

            for (const warn of clip.warnings || []) {
                const line = document.createElement("div");
                line.className = "warn";
                line.textContent = `Warning: ${warn.message}`;
                li.appendChild(line);
            }

            clipList.appendChild(li);
        }
    }

    try {
        paint(await window.perfectvoice.status());
    } catch (err) {
        errorEl.textContent = String(err && err.message ? err.message : err);
    }

    startBtn.addEventListener("click", async () => {
        startBtn.disabled = true;
        errorEl.textContent = "";
        try {
            paint(await window.perfectvoice.startEngine());
        } catch (err) {
            errorEl.textContent = String(err && err.message ? err.message : err);
        } finally {
            syncRunButtons();
        }
    });

    inspectBtn.addEventListener("click", async () => {
        inspectBtn.disabled = true;
        inspectError.textContent = "";
        try {
            paintInspect(await window.perfectvoice.inspect());
        } catch (err) {
            inspectError.textContent = String(err && err.message ? err.message : err);
        } finally {
            syncRunButtons();
        }
    });

    placeBtn.addEventListener("click", async () => {
        placeBtn.disabled = true;
        inspectError.textContent = "";
        try {
            const result = await window.perfectvoice.placeTestWav();
            if (!result || !result.ok) {
                inspectError.textContent = (result && result.error) || "Place test WAV failed.";
                if (result && result.inspect) paintInspect(result.inspect);
                return;
            }
            if (result.inspectSource || result.clipName) {
                inspectMeta.textContent =
                    `Placed test WAV for “${result.clipName || "clip"}”  ·  ` +
                    `startFrame=${result.clipInfo && result.clipInfo.startFrame}  ` +
                    `endFrame=${result.clipInfo && result.clipInfo.endFrame}  ` +
                    `recordFrame=${result.recordFrame}  ·  track ${result.trackName} #${result.trackIndex}` +
                    (result.generated ? "  ·  generated silence WAV" : "");
            }
            if (result.warnings && result.warnings.length) {
                inspectError.textContent = result.warnings.join("\n");
            }
        } catch (err) {
            inspectError.textContent = String(err && err.message ? err.message : err);
        } finally {
            syncRunButtons();
        }
    });

    function selectedModel() {
        return (modelSelect && modelSelect.value) || "htdemucs";
    }

    function formatJobEvent(ev) {
        if (!ev) return "";
        const data = ev.data || {};
        if (ev.type === "queued") {
            return `Queued job ${data.id || ev.jobId || ""}`.trim();
        }
        if (ev.type === "progress") {
            const len = Number(data.audio_length) || 0;
            const off = Number(data.segment_offset) || 0;
            const pct = len > 0 ? Math.min(100, Math.round((100 * off) / len)) : 0;
            const clip = data.clip_id ? `clip ${data.clip_id.slice(0, 8)}…` : "clip";
            return `Calibrating… ${clip}  ${pct}%  (${off}/${len})`;
        }
        if (ev.type === "done") return `Job done (${data.id || ev.jobId || ""})`.trim();
        if (ev.type === "error") return data.message || "Job error";
        return ev.type || "";
    }

    if (window.perfectvoice.onJobEvent) {
        window.perfectvoice.onJobEvent((ev) => {
            const line = formatJobEvent(ev);
            if (line) jobProgress.textContent = line;
        });
    }

    function setJobRunning(running) {
        jobRunning = running;
        if (cancelTimer) {
            clearTimeout(cancelTimer);
            cancelTimer = null;
        }
        if (running) {
            cancelBtn.disabled = true;
            cancelTimer = setTimeout(() => {
                if (jobRunning) cancelBtn.disabled = false;
            }, 1000);
        }
        syncRunButtons();
    }

    removeBtn.addEventListener("click", async () => {
        jobError.textContent = "";
        setJobRunning(true);
        jobProgress.textContent = "Submitting job…";
        try {
            const result = await window.perfectvoice.removeAccompaniment({
                model: selectedModel(),
                dfn: !!(dfnCheck && dfnCheck.checked),
                muteOriginal: !!(muteCheck && muteCheck.checked),
                useCache: !(cacheCheck && !cacheCheck.checked),
            });
            if (result && result.inspect) paintInspect(result.inspect);
            if (!result || !result.ok) {
                jobError.textContent = (result && result.error) || "Remove musical accompaniment failed.";
                if (result && result.cancelled) {
                    jobProgress.textContent = "Job cancelled. Timeline was not changed.";
                } else if (result && result.error && /Model not installed/i.test(result.error)) {
                    jobProgress.textContent = result.error;
                } else {
                    jobProgress.textContent = "Job did not finish.";
                }
                if (result && result.warnings && result.warnings.length) {
                    jobError.textContent += (jobError.textContent ? "\n" : "") + result.warnings.join("\n");
                }
                return;
            }
            const n = (result.placed || []).length;
            const names = (result.placed || []).map((p) => p.clipName).filter(Boolean);
            jobProgress.textContent =
                `Placed ${n} isolated WAV(s)` +
                (names.length ? ` — ${names.join(", ")}` : "") +
                (result.mute && result.mute.muted
                    ? `  ·  muted ${result.mute.muted} original clip(s)`
                    : "");
            if (result.warnings && result.warnings.length) {
                jobError.textContent = result.warnings.join("\n");
            }
        } catch (err) {
            jobError.textContent = String(err && err.message ? err.message : err);
            jobProgress.textContent = "Job failed. Timeline was not changed.";
        } finally {
            setJobRunning(false);
        }
    });

    cancelBtn.addEventListener("click", async () => {
        cancelBtn.disabled = true;
        try {
            const result = await window.perfectvoice.cancelJob();
            if (!result || !result.ok) {
                jobError.textContent = (result && result.error) || "Cancel failed.";
            } else {
                jobProgress.textContent = "Cancel requested…";
            }
        } catch (err) {
            jobError.textContent = String(err && err.message ? err.message : err);
        }
    });

    downloadBtn.addEventListener("click", async () => {
        jobError.textContent = "";
        downloadBtn.disabled = true;
        try {
            const result = await window.perfectvoice.downloadModel(selectedModel());
            if (result && result.ok) {
                jobProgress.textContent = "Model download started.";
                return;
            }
            jobError.textContent =
                (result && result.error) || "Download model is not implemented in this release.";
        } catch (err) {
            jobError.textContent = String(err && err.message ? err.message : err);
        } finally {
            downloadBtn.disabled = false;
            syncRunButtons();
        }
    });
});
