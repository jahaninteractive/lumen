// Lumen.app's shell: runs `python -m lumen serve` from inside the bundle, opens the
// workbench in the default browser, reopens it from the Dock and stops the server on Quit.
// It also offers the optional downloads (the bundled bootstrap.sh), a move to Applications
// and an uninstall that moves Lumen's own folders and the app to the Trash.
// The component list, URLs and sizes live in bootstrap.sh, never here.
//
// LUMEN_APP_HEADLESS=1 (the smoke script only): no browser, no window, no alert; the
// server's "Lumen is running at" line is printed on stdout and failures go to stderr.
// With LUMEN_APP_UNINSTALL=keep|data|exports|all as well, it runs the uninstall's trash steps
// (no Keychain, no server, the bundle kept) and exits; it needs LUMEN_VENDOR and LUMEN_LIBRARY set.
// An inherited LUMEN_VENDOR is honoured only headless: a normal launch always uses Application Support.
import AppKit

/// An environment value, with an empty one read as unset, as the Python package reads it.
func setting(_ name: String) -> String? {
    let value = ProcessInfo.processInfo.environment[name] ?? ""
    return value.isEmpty ? nil : value
}

let headless = setting("LUMEN_APP_HEADLESS") == "1"
// LUMEN_APP_SNAPSHOT=<dir> (a debug hook): renders the Setup and Uninstall windows to PNGs there
// without showing them, starts no server, downloads nothing and exits; needs LUMEN_VENDOR and LUMEN_LIBRARY.
let snapshotDir = setting("LUMEN_APP_SNAPSHOT")
let defaultPort = 8765
let urlLine = try! Regex(#"Lumen is running at (http://\S+)"#)
let resources = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources")

/// What the server and bootstrap.sh both run with: the bundle's tools first, and the vendor root.
let childEnvironment: [String: String] = {
    var environment = ProcessInfo.processInfo.environment
    environment["PATH"] = "\(resources.path)/bin:\(resources.path)/python/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    // Forced here, so the server and bootstrap.sh agree with the shell; inherited only for the smoke script.
    environment["LUMEN_VENDOR"] = (headless || snapshotDir != nil ? setting("LUMEN_VENDOR") : nil) ?? FileManager.default
        .homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Lumen").path
    environment["LUMEN_HELPER_DIR"] = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers").path
    environment["PYTHONDONTWRITEBYTECODE"] = "1"  // the bundle is signed; nothing may write into it
    return environment
}()

let vendorPath = childEnvironment["LUMEN_VENDOR"]!
let libraryPath = NSString(string: setting("LUMEN_LIBRARY") ?? "~/Pictures/Lumen Library").expandingTildeInPath
let vendorURL = URL(fileURLWithPath: vendorPath)
let libraryURL = URL(fileURLWithPath: libraryPath)

/// A GET that waits at most a second, so a launch or Quit never hangs on a wedged port.
func get(_ url: URL) -> Data? {
    var request = URLRequest(url: url, timeoutInterval: 1)
    request.cachePolicy = .reloadIgnoringLocalCacheData
    var result: Data?
    let done = DispatchSemaphore(value: 0)
    URLSession.shared.dataTask(with: request) { data, response, _ in
        if response != nil { result = data ?? Data() }
        done.signal()
    }.resume()
    _ = done.wait(timeout: .now() + 2)
    return result
}

/// Runs `process` with stdout and stderr in one pipe. Each line goes to `onLine`, then the exit
/// status to `onExit`, on the main thread; the exit waits for the end of the output, so no line is lost.
func launch(_ process: Process, onLine: @escaping (String) -> Void, onExit: @escaping (Int32) -> Void) throws {
    let output = Pipe()
    process.standardOutput = output
    process.standardError = output
    let done = DispatchGroup()
    done.enter()
    done.enter()
    var pending = Data()
    output.fileHandleForReading.readabilityHandler = { handle in
        let data = handle.availableData
        pending.append(data)
        var lines: [String] = []
        while let newline = pending.firstIndex(of: 0x0A) {
            lines.append(String(decoding: pending[pending.startIndex..<newline], as: UTF8.self))
            pending.removeSubrange(pending.startIndex...newline)
        }
        if data.isEmpty && !pending.isEmpty {
            lines.append(String(decoding: pending, as: UTF8.self))
            pending = Data()
        }
        if !lines.isEmpty { DispatchQueue.main.async { lines.forEach(onLine) } }
        if data.isEmpty {
            handle.readabilityHandler = nil
            done.leave()
        }
    }
    process.terminationHandler = { _ in done.leave() }
    done.notify(queue: .main) { onExit(process.terminationStatus) }
    try process.run()
}

/// The move offer: not in /Applications, and on a disk image, translocated or somewhere read-only.
func shouldOfferMove(_ path: String, parentWritable: Bool) -> Bool {
    if path.hasPrefix("/Applications/") { return false }
    return path.hasPrefix("/Volumes/") || path.contains("/AppTranslocation/") || !parentWritable
}

final class Shell: NSObject, NSApplicationDelegate {
    var server: Process?
    // Held and never written or closed: the server exits when it closes, which is how a
    // crashed or force-quit app takes the server with it (serve --exit-with-parent).
    let serverInput = Pipe()
    var serverURL: URL?
    var recentLines: [String] = []
    var quitting = false
    var setup: Setup?
    var holdBrowser = false  // the first-run setup window is open; the browser waits for it
    var uninstaller: Uninstall?

    // MARK: Launch

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = buildMenu()
        if offerMove() { return }
        let firstRun = !UserDefaults.standard.bool(forKey: "setupShown")
        holdBrowser = firstRun
        startServer()
        if firstRun {
            UserDefaults.standard.set(true, forKey: "setupShown")
            showSetup()
        }
    }

    func startServer() {
        var port = defaultPort
        if headless {
            port = 0
        } else if let lumen = lumenOnDefaultPort() {
            if lumen {
                alreadyRunning()
                return
            }
            port = 0  // something else holds the port
        }

        let process = Process()
        process.executableURL = resources.appendingPathComponent("python/bin/python3")
        process.arguments = ["-I", "-B", "-m", "lumen", "serve", "--port", String(port), "--exit-with-parent"]
        process.environment = childEnvironment
        process.standardInput = serverInput
        do {
            try launch(process, onLine: serverLine, onExit: serverEnded)
        } catch {
            fail("Lumen could not start.", detail: error.localizedDescription)
        }
        server = process
    }

    /// What answers on the default port: nil for nothing, true when it is a Lumen server.
    func lumenOnDefaultPort() -> Bool? {
        guard let answer = get(URL(string: "http://127.0.0.1:\(defaultPort)/api/status")!) else { return nil }
        return ((try? JSONSerialization.jsonObject(with: answer)) as? [String: Any])?["version"] != nil
    }

    func serverLine(_ line: String) {
        recentLines.append(line)
        if recentLines.count > 20 { recentLines.removeFirst() }
        if serverURL == nil, let match = line.firstMatch(of: urlLine),
           let found = match.output[1].substring, let url = URL(string: String(found)) {
            serverURL = url
            if headless {
                print(line)
                fflush(stdout)
            } else if !holdBrowser {
                NSWorkspace.shared.open(url)
            }
        }
    }

    func serverEnded(_ status: Int32) {
        if quitting { return }
        let detail = recentLines.joined(separator: "\n")
        fail(serverURL == nil ? "Lumen could not start." : "Lumen stopped unexpectedly.", detail: detail)
    }

    func alreadyRunning() {
        let alert = NSAlert()
        alert.messageText = "Lumen is already running."
        alert.informativeText = "It was started outside this app, so quitting here will not stop it."
        alert.addButton(withTitle: "Open")
        alert.addButton(withTitle: "Quit")
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn {
            serverURL = URL(string: "http://127.0.0.1:\(defaultPort)")
            holdBrowser = false
            openLumen()
        } else {
            NSApp.terminate(nil)
        }
    }

    func fail(_ message: String, detail: String) -> Never {
        setup?.stop()
        if headless {
            FileHandle.standardError.write(Data("\(message)\n\(detail)\n".utf8))
        } else {
            let alert = NSAlert()
            alert.alertStyle = .critical
            alert.messageText = message
            alert.informativeText = detail
            NSApp.activate(ignoringOtherApps: true)
            alert.runModal()
        }
        exit(1)
    }

    // MARK: Move to Applications

    /// True when the app is moving itself and this copy must not start a server.
    func offerMove() -> Bool {
        let here = Bundle.main.bundleURL
        let parentWritable = FileManager.default.isWritableFile(atPath: here.deletingLastPathComponent().path)
        guard shouldOfferMove(here.path, parentWritable: parentWritable) else { return false }
        let alert = NSAlert()
        alert.messageText = "Move Lumen to your Applications folder?"
        alert.addButton(withTitle: "Move")
        alert.addButton(withTitle: "Not Now")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return false }

        let target = URL(fileURLWithPath: "/Applications/Lumen.app")
        if NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "")
            .contains(where: { $0.bundleURL?.path.hasPrefix("/Applications/") == true }) {
            let running = NSAlert()
            running.messageText = "Quit the Lumen that is already open, then open this one again."
            running.runModal()
            NSApp.terminate(nil)
            return true
        }
        do {
            if FileManager.default.fileExists(atPath: target.path) {
                try FileManager.default.trashItem(at: target, resultingItemURL: nil)
            }
            try FileManager.default.copyItem(at: here, to: target)
            // copyItem keeps the quarantine, and a quarantined copy runs translocated: it would offer the move forever.
            let unquarantine = Process()
            unquarantine.executableURL = URL(fileURLWithPath: "/usr/bin/xattr")
            unquarantine.arguments = ["-dr", "com.apple.quarantine", target.path]
            unquarantine.standardError = FileHandle.nullDevice
            if (try? unquarantine.run()) != nil { unquarantine.waitUntilExit() }  // a failure: there was none to remove
        } catch {
            let failed = NSAlert()
            failed.alertStyle = .warning
            failed.messageText = "Lumen could not be moved to Applications."
            failed.informativeText = error.localizedDescription
            failed.runModal()
            return false
        }
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.createsNewApplicationInstance = true  // this copy has the same identifier
        NSWorkspace.shared.openApplication(at: target, configuration: configuration) { _, error in
            DispatchQueue.main.async {
                if let error { self.fail("Lumen could not open the copy in Applications.", detail: error.localizedDescription) }
                NSApp.terminate(nil)
            }
        }
        return true
    }

    // MARK: Dock and menu

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openLumen()
        return false
    }

    /// The Dock icon opens the browser, which takes the menu bar away, so the Dock menu repeats it.
    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let menu = NSMenu()
        menu.addItem(withTitle: "Open Lumen", action: #selector(openLumen), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Set Up Lumen…", action: #selector(showSetup), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Uninstall Lumen…", action: #selector(showUninstall), keyEquivalent: "").target = self
        return menu
    }

    @objc func openLumen() {
        if let url = serverURL { NSWorkspace.shared.open(url) }
    }

    /// The standard panel: the icon, name and version come from the bundle.
    @objc func showAbout() {
        NSApp.activate(ignoringOtherApps: true)
        NSApp.orderFrontStandardAboutPanel(nil)
    }

    @objc func showSetup() {
        if setup == nil {
            setup = Setup(firstRun: holdBrowser, onClose: { [unowned self] in
                setup = nil
                if holdBrowser && !quitting {
                    holdBrowser = false
                    openLumen()
                }
            })
        }
        setup?.window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func buildMenu() -> NSMenu {
        let appMenu = NSMenu(title: "Lumen")
        appMenu.addItem(withTitle: "About Lumen", action: #selector(showAbout), keyEquivalent: "").target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Open Lumen", action: #selector(openLumen), keyEquivalent: "o").target = self
        appMenu.addItem(withTitle: "Set Up Lumen…", action: #selector(showSetup), keyEquivalent: "").target = self
        appMenu.addItem(withTitle: "Uninstall Lumen…", action: #selector(showUninstall), keyEquivalent: "").target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Lumen", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let fileMenu = NSMenu(title: "File")
        fileMenu.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        // No target: the first responder handles these, so the install log can be copied.
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        let menu = NSMenu()
        for submenu in [appMenu, fileMenu, editMenu] {
            let item = NSMenuItem()
            item.submenu = submenu
            menu.addItem(item)
        }
        return menu
    }

    // MARK: Quit

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard mayStopJobs("Quit") else { return .terminateCancel }
        stopServer()
        return .terminateNow
    }

    /// False when our server has a queued or running job and the photographer cancels.
    /// Asks nothing once the server is stopped, so Uninstall's own terminate does not ask twice.
    func mayStopJobs(_ action: String) -> Bool {
        guard let server, server.isRunning, let url = serverURL, let answer = get(url.appendingPathComponent("api/jobs")),
              let jobs = ((try? JSONSerialization.jsonObject(with: answer)) as? [String: Any])?["jobs"] as? [[String: Any]],
              jobs.contains(where: { ["queued", "running"].contains($0["status"] as? String ?? "") }) else { return true }
        let alert = NSAlert()
        alert.messageText = "A job is still running. \(action) anyway?"
        alert.addButton(withTitle: action)
        alert.addButton(withTitle: "Cancel")
        return alert.runModal() == .alertFirstButtonReturn
    }

    /// Sets `quitting` first, so the server's exit is not reported as a crash by `serverEnded`.
    func stopServer() {
        quitting = true
        setup?.stop()
        guard let server, server.isRunning else { return }
        server.interrupt()
        let deadline = Date().addingTimeInterval(10)
        while server.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
        if server.isRunning { server.terminate() }
    }

    // MARK: Uninstall

    @objc func showUninstall() {
        if uninstaller == nil {
            uninstaller = Uninstall(onUninstall: { [unowned self] in uninstall(data: $0, exports: $1) },
                                    onClose: { [unowned self] in uninstaller = nil })
        }
        uninstaller?.window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// The window's Uninstall: keys, server, then the trash steps and the bundle; quits either way.
    func uninstall(data: Bool, exports: Bool) {
        do {  // before the keys go and the server stops, so a refusal changes nothing
            try checkUninstallFolders()
        } catch {
            let alert = NSAlert()
            alert.messageText = "Lumen cannot uninstall these folders."
            alert.informativeText = error.localizedDescription
            alert.runModal()
            return
        }
        if server?.isRunning != true && lumenOnDefaultPort() == true {
            let alert = NSAlert()
            alert.messageText = "Another Lumen is still running."
            alert.informativeText = "Quit it first, then choose Uninstall Lumen… again."
            alert.runModal()
            return
        }
        guard mayStopJobs("Uninstall") else { return }
        var notes: [String] = []
        if !removeKeys() {
            notes.append("A saved service key could not be removed from the Keychain; remove it in Keychain Access under the name Lumen.")
        }
        stopServer()
        uninstaller?.window.orderOut(nil)
        let alert: NSAlert
        do {
            try trashLumen(data: data, exports: exports)
            try FileManager.default.trashItem(at: Bundle.main.bundleURL, resultingItemURL: nil)
            alert = uninstalled(data: data, exports: exports, notes: notes)
        } catch {
            alert = NSAlert()
            alert.alertStyle = .critical
            alert.messageText = "Lumen could not finish uninstalling."
            alert.informativeText = ([error.localizedDescription] + notes + ["Lumen will quit now. Open it again to try again."])
                .joined(separator: "\n\n")
        }
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
        NSApp.terminate(nil)
    }

    /// Removes the Base and Online keys through the package's own keychain module; false when
    /// either could not be confirmed gone. A key that was never saved is not a failure.
    func removeKeys() -> Bool {
        let process = Process()
        process.executableURL = resources.appendingPathComponent("python/bin/python3")
        process.arguments = ["-I", "-B", "-c", """
            from lumen import keychain
            failed = False
            for engine in keychain.KEY_ENGINES:
                try:
                    keychain.delete_key(engine)
                except RuntimeError:
                    failed = True
            raise SystemExit(failed)
            """]
        process.environment = childEnvironment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { return false }
        process.waitUntilExit()
        return process.terminationStatus == 0
    }
}

/// The closing message, built from what was moved; `notes` (a Keychain failure) follow it.
func uninstalled(data: Bool, exports: Bool, notes: [String]) -> NSAlert {
    var moved = ["Lumen", "its downloaded tools and data"]
    if data { moved.append("its working data") }
    if exports { moved.append("your exported files") }
    // "tools and data" holds its own "and", so a longer list takes a comma before the last.
    var text = moved.dropLast().joined(separator: ", ") + (moved.count > 2 ? "," : "") + " and \(moved.last!) are in the Trash."
    if !(data && exports) { text += " What you kept is still in \(NSString(string: libraryPath).abbreviatingWithTildeInPath)." }
    text += " Nothing is deleted until you empty the Trash. You can close the Lumen tab in your browser."
    let alert = NSAlert()
    alert.messageText = "Lumen was uninstalled."
    alert.informativeText = ([text] + notes).joined(separator: "\n\n")
    return alert
}

struct Refusal: LocalizedError {
    let errorDescription: String?
}

/// Throws unless both folders are absolute, neither is the home folder or one of its ancestors
/// (which covers /), and neither equals or holds the other.
func checkUninstallFolders() throws {
    func resolved(_ path: String) -> String { URL(fileURLWithPath: path).standardizedFileURL.resolvingSymlinksInPath().path }
    func holds(_ outer: String, _ inner: String) -> Bool { outer == inner || inner.hasPrefix(outer.hasSuffix("/") ? outer : outer + "/") }
    let home = resolved(FileManager.default.homeDirectoryForCurrentUser.path)
    let folders = [("Downloaded tools", vendorPath), ("Library", libraryPath)]
    for (name, path) in folders {
        if !path.hasPrefix("/") { throw Refusal(errorDescription: "Uninstall refused the \(name) folder \"\(path)\": it is not an absolute path.") }
        if holds(resolved(path), home) {
            throw Refusal(errorDescription: "Uninstall refused the \(name) folder \(path): it is your home folder or holds it.")
        }
    }
    if holds(resolved(vendorPath), resolved(libraryPath)) || holds(resolved(libraryPath), resolved(vendorPath)) {
        throw Refusal(errorDescription: "Uninstall refused the folders \(vendorPath) and \(libraryPath): one holds the other.")
    }
}

/// The trash steps shared by the window and the headless hook; throws at the first failure, so
/// nothing after it runs. Everything goes to the Trash: nothing here deletes a file.
func trashLumen(data: Bool, exports: Bool) throws {
    try checkUninstallFolders()
    let files = FileManager.default
    if files.fileExists(atPath: vendorURL.path) { try files.trashItem(at: vendorURL, resultingItemURL: nil) }
    let outputs = libraryURL.appendingPathComponent("outputs")
    if data && exports {
        if files.fileExists(atPath: libraryURL.path) { try files.trashItem(at: libraryURL, resultingItemURL: nil) }
    } else if data && files.fileExists(atPath: libraryURL.path) {
        for item in try files.contentsOfDirectory(at: libraryURL, includingPropertiesForKeys: nil)
        where item.lastPathComponent != "outputs" {
            try files.trashItem(at: item, resultingItemURL: nil)
        }
    } else if exports && files.fileExists(atPath: outputs.path) {
        try files.trashItem(at: outputs, resultingItemURL: nil)
    }
    UserDefaults.standard.removePersistentDomain(forName: Bundle.main.bundleIdentifier ?? "au.com.jahan-interactive.lumen")
}

/// Shows "nothing to remove" now, or "calculating…" now and the `du` size later on the main
/// thread, so a large library never holds up the window. True when any of `urls` exists.
@discardableResult
func measure(_ urls: [URL], _ show: @escaping (String) -> Void) -> Bool {
    let present = urls.filter { FileManager.default.fileExists(atPath: $0.path) }
    guard !present.isEmpty else {
        show("nothing to remove")
        return false
    }
    show("calculating…")
    DispatchQueue.global().async {
        let du = Process()
        du.executableURL = URL(fileURLWithPath: "/usr/bin/du")
        du.arguments = ["-skc"] + present.map(\.path)
        let output = Pipe()
        du.standardOutput = output
        du.standardError = FileHandle.nullDevice
        var size = "size unknown"
        if (try? du.run()) != nil {
            let text = String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            du.waitUntilExit()
            if let total = text.split(separator: "\n").last?.split(separator: "\t").first {
                size = Int64(total.trimmingCharacters(in: .whitespaces)).map { formatted($0 * 1024) } ?? size
            }
        }
        DispatchQueue.main.async { show(size) }
    }
    return true
}

// MARK: Window parts

func stack(_ views: [NSView], spacing: CGFloat, vertical: Bool = true) -> NSStackView {
    let stack = NSStackView(views: views)
    stack.orientation = vertical ? .vertical : .horizontal
    stack.alignment = vertical ? .leading : .centerY
    stack.spacing = spacing
    return stack
}

/// Secondary-colour text, wrapping at `width` when given.
func quiet(_ text: String, width: CGFloat? = nil, small: Bool = false) -> NSTextField {
    let field = width == nil ? NSTextField(labelWithString: text) : NSTextField(wrappingLabelWithString: text)
    field.textColor = .secondaryLabelColor
    if small { field.font = .systemFont(ofSize: NSFont.smallSystemFontSize) }
    if let width { field.widthAnchor.constraint(equalToConstant: width).isActive = true }
    return field
}

func separator(_ width: CGFloat) -> NSBox {
    let line = NSBox()
    line.boxType = .separator
    line.widthAnchor.constraint(equalToConstant: width).isActive = true
    return line
}

/// The app's icon beside a bold title and one secondary line, as a Mac app's own windows open.
func header(_ title: String, _ detail: String, width: CGFloat) -> NSView {
    let icon = NSImageView(image: NSApp.applicationIconImage)
    icon.setAccessibilityElement(false)  // decorative: the title says whose window this is
    icon.widthAnchor.constraint(equalToConstant: 64).isActive = true
    icon.heightAnchor.constraint(equalToConstant: 64).isActive = true
    let heading = NSTextField(labelWithString: title)
    heading.font = .boldSystemFont(ofSize: 17)
    heading.setAccessibilityRole(.staticText)
    let words = stack([heading, quiet(detail, width: width - 80)], spacing: 4)
    return stack([icon, words], spacing: 16, vertical: false)
}

/// A tick box with its size at the right edge and `below` (its explanation) indented beneath.
func option(_ box: NSButton, size: NSTextField, below: [NSView], width: CGFloat) -> NSStackView {
    let line = NSStackView()
    line.setViews([box], in: .leading)
    line.setViews([size], in: .trailing)
    line.widthAnchor.constraint(equalToConstant: width).isActive = true
    let details = stack(below, spacing: 6)
    details.edgeInsets = NSEdgeInsets(top: 0, left: 20, bottom: 0, right: 0)
    return stack([line, details], spacing: 3)
}

/// "about 50 MB", "310 MB", "6 GB" from bootstrap.sh's --list as bytes; 0 when it cannot be read.
func bytes(_ size: String) -> Int64 {
    guard let match = size.firstMatch(of: try! Regex(#"(\d+(?:\.\d+)?)\s*([MG])B"#)),
          let number = Double(match.output[1].substring ?? "") else { return 0 }
    return Int64(number * (match.output[2].substring == "G" ? 1e9 : 1e6))
}

func formatted(_ bytes: Int64) -> String { ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file) }

/// Free space on the volume that holds (or will hold) the downloads.
func freeSpace() -> Int64? {
    var folder = vendorURL
    while !FileManager.default.fileExists(atPath: folder.path) && folder.path != "/" { folder.deleteLastPathComponent() }
    return (try? folder.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]))?
        .volumeAvailableCapacityForImportantUsage
}

// MARK: Setup window

/// One tick box per line of `bootstrap.sh --list`; Install runs the script with the ticked keys.
/// The only key known here is `places`, whose row carries the country picker.
final class Setup: NSObject, NSWindowDelegate {
    let window = NSWindow(contentRect: .zero, styleMask: [.titled, .closable], backing: .buffered, defer: false)
    let firstRun: Bool  // the launch's own showing, which holds the browser: Lumen is installed, these are extras
    let onClose: () -> Void
    let rows = NSStackView()
    let progress = NSProgressIndicator()
    let space = quiet("")
    var sizes: [String: Int64] = [:]  // bytes per missing key, from --list
    let countriesLabel = NSTextField(labelWithString: "Countries you photograph in")
    let countries = NSPopUpButton(frame: .zero, pullsDown: true)
    let chosenLabel = NSTextField(wrappingLabelWithString: "None chosen")
    var chosen: Set<String> = []  // two-letter codes, as LUMEN_GEONAMES_COUNTRIES takes them
    let logView = NSTextView()
    let logScroll = NSScrollView()
    let message = NSTextField(wrappingLabelWithString: "")
    let install = NSButton(title: "Install", target: nil, action: nil)
    let dismiss = NSButton(title: "Not Now", target: nil, action: nil)
    var boxes: [(key: String, box: NSButton)] = []  // missing components only
    var places: NSButton?
    var ticked: [String: Bool] = [:]  // the photographer's choices, kept across a reload
    var script: Process?
    var skipped: [String] = []
    var lastLine = ""
    var closed = false

    init(firstRun: Bool, onClose: @escaping () -> Void) {
        self.firstRun = firstRun
        self.onClose = onClose
        super.init()
        let width: CGFloat = 440  // inside the margins
        countries.setAccessibilityTitleUIElement(countriesLabel)
        countries.addItem(withTitle: "Add a country…")  // a pull-down shows its first item as the title
        // macOS's own region list; these codes are groupings or places GeoNames has no country file for.
        let notCountries: Set<String> = ["AC", "CP", "CQ", "DG", "EA", "EU", "EZ", "IC", "QO", "TA", "UN"]
        let regions = Locale.Region.isoRegions.map(\.identifier).filter {
            $0.count == 2 && $0.allSatisfy { $0.isASCII && $0.isLetter } && !notCountries.contains($0)
        }
        for (name, code) in regions.map({ (Locale.current.localizedString(forRegionCode: $0) ?? $0, $0) })
            .sorted(by: { $0.0.localizedStandardCompare($1.0) == .orderedAscending }) {
            countries.addItem(withTitle: name)
            countries.lastItem?.representedObject = code
        }
        (countries.cell as? NSPopUpButtonCell)?.altersStateOfSelectedItem = false  // pickCountry toggles the tick itself
        countries.target = self
        countries.action = #selector(pickCountry)
        chosenLabel.widthAnchor.constraint(equalToConstant: width - 20).isActive = true
        chosenLabel.textColor = .secondaryLabelColor
        chosenLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        countriesLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        chosenLabel.setAccessibilityLabel("Chosen countries")
        logView.isEditable = false
        logView.font = .monospacedSystemFont(ofSize: NSFont.smallSystemFontSize, weight: .regular)
        logView.isVerticallyResizable = true
        logView.autoresizingMask = .width
        logView.setAccessibilityLabel("Install progress")
        logScroll.documentView = logView
        logScroll.hasVerticalScroller = true
        logScroll.borderType = .bezelBorder
        logScroll.isHidden = true
        logScroll.widthAnchor.constraint(equalToConstant: width).isActive = true
        logScroll.heightAnchor.constraint(equalToConstant: 140).isActive = true
        progress.style = .bar
        progress.isIndeterminate = true
        progress.isHidden = true
        progress.setAccessibilityLabel("Installing")
        progress.widthAnchor.constraint(equalToConstant: width).isActive = true
        message.widthAnchor.constraint(equalToConstant: width).isActive = true
        message.isHidden = true
        rows.orientation = .vertical
        rows.alignment = .leading
        rows.spacing = 14
        install.target = self
        install.action = #selector(installTicked)
        install.keyEquivalent = "\r"
        dismiss.target = window
        dismiss.action = #selector(NSWindow.performClose(_:))
        dismiss.keyEquivalent = "\u{1b}"
        let buttons = NSStackView()
        buttons.setViews([space], in: .leading)
        buttons.setViews([dismiss, install], in: .trailing)
        let ai = quiet("AI advice: connect a service in Settings, or see the guide to run it on this Mac.", width: width, small: true)
        let top = firstRun
            ? header("Welcome to Lumen", "Lumen is ready to use. These extras are optional; choose any to download now, or add them later from the Lumen menu.", width: width)
            : header("Set Up Lumen", "Choose what to download. You can add any of these later from the Lumen menu.", width: width)
        let content = NSStackView(views: [top, separator(width), rows, separator(width), ai, progress, logScroll, message, buttons])
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 16
        content.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)
        content.widthAnchor.constraint(equalToConstant: width + 40).isActive = true
        buttons.widthAnchor.constraint(equalToConstant: width).isActive = true
        window.title = "Set Up Lumen"
        window.contentView = content
        window.isReleasedWhenClosed = false
        window.delegate = self
        reload()
        window.layoutIfNeeded()  // so center() places the window at its real size
        window.center()
    }

    /// Rebuilds the tick boxes from `--list`, which only reads.
    func reload() {
        rows.arrangedSubviews.forEach { $0.removeFromSuperview() }
        boxes = []
        sizes = [:]
        places = nil
        let list = Process()
        list.executableURL = URL(fileURLWithPath: "/bin/zsh")
        list.arguments = [resources.appendingPathComponent("bootstrap.sh").path, "--downloads-only",
                          "--dest", childEnvironment["LUMEN_VENDOR"]!, "--list"]
        list.environment = childEnvironment
        list.standardInput = FileHandle.nullDevice
        let output = Pipe()
        list.standardOutput = output
        list.standardError = output
        var text = ""
        do {
            try list.run()
            text = String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            list.waitUntilExit()
        } catch {
            text = error.localizedDescription
        }
        if list.isRunning || list.terminationStatus != 0 {
            show("The list of downloads could not be read: \(text)")
        }
        for line in text.split(separator: "\n") {
            let field = line.split(separator: "\t", omittingEmptySubsequences: false).map(String.init)
            guard field.count == 6 else { continue }
            let (key, label, size, purpose, ready) = (field[0], field[1], field[2], field[3], field[4] == "ready")
            let box = NSButton(checkboxWithTitle: label, target: self, action: #selector(changed))
            box.setAccessibilityLabel(ready ? "\(label), installed" : "\(label), \(size)")  // the size beside it is not read
            box.state = ready || (ticked[key] ?? (field[5] == "on")) ? .on : .off
            box.isEnabled = !ready && script == nil
            box.setAccessibilityHelp(purpose)
            var below: [NSView] = [quiet(purpose, width: 420, small: true)]
            if key == "places" {
                places = box
                below += [stack([countriesLabel, countries], spacing: 8, vertical: false), chosenLabel]
            }
            rows.addArrangedSubview(option(box, size: quiet(ready ? "Installed" : size), below: below, width: 440))
            if !ready {
                boxes.append((key, box))
                sizes[key] = bytes(size)
            }
        }
        changed()
    }

    @objc func changed() {
        for (key, box) in boxes { ticked[key] = box.state == .on }
        install.isEnabled = script == nil && !tickedKeys.isEmpty
        countries.isEnabled = script == nil && places?.state == .on
        // Nothing left for Install to do: Done is the way out, and Return takes it.
        let done = script == nil && tickedKeys.isEmpty
        dismiss.title = firstRun ? "Open Lumen" : done ? "Done" : "Not Now"
        dismiss.keyEquivalent = done ? "\r" : "\u{1b}"
        install.keyEquivalent = done ? "" : "\r"
        let free = freeSpace().map { "\(formatted($0)) free" }
        space.stringValue = ([neededBytes > 0 ? "Needs about \(formatted(neededBytes))" : nil, free])
            .compactMap { $0 }.joined(separator: " · ")
    }

    /// Twice the ticked downloads: a disk image is downloaded, then its app copied out of it.
    var neededBytes: Int64 {
        2 * boxes.filter { $0.box.state == .on }.reduce(0) { $0 + (sizes[$1.key] ?? 0) }
    }

    /// Toggles the picked country's tick; the pull-down's title item is never picked.
    @objc func pickCountry() {
        guard let item = countries.selectedItem, let code = item.representedObject as? String else { return }
        item.state = item.state == .on ? .off : .on
        if item.state == .on { chosen.insert(code) } else { chosen.remove(code) }
        showChosen()
        changed()
    }

    func showChosen() {
        let names = countries.itemArray.filter { $0.state == .on }.map(\.title)
        chosenLabel.stringValue = names.isEmpty ? "None chosen" : names.joined(separator: ", ")
    }

    /// The ticked missing keys, plus `places` when it is installed and countries were chosen:
    /// the script skips the files already there and fetches the country files.
    var tickedKeys: [String] {
        var keys = boxes.filter { $0.box.state == .on }.map(\.key)
        if places != nil && !boxes.contains(where: { $0.key == "places" }) && !chosen.isEmpty {
            keys.append("places")
        }
        return keys
    }

    func show(_ text: String) {
        message.stringValue = text
        message.isHidden = text.isEmpty
    }

    @objc func installTicked() {
        let keys = tickedKeys
        guard script == nil, !keys.isEmpty else { return }
        if let free = freeSpace(), free < neededBytes {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "Not enough free space"
            alert.informativeText = "These need about \(formatted(neededBytes)), and this Mac has \(formatted(free)) free. "
                + "Free some space or untick something."
            alert.addButton(withTitle: "Cancel")  // the first button: the default, on Return
            alert.addButton(withTitle: "Install Anyway")
            guard alert.runModal() == .alertSecondButtonReturn else { return }
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = [resources.appendingPathComponent("bootstrap.sh").path, "--downloads-only",
                             "--dest", childEnvironment["LUMEN_VENDOR"]!, "--only", keys.joined(separator: ",")]
        var environment = childEnvironment
        environment["LUMEN_GEONAMES_COUNTRIES"] = chosen.sorted().joined(separator: " ")  // the script splits on spaces
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        skipped = []
        lastLine = ""
        logView.string = ""
        logScroll.isHidden = false
        progress.isHidden = false
        progress.startAnimation(nil)
        show("")
        do {
            try launch(process, onLine: { [weak self] in self?.add($0) }, onExit: { [weak self] in self?.finished($0) })
        } catch {
            progress.stopAnimation(nil)
            progress.isHidden = true
            show(error.localizedDescription)
            return
        }
        script = process
        boxes.forEach { $0.box.isEnabled = false }
        changed()
    }

    func add(_ line: String) {
        lastLine = line
        if line.hasPrefix("Skipped: ") { skipped.append(String(line.dropFirst("Skipped: ".count))) }
        logView.textStorage?.append(NSAttributedString(string: line + "\n", attributes: [
            .font: logView.font as Any, .foregroundColor: NSColor.textColor]))
        logView.scrollToEndOfDocument(nil)
    }

    func finished(_ status: Int32) {
        script = nil
        if closed { return }
        progress.stopAnimation(nil)
        progress.isHidden = true
        var text = status == 0 ? "" : lastLine
        if !skipped.isEmpty {
            text += (text.isEmpty ? "" : "\n\n") + "Not finished. Press Install to try again:\n" + skipped.joined(separator: "\n")
        }
        if status == 0 && skipped.isEmpty {  // the chosen countries are fetched: none is new any more
            chosen = []
            countries.itemArray.forEach { $0.state = .off }
            showChosen()
        }
        reload()
        if status == 0 && skipped.isEmpty {
            add(firstRun ? "Finished. Press Open Lumen to start." : "Finished.")
        }
        show(text)
        NSAccessibility.post(element: window, notification: .announcementRequested, userInfo: [
            .announcement: text.isEmpty ? "Install finished." : text, .priority: NSAccessibilityPriorityLevel.high.rawValue])
    }

    /// Stops a running install. zsh runs its TERM trap (which detaches a mounted disk image)
    /// only once its foreground child exits, so the children are signalled too.
    func stop() {
        guard let script, script.isRunning else { return }
        script.terminate()
        let children = Process()
        children.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        children.arguments = ["-TERM", "-P", String(script.processIdentifier)]
        try? children.run()
        children.waitUntilExit()
        let deadline = Date().addingTimeInterval(10)
        while script.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
    }

    func windowWillClose(_ notification: Notification) {
        closed = true
        stop()
        onClose()
    }
}

// MARK: Uninstall window

/// The window is the confirmation: what always goes, two library boxes that start off, and
/// Uninstall, which is not the default button. Cancel takes Escape.
final class Uninstall: NSObject, NSWindowDelegate {
    let window = NSWindow(contentRect: .zero, styleMask: [.titled, .closable], backing: .buffered, defer: false)
    let onUninstall: (Bool, Bool) -> Void
    let onClose: () -> Void
    let data = NSButton(checkboxWithTitle: "", target: nil, action: nil)
    let exports = NSButton(checkboxWithTitle: "", target: nil, action: nil)

    init(onUninstall: @escaping (Bool, Bool) -> Void, onClose: @escaping () -> Void) {
        self.onUninstall = onUninstall
        self.onClose = onClose
        super.init()
        let width: CGFloat = 440  // inside the margins
        let library = stack([NSTextField(labelWithString: "Your library stays where it is unless you tick below."),
                             quiet(NSString(string: libraryPath).abbreviatingWithTildeInPath, width: width, small: true)], spacing: 3)
        let outputs = libraryURL.appendingPathComponent("outputs")
        let working = ((try? FileManager.default.contentsOfDirectory(at: libraryURL, includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.lastPathComponent != "outputs" }
        var choices: [NSView] = []
        for (box, title, note, urls) in [
            (data, "Also remove my working data", "Ratings, picks, tags, crops and develop settings", working),
            (exports, "Also remove my exported files", "JPEGs, crops and astro results", [outputs])] {
            box.title = title
            box.setAccessibilityHelp(note)
            let size = quiet("")
            box.isEnabled = measure(urls) { [box] in
                size.stringValue = $0
                box.setAccessibilityLabel("\(title), \($0)")  // the size beside it is not read
            }
            choices.append(option(box, size: size, below: [quiet(note, small: true)], width: width))
        }
        let vendor = quiet("", width: width, small: true)
        let always = " Saved service keys are removed from your Keychain. oMLX and any AI models are left alone."
        measure([vendorURL]) { vendor.stringValue = "Downloaded tools and data: \($0).\(always)" }
        let go = NSButton(title: "Uninstall", target: self, action: #selector(uninstall))
        let cancel = NSButton(title: "Cancel", target: window, action: #selector(NSWindow.performClose(_:)))
        cancel.keyEquivalent = "\u{1b}"
        let buttons = NSStackView()
        buttons.setViews([cancel, go], in: .trailing)
        buttons.widthAnchor.constraint(equalToConstant: width).isActive = true
        let top = header("Uninstall Lumen", "Lumen and the tools and data it downloaded move to the Trash. "
                         + "Your camera originals are never touched.", width: width)
        let content = NSStackView(views: [top, separator(width), library, stack(choices, spacing: 12),
                                          separator(width), vendor, buttons])
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 16
        content.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)
        content.widthAnchor.constraint(equalToConstant: width + 40).isActive = true
        window.title = "Uninstall Lumen"
        window.contentView = content
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.initialFirstResponder = cancel
        window.layoutIfNeeded()
        window.center()
    }

    @objc func uninstall() {
        onUninstall(data.state == .on, exports.state == .on)
    }

    func windowWillClose(_ notification: Notification) { onClose() }
}

let shell = Shell()
if headless, let choice = setting("LUMEN_APP_UNINSTALL") {
    let choices = ["keep": (false, false), "data": (true, false), "exports": (false, true), "all": (true, true)]
    func refuse(_ reason: String) -> Never {
        FileHandle.standardError.write(Data("LUMEN_APP_UNINSTALL: \(reason)\n".utf8))
        exit(2)
    }
    guard setting("LUMEN_VENDOR") != nil, setting("LUMEN_LIBRARY") != nil else {
        refuse("refused: set both LUMEN_VENDOR and LUMEN_LIBRARY, so it can never reach the real folders")
    }
    guard let (data, exports) = choices[choice] else { refuse("expected keep, data, exports or all") }
    do {
        try trashLumen(data: data, exports: exports)
    } catch {
        FileHandle.standardError.write(Data("Lumen could not finish uninstalling.\n\(error.localizedDescription)\n".utf8))
        exit(1)
    }
    exit(0)
} else if let dir = snapshotDir {
    guard setting("LUMEN_VENDOR") != nil, setting("LUMEN_LIBRARY") != nil else {
        FileHandle.standardError.write(Data("LUMEN_APP_SNAPSHOT: refused: set both LUMEN_VENDOR and LUMEN_LIBRARY\n".utf8))
        exit(2)
    }
    NSApplication.shared.setActivationPolicy(.prohibited)  // never activated, never on screen
    func save(_ window: NSWindow, _ name: String) {
        window.layoutIfNeeded()
        window.displayIfNeeded()
        let view = window.contentView!
        guard let image = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { exit(1) }
        view.cacheDisplay(in: view.bounds, to: image)
        do {
            try image.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: dir).appendingPathComponent(name))
        } catch {
            FileHandle.standardError.write(Data("\(name): \(error.localizedDescription)\n".utf8))
            exit(1)
        }
    }
    shell.holdBrowser = true
    save(Setup(firstRun: true, onClose: {}).window, "setup-first-run.png")
    let setup = Setup(firstRun: false, onClose: {})
    save(setup.window, "setup.png")
    // A clean install's end, through the real path: nothing ticked, a few lines, then finished(0).
    setup.boxes.forEach { setup.ticked[$0.key] = false }
    setup.logScroll.isHidden = false
    ["Downloading https://download.geonames.org/export/dump/cities500.zip", "GeoNames cities500: ready",
     "Downloading Natural Earth layers", "Map: ready", "Lumen is installed."].forEach(setup.add)
    setup.finished(0)
    save(setup.window, "setup-finished.png")
    let uninstall = Uninstall(onUninstall: { _, _ in }, onClose: {})
    RunLoop.main.run(until: Date().addingTimeInterval(2))  // the du sizes arrive on the main queue
    save(uninstall.window, "uninstall.png")
    let done = uninstalled(data: true, exports: false, notes: [])
    done.layout()
    save(done.window, "uninstalled.png")
    exit(0)
} else if headless {
    // No NSApplication: nothing here may need the window server.
    shell.startServer()
    dispatchMain()
} else {
    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    app.delegate = shell
    app.run()
}
