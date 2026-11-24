import Foundation
import Capacitor

/// HTTP to the chosen deployment, from native code.
///
/// The app's screens are bundled (capacitor://localhost) and the deployment is
/// another origin, so the page cannot call it with fetch(): the deployment
/// sends no CORS headers and its session cookie is first-party to it alone.
/// URLSession has neither limit, keeps the session cookie in
/// HTTPCookieStorage.shared (persisted, it carries an expiry) and, unlike
/// CapacitorHttp, can hand a response over while it is still arriving, which
/// the chat's server-sent events need.
///
/// JS name `OlanoHttp`. The contract, mirrored by OlanoHttpPlugin.java:
///
/// - `request({ url, method?, headers?, body?, bodyEncoding?, responseType?, timeout? })`
///   resolves `{ status, headers, body, url }`; `body` is text, or base64 when
///   `responseType` is "base64" (images). A request `body` is sent as its
///   UTF-8 text, or as the bytes it decodes to when `bodyEncoding` is
///   "base64" (a file uploaded to an agent's workspace).
/// - `stream({ id, url, method?, headers?, body? })` resolves
///   `{ status, headers }` once the response starts, then emits `chunk`
///   events `{ id, data }` (base64, so a character split across two reads
///   survives) and one `end` event `{ id, error? }`.
/// - `cancel({ id })` ends a stream; its `end` event still follows.
/// - `clearCookies()` forgets every cookie of the saved deployment.
/// - `socketOpen({ id, url, headers? })` opens a WebSocket to an `/api/` path of
///   the deployment (`url` is its https:// address; the session cookie goes
///   with the handshake), then emits `socketOpen` `{ id }`, `socketMessage`
///   `{ id, binary, data }` (text, or base64 when `binary`) and one
///   `socketClose` `{ id, code, reason, error? }`. The live browser view is the
///   one user (its picture arrives as binary JPEG frames).
/// - `socketSend({ id, text })` sends a text message; `socketClose({ id })`
///   closes it (its `socketClose` event still follows).
///
/// Every URL must be on the deployment saved by OlanoShell. Two exceptions:
/// `GET <origin>/api/status` on any address, which the connect screen uses
/// to check an address before saving it, and `GET` of the app's own public
/// config, `appConfigURL` exactly (src/appConfig.ts), sent without cookies.
@objc(OlanoHttpPlugin)
public class OlanoHttpPlugin: CAPPlugin, CAPBridgedPlugin, URLSessionDataDelegate, URLSessionWebSocketDelegate {
    public let identifier = "OlanoHttpPlugin"
    public let jsName = "OlanoHttp"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "request", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "stream", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "cancel", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "clearCookies", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "socketOpen", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "socketSend", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "socketClose", returnType: CAPPluginReturnPromise)
    ]

    private struct OpenStream {
        let id: String
        var call: CAPPluginCall?
    }

    /// Guards `streams` and `tasksById`: plugin calls and delegate callbacks
    /// arrive on different threads.
    private let lock = NSLock()
    private var streams: [Int: OpenStream] = [:]
    private var tasksById: [String: URLSessionDataTask] = [:]
    /// Open WebSockets by id, and the id of each socket task (also under `lock`).
    private var socketsById: [String: URLSessionWebSocketTask] = [:]
    private var socketIds: [Int: String] = [:]

    private lazy var requestSession: URLSession = {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        // The delegate only refuses redirects; completion handlers do the rest.
        return URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }()

    private lazy var streamSession: URLSession = {
        let config = URLSessionConfiguration.default
        // A turn can sit on a slow tool for minutes between two frames.
        config.timeoutIntervalForRequest = 600
        config.timeoutIntervalForResource = 6 * 60 * 60
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        return URLSession(configuration: config, delegate: self, delegateQueue: queue)
    }()

    private lazy var socketSession: URLSession = {
        let config = URLSessionConfiguration.default
        config.httpCookieStorage = HTTPCookieStorage.shared
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        return URLSession(configuration: config, delegate: self, delegateQueue: queue)
    }()

    // MARK: - Plugin methods

    @objc func request(_ call: CAPPluginCall) {
        let request: URLRequest
        do {
            request = try buildRequest(call, timeout: (call.getDouble("timeout") ?? 30000) / 1000)
        } catch let error as Refused {
            call.reject(error.message, "REFUSED")
            return
        } catch {
            call.reject("Bad request.", "REFUSED")
            return
        }
        let wantsBase64 = call.getString("responseType") == "base64"
        requestSession.dataTask(with: request) { data, response, error in
            if let error = error {
                call.reject(error.localizedDescription, "NETWORK", error)
                return
            }
            guard let http = response as? HTTPURLResponse else {
                call.reject("No response from the deployment.", "NETWORK")
                return
            }
            let bytes = data ?? Data()
            let body = wantsBase64 ? bytes.base64EncodedString() : Self.text(bytes)
            call.resolve([
                "status": http.statusCode,
                "headers": Self.headers(http),
                "body": body,
                "url": http.url?.absoluteString ?? ""
            ])
        }.resume()
    }

    @objc func stream(_ call: CAPPluginCall) {
        guard let id = call.getString("id"), !id.isEmpty else {
            call.reject("A stream needs an id.", "REFUSED")
            return
        }
        let request: URLRequest
        do {
            request = try buildRequest(call, timeout: 600)
        } catch let error as Refused {
            call.reject(error.message, "REFUSED")
            return
        } catch {
            call.reject("Bad request.", "REFUSED")
            return
        }
        let task = streamSession.dataTask(with: request)
        lock.lock()
        streams[task.taskIdentifier] = OpenStream(id: id, call: call)
        tasksById[id] = task
        lock.unlock()
        task.resume()
    }

    @objc func cancel(_ call: CAPPluginCall) {
        let id = call.getString("id") ?? ""
        lock.lock()
        let task = tasksById[id]
        lock.unlock()
        task?.cancel()
        call.resolve()
    }

    @objc func clearCookies(_ call: CAPPluginCall) {
        let storage = HTTPCookieStorage.shared
        if let saved = ServerStore.serverURL, let url = URL(string: saved) {
            for cookie in storage.cookies(for: url) ?? [] {
                storage.deleteCookie(cookie)
            }
        }
        call.resolve()
    }

    // MARK: - WebSockets

    @objc func socketOpen(_ call: CAPPluginCall) {
        guard let id = call.getString("id"), !id.isEmpty else {
            call.reject("A socket needs an id.", "REFUSED")
            return
        }
        guard let raw = call.getString("url"), let url = URL(string: raw) else {
            call.reject("Not a valid address.", "REFUSED")
            return
        }
        do {
            try Self.check(url, method: "GET")
        } catch let error as Refused {
            call.reject(error.message, "REFUSED")
            return
        } catch {
            call.reject("Bad request.", "REFUSED")
            return
        }
        guard url.path.hasPrefix("/api/"), !Self.isAppConfig(url, method: "GET"),
              var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            call.reject("The app only opens sockets to the deployment's API.", "REFUSED")
            return
        }
        parts.scheme = url.scheme?.lowercased() == "http" ? "ws" : "wss"
        guard let socketURL = parts.url else {
            call.reject("Not a valid address.", "REFUSED")
            return
        }
        var request = URLRequest(url: socketURL)
        // The handshake carries the session cookie the requests use. Added by
        // hand: a cookie stored for https:// is not matched to a wss:// URL.
        let cookies = HTTPCookieStorage.shared.cookies(for: url) ?? []
        for (name, value) in HTTPCookie.requestHeaderFields(with: cookies) {
            request.setValue(value, forHTTPHeaderField: name)
        }
        if let headers = call.getObject("headers") {
            for (name, value) in headers {
                if let value = value as? String {
                    request.setValue(value, forHTTPHeaderField: name)
                }
            }
        }
        let task = socketSession.webSocketTask(with: request)
        lock.lock()
        socketsById[id] = task
        socketIds[task.taskIdentifier] = id
        lock.unlock()
        task.resume()
        receive(task, id: id)
        call.resolve()
    }

    @objc func socketSend(_ call: CAPPluginCall) {
        let id = call.getString("id") ?? ""
        guard let text = call.getString("text") else {
            call.reject("Nothing to send.", "REFUSED")
            return
        }
        lock.lock()
        let task = socketsById[id]
        lock.unlock()
        guard let task = task else {
            call.reject("The socket is closed.", "CLOSED")
            return
        }
        task.send(.string(text)) { error in
            if let error = error {
                call.reject(error.localizedDescription, "NETWORK", error)
            } else {
                call.resolve()
            }
        }
    }

    @objc func socketClose(_ call: CAPPluginCall) {
        let id = call.getString("id") ?? ""
        lock.lock()
        let task = socketsById[id]
        lock.unlock()
        task?.cancel(with: .normalClosure, reason: nil)
        call.resolve()
    }

    /// Reads one message, hands it to the page, and waits for the next.
    private func receive(_ task: URLSessionWebSocketTask, id: String) {
        task.receive { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success(let message):
                switch message {
                case .data(let data):
                    self.deliverBinary(data, id: id)
                case .string(let text):
                    self.notifyListeners("socketMessage", data: ["id": id, "binary": false, "data": text])
                @unknown default:
                    break
                }
                self.receive(task, id: id)
            case .failure(let error):
                self.socketEnded(task, code: task.closeCode.rawValue, reason: task.closeReason, error: error.localizedDescription)
            }
        }
    }

    /// The live browser view's frames arrive as binary messages, as many a
    /// second as the page changes. Each crosses to the page as a base64 string
    /// of a few hundred kilobytes that the web view evaluates on its main
    /// thread, the thread the person's taps need: at the rate a video feed
    /// produces frames the page spent every moment unpacking pictures and the
    /// taps went nowhere. So a socket's binary messages reach the page at most
    /// once per `binaryInterval`, the newest one once the interval has passed;
    /// nothing but the newest picture is ever wanted. Text messages (the page's
    /// state, the pointer) are small and go at once.
    private let binaryInterval: TimeInterval = 0.08
    private var binaryHeld: [String: Data] = [:]
    private var binaryLastAt: [String: TimeInterval] = [:]
    private var binaryTimers: [String: DispatchWorkItem] = [:]

    private func deliverBinary(_ data: Data, id: String) {
        lock.lock()
        let now = Date().timeIntervalSinceReferenceDate
        let last = binaryLastAt[id] ?? 0
        if binaryTimers[id] == nil && now - last >= binaryInterval {
            binaryLastAt[id] = now
            lock.unlock()
            notifyListeners("socketMessage", data: ["id": id, "binary": true, "data": data.base64EncodedString()])
            return
        }
        binaryHeld[id] = data
        if binaryTimers[id] == nil {
            let item = DispatchWorkItem { [weak self] in self?.flushBinary(id: id) }
            binaryTimers[id] = item
            let delay = max(0, binaryInterval - (now - last))
            DispatchQueue.global(qos: .userInteractive).asyncAfter(deadline: .now() + delay, execute: item)
        }
        lock.unlock()
    }

    private func flushBinary(id: String) {
        lock.lock()
        binaryTimers[id] = nil
        guard let data = binaryHeld.removeValue(forKey: id) else {
            lock.unlock()
            return
        }
        binaryLastAt[id] = Date().timeIntervalSinceReferenceDate
        lock.unlock()
        notifyListeners("socketMessage", data: ["id": id, "binary": true, "data": data.base64EncodedString()])
    }

    /// Reports a socket's end once, however it ended.
    private func socketEnded(_ task: URLSessionTask, code: Int, reason: Data?, error: String?) {
        lock.lock()
        let id = socketIds.removeValue(forKey: task.taskIdentifier)
        if let id = id {
            socketsById.removeValue(forKey: id)
            binaryHeld.removeValue(forKey: id)
            binaryLastAt.removeValue(forKey: id)
            binaryTimers.removeValue(forKey: id)?.cancel()
        }
        lock.unlock()
        guard let id = id else { return }
        var event: [String: Any] = [
            "id": id,
            "code": code,
            "reason": reason.flatMap { String(data: $0, encoding: .utf8) } ?? ""
        ]
        if let error = error, code == 0 || code == URLSessionWebSocketTask.CloseCode.abnormalClosure.rawValue {
            event["error"] = error
        }
        notifyListeners("socketClose", data: event)
    }

    public func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didOpenWithProtocol protocol: String?) {
        lock.lock()
        let id = socketIds[webSocketTask.taskIdentifier]
        lock.unlock()
        if let id = id {
            notifyListeners("socketOpen", data: ["id": id])
        }
    }

    public func urlSession(
        _ session: URLSession,
        webSocketTask: URLSessionWebSocketTask,
        didCloseWith closeCode: URLSessionWebSocketTask.CloseCode,
        reason: Data?
    ) {
        socketEnded(webSocketTask, code: closeCode.rawValue, reason: reason, error: nil)
    }

    // MARK: - URLSessionDataDelegate

    /// Redirects are handed to the page as they are, never followed: a
    /// redirect could otherwise lead the session cookie's requests off the
    /// deployment, past `check`. (Android: setInstanceFollowRedirects(false).)
    public func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }

    // The three below are about streams. Task identifiers are only unique
    // within one session, so each checks which session is calling: the
    // request session's tasks report through their completion handlers.

    public func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
    ) {
        guard session === streamSession else {
            completionHandler(.allow)
            return
        }
        lock.lock()
        let call = streams[dataTask.taskIdentifier]?.call
        streams[dataTask.taskIdentifier]?.call = nil
        lock.unlock()
        let http = response as? HTTPURLResponse
        call?.resolve([
            "status": http?.statusCode ?? 0,
            "headers": http.map(Self.headers) ?? [:]
        ])
        completionHandler(.allow)
    }

    public func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard session === streamSession else { return }
        lock.lock()
        let id = streams[dataTask.taskIdentifier]?.id
        lock.unlock()
        guard let id = id else { return }
        notifyListeners("chunk", data: ["id": id, "data": data.base64EncodedString()])
    }

    public func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if session === socketSession {
            // A socket that never opened (refused, no network) ends here.
            socketEnded(task, code: (task as? URLSessionWebSocketTask)?.closeCode.rawValue ?? 0, reason: nil, error: error?.localizedDescription ?? "The connection closed.")
            return
        }
        guard session === streamSession else { return }
        lock.lock()
        let open = streams.removeValue(forKey: task.taskIdentifier)
        if let id = open?.id {
            tasksById.removeValue(forKey: id)
        }
        lock.unlock()
        guard let open = open else { return }
        let cancelled = (error as NSError?)?.code == NSURLErrorCancelled
        if let call = open.call {
            // Failed before any response: the promise is still waiting.
            call.reject(cancelled ? "Cancelled." : (error?.localizedDescription ?? "No response."), cancelled ? "CANCELLED" : "NETWORK")
        }
        var event: [String: Any] = ["id": open.id]
        if let error = error, !cancelled {
            event["error"] = error.localizedDescription
        }
        if cancelled {
            event["cancelled"] = true
        }
        notifyListeners("end", data: event)
    }

    // MARK: - Helpers

    private struct Refused: Error {
        let message: String
    }

    private func buildRequest(_ call: CAPPluginCall, timeout: TimeInterval) throws -> URLRequest {
        let method = (call.getString("method") ?? "GET").uppercased()
        guard let raw = call.getString("url"), let url = URL(string: raw) else {
            throw Refused(message: "Not a valid address.")
        }
        try Self.check(url, method: method)
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        request.httpMethod = method
        // Cookies are the deployment's session; the app config is public and gets none.
        request.httpShouldHandleCookies = !Self.isAppConfig(url, method: method)
        if let headers = call.getObject("headers") {
            for (name, value) in headers {
                if let value = value as? String {
                    request.setValue(value, forHTTPHeaderField: name)
                }
            }
        }
        if let body = call.getString("body") {
            if call.getString("bodyEncoding") == "base64" {
                guard let bytes = Data(base64Encoded: body) else {
                    throw Refused(message: "The file could not be read.")
                }
                request.httpBody = bytes
            } else {
                request.httpBody = body.data(using: .utf8)
            }
        }
        return request
    }

    /// The app's remote config (src/appConfig.ts, APP_CONFIG_URL): public,
    /// read without cookies, and the only non-deployment URL besides a probe.
    static let appConfigURL = "https://cloud.olano.ai/api/v1/mobile/config"

    static func isAppConfig(_ url: URL, method: String) -> Bool {
        method == "GET" && url.absoluteString == appConfigURL
    }

    /// The deployment saved by OlanoShell, a status probe of any address, or the app config.
    static func check(_ url: URL, method: String) throws {
        guard let scheme = url.scheme?.lowercased(), scheme == "https" || (scheme == "http" && ServerURL.allowsHTTP) else {
            throw Refused(message: "Only https:// addresses can be used.")
        }
        let origin = try? ServerURL.normalize(url.absoluteString, allowHTTP: ServerURL.allowsHTTP)
        if let saved = ServerStore.serverURL, origin == saved {
            return
        }
        if method == "GET" && url.path == "/api/status" {
            return
        }
        if isAppConfig(url, method: method) {
            return
        }
        throw Refused(message: "The app only talks to the deployment it is connected to.")
    }

    private static func headers(_ response: HTTPURLResponse) -> [String: String] {
        var out: [String: String] = [:]
        for (key, value) in response.allHeaderFields {
            if let key = key as? String, let value = value as? String {
                out[key.lowercased()] = value
            }
        }
        return out
    }

    private static func text(_ data: Data) -> String {
        String(data: data, encoding: .utf8) ?? String(decoding: data, as: UTF8.self)
    }
}
