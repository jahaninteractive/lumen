// Lumen.app's shell: runs `python -m lumen serve` from inside the bundle, opens the
// workbench in the default browser, reopens it from the Dock and stops the server on Quit.
// The component list, URLs and sizes live in bootstrap.sh, never here.
//
// LUMEN_APP_HEADLESS=1 (the smoke script only): no browser, no window, no alert; the
// server's "Lumen is running at" line is printed on stdout and failures go to stderr.
import AppKit

let headless = ProcessInfo.processInfo.environment["LUMEN_APP_HEADLESS"] == "1"
let defaultPort = 8765
let urlLine = try! Regex(#"Lumen is running at (http://\S+)"#)

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

final class Shell: NSObject, NSApplicationDelegate {
    var server: Process?
    // Held and never written or closed: the server exits when it closes, which is how a
    // crashed or force-quit app takes the server with it (serve --exit-with-parent).
    let serverInput = Pipe()
    var serverURL: URL?
    var recentLines: [String] = []
    var pending = Data()
    var quitting = false

    // MARK: Launch

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = buildMenu()
        startServer()
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

        let contents = Bundle.main.bundleURL.appendingPathComponent("Contents")
        let resources = contents.appendingPathComponent("Resources")
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "\(resources.path)/bin:\(resources.path)/python/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        if environment["LUMEN_VENDOR"] == nil {  // an inherited one wins, for the smoke script
            environment["LUMEN_VENDOR"] = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Application Support/Lumen").path
        }
        environment["LUMEN_HELPER_DIR"] = contents.appendingPathComponent("Helpers").path
        environment["PYTHONDONTWRITEBYTECODE"] = "1"  // the bundle is signed; nothing may write into it

        let process = Process()
        process.executableURL = resources.appendingPathComponent("python/bin/python3")
        process.arguments = ["-m", "lumen", "serve", "--port", String(port), "--exit-with-parent"]
        process.environment = environment
        process.standardInput = serverInput
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        output.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            DispatchQueue.main.async { self.read(data) }
        }
        process.terminationHandler = { finished in
            DispatchQueue.main.async { self.serverEnded(finished.terminationStatus) }
        }
        do {
            try process.run()
        } catch {
            fail("Lumen could not start.", detail: error.localizedDescription)
        }
        server = process
    }

    func read(_ data: Data) {
        pending.append(data)
        while let newline = pending.firstIndex(of: 0x0A) {
            let line = String(decoding: pending[pending.startIndex..<newline], as: UTF8.self)
            pending.removeSubrange(pending.startIndex...newline)
            recentLines.append(line)
            if recentLines.count > 20 { recentLines.removeFirst() }
            if serverURL == nil, let match = line.firstMatch(of: urlLine),
               let found = match.output[1].substring, let url = URL(string: String(found)) {
                serverURL = url
                if headless {
                    print(line)
                    fflush(stdout)
                } else {
                    NSWorkspace.shared.open(url)
                }
            }
        }
    }

    func serverEnded(_ status: Int32) {
        if quitting { return }
        if serverURL == nil {
            fail("Lumen could not start.", detail: recentLines.joined(separator: "\n"))
        }
        // ponytail: a server that dies after starting ends the app quietly; an alert can come with the setup window.
        FileHandle.standardError.write(Data((recentLines.joined(separator: "\n") + "\n").utf8))
        exit(status == 0 ? 1 : status)
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
            openLumen()
        } else {
            NSApp.terminate(nil)
        }
    }

    func fail(_ message: String, detail: String) -> Never {
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

    // MARK: Dock and menu

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openLumen()
        return false
    }

    @objc func openLumen() {
        if let url = serverURL { NSWorkspace.shared.open(url) }
    }

    func buildMenu() -> NSMenu {
        let appMenu = NSMenu(title: "Lumen")
        appMenu.addItem(withTitle: "Open Lumen", action: #selector(openLumen), keyEquivalent: "o").target = self
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
        guard let server, server.isRunning else { return .terminateNow }
        if let url = serverURL, let answer = get(url.appendingPathComponent("api/jobs")),
           let jobs = ((try? JSONSerialization.jsonObject(with: answer)) as? [String: Any])?["jobs"] as? [[String: Any]],
           jobs.contains(where: { ["queued", "running"].contains($0["status"] as? String ?? "") }) {
            let alert = NSAlert()
            alert.messageText = "A job is still running. Quit anyway?"
            alert.addButton(withTitle: "Quit")
            alert.addButton(withTitle: "Cancel")
            if alert.runModal() != .alertFirstButtonReturn { return .terminateCancel }
        }
        quitting = true
        server.interrupt()
        let deadline = Date().addingTimeInterval(10)
        while server.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
        if server.isRunning { server.terminate() }
        return .terminateNow
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
