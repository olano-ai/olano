import SwiftUI
import WidgetKit

/// The app's widgets and controls (target `OlanoWidgets`, bundle id
/// `ai.olano.app.widgets`, iOS 18). They read the deployment with the session
/// the app hands over (Shared/OlanoShared.swift: the App Group when the build
/// has one, the shared keychain otherwise), so a phone that has signed in
/// once in the app draws them with no further set-up.
@main
struct OlanoWidgetsBundle: WidgetBundle {
    var body: some Widget {
        AskWidget()
        AgentCardWidget()
        QuickAsksWidget()
        ApprovalsWidget()
        TeamWidget()
        PulseWidget()
        NextRunWidget()
        TalkControl()
        AskControl()
    }
}
