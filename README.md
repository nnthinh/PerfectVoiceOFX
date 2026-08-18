# PerfectVoice

**Workflow Integration** panel + Python sidecar cho [DaVinci Resolve](https://www.blackmagicdesign.com/products/davinciresolve) **Studio**. Tách **giọng thoại khỏi nhạc nền / musical accompaniment** ngay trên timeline bằng Meta **Demucs** (`htdemucs` / `htdemucs_ft`).

Tên repo là `PerfectVoiceOFX` vì đó là tên user đặt. **Host không phải OpenFX.** OpenFX trong Resolve là image-effect API; không có audio buffer chính thức. Sản phẩm sống ở **Workspace → Workflow Integrations → PerfectVoice**.

## Đây là gì / không phải gì

Editor chọn clip trên Edit hoặc Fairlight → *Remove musical accompaniment* → sidecar Python/PyTorch xử lý **offline** → track audio mới `PV Isolated Voice`, đồng bộ sample-accurate. Clip gốc không bị ghi đè.

**Demucs không phải denoiser** và không phải SOTA vocal isolation 2025–26. Nó là *music source separation* (drums / bass / other / vocals). Mạnh với bed nhạc; yếu với HVAC, giao thông, phòng, mic. Optional **DeepFilterNet 3** (default off) giảm residual environmental noise — không bật thì **không** claim “giọng trong trẻo”. Voice Isolation sẵn có trong Studio vẫn phù hợp hơn nếu tạp âm là noise, không phải bài hát.

v1.0 **không** bundle pretrained weights. User click *Download model* lần đầu.

Thiết kế đầy đủ: [docs/design.md](docs/design.md).

## Yêu cầu (v1.0)

| | |
| --- | --- |
| OS | **macOS 13+ Apple Silicon (arm64)** only |
| Host | **DaVinci Resolve Studio standalone 20.0+** (khuyến nghị **21.0.4**, 2026-08-05). Tải từ Blackmagic, **không** Mac App Store. 18.6 / 19.x không hỗ trợ. |
| UI | English (panel, errors). Tài liệu tiếng Việt. |
| Windows + NVIDIA CUDA | v1.1 — chưa phải v1.0 |
| Intel Mac / Linux | non-goal |

Free Resolve không có Workflow Integration Electron. Studio standalone là bắt buộc.

## Model weights

Installer **không** chứa official `htdemucs*` checkpoints.

1. User click **Download model** trên panel (~84 MB Fast / ~330 MB Quality).
2. File nằm local (`~/Library/Application Support/PerfectVoice/models/demucs/` trên macOS).
3. Infer chỉ `Separator(..., repo=<local path>)`. Không auto-fetch khi chạy job.

Dev được phép tải official weights bằng `scripts/download_demucs.py` (chưa có trong PR này).

Official weights **không** MIT. Alexandre Défossez, [facebookresearch/demucs#327](https://github.com/facebookresearch/demucs/issues/327) (2022-05-23): *“The model weights are not covered by the MIT license, and are provided only for scientific purposes.”* Click *Download model* không tạo sublicense. Chi tiết: [NOTICE](NOTICE), [docs/licenses/](docs/licenses/).

## Cây repo (skeleton)

Logic engine, panel JS, và download script **chưa** có — chỉ placeholder.

```
docs/design.md                 # design (rev 4)
docs/licenses/                 # Demucs MIT + weights disclaimer
host/com.perfectvoice.panel/   # Workflow Integration (trống)
engine/perfectvoice_engine/    # localhost sidecar (`perfectvoice-engine serve`)
engine/models/                 # không commit weights
shared/schema/                 # clip / params / job / hash-fields JSON Schema v1
shared/openapi.yaml            # localhost HTTP sketch (no /v1/models/download yet)
installer/macos/
installer/windows/
scripts/                       # CI URL gate; fetch scripts sau
tests/unit/                    # python3 -m unittest tests/unit/test_schemas.py
tests/golden/
```

## Schema contracts

`shared/schema/` is the clip / params / job v1 contract. Client params **must not** send `wet_dry_sample_rate` (engine-derived). `handles_*_actual` lives on the job result only.

```
python3 -m pip install -r requirements-dev.txt
python3 -m unittest tests.unit.test_schemas tests.unit.test_serve
```

## License

- Code PerfectVoice: [MIT](LICENSE), Copyright 2026 PerfectVoice contributors.
- Third-party: [NOTICE](NOTICE).
