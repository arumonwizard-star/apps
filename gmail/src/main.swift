// Gmail（Mac）
// ・ウインドウを閉じても裏で受信を続ける（⌘Q で終了するまで）
// ・Dock のアイコンに未読の数、新着はバナーで通知、押すとそのメールを表示
// ・ログイン時は画面を出さずに起動（--background）
import Cocoa
import WebKit
import UserNotifications
import ImageIO
import UniformTypeIdentifiers

let appName = "Gmail"
let bundleID = "jp.yuito.gmail"
let home = FileManager.default.homeDirectoryForCurrentUser
let homeURL = URL(string: "https://mail.google.com/mail/u/0/#inbox")!
let safariUA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.1 Safari/605.1.15"
let logURL = home.appendingPathComponent("Library/Logs/Gmail.log")
let oldSafariApp = home.appendingPathComponent("Applications/.apps-updater/old/Gmail-safari.app")

func log(_ s: String) {
    let f = DateFormatter(); f.dateFormat = "MM/dd HH:mm:ss"
    let line = "[\(f.string(from: Date()))] \(s)\n"
    if let h = try? FileHandle(forWritingTo: logURL) {
        h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); try? h.close()
    } else {
        try? FileManager.default.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? line.write(to: logURL, atomically: true, encoding: .utf8)
    }
}

// ページに入れる部品：通知の受け口と、未読の確認
let pageScript = """
(function(){
  const post = (m) => { try { window.webkit.messageHandlers.gm.postMessage(m); } catch(e){} };
  class GN {
    constructor(title, o){ o = o || {}; this.title = title; this.body = o.body || ''; this.tag = o.tag || ''; this.onclick = null; this.onclose = null;
      post({type:'notify', title: String(title||''), body: String(this.body||''), tag: String(this.tag||'')}); }
    close(){} addEventListener(){} removeEventListener(){}
    static requestPermission(cb){ if (cb) { try { cb('granted'); } catch(e){} } return Promise.resolve('granted'); }
  }
  Object.defineProperty(GN, 'permission', { get: () => 'granted' });
  window.Notification = GN;
  try {
    const q = navigator.permissions && navigator.permissions.query && navigator.permissions.query.bind(navigator.permissions);
    if (q) navigator.permissions.query = (d) => (d && d.name === 'notifications') ? Promise.resolve({state:'granted', onchange:null}) : q(d);
  } catch(e){}
  window.__gmCheck = async function(){
    try {
      const m = location.pathname.match(/\\/mail\\/u\\/(\\d+)/); const u = m ? m[1] : '0';
      const r = await fetch('/mail/u/' + u + '/feed/atom', {credentials:'include', cache:'no-store'});
      if (!r.ok) { post({type:'feed', ok:false, error:'status ' + r.status}); return; }
      const d = new DOMParser().parseFromString(await r.text(), 'application/xml');
      const fc = d.querySelector('fullcount');
      if (!fc) { post({type:'feed', ok:false, error:'no fullcount'}); return; }
      const tx = (e, s) => { const x = e.querySelector(s); return x ? (x.textContent || '') : ''; };
      const entries = Array.from(d.querySelectorAll('entry')).slice(0, 20).map(e => {
        const l = e.querySelector('link');
        return { id: tx(e,'id'), title: tx(e,'title'), summary: tx(e,'summary'), author: tx(e,'author name'), link: l ? (l.getAttribute('href')||'') : '' };
      });
      post({type:'feed', ok:true, count: parseInt(fc.textContent) || 0, entries: entries});
    } catch(e) { post({type:'feed', ok:false, error:String(e)}); }
  };
})();
"""

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate,
                         WKScriptMessageHandler, UNUserNotificationCenterDelegate, WKDownloadDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var popupCatchers: [WKWebView] = []
    var activity: NSObjectProtocol?
    var titleObs: NSKeyValueObservation?
    var loadObs: NSKeyValueObservation?
    var progObs: NSKeyValueObservation?
    var bar = NSView(), hud = NSVisualEffectView(), spinner = NSProgressIndicator()
    var hudTimer: Timer?

    let runningVersion = (Bundle.main.infoDictionary?["CFBundleVersion"] as? String) ?? "0"
    var feedOK = false, lastFeedOK = Date.distantPast, firstFeed = true
    var titleCount = -1, lastBridgeNotify = Date.distantPast
    var seen: [String] = UserDefaults.standard.stringArray(forKey: "seen") ?? []
    var updatePendingSince: Date?
    var downloadDest: [ObjectIdentifier: URL] = [:]

    // MARK: 起動
    func applicationDidFinishLaunching(_ n: Notification) {
        let background = CommandLine.arguments.contains("--background")
        log("起動 版\(runningVersion)\(background ? "（ログイン時）" : "")")
        NSApp.appearance = NSAppearance(named: .darkAqua)
        setupIcon()
        buildMenu()
        buildWindow()
        activity = ProcessInfo.processInfo.beginActivity(options: [.userInitiatedAllowingIdleSystemSleep], reason: "受信の確認")

        let c = UNUserNotificationCenter.current()
        c.delegate = self
        c.requestAuthorization(options: [.alert, .sound, .badge]) { ok, err in
            log("通知の許可: \(ok)\(err.map { " \($0)" } ?? "")")
        }
        installLoginAgent()

        webView.load(URLRequest(url: homeURL))
        if !background { showWindow() }

        Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in self?.checkFeed() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 12) { [weak self] in self?.checkFeed() }
        Timer.scheduledTimer(withTimeInterval: 120, repeats: true) { [weak self] _ in self?.checkUpdate() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool {
        showWindow(); return true
    }

    func showWindow() {
        NSApp.unhide(nil)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func windowDidBecomeKey(_ n: Notification) { checkFeed() }

    func windowShouldClose(_ w: NSWindow) -> Bool {
        w.orderOut(nil)   // 閉じても裏で受信を続ける
        return false
    }

    // MARK: 画面
    func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = appName
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.minSize = NSSize(width: 600, height: 400)
        window.center()
        window.setFrameAutosaveName("GmailMain")
        window.backgroundColor = NSColor(white: 0.12, alpha: 1)

        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()
        cfg.userContentController.addUserScript(WKUserScript(source: pageScript, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        cfg.userContentController.add(self, name: "gm")
        cfg.preferences.javaScriptCanOpenWindowsAutomatically = true
        webView = WKWebView(frame: .zero, configuration: cfg)
        webView.customUserAgent = safariUA
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.underPageBackgroundColor = NSColor(white: 0.12, alpha: 1)
        // 隠れていても止まらないようにする
        if webView.responds(to: NSSelectorFromString("_setWindowOcclusionDetectionEnabled:")) {
            webView.setValue(false, forKey: "windowOcclusionDetectionEnabled")
        }

        let root = NSView()
        root.wantsLayer = true
        window.contentView = root
        webView.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            webView.topAnchor.constraint(equalTo: root.safeAreaLayoutGuide.topAnchor),
        ])

        // 読み込み中：上端の細いバー＋上部中央の表示
        bar.wantsLayer = true
        bar.layer?.backgroundColor = NSColor.systemBlue.cgColor
        bar.layer?.anchorPoint = .zero
        bar.translatesAutoresizingMaskIntoConstraints = false
        bar.alphaValue = 0
        root.addSubview(bar)
        NSLayoutConstraint.activate([
            bar.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            bar.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            bar.topAnchor.constraint(equalTo: webView.topAnchor),
            bar.heightAnchor.constraint(equalToConstant: 2),
        ])
        hud.material = .hudWindow; hud.state = .active; hud.blendingMode = .withinWindow
        hud.wantsLayer = true; hud.layer?.cornerRadius = 14
        hud.translatesAutoresizingMaskIntoConstraints = false
        hud.alphaValue = 0
        spinner.style = .spinning; spinner.controlSize = .small
        spinner.translatesAutoresizingMaskIntoConstraints = false
        let label = NSTextField(labelWithString: "読み込み中")
        label.font = .systemFont(ofSize: 12, weight: .medium); label.textColor = .white
        label.translatesAutoresizingMaskIntoConstraints = false
        hud.addSubview(spinner); hud.addSubview(label)
        root.addSubview(hud)
        NSLayoutConstraint.activate([
            hud.centerXAnchor.constraint(equalTo: root.centerXAnchor),
            hud.topAnchor.constraint(equalTo: webView.topAnchor, constant: 10),
            hud.heightAnchor.constraint(equalToConstant: 28),
            spinner.leadingAnchor.constraint(equalTo: hud.leadingAnchor, constant: 10),
            spinner.centerYAnchor.constraint(equalTo: hud.centerYAnchor),
            label.leadingAnchor.constraint(equalTo: spinner.trailingAnchor, constant: 6),
            label.trailingAnchor.constraint(equalTo: hud.trailingAnchor, constant: -12),
            label.centerYAnchor.constraint(equalTo: hud.centerYAnchor),
        ])

        loadObs = webView.observe(\.isLoading, options: [.new]) { [weak self] wv, _ in self?.loadingChanged(wv.isLoading) }
        progObs = webView.observe(\.estimatedProgress, options: [.new]) { [weak self] wv, _ in
            guard let self = self else { return }
            let p = max(0.05, min(1, wv.estimatedProgress))
            self.bar.layer?.setAffineTransform(CGAffineTransform(scaleX: CGFloat(p), y: 1))
        }
        titleObs = webView.observe(\.title, options: [.new]) { [weak self] wv, _ in self?.titleChanged(wv.title ?? "") }
    }

    func loadingChanged(_ loading: Bool) {
        hudTimer?.invalidate()
        if loading {
            hudTimer = Timer.scheduledTimer(withTimeInterval: 0.15, repeats: false) { [weak self] _ in
                guard let self = self, self.webView.isLoading else { return }
                self.spinner.startAnimation(nil)
                NSAnimationContext.runAnimationGroup { c in c.duration = 0.15
                    self.bar.animator().alphaValue = 1; self.hud.animator().alphaValue = 1 }
            }
        } else {
            NSAnimationContext.runAnimationGroup({ c in c.duration = 0.2
                self.bar.animator().alphaValue = 0; self.hud.animator().alphaValue = 0
            }, completionHandler: { self.spinner.stopAnimation(nil) })
        }
    }

    // MARK: メニュー
    func buildMenu() {
        let main = NSMenu()
        func add(_ title: String, _ items: [NSMenuItem]) -> NSMenu {
            let top = NSMenuItem(); let m = NSMenu(title: title)
            items.forEach { m.addItem($0) }; top.submenu = m; main.addItem(top); return m
        }
        func item(_ t: String, _ a: Selector?, _ k: String = "", _ mods: NSEvent.ModifierFlags = [.command], target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: t, action: a, keyEquivalent: k); i.keyEquivalentModifierMask = mods; i.target = target; return i
        }
        _ = add(appName, [
            item("\(appName)について", #selector(NSApplication.orderFrontStandardAboutPanel(_:))),
            .separator(),
            item("\(appName)を非表示", #selector(NSApplication.hide(_:)), "h"),
            item("ほかを非表示", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            item("すべてを表示", #selector(NSApplication.unhideAllApplications(_:))),
            .separator(),
            item("\(appName)を終了", #selector(NSApplication.terminate(_:)), "q"),
        ])
        _ = add("編集", [
            item("取り消し", Selector(("undo:")), "z"),
            item("やり直し", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("カット", #selector(NSText.cut(_:)), "x"),
            item("コピー", #selector(NSText.copy(_:)), "c"),
            item("ペースト", #selector(NSText.paste(_:)), "v"),
            item("すべてを選択", #selector(NSText.selectAll(_:)), "a"),
        ])
        _ = add("表示", [
            item("受信トレイ", #selector(goInbox), "1", target: self),
            item("再読み込み", #selector(reload), "r", target: self),
            .separator(),
            item("拡大", #selector(zoomIn), "+", target: self),
            item("縮小", #selector(zoomOut), "-", target: self),
            item("実際のサイズ", #selector(zoomReset), "0", target: self),
            .separator(),
            item("前のページ", #selector(goBack), "[", target: self),
            item("次のページ", #selector(goForward), "]", target: self),
        ])
        let win = add("ウインドウ", [
            item("しまう", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("閉じる", #selector(NSWindow.performClose(_:)), "w"),
            .separator(),
            item(appName, #selector(showMain), "0", [.command, .option], target: self),
        ])
        NSApp.mainMenu = main
        NSApp.windowsMenu = win
    }
    @objc func showMain() { showWindow() }
    @objc func goInbox() { showWindow(); webView.load(URLRequest(url: homeURL)) }
    @objc func reload() { webView.reload() }
    @objc func zoomIn() { webView.pageZoom = min(3, webView.pageZoom + 0.1) }
    @objc func zoomOut() { webView.pageZoom = max(0.5, webView.pageZoom - 0.1) }
    @objc func zoomReset() { webView.pageZoom = 1 }
    @objc func goBack() { webView.goBack() }
    @objc func goForward() { webView.goForward() }

    // MARK: 未読の数と新着
    func checkFeed() {
        guard let u = webView.url, u.host == "mail.google.com" else { return }
        webView.evaluateJavaScript("window.__gmCheck && window.__gmCheck(); 0", completionHandler: nil)
    }

    func setBadge(_ n: Int) {
        NSApp.dockTile.badgeLabel = n > 0 ? (n > 999 ? "999+" : "\(n)") : nil
    }

    var feedTimer: Timer?
    func titleChanged(_ t: String) {
        if window.title != appName { window.title = appName }
        if t.isEmpty { return }
        var n = 0, hasCount = false
        if let r = t.range(of: #"\((\d+)\)"#, options: .regularExpression) {
            n = Int(t[r].dropFirst().dropLast()) ?? 0; hasCount = true
        }
        let frag = webView.url?.fragment ?? ""
        let list = frag.isEmpty || frag == "inbox" || frag.hasPrefix("inbox?")
        if hasCount || list { setBadge(n) }   // 既読・新着をすぐ反映
        // 少し待ってから、正確な数を確認
        feedTimer?.invalidate()
        feedTimer = Timer.scheduledTimer(withTimeInterval: 0.8, repeats: false) { [weak self] _ in self?.checkFeed() }
        // 確認が使えないときだけ、見出しの数の増加で新着を知らせる
        guard !(feedOK && Date().timeIntervalSince(lastFeedOK) < 180), hasCount || list else { return }
        if titleCount >= 0 && n > titleCount {
            let before = titleCount
            DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
                guard let self = self, Date().timeIntervalSince(self.lastBridgeNotify) > 15 else { return }
                self.post(title: appName, subtitle: "", body: "新着メール \(n - before)件", link: nil, id: "title-\(Date().timeIntervalSince1970)")
            }
        }
        titleCount = n
    }

    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        guard let d = m.body as? [String: Any], let type = d["type"] as? String else { return }
        if type == "feed" {
            guard (d["ok"] as? Bool) == true else {
                if feedOK || firstFeed { log("未読の確認に失敗: \(d["error"] ?? "")") }
                feedOK = false; return
            }
            if !feedOK { log("未読の確認が有効") }
            feedOK = true; lastFeedOK = Date()
            let count = (d["count"] as? Int) ?? (d["count"] as? NSNumber)?.intValue ?? 0
            setBadge(count)
            let entries = (d["entries"] as? [[String: Any]]) ?? []
            var fresh: [[String: Any]] = []
            for e in entries {
                guard let id = e["id"] as? String, !id.isEmpty else { continue }
                if !seen.contains(id) { fresh.append(e); seen.append(id) }
            }
            if seen.count > 600 { seen.removeFirst(seen.count - 600) }
            UserDefaults.standard.set(seen, forKey: "seen")
            if firstFeed { firstFeed = false; return }   // 起動直後の分は知らせない
            if fresh.count > 3 {
                post(title: appName, subtitle: "", body: "新着メール \(fresh.count)件", link: nil, id: "many-\(Date().timeIntervalSince1970)")
            } else {
                for e in fresh.reversed() {
                    let author = (e["author"] as? String) ?? ""
                    let subject = (e["title"] as? String) ?? ""
                    post(title: author.isEmpty ? appName : author,
                         subtitle: subject.isEmpty ? "（件名なし）" : subject,
                         body: (e["summary"] as? String) ?? "",
                         link: e["link"] as? String, id: (e["id"] as? String) ?? UUID().uuidString)
                }
            }
        } else if type == "notify" {
            // 確認が使えないときだけ、ページからの通知を出す
            guard !(feedOK && Date().timeIntervalSince(lastFeedOK) < 180) else { return }
            lastBridgeNotify = Date()
            post(title: (d["title"] as? String) ?? appName, subtitle: "", body: (d["body"] as? String) ?? "",
                 link: nil, id: "page-\(Date().timeIntervalSince1970)")
        }
    }

    func post(title: String, subtitle: String, body: String, link: String?, id: String) {
        let c = UNMutableNotificationContent()
        c.title = title; c.subtitle = subtitle; c.body = body
        c.sound = .default
        c.threadIdentifier = "mail"
        if let l = link { c.userInfo = ["link": l] }
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: id, content: c, trigger: nil)) { e in
            if let e = e { log("通知の失敗: \(e)") }
        }
    }

    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler h: @escaping (UNNotificationPresentationOptions) -> Void) {
        h(NSApp.isActive && window.isVisible ? [] : [.banner, .list, .sound])
    }

    func userNotificationCenter(_ c: UNUserNotificationCenter, didReceive r: UNNotificationResponse,
                                withCompletionHandler h: @escaping () -> Void) {
        showWindow()
        if let l = r.notification.request.content.userInfo["link"] as? String, let u = URL(string: l),
           u.host?.hasSuffix("google.com") == true {
            webView.load(URLRequest(url: u))
        }
        h()
    }

    // MARK: 移動・別ウインドウ・保存・ファイル選択
    func inApp(_ u: URL) -> Bool {
        guard let h = u.host?.lowercased() else { return true }
        for s in ["google.com", "gstatic.com", "googleusercontent.com", "youtube.com", "google.co.jp", "googleapis.com"] {
            if h == s || h.hasSuffix("." + s) { return true }
        }
        return false
    }

    func openOutside(_ u: URL) { NSWorkspace.shared.open(u) }

    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction, decisionHandler h: @escaping (WKNavigationActionPolicy) -> Void) {
        if a.shouldPerformDownload { h(.download); return }
        guard let u = a.request.url else { h(.allow); return }
        if u.scheme == "mailto" {
            h(.cancel)
            var c = URLComponents(string: "https://mail.google.com/mail/u/0/")!
            c.queryItems = [URLQueryItem(name: "extsrc", value: "mailto"), URLQueryItem(name: "url", value: u.absoluteString)]
            if let x = c.url { webView.load(URLRequest(url: x)) }
            return
        }
        if ["http", "https"].contains(u.scheme ?? ""), a.targetFrame?.isMainFrame == true, !inApp(u) {
            openOutside(u); h(.cancel); return
        }
        h(.allow)
    }

    func webView(_ w: WKWebView, decidePolicyFor r: WKNavigationResponse, decisionHandler h: @escaping (WKNavigationResponsePolicy) -> Void) {
        if let hr = r.response as? HTTPURLResponse,
           let cd = hr.value(forHTTPHeaderField: "Content-Disposition"), cd.lowercased().hasPrefix("attachment") {
            h(.download); return
        }
        h(r.canShowMIMEType ? .allow : .download)
    }

    func webView(_ w: WKWebView, navigationAction a: WKNavigationAction, didBecome d: WKDownload) { d.delegate = self }
    func webView(_ w: WKWebView, navigationResponse r: WKNavigationResponse, didBecome d: WKDownload) { d.delegate = self }

    func download(_ d: WKDownload, decideDestinationUsing r: URLResponse, suggestedFilename name: String,
                  completionHandler h: @escaping (URL?) -> Void) {
        let dir = home.appendingPathComponent("Downloads")
        let base = (name as NSString).deletingPathExtension, ext = (name as NSString).pathExtension
        var u = dir.appendingPathComponent(name), i = 2
        while FileManager.default.fileExists(atPath: u.path) {
            u = dir.appendingPathComponent(ext.isEmpty ? "\(base) \(i)" : "\(base) \(i).\(ext)"); i += 1
        }
        downloadDest[ObjectIdentifier(d)] = u
        h(u)
    }
    func downloadDidFinish(_ d: WKDownload) {
        if let u = downloadDest.removeValue(forKey: ObjectIdentifier(d)) {
            log("保存: \(u.lastPathComponent)")
            NSWorkspace.shared.activateFileViewerSelecting([u])
        }
    }
    func download(_ d: WKDownload, didFailWithError e: Error, resumeData: Data?) { log("保存の失敗: \(e)") }

    func webView(_ w: WKWebView, createWebViewWith cfg: WKWebViewConfiguration, for a: WKNavigationAction,
                 windowFeatures f: WKWindowFeatures) -> WKWebView? {
        if let u = a.request.url, u.absoluteString != "about:blank", !u.absoluteString.isEmpty {
            if u.host == "accounts.google.com" { webView.load(a.request) } else { openOutside(u) }
            return nil
        }
        // 中身が後から決まる別ウインドウは、行き先が分かった時点で振り分ける
        let c = WKWebView(frame: .zero, configuration: cfg)
        c.navigationDelegate = PopupCatcher.shared
        PopupCatcher.shared.owner = self
        popupCatchers.append(c)
        return c
    }

    func webView(_ w: WKWebView, runOpenPanelWith p: WKOpenPanelParameters, initiatedByFrame f: WKFrameInfo,
                 completionHandler h: @escaping ([URL]?) -> Void) {
        let o = NSOpenPanel()
        o.allowsMultipleSelection = p.allowsMultipleSelection
        o.canChooseDirectories = p.allowsDirectories
        o.canChooseFiles = true
        o.beginSheetModal(for: window) { r in h(r == .OK ? o.urls : nil) }
    }

    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo,
                 completionHandler h: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = m; a.addButton(withTitle: "OK")
        a.beginSheetModal(for: window) { _ in h() }
    }

    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo,
                 completionHandler h: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = m; a.addButton(withTitle: "OK"); a.addButton(withTitle: "キャンセル")
        a.beginSheetModal(for: window) { r in h(r == .alertFirstButtonReturn) }
    }

    func webViewWebContentProcessDidTerminate(_ w: WKWebView) {
        log("ページが止まったので再読み込み"); w.reload()
    }

    func webView(_ w: WKWebView, didFinish n: WKNavigation!) {
        if let u = w.url { log("表示: \(u.host ?? "")\(u.path)") }
    }

    // MARK: ログイン時の起動（画面は出さない）
    func installLoginAgent() {
        let label = "\(bundleID).login"
        let dir = home.appendingPathComponent("Library/LaunchAgents")
        let url = dir.appendingPathComponent("\(label).plist")
        let plist: [String: Any] = [
            "Label": label,
            "ProgramArguments": ["/usr/bin/open", "-g", "-j", "-a", Bundle.main.bundlePath, "--args", "--background"],
            "RunAtLoad": true,
            "LimitLoadToSessionType": "Aqua",
        ]
        guard let data = try? PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0) else { return }
        if (try? Data(contentsOf: url)) == data { return }
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        do { try data.write(to: url); log("ログイン時の起動を設定") } catch { log("ログイン時の起動の設定に失敗: \(error)") }
    }

    // MARK: アイコン（以前の Gmail と同じもの）
    func setupIcon() {
        let res = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources")
        let icns = res.appendingPathComponent("AppIcon.icns")
        if FileManager.default.fileExists(atPath: icns.path) { return }   // 実行中も Dock は同梱のアイコンをそのまま使う
        let saved = home.appendingPathComponent("Library/Application Support/Gmail/AppIcon.icns")
        if !FileManager.default.fileExists(atPath: saved.path), FileManager.default.fileExists(atPath: oldSafariApp.path) {
            let img = NSWorkspace.shared.icon(forFile: oldSafariApp.path)
            try? FileManager.default.createDirectory(at: saved.deletingLastPathComponent(), withIntermediateDirectories: true)
            if let dest = CGImageDestinationCreateWithURL(saved as CFURL, UTType.icns.identifier as CFString, 3, nil) {
                for s in [256, 512, 1024] {
                    var r = NSRect(x: 0, y: 0, width: s, height: s)
                    if let cg = img.cgImage(forProposedRect: &r, context: nil, hints: nil) { CGImageDestinationAddImage(dest, cg, nil) }
                }
                if CGImageDestinationFinalize(dest) { log("アイコンを用意") }
            }
        }
        if FileManager.default.fileExists(atPath: saved.path) {
            try? FileManager.default.createDirectory(at: res, withIntermediateDirectories: true)
            try? FileManager.default.copyItem(at: saved, to: icns)
            let now = Date()
            try? FileManager.default.setAttributes([.modificationDate: now], ofItemAtPath: Bundle.main.bundlePath)
        }
    }

    // MARK: 新しい版への入れ替え（画面を閉じている間に、そっと）
    func checkUpdate() {
        let p = Bundle.main.bundleURL.appendingPathComponent("Contents/Info.plist")
        guard let d = NSDictionary(contentsOf: p), let v = d["CFBundleVersion"] as? String, v != runningVersion else {
            updatePendingSince = nil; return
        }
        if updatePendingSince == nil { updatePendingSince = Date(); return }   // 入れ替え途中かもしれないので少し待つ
        guard Date().timeIntervalSince(updatePendingSince!) > 90 else { return }
        if window.isVisible && NSApp.isActive { return }   // 使っている最中は待つ
        let visible = window.isVisible
        log("新しい版 \(v) に入れ替え")
        let path = Bundle.main.bundlePath.replacingOccurrences(of: "'", with: "'\\''")
        let args = visible ? "-g -a '\(path)'" : "-g -j -a '\(path)' --args --background"
        let t = Process()
        t.executableURL = URL(fileURLWithPath: "/bin/sh")
        t.arguments = ["-c", "sleep 2; /usr/bin/open \(args)"]
        try? t.run()
        NSApp.terminate(nil)
    }
}

final class PopupCatcher: NSObject, WKNavigationDelegate {
    static let shared = PopupCatcher()
    weak var owner: AppDelegate?
    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction, decisionHandler h: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let u = a.request.url, u.absoluteString != "about:blank" else { h(.allow); return }
        h(.cancel)
        if let o = owner {
            if u.host == "accounts.google.com" { o.webView.load(a.request) } else { o.openOutside(u) }
            o.popupCatchers.removeAll { $0 === w }
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
