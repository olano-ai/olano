import Foundation
import LocalAuthentication
import UIKit
import Capacitor

/// Which Olano deployment the app talks to.
///
/// JS name `OlanoShell`. Registered as an instance by
/// `OlanoBridgeViewController.capacitorDidLoad()` (it is part of the app, not an
/// npm package, so `packageClassList` does not list it). The saved origin is
/// also the only one OlanoHttp will call. The contract, mirrored by
/// OlanoShellPlugin.java:
///
/// - `getServer()` resolves `{ url: string | null }`
/// - `setServer({ url })` validates, saves and resolves `{ url }`, the origin
/// - `clearServer()` forgets it
/// - `getAppInfo()` resolves `{ version, build, platform }`
/// - `lockStatus()` resolves `{ available, kind }`: whether the phone has a
///   passcode, and how it unlocks ("face", "fingerprint", "optic" or "passcode")
/// - `unlock({ reason })` asks for Face ID / Touch ID with the passcode as the
///   fallback; resolves `{ ok: true }` or rejects with code "cancelled",
///   "unavailable" (no passcode set) or "failed" (the app lock, src/native/appLock.ts)
/// - `takeShared()` resolves `{ text, files: [{ name, mime, data, size }] }` for
///   what the share extension (ShareExtension/ShareViewController.swift) kept,
///   then forgets it; `data` is base64, empty for a file over 10 MB
/// - `setAgents({ agents: [{ name, display }], default? })` publishes the
///   roster Siri and the widgets may name (AppIntents.swift,
///   Shared/AgentEntity.swift), the agent they use when none was picked, and
///   refreshes the phrases Siri listens for
/// - `takeTalk()` resolves `{ agent: string | null }`: the agent "Talk to an
///   Agent" (Siri, the Action button) asked for, then forgets it; while the
///   page is listening the same request arrives as the `talk` event instead
/// - `takeDeepLink()` resolves `{ url: string | null }`: the app's own link
///   (`olano://open`, `olano://talk`) the app was opened with, read once and
///   then forgotten; the `deeplink` event says one is waiting while the page
///   runs. The one route the widgets, the controls and the Action button
///   reach the screens by (`DeepLinkRequest` below).
/// - `syncWidgets({ admin? })` hands the widget extension the session and
///   redraws the widgets; resolves `{ shared }`
@objc(OlanoShellPlugin)
public class OlanoShellPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "OlanoShellPlugin"
    public let jsName = "OlanoShell"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "getServer", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "setServer", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "clearServer", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "getAppInfo", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "lockStatus", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "unlock", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "takeShared", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "setAgents", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "takeTalk", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "takeDeepLink", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "syncWidgets", returnType: CAPPluginReturnPromise)
    ]

    /// Hands the widgets what they need (Shared/OlanoShared.swift): the
    /// deployment's address, the session cookie URLSession holds for it, and
    /// whether the person is an admin; then asks the system to redraw them.
    /// Called by the page when it signs in, when it comes to the front, when
    /// the agent list loads, when a turn ends and when it signs out
    /// (`src/native/widgets.ts`). Resolves `{ shared: bool }`: whether a
    /// session was handed over (false before sign-in, or when the keychain
    /// refused it).
    @objc func syncWidgets(_ call: CAPPluginCall) {
        let origin = ServerStore.serverURL
        SharedSession.origin = origin
        var shared = false
        if let origin, let url = URL(string: origin) {
            let cookies = HTTPCookieStorage.shared.cookies(for: url) ?? []
            let header = cookies.map { "\($0.name)=\($0.value)" }.joined(separator: "; ")
            if header.isEmpty {
                SharedSession.clear()
                SharedSession.origin = origin
            } else {
                shared = SharedSession.saveCookieHeader(header)
            }
        } else {
            SharedSession.clear()
        }
        if let admin = call.getBool("admin") {
            SharedSession.isAdmin = admin
        }
        WidgetRefresh.reloadAll()
        call.resolve(["shared": shared])
    }

    private var talkObserver: NSObjectProtocol?
    private var linkObserver: NSObjectProtocol?

    public override func load() {
        super.load()
        // "Talk to an Agent" while the app runs: handed to the page at once
        // when it listens; kept for `takeTalk()` (a cold start) otherwise.
        talkObserver = NotificationCenter.default.addObserver(forName: TalkRequest.posted, object: nil, queue: .main) { [weak self] _ in
            guard let self, self.hasListeners("talk"), let agent = TalkRequest.take() else { return }
            self.notifyListeners("talk", data: ["agent": agent])
        }
        // The app's own links. Capacitor posts this for a link that opens the
        // running app, and once for the link the app was launched with (after
        // the bridge's view appeared, so this observer is in place). It is kept
        // here, not read off Capacitor's App plugin: that plugin hands a
        // launch link's event to the first page listener only (the share
        // handler, which ignores it), and its `getLaunchUrl()` answers the
        // same link for the life of the process, so a page loaded again later
        // (OlanoBridgeViewController.reloadIfBlank) opened a conversation
        // nobody had just asked for.
        linkObserver = NotificationCenter.default.addObserver(forName: .capacitorOpenURL, object: nil, queue: .main) { [weak self] notification in
            guard let url = (notification.object as? [String: Any])?["url"] as? URL, DeepLinkRequest.isOurs(url) else { return }
            DeepLinkRequest.post(url)
            // A nudge, never the link itself: the page reads it with
            // `takeDeepLink()`, so a page that is being loaded again while the
            // link arrives still finds it waiting, and a running page never
            // sees it twice.
            if let self, self.hasListeners("deeplink") {
                self.notifyListeners("deeplink", data: [:])
            }
        }
    }

    deinit {
        if let talkObserver {
            NotificationCenter.default.removeObserver(talkObserver)
        }
        if let linkObserver {
            NotificationCenter.default.removeObserver(linkObserver)
        }
    }

    @objc func takeDeepLink(_ call: CAPPluginCall) {
        if let url = DeepLinkRequest.take() {
            call.resolve(["url": url.absoluteString])
        } else {
            call.resolve(["url": NSNull()])
        }
    }

    @objc func setAgents(_ call: CAPPluginCall) {
        let rows = (call.getArray("agents") ?? []).compactMap { $0 as? [String: Any] }
        let entries = rows.compactMap { row -> AgentRoster.Entry? in
            guard let name = (row["name"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines), !name.isEmpty else { return nil }
            let display = (row["display"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            return AgentRoster.Entry(name: name, display: display.isEmpty ? name : display)
        }
        AgentRoster.save(entries)
        let chosen = (call.getString("default") ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        DefaultAgent.name = entries.contains(where: { $0.name == chosen }) ? chosen : nil
        if #available(iOS 16.4, *) {
            OlanoShortcuts.updateAppShortcutParameters()
        }
        call.resolve()
    }

    @objc func takeTalk(_ call: CAPPluginCall) {
        if let agent = TalkRequest.take() {
            call.resolve(["agent": agent])
        } else {
            call.resolve(["agent": NSNull()])
        }
    }

    @objc func getServer(_ call: CAPPluginCall) {
        if let url = ServerStore.serverURL {
            call.resolve(["url": url])
        } else {
            call.resolve(["url": NSNull()])
        }
    }

    @objc func setServer(_ call: CAPPluginCall) {
        let origin: String
        do {
            origin = try ServerURL.normalize(call.getString("url") ?? "", allowHTTP: ServerURL.allowsHTTP)
        } catch let error as ServerURL.Invalid {
            call.reject(error.message, "INVALID_URL")
            return
        } catch {
            call.reject("That is not a valid web address.", "INVALID_URL")
            return
        }
        ServerStore.serverURL = origin
        SharedSession.origin = origin
        call.resolve(["url": origin])
    }

    @objc func clearServer(_ call: CAPPluginCall) {
        ServerStore.serverURL = nil
        SharedSession.clear()
        WidgetRefresh.reloadAll()
        call.resolve()
    }

    @objc func getAppInfo(_ call: CAPPluginCall) {
        let info = Bundle.main.infoDictionary ?? [:]
        call.resolve([
            "version": info["CFBundleShortVersionString"] as? String ?? "",
            "build": info["CFBundleVersion"] as? String ?? "",
            "platform": "ios"
        ])
    }

    /// Larger files are refused here, before they are read: a message carries at most 10 MB.
    private static let maxSharedBytes = 10 * 1024 * 1024

    @objc func takeShared(_ call: CAPPluginCall) {
        DispatchQueue.global(qos: .userInitiated).async {
            var texts: [String] = []
            var files: [[String: Any]] = []
            let manager = FileManager.default
            if let group = manager.containerURL(forSecurityApplicationGroupIdentifier: "group.ai.olano.app") {
                let shared = group.appendingPathComponent("Shared", isDirectory: true)
                let folders = (try? manager.contentsOfDirectory(at: shared, includingPropertiesForKeys: [.creationDateKey])) ?? []
                let ordered = folders.sorted {
                    let a = (try? $0.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? .distantPast
                    let b = (try? $1.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? .distantPast
                    return a < b
                }
                for folder in ordered {
                    defer { try? manager.removeItem(at: folder) }
                    guard let data = try? Data(contentsOf: folder.appendingPathComponent("manifest.json")),
                          let manifest = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
                    if let text = manifest["text"] as? String, !text.isEmpty { texts.append(text) }
                    for entry in manifest["files"] as? [[String: String]] ?? [] {
                        let name = entry["name"] ?? "Shared"
                        let url = folder.appendingPathComponent(entry["file"] ?? name)
                        let size = ((try? manager.attributesOfItem(atPath: url.path)[.size]) as? NSNumber)?.intValue ?? 0
                        var file: [String: Any] = ["name": name, "mime": entry["mime"] ?? "application/octet-stream", "size": size]
                        if size <= Self.maxSharedBytes, let bytes = try? Data(contentsOf: url) {
                            file["data"] = bytes.base64EncodedString()
                        } else {
                            file["data"] = ""
                        }
                        files.append(file)
                    }
                }
            }
            call.resolve(["text": texts.joined(separator: "\n"), "files": files])
        }
    }

    @objc func lockStatus(_ call: CAPPluginCall) {
        let context = LAContext()
        // Biometrics or the passcode: the policy the lock asks with.
        let available = context.canEvaluatePolicy(.deviceOwnerAuthentication, error: nil)
        var kind = available ? "passcode" : "none"
        if available && context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil) {
            switch context.biometryType {
            case .faceID: kind = "face"
            case .touchID: kind = "fingerprint"
            case .none: kind = "passcode"
            default: kind = "optic"
            }
        }
        call.resolve(["available": available, "kind": kind])
    }

    @objc func unlock(_ call: CAPPluginCall) {
        let context = LAContext()
        let reason = call.getString("reason") ?? "Unlock Olano"
        context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { ok, error in
            DispatchQueue.main.async {
                if ok {
                    call.resolve(["ok": true])
                    return
                }
                let code: String
                switch (error as? LAError)?.code {
                case .some(.userCancel), .some(.appCancel), .some(.systemCancel), .some(.userFallback):
                    code = "cancelled"
                case .some(.passcodeNotSet):
                    code = "unavailable"
                default:
                    code = "failed"
                }
                call.reject(error?.localizedDescription ?? "Not unlocked.", code)
            }
        }
    }

}

/// One of the app's own links (`olano://open?...`, `olano://talk?...`) on
/// its way to the screens, from a widget, a Control Center control, the
/// Action button or a Shortcut. One slot, the newest link winning: a person
/// who taps two widgets before the app is up wants the second.
enum DeepLinkRequest {
    private static var pending: URL?

    /// Whether a link is one the screens handle (src/siri/deepLinks.ts).
    /// `olano://share` is the share extension's and travels with the share
    /// itself (src/native/shareIn.ts); a `file://` document is Capacitor's.
    static func isOurs(_ url: URL) -> Bool {
        guard url.scheme?.lowercased() == "olano" else { return false }
        return (url.host ?? "").lowercased() != "share"
    }

    static func post(_ url: URL) {
        pending = url
    }

    static func take() -> URL? {
        defer { pending = nil }
        return pending
    }
}

/// Where the chosen deployment is kept: `UserDefaults.standard`, key `olano.serverURL`.
enum ServerStore {
    static let key = "olano.serverURL"

    static var serverURL: String? {
        get {
            guard let value = UserDefaults.standard.string(forKey: key), !value.isEmpty else { return nil }
            return value
        }
        set {
            if let value = newValue {
                UserDefaults.standard.set(value, forKey: key)
            } else {
                UserDefaults.standard.removeObject(forKey: key)
            }
        }
    }
}

/// Validates a deployment address and reduces it to its origin
/// (`scheme://host[:port]`, no path, no trailing slash). The Android twin is
/// ServerUrl.java; both refuse http:// outside debug builds.
enum ServerURL {
    struct Invalid: Error {
        let message: String
    }

    /// Plain http is for a debug build pointed at a box on the local network.
    static var allowsHTTP: Bool {
        #if DEBUG
        return true
        #else
        return false
        #endif
    }

    static func normalize(_ raw: String, allowHTTP: Bool) throws -> String {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            throw Invalid(message: "Enter the address of an Olano deployment.")
        }
        guard let components = URLComponents(string: text), let scheme = components.scheme?.lowercased() else {
            throw Invalid(message: "The address must start with https://.")
        }
        switch scheme {
        case "https":
            break
        case "http" where allowHTTP:
            break
        case "http":
            throw Invalid(message: "Only https:// addresses can be opened in this build of the app.")
        default:
            throw Invalid(message: "Only https:// addresses can be opened.")
        }
        guard components.user == nil, components.password == nil else {
            throw Invalid(message: "Leave the user name and password out of the address.")
        }
        // The percent-encoded form keeps IPv6 brackets ("[fe80::1]").
        guard let host = components.percentEncodedHost?.lowercased(), !host.isEmpty else {
            throw Invalid(message: "The address is missing a valid host name.")
        }

        var origin = URLComponents()
        origin.scheme = scheme
        origin.percentEncodedHost = host
        if let port = components.port, !(scheme == "https" && port == 443), !(scheme == "http" && port == 80) {
            origin.port = port
        }
        guard let string = origin.url?.absoluteString else {
            throw Invalid(message: "That is not a valid web address.")
        }
        return string
    }
}
