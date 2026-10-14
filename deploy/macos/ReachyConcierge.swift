import AppKit
import AVFoundation
import Foundation

// A stable signed bundle is the TCC-responsible parent of the Python child.
final class Launcher: NSObject, NSApplicationDelegate {
    var child: Process?
    var signalSource: DispatchSourceSignal?

    func applicationDidFinishLaunching(_ notification: Notification) {
        request(.video) { self.request(.audio) { self.startChild() } }
    }

    func request(_ kind: AVMediaType, completion: @escaping () -> Void) {
        switch AVCaptureDevice.authorizationStatus(for: kind) {
        case .authorized: completion()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: kind) { allowed in
                DispatchQueue.main.async {
                    if allowed { completion() } else { NSApplication.shared.terminate(nil) }
                }
            }
        default: NSApplication.shared.terminate(nil)
        }
    }

    func startChild() {
        let arguments = Array(CommandLine.arguments.dropFirst())
        guard arguments.count >= 2 else { NSApplication.shared.terminate(nil); return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: arguments[0])
        process.currentDirectoryURL = URL(fileURLWithPath: arguments[1])
        process.arguments = ["-m", "app.main"]
        var environment = ProcessInfo.processInfo.environment
        environment["OPENCV_AVFOUNDATION_SKIP_AUTH"] = "1"
        environment["HOST"] = "127.0.0.1"
        environment["PHYSICAL_UNLOCK_ENABLED"] = "false"
        environment["ACCESS_MODE"] = "shadow"
        environment["DOOR_STAGE"] = "0"
        process.environment = environment
        process.terminationHandler = { _ in DispatchQueue.main.async { NSApplication.shared.terminate(nil) } }
        signal(SIGTERM, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        source.setEventHandler { process.terminate() }
        source.resume()
        signalSource = source
        child = process
        do { try process.run() } catch { NSApplication.shared.terminate(nil) }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if let process = child, process.isRunning {
            process.terminate()
            return .terminateCancel
        }
        return .terminateNow
    }
}

let application = NSApplication.shared
let launcher = Launcher()
application.delegate = launcher
application.setActivationPolicy(.accessory)
application.run()
