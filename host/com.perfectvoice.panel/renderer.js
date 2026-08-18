window.addEventListener("DOMContentLoaded", async () => {
    const statusEl = document.getElementById("status");
    const pathEl = document.getElementById("enginePath");
    const healthEl = document.getElementById("health");
    const errorEl = document.getElementById("error");
    const resolveEl = document.getElementById("resolveNote");
    const startBtn = document.getElementById("startBtn");
    const inspectBtn = document.getElementById("inspectBtn");
    const placeBtn = document.getElementById("placeBtn");
    const clipList = document.getElementById("clipList");
    const inspectMeta = document.getElementById("inspectMeta");
    const inspectError = document.getElementById("inspectError");

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
    }

    function fmtNum(n, digits) {
        if (n == null || !Number.isFinite(Number(n))) return "—";
        return Number(n).toFixed(digits);
    }

    function paintInspect(result) {
        inspectError.textContent = "";
        clipList.replaceChildren();
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
            startBtn.disabled = false;
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
            inspectBtn.disabled = false;
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
            placeBtn.disabled = false;
        }
    });
});
