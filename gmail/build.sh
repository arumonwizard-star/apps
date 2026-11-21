#!/bin/bash
# Gmail.app を作る（GitHub Actions の Mac で動く。手元の Mac でも可）
#   使い方: gmail/build.sh <版の番号>
set -euo pipefail
cd "$(dirname "$0")"
BUILD="${1:-0}"
APP="Gmail.app"
rm -rf "$APP" build && mkdir -p build "$APP/Contents/MacOS" "$APP/Contents/Resources"
for arch in arm64 x86_64; do
  swiftc -O -swift-version 5 -target "$arch-apple-macos13.0" src/main.swift -o "build/Gmail-$arch" \
    -framework Cocoa -framework WebKit -framework UserNotifications
done
lipo -create build/Gmail-arm64 build/Gmail-x86_64 -output "$APP/Contents/MacOS/Gmail"
sed "s/__BUILD__/$BUILD/g" Info.plist > "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"
# 実行ファイルだけに署名する（アイコンは Mac 側で後から入るため、中身全体は封じない）
codesign --force -s - --identifier jp.yuito.gmail "$APP/Contents/MacOS/Gmail"
rm -rf build
echo "作成: $APP（版 1.$BUILD）"
