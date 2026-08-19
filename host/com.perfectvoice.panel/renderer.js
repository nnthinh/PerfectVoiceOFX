window.addEventListener("DOMContentLoaded", async () => {
    const statusEl = document.getElementById("status");
    const pathEl = document.getElementById("enginePath");
    const errorEl = document.getElementById("error");
    const resolveEl = document.getElementById("resolveNote");
    const removeBtn = document.getElementById("removeBtn");
    const cancelBtn = document.getElementById("cancelBtn");
    const muteCheck = document.getElementById("muteCheck");
    const cacheCheck = document.getElementById("cacheCheck");
    const modelSelect = document.getElementById("modelSelect");
    const wetInput = document.getElementById("wetInput");
    const wetVal = document.getElementById("wetVal");
    const shiftsSelect = document.getElementById("shiftsSelect");
    const overlapSelect = document.getElementById("overlapSelect");
    const overallBar = document.getElementById("overallBar");
    const overallPctText = document.getElementById("overallPctText");
    const currentBar = document.getElementById("currentBar");
    const currentPassTitle = document.getElementById("currentPassTitle");
    const currentPassPctText = document.getElementById("currentPassPctText");
    const statElapsed = document.getElementById("statElapsed");
    const statEta = document.getElementById("statEta");
    const statSpeed = document.getElementById("statSpeed");
    const statPasses = document.getElementById("statPasses");
    const jobBadge = document.getElementById("jobBadge");
    const jobLog = document.getElementById("jobLog");
    const jobError = document.getElementById("jobError");
    const dlWrap = document.getElementById("dlWrap");
    const dlLabel = document.getElementById("dlLabel");
    const dlBar = document.getElementById("dlBar");
    const dlLog = document.getElementById("dlLog");
    const telemetryWrap = document.getElementById("telemetryWrap");
    const telCompute = document.getElementById("telCompute");
    const telRam = document.getElementById("telRam");
    const telCpu = document.getElementById("telCpu");
    const tabMusic = document.getElementById("tabMusic");
    const tabTse = document.getElementById("tabTse");
    const musicControls = document.getElementById("musicControls");
    const tseControls = document.getElementById("tseControls");
    const speakerSelect = document.getElementById("speakerSelect");
    const speakerCount = document.getElementById("speakerCount");
    const enrollBtn = document.getElementById("enrollBtn");
    const delSpeakerBtn = document.getElementById("delSpeakerBtn");

    let currentMode = "music";
    let enrolledSpeakers = [];
    let lastStatus = null;
    let lastInspect = null;
    let jobRunning = false;
    let downloading = false;
    let cancelTimer = null;
    let jobStartTime = null;
    let jobTimerInterval = null;
    let logEntries = [];

    let lastOverallPct = 0;
    let lastAudioDurS = 0;
    let lastShiftIdx = 0;
    let lastSpeed = 0;

    function formatTime() {
        const d = new Date();
        const pad = (n) => String(n).padStart(2, "0");
        return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    }

    function formatSecs(sec) {
        if (!Number.isFinite(sec) || sec < 0) return "00:00";
        const m = Math.floor(sec / 60);
        const s = Math.floor(sec % 60);
        return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    }

    function setBadge(state, text) {
        if (!jobBadge) return;
        jobBadge.className = `job-badge ${state}`;
        jobBadge.textContent = String(text).toUpperCase();
    }

    function appendLog(line) {
        if (!jobLog || !line) return;
        const formatted = `[${formatTime()}] ${line}`;
        logEntries.push(formatted);
        if (logEntries.length > 80) logEntries.shift();
        jobLog.textContent = logEntries.join("\n");
        jobLog.scrollTop = jobLog.scrollHeight;
    }

    function clearLog(initialLine) {
        logEntries = [];
        if (initialLine) {
            appendLog(initialLine);
        } else if (jobLog) {
            jobLog.textContent = "";
        }
    }

    function updateWetLabel(val) {
        if (!wetVal) return;
        const v = Number(val);
        if (!Number.isFinite(v)) return;
        const pct = Math.round(v * 100);
        wetVal.textContent = `${v.toFixed(2)} (${pct}%)`;
    }

    function startJobTelemetry() {
        if (telemetryWrap) telemetryWrap.hidden = false;
        jobStartTime = Date.now();
        lastOverallPct = 0;
        lastAudioDurS = 0;
        lastShiftIdx = 0;
        lastSpeed = 0;

        if (telCompute) telCompute.textContent = "Apple Metal (MPS)";
        if (telRam) telRam.textContent = "Measuring…";
        if (telCpu) telCpu.textContent = "Active";
        if (telTime) telTime.textContent = "0.0s";

        if (statElapsed) statElapsed.textContent = "00:00";
        if (statEta) statEta.textContent = "Estimating…";
        if (statSpeed) statSpeed.textContent = "—";
        if (statPasses) statPasses.textContent = "Calculating…";
        if (overallBar) overallBar.style.width = "0%";
        if (currentBar) currentBar.style.width = "0%";
        if (overallPctText) overallPctText.textContent = "0%";
        if (currentPassPctText) currentPassPctText.textContent = "—";

        if (jobTimerInterval) clearInterval(jobTimerInterval);
        jobTimerInterval = setInterval(() => {
            if (!jobStartTime) return;
            const E = (Date.now() - jobStartTime) / 1000;
            if (telTime) telTime.textContent = `${E.toFixed(1)}s`;
            if (statElapsed) statElapsed.textContent = formatSecs(E);

            if (lastOverallPct > 0 && E >= 1.0) {
                const f = lastOverallPct / 100;
                const eta = Math.max(0, (E / f) - E);
                if (statEta) statEta.textContent = `~${formatSecs(eta)}`;
                if (lastAudioDurS > 0) {
                    lastSpeed = (lastAudioDurS * f) / E;
                    if (statSpeed) statSpeed.textContent = `${lastSpeed.toFixed(1)}× RT`;
                }
            }
        }, 200);
    }

    function stopJobTelemetry(success, isCached) {
        if (jobTimerInterval) {
            clearInterval(jobTimerInterval);
            jobTimerInterval = null;
        }
        if (jobStartTime) {
            const E = (Date.now() - jobStartTime) / 1000;
            if (telTime) telTime.textContent = isCached ? `${E.toFixed(1)}s (Cache)` : `${E.toFixed(1)}s`;
            if (statElapsed) statElapsed.textContent = formatSecs(E);
            if (statEta) statEta.textContent = "00:00 (Done)";
            if (statSpeed) {
                statSpeed.textContent = isCached
                    ? "Instant (Cache)"
                    : lastSpeed > 0
                        ? `${lastSpeed.toFixed(1)}× RT`
                        : "Done";
            }
        }
        if (telCpu) {
            telCpu.textContent = success ? "Idle" : "—";
        }
    }

    function updateTelemetry(data) {
        if (!telemetryWrap) return;
        if (!data) return;
        telemetryWrap.hidden = false;
        if (data.device && telCompute) {
            const dev = String(data.device).toLowerCase();
            if (dev === "mps") {
                telCompute.textContent = "Apple Metal (MPS)";
            } else if (dev === "cuda") {
                telCompute.textContent = "NVIDIA CUDA (GPU)";
            } else {
                telCompute.textContent = "CPU (Float32)";
            }
        }
        if (data.memory_mb != null && Number(data.memory_mb) > 0 && telRam) {
            const mb = Number(data.memory_mb);
            telRam.textContent = mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${Math.round(mb)} MB`;
        }
        if (data.cpu_percent != null && Number(data.cpu_percent) >= 0 && telCpu) {
            const cpu = Math.round(Number(data.cpu_percent));
            telCpu.textContent = cpu > 0 ? `${cpu}%` : "Active";
        }
    }

    function paint(s) {
        const connected = !!(s && s.connected);
        const statusDot = document.getElementById("statusDot");
        if (statusEl) {
            statusEl.textContent = connected ? "Engine connected" : "Engine not connected";
            statusEl.className = connected ? "status-text ok" : "status-text off";
        }
        if (statusDot) {
            statusDot.className = connected ? "status-dot ok" : "status-dot off";
        }
        if (pathEl) pathEl.textContent = (s && s.enginePath) || "not found";
        if (resolveEl) {
            if (s && s.resolveReady) {
                resolveEl.textContent = "";
            } else if (s && s.resolveError) {
                resolveEl.textContent = s.resolveError;
            }
        }
        if (errorEl) errorEl.textContent = (s && s.error) || "";
        lastStatus = s;
        syncRunButtons();
    }

    function engineHealthy(s) {
        if (!s || !s.connected || !s.health) return false;
        return s.health.ok === true || s.health.status === "ok";
    }

    async function loadSpeakers() {
        if (!window.perfectvoice.getSpeakers) return;
        try {
            const res = await window.perfectvoice.getSpeakers();
            if (res && res.ok && Array.isArray(res.speakers)) {
                enrolledSpeakers = res.speakers;
                if (speakerSelect) {
                    speakerSelect.innerHTML = "";
                    const autoOpt = document.createElement("option");
                    autoOpt.value = "";
                    autoOpt.textContent = "🎯 Auto-Sample at Playhead";
                    speakerSelect.appendChild(autoOpt);

                    for (const spk of enrolledSpeakers) {
                        const opt = document.createElement("option");
                        opt.value = spk.speaker_id;
                        opt.textContent = `👤 ${spk.name} (${spk.sample_duration_s}s)`;
                        speakerSelect.appendChild(opt);
                    }
                }
                if (speakerCount) {
                    speakerCount.textContent = enrolledSpeakers.length > 0
                        ? `${enrolledSpeakers.length} saved`
                        : "Auto (Playhead)";
                }
                syncRunButtons();
            }
        } catch {
            // ignore
        }
    }

    function setMode(mode) {
        currentMode = mode;
        if (mode === "tse") {
            if (tabTse) tabTse.classList.add("active");
            if (tabMusic) tabMusic.classList.remove("active");
            if (tseControls) tseControls.style.display = "block";
            if (musicControls) musicControls.style.display = "none";
            loadSpeakers();
            appendLog("Switched to Target Speaker Extraction (TSE) Mode.");
            appendLog("TSE isolates the enrolled speaker and eliminates background singing vocals.");
        } else {
            if (tabMusic) tabMusic.classList.add("active");
            if (tabTse) tabTse.classList.remove("active");
            if (musicControls) musicControls.style.display = "block";
            if (tseControls) tseControls.style.display = "none";
            appendLog("Switched to Music & Beats (Demucs) Mode.");
        }
        syncRunButtons();
    }

    if (tabMusic) tabMusic.addEventListener("click", () => setMode("music"));
    if (tabTse) tabTse.addEventListener("click", () => setMode("tse"));

    if (enrollBtn) {
        enrollBtn.addEventListener("click", async () => {
            enrollBtn.disabled = true;
            appendLog("🎙️ Enrolling speaker voiceprint: Pre-separating vocal sample via fast Demucs…");
            try {
                const res = await window.perfectvoice.enrollSpeaker();
                if (res && res.ok && res.speaker) {
                    appendLog(`✅ Successfully enrolled speaker "${res.speaker.name}" (${res.speaker.sample_duration_s}s clean vocal sample)!`);
                    appendLog("Profile is ready. Select target clip(s) and click 'Clean voice' in TSE mode.");
                    await loadSpeakers();
                    if (speakerSelect) speakerSelect.value = res.speaker.speaker_id;
                } else {
                    const err = (res && res.error) || "Enrollment failed. Select a clip on timeline first.";
                    appendLog(`❌ Enrollment failed: ${err}`);
                    jobError.textContent = err;
                }
            } catch (err) {
                appendLog(`❌ Enrollment error: ${err.message || err}`);
            } finally {
                enrollBtn.disabled = false;
            }
        });
    }

    if (delSpeakerBtn) {
        delSpeakerBtn.addEventListener("click", async () => {
            const spkId = speakerSelect ? speakerSelect.value : "";
            if (!spkId) return;
            try {
                await window.perfectvoice.deleteSpeaker(spkId);
                appendLog("Deleted speaker profile.");
                await loadSpeakers();
            } catch (err) {
                appendLog(`Failed to delete profile: ${err.message || err}`);
            }
        });
    }

    function selectedModel() {
        return (modelSelect && modelSelect.value) || "htdemucs";
    }

    function currentPrefs() {
        const prefs = {
            model: selectedModel(),
            dfn: false,
            muteOriginal: !!(muteCheck && muteCheck.checked),
            useCache: !(cacheCheck && !cacheCheck.checked),
            wet: wetInput ? Number(wetInput.value) : 0.85,
            shifts: shiftsSelect ? parseInt(shiftsSelect.value, 10) : 1,
            overlap: overlapSelect ? Number(overlapSelect.value) : 0.25,
            mode: currentMode,
        };
        if (currentMode === "tse" && speakerSelect && speakerSelect.value) {
            prefs.speaker_id = speakerSelect.value;
        }
        return prefs;
    }

    function applyPrefs(p) {
        if (!p) return;
        if (p.model && modelSelect) modelSelect.value = p.model;
        if (p.wet != null && wetInput) {
            wetInput.value = String(p.wet);
            updateWetLabel(p.wet);
        }
        if (p.shifts != null && shiftsSelect) shiftsSelect.value = String(p.shifts);
        if (p.overlap != null && overlapSelect) overlapSelect.value = String(p.overlap);
        if (p.muteOriginal != null && muteCheck) muteCheck.checked = !!p.muteOriginal;
        if (p.useCache != null && cacheCheck) cacheCheck.checked = !!p.useCache;
        if (p.mode) setMode(p.mode);
    }

    function persistPrefs() {
        if (window.perfectvoice.setUiPrefs) {
            window.perfectvoice.setUiPrefs(currentPrefs()).catch(() => {});
        }
    }

    function formatBytes(bytes) {
        const n = Number(bytes);
        if (!Number.isFinite(n) || n <= 0) return "0 B";
        const units = ["B", "KB", "MB", "GB"];
        const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
        const val = n / Math.pow(1024, i);
        return `${val.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
    }

    function isModelReady(status, name) {
        if (!status || !status.health || !status.health.models_ready) return false;
        return !!status.health.models_ready[name];
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
        if (total > 0) {
            appendLog(`Downloading ${file}: ${pct}% (${formatBytes(done)} / ${formatBytes(total)})`);
        }
    }

    function hideDownloadProgress() {
        if (dlWrap) dlWrap.hidden = true;
        if (dlBar) dlBar.style.width = "0";
    }

    function syncRunButtons() {
        const isTse = currentMode === "tse";
        const ready = isModelReady(lastStatus, selectedModel());
        const busy = jobRunning || downloading;
        if (removeBtn) removeBtn.disabled = busy || !ready;
        if (!jobRunning && cancelBtn) {
            cancelBtn.disabled = true;
        }
        if (modelSelect) modelSelect.disabled = busy;
        if (wetInput) wetInput.disabled = busy;
        if (shiftsSelect) shiftsSelect.disabled = busy;
        if (overlapSelect) overlapSelect.disabled = busy;
        if (muteCheck) muteCheck.disabled = busy;
        if (cacheCheck) cacheCheck.disabled = busy;
        if (speakerSelect) speakerSelect.disabled = busy;
        if (enrollBtn) enrollBtn.disabled = busy;
        if (delSpeakerBtn) delSpeakerBtn.disabled = busy;
        if (busy) return;
        if (!engineHealthy(lastStatus)) {
            setBadge("idle", "Starting");
            if (logEntries.length === 0 && jobLog) jobLog.textContent = "Starting engine…";
        } else if (!ready) {
            setBadge("idle", "Downloading");
            if (logEntries.length === 0 && jobLog) jobLog.textContent = "Model not ready. Downloading…";
        } else {
            if (logEntries.length === 0 && jobLog) {
                setBadge("idle", "Ready");
                jobLog.textContent = isTse
                    ? "Target Speaker Ready. Place playhead on speaker's voice, then click Clean voice."
                    : "Ready. Select a clip with audio, then click Clean voice.";
            }
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
        setBadge("running", "Downloading");
        appendLog(`Model ${selectedModel()} not found locally. Starting download…`);
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
                    setBadge("idle", "Ready");
                    appendLog("Model downloaded and ready.");
                } else {
                    setBadge("error", "Missing");
                    jobError.textContent = "Download finished but engine still reports model as missing.";
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
        await loadSpeakers();
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
    if (wetInput) {
        wetInput.addEventListener("input", () => {
            updateWetLabel(wetInput.value);
        });
        wetInput.addEventListener("change", persistPrefs);
    }
    if (shiftsSelect) {
        shiftsSelect.addEventListener("change", persistPrefs);
    }
    if (overlapSelect) {
        overlapSelect.addEventListener("change", persistPrefs);
    }
    for (const el of [muteCheck, cacheCheck]) {
        if (el) el.addEventListener("change", persistPrefs);
    }

    if (window.perfectvoice.onJobEvent) {
        window.perfectvoice.onJobEvent((ev) => {
            if (!ev) return;
            const data = ev.data || {};
            if (ev.type === "progress") {
                if (data.bytes_done != null || data.filename) {
                    paintDownloadProgress(data);
                    return;
                }
                updateTelemetry(data);

                if (data.overall_pct != null) lastOverallPct = Number(data.overall_pct);
                if (data.audio_dur_s != null) lastAudioDurS = Number(data.audio_dur_s);

                const currentPass = data.current_pass || 1;
                const totalPasses = data.total_passes || 1;
                const overallPct = data.overall_pct != null ? data.overall_pct : 0;
                const chunkPct = data.chunk_pct != null ? data.chunk_pct : 0;
                const chunkIdx = data.chunk_idx || 1;
                const totalChunks = data.total_chunks || 1;
                const shiftIdx = data.shift_idx || 1;
                const totalShifts = data.total_shifts || 1;
                const modelIdx = data.model_idx || 1;
                const totalModels = data.total_models || 1;
                const posS = data.current_pos_s != null ? data.current_pos_s : 0;
                const durS = data.audio_dur_s != null ? data.audio_dur_s : 0;

                // 1. Update Overall Progress Bar & Text
                if (overallBar) overallBar.style.width = `${overallPct}%`;
                if (overallPctText) {
                    overallPctText.textContent = `${overallPct}% (${currentPass}/${totalPasses})`;
                }
                if (statPasses) {
                    statPasses.textContent = totalModels > 1
                        ? `${currentPass} / ${totalPasses} (${totalChunks}c × ${totalShifts}s × ${totalModels}m)`
                        : `${currentPass} / ${totalPasses} (${totalChunks}c × ${totalShifts}s)`;
                }

                // 2. Update Current Pass Progress Bar & Text
                if (currentBar) currentBar.style.width = `${chunkPct}%`;
                if (currentPassTitle) {
                    currentPassTitle.textContent = totalModels > 1
                        ? `Model ${modelIdx}/${totalModels} · Shift ${shiftIdx}/${totalShifts} · Chunk ${chunkIdx}/${totalChunks}`
                        : `Shift ${shiftIdx}/${totalShifts} · Chunk ${chunkIdx}/${totalChunks}`;
                }
                if (currentPassPctText) {
                    currentPassPctText.textContent = durS > 0
                        ? `${chunkPct}% (${posS}s / ${durS}s)`
                        : `${chunkPct}%`;
                }

                // 3. Smart Terminal Logging (Throttled per shift/model & key milestones)
                const shiftKey = `${modelIdx}_${shiftIdx}`;
                if (shiftKey !== lastShiftIdx) {
                    const modelPrefix = totalModels > 1 ? `Model ${modelIdx}/${totalModels} · ` : "";
                    appendLog(`🔄 ${modelPrefix}Shift ${shiftIdx}/${totalShifts} started (${totalChunks} chunks · ~${durS}s audio)...`);
                    lastShiftIdx = shiftKey;
                }
                if (
                    chunkIdx === 1 ||
                    chunkIdx === Math.round(totalChunks / 2) ||
                    chunkIdx === totalChunks
                ) {
                    appendLog(
                        `Pass ${currentPass}/${totalPasses} · Chunk ${chunkIdx}/${totalChunks} · ${chunkPct}%`
                    );
                }
            }
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
        startJobTelemetry();
        setBadge("running", "Running");
        clearLog("Starting Clean voice job…");
        appendLog("Inspecting timeline selection…");
        let success = false;
        let isCached = false;
        try {
            const prefs = currentPrefs();
            const result = await window.perfectvoice.removeAccompaniment(prefs);
            if (!result || !result.ok) {
                setBadge("error", "Error");
                if (overallBar) overallBar.style.width = "0%";
                if (currentBar) currentBar.style.width = "0%";
                if (statEta) statEta.textContent = "—";
                const errText = (result && result.error) || "Clean voice failed.";
                jobError.textContent = errText;
                if (result && result.cancelled) {
                    appendLog("Job cancelled. Timeline was not changed.");
                } else if (result && result.error && /Model not installed/i.test(result.error)) {
                    appendLog(`Error: ${result.error}`);
                } else {
                    appendLog(`Failed: ${errText}`);
                }
                if (result && result.warnings && result.warnings.length) {
                    for (const w of result.warnings) appendLog(`Warning: ${w}`);
                }
                return;
            }
            success = true;
            jobError.textContent = "";
            if (overallBar) overallBar.style.width = "100%";
            if (currentBar) currentBar.style.width = "100%";
            const placed = result.placed || [];
            const n = placed.length;
            isCached = placed.some((p) => p.cacheHit);
            if (isCached) {
                setBadge("cached", "Cache Hit");
                if (overallPctText) overallPctText.textContent = "100% (Instant)";
                if (currentPassPctText) currentPassPctText.textContent = "Cache Hit";
                if (statPasses) statPasses.textContent = "Cached";
                appendLog("⚡ Cache Hit: Reused previously isolated audio (instant).");
                appendLog("Tip: Uncheck 'Use cache' above if you want to re-process with new mix settings.");
            } else {
                setBadge("done", "Completed");
                if (overallPctText) overallPctText.textContent = "100%";
                if (currentPassPctText) currentPassPctText.textContent = "Done";
                const speedText = lastSpeed > 0 ? `${lastSpeed.toFixed(1)}× Real-time` : "Done";
                appendLog(`Inference completed successfully (${speedText}).`);
            }
            const names = placed.map((p) => p.clipName).filter(Boolean);
            appendLog(`Placed ${n} isolated WAV(s) -> track "PV Isolated Voice"${names.length ? ` (${names.join(", ")})` : ""}`);
            if (result.mute && result.mute.muted) {
                appendLog(`Muted ${result.mute.muted} original timeline clip(s).`);
            }
            if (result.warnings && result.warnings.length) {
                for (const w of result.warnings) appendLog(`Notice: ${w}`);
            }
        } catch (err) {
            setBadge("error", "Error");
            if (overallBar) overallBar.style.width = "0%";
            if (currentBar) currentBar.style.width = "0%";
            const msg = String(err && err.message ? err.message : err);
            jobError.textContent = msg;
            appendLog(`Job failed: ${msg}`);
        } finally {
            stopJobTelemetry(success, isCached);
            setJobRunning(false);
        }
    });

    cancelBtn.addEventListener("click", async () => {
        cancelBtn.disabled = true;
        appendLog("Cancel requested…");
        try {
            const result = await window.perfectvoice.cancelJob();
            if (!result || !result.ok) {
                jobError.textContent = (result && result.error) || "Cancel failed.";
                appendLog(`Cancel failed: ${(result && result.error) || "Unknown"}`);
            }
        } catch (err) {
            jobError.textContent = String(err && err.message ? err.message : err);
        }
    });
});
