import UIKit
import UniformTypeIdentifiers

/// "Olano" in the share sheet: a link from Safari, photos, documents from
/// Files or Mail, or some text.
///
/// The extension keeps what was shared in the App Group's `Shared/<id>/`
/// folder with a `manifest.json` (`{ text, files: [{ name, mime, file }] }`),
/// then opens Olano (`olano://share`). The app takes it from there
/// (OlanoShellPlugin `takeShared`, src/native/shareIn.ts): it asks which agent
/// gets it and puts it in that agent's composer, unsent. If the phone does not
/// let the app open from here, the share waits until Olano is next opened.
final class ShareViewController: UIViewController {
    nonisolated static let appGroup = "group.ai.olano.app"

    private let status = UILabel()
    private let spinner = UIActivityIndicatorView(style: .medium)

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        status.text = "Adding to Olano…"
        status.font = .preferredFont(forTextStyle: .headline)
        status.textAlignment = .center
        status.numberOfLines = 0
        let stack = UIStackView(arrangedSubviews: [spinner, status])
        stack.axis = .vertical
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: view.centerYAnchor),
            stack.leadingAnchor.constraint(greaterThanOrEqualTo: view.leadingAnchor, constant: 24),
        ])
        spinner.startAnimating()
        Task { await collect() }
    }

    private func collect() async {
        guard let folder = Self.newFolder() else {
            finish("Olano could not keep this. Try again.")
            return
        }
        var texts: [String] = []
        var files: [[String: String]] = []
        let items = (extensionContext?.inputItems as? [NSExtensionItem]) ?? []
        for item in items {
            for provider in item.attachments ?? [] {
                if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier)
                    || provider.hasItemConformingToTypeIdentifier(UTType.image.identifier)
                    || provider.hasItemConformingToTypeIdentifier(UTType.movie.identifier)
                    || provider.hasItemConformingToTypeIdentifier(UTType.pdf.identifier) {
                    if let saved = await saveFile(provider, into: folder) { files.append(saved) }
                } else if provider.hasItemConformingToTypeIdentifier(UTType.url.identifier) {
                    if let url = await loadItem(provider, UTType.url.identifier) as? URL { texts.append(url.absoluteString) }
                } else if provider.hasItemConformingToTypeIdentifier(UTType.plainText.identifier) {
                    if let text = await loadItem(provider, UTType.plainText.identifier) as? String { texts.append(text) }
                } else if provider.hasItemConformingToTypeIdentifier(UTType.data.identifier) {
                    if let saved = await saveFile(provider, into: folder) { files.append(saved) }
                }
            }
        }
        let text = texts.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }.joined(separator: "\n")
        guard !text.isEmpty || !files.isEmpty else {
            try? FileManager.default.removeItem(at: folder)
            finish("There was nothing Olano could use in this.")
            return
        }
        let manifest: [String: Any] = ["text": text, "files": files]
        guard let data = try? JSONSerialization.data(withJSONObject: manifest),
              (try? data.write(to: folder.appendingPathComponent("manifest.json"), options: .atomic)) != nil else {
            try? FileManager.default.removeItem(at: folder)
            finish("Olano could not keep this. Try again.")
            return
        }
        let opened = openApp()
        finish(opened ? nil : "Saved. Open Olano to choose which agent gets it.")
    }

    /// Ends the share sheet, after a moment to read `message` when there is one.
    private func finish(_ message: String?) {
        DispatchQueue.main.async {
            self.spinner.stopAnimating()
            guard let message else {
                self.extensionContext?.completeRequest(returningItems: nil)
                return
            }
            self.status.text = message
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.8) {
                self.extensionContext?.completeRequest(returningItems: nil)
            }
        }
    }

    // MARK: reading what was shared

    private func loadItem(_ provider: NSItemProvider, _ type: String) async -> NSSecureCoding? {
        await withCheckedContinuation { continuation in
            provider.loadItem(forTypeIdentifier: type, options: nil) { item, _ in
                continuation.resume(returning: item)
            }
        }
    }

    /// Copies one shared file into `folder`; its manifest entry, or nil.
    private func saveFile(_ provider: NSItemProvider, into folder: URL) async -> [String: String]? {
        let type = provider.registeredTypeIdentifiers.first { UTType($0)?.conforms(to: .data) == true }
            ?? UTType.data.identifier
        let utType = UTType(type)
        let mime = utType?.preferredMIMEType ?? "application/octet-stream"
        let fallbackName = (provider.suggestedName ?? "Shared") + (utType?.preferredFilenameExtension.map { ".\($0)" } ?? "")

        // A file the system hands over as one (Files, Mail attachments, most photos).
        let copied: URL? = await withCheckedContinuation { continuation in
            provider.loadFileRepresentation(forTypeIdentifier: type) { url, _ in
                // The system deletes `url` once this returns: copy it now.
                guard let url else {
                    continuation.resume(returning: nil)
                    return
                }
                let destination = Self.freeURL(in: folder, name: url.lastPathComponent)
                let ok = (try? FileManager.default.copyItem(at: url, to: destination)) != nil
                continuation.resume(returning: ok ? destination : nil)
            }
        }
        if let copied {
            return ["name": copied.lastPathComponent, "mime": mime, "file": copied.lastPathComponent]
        }

        // Some apps (a screenshot being marked up) hand over an image or bytes instead.
        let item = await loadItem(provider, type)
        var bytes: Data?
        var name = fallbackName
        var itemMime = mime
        if let image = item as? UIImage {
            bytes = image.jpegData(compressionQuality: 0.9)
            name = (provider.suggestedName ?? "Photo") + ".jpg"
            itemMime = "image/jpeg"
        } else if let data = item as? Data {
            bytes = data
        } else if let url = item as? URL, url.isFileURL {
            bytes = try? Data(contentsOf: url)
            name = url.lastPathComponent
        }
        guard let bytes else { return nil }
        let destination = Self.freeURL(in: folder, name: name)
        guard (try? bytes.write(to: destination)) != nil else { return nil }
        return ["name": destination.lastPathComponent, "mime": itemMime, "file": destination.lastPathComponent]
    }

    // MARK: the App Group

    nonisolated static func newFolder() -> URL? {
        guard let group = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: appGroup) else { return nil }
        let folder = group.appendingPathComponent("Shared", isDirectory: true).appendingPathComponent(UUID().uuidString, isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            return folder
        } catch {
            return nil
        }
    }

    /// `name` in `folder`, or "name 2.ext" when two shared files share a name.
    nonisolated static func freeURL(in folder: URL, name: String) -> URL {
        let clean = name.isEmpty || name == "manifest.json" ? "Shared" : name
        var candidate = folder.appendingPathComponent(clean)
        let stem = (clean as NSString).deletingPathExtension
        let ext = (clean as NSString).pathExtension
        var n = 2
        while FileManager.default.fileExists(atPath: candidate.path) {
            candidate = folder.appendingPathComponent(ext.isEmpty ? "\(stem) \(n)" : "\(stem) \(n).\(ext)")
            n += 1
        }
        return candidate
    }

    // MARK: opening the app

    /// A share extension has no API to open its app; the app's UIApplication
    /// is in the responder chain, so its open method is called directly.
    private func openApp() -> Bool {
        guard let url = URL(string: "olano://share") else { return false }
        let selector = NSSelectorFromString("openURL:options:completionHandler:")
        typealias Open = @convention(c) (AnyObject, Selector, NSURL, NSDictionary, AnyObject?) -> Void
        var responder: UIResponder? = self
        while let current = responder {
            if current !== self, current.responds(to: selector), let method = current.method(for: selector) {
                let open = unsafeBitCast(method, to: Open.self)
                open(current, selector, url as NSURL, NSDictionary(), nil)
                return true
            }
            responder = current.next
        }
        return false
    }
}
