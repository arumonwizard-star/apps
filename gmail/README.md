# Gmail（Mac）

Gmail を普通の Mac アプリとして使うためのもの。

- ウインドウを閉じても裏で受信を続ける（⌘Q で終了）。
- Dock のアイコンに受信トレイの未読数。新着はバナーで通知し、押すとそのメールを表示。
- ログイン時に画面を出さずに起動（~/Library/LaunchAgents/jp.yuito.gmail.login.plist）。
- アイコンは以前の Safari 版 Gmail のものを引き継ぐ（mac-updater が入れる）。

## 反映のしかた
1. `gmail/src/main.swift` を直して main に送信する。
2. GitHub Actions「Gmail を作成」が Mac で `gmail/build.sh` を動かし、`gmail/Gmail.app` をリポジトリに入れる。
3. Mac の mac-updater が5分以内に ~/Applications/Gmail.app へ反映。アプリは画面を閉じている間に新しい版へ入れ替わる。

初回だけ、以前の Safari 版 Gmail.app を ~/Applications/.apps-updater/old/Gmail-safari.app に移し、Dock の Gmail を新しいほうへ向け直す。

## テスト
- Actions の「確認」で、実行ファイル（arm64/x86_64）と Info.plist を確認する。
- 動作記録：Mac の ~/Library/Logs/Gmail.log
