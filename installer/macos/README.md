# macOS installer (PR 11)

`.pkg` user-space cho panel + engine. Entitlement engine lấy từ spike **00c**: [`entitlements-engine.plist`](entitlements-engine.plist).

**Máy này không có Developer ID Application / Installer.** Không chạy `notarytool submit`. Thiếu chứng chỉ **không** làm fail build.

## Path (đóng băng §3.8)

| | User-space (mặc định) |
| --- | --- |
| Engine | `~/Library/Application Support/PerfectVoice/engine/perfectvoice-engine` |
| Panel | `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins/com.perfectvoice.panel/` |

Spike 00c xác nhận Resolve 21 **scan** plugin user-space. Script **từ chối** `--system` / `/Library` (tránh đè Audiio / GrokDavinci / SamplePlugin).

Fallback `/Library/Application Support/PerfectVoice/engine/perfectvoice-engine` vẫn được panel đọc nếu file tồn tại — installer **không** đặt file đó.

## Payload

- **Engine:** `hello-engine` (Mach-O, contract spawn / `READY` / `GET /v1/health`). **Không** phải Demucs. Production engine là **PyInstaller onedir** — build sau bằng `--engine-dir DIR`.
- **Không** bundle official `htdemucs*` / `.th` / `.safetensors`. User click *Download model* (PR 15).
- **Không** yêu cầu user Python / conda.
- **Không** bundle `WorkflowIntegration.node` (copy từ Resolve đang cài lúc postinstall).
- Codesign engine (khi có identity) dùng entitlement 00c: `cs.disable-library-validation`, `cs.allow-jit`, `cs.allow-unsigned-executable-memory`, `network.client`.

## Lệnh

```bash
# smoke: stage + pkgbuild + productbuild trong temp, xóa, exit 0
bash installer/macos/build-pkg.sh --dry-run

# ghi unsigned product pkg
bash installer/macos/build-pkg.sh
# → installer/macos/dist/PerfectVoice-0.1.0-arm64.pkg

# cài user-space ngay (rsync, không sudo, không Installer.app)
bash installer/macos/install-user.sh
# tương đương: bash installer/macos/build-pkg.sh --install

# khi đã có PyInstaller onedir
bash installer/macos/build-pkg.sh --engine-dir /path/to/onedir
```

`--sign` chỉ ký nếu tìm thấy `Developer ID Application`. Không có thì in cảnh báo và **vẫn** ghi pkg unsigned.

`--sign-dev` dùng `Apple Development` (máy dev). Gatekeeper reject; spawn local vẫn OK (00c). **Không** notarize bằng identity này.

## Notarize — **khi đã có** Developer ID + credential

Không chạy trên máy này. Copy khi team có identity:

```
security find-identity -v -p codesigning
# kỳ vọng:
#   "Developer ID Application: <Legal Name> (<TEAMID>)"
#   "Developer ID Installer: <Legal Name> (<TEAMID>)"
```

### 1. Deep-sign onedir (từ trong ra) + entitlement 00c

Cùng lệnh spike [`docs/spikes/notarize.md`](../../docs/spikes/notarize.md) §8 — tóm tắt:

```bash
ID="Developer ID Application: <Legal Name> (<TEAMID>)"
ENT=installer/macos/entitlements-engine.plist
ONEDIR="$HOME/Library/Application Support/PerfectVoice/engine"

find "$ONEDIR" -type f \( -name '*.dylib' -o -name '*.so' -o -name '*.so.*' \) -print0 \
  | xargs -0 -n 1 codesign --force --options runtime --timestamp --sign "$ID"

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

Hoặc build onedir ở chỗ khác rồi `build-pkg.sh --engine-dir … --sign` (script gọi `codesign` với cùng `$ENT` khi có Developer ID).

### 2. Ký product `.pkg` bằng Developer ID Installer

```bash
ID_INST="Developer ID Installer: <Legal Name> (<TEAMID>)"
PKG=installer/macos/dist/PerfectVoice-0.1.0-arm64.pkg

# build unsigned trước, rồi:
productsign --sign "$ID_INST" "$PKG" "${PKG%.pkg}-signed.pkg"
```

`productbuild --sign "$ID_INST"` cũng được nếu gọi tay (script mặc định **không** ký product — tránh fail khi thiếu identity).

### 3. Submit + staple (không chạy ở đây)

```bash
xcrun notarytool store-credentials "perfectvoice-notary" \
  --apple-id "$APPLE_ID" \
  --team-id "$TEAM_ID" \
  --password "$APP_SPECIFIC_PASSWORD"

xcrun notarytool submit installer/macos/dist/PerfectVoice-0.1.0-arm64.pkg \
  --keychain-profile "perfectvoice-notary" \
  --wait

xcrun stapler staple installer/macos/dist/PerfectVoice-0.1.0-arm64.pkg
spctl --assess --type install --verbose installer/macos/dist/PerfectVoice-0.1.0-arm64.pkg
```

**Không** dùng *Apple Development* cho bước 2–3 (notary từ chối). Máy đo 00c chỉ có `Apple Development: kenwin1608@icloud.com (A72Y573S46)` / team `TUDKNKV6KD`.

Zip onedir (`ditto -c -k`) chỉ để notary engine lẻ — **không staple được zip**. Ship user thì `.pkg` + staple như trên.

## Cài / gỡ

```bash
bash installer/macos/install-user.sh
# hoặc, khi pkg đã currentUserHome + auth=none:
# installer -pkg installer/macos/dist/PerfectVoice-0.1.0-arm64.pkg \
#   -target CurrentUserHomeDirectory
```

Gỡ: xóa hai thư mục user-space ở bảng path. Giữ `~/Library/Application Support/PerfectVoice/models/` và cache trừ khi muốn xóa hết.
