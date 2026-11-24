import Foundation
import UIKit
import UserNotifications

/// Approve, Decline and Reply straight from a notification, without opening
/// the app (an approval waits five minutes by default; a reply should not need
/// the app to load).
///
/// The deployment marks a notification with a category (olano/push/events.py):
/// "approval" carries Approve and Decline, "reply" a Reply field. The buttons
/// are registered at launch (`registerCategories`). When one is pressed, iOS
/// wakes the app in the background and this answers the deployment directly,
/// with the session cookie OlanoHttp keeps in HTTPCookieStorage.shared:
///
/// - Approve / Decline a conversation's approval: `POST /api/chat/approval`
/// - Approve / Decline an Approvals inbox item: `POST /api/hitl/resolve`
/// - Reply: `POST /api/chat/stream`, let go once the turn has started (the
///   turn runs on without anyone watching, and its reply comes as a push)
///
/// Every other response (a tap on the notification) goes on to Capacitor's
/// notification router, which hands it to the app's screens. When answering
/// fails, a notification says so, and tapping it opens the app.
final class NotificationActions: NSObject, UNUserNotificationCenterDelegate {
    static let shared = NotificationActions()

    /// Capacitor's router, which the bridge holds.
    private weak var next: UNUserNotificationCenterDelegate?
    /// A tap that arrived before Capacitor's router existed (a cold start).
    private var pending: (UNNotificationResponse, () -> Void)?

    static func registerCategories() {
        // Answered in the background: no app to open. The phone must be
        // unlocked first, so nobody approves anything from a lock screen.
        let approve = UNNotificationAction(identifier: "approve", title: "Approve", options: [.authenticationRequired])
        let decline = UNNotificationAction(identifier: "decline", title: "Decline", options: [.authenticationRequired, .destructive])
        let reply = UNTextInputNotificationAction(
            identifier: "reply",
            title: "Reply",
            options: [.authenticationRequired],
            textInputButtonTitle: "Send",
            textInputPlaceholder: "Message"
        )
        UNUserNotificationCenter.current().setNotificationCategories([
            UNNotificationCategory(identifier: "approval", actions: [approve, decline], intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: "reply", actions: [reply], intentIdentifiers: [], options: []),
        ])
    }

    /// In front of whatever handles notifications now: at launch (nothing yet,
    /// so a cold start's button press is not lost) and again once Capacitor's
    /// bridge has put its router in place (OlanoBridgeViewController).
    static func install() {
        let center = UNUserNotificationCenter.current()
        if center.delegate !== shared {
            shared.next = center.delegate
            center.delegate = shared
        }
        if let pending = shared.pending, let next = shared.next {
            shared.pending = nil
            let (response, done) = pending
            next.userNotificationCenter?(center, didReceive: response, withCompletionHandler: done) ?? done()
        }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        if let next {
            next.userNotificationCenter?(center, willPresent: notification, withCompletionHandler: completionHandler)
                ?? completionHandler([])
        } else {
            completionHandler([])
        }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let info = response.notification.request.content.userInfo
        let action = response.actionIdentifier
        if action == "approve" || action == "decline" {
            answerApproval(info, approve: action == "approve", done: completionHandler)
        } else if action == "reply", let text = (response as? UNTextInputNotificationResponse)?.userText {
            reply(info, text: text, done: completionHandler)
        } else if let next {
            next.userNotificationCenter?(center, didReceive: response, withCompletionHandler: completionHandler)
                ?? completionHandler()
        } else {
            pending = (response, completionHandler)
        }
    }

    // MARK: answering

    private func value(_ info: [AnyHashable: Any], _ key: String) -> String {
        if let text = info[key] as? String { return text }
        if let number = info[key] as? NSNumber { return number.stringValue }
        return ""
    }

    private func answerApproval(_ info: [AnyHashable: Any], approve: Bool, done: @escaping () -> Void) {
        let agent = value(info, "agent")
        let body: [String: Any]
        let path: String
        if value(info, "kind") == "inbox_approval" {
            path = "/api/hitl/resolve"
            body = ["id": value(info, "item_id"), "approved": approve]
        } else {
            let count = max(1, Int(value(info, "count")) ?? 1)
            path = "/api/chat/approval"
            body = [
                "thread_id": value(info, "thread_id"),
                "decisions": Array(repeating: ["type": approve ? "approve" : "reject"], count: count),
            ]
        }
        send(path: path, body: body, info: info, headOnly: false) { [weak self] status in
            if status == nil || !(200..<300).contains(status!) {
                // Gone (404) or already answered (409) is not worth a second notification.
                if status != 404 && status != 409 {
                    self?.notifyFailure(info, title: "Not answered", body: self?.failureText(status, doing: approve ? "approve" : "decline", agent: agent) ?? "")
                }
            }
            done()
        }
    }

    private func reply(_ info: [AnyHashable: Any], text: String, done: @escaping () -> Void) {
        let message = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty else { done(); return }
        let agent = value(info, "agent")
        let body: [String: Any] = [
            "agent": agent,
            "thread_id": value(info, "thread_id"),
            "message": message,
            "client": "mobile",
        ]
        send(path: "/api/chat/stream", body: body, info: info, headOnly: true) { [weak self] status in
            if let status, (200..<300).contains(status) {
                done()
                return
            }
            let why = status == 409 ? "It is still working on the last message. Open Olano to add yours." : self?.failureText(status, doing: "send that", agent: agent) ?? ""
            self?.notifyFailure(info, title: "Reply not sent", body: why)
            done()
        }
    }

    private func failureText(_ status: Int?, doing: String, agent: String) -> String {
        if status == 401 { return "Olano needs you to sign in again. Open it to \(doing)." }
        if status == nil { return "The deployment could not be reached. Open Olano to \(doing)." }
        return "Something went wrong. Open Olano to \(doing)."
    }

    /// POSTs JSON to the saved deployment. `headOnly`: let go once the response
    /// starts (a chat turn streams for as long as it runs).
    private func send(path: String, body: [String: Any], info: [AnyHashable: Any], headOnly: Bool, done: @escaping (Int?) -> Void) {
        guard let origin = ServerStore.serverURL, let url = URL(string: origin + path),
              let data = try? JSONSerialization.data(withJSONObject: body) else {
            done(nil)
            return
        }
        // A notification from another deployment the app is no longer pointed at.
        let host = value(info, "host")
        if !host.isEmpty {
            let theirs = URL(string: host)?.host ?? host
            if let saved = URL(string: origin)?.host, theirs.lowercased() != saved.lowercased() {
                done(nil)
                return
            }
        }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 25)
        request.httpMethod = "POST"
        request.httpShouldHandleCookies = true
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(headOnly ? "text/event-stream" : "application/json", forHTTPHeaderField: "Accept")
        request.httpBody = data

        // The phone gives a woken app about 30 seconds; ask for them.
        var task = UIBackgroundTaskIdentifier.invalid
        task = UIApplication.shared.beginBackgroundTask(withName: "olano.notification-action") {
            UIApplication.shared.endBackgroundTask(task)
        }
        let finish: (Int?) -> Void = { status in
            DispatchQueue.main.async {
                done(status)
                UIApplication.shared.endBackgroundTask(task)
            }
        }
        if headOnly {
            HeadOnly.start(request, done: finish)
        } else {
            URLSession.shared.dataTask(with: request) { _, response, _ in
                finish((response as? HTTPURLResponse)?.statusCode)
            }.resume()
        }
    }

    private func notifyFailure(_ info: [AnyHashable: Any], title: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        content.userInfo = info.filter { $0.key as? String != "aps" }
        let request = UNNotificationRequest(identifier: "olano.failed.\(UUID().uuidString)", content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }
}

/// A request whose answer is wanted only as far as its status: the chat
/// stream's events keep coming for as long as the turn runs.
private final class HeadOnly: NSObject, URLSessionDataDelegate {
    private var done: ((Int?) -> Void)?
    private var session: URLSession?

    static func start(_ request: URLRequest, done: @escaping (Int?) -> Void) {
        let owner = HeadOnly()
        owner.done = done
        let session = URLSession(configuration: .default, delegate: owner, delegateQueue: nil)
        owner.session = session
        session.dataTask(with: request).resume()
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
    ) {
        completionHandler(.cancel)
        finish((response as? HTTPURLResponse)?.statusCode)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        finish(nil)
    }

    private func finish(_ status: Int?) {
        guard let done else { return }
        self.done = nil
        done(status)
        session?.invalidateAndCancel()
        session = nil
    }
}
