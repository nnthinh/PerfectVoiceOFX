# Windows installer (PR 13, v1.1)

PowerShell **user-space** copy cho panel + CUDA engine. Cùng IPC/auth với
macOS / PR 02: token-file hoặc stdin, bind `127.0.0.1`, **không** `--token-fd 3`.
`protocol_version` = **1**.

Inno/WiX (ký Authenticode + SmartScreen) là bước ship sau. PR này không đổi
HTTP contract.

## Path (đóng băng §3.8)

| | User-space (mặc định) |
| --- | --- |
| Engine | `%LOCALAPPDATA%\PerfectVoice\engine\perfectvoice-engine.exe` |
| Models | `%LOCALAPPDATA%\PerfectVoice\models\` |
| Logs | `%LOCALAPPDATA%\PerfectVoice\Logs\` |
| Cache | `%LOCALAPPDATA%\PerfectVoice\Cache\` |
| Token/run | `%LOCALAPPDATA%\PerfectVoice\run\` |
| Config | `%LOCALAPPDATA%\PerfectVoice\config.json` |
| Panel | `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Workflow Integration Plugins\com.perfectvoice.panel\` |

`enginePath` rule 4: panel trên Windows đọc
`%LOCALAPPDATA%\PerfectVoice\engine\perfectvoice-engine.exe` (không `~/Library`).
`spawn` absolute path; env Win32 giữ `SYSTEMROOT` + `LOCALAPPDATA` + PATH tối thiểu
`%SystemRoot%\System32`. Dev: env `PERFECTVOICE_ENGINE` (absolute).

Design ghi path plugin all-users `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Workflow Integration Plugins\`.
Installer **từ chối** `-System` / `Program Files` / `ProgramData` (không admin,
không đè plugin máy). **Chưa** có spike Windows xác nhận Resolve scan `%APPDATA%`.
Nếu Workspace → Workflow Integrations không hiện PerfectVoice: copy **tay** thư mục
user-space vào PROGRAMDATA. **Không** chạy lại script elevated — script từ chối
Administrator trên dest live.

Lock file (khi engine implement): `%LOCALAPPDATA%\PerfectVoice\engine.lock`.

Reinstall **xóa** dest `engine\` + `panel\` rồi copy lại (không overlay). Leftover
`.node` / `_internal` DLL / weight từ lần trước không được giữ.

## Payload

- **Engine:** PyInstaller **onedir** Windows x64, entry `perfectvoice-engine.exe`.
  Torch wheel **cu126** (xem [CUDA SKU](#cuda-sku)). **Không** Python / conda user.
- **Không** bundle official `htdemucs*` / `.th` / `.safetensors` / DeepFilterNet
  `*.onnx`. User click *Download model* (PR 15). `-EngineDir` **và** panel source
  **fail-closed** nếu còn checkpoint (scan cả file ẩn; copier **không** strip).
- **Không** bundle `WorkflowIntegration.node`. Có trong `host/…/panel` → exit 1.
  Copy từ Studio **sau** khi dest panel đã wipe sạch.
- IPC không đổi: `serve --bind 127.0.0.1 --port 0 --token-file <abs>`
  (hoặc một dòng token trên stdin rồi EOF). Cấm `--token-fd`.

## Lệnh

Chạy **không** elevated. PowerShell 5.1+ hoặc pwsh 7.

```powershell
# cài user-space (cần onedir đã build)
powershell -NoProfile -ExecutionPolicy Bypass -File installer\windows\Install-User.ps1 `
  -EngineDir D:\build\perfectvoice-engine-onedir

# tương đương
installer\windows\install-user.cmd -EngineDir D:\build\perfectvoice-engine-onedir

# stage vào thư mục tạm, không ghi %LOCALAPPDATA%
powershell -NoProfile -ExecutionPolicy Bypass -File installer\windows\Install-User.ps1 `
  -DryRun

# gỡ engine + panel; giữ models / Cache / Logs trừ khi -Purge
installer\windows\uninstall-user.cmd
```

`-System` / chạy-as-Administrator → exit 2.

Policy (chạy được trên macOS CI, không cần Windows):

```bash
bash installer/windows/check-policy.sh
```

## CUDA SKU

Pin máy-đọc: [`cuda-sku.txt`](cuda-sku.txt).

| | v1.1 lock |
| --- | --- |
| **Ship wheel** | PyTorch **`cu126`** (CUDA **12.6**) |
| **Index** | `https://download.pytorch.org/whl/cu126` |
| **Range chấp nhận** | CUDA **12.6–12.8** (`cu126` / `cu128`). **Một** SKU / onedir. |
| **Không ship** | `cu118` (CUDA 11.8), CPU-only, ROCm |
| **Python build** | **3.12** x64 (`win_amd64`) |
| **torch** | ≥ 2.6 (stable selector tại thời điểm lock: 2.7.x) |
| **Driver** | NVIDIA Studio / Game Ready **≥ 560** (`cu126`). `cu128` cần driver mới hơn (~570). |
| **CUDA Toolkit** | **Không** cần trên máy editor — wheel nhúng runtime. |

Build onedir (máy Windows + NVIDIA, **không** chạy lúc cài):

```powershell
py -3.12 -m venv .venv-cu126
.\.venv-cu126\Scripts\pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126
.\.venv-cu126\Scripts\pip install -e engine pyinstaller
# rồi PyInstaller onedir → perfectvoice-engine.exe
# không copy models/*.th vào dist
```

`nvidia-smi` trên máy editor phải báo CUDA ≥ 12.6. Thiếu GPU / driver cũ:
engine vẫn bind localhost; job CUDA fail closed (không đổi protocol).

## VC++ Redistributable

PyTorch + onedir cần **Microsoft Visual C++ 2015–2022 (x64)**.

- URL: <https://aka.ms/vs/17/release/vc_redist.x64.exe>
- Installer **cảnh báo** nếu thiếu key
  `HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64` — không
  silent-download, không fail copy.
- Không bundle `vc_redist.x64.exe` trong payload engine.

## SmartScreen

Unsigned `.exe` / `.cmd` / `.ps1` tải từ internet sẽ dính
**Windows protected your PC**. Đây là kỳ vọng cho bản dev.

- Không tắt SmartScreen / Windows Defender.
- Unblock: Properties → *Unblock*, hoặc `Unblock-File`.
- More info → Run anyway chỉ cho bản tự build.
- Ship: ký **Authenticode** ( ideally EV) cho `perfectvoice-engine.exe` và
  installer Inno/WiX sau này. Reputation SmartScreen cần thời gian + số
  download, không có mẹo tắt.
- ExecutionPolicy: wrapper `.cmd` dùng `-ExecutionPolicy Bypass` **chỉ**
  cho file repo này, không đổi policy máy.

## IPC / auth (không đổi)

```
spawn(absEnginePath,
      ["serve", "--bind", "127.0.0.1", "--port", "0", "--token-file", tokenPath],
      { cwd: engineDir })
# token 256-bit hex, file one-shot ACL user-only dưới %LOCALAPPDATA%\PerfectVoice\run\
# engine đọc, unlink, in READY http://127.0.0.1:<port>
# GET /v1/health  Authorization: Bearer <token>
# → {"ok": true, "protocol_version": 1}
```

`protocol_version` lệch → panel *"Update PerfectVoice engine"*. Không đoán field.

## Cài / gỡ

Gỡ: xóa engine + panel (script `-Uninstall`). Giữ
`%LOCALAPPDATA%\PerfectVoice\models\` và `Cache\` trừ khi `-Purge`.

`WorkflowIntegration.node` lấy từ Studio:

- `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Workflow Integrations\Examples\SamplePlugin\WorkflowIntegration.node`
- hoặc `C:\Program Files\Blackmagic Design\DaVinci Resolve\Developer\Workflow Integrations\Examples\SamplePlugin\WorkflowIntegration.node`

Restart Resolve Studio → Workspace → Workflow Integrations → PerfectVoice.
