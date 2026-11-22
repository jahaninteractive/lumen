// Lumen.app's shell: runs `python -m lumen serve` from inside the bundle, opens the
// workbench in the default browser, reopens it from the Dock and stops the server on Quit.
// It also offers the optional downloads (the bundled bootstrap.sh) and a move to Applications.
// The component list, URLs and sizes live in bootstrap.sh, never here.
//
// LUMEN_APP_HEADLESS=1 (the smoke script only): no browser, no window, no alert; the
// server's "Lumen is running at" line is printed on stdout and failures go to stderr.
import AppKit

let headless = ProcessInfo.processInfo.environment["LUMEN_APP_HEADLESS"] == "1"
let defaultPort = 8765
let urlLine = try! Regex(#"Lumen is running at (http://\S+)"#)
let resources = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources")

/// What the server and bootstrap.sh both run with: the bundle's tools first, and the vendor root.
let childEnvironment: [String: String] = {
    var environment = ProcessInfo.processInfo.environment
    environment["PATH"] = "\(resources.path)/bin:\(resources.path)/python/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    if environment["LUMEN_VENDOR"] == nil {  // an inherited one wins, for the smoke script
        environment["LUMEN_VENDOR"] = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Lumen").path
    }
    environment["LUMEN_HELPER_DIR"] = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers").path
    environment["PYTHONDONTWRITEBYTECODE"] = "1"  // the bundle is signed; nothing may write into it
    return environment
}()

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
        } else if let answer = get(URL(string: "http://127.0.0.1:\(defaultPort)/api/status")!) {
            let status = (try? JSONSerialization.jsonObject(with: answer)) as? [String: Any]
            if status?["version"] != nil {
                alreadyRunning()
                return
            }
            port = 0  // something else holds the port
        }

        let process = Process()
        process.executableURL = resources.appendingPathComponent("python/bin/python3")
        process.arguments = ["-m", "lumen", "serve", "--port", String(port), "--exit-with-parent"]
        process.environment = childEnvironment
        process.standardInput = serverInput
        do {
            try launch(process, onLine: serverLine, onExit: serverEnded)
        } catch {
            fail("Lumen could not start.", detail: error.localizedDescription)
        }
        server = process
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

    @objc func openLumen() {
        if let url = serverURL { NSWorkspace.shared.open(url) }
    }

    @objc func showSetup() {
        if setup == nil {
            setup = Setup(onClose: { [unowned self] in
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
        appMenu.addItem(withTitle: "Open Lumen", action: #selector(openLumen), keyEquivalent: "o").target = self
        appMenu.addItem(withTitle: "Set Up Lumen…", action: #selector(showSetup), keyEquivalent: "").target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Lumen", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let appItem = NSMenuItem()
        appItem.submenu = appMenu
        let menu = NSMenu()
        menu.addItem(appItem)
        return menu
    }

    // MARK: Quit

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if let server, server.isRunning, let url = serverURL, let answer = get(url.appendingPathComponent("api/jobs")),
           let jobs = ((try? JSONSerialization.jsonObject(with: answer)) as? [String: Any])?["jobs"] as? [[String: Any]],
           jobs.contains(where: { ["queued", "running"].contains($0["status"] as? String ?? "") }) {
            let alert = NSAlert()
            alert.messageText = "A job is still running. Quit anyway?"
            alert.addButton(withTitle: "Quit")
            alert.addButton(withTitle: "Cancel")
            if alert.runModal() != .alertFirstButtonReturn { return .terminateCancel }
        }
        quitting = true
        setup?.stop()
        guard let server, server.isRunning else { return .terminateNow }
        server.interrupt()
        let deadline = Date().addingTimeInterval(10)
        while server.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
        if server.isRunning { server.terminate() }
        return .terminateNow
    }
}

// MARK: Setup window

/// One tick box per line of `bootstrap.sh --list`; Install runs the script with the ticked keys.
/// The only key known here is `places`, whose row carries the country codes field.
final class Setup: NSObject, NSWindowDelegate {
    let window = NSWindow(contentRect: .zero, styleMask: [.titled, .closable], backing: .buffered, defer: false)
    let onClose: () -> Void
    let rows = NSStackView()
    let countriesLabel = NSTextField(labelWithString: "Countries you photograph in (two-letter codes, e.g. ZA AQ)")
    let countries = NSTextField()
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

    init(onClose: @escaping () -> Void) {
        self.onClose = onClose
        super.init()
        let width: CGFloat = 440  // inside the margins
        countries.setAccessibilityTitleUIElement(countriesLabel)
        countries.widthAnchor.constraint(equalToConstant: 200).isActive = true
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
        message.widthAnchor.constraint(equalToConstant: width).isActive = true
        message.isHidden = true
        rows.orientation = .vertical
        rows.alignment = .leading
        rows.spacing = 4
        install.target = self
        install.action = #selector(installTicked)
        install.keyEquivalent = "\r"
        dismiss.target = window
        dismiss.action = #selector(NSWindow.performClose(_:))
        dismiss.keyEquivalent = "\u{1b}"
        let buttons = NSStackView()
        buttons.setViews([dismiss, install], in: .trailing)
        let ai = NSTextField(wrappingLabelWithString: "AI advice: connect a service in Settings, or see the guide to run it on this Mac.")
        ai.widthAnchor.constraint(equalToConstant: width).isActive = true
        let content = NSStackView(views: [rows, ai, logScroll, message, buttons])
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 14
        content.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)
        content.widthAnchor.constraint(equalToConstant: width + 40).isActive = true
        buttons.widthAnchor.constraint(equalToConstant: width).isActive = true
        window.title = "Set Up Lumen"
        window.contentView = content
        window.isReleasedWhenClosed = false
        window.delegate = self
        reload()
        window.center()
    }

    func indented(_ views: [NSView]) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 4
        stack.edgeInsets = NSEdgeInsets(top: 0, left: 20, bottom: 6, right: 0)
        return stack
    }

    /// Rebuilds the tick boxes from `--list`, which only reads.
    func reload() {
        rows.arrangedSubviews.forEach { $0.removeFromSuperview() }
        boxes = []
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
            let box = NSButton(checkboxWithTitle: ready ? "\(label) — installed" : "\(label) (\(size))",
                               target: self, action: #selector(changed))
            box.state = ready || (ticked[key] ?? (field[5] == "on")) ? .on : .off
            box.isEnabled = !ready && script == nil
            box.setAccessibilityHelp(purpose)
            let note = NSTextField(labelWithString: purpose)
            note.textColor = .secondaryLabelColor
            var below: [NSView] = [note]
            if key == "places" {
                places = box
                below += [countriesLabel, countries]
            }
            rows.addArrangedSubview(box)
            rows.addArrangedSubview(indented(below))
            if !ready { boxes.append((key, box)) }
        }
        if boxes.isEmpty { dismiss.title = "Done" }
        changed()
    }

    @objc func changed() {
        for (key, box) in boxes { ticked[key] = box.state == .on }
        install.isEnabled = script == nil && boxes.contains { $0.box.state == .on }
        countries.isEnabled = script == nil && places?.state == .on
    }

    func show(_ text: String) {
        message.stringValue = text
        message.isHidden = text.isEmpty
    }

    @objc func installTicked() {
        let keys = boxes.filter { $0.box.state == .on }.map(\.key)
        guard script == nil, !keys.isEmpty else { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = [resources.appendingPathComponent("bootstrap.sh").path, "--downloads-only",
                             "--dest", childEnvironment["LUMEN_VENDOR"]!, "--only", keys.joined(separator: ",")]
        var environment = childEnvironment
        environment["LUMEN_GEONAMES_COUNTRIES"] = countries.stringValue.trimmingCharacters(in: .whitespaces)
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        skipped = []
        lastLine = ""
        logView.string = ""
        logScroll.isHidden = false
        show("")
        do {
            try launch(process, onLine: { [weak self] in self?.add($0) }, onExit: { [weak self] in self?.finished($0) })
        } catch {
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
        var text = status == 0 ? "" : lastLine
        if !skipped.isEmpty {
            text += (text.isEmpty ? "" : "\n\n") + "Not finished. Press Install to try again:\n" + skipped.joined(separator: "\n")
        }
        dismiss.title = "Done"
        reload()
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

let shell = Shell()
if headless {
    // No NSApplication: nothing here may need the window server.
    shell.startServer()
    dispatchMain()
} else {
    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    app.delegate = shell
    app.run()
}
