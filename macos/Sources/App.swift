import AppKit
import Darwin
import Foundation
import ServiceManagement
import UniformTypeIdentifiers
import WebKit

let reopenNote = Notification.Name("local.sunmuxin.reading-log.reopen")

@main
enum ReadingLogApp {
    static func main() {
        let mine = ProcessInfo.processInfo.processIdentifier
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: "local.sunmuxin.reading-log")
            .filter { $0.processIdentifier != mine }
        if let other = others.first {
            DistributedNotificationCenter.default().postNotificationName(
                reopenNote,
                object: nil,
                userInfo: nil,
                deliverImmediately: true
            )
            other.activate()
            return
        }
        let delegate = AppDelegate()
        AppDelegate.held = delegate
        let app = NSApplication.shared
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        app.run()
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, WKDownloadDelegate {
    static var held: AppDelegate?

    private let server = ServerManager()
    private let renderer = WallpaperRenderer()
    private var lastFingerprint: String?
    private var busy = false
    private var pollTimer: Timer?
    private var activity: NSObjectProtocol?
    private var nudgeSource: DispatchSourceFileSystemObject?
    private var b7NudgeSource: DispatchSourceFileSystemObject?
    private var pollWork: DispatchWorkItem?
    private var b7Busy = false
    private var mainWindow: NSWindow?
    private var webView: WKWebView!
    private var displayPopup: NSPopUpButton!
    private var autoCheck: NSButton!
    private var loginCheck: NSButton!
    private var statusField: NSTextField!
    private var pendingDownload: URL?
    private var wallpaperBooted = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        activity = ProcessInfo.processInfo.beginActivity(
            options: [.userInitiatedAllowingIdleSystemSleep],
            reason: "书历"
        )
        setupAppMenu()
        showMainWindow()
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(screensChanged),
            name: NSApplication.didChangeScreenParametersNotification,
            object: nil
        )
        NSApp.activate()
        scheduleMidnight()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.schedulePoll(captureIfNeeded: false) }
        }
        pollTimer?.tolerance = 8
        watchNudgeFile()
        DistributedNotificationCenter.default().addObserver(
            self,
            selector: #selector(onReopenNote),
            name: reopenNote,
            object: nil
        )
        Task { await boot() }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMainWindow()
        NSApp.activate()
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationWillTerminate(_ notification: Notification) {
        DistributedNotificationCenter.default().removeObserver(self)
        nudgeSource?.cancel()
        b7NudgeSource?.cancel()
        coverPickNudgeSource?.cancel()
        server.stopIfOwned()
        if let activity { ProcessInfo.processInfo.endActivity(activity) }
    }

    private func setupAppMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "设为壁纸", action: #selector(setNow), keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "退出书历", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem()
        main.addItem(editItem)
        let edit = NSMenu(title: "编辑")
        addEditItem(edit, title: "剪切", action: #selector(NSText.cut(_:)), key: "x")
        addEditItem(edit, title: "复制", action: #selector(NSText.copy(_:)), key: "c")
        addEditItem(edit, title: "粘贴", action: #selector(NSText.paste(_:)), key: "v")
        addEditItem(edit, title: "全选", action: #selector(NSText.selectAll(_:)), key: "a")
        editItem.submenu = edit

        NSApp.mainMenu = main
    }

    private func addEditItem(_ menu: NSMenu, title: String, action: Selector, key: String) {
        menu.addItem(withTitle: title, action: action, keyEquivalent: key)
        let ctrl = NSMenuItem(title: title, action: action, keyEquivalent: key)
        ctrl.keyEquivalentModifierMask = [.control]
        ctrl.isHidden = true
        menu.addItem(ctrl)
    }

    private var autoUpdateEnabled: Bool {
        get {
            if UserDefaults.standard.object(forKey: "autoUpdate") == nil { return true }
            return UserDefaults.standard.bool(forKey: "autoUpdate")
        }
        set { UserDefaults.standard.set(newValue, forKey: "autoUpdate") }
    }

    func setStatus(_ text: String) {
        statusField?.stringValue = text
        Log.line(text)
    }

    @objc private func onReopenNote() {
        showMainWindow()
        NSApp.activate()
    }

    @objc private func displayChanged() {
        guard let item = displayPopup.selectedItem,
              let num = item.representedObject as? NSNumber else { return }
        let id = CGDirectDisplayID(num.uint32Value)
        guard let screen = NSScreen.screens.first(where: { $0.displayID == id }) else { return }
        DisplayChoice.select(screen)
        setStatus("壁纸只用「\(screen.localizedName)」")
        installPlannerCanvasScript()
        pushPlannerCanvasWidth()
    }

    @objc private func setNow() {
        Task { await refreshWallpaper(reason: "手动", recapture: true) }
    }

    @objc private func toggleAuto() {
        autoUpdateEnabled = autoCheck.state == .on
        setStatus(autoUpdateEnabled ? "已打开自动更新（仅所选屏幕）" : "已关闭自动更新")
    }

    @objc private func toggleLogin() {
        do {
            if loginCheck.state == .on {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
            loginCheck.state = SMAppService.mainApp.status == .enabled ? .on : .off
            setStatus(loginCheck.state == .on ? "已打开开机启动" : "已关闭开机启动")
        } catch {
            loginCheck.state = SMAppService.mainApp.status == .enabled ? .on : .off
            setStatus("开机启动失败：把应用放在「应用程序」后再试。")
        }
    }

    private func rebuildDisplayPopup() {
        displayPopup.removeAllItems()
        let resolved = DisplayChoice.resolveScreen()
        var pick = 0
        for (i, screen) in NSScreen.screens.enumerated() {
            displayPopup.addItem(withTitle: screen.menuLabel)
            displayPopup.item(at: i)?.representedObject = NSNumber(value: screen.displayID)
            if screen.displayID == resolved.displayID { pick = i }
        }
        if displayPopup.numberOfItems == 0 {
            displayPopup.addItem(withTitle: "没有显示器")
            displayPopup.isEnabled = false
            return
        }
        displayPopup.isEnabled = true
        displayPopup.selectItem(at: pick)
        installPlannerCanvasScript()
        pushPlannerCanvasWidth()
    }

    private func livePlannerCSSSize() -> (width: Int, height: Int) {
        let wall = DisplayChoice.wallpaperPixelSize()
        return DisplayChoice.wallpaperCSSSize(pixelWidth: wall.width, pixelHeight: wall.height)
    }

    private func plannerPageURL() -> URL {
        let css = livePlannerCSSSize()
        var comps = URLComponents(url: ServerManager.rootURL, resolvingAgainstBaseURL: false)!
        comps.queryItems = [
            URLQueryItem(name: "canvasWidth", value: String(css.width)),
            URLQueryItem(name: "canvasHeight", value: String(css.height)),
            URLQueryItem(name: "displayPixelWidth", value: String(DisplayChoice.plannerPixelWidth)),
            URLQueryItem(name: "displayPixelHeight", value: String(DisplayChoice.plannerPixelHeight))
        ]
        return comps.url ?? ServerManager.rootURL
    }

    private func plannerCanvasUserScript() -> WKUserScript {
        let css = livePlannerCSSSize()
        let pxW = DisplayChoice.plannerPixelWidth
        let pxH = DisplayChoice.plannerPixelHeight
        let source = """
        document.documentElement.style.setProperty('--planner-canvas-width', '\(css.width)px');
        document.documentElement.style.setProperty('--planner-canvas-height', '\(css.height)px');
        document.documentElement.dataset.plannerCanvasWidth = '\(css.width)';
        document.documentElement.dataset.plannerCanvasHeight = '\(css.height)';
        document.documentElement.dataset.plannerCanvasPixelWidth = '\(pxW)';
        document.documentElement.dataset.plannerCanvasPixelHeight = '\(pxH)';
        """
        return WKUserScript(source: source, injectionTime: .atDocumentStart, forMainFrameOnly: true)
    }

    private func installPlannerCanvasScript() {
        guard let controller = webView?.configuration.userContentController else { return }
        controller.removeAllUserScripts()
        controller.addUserScript(plannerCanvasUserScript())
    }

    private func pushPlannerCanvasWidth() {
        guard let wv = webView else { return }
        let css = livePlannerCSSSize()
        let pxW = DisplayChoice.plannerPixelWidth
        let pxH = DisplayChoice.plannerPixelHeight
        let wall = DisplayChoice.wallpaperPixelSize()
        let mag = DisplayChoice.wallpaperMagnification()
        let displayID = DisplayChoice.selectedID
        let js = """
        (function() {
          var w = \(css.width), h = \(css.height);
          if (window.applyPlannerCanvasSize) window.applyPlannerCanvasSize(w, h);
          else if (window.applyPlannerCanvasWidth) window.applyPlannerCanvasWidth(w, h);
          else {
            document.documentElement.style.setProperty('--planner-canvas-width', w + 'px');
            document.documentElement.style.setProperty('--planner-canvas-height', h + 'px');
            document.documentElement.dataset.plannerCanvasWidth = String(w);
            document.documentElement.dataset.plannerCanvasHeight = String(h);
          }
          document.documentElement.dataset.plannerCanvasPixelWidth = '\(pxW)';
          document.documentElement.dataset.plannerCanvasPixelHeight = '\(pxH)';
          if (window.syncWallpaperResolutionInputs) window.syncWallpaperResolutionInputs(\(wall.width), \(wall.height), '\(displayID)', \(mag));
        })();
        """
        wv.evaluateJavaScript(js, completionHandler: nil)
    }

    @objc private func screensChanged() {
        rebuildDisplayPopup()
    }

    private func showMainWindow() {
        if let win = mainWindow {
            rebuildDisplayPopup()
            win.makeKeyAndOrderFront(nil)
            win.makeFirstResponder(webView)
            return
        }
        let width: CGFloat = 1280
        let height: CGFloat = 860
        let toolH: CGFloat = 44
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: width, height: height),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        win.title = "书历"
        win.minSize = NSSize(width: 900, height: 600)
        win.isReleasedWhenClosed = false
        win.setFrameAutosaveName("ReadingLogMain")
        if !win.setFrameUsingName("ReadingLogMain") {
            win.center()
        }

        let root = NSView(frame: NSRect(x: 0, y: 0, width: width, height: height))
        let bar = NSView(frame: NSRect(x: 0, y: height - toolH, width: width, height: toolH))
        bar.wantsLayer = true
        bar.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        bar.autoresizingMask = [.width, .minYMargin]

        displayPopup = NSPopUpButton(frame: NSRect(x: 12, y: 8, width: 280, height: 28), pullsDown: false)
        displayPopup.target = self
        displayPopup.action = #selector(displayChanged)
        displayPopup.refusesFirstResponder = true
        bar.addSubview(displayPopup)
        rebuildDisplayPopup()

        autoCheck = NSButton(checkboxWithTitle: "自动更新壁纸", target: self, action: #selector(toggleAuto))
        autoCheck.frame = NSRect(x: 304, y: 10, width: 120, height: 24)
        autoCheck.state = autoUpdateEnabled ? .on : .off
        autoCheck.refusesFirstResponder = true
        bar.addSubview(autoCheck)

        loginCheck = NSButton(checkboxWithTitle: "开机启动", target: self, action: #selector(toggleLogin))
        loginCheck.frame = NSRect(x: 430, y: 10, width: 90, height: 24)
        loginCheck.state = SMAppService.mainApp.status == .enabled ? .on : .off
        loginCheck.refusesFirstResponder = true
        bar.addSubview(loginCheck)

        let wallBtn = NSButton(title: "设为壁纸", target: self, action: #selector(setNow))
        wallBtn.bezelStyle = .rounded
        wallBtn.frame = NSRect(x: 530, y: 8, width: 88, height: 28)
        wallBtn.refusesFirstResponder = true
        bar.addSubview(wallBtn)

        statusField = NSTextField(labelWithString: "正在启动…")
        statusField.frame = NSRect(x: 630, y: 12, width: 630, height: 20)
        statusField.lineBreakMode = .byTruncatingTail
        statusField.textColor = .secondaryLabelColor
        statusField.autoresizingMask = [.width]
        bar.addSubview(statusField)

        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "readingLog")
        config.userContentController.addUserScript(plannerCanvasUserScript())
        let wv = WKWebView(frame: NSRect(x: 0, y: 0, width: width, height: height - toolH), configuration: config)
        wv.autoresizingMask = [.width, .height]
        wv.navigationDelegate = self
        wv.uiDelegate = self
        webView = wv

        root.addSubview(wv)
        root.addSubview(bar)
        win.contentView = root
        win.initialFirstResponder = wv
        win.makeKeyAndOrderFront(nil)
        win.makeFirstResponder(wv)
        DispatchQueue.main.async { [weak win, weak wv] in
            guard let win, let wv else { return }
            win.makeFirstResponder(wv)
        }
        mainWindow = win
    }

    private func clearWebChromeFocus() {
        guard let wv = webView else { return }
        wv.window?.makeFirstResponder(wv)
        wv.evaluateJavaScript("""
        (function() {
          var el = document.activeElement;
          if (el && el !== document.body && el !== document.documentElement && el.blur) el.blur();
        })();
        """, completionHandler: nil)
    }

    private func boot() async {
        setStatus("正在启动本地服务…")
        do {
            try await server.ensureRunning()
            setStatus("正在打开…")
            // Fire-and-forget: never block boot/webview on tunnel checks (URLSession used to hang).
            server.openRemoteTunnelIfNeeded()
            webView.load(URLRequest(url: plannerPageURL(), cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20))
        } catch {
            setStatus(error.localizedDescription)
            showErrorPage(error.localizedDescription)
        }
    }

    private func bootWallpaperIfNeeded() async {
        if wallpaperBooted { return }
        wallpaperBooted = true
        lastFingerprint = await monthFingerprint()
        if let saved = WallpaperStateStore.load(),
           saved.fingerprint == lastFingerprint,
           WallpaperSetter.selectedStillOurs(saved.path) {
            Log.line("boot skip wallpaper, unchanged")
            return
        }
        if autoUpdateEnabled {
            await refreshWallpaper(reason: "启动", recapture: true)
        }
    }

    private func showErrorPage(_ msg: String) {
        let html = "<meta charset=utf-8><body style='font-family:-apple-system;padding:40px;color:#6b5f54;background:#fcfbf8'><h2>书历</h2><p>\(msg)</p></body>"
        webView.loadHTMLString(html, baseURL: nil)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        pushPlannerCanvasWidth()
        setStatus("已打开")
        clearWebChromeFocus()
        DispatchQueue.main.async { [weak self] in self?.clearWebChromeFocus() }
        Task { await snapshotWindowProof() }
        Task {
            try? await Task.sleep(nanoseconds: 800_000_000)
            await bootWallpaperIfNeeded()
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        setStatus(error.localizedDescription)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        setStatus(error.localizedDescription)
    }

    /// Required for `<input type="file">` / showPicker in WKWebView. Without this,
    /// cover upload silently does nothing (WebKit never presents a panel).
    /// Note: books UI uses CSS transform/zoom on ancestors; WebKit often never
    /// invokes this for those file inputs — see pickCover via JS bridge.
    func webView(
        _ webView: WKWebView,
        runOpenPanelWith parameters: WKOpenPanelParameters,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping ([URL]?) -> Void
    ) {
        Log.line("runOpenPanelWith invoked allowsMultiple=\(parameters.allowsMultipleSelection)")
        NSLog("[书历] runOpenPanelWith invoked")
        presentImageOpenPanel(allowsMultiple: parameters.allowsMultipleSelection) { urls in
            completionHandler(urls)
        }
    }

    /// App-modal NSOpenPanel (not sheet). Sheet presentation can fail to appear
    /// when WebKit is mid-gesture / under transform hit-testing quirks.
    private func presentImageOpenPanel(allowsMultiple: Bool, completion: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = allowsMultiple
        panel.allowedContentTypes = [.image]
        panel.prompt = "选择"
        panel.message = "选择封面图片"
        panel.begin { response in
            let urls = response == .OK ? panel.urls : nil
            Log.line("openPanel response=\(response.rawValue) count=\(urls?.count ?? 0)")
            completion(urls)
        }
    }

    /// Native cover picker for the Mac app. Bypass `<input type=file>` because
    /// `#booksPan` CSS transform / `#cardPopup` CSS zoom often prevent WebKit
    /// from activating runOpenPanelWith at all.
    private func pickCoverForWeb(requestId: String) {
        let rid = requestId.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !rid.isEmpty else { return }
        Log.line("pickCover bridge requestId=\(rid)")
        NSLog("[书历] pickCover bridge requestId=%@", rid)
        presentImageOpenPanel(allowsMultiple: false) { [weak self] urls in
            guard let self else { return }
            guard let url = urls?.first else {
                self.deliverCoverPick(requestId: rid, payload: ["ok": false, "cancelled": true])
                return
            }
            self.deliverCoverPickFromFile(requestId: rid, url: url)
        }
    }

    private func deliverCoverPickFromFile(requestId: String, url: URL) {
        let accessed = url.startAccessingSecurityScopedResource()
        defer { if accessed { url.stopAccessingSecurityScopedResource() } }
        do {
            let data = try Data(contentsOf: url)
            guard !data.isEmpty, data.count <= 40 * 1024 * 1024 else {
                deliverCoverPick(requestId: requestId, payload: [
                    "ok": false,
                    "error": data.isEmpty ? "空文件" : "图片过大（上限 40MB）"
                ])
                return
            }
            let name = url.lastPathComponent
            let ext = url.pathExtension.lowercased()
            let mime: String
            switch ext {
            case "png": mime = "image/png"
            case "jpg", "jpeg": mime = "image/jpeg"
            case "gif": mime = "image/gif"
            case "webp": mime = "image/webp"
            case "heic", "heif": mime = "image/heic"
            default: mime = "application/octet-stream"
            }
            deliverCoverPick(requestId: requestId, payload: [
                "ok": true,
                "name": name,
                "mime": mime,
                "base64": data.base64EncodedString()
            ])
        } catch {
            deliverCoverPick(requestId: requestId, payload: [
                "ok": false,
                "error": error.localizedDescription
            ])
        }
    }

    private func deliverCoverPick(requestId: String, payload: [String: Any]) {
        var body = payload
        body["requestId"] = requestId
        guard let data = try? JSONSerialization.data(withJSONObject: body),
              let json = String(data: data, encoding: .utf8) else {
            Log.line("pickCover deliver encode failed")
            return
        }
        let js = "window.__onNativeCoverPick && window.__onNativeCoverPick(\(json))"
        webView.evaluateJavaScript(js) { _, err in
            if let err {
                Log.line("pickCover deliver JS error: \(err.localizedDescription)")
            } else {
                Log.line("pickCover deliver ok requestId=\(requestId)")
            }
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.shouldPerformDownload {
            decisionHandler(.download)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if navigationResponse.canShowMIMEType {
            decisionHandler(.allow)
        } else {
            decisionHandler(.download)
        }
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String) async -> URL? {
        let dest = uniqueDownloadURL(suggestedFilename)
        pendingDownload = dest
        return dest
    }

    func downloadDidFinish(_ download: WKDownload) {
        if let dest = pendingDownload {
            revealSavedFile(dest)
        }
        pendingDownload = nil
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        setStatus("保存失败：\(error.localizedDescription)")
        pendingDownload = nil
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "readingLog" else { return }
        let body = message.body as? [String: Any]
        let action = body?["action"] as? String ?? (message.body as? String)
        if action == "wallpaperSize" || action == "setWallpaper" {
            if let w = jsonInt(body?["canvasWidth"]), let h = jsonInt(body?["canvasHeight"]), w >= 320, h >= 200 {
                DisplayChoice.setWallpaperPixelSize(width: w, height: h)
            }
            if let s = jsonInt(body?["canvasScale"]) ?? jsonInt(body?["magnification"]), s >= 1, s <= 4 {
                DisplayChoice.setWallpaperMagnification(s)
            }
            if action == "setWallpaper" {
                Task { await refreshWallpaper(reason: "网页", recapture: true) }
            }
            return
        }
        if action == "revealFile" {
            let path = (body?["path"] as? String) ?? ""
            if !path.isEmpty {
                revealSavedFile(URL(fileURLWithPath: path))
            }
            return
        }
        if action == "saveImage" {
            savePngToDownloads(
                filename: (body?["filename"] as? String) ?? "书历.png",
                base64: (body?["pngBase64"] as? String) ?? ""
            )
            return
        }
        if action == "pickCover" {
            pickCoverForWeb(requestId: (body?["requestId"] as? String) ?? "")
            return
        }
    }

    private func downloadsDirectory() -> URL {
        FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Downloads")
    }

    private func uniqueDownloadURL(_ suggested: String) -> URL {
        let dir = downloadsDirectory()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let raw = suggested.isEmpty ? "书历.png" : (suggested as NSString).lastPathComponent
        var dest = dir.appendingPathComponent(raw)
        if !FileManager.default.fileExists(atPath: dest.path) { return dest }
        let stem = dest.deletingPathExtension().lastPathComponent
        let ext = dest.pathExtension
        var i = 2
        while true {
            let next = ext.isEmpty ? "\(stem)-\(i)" : "\(stem)-\(i).\(ext)"
            dest = dir.appendingPathComponent(next)
            if !FileManager.default.fileExists(atPath: dest.path) { return dest }
            i += 1
        }
    }

    private func revealSavedFile(_ url: URL) {
        guard FileManager.default.fileExists(atPath: url.path) else { return }
        NSWorkspace.shared.activateFileViewerSelecting([url])
        setStatus("已保存到「下载」：\(url.lastPathComponent)")
    }

    private func savePngToDownloads(filename: String, base64: String) {
        let cleaned = base64.replacingOccurrences(of: "\n", with: "").replacingOccurrences(of: "\r", with: "")
        guard let data = Data(base64Encoded: cleaned), !data.isEmpty else {
            setStatus("保存图片失败：无法解码")
            return
        }
        let dest = uniqueDownloadURL(filename.isEmpty ? "书历.png" : filename)
        do {
            try data.write(to: dest, options: .atomic)
            revealSavedFile(dest)
        } catch {
            setStatus("保存图片失败：\(error.localizedDescription)")
        }
    }

    private func snapshotWindowProof() async {
        guard let wv = webView else { return }
        let title = (try? await wv.evaluateJavaScript("document.title")) as? String
        let hasCal = (try? await wv.evaluateJavaScript("!!document.querySelector('.planner,.cal-tools,#monthView')")) as? Bool
        Log.line("webview title=\(title ?? "?") calendar=\(hasCal ?? false)")
        let img = try? await wv.takeSnapshot(configuration: nil)
        if let img, let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
           let png = rep.representation(using: .png, properties: [:]) {
            let url = Log.dir.appendingPathComponent("app-window.png")
            try? png.write(to: url)
            Log.line("wrote window proof \(url.path) \(png.count) bytes")
        }
    }

    private var coverPickNudgeSource: DispatchSourceFileSystemObject?

    private func watchNudgeFile() {
        try? FileManager.default.createDirectory(at: Log.dir, withIntermediateDirectories: true)
        let nudge = Log.dir.appendingPathComponent("wallpaper.nudge")
        if !FileManager.default.fileExists(atPath: nudge.path) {
            FileManager.default.createFile(atPath: nudge.path, contents: Data())
        }
        let fd = open(nudge.path, O_EVTONLY)
        guard fd >= 0 else { return }
        let src = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: [.write, .extend, .attrib, .rename], queue: .main)
        src.setEventHandler { [weak self] in
            Task { @MainActor in
                await self?.refreshWallpaper(reason: "网页设为壁纸", recapture: true)
            }
        }
        src.setCancelHandler { close(fd) }
        src.resume()
        nudgeSource = src
        watchB7NudgeFile()
        watchCoverPickNudge()
    }

    /// Lock-path B7 PNG: render 书历 wall canvas (from nudge JSON / phone prefs)
    /// into wallpaper-b7-capture.png without calling setDesktopImage
    /// (Mac Studio wall stays independent).
    private func watchB7NudgeFile() {
        let nudge = Log.dir.appendingPathComponent("wallpaper-b7.nudge")
        if !FileManager.default.fileExists(atPath: nudge.path) {
            FileManager.default.createFile(atPath: nudge.path, contents: Data("{}".utf8))
        }
        let fd = open(nudge.path, O_EVTONLY)
        guard fd >= 0 else { return }
        let src = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: [.write, .extend, .attrib, .rename], queue: .main)
        src.setEventHandler { [weak self] in
            Task { @MainActor in
                await self?.refreshB7Capture()
            }
        }
        src.setCancelHandler { close(fd) }
        src.resume()
        b7NudgeSource = src
    }

    private func readB7Nudge() -> (width: Int, height: Int, mag: Int, orient: String?) {
        let url = Log.dir.appendingPathComponent("wallpaper-b7.nudge")
        guard let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return (ServerManager.b7WallWidth, ServerManager.b7WallHeight, ServerManager.b7WallMag, nil)
        }
        let w = max(320, jsonInt(obj["width"]) ?? ServerManager.b7WallWidth)
        let h = max(200, jsonInt(obj["height"]) ?? ServerManager.b7WallHeight)
        let m = DisplayChoice.clampWallpaperMagnification(jsonInt(obj["mag"]) ?? ServerManager.b7WallMag)
        let raw = (obj["orient"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? ""
        let orient: String? = (raw == "portrait" || raw == "landscape") ? raw : nil
        return (w, h, m, orient)
    }

    /// Generate B7 lock PNG only — never applies Mac desktop wallpaper.
    private func refreshB7Capture() async {
        if b7Busy {
            Log.line("b7 capture skipped, already busy")
            return
        }
        // Wait briefly if Mac desktop capture holds the shared renderer.
        for _ in 0..<40 {
            if !busy { break }
            try? await Task.sleep(nanoseconds: 250_000_000)
        }
        if busy {
            Log.line("b7 capture deferred, mac wallpaper still busy")
            return
        }
        b7Busy = true
        defer { b7Busy = false }
        do {
            try await server.ensureRunning()
            let req = readB7Nudge()
            let screen = DisplayChoice.screen()
            Log.line("b7 capture start \(req.width)x\(req.height) mag=\(req.mag) orient=\(req.orient ?? "status")")
            setStatus("正在生成 B7 锁屏壁纸 \(req.width)×\(req.height)…")
            let (png, pixel) = try await renderer.captureCurrentMonth(
                on: screen,
                pixelWidth: req.width,
                pixelHeight: req.height,
                magnification: req.mag,
                device: "b7",
                orientOverride: req.orient
            )
            let dest = try WallpaperRenderer.writeB7Capture(png)
            setStatus("已生成 B7 锁屏 \(Int(pixel.width))×\(Int(pixel.height))（未改 Mac 桌面）")
            Log.line("b7 capture done \(Int(pixel.width))x\(Int(pixel.height)) → \(dest.lastPathComponent) (desktop unchanged)")
        } catch {
            setStatus("B7 锁屏壁纸失败：\(error.localizedDescription)")
            Log.line("b7 capture fail \(error.localizedDescription)")
        }
    }

    /// Debug/automation: touch ~/.reading-log/cover-pick.nudge → switch to 卡片 and
    /// click first cover (exercises pickCover JS bridge → NSOpenPanel).
    private func watchCoverPickNudge() {
        let nudge = Log.dir.appendingPathComponent("cover-pick.nudge")
        if !FileManager.default.fileExists(atPath: nudge.path) {
            FileManager.default.createFile(atPath: nudge.path, contents: Data())
        }
        let fd = open(nudge.path, O_EVTONLY)
        guard fd >= 0 else { return }
        let src = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: [.write, .extend, .attrib, .rename], queue: .main)
        src.setEventHandler { [weak self] in
            self?.triggerCoverPickFromNudge()
        }
        src.setCancelHandler { close(fd) }
        src.resume()
        coverPickNudgeSource = src
    }

    private func triggerCoverPickFromNudge() {
        Log.line("cover-pick.nudge → click first cover via JS")
        let js = """
        (function() {
          var tab = document.getElementById('tabBooks');
          if (tab) tab.click();
          setTimeout(function() {
            var slot = document.querySelector('#bookView .cover-slot');
            if (!slot) { console.log('no cover-slot'); return; }
            slot.click();
          }, 600);
        })();
        """
        webView.evaluateJavaScript(js) { _, err in
            if let err { Log.line("cover-pick nudge JS error: \(err.localizedDescription)") }
        }
    }

    private func schedulePoll(captureIfNeeded: Bool) {
        pollWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            Task { @MainActor in await self?.poll(captureIfNeeded: captureIfNeeded) }
        }
        pollWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8, execute: work)
    }

    private func poll(captureIfNeeded: Bool) async {
        if !autoUpdateEnabled && !captureIfNeeded { return }
        if !(await server.isHealthy()) {
            do { try await server.ensureRunning() } catch { return }
        }
        let fp = await monthFingerprint()
        if !captureIfNeeded, let fp, fp == lastFingerprint || fp == WallpaperStateStore.load()?.fingerprint {
            Log.line("poll skip, fingerprint unchanged")
            lastFingerprint = fp
            return
        }
        await refreshWallpaper(reason: "记录变化", recapture: true)
        lastFingerprint = fp ?? lastFingerprint
    }

    private func scheduleMidnight() {
        let cal = Calendar.current
        guard let next = cal.nextDate(after: Date(), matching: DateComponents(hour: 0, minute: 1, second: 0), matchingPolicy: .nextTime) else { return }
        let delay = max(5, next.timeIntervalSinceNow)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            Task { @MainActor in
                await self?.silentSync()
                await self?.refreshWallpaper(reason: "每日", recapture: true)
                self?.scheduleMidnight()
            }
        }
    }

    /// Midnight still refreshes the wallpaper. WeRead runs only at times the page saved; an empty list does not fall back to 0:01.
    private func silentSync() async {
        var ir = URLRequest(url: URL(string: "http://127.0.0.1:18766/api/sync/ireader")!)
        ir.httpMethod = "POST"
        ir.setValue("application/json", forHTTPHeaderField: "Content-Type")
        ir.httpBody = Data("{}".utf8)
        ir.timeoutInterval = 60
        _ = try? await URLSession.shared.data(for: ir)
    }

    /// Prefer the live wallW/wallH/wallMag inputs over stale UserDefaults.
    private func pullWallpaperSizeFromWeb() async {
        guard let wv = webView else { return }
        let raw = try? await wv.evaluateJavaScript("""
        (function() {
          if (!window.readWallpaperSizeInputs) return null;
          var o = window.readWallpaperSizeInputs();
          if (!o) return null;
          return { w: o.w|0, h: o.h|0, scale: o.scale|0 };
        })();
        """)
        guard let dict = raw as? [String: Any] else { return }
        if let w = jsonInt(dict["w"]), let h = jsonInt(dict["h"]), w >= 320, h >= 200 {
            DisplayChoice.setWallpaperPixelSize(width: w, height: h)
        }
        if let s = jsonInt(dict["scale"]), s >= 1, s <= 4 {
            DisplayChoice.setWallpaperMagnification(s)
        }
        Log.line("pulled wall size \(DisplayChoice.wallpaperPixelSize().width)x\(DisplayChoice.wallpaperPixelSize().height) mag=\(DisplayChoice.wallpaperMagnification())")
    }

    func refreshWallpaper(reason: String, recapture: Bool) async {
        if busy {
            setStatus("正在生成壁纸，请稍候…")
            return
        }
        busy = true
        defer { busy = false }
        do {
            try await server.ensureRunning()
            await pullWallpaperSizeFromWeb()
            let fp = await monthFingerprint() ?? ""
            if !recapture, let saved = WallpaperStateStore.load(), saved.fingerprint == fp,
               WallpaperSetter.selectedStillOurs(saved.path) {
                lastFingerprint = fp
                setStatus("壁纸已是最新，未改其它屏幕")
                Log.line("skip capture, fingerprint unchanged")
                return
            }
            let screen = DisplayChoice.screen()
            let wall = DisplayChoice.wallpaperPixelSize()
            let mag = DisplayChoice.wallpaperMagnification()
            setStatus("正在生成壁纸（\(reason)）→ \(screen.localizedName)…")
            let (png, pixel) = try await renderer.captureCurrentMonth(
                on: screen,
                pixelWidth: wall.width,
                pixelHeight: wall.height,
                magnification: mag
            )
            let outcome = try WallpaperSetter.apply(png: png, fingerprint: fp, pixel: pixel, screen: screen)
            lastFingerprint = fp
            switch outcome {
            case .applied:
                setStatus("已设到「\(screen.localizedName)」\(Int(pixel.width))×\(Int(pixel.height))")
            case .skipped:
                setStatus("壁纸未变（已是最新）。其它屏幕未改。")
            }
        } catch {
            setStatus("设为壁纸失败：\(error.localizedDescription)")
            Log.line("wallpaper fail \(error.localizedDescription)")
        }
    }
}
