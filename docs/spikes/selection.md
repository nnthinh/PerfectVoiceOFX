# Spike PR 00b — selection + place WAV

| Trường | Giá trị |
| --- | --- |
| **Máy** | macOS arm64, `/Applications/DaVinci Resolve/DaVinci Resolve.app` |
| **App đã cài** | **21.0.4** (`CFBundleShortVersionString=21.0.4`, `CFBundleVersion=21.0.40005`) |
| **Scripting README** | `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/README.txt` — Last Updated **24 Jul 2026** |
| **CHANGELOG** | cùng thư mục, Last Updated **5 May 2026** (tới **21.0 Beta**) |
| **Python** | 3.14 import được `DaVinciResolveScript` / `fusionscript.so` |
| **Live dump** | **Chưa.** `scriptapp("Resolve")` trả `None` — Resolve không chạy lúc spike. |

Mục tiêu: khóa đường chọn clip, tên property cho reject matrix, và Appendix A (`endFrame` inclusive/exclusive) — **không đoán** key `GetSetting`.

---

## 1. Máy này đã xác nhận

| Mục | Kết quả | Nguồn |
| --- | --- | --- |
| Studio 21.0.4 standalone đã cài | Có | `Info.plist` |
| `RESOLVE_SCRIPT_API` / `RESOLVE_SCRIPT_LIB` đúng path máy | Có | Machine facts + `scripts/spikes/resolve_connect.py` |
| `import DaVinciResolveScript` trên Python 3.14 | Thành công | chạy trực tiếp |
| Host đang mở để dump `GetSetting()` / clip | **Không** | `scriptapp` = `None` |
| `Timeline.GetSelectedClips()` có trong README 24 Jul 2026 | Có | README dòng Timeline |
| CHANGELOG liệt kê `GetSelectedClips` như API mới 21.0.4 | **Không** | CHANGELOG dừng ở 21.0 Beta (5 May 2026) |
| `endFrame` của `AppendToTimeline` | **Inclusive** | ví dụ official `7_add_subclips_to_timeline.py`: `startFrame=0`, `endFrame=23` = 24 frame đầu |
| Appendix A (clamp + rational fps + cấm `src_in` → `startFrame`) | Unit test pass, không cần Resolve | `tests/unit/test_perfectvoice_time.py` |

**Không** xác nhận bằng lời gọi live: `GetSelectedClips()` có callable trên object timeline 21.0.4, snapshot key `GetSetting` / `GetClipProperty`, giá trị speed / reverse / Elastic Wave.

---

## 2. Còn cần live dump

Chạy khi Studio đang mở, project có clip linked A/V (và nếu được: một clip retime, một clip reverse, một clip Elastic Wave, một clip Voice Isolation):

```bash
python3 scripts/spikes/dump_resolve_selection.py --out /tmp/pv-selection-dump.json
# place thật (đổi timeline) — chỉ khi chủ dự án đồng ý:
python3 scripts/spikes/place_test_wav.py --apply
```

Cần ghi vào dump:

1. `resolve.GetVersion()` / `GetVersionString()` / `GetProductName()` (Studio vs Free).
2. `hasattr(timeline, "GetSelectedClips")` và kết quả gọi khi đang chọn 1 clip linked A/V (kỳ vọng **cả video lẫn audio**).
3. `project.GetSetting()` và `timeline.GetSetting()` **không đối số** — toàn bộ key. Pin `timelineSampleRate` / `timelineFrameRate` (README đã nêu) + format giá trị thật (`"48000"`? `"48 kHz"`? `"29.97 DF"`?).
4. `MediaPoolItem.GetClipProperty()` không đối số — xác nhận `"File Path"`, `"Sample Rate"`, số kênh, FPS nguồn.
5. `TimelineItem.GetProperty()` không đối số trên clip speed ≠ 100%, reverse, Elastic Wave — **tên key speed / reverse / EW chưa có trong README**.
6. `GetVoiceIsolationState()`, `GetSourceAudioChannelMapping()`, `GetLinkedItems()`, `GetStart` / `GetSourceStartTime` / `GetSourceEndTime`.
7. `place_test_wav.py --apply`: `ImportMedia` + `AppendToTimeline` với `mediaType=2`, `startFrame`/`endFrame` trên lưới WAV, `recordFrame=GetStart()`.

Cho tới khi có file dump: **cấm** pin thêm key `GetSetting` / `GetClipProperty` ngoài những gì README + example official đã viết.

---

## 3. Property names từ Developer Scripting API (dump README)

Gọi không đối số (hoặc `None` / key rỗng) trả snapshot mọi key — README § “Looking up Project and Clip properties”. Key sai → kết quả tầm thường, không exception rõ.

### 3.1 `Project.GetSetting` / `Timeline.GetSetting`

| Key | Nguồn | Ý nghĩa README |
| --- | --- | --- |
| `timelineFrameRate` | README + `Examples/5_get_project_information.py` | Timeline frame rate; DF = append `" DF"` (`"29.97 DF"`) |
| `timelineSampleRate` | README | Fairlight *Audio sample rate* |
| `superScale` | README | 0=Auto … 4=4x |
| `timelineResolutionWidth` / `timelineResolutionHeight` | example official | không nằm trong đoạn enumerated, nhưng example BMD dùng |

README **không** liệt kê full set. Design cấm đoán thêm trước dump live.

### 3.2 `MediaPoolItem.GetClipProperty`

| Key | Nguồn |
| --- | --- |
| *(no-arg → dict)* | README |
| `"Super Scale"`, `"Cloud Sync"` | README enumerated |
| `"File Path"` | `Workflow Integrations/Examples/WorkflowIntegrationPythonExample.py` |
| `"File Name"` | `Examples/5_get_project_information.py`, `1_sorted_timeline_from_folder.py` |
| `"Frames"` | `Examples/10_handle_media_pool_clip_markers.py` |
| `"Video Codec"` | `Examples/7_add_subclips_to_timeline.py` |
| `"Date Added"` | Workflow Integration example |
| `"Sample Rate"` | **Design + README prose** (“intrinsic clip properties like date created or sample rate”). **Chưa** thấy string đúng trong example. Live dump bắt buộc trước khi pin schema. |

### 3.3 `TimelineItem` — method (README 24 Jul 2026)

| Method | Trả về | Dùng cho |
| --- | --- | --- |
| `GetSelectedClips()` **trên Timeline** | `[TimelineItem…]` | selection 21.0.4+ |
| `MediaPool.GetSelectedClips()` | `[MediaPoolItem…]` | **bin**, không phải timeline — đừng nhầm |
| `GetStart([subframe])` / `GetEnd` / `GetDuration` | frame timeline | `recordFrame` |
| `GetSourceStartTime()` / `GetSourceEndTime()` | giây, subframe | `t0` / `t1` Appendix A |
| `GetSourceStartFrame()` / `GetSourceEndFrame()` | frame nguồn | fallback nếu thiếu time |
| `GetLeftOffset` / `GetRightOffset` | handle còn lại trên file | đối chiếu clamp |
| `GetLinkedItems()` | `[TimelineItem…]` | dedupe A/V |
| `GetTrackTypeAndIndex()` | `[trackType, trackIndex]` | `"audio"` / `"video"` / `"subtitle"` |
| `GetSourceAudioChannelMapping()` | JSON string | kênh / 5.1 reject |
| `GetAudioMapping()` (MediaPoolItem) | JSON string | `linked_audio.*.path`, `offset` (slip sample) |
| `GetVoiceIsolationState()` | `{isEnabled, amount}` | reject nếu enable (API từ **20.1**) |
| `GetProperty()` / `GetProperty(key)` | dict / value | transform + `RetimeProcess` |
| `GetMediaPoolItem()` | MediaPoolItem | File Path |
| `GetFusionCompCount()` | int | gợi ý Fusion clip |

### 3.4 `TimelineItem.GetProperty` — key README

Toàn bộ key được liệt kê là **video transform / composite**: `Pan`, `Tilt`, `ZoomX/Y`, `ZoomGang`, `RotationAngle`, `AnchorPointX/Y`, `Pitch`, `Yaw`, `FlipX`, `FlipY`, `Crop*`, `DynamicZoomEase`, `CompositeMode`, `Opacity`, `Distortion`, **`RetimeProcess`**, `MotionEstimation`, `Scaling`, `ResizeFilter`.

- `RetimeProcess` = *cách* nội suy (project / nearest / frame blend / optical flow) — **không** phải hệ số speed.
- `FlipX` / `FlipY` = lật hình, **không** phải reverse audio.
- **Không** có key README cho: clip speed %, reverse audio, Elastic Wave, clip FX Fairlight, slip.

### 3.5 `AppendToTimeline` clipInfo (README)

```
mediaPoolItem, startFrame, endFrame,
mediaType (1=video only, 2=audio only),
trackIndex, recordFrame
```

`ImportMedia([paths…])` hoặc `ImportMedia([{FilePath, StartIndex, EndIndex}])`.

`AddTrack("audio", "stereo")` — `subTrackType` mặc định là `"mono"` nếu bỏ qua.

### 3.6 Audio mapping (README)

JSON giống nhau cho `GetAudioMapping` / `GetSourceAudioChannelMapping`:

- `embedded_audio_channels`
- `linked_audio.{n}.{channels, offset, path}` — `offset` tính bằng **sample** (âm = file bắt đầu trễ; dương = digital black rồi mới tới sample 0)
- `track_mapping.{n}.{channel_idx, mute, type}` — `type` ∈ Stereo / 5.1 / 7.1 / …

`type` 5.1 / 7.1 / adaptive > 2 ch → reject matrix (không downmix).

---

## 4. Reject matrix — tên đã biết / chưa biết

| Điều kiện | Cách phát hiện **đã có tên API** | Còn thiếu (cần dump) |
| --- | --- | --- |
| Offline / không file | `GetClipProperty("File Path")` rỗng sau mọi fallback | key path khác nếu có trong snapshot |
| Sample rate nguồn | *hy vọng* `"Sample Rate"` | **xác nhận string** |
| >2 kênh | `GetSourceAudioChannelMapping` / `GetAudioMapping` → `type`, `channels` | — |
| Voice Isolation | `GetVoiceIsolationState()["isEnabled"]` (item + track, 20.1+) | — |
| Nested / Fusion / generator | `GetFusionCompCount()>0`; `CreateCompoundClip` tồn tại; không File Path | type clip string trong `GetClipProperty` |
| Slip | so `GetSourceStartTime`/`End` với `GetStart`/`GetDuration` khi speed=1; `linked_audio.offset` | ngưỡng “lạ” |
| Speed ≠ 100% | suy từ `duration_tl / ((t1-t0)*out_fps) ≠ 1` | **tên property speed** |
| Reverse | không có | dump `GetProperty` trên clip đảo |
| Elastic Wave | không có | dump live + Fairlight inspector |
| Clip FX (Fairlight) | không có stack API | dump / heuristic; Voice Isolation thì đã có |

v1: thiếu tên → **reject khi heuristic lệch**, không im lặng xử lý sai sync.

---

## 5. Đường selection khuyến nghị v1

Probe **runtime** `callable(timeline.GetSelectedClips)` — đừng chỉ tin chuỗi version. CHANGELOG trên máy này không ghi ngày thêm method; README 24 Jul 2026 thì có.

| Version | Primary | Fallback |
| --- | --- | --- |
| **21.0.4+** | `timeline.GetSelectedClips()` rồi **dedupe** | playhead + `GetLinkedItems` |
| **20.0 – 21.0.3** | **Không** giả định `GetSelectedClips` | `GetCurrentVideoItem()` + `GetLinkedItems`; hoặc audio track enabled dưới playhead; user tick trên panel |
| **< 20.0** (18.6 / 19.x) | **Không hỗ trợ** | — |
| MAS Resolve | **Không hỗ trợ** | — |

**Dedupe** (một job / một identity):

1. Group theo `GetLinkedItems()` (một group = một bộ A/V sync).
2. Trong group: **ưu tiên File Path của audio sibling** (`GetTrackTypeAndIndex()[0] == "audio"`).
3. Không có audio sibling (embedded): lấy File Path video + `-map 0:a:{stream}`.
4. Hai audio path khác nhau: một job / path, trừ khi user tick thêm.
5. Cấm iterate mọi audio item trên mọi track.

`MediaPool.GetSelectedClips()` là selection **bin** — không dùng làm selection timeline.

---

## 6. Appendix A — khóa `endFrame`

Công thức (implement: `shared/perfectvoice_time.py`):

```
H_left_actual  = min(H, max(0, t0))
H_right_actual = min(H, max(0, file_dur - t1))
src_in_sample  = round_half_up((t0 - H_left_actual)  * src_sr)
src_out_sample = round_half_up((t1 + H_right_actual) * src_sr)   # exclusive
handleStartFrm = round_half_up(H_left_actual * out_fps)
handleEndFrm   = round_half_up((H_left_actual + (t1 - t0)) * out_fps)  # exclusive
```

Fixture: `t0=0.2`, `H=0.5` → `H_left_actual=0.2`.

**Khóa place:**

| Field | Giá trị | Ghi chú |
| --- | --- | --- |
| `startFrame` | `handleStartFrm` | lưới **WAV output**, không phải `src_in_sample` |
| `endFrame` | `handleEndFrm - 1` | **inclusive** — khớp example BMD 0…23 |
| `recordFrame` | `originalItem.GetStart()` | frame timeline gốc |
| `mediaType` | `2` | audio only |

`round_half_up` ≠ `round()` của Python 3 (half-even). Drop-frame chỉ là nhãn timecode; toán dùng 30000/1001.

Test bắt buộc: 24000/1001, 24/1, 25/1, 30000/1001; clamp SOF (`t0 < H`); `src_in_sample` không bao giờ vào `startFrame`.

---

## 7. Script spike

| Script | Việc |
| --- | --- |
| `scripts/spikes/dump_resolve_selection.py` | Connect nếu Resolve chạy; dump version / settings / clip. Dedupe: một row / audio File Path khác nhau; path trùng → `suppressed_duplicate`. Không chạy → JSON README + exit 0 |
| `scripts/spikes/place_test_wav.py` | Tính clipInfo Appendix A. Khi connected: `out_fps` từ `project.GetSetting("timelineFrameRate")` (trừ khi `--fps-num`); `recordFrame` từ `GetSelectedClips` rồi playhead+linked, `GetStartFrame` chỉ là last resort + warning. WAV mặc định dài `N_out` @ 48 kHz; `--apply` từ chối nếu `endFrame` vượt media. |
| `scripts/spikes/resolve_connect.py` | Helper IPC |
| `shared/perfectvoice_time.py` | Appendix A thuần + `parse_timeline_frame_rate` (`23.976`→24000/1001, `29.97 DF`→30000/1001) |
| `tests/unit/test_perfectvoice_time.py` | `python3 -m unittest discover -s tests/unit -v` |
| `tests/unit/test_selection_dedupe.py` | Dual-system hai path = hai job; path trùng bị flag |

Cả hai script spike **exit 0** khi Resolve tắt — CI không đỏ vì host vắng.

---

## 8. Việc **không** làm ở PR này

- Không sửa `docs/design.md` (chưa có bằng chứng live trái design).
- Không pin `"Sample Rate"` vào schema (PR 01) cho tới dump.
- Không claim đã chạy `GetSelectedClips()` trên 21.0.4 — chỉ claim README có method và app 21.0.4 đã cài.
