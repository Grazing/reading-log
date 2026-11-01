import AppKit
import CryptoKit
import Darwin
import Foundation
import WebKit

enum ReadingLogError: LocalizedError {
    case server(String)
    case render(String)
    case wallpaper(String)

    var errorDescription: String? {
        switch self {
        case .server(let s), .render(let s), .wallpaper(let s):
            return s
        }
    }
}

enum Log {
    static let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".reading-log")

    static func line(_ msg: String) {
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("wallpaper.log")
        let text = ISO8601DateFormatter().string(from: Date()) + " " + msg + "\n"
        if FileManager.default.fileExists(atPath: url.path) {
            if let handle = try? FileHandle(forWritingTo: url) {
                defer { try? handle.close() }
                _ = try? handle.seekToEnd()
                try? handle.write(contentsOf: Data(text.utf8))
            }
        } else {
            try? text.write(to: url, atomically: true, encoding: .utf8)
        }
    }
}

func sha256Hex(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

extension NSScreen {
    var displayID: CGDirectDisplayID {
        let key = NSDeviceDescriptionKey("NSScreenNumber")
        return (deviceDescription[key] as? NSNumber)?.uint32Value ?? 0
    }

    /// CoreGraphics UUID — survives reboot better than CGDirectDisplayID.
    var displayUUIDString: String? {
        guard let ref = CGDisplayCreateUUIDFromDisplayID(displayID) else { return nil }
        let uuid = ref.takeRetainedValue()
        return CFUUIDCreateString(nil, uuid) as String
    }

    var menuLabel: String {
        var name = localizedName
        if name.isEmpty { name = "显示器" }
        // System arrangement “main” (menu bar), not the wallpaper target.
        if self == NSScreen.main { name += "（系统主屏）" }
        let px = Int(frame.width * backingScaleFactor)
        let py = Int(frame.height * backingScaleFactor)
        return "\(name)  \(px)×\(py)"
    }
}

enum DisplayChoice {
    static let defaultsKey = "wallpaperDisplayID"
    static let nameKey = "wallpaperDisplayName"
    static let uuidKey = "wallpaperDisplayUUID"

    /// Resolve wallpaper target without trusting volatile CGDirectDisplayID alone.
    /// After reboot the same ID can move to another panel (Studio id=8 → Cintiq id=8);
    /// matching ID first used to overwrite the saved name and stick on the wrong screen.
    static func resolveScreen() -> NSScreen {
        let screens = NSScreen.screens
        guard !screens.isEmpty else {
            return NSScreen.main ?? NSScreen.screens[0]
        }
        let storedID = UInt32(UserDefaults.standard.integer(forKey: defaultsKey))
        let storedName = (UserDefaults.standard.string(forKey: nameKey) ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let storedUUID = (UserDefaults.standard.string(forKey: uuidKey) ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)

        if !storedUUID.isEmpty,
           let match = screens.first(where: { $0.displayUUIDString == storedUUID }) {
            if match.displayID != storedID {
                Log.line("display id \(storedID) → \(match.localizedName) id=\(match.displayID) via uuid")
            }
            remember(match)
            return match
        }

        if !storedName.isEmpty,
           let match = screens.first(where: { $0.localizedName == storedName }) {
            if storedID != 0, match.displayID != storedID {
                Log.line("display id \(storedID) stale → remapped to \(match.localizedName) id=\(match.displayID)")
            }
            remember(match)
            return match
        }

        // ID only when it still names the same panel (or no name was ever saved).
        if storedID != 0, let match = screens.first(where: { $0.displayID == storedID }) {
            if storedName.isEmpty || match.localizedName == storedName {
                remember(match)
                return match
            }
            Log.line("display id \(storedID) reused by \(match.localizedName), wanted \(storedName) — ignore id")
        }

        // Temporary fallback only — do not overwrite the saved preference.
        let fallback = NSScreen.main ?? screens[0]
        Log.line("display selection missing (id=\(storedID) name=\(storedName) uuid=\(storedUUID)) → temp \(fallback.localizedName) id=\(fallback.displayID)")
        return fallback
    }

    static var selectedID: CGDirectDisplayID {
        get { resolveScreen().displayID }
        set {
            if let match = NSScreen.screens.first(where: { $0.displayID == newValue }) {
                remember(match)
            } else {
                UserDefaults.standard.set(Int(newValue), forKey: defaultsKey)
            }
        }
    }

    static func remember(_ screen: NSScreen) {
        UserDefaults.standard.set(Int(screen.displayID), forKey: defaultsKey)
        UserDefaults.standard.set(screen.localizedName, forKey: nameKey)
        if let uuid = screen.displayUUIDString, !uuid.isEmpty {
            UserDefaults.standard.set(uuid, forKey: uuidKey)
        }
    }

    static func select(_ screen: NSScreen) {
        remember(screen)
        let uuid = screen.displayUUIDString ?? "?"
        Log.line("wallpaper target → \(screen.localizedName) id=\(screen.displayID) uuid=\(uuid)")
    }

    static func screen() -> NSScreen {
        resolveScreen()
    }

    /// CSS px of the wallpaper canvas: logical layout size.
    /// Landscape design base 3840×2160; portrait design base 2160×2880.
    /// Final PNG pixels = CSS × magnification (native render, not bitmap stretch).
    static func wallpaperCSSSize(pixelWidth: Int, pixelHeight: Int) -> (width: Int, height: Int) {
        let w = max(320, pixelWidth)
        let h = max(200, pixelHeight)
        return (w, h)
    }

    static func clampWallpaperMagnification(_ n: Int) -> Int {
        min(4, max(1, n))
    }

    static func plannerCanvasCSSWidth() -> Int {
        Int(screen().frame.width.rounded())
    }

    static func plannerCanvasCSSHeight() -> Int {
        Int(screen().frame.height.rounded())
    }

    static func plannerCanvasPixelWidth() -> Int {
        let s = screen()
        return Int((s.frame.width * s.backingScaleFactor).rounded())
    }

    static func plannerCanvasPixelHeight() -> Int {
        let s = screen()
        return Int((s.frame.height * s.backingScaleFactor).rounded())
    }

    /// Stable per-display keys (UUID / name). Legacy keys used volatile CGDirectDisplayID.
    private static func stableSettingKeys(prefix: String, screen: NSScreen) -> [String] {
        var keys: [String] = []
        if let uuid = screen.displayUUIDString, !uuid.isEmpty {
            keys.append("\(prefix).uuid.\(uuid)")
        }
        let name = screen.localizedName.trimmingCharacters(in: .whitespacesAndNewlines)
        if !name.isEmpty {
            keys.append("\(prefix).name.\(name)")
        }
        keys.append("\(prefix).\(screen.displayID)")
        return keys
    }

    static func wallpaperPixelKey(_ id: CGDirectDisplayID = selectedID) -> String {
        if let screen = NSScreen.screens.first(where: { $0.displayID == id }) {
            return stableSettingKeys(prefix: "wallpaperPixelSize", screen: screen)[0]
        }
        return "wallpaperPixelSize.\(id)"
    }

    static let plannerPixelWidth = 3840
    static let plannerPixelHeight = 2160
    static let plannerPortraitPixelWidth = 2160
    static let plannerPortraitPixelHeight = 2880
    /// Wrong portrait base briefly shipped; migrate stored sizes.
    private static let plannerPortraitLegacyHeight = 2180

    private static func readStringSetting(prefix: String, screen: NSScreen) -> String? {
        let defaults = UserDefaults.standard
        for key in stableSettingKeys(prefix: prefix, screen: screen) {
            if let raw = defaults.string(forKey: key), !raw.isEmpty { return raw }
        }
        return nil
    }

    private static func readIntSetting(prefix: String, screen: NSScreen) -> Int {
        let defaults = UserDefaults.standard
        for key in stableSettingKeys(prefix: prefix, screen: screen) {
            let n = defaults.integer(forKey: key)
            if n != 0 { return n }
        }
        return 0
    }

    private static func writeSetting(prefix: String, screen: NSScreen, string: String?, int: Int?) {
        let defaults = UserDefaults.standard
        let keys = stableSettingKeys(prefix: prefix, screen: screen)
        guard let primary = keys.first else { return }
        let legacy = "\(prefix).\(screen.displayID)"
        if let string {
            defaults.set(string, forKey: primary)
            defaults.set(string, forKey: legacy)
        }
        if let int {
            defaults.set(int, forKey: primary)
            defaults.set(int, forKey: legacy)
        }
    }

    static func wallpaperPixelSize() -> (width: Int, height: Int) {
        let screen = resolveScreen()
        if let raw = readStringSetting(prefix: "wallpaperPixelSize", screen: screen) {
            let parts = raw.split(separator: "x")
            if parts.count == 2, let w = Int(parts[0]), let h = Int(parts[1]), w >= 320, h >= 200 {
                if w == plannerPortraitPixelWidth && h == plannerPortraitLegacyHeight {
                    setWallpaperPixelSize(width: w, height: plannerPortraitPixelHeight)
                    return (w, plannerPortraitPixelHeight)
                }
                // Migrate volatile-ID value onto stable key.
                writeSetting(prefix: "wallpaperPixelSize", screen: screen, string: "\(w)x\(h)", int: nil)
                return (w, h)
            }
        }
        return (plannerPixelWidth, plannerPixelHeight)
    }

    static func setWallpaperPixelSize(width: Int, height: Int) {
        guard width >= 320, height >= 200 else { return }
        writeSetting(prefix: "wallpaperPixelSize", screen: resolveScreen(), string: "\(width)x\(height)", int: nil)
    }

    static func wallpaperMagnificationKey(_ id: CGDirectDisplayID = selectedID) -> String {
        if let screen = NSScreen.screens.first(where: { $0.displayID == id }) {
            return stableSettingKeys(prefix: "wallpaperMagnification", screen: screen)[0]
        }
        return "wallpaperMagnification.\(id)"
    }

    static func wallpaperMagnification() -> Int {
        let screen = resolveScreen()
        let n = readIntSetting(prefix: "wallpaperMagnification", screen: screen)
        let value = clampWallpaperMagnification(n == 0 ? 1 : n)
        if n != 0 {
            writeSetting(prefix: "wallpaperMagnification", screen: screen, string: nil, int: value)
        }
        return value
    }

    static func setWallpaperMagnification(_ n: Int) {
        writeSetting(
            prefix: "wallpaperMagnification",
            screen: resolveScreen(),
            string: nil,
            int: clampWallpaperMagnification(n)
        )
    }
}

final class ServerManager {
    static let port = 18766
    static let rootURL = URL(string: "http://127.0.0.1:18766/")!
    static let statusURL = URL(string: "http://127.0.0.1:18766/api/status?device=mac")!
    static let stampURL = URL(string: "http://127.0.0.1:18766/api/stamp?device=mac")!
    /// B7 书历竖版设计默认（PORTRAIT_PIXEL_*). Used when lock PNG omits W×H.
    /// Not Hibreak wm size — wall canvas comes from the phone UI / nudge JSON.
    static let b7WallWidth = 2160
    static let b7WallHeight = 2880
    static let b7WallMag = 1

    static func statusURL(device: String) -> URL {
        let id = (device == "b7") ? "b7" : "mac"
        return URL(string: "http://127.0.0.1:18766/api/status?device=\(id)")!
    }

    private var process: Process?
    private var logHandle: FileHandle?
    private(set) var ownsProcess = false

    func isHealthy() async -> Bool {
        await statusOK()
    }

    func ensureRunning() async throws {
        if await statusOK() {
            if listenerMatchesProject() {
                Log.line("reuse existing :18766 cwd=\(projectRoot().path)")
                return
            }
            Log.line("existing :18766 is not the live project, replacing")
            clearStaleListener()
            try await Task.sleep(nanoseconds: 350_000_000)
        }
        if process?.isRunning != true {
            clearStaleListener()
            try await Task.sleep(nanoseconds: 350_000_000)
            if await statusOK(), listenerMatchesProject() {
                Log.line("reuse existing :18766 after clearing stale cwd=\(projectRoot().path)")
                return
            }
            try startOwned()
        }
        for _ in 0..<80 {
            if await statusOK() { return }
            if process?.isRunning != true {
                let tail = lastServerLog()
                throw ReadingLogError.server(
                    tail.isEmpty ? "本地服务未能启动。" : "本地服务未能启动：\(tail)"
                )
            }
            try await Task.sleep(nanoseconds: 250_000_000)
        }
        throw ReadingLogError.server("本地服务启动超时。")
    }

    func startOwned() throws {
        if let old = process, old.isRunning {
            old.terminate()
            old.waitUntilExit()
        }
        let python = try pythonPath()
        let root = projectRoot()
        let script = root.appendingPathComponent("app.py")
        guard FileManager.default.isReadableFile(atPath: script.path) else {
            throw ReadingLogError.server("找不到 app.py：\(script.path)")
        }
        try? FileManager.default.createDirectory(at: Log.dir, withIntermediateDirectories: true)
        let logFile = Log.dir.appendingPathComponent("server.log")
        if !FileManager.default.fileExists(atPath: logFile.path) {
            FileManager.default.createFile(atPath: logFile.path, contents: nil)
        }
        let handle = try FileHandle(forWritingTo: logFile)
        try handle.seekToEnd()
        logHandle = handle

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: python)
        proc.arguments = [script.path, "--owned"]
        proc.currentDirectoryURL = root
        var env = ProcessInfo.processInfo.environment
        let extras = [
            "/usr/bin", "/bin", "/usr/sbin", "/sbin",
            "/opt/homebrew/bin", "/usr/local/bin",
            "/Applications/Xcode.app/Contents/Developer/usr/bin",
            "/Library/Developer/CommandLineTools/usr/bin"
        ]
        let inherited = env["PATH"] ?? ""
        env["PATH"] = (extras + inherited.split(separator: ":").map(String.init)).joined(separator: ":")
        let pyPath = env["PYTHONPATH"] ?? ""
        env["PYTHONPATH"] = pyPath.isEmpty ? root.path : "\(root.path):\(pyPath)"
        env["READING_LOG_OWNED"] = "1"
        env["READING_LOG_NO_BROWSER"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if env["LANG"] == nil { env["LANG"] = "en_US.UTF-8" }
        proc.environment = env
        proc.standardInput = FileHandle.nullDevice
        proc.standardOutput = handle
        proc.standardError = handle
        proc.terminationHandler = { finished in
            Log.line("python exited pid=\(finished.processIdentifier) status=\(finished.terminationStatus)")
        }
        try proc.run()
        process = proc
        ownsProcess = true
        Log.line("started python \(python) pid=\(proc.processIdentifier) cwd=\(root.path) PYTHONPATH=\(root.path)")
    }

    func stopIfOwned() {
        guard ownsProcess else { return }
        process?.terminate()
        ownsProcess = false
        Log.line("stopped owned python")
    }

    /// Open Terminal with remote.command (ngrok) when no live 18766 tunnel.
    /// Only trusts ngrok's local API (not process-name heuristics — those false-skip).
    func openRemoteTunnelIfNeeded() {
        Log.line("openRemoteTunnelIfNeeded: begin")
        if ngrokHasReadingLogTunnelSync() {
            Log.line("remote tunnel already running, skip Terminal")
            return
        }
        let script = projectRoot().appendingPathComponent("remote.command")
        guard FileManager.default.isReadableFile(atPath: script.path) else {
            Log.line("remote.command missing: \(script.path)")
            return
        }
        // Prefer NSWorkspace so AppKit launches Terminal reliably from a GUI app.
        if let terminal = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.Terminal") {
            let cfg = NSWorkspace.OpenConfiguration()
            cfg.activates = true
            NSWorkspace.shared.open([script], withApplicationAt: terminal, configuration: cfg) { _, err in
                if let err {
                    Log.line("open remote.command failed: \(err.localizedDescription)")
                } else {
                    Log.line("opened Terminal for remote.command")
                }
            }
            return
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        task.arguments = ["-a", "Terminal", script.path]
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        do {
            try task.run()
            Log.line("opened Terminal for remote.command (open fallback)")
        } catch {
            Log.line("open remote.command failed: \(error.localizedDescription)")
        }
    }

    /// curl --max-time 1 against ngrok API; never blocks on URLSession.
    private func ngrokHasReadingLogTunnelSync() -> Bool {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
        task.arguments = [
            "-sf", "--max-time", "1",
            "http://127.0.0.1:4040/api/tunnels"
        ]
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = FileHandle.nullDevice
        do { try task.run() } catch { return false }
        task.waitUntilExit()
        guard task.terminationStatus == 0 else { return false }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let tunnels = obj["tunnels"] as? [[String: Any]] else { return false }
        for t in tunnels {
            let pub = (t["public_url"] as? String) ?? ""
            guard pub.hasPrefix("https://") else { continue }
            let conf = t["config"] as? [String: Any]
            let addr = (conf?["addr"] as? String) ?? ""
            if addr.contains("18766") || addr.hasSuffix(":18766") { return true }
        }
        return false
    }

    static let defaultProjectPath = "/Users/sunmuxin/Projects/reading-log"
    static let projectPathDefaultsKey = "projectPath"

    /// Prefer the live checkout so local edits stay current. The copy inside the
    /// app is only used when that checkout is not on this Mac.
    func projectRoot() -> URL {
        var candidates: [URL] = []
        if let stored = UserDefaults.standard.string(forKey: Self.projectPathDefaultsKey), !stored.isEmpty {
            candidates.append(URL(fileURLWithPath: (stored as NSString).expandingTildeInPath))
        }
        let fileStored = Log.dir.appendingPathComponent("project-path")
        if let raw = try? String(contentsOf: fileStored, encoding: .utf8) {
            let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                candidates.append(URL(fileURLWithPath: (trimmed as NSString).expandingTildeInPath))
            }
        }
        candidates.append(URL(fileURLWithPath: Self.defaultProjectPath))
        for url in candidates where FileManager.default.fileExists(atPath: url.appendingPathComponent("app.py").path) {
            if UserDefaults.standard.string(forKey: Self.projectPathDefaultsKey) != url.path {
                UserDefaults.standard.set(url.path, forKey: Self.projectPathDefaultsKey)
            }
            return url
        }
        if let bundled = bundledProjectRoot() {
            return bundled
        }
        return URL(fileURLWithPath: Self.defaultProjectPath)
    }

    private func bundledProjectRoot() -> URL? {
        guard let url = Bundle.main.resourceURL?.appendingPathComponent("reading-log"),
              FileManager.default.fileExists(atPath: url.appendingPathComponent("app.py").path) else { return nil }
        return url
    }

    private func statusOK() async -> Bool {
        var req = URLRequest(url: Self.statusURL)
        req.timeoutInterval = 1.5
        req.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (_, resp) = try await URLSession.shared.data(for: req)
            return (resp as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }

    private func lastServerLog() -> String {
        let url = Log.dir.appendingPathComponent("server.log")
        guard let data = try? Data(contentsOf: url),
              let text = String(data: data, encoding: .utf8) else { return "" }
        return text.split(whereSeparator: \.isNewline).suffix(4).joined(separator: " ")
    }

    private func listenerMatchesProject() -> Bool {
        let root = projectRoot().resolvingSymlinksInPath().path
        let pids = listeningPids()
        guard !pids.isEmpty else { return false }
        return pids.allSatisfy { pid in
            guard let cwd = processCwd(pid) else { return false }
            return (cwd as NSString).standardizingPath == root
        }
    }

    private func listeningPids() -> [Int32] {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        task.arguments = ["-nP", "-iTCP:\(Self.port)", "-sTCP:LISTEN", "-t"]
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = FileHandle.nullDevice
        do { try task.run() } catch { return [] }
        task.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard let text = String(data: data, encoding: .utf8) else { return [] }
        let myPid = ProcessInfo.processInfo.processIdentifier
        return text.split(whereSeparator: \.isNewline).compactMap { raw in
            guard let pid = Int32(raw.trimmingCharacters(in: .whitespaces)), pid > 1, pid != myPid else { return nil }
            return pid
        }
    }

    private func processCwd(_ pid: Int32) -> String? {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        task.arguments = ["-a", "-p", String(pid), "-d", "cwd", "-Fn"]
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = FileHandle.nullDevice
        do { try task.run() } catch { return nil }
        task.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard let text = String(data: data, encoding: .utf8) else { return nil }
        for line in text.split(whereSeparator: \.isNewline) {
            if line.hasPrefix("n") {
                return String(line.dropFirst())
            }
        }
        return nil
    }

    private func clearStaleListener() {
        let myPid = ProcessInfo.processInfo.processIdentifier
        for pid in listeningPids() where pid != myPid {
            kill(pid, SIGTERM)
            Log.line("cleared stale :18766 pid=\(pid)")
        }
    }

    private func pythonPath() throws -> String {
        if let bundled = Bundle.main.resourceURL?
            .appendingPathComponent("python/bin/python3").path,
           FileManager.default.isExecutableFile(atPath: bundled) {
            return bundled
        }
        var candidates = [
            "/Applications/Xcode.app/Contents/Developer/usr/bin/python3",
            "/Library/Developer/CommandLineTools/usr/bin/python3",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3"
        ]
        if let found = xcrunPython(), !candidates.contains(found) {
            candidates.insert(found, at: 0)
        }
        if let first = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) {
            return first
        }
        throw ReadingLogError.server("找不到 python3。")
    }

    private func xcrunPython() -> String? {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/xcrun")
        task.arguments = ["--find", "python3"]
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = FileHandle.nullDevice
        do { try task.run() } catch { return nil }
        task.waitUntilExit()
        guard task.terminationStatus == 0 else { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let path = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !path.isEmpty, FileManager.default.isExecutableFile(atPath: path) else { return nil }
        return path
    }
}

struct PlannerPad {
    /// Margins as percent of wallpaper width (left/right) or height (top/bottom), 0…50.
    var left: Int
    var right: Int
    var top: Int
    var bottom: Int
    var linkLR: Bool
    var linkTB: Bool

    static let fallback = PlannerPad(left: 1, right: 1, top: 4, bottom: 3, linkLR: true, linkTB: true)

    var clamped: PlannerPad {
        PlannerPad(
            left: min(50, max(0, left)),
            right: min(50, max(0, right)),
            top: min(50, max(0, top)),
            bottom: min(50, max(0, bottom)),
            linkLR: linkLR,
            linkTB: linkTB
        )
    }
}

func jsonInt(_ obj: Any?) -> Int? {
    if let n = obj as? Int { return n }
    if let n = obj as? NSNumber { return n.intValue }
    if let s = obj as? String, let n = Int(s) { return n }
    return nil
}

func jsonFlag(_ obj: Any?, default def: Bool = false) -> Bool {
    if let b = obj as? Bool { return b }
    if let n = obj as? NSNumber { return n.boolValue }
    if let s = obj as? String {
        switch s.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "1", "true", "on", "yes": return true
        case "0", "false", "off", "no": return false
        default: break
        }
    }
    return def
}

func plannerPadFromStatus(_ obj: [String: Any]?, orient: String? = nil) -> PlannerPad {
    let pad = obj?["plannerPad"] as? [String: Any]
    let o = (orient ?? "landscape").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    let slotKey = (o == "portrait") ? "portrait" : "landscape"
    let slot = pad?[slotKey] as? [String: Any]
    let fb = (slotKey == "portrait")
        ? PlannerPad(left: 2, right: 2, top: 8, bottom: 1, linkLR: true, linkTB: true)
        : PlannerPad.fallback
    let leftPct = jsonInt(slot?["left"]) ?? jsonInt(pad?["left"])
    let rightPct = jsonInt(slot?["right"]) ?? jsonInt(pad?["right"])
    let topPct = jsonInt(slot?["top"]) ?? jsonInt(pad?["top"])
    let bottomPct = jsonInt(slot?["bottom"]) ?? jsonInt(pad?["bottom"])
    // Prefer % fields; fall back to legacy CSS-px x/b (converted via design size).
    if leftPct != nil || rightPct != nil || topPct != nil || bottomPct != nil {
        let left = leftPct ?? rightPct ?? fb.left
        return PlannerPad(
            left: left,
            right: rightPct ?? left,
            top: topPct ?? fb.top,
            bottom: bottomPct ?? fb.bottom,
            linkLR: jsonFlag(slot?["linkLR"] ?? pad?["linkLR"], default: true),
            linkTB: jsonFlag(slot?["linkTB"] ?? pad?["linkTB"], default: true)
        ).clamped
    }
    let designW = (slotKey == "portrait") ? 2160 : 3840
    let designH = (slotKey == "portrait") ? 2880 : 2160
    let xPx = jsonInt(slot?["x"]) ?? jsonInt(pad?["x"]) ?? 40
    let bPx = jsonInt(slot?["b"]) ?? jsonInt(pad?["b"]) ?? 56
    let lr = min(50, max(0, Int((Double(xPx) / Double(designW) * 100.0).rounded())))
    let bottom = min(50, max(0, Int((Double(bPx) / Double(designH) * 100.0).rounded())))
    return PlannerPad(
        left: lr,
        right: lr,
        top: fb.top,
        bottom: bottom,
        linkLR: true,
        linkTB: true
    ).clamped
}

func calStatsFromStatus(_ obj: [String: Any]?) -> Bool {
    let stats = obj?["calStats"]
    if let dict = stats as? [String: Any] {
        return jsonFlag(dict["on"], default: false)
    }
    return jsonFlag(stats, default: false)
}

struct WallpaperPage {
    var url: URL
    var pad: PlannerPad
    var calStatsOn: Bool
    var orient: String
    var portraitRecRows: Int
}

func plannerOrientFromStatus(_ obj: [String: Any]?) -> String {
    let raw = obj?["plannerOrient"]
    if let dict = raw as? [String: Any] {
        let v = (dict["orient"] as? String) ?? (dict["value"] as? String) ?? ""
        let lower = v.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if lower == "portrait" || lower == "竖版" || lower == "vertical" { return "portrait" }
        return "landscape"
    }
    if let v = raw as? String {
        let lower = v.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if lower == "portrait" || lower == "竖版" || lower == "vertical" { return "portrait" }
    }
    return "landscape"
}

func portraitRecRowsFromStatus(_ obj: [String: Any]?) -> Int {
    let raw = obj?["portraitRecRows"]
    let n: Int?
    if let dict = raw as? [String: Any] {
        n = jsonInt(dict["rows"]) ?? jsonInt(dict["value"]) ?? jsonInt(dict["n"])
    } else {
        n = jsonInt(raw)
    }
    guard let rows = n else { return 5 }
    return max(3, min(20, rows))
}

func wallpaperPageURL(
    canvasCSSWidth: Int? = nil,
    canvasCSSHeight: Int? = nil,
    device: String = "mac",
    orientOverride: String? = nil
) async -> WallpaperPage? {
    let deviceId = (device == "b7") ? "b7" : "mac"
    var req = URLRequest(url: ServerManager.statusURL(device: deviceId))
    req.timeoutInterval = 4
    guard let (data, resp) = try? await URLSession.shared.data(for: req),
          ((resp as? HTTPURLResponse)?.statusCode ?? 0) < 400,
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        return nil
    }
    var orient = plannerOrientFromStatus(obj)
    if let raw = orientOverride?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased(),
       raw == "portrait" || raw == "landscape" {
        orient = raw
    }
    let pad = plannerPadFromStatus(obj, orient: orient)
    let statsOn = calStatsFromStatus(obj)
    let recRows = portraitRecRowsFromStatus(obj)
    var items = [
        URLQueryItem(name: "wallpaper", value: "1"),
        URLQueryItem(name: "device", value: deviceId),
        URLQueryItem(name: "padLeft", value: String(pad.left)),
        URLQueryItem(name: "padRight", value: String(pad.right)),
        URLQueryItem(name: "padTop", value: String(pad.top)),
        URLQueryItem(name: "padBottom", value: String(pad.bottom)),
        URLQueryItem(name: "stats", value: statsOn ? "1" : "0"),
        URLQueryItem(name: "orient", value: orient),
        URLQueryItem(name: "recRows", value: String(recRows)),
        URLQueryItem(name: "canvasWidth", value: String(canvasCSSWidth ?? DisplayChoice.plannerCanvasCSSWidth())),
        URLQueryItem(name: "canvasHeight", value: String(canvasCSSHeight ?? DisplayChoice.plannerCanvasCSSHeight()))
    ]
    let value = (obj["wallpaperValue"] as? String) ?? "current"
    let mode = (obj["wallpaperMode"] as? String) ?? ""
    let isRecent = value == "recent" || value == "prev-recent" || mode == "recent" || mode == "prev-recent"
    if isRecent {
        // 本周期 is always the current window. Do not follow status wallpaperOffset,
        // which used to mirror the calendar's live recentOffset.
        let offset = (value == "prev-recent" || mode == "prev-recent") ? 1 : 0
        items.append(URLQueryItem(name: "mode", value: "recent"))
        items.append(URLQueryItem(name: "offset", value: String(offset)))
    } else {
        let y = jsonInt(obj["wallpaperYear"])
        let m = jsonInt(obj["wallpaperMonth"])
        if let y, let m {
            items.append(URLQueryItem(name: "year", value: String(y)))
            items.append(URLQueryItem(name: "month", value: String(m)))
        } else {
            return nil
        }
    }
    var comps = URLComponents(string: "http://127.0.0.1:18766/")!
    comps.queryItems = items
    guard let url = comps.url else { return nil }
    return WallpaperPage(url: url, pad: pad, calStatsOn: statsOn, orient: orient, portraitRecRows: recRows)
}

func monthFingerprint() async -> String? {
    var stampReq = URLRequest(url: ServerManager.stampURL)
    stampReq.timeoutInterval = 3
    if let (data, resp) = try? await URLSession.shared.data(for: stampReq),
       ((resp as? HTTPURLResponse)?.statusCode ?? 0) < 400,
       let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
       let fp = obj["fingerprint"] as? String, !fp.isEmpty {
        return fp
    }
    let cal = Calendar.current
    let now = Date()
    let y = cal.component(.year, from: now)
    let m = cal.component(.month, from: now)
    guard let url = URL(string: "http://127.0.0.1:18766/api/month?year=\(y)&month=\(m)") else { return nil }
    var req = URLRequest(url: url)
    req.timeoutInterval = 8
    guard let (data, resp) = try? await URLSession.shared.data(for: req),
          ((resp as? HTTPURLResponse)?.statusCode ?? 0) < 400 else { return nil }
    return sha256Hex(data)
}

struct WallpaperState: Codable {
    var pngHash: String
    var fingerprint: String
    var path: String
    var pixelW: Int
    var pixelH: Int
}

enum WallpaperStateStore {
    static let url = Log.dir.appendingPathComponent("wallpaper-state.json")

    static func load() -> WallpaperState? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(WallpaperState.self, from: data)
    }

    static func save(_ state: WallpaperState) {
        try? FileManager.default.createDirectory(at: Log.dir, withIntermediateDirectories: true)
        if let data = try? JSONEncoder().encode(state) {
            try? data.write(to: url, options: .atomic)
        }
    }
}

/// One-shot resume gate for capture timeout races (cannot nest a class in a generic closure).
private final class CaptureTimeoutGate: @unchecked Sendable {
    private let lock = NSLock()
    private var finished = false
    func finish(_ body: () -> Void) {
        lock.lock()
        defer { lock.unlock() }
        guard !finished else { return }
        finished = true
        body()
    }
}

@MainActor
final class WallpaperRenderer: NSObject, WKNavigationDelegate {
    private var window: NSWindow?
    private var webView: WKWebView?
    private var navWait: CheckedContinuation<Void, Error>?
    private var rendering = false

    func captureCurrentMonth(
        on screen: NSScreen,
        pixelWidth: Int? = nil,
        pixelHeight: Int? = nil,
        magnification: Int? = nil,
        device: String = "mac",
        orientOverride: String? = nil
    ) async throws -> (Data, CGSize) {
        if rendering { throw ReadingLogError.render("正在生成壁纸") }
        rendering = true
        defer {
            rendering = false
            teardown()
        }
        let deviceId = (device == "b7") ? "b7" : "mac"
        let scale = max(screen.backingScaleFactor, 0.5)
        let nativeW = max(320, Int((screen.frame.width * scale).rounded()))
        let nativeH = max(200, Int((screen.frame.height * scale).rounded()))
        let defaultW = deviceId == "b7" ? ServerManager.b7WallWidth : nativeW
        let defaultH = deviceId == "b7" ? ServerManager.b7WallHeight : nativeH
        let defaultMag = deviceId == "b7" ? ServerManager.b7WallMag : DisplayChoice.wallpaperMagnification()
        let pxW = max(320, pixelWidth ?? defaultW)
        let pxH = max(200, pixelHeight ?? defaultH)
        let mag = DisplayChoice.clampWallpaperMagnification(magnification ?? defaultMag)
        // Layout CSS stays at logical W×H; contentsScale = mag → native W*mag × H*mag (no stretch).
        let css = DisplayChoice.wallpaperCSSSize(pixelWidth: pxW, pixelHeight: pxH)
        let outW = pxW * mag
        let outH = pxH * mag
        let design = CGSize(width: CGFloat(css.width), height: CGFloat(css.height))
        if design.width < 200 || design.height < 200 {
            throw ReadingLogError.render("所选屏幕尺寸无效")
        }

        // Dedicated offscreen WKWebView (?wallpaper=1) — never touch the live app UI.
        // (Host capture used to setMode → flash track/books to wallpaper month and back.)
        // PNG is written via /api/wallpaper/capture (no multi‑MB base64 over JS bridge).

        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        let wv = WKWebView(frame: NSRect(origin: .zero, size: design), configuration: config)
        wv.navigationDelegate = self
        wv.underPageBackgroundColor = NSColor(calibratedRed: 252 / 255, green: 251 / 255, blue: 248 / 255, alpha: 1)

        // Off-screen so macOS does not clamp the window to the display aspect.
        let origin = NSPoint(x: -20000, y: 0)
        let win = NSWindow(
            contentRect: NSRect(origin: origin, size: design),
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        win.isReleasedWhenClosed = false
        win.isOpaque = true
        // Keep mostly invisible but opaque enough that WebKit still runs rAF.
        // alpha≈0 / non-opaque offscreen windows could stall requestAnimationFrame
        // and hang capturePlannerPngBase64 forever (see waitPaint fallback in JS).
        win.alphaValue = 0.05
        win.backgroundColor = NSColor(calibratedRed: 252 / 255, green: 251 / 255, blue: 248 / 255, alpha: 1)
        win.hasShadow = false
        win.ignoresMouseEvents = true
        win.isExcludedFromWindowsMenu = true
        win.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .transient]
        win.level = .statusBar
        win.contentView = wv
        win.setContentSize(design)
        win.setFrameOrigin(origin)
        win.orderFrontRegardless()
        wv.setFrameSize(design)
        window = win
        webView = wv
        wv.layoutSubtreeIfNeeded()

        let fallbackOrient = (deviceId == "b7") ? "portrait" : "landscape"
        var page = URL(string: "http://127.0.0.1:18766/?wallpaper=1&device=\(deviceId)&padLeft=1&padRight=1&padTop=4&padBottom=3&stats=0&orient=\(fallbackOrient)&canvasWidth=\(css.width)&canvasHeight=\(css.height)")!
        var pad = (deviceId == "b7")
            ? PlannerPad(left: 2, right: 2, top: 6, bottom: 1, linkLR: true, linkTB: true)
            : PlannerPad.fallback
        var statsOn = false
        var orient = fallbackOrient
        var recRows = 5
        if let resolved = await wallpaperPageURL(
            canvasCSSWidth: css.width,
            canvasCSSHeight: css.height,
            device: deviceId,
            orientOverride: orientOverride
        ) {
            page = resolved.url
            pad = resolved.pad
            statsOn = resolved.calStatsOn
            orient = resolved.orient
            recRows = resolved.portraitRecRows
        }
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
            navWait = cont
            wv.load(URLRequest(url: page, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 15))
        }
        try await waitReady(wv)
        try await applyPlannerPad(wv, pad: pad)
        await applyCalStats(wv, on: statsOn)
        await applyOrient(wv, orient: orient)
        await applyPortraitRecRows(wv, rows: recRows)
        try await applyPlannerCanvasSize(wv, width: css.width, height: css.height)
        try await Task.sleep(nanoseconds: 350_000_000)

        // WKWebView.takeSnapshot ignores layer.contentsScale and returns screen-backed
        // pixels (often 1× for this offscreen window). pngData would then letterbox or
        // crop — never a native W*mag×H*mag render. Use the same html2canvas path as
        // 「保存图片」 so magnification is true device pixels, not a bitmap stretch.
        let png: Data
        var actualW = outW
        var actualH = outH
        do {
            let captured = try await withCaptureTimeout {
                try await self.capturePngViaJS(wv, mag: mag, cssWidth: css.width, cssHeight: css.height)
            }
            png = captured.0
            if let w = captured.1, let h = captured.2, w >= 320, h >= 200 {
                actualW = w
                actualH = h
            }
            Log.line("js capture \(png.count) bytes actual \(actualW)x\(actualH) target \(outW)x\(outH) mag=\(mag)")
        } catch {
            // Timeout tears down the offscreen webview to abort stuck JS — no snapshot left.
            if webView == nil {
                Log.line("js capture failed (\(error.localizedDescription)), webview torn down — not snapshotting")
                throw error
            }
            Log.line("js capture failed (\(error.localizedDescription)), falling back to takeSnapshot")
            png = try await capturePngViaSnapshot(wv, outW: outW, outH: outH, mag: mag)
        }
        let pixel = CGSize(width: actualW, height: actualH)
        Log.line("captured png device=\(deviceId) \(png.count) bytes \(actualW)x\(actualH) layout=\(css.width)x\(css.height) mag=\(mag)x orient=\(orient) padL=\(pad.left) padR=\(pad.right) padT=\(pad.top) padB=\(pad.bottom) stats=\(statsOn ? 1 : 0)")
        return (png, pixel)
    }

    /// Write PNG bytes for Hibreak lock (never used as Mac desktop image).
    static func writeB7Capture(_ png: Data) throws -> URL {
        try FileManager.default.createDirectory(at: Log.dir, withIntermediateDirectories: true)
        let dest = Log.dir.appendingPathComponent("wallpaper-b7-capture.png")
        try png.write(to: dest, options: .atomic)
        let stable = Log.dir.appendingPathComponent("wallpaper-b7.png")
        try png.write(to: stable, options: .atomic)
        Log.line("wrote b7 capture \(png.count) bytes → \(dest.lastPathComponent)")
        return dest
    }

    /// Race capture against a wall clock. Do **not** use `withThrowingTaskGroup`:
    /// when the sleep child throws, the group still waits for the capture child to
    /// finish — and `callAsyncJavaScript` ignores cancellation, so a stuck
    /// html2canvas / pixel-tone at 7680×4320 hung past the "90s timeout" forever.
    private func withCaptureTimeout<T>(_ operation: @escaping () async throws -> T) async throws -> T {
        let timeoutSeconds: TimeInterval = 90
        let gate = CaptureTimeoutGate()
        return try await withCheckedThrowingContinuation { (cont: CheckedContinuation<T, Error>) in
            Task { @MainActor in
                do {
                    let value = try await operation()
                    gate.finish { cont.resume(returning: value) }
                } catch {
                    gate.finish { cont.resume(throwing: error) }
                }
            }
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: UInt64(timeoutSeconds * 1_000_000_000))
                gate.finish { [weak self] in
                    // Tear down the offscreen WKWebView so the pending JS call aborts
                    // instead of keeping `busy` stuck on "请稍候".
                    self?.teardown()
                    cont.resume(throwing: ReadingLogError.render("生成壁纸超时（\(Int(timeoutSeconds)) 秒）"))
                }
            }
        }
    }

    /// Parse `{path,width,height}` from JS (PNG written via /api/wallpaper/capture).
    private func pngDataFromCaptureResult(_ raw: Any?) throws -> (Data, Int?, Int?) {
        if let dict = raw as? [String: Any], let path = dict["path"] as? String, !path.isEmpty {
            let url = URL(fileURLWithPath: path)
            guard let data = try? Data(contentsOf: url), !data.isEmpty else {
                throw ReadingLogError.render("无法读取壁纸截图文件")
            }
            let w = dict["width"] as? Int ?? (dict["width"] as? NSNumber)?.intValue
            let h = dict["height"] as? Int ?? (dict["height"] as? NSNumber)?.intValue
            return (data, w, h)
        }
        // Legacy: raw base64 string (pre-file path).
        if let b64 = raw as? String {
            let cleaned = b64
                .replacingOccurrences(of: "\n", with: "")
                .replacingOccurrences(of: "\r", with: "")
                .replacingOccurrences(of: "data:image/png;base64,", with: "")
            if let data = Data(base64Encoded: cleaned), !data.isEmpty {
                return (data, nil, nil)
            }
        }
        throw ReadingLogError.render("JS 截图未返回文件路径")
    }

    private func capturePngViaJS(_ webView: WKWebView, mag: Int, cssWidth: Int, cssHeight: Int) async throws -> (Data, Int?, Int?) {
        let script = """
        if (typeof window.capturePlannerPngBase64 !== 'function') {
          throw new Error('capturePlannerPngBase64 missing');
        }
        return await window.capturePlannerPngBase64(\(mag), \(cssWidth), \(cssHeight));
        """
        let raw = try await webView.callAsyncJavaScript(
            script,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        return try pngDataFromCaptureResult(raw)
    }

    private func capturePngViaSnapshot(_ wv: WKWebView, outW: Int, outH: Int, mag: Int) async throws -> Data {
        wv.wantsLayer = true
        wv.layer?.contentsScale = CGFloat(mag)
        window?.contentView?.wantsLayer = true
        window?.contentView?.layer?.contentsScale = CGFloat(mag)
        let snap = WKSnapshotConfiguration()
        snap.rect = wv.bounds
        snap.afterScreenUpdates = true
        let image: NSImage = try await withCheckedThrowingContinuation { cont in
            wv.takeSnapshot(with: snap) { img, err in
                if let img { cont.resume(returning: img) }
                else { cont.resume(throwing: err ?? ReadingLogError.render("截图失败")) }
            }
        }
        if let bit = image.representations.compactMap({ $0 as? NSBitmapImageRep }).first {
            Log.line("snapshot bitmap \(bit.pixelsWide)x\(bit.pixelsHigh) target \(outW)x\(outH) mag=\(mag)")
            // Only accept an exact pixel match — never stretch/letterbox up to mag.
            if bit.pixelsWide == outW, bit.pixelsHigh == outH, let png = pngData(from: image) {
                return png
            }
            if mag == 1, let png = pngData(from: image, pixelsWide: outW, pixelsHigh: outH) {
                return png
            }
        }
        throw ReadingLogError.render("截图像素 \(outW)×\(outH)@\(mag)x 未原生生成")
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        navWait?.resume()
        navWait = nil
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        navWait?.resume(throwing: error)
        navWait = nil
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        navWait?.resume(throwing: error)
        navWait = nil
    }

    private func waitReady(_ webView: WKWebView) async throws {
        let deadline = Date().addingTimeInterval(10)
        while Date() < deadline {
            let ready = (try? await webView.evaluateJavaScript("document.documentElement.dataset.wallpaperReady || ''")) as? String
            if ready == "1" {
                let err = (try? await webView.evaluateJavaScript("document.documentElement.dataset.wallpaperError || ''")) as? String
                if let err, !err.isEmpty { throw ReadingLogError.render(err) }
                return
            }
            try await Task.sleep(nanoseconds: 120_000_000)
        }
        throw ReadingLogError.render("月历页加载超时")
    }

    private func applyPlannerCanvasSize(_ webView: WKWebView, width: Int, height: Int) async throws {
        let js = """
        (function() {
          var w = \(width), h = \(height);
          if (window.applyPlannerCanvasSize) window.applyPlannerCanvasSize(w, h);
          else {
            document.documentElement.style.setProperty('--planner-canvas-width', w + 'px');
            document.documentElement.style.setProperty('--planner-canvas-height', h + 'px');
            document.documentElement.dataset.plannerCanvasWidth = String(w);
            document.documentElement.dataset.plannerCanvasHeight = String(h);
          }
        })();
        """
        _ = try? await webView.evaluateJavaScript(js)
    }

    private func applyPlannerPad(_ webView: WKWebView, pad: PlannerPad) async throws {
        let js = """
        (function() {
          var pad = {
            left: \(pad.left),
            right: \(pad.right),
            top: \(pad.top),
            bottom: \(pad.bottom),
            linkLR: \(pad.linkLR ? "true" : "false"),
            linkTB: \(pad.linkTB ? "true" : "false")
          };
          if (window.applyPlannerPad) window.applyPlannerPad(pad);
          else {
            var w = Number(document.documentElement.dataset.plannerCanvasWidth) || 3840;
            var h = Number(document.documentElement.dataset.plannerCanvasHeight) || 2160;
            function pctPx(p, dim) { return Math.max(0, Math.round(dim * Math.max(0, Math.min(50, p)) / 100)); }
            document.documentElement.style.setProperty('--planner-inset-left', pctPx(pad.left, w) + 'px');
            document.documentElement.style.setProperty('--planner-inset-right', pctPx(pad.right, w) + 'px');
            document.documentElement.style.setProperty('--planner-inset-top', pctPx(pad.top, h) + 'px');
            document.documentElement.style.setProperty('--planner-inset-bottom', pctPx(pad.bottom, h) + 'px');
          }
          document.documentElement.dataset.plannerPadLeft = String(pad.left);
          document.documentElement.dataset.plannerPadRight = String(pad.right);
          document.documentElement.dataset.plannerPadTop = String(pad.top);
          document.documentElement.dataset.plannerPadBottom = String(pad.bottom);
          document.documentElement.dataset.plannerPadLinkLR = pad.linkLR ? '1' : '0';
          document.documentElement.dataset.plannerPadLinkTB = pad.linkTB ? '1' : '0';
        })();
        """
        _ = try? await webView.evaluateJavaScript(js)
    }

    private func applyCalStats(_ webView: WKWebView, on: Bool) async {
        let flag = on ? "true" : "false"
        let js = """
        (function() {
          var on = \(flag);
          if (window.applyCalStats) window.applyCalStats(on);
          else document.body.classList.toggle('cal-stats-on', on);
        })();
        """
        _ = try? await webView.evaluateJavaScript(js)
    }

    private func applyOrient(_ webView: WKWebView, orient: String) async {
        let value = (orient == "portrait") ? "portrait" : "landscape"
        let js = """
        (function() {
          var orient = '\(value)';
          if (window.applyOrient) window.applyOrient(orient, { applySize: false });
          else {
            document.body.classList.toggle('orient-portrait', orient === 'portrait');
            document.body.classList.toggle('orient-landscape', orient !== 'portrait');
            document.documentElement.dataset.plannerOrient = orient;
          }
        })();
        """
        _ = try? await webView.evaluateJavaScript(js)
    }

    private func applyPortraitRecRows(_ webView: WKWebView, rows: Int) async {
        let n = max(3, min(20, rows))
        let js = """
        (function() {
          var rows = \(n);
          if (window.applyPortraitRecRows) window.applyPortraitRecRows(rows);
        })();
        """
        _ = try? await webView.evaluateJavaScript(js)
    }

    private func teardown() {
        webView?.navigationDelegate = nil
        window?.contentView = nil
        window?.close()
        webView = nil
        window = nil
        if let wait = navWait {
            navWait = nil
            wait.resume(throwing: ReadingLogError.render("已取消"))
        }
    }
}

func pngData(from image: NSImage) -> Data? {
    if let bitmap = image.representations.compactMap({ $0 as? NSBitmapImageRep }).first,
       let data = bitmap.representation(using: .png, properties: [:]) {
        return data
    }
    guard let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff) else { return nil }
    return rep.representation(using: .png, properties: [:])
}

func pngData(from image: NSImage, pixelsWide: Int, pixelsHigh: Int) -> Data? {
    let pw = max(1, pixelsWide)
    let ph = max(1, pixelsHigh)
    if let data = pngData(from: image), let rep = NSBitmapImageRep(data: data),
       rep.pixelsWide == pw, rep.pixelsHigh == ph {
        return data
    }
    guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: pw,
        pixelsHigh: ph,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    ) else {
        return pngData(from: image)
    }
    rep.size = NSSize(width: pw, height: ph)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let paper = NSColor(calibratedRed: 252 / 255, green: 251 / 255, blue: 248 / 255, alpha: 1)
    paper.setFill()
    NSRect(x: 0, y: 0, width: CGFloat(pw), height: CGFloat(ph)).fill()
    let srcW: Int
    let srcH: Int
    if let srcRep = image.representations.compactMap({ $0 as? NSBitmapImageRep }).first {
        srcW = srcRep.pixelsWide
        srcH = srcRep.pixelsHigh
    } else {
        srcW = max(1, Int(image.size.width.rounded()))
        srcH = max(1, Int(image.size.height.rounded()))
    }
    let dx = (pw - srcW) / 2
    let dy = (ph - srcH) / 2
    let blit = image.copy() as? NSImage ?? image
    blit.size = NSSize(width: srcW, height: srcH)
    NSGraphicsContext.current?.imageInterpolation = .none
    blit.draw(
        in: NSRect(x: CGFloat(dx), y: CGFloat(dy), width: CGFloat(srcW), height: CGFloat(srcH)),
        from: .zero,
        operation: .sourceOver,
        fraction: 1
    )
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])
}

enum ApplyOutcome {
    case applied(URL)
    case skipped(URL)
}

enum WallpaperSetter {
    static func path(for screen: NSScreen) -> String? {
        NSWorkspace.shared.desktopImageURL(for: screen)?.path
    }

    static func snapshotAll() -> [(id: CGDirectDisplayID, name: String, path: String)] {
        NSScreen.screens.map {
            ($0.displayID, $0.localizedName, path(for: $0) ?? "")
        }
    }

    static func selectedStillOurs(_ path: String) -> Bool {
        guard let current = self.path(for: DisplayChoice.screen()) else { return false }
        return current == path
    }

    static func apply(png: Data, fingerprint: String, pixel: CGSize, screen: NSScreen) throws -> ApplyOutcome {
        try FileManager.default.createDirectory(at: Log.dir, withIntermediateDirectories: true)
        let hash = sha256Hex(png)
        let hashed = Log.dir.appendingPathComponent("wallpaper-\(String(hash.prefix(16))).png")
        let stable = Log.dir.appendingPathComponent("wallpaper.png")
        if let prev = WallpaperStateStore.load(), prev.pngHash == hash {
            if FileManager.default.fileExists(atPath: prev.path), selectedStillOurs(prev.path) {
                Log.line("skip setDesktop, hash unchanged \(String(hash.prefix(12)))")
                var keep = prev
                keep.fingerprint = fingerprint
                WallpaperStateStore.save(keep)
                return .skipped(URL(fileURLWithPath: prev.path))
            }
        }
        if !FileManager.default.fileExists(atPath: hashed.path) {
            try png.write(to: hashed, options: .atomic)
        }
        try png.write(to: stable, options: .atomic)
        let othersBefore = snapshotAll().filter { $0.id != screen.displayID }
        let fill = NSColor(calibratedRed: 0.988, green: 0.984, blue: 0.973, alpha: 1)
        var opts = NSWorkspace.shared.desktopImageOptions(for: screen) ?? [:]
        opts.removeValue(forKey: .imageScaling)
        opts[.allowClipping] = true
        opts[.fillColor] = fill
        do {
            try NSWorkspace.shared.setDesktopImageURL(hashed, for: screen, options: opts)
        } catch {
            Log.line("NSWorkspace one-screen fail: \(error.localizedDescription)")
            throw ReadingLogError.wallpaper(error.localizedDescription)
        }
        WallpaperStateStore.save(WallpaperState(
            pngHash: hash,
            fingerprint: fingerprint,
            path: hashed.path,
            pixelW: Int(pixel.width),
            pixelH: Int(pixel.height)
        ))
        pruneOld(keeping: hashed)
        let after = snapshotAll()
        let targetNow = after.first { $0.id == screen.displayID }?.path ?? ""
        let othersAfter = after.filter { $0.id != screen.displayID }
        var othersOk = true
        for old in othersBefore {
            if let now = othersAfter.first(where: { $0.id == old.id }), now.path != old.path {
                othersOk = false
                Log.line("other display CHANGED \(old.name): \(old.path) -> \(now.path)")
            }
        }
        Log.line("set wallpaper on \(screen.localizedName) id=\(screen.displayID) -> \(hashed.path) targetNow=\(targetNow) othersUnchanged=\(othersOk)")
        return .applied(hashed)
    }

    static func pruneOld(keeping current: URL) {
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(at: Log.dir, includingPropertiesForKeys: nil) else { return }
        for url in files {
            let name = url.lastPathComponent
            guard name.hasPrefix("wallpaper-"), name.hasSuffix(".png"), url != current else { continue }
            if name == "wallpaper-b.png" || name == "wallpaper-test.png" {
                try? fm.removeItem(at: url)
                continue
            }
            if name != current.lastPathComponent {
                try? fm.removeItem(at: url)
            }
        }
    }

}
