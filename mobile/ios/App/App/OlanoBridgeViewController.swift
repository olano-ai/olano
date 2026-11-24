import UIKit
import Capacitor

/// The app's bridge view controller. The app's screens are bundled (webDir
/// `www`) and always load from the app itself; the deployment is only ever a
/// server the screens call through OlanoHttp. Created by `SceneDelegate` (and
/// named in Main.storyboard).
class OlanoBridgeViewController: CAPBridgeViewController {

    override func capacitorDidLoad() {
        super.capacitorDidLoad()
        // Part of the app rather than npm packages, so `packageClassList`
        // does not list them.
        bridge?.registerPluginInstance(OlanoShellPlugin())
        bridge?.registerPluginInstance(OlanoHttpPlugin())
        bridge?.registerPluginInstance(OlanoSpeechPlugin())
        // The bridge has just made Capacitor's notification router the
        // delegate; the notification buttons go back in front of it.
        NotificationActions.install()
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        // The screens draw their own swipe-back (Ionic's iOS gesture); the web
        // view's history swipe would fight it.
        webView?.allowsBackForwardNavigationGestures = false
        // Rubber-banding at the top of a page is the page's own job
        // (pull to refresh); the web view itself should never bounce.
        webView?.scrollView.bounces = false
        foregroundObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.willEnterForegroundNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            self?.reloadIfBlank()
        }
    }

    deinit {
        if let foregroundObserver {
            NotificationCenter.default.removeObserver(foregroundObserver)
        }
    }

    private var foregroundObserver: NSObjectProtocol?

    /// Coming back to the app after a while sometimes showed a white screen
    /// that only killing the app cleared. iOS may end the web view's content
    /// process while the app is suspended, and WebKit does not always say so
    /// (Capacitor reloads the page only when it does), so the page comes back
    /// blank. Asking the page whether it has drawn anything tells: no answer
    /// means the process is gone, an empty root means the app's script never
    /// ran or died. Either way, load the app again; the session survives it
    /// (the cookie and the deployment's address live in native storage).
    private func reloadIfBlank() {
        guard let webView else { return }
        let probe = "(function(){var r=document.getElementById('root');return r?r.childElementCount:-1})()"
        webView.evaluateJavaScript(probe) { [weak self] result, error in
            let drawn = (result as? NSNumber).map { $0.intValue > 0 } ?? false
            if error != nil || !drawn {
                CAPLog.print("⚡️  The page was blank on return to the app; loading it again")
                self?.loadWebView()
            }
        }
    }
}
