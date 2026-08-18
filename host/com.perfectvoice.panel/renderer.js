window.addEventListener("DOMContentLoaded", async () => {
    const statusEl = document.getElementById("status");
    const pathEl = document.getElementById("enginePath");
    const healthEl = document.getElementById("health");
    const errorEl = document.getElementById("error");
    const resolveEl = document.getElementById("resolveNote");
    const startBtn = document.getElementById("startBtn");

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
});
