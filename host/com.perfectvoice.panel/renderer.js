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
    const dlWrap = document.getElementById("dlWrap");
    const dlLabel = document.getElementById("dlLabel");
    const dlBar = document.getElementById("dlBar");
    const dlLog = document.getElementById("dlLog");

    let lastStatus = null;
    let lastInspect = null;
    let jobRunning = false;
    let downloading = false;
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

    function selectedModel() {
        return (modelSelect && modelSelect.value) || "htdemucs";
    }

    function currentPrefs() {
        return {
            model: selectedModel(),
            dfn: !!(dfnCheck && dfnCheck.checked),
            muteOriginal: !!(muteCheck && muteCheck.checked),
            useCache: !(cacheCheck && !cacheCheck.checked),
        };
    }

    function applyPrefs(prefs) {
        if (!prefs) return;
        if (modelSelect && (prefs.model === "htdemucs" || prefs.model === "htdemucs_ft")) {
            modelSelect.value = prefs.model;
        }
        if (dfnCheck) dfnCheck.checked = prefs.dfn === true;
        if (muteCheck) muteCheck.checked = prefs.muteOriginal === true;
        if (cacheCheck) cacheCheck.checked = prefs.useCache !== false;
    }

    function persistPrefs() {
        if (!window.perfectvoice.setUiPrefs) return;
        window.perfectvoice.setUiPrefs(currentPrefs()).catch(() => {});
    }

    function modelReadyMap(s) {
        if (!s) return null;
        if (s.modelsReady && typeof s.modelsReady === "object") return s.modelsReady;
        if (s.health && s.health.models_ready && typeof s.health.models_ready === "object") {
            return s.health.models_ready;
        }
        return null;
    }

    function isModelReady(s, name) {
        const ready = modelReadyMap(s);
        return !!(ready && ready[name] === true);
    }

    function formatBytes(n) {
        const v = Number(n);
        if (!Number.isFinite(v) || v < 0) return "—";
        if (v < 1024) return `${Math.round(v)} B`;
        if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
        return `${(v / (1024 * 1024)).toFixed(1)} MB`;
    }

    function paintDownloadProgress(data) {
        if (!dlWrap) return;
        dlWrap.hidden = false;
        const file = (data && data.filename) || selectedModel();
        const done = Number(data && data.bytes_done);
        const total = Number(data && data.bytes_total);
        const pct = total > 0 && Number.isFinite(done) ? Math.min(100, Math.round((100 * done) / total)) : 0;
        if (dlBar) dlBar.style.width = `${pct}%`;
        if (dlLabel) {
            dlLabel.textContent = total > 0
                ? `Downloading ${file}… ${pct}%`
                : `Downloading ${file}…`;
        }
        if (dlLog) {
            dlLog.textContent = total > 0
                ? `${file}\n${formatBytes(done)} / ${formatBytes(total)}`
                : `${file}\n${formatBytes(done)}`;
        }
        jobProgress.textContent = total > 0
            ? `Downloading ${file}: ${pct}% (${formatBytes(done)} / ${formatBytes(total)})`
            : `Downloading ${file}…`;
    }

    function hideDownloadProgress() {
        if (dlWrap) dlWrap.hidden = true;
        if (dlBar) dlBar.style.width = "0";
    }

    function syncRunButtons() {
        const ready = isModelReady(lastStatus, selectedModel());
        const busy = jobRunning || downloading;
        removeBtn.disabled = busy || !ready;
        if (!jobRunning) {
            cancelBtn.disabled = true;
        }
        startBtn.disabled = busy;
        inspectBtn.disabled = busy;
        placeBtn.disabled = busy || !ready;
        downloadBtn.disabled = busy || ready;
        if (modelSelect) modelSelect.disabled = busy;
        if (busy) return;
        const current = jobProgress.textContent || "";
        const isHint =
            !current ||
            current === "—" ||
            /select a clip|starting engine|downloading |model ready/i.test(current);
        if (!isHint) return;
        if (!engineHealthy(lastStatus)) {
            jobProgress.textContent = "Starting engine…";
        } else if (!ready) {
            jobProgress.textContent = "Model not ready. Downloading…";
        } else {
            jobProgress.textContent = "Select a clip with audio, then click Clean voice.";
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

    async function ensureModelDownloaded() {
        if (downloading) return;
        if (!engineHealthy(lastStatus)) return;
        if (isModelReady(lastStatus, selectedModel())) {
            hideDownloadProgress();
            syncRunButtons();
            return;
        }
        downloading = true;
        jobError.textContent = "";
        paintDownloadProgress({ filename: selectedModel(), bytes_done: 0, bytes_total: 0 });
        syncRunButtons();
        try {
            const result = await window.perfectvoice.downloadModel(selectedModel());
            paint(result && result.connected != null ? result : await window.perfectvoice.status());
            if (result && result.ok) {
                hideDownloadProgress();
                if (!isModelReady(lastStatus, selectedModel())) {
                    paint(await window.perfectvoice.startEngine());
                }
                if (isModelReady(lastStatus, selectedModel())) {
                    jobProgress.textContent =
                        "Model ready. Select a clip with audio, then click Clean voice.";
                } else {
                    jobError.textContent =
                        "Download finished but the engine still reports the model as missing.";
                    jobProgress.textContent = "Model not ready.";
                }
            } else if (result && result.notImplemented) {
                hideDownloadProgress();
                jobError.textContent = result.error || "Download is not available on this engine.";
            } else {
                jobError.textContent = (result && result.error) || "Model download failed.";
            }
        } catch (err) {
            jobError.textContent = String(err && err.message ? err.message : err);
        } finally {
            downloading = false;
            syncRunButtons();
        }
    }

    try {
        if (window.perfectvoice.getUiPrefs) {
            applyPrefs(await window.perfectvoice.getUiPrefs());
        }
        paint(await window.perfectvoice.status());
        if (!engineHealthy(lastStatus)) {
            statusEl.textContent = "Starting engine…";
            paint(await window.perfectvoice.startEngine());
        }
        await ensureModelDownloaded();
    } catch (err) {
        errorEl.textContent = String(err && err.message ? err.message : err);
        syncRunButtons();
    }

    if (modelSelect) {
        modelSelect.addEventListener("change", () => {
            persistPrefs();
            ensureModelDownloaded();
        });
    }
    for (const el of [dfnCheck, muteCheck, cacheCheck]) {
        if (el) el.addEventListener("change", persistPrefs);
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

    function formatJobEvent(ev) {
        if (!ev) return "";
        const data = ev.data || {};
        if (ev.type === "queued") {
            return `Queued job ${data.id || ev.jobId || ""}`.trim();
        }
        if (ev.type === "download") {
            paintDownloadProgress(data);
            return "";
        }
        if (ev.type === "progress") {
            if (data.bytes_done != null || data.filename) {
                paintDownloadProgress(data);
                return "";
            }
            if (data.message) return data.message;
            const len = Number(data.audio_length) || 0;
            const off = Number(data.segment_offset) || 0;
            const pct = len > 0 ? Math.min(100, Math.round((100 * off) / len)) : 0;
            const clip = data.clip_id ? `clip ${data.clip_id.slice(0, 8)}…` : "clip";
            return `Working… ${clip}  ${pct}%  (${off}/${len})`;
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
    if (window.perfectvoice.onDownloadEvent) {
        window.perfectvoice.onDownloadEvent((ev) => {
            paintDownloadProgress((ev && ev.data) || ev || {});
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
                jobError.textContent = (result && result.error) || "Clean voice failed.";
                if (result && result.cancelled) {
                    jobProgress.textContent = "Cancelled. Timeline was not changed.";
                } else if (result && result.error && /Model not installed/i.test(result.error)) {
                    jobProgress.textContent = result.error;
                } else {
                    jobProgress.textContent = "Did not finish.";
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
                (result.outputDir ? `  ·  ${result.outputDir}` : "") +
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
        await ensureModelDownloaded();
    });
});
