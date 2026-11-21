# ゆいとさんのアプリ置き場

Claude が作ったアプリの本体をここにまとめる。どのチャットからでも、ここを直せば全アプリを直せる。
鍵・トークン・パスワード類は入れない。

| フォルダ | アプリ | 動く場所 |
| --- | --- | --- |
| light-operator | Light Operator（サーバー・操作画面） | Mac：~/Applications/.lightoperator/engine |
| ch-cut | CH Cut | Mac：~/Applications/CHCut |
| ara-safety | ARA安全装置 | Mac：~/Applications/ARA安全装置.app |
| xfile-sync | X-FILE 同期 | Mac：~/Applications/X-FILE 同期.app |
| x-file | X-FILE | Cloudflare Workers（未移行） |

別のリポジトリにあるもの：dramatic-simulator、aigis-script、uketsuke-keikaku、serifu-take。Alien Lab は Claude 上のページ。

## 反映のしかた

- Mac のアプリ：main に送信すると、Mac の mac-updater/update.sh（~/Applications/.apps-updater）が5分以内に取り込む。
  Light Operator の見張り役（supervisor.sh）から5分ごとに呼ばれる。中身が違うファイルだけ上書きし、消すことはしない。
  supervisor.sh・鍵・CH Cut のプリセットは自動では入れ替えない。記録は ~/Applications/.apps-updater/update.log。
- Light Operator のサーバーは、server.js / server.py が変わると自動で入れ替わる。画面（public）は次に開いたときから。
- CH Cut・ARA安全装置・X-FILE 同期は、次に開いたときから新しい版。

作り方の決まりごとは my-app-style スキルに従う。
