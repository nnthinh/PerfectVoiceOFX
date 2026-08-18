# Spike: Developer ID + spawn onedir từ Workflow Integration

| Trường | Giá trị |
| --- | --- |
| **PR** | 00c — `spike(macos): Developer ID onedir spawned by Workflow Integration` |
| **Ngày đo** | 2026-08-18 |
| **Máy** | MacBook Pro, Darwin 25.6.0, **arm64** (T6000) |
| **OS** | macOS 26.6.1 (25G76), SIP **enabled** |
| **Host** | DaVinci Resolve **Studio standalone 21.0.4** (`21.0.40005`) tại `/Applications/DaVinci Resolve/DaVinci Resolve.app` |
| **Kết luận ngắn** | Spawn **unsigned** và **Apple Development + Hardened Runtime** của hello-engine **thành công** từ Node và từ **chính binary Electron của Resolve**. Máy **không có Developer ID Application** → **không notarize**. Không resign Resolve. |

Đây **không** phải chứng minh PyTorch onedir đã ship. Hello-engine không load torch. Mục tiêu: đóng băng contract §3.8, đo codesign máy này, ghi blocker notarize.

## 1. Kết luận (đọc trước)

1. **Không có Developer ID trên máy này.** `security find-identity -v -p codesigning` chỉ ra một identity: `Apple Development: kenwin1608@icloud.com (A72Y573S46)` (Team `TUDKNKV6KD`). Không `Developer ID Application`, không `Developer ID Installer`. **Không chạy `notarytool submit`.**
2. **Spawn absolute-path hello-engine không bị EPERM** khi parent là:
   - Node v22.23.1
   - Electron 36.3.2 **của Resolve** (`ELECTRON_RUN_AS_NODE=1`) — cùng Mach-O Hardened Runtime sẽ host WI panel
3. **Studio standalone, không Mac App Store** (không receipt). App chính Resolve ký **adhoc**, TeamIdentifier **not set**.
4. **WI Electron** (process thật sự spawn sidecar) lại là **Developer ID Application: Blackmagic Design Inc (`9ZGFBWLSYP`)** + **Hardened Runtime**, entitlement **chỉ** `com.apple.security.cs.allow-jit`. **Không** `app-sandbox`. **Không** `disable-library-validation`.
5. Hệ quả: **spawn process con** (hợp đồng §3.8) được phép. **`dlopen` / load `.node` / dylib của PerfectVoice vào process Electron** sẽ đụng library validation (team id khác BMD). Đúng hướng: sidecar out-of-process. **Không** được re-sign Resolve.
6. `spctl --assess` **reject** hello-engine ký Apple Development (đúng: không phải Developer ID + notarize). Spawn từ parent đã chạy **vẫn OK** vì file local, không quarantine, không mở từ Finder.
7. Blocker còn lại cho **PyTorch onedir notarized**: chứng chỉ Developer ID + credential notary + deep-sign toàn bộ dylib + (khi phân phối) staple trên `.dmg`/`.pkg`. Entitlement engine đã ghi sẵn tại [`installer/macos/entitlements-engine.plist`](../../installer/macos/entitlements-engine.plist).

**Fail closed:** nếu `spawn` trả `EPERM` / `EACCES`, panel/script in copy English *“Cannot start engine (spawn blocked or not installed). Need Studio standalone + a codesigned engine.”* rồi thoát ≠ 0. Trên máy này **không** kích hoạt — spawn thành công.

## 2. Identities (`security find-identity`)

```
$ security find-identity -v -p codesigning
  1) BE34DDAC96D2E16949A0A2229A981EB33E08616C "Apple Development: kenwin1608@icloud.com (A72Y573S46)"
     1 valid identities found
```

- Subject: `Apple Development: kenwin1608@icloud.com (A72Y573S46)`
- Org / team: `Win Ken` / `TUDKNKV6KD`
- Hiệu lực: 2026-03-13 → 2027-03-13
- Keychain có *Developer ID Certification Authority* (Apple root) — **không** có leaf *Developer ID Application* của team PerfectVoice.

Apple Development **không** dùng để notarize hay phân phối ngoài máy dev.

## 3. Codesign Resolve / Electron WI

| Binary | Identifier | Signature | Hardened Runtime | Team | Entitlements |
| --- | --- | --- | --- | --- | --- |
| `DaVinci Resolve.app` | `com.blackmagic-design.DaVinciResolve` | **adhoc** (`flags=0x2`) | không | not set | (trống) |
| `…/.hidden/Electron.app` (WI host, Electron **36.3.2**) | `com.github.Electron` | Developer ID Application: Blackmagic Design Inc | **có** (`0x10000`) | `9ZGFBWLSYP` | `cs.allow-jit` |
| Electron Helper | `com.github.Electron.helper` | cùng BMD | có | `9ZGFBWLSYP` | `cs.allow-jit` |
| DaVinci Resolve Helper | `com.blackmagic-design.DaVinciResolveHelper` | cùng BMD | có | `9ZGFBWLSYP` | `cs.allow-unsigned-executable-memory` + **`cs.disable-library-validation`** |
| Resolve Web Helper | `com.blackmagic-design.resolve.webhelper` | cùng BMD | có | `9ZGFBWLSYP` | jit + unsigned-exec-mem + **disable-library-validation** |
| `WorkflowIntegration.node` (SamplePlugin) | `com.blackmagic-design.WorkflowIntegration` | cùng BMD | có | `9ZGFBWLSYP` | (Mach-O .node, universal x86_64+arm64) |

`spctl --assess` trên `DaVinci Resolve.app`: **rejected** (adhoc). App vẫn chạy vì được cài từ installer Blackmagic, không phải Gatekeeper “open downloaded app”.

`kMDItemAppStoreHasReceipt` = null → **standalone**, không MAS.

Design §3.8 nói “parent Resolve có Hardened Runtime / library validation → child khác signature có thể EPERM”. **Nửa đúng:**

- Process **WI** (Electron) *có* Hardened Runtime.
- Library validation chặn **load in-process**, không chặn `posix_spawn`/`execve` của binary tuyệt đối ngoài bundle.
- Electron **không sandbox** → không có MAC deny `process-exec`.
- MAS Resolve (không có trên máy) mới là kịch bản spawn sidecar gần như chắc EPERM.

Audiio (plugin đã cài) cũng `require("child_process").spawn` helper `/bin/bash` — tiền lệ spawn từ WI trên Studio standalone.

## 4. Hello-engine (artifact spike)

| File | Vai trò |
| --- | --- |
| [`scripts/spikes/hello-engine.c`](../../scripts/spikes/hello-engine.c) | Binary Mach-O, contract §3.8 |
| [`scripts/spikes/hello-engine.py`](../../scripts/spikes/hello-engine.py) | Cùng protocol, fallback Python 3 |
| [`scripts/spikes/build-hello-engine.sh`](../../scripts/spikes/build-hello-engine.sh) | `clang`; `--sign-dev` (Apple Development, **không** notarize) |
| [`scripts/spikes/spawn-hello-engine.js`](../../scripts/spikes/spawn-hello-engine.js) | `child_process.spawn` đúng shape panel |
| [`scripts/spikes/hello-wi-panel/`](../../scripts/spikes/hello-wi-panel/) | Panel WI tối thiểu (không commit `.node`) |
| [`installer/macos/entitlements-engine.plist`](../../installer/macos/entitlements-engine.plist) | Entitlement **engine** cho onedir sau này |

Contract đã triển khai:

```
spawn(absEnginePath,
      ["serve", "--bind", "127.0.0.1", "--port", "0", "--token-file", tokenPath],
      { cwd: engineDir })
```

- Absolute path only. `PATH` của child bị xóa.
- Token file `0600` dưới `~/Library/Application Support/PerfectVoice/run/<uuid>.token` (256-bit hex). Engine đọc rồi **unlink**.
- Bind **chỉ** `127.0.0.1`. `0.0.0.0` → exit 1.
- `--token-fd` → exit 1 (cấm, không portable Win32).
- stdout: `READY http://127.0.0.1:<port>`
- `GET /v1/health` + `Authorization: Bearer <token>` → `{"ok": true, "protocol_version": 1}`
- Thiếu Bearer → 401.

`enginePath` (cùng rule §3.8): `PERFECTVOICE_ENGINE` (absolute + tồn tại) → `~/Library/Application Support/PerfectVoice/engine/perfectvoice-engine` → `/Library/Application Support/PerfectVoice/engine/perfectvoice-engine`. Spike script thêm fallback `scripts/spikes/hello-engine` rồi `.py`.

## 5. Kết quả đo trên máy này

### 5.1 Unsigned (adhoc, linker-signed)

```
Format=Mach-O thin (arm64)
flags=0x20002(adhoc,linker-signed)
TeamIdentifier=not set
```

```
$ node scripts/spikes/spawn-hello-engine.js
spawn …/scripts/spikes/hello-engine serve --bind 127.0.0.1 --port 0 --token-file …/run/<uuid>.token
ready http://127.0.0.1:63311
health 200 {"ok": true, "protocol_version": 1}
ok unsigned-or-local spawn + /v1/health
```

Cùng lệnh qua Python fallback (giấu Mach-O): `health 200`, token unlink.

### 5.2 Parent = Electron Resolve (Hardened Runtime)

```
ELECTRON_RUN_AS_NODE=1 \
  "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Applications/.hidden/Electron.app/Contents/MacOS/Electron" \
  scripts/spikes/spawn-hello-engine.js
```

Kết quả **giống hệt**: READY + `health 200`. **Không EPERM.** Đây là bằng chứng mạnh nhất spike này có thể lấy **mà không** cài panel vào `/Library` và không mở UI Resolve.

`ELECTRON_RUN_AS_NODE=1` dùng **cùng** Mach-O, cùng Hardened Runtime, cùng entitlement `allow-jit`. Khác với session WI đầy đủ: không load `WorkflowIntegration.node`, không mở BrowserWindow. API `child_process.spawn` là một. Chưa click *Workspace → Workflow Integrations* trên Studio đang chạy.

### 5.3 Ký Apple Development + entitlements engine

```
$ bash scripts/spikes/build-hello-engine.sh --sign-dev
Authority=Apple Development: kenwin1608@icloud.com (A72Y573S46)
TeamIdentifier=TUDKNKV6KD
flags=0x10000(runtime)
```

Entitlement gắn đúng: `cs.disable-library-validation`, `cs.allow-jit`, `cs.allow-unsigned-executable-memory`, `network.client`.

Spawn từ Node **và** từ Electron Resolve: lại `health 200`. Không EPERM dù **team id con (`TUDKNKV6KD`) ≠ team id parent (`9ZGFBWLSYP`)**.

```
$ spctl --assess --type execute --verbose scripts/spikes/hello-engine
scripts/spikes/hello-engine: rejected
```

Gatekeeper reject (không Developer ID / không notarize) **không** chặn spawn từ process đã chạy.

### 5.4 HTTP / token

| Kiểm | Kết quả |
| --- | --- |
| `GET /v1/health` không Bearer | `401 {"ok":false,"error":"unauthorized"}` |
| `GET /v1/health` + Bearer đúng | `200 {"ok": true, "protocol_version": 1}` |
| Token file sau READY | đã unlink |
| `--bind 0.0.0.0` | `bind must be 127.0.0.1` (exit 1) |
| `--token-fd` | từ chối (exit 1) |

## 6. Hardened Runtime — ghi chú cho onedir PyTorch

Trên **engine** (không phải Resolve):

| Entitlement | Lý do |
| --- | --- |
| `com.apple.security.cs.disable-library-validation` | dylib PyTorch / PyInstaller không cùng Team ID, nhiều file adhoc |
| `com.apple.security.cs.allow-jit` | libtorch JIT / codegen |
| `com.apple.security.cs.allow-unsigned-executable-memory` | cùng lý do JIT |
| `com.apple.security.network.client` | HTTPS tải weight sau *Download model* (sandbox-style; no-op nếu không bật `app-sandbox`) |

**Không** bật mặc định `cs.allow-dyld-environment-variables` (nới mặt tấn công). Chỉ thêm nếu PyInstaller onedir thật sự cần.

**Không** bật `app-sandbox` trên engine — sidecar cần đọc media user (`allowed_roots[]`), bind localhost, không sống trong container MAS.

Parent Electron **thiếu** `disable-library-validation` → cấm nhét torch vào process WI. Helper “DaVinci Resolve Helper” *có* entitlement đó nhưng không phải process panel.

Cùng Team ID với BMD (`9ZGFBWLSYP`) **không khả thi**. Spawn + HTTP localhost là isolation đúng.

## 7. Cài hello WI plugin (không đụng `/Library`)

**Không** cài spike vào `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins/` — thư mục đó đang có **Audiio**, **GrokDavinci**, **SamplePlugin** (root:staff).

Resolve 21 trên máy này **cũng** đọc plugin user-space:

```
~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins/
```

(đã thấy bản copy Audiio / GrokDavinci). Lệnh an toàn, không sudo:

```bash
# từ root repo
bash scripts/spikes/install-hello-wi-panel.sh
```

Script copy `scripts/spikes/hello-wi-panel/` → `…/PerfectVoiceHelloSpike/` và lấy `WorkflowIntegration.node` từ Developer Examples (không commit `.node` — xem `docs/licenses/blackmagic.md`). `install-hello-wi-panel.sh --system` **từ chối** có chủ đích.

Sau đó: restart Studio → **Workspace → Workflow Integrations → PerfectVoice Hello Spike** → *Spawn hello-engine*.

Cần Mach-O `scripts/spikes/hello-engine` (chạy `build-hello-engine.sh`) **hoặc** đặt binary vào `~/Library/Application Support/PerfectVoice/engine/perfectvoice-engine` / `PERFECTVOICE_ENGINE`. Panel resolve path theo §3.8; khi chạy *trong* Resolve, fallback `../hello-engine` chỉ đúng nếu cài cạnh `scripts/spikes/` (dev). User-space copy nên set `PERFECTVOICE_ENGINE` hoặc thả binary vào `~/Library/Application Support/PerfectVoice/engine/perfectvoice-engine`.

Chưa chạy install trên máy spike này (tránh thêm plugin vào menu Resolve của user). Spawn đã chứng minh bằng Electron binary của Resolve.

### Cài tay vào `/Library` (không khuyến nghị)

Chỉ khi chấp nhận sudo và **thư mục mới** `PerfectVoiceHelloSpike` (không đè plugin cũ):

```bash
DST="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins/PerfectVoiceHelloSpike"
sudo mkdir -p "$DST"
sudo rsync -a --exclude WorkflowIntegration.node scripts/spikes/hello-wi-panel/ "$DST/"
sudo cp "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Workflow Integrations/Examples/SamplePlugin/WorkflowIntegration.node" "$DST/"
```

## 8. Lệnh notarize — **khi đã có** Developer ID + credential

Không chạy trên máy này. Copy khi team có:

1. Identity xuất hiện:

   ```bash
   security find-identity -v -p codesigning
   # kỳ vọng: "Developer ID Application: <Legal Name> (<TEAMID>)"
   ```

2. Deep-sign onedir **từ trong ra** (dylib / `.so` trước, binary ngoài cùng sau), Hardened Runtime + entitlement engine:

   ```bash
   ID="Developer ID Application: <Legal Name> (<TEAMID>)"
   ENT=installer/macos/entitlements-engine.plist
   ONEDIR="$HOME/Library/Application Support/PerfectVoice/engine"

   find "$ONEDIR" -type f \( -name '*.dylib' -o -name '*.so' -o -name '*.so.*' \) -print0 \
     | xargs -0 -n 1 codesign --force --options runtime --timestamp --sign "$ID"

   # Mach-O executable trong onedir (trừ binary entry)
   find "$ONEDIR" -type f -perm +111 ! -name 'perfectvoice-engine' -print0 \
     | while IFS= read -r -d '' f; do
         file "$f" | grep -q 'Mach-O' || continue
         codesign --force --options runtime --timestamp --sign "$ID" "$f"
       done

   codesign --force --options runtime --timestamp \
     --entitlements "$ENT" --sign "$ID" \
     "$ONEDIR/perfectvoice-engine"

   codesign --verify --verbose=2 "$ONEDIR/perfectvoice-engine"
   ```

3. Gói zip (notary nhận zip; **không staple được zip**):

   ```bash
   ditto -c -k --keepParent "$ONEDIR" /tmp/perfectvoice-engine.zip
   ```

4. Submit. Ưu tiên keychain profile (không nhét password vào argv lâu):

   ```bash
   xcrun notarytool store-credentials "perfectvoice-notary" \
     --apple-id "$APPLE_ID" \
     --team-id "$TEAM_ID" \
     --password "$APP_SPECIFIC_PASSWORD"

   xcrun notarytool submit /tmp/perfectvoice-engine.zip \
     --keychain-profile "perfectvoice-notary" \
     --wait
   ```

   Tương đương một lần: `--apple-id` / `--team-id` / `--password`. Máy này có `xcrun notarytool` (Xcode). **Không** gọi submit.

5. Phân phối qua `.dmg` hoặc `.pkg` rồi staple:

   ```bash
   xcrun stapler staple PerfectVoiceEngine.dmg
   spctl --assess --type execute --verbose "$ONEDIR/perfectvoice-engine"
   ```

6. Quarantine sau download: `xattr -d com.apple.quarantine` chỉ là workaround dev. Ship thì notarize + staple.

**Không** dùng identity *Apple Development* cho bước 2–5 (Gatekeeper reject, notary từ chối).

## 9. Việc còn chặn PyTorch onedir notarized

| Blocker | Mức | Ghi chú |
| --- | --- | --- |
| Chưa có **Developer ID Application** (+ Installer nếu ship `.pkg`) | **Chặn notarize** | Chỉ Apple Development trên máy đo |
| Chưa có Apple ID / Team ID / app-specific password / notary profile | **Chặn submit** | Không đoán credential |
| Chưa build PyInstaller onedir + torch | Chưa đo | Hello-engine cố ý không đợi Demucs |
| Deep-sign hàng trăm dylib torch (adhoc / team khác) | Rủi ro build | Cần `disable-library-validation` trên **engine** (plist đã có) |
| JIT / unsigned executable memory | Rủi ro runtime | Entitlement đã khai |
| Engine tải về bị quarantine + `spctl` reject | Rủi ro UX | Hết khi notarize+staple; spawn local unsigned *đã* OK |
| EPERM spawn từ WI Electron standalone | **Không thấy** trên 21.0.4 | Vẫn fail closed trong code |
| MAS Resolve | Ngoài scope v1 | Không cài trên máy |
| Cùng team id BMD | Không làm được | Không cần nếu chỉ spawn |
| Resign Resolve / Electron | **Cấm** | Phá chữ ký BMD, Gatekeeper, cập nhật |

## 10. Cách chạy lại spike

```bash
bash scripts/spikes/build-hello-engine.sh          # unsigned Mach-O
# bash scripts/spikes/build-hello-engine.sh --sign-dev
node scripts/spikes/spawn-hello-engine.js

ELECTRON_RUN_AS_NODE=1 \
  "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Applications/.hidden/Electron.app/Contents/MacOS/Electron" \
  scripts/spikes/spawn-hello-engine.js
```

Mach-O `scripts/spikes/hello-engine` **không** commit (`.gitignore`).
