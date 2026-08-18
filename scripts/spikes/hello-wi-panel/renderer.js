window.addEventListener("DOMContentLoaded", async () => {
    const log = document.getElementById("log");
    const pathEl = document.getElementById("enginePath");
    function line(msg) {
        log.textContent += msg + "\n";
    }
    try {
        pathEl.textContent = await window.pvSpike.enginePath();
    } catch (err) {
        pathEl.textContent = String(err);
    }
    document.getElementById("spawnBtn").addEventListener("click", async () => {
        log.textContent = "";
        try {
            const result = await window.pvSpike.spawnHello();
            line(JSON.stringify(result, null, 2));
        } catch (err) {
            line(String(err && err.message ? err.message : err));
        }
    });
});
