import AVFoundation
import Capacitor
import NaturalLanguage
import Speech

/// Speech on the phone itself: live dictation into the composer, turning a
/// recorded voice note into words, and reading a reply aloud, without the
/// deployment.
///
/// Dictation listens to the microphone itself (AVAudioEngine) and streams the
/// words as they are recognised. It is offered only where recognition runs on
/// the device for the language, so no audio ever leaves the phone, and it runs
/// with no time limit.
///
/// A voice note is still recorded by the screens (16 kHz WAV, see
/// `src/native/recorder.ts`); this transcribes that file with Apple's speech
/// recognizer, on the device when it supports the language (no audio leaves the
/// phone, no time limit), through Apple otherwise. The words go to the agent as
/// the message, so a voice note no longer waits for, or depends on, the
/// deployment's speech-to-text.
///
/// JS name `OlanoSpeech`. The contract, mirrored by OlanoSpeechPlugin.java:
///
/// - `recognitionStatus({ language? })` resolves `{ available, onDevice,
///   permission, language }`; `permission` is "granted", "denied", "restricted"
///   or "prompt" (not asked yet).
/// - `transcribe({ data, durationMs?, language? })`: `data` is the recording as
///   base64 WAV. Asks for permission the first time. Resolves `{ text,
///   onDevice, language }` (`text` is "" when nothing was said); rejects with
///   code "denied", "unavailable", "timeout" or "failed".
/// - `speak({ text, language? })` reads `text` aloud in the best voice the phone
///   has for its language (detected from the words unless given), and resolves
///   `{ finished }` when it ends: true when read to the end, false when stopped.
/// - `stopSpeaking()` stops reading.
/// - `dictationStatus({ language? })` resolves `{ available, permission,
///   language }`: `available` only where recognition runs on the device for the
///   language; `permission` covers both speech recognition and the microphone.
/// - `startDictation({ language? })` asks for both permissions the first time and
///   resolves `{ language }` once listening; rejects with "denied",
///   "unavailable" or "failed". While it listens it sends the events
///   `dictationText` `{ text, final }` (the current segment's words; a final one
///   closes the segment and the next words start another), `dictationLevel`
///   `{ level }` (0 to 1, about ten a second) and, when it stops by itself,
///   `dictationEnd` `{ reason: "error" | "interrupted", message }`.
/// - `stopDictation()` stops listening and resolves once the last words have
///   been sent; `cancelDictation()` stops and drops them.
@objc(OlanoSpeechPlugin)
public class OlanoSpeechPlugin: CAPPlugin, CAPBridgedPlugin, AVSpeechSynthesizerDelegate {
    public let identifier = "OlanoSpeechPlugin"
    public let jsName = "OlanoSpeech"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "recognitionStatus", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "transcribe", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "speak", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "stopSpeaking", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "dictationStatus", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "startDictation", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "stopDictation", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "cancelDictation", returnType: CAPPluginReturnPromise)
    ]

    // Everything below runs on the main queue: plugin calls hop onto it, and
    // the recognizer and the synthesizer call back on it.
    private var recognition: SFSpeechRecognitionTask?
    private var recognitionCall: CAPPluginCall?
    private var recognitionFile: URL?
    private var recognitionTimer: DispatchWorkItem?
    private var synthesizer: AVSpeechSynthesizer?
    private var speakCall: CAPPluginCall?

    // Dictation. `dictationGeneration` moves on every teardown, so a callback
    // from a finished session (or a finished segment's task) finds itself stale.
    private var dictationEngine: AVAudioEngine?
    private var dictationRecognizer: SFSpeechRecognizer?
    private var dictationRequest: SFSpeechAudioBufferRecognitionRequest?
    private var dictationTask: SFSpeechRecognitionTask?
    private var dictationActive = false
    private var dictationGeneration = 0
    private var dictationSegment = ""
    private var dictationStopCall: CAPPluginCall?
    private var dictationStopTimer: DispatchWorkItem?
    private var dictationRestarts: [Date] = []
    // Read on the audio thread: which request the microphone feeds, and when
    // the last loudness reading went out.
    private let tapLock = NSLock()
    private var tapRequest: SFSpeechAudioBufferRecognitionRequest?
    private var tapLevelAt: TimeInterval = 0
    private var tapLoudest: Float = 0

    override public func load() {
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(audioInterrupted(_:)),
            name: AVAudioSession.interruptionNotification,
            object: nil
        )
    }

    // MARK: Recognition

    @objc func recognitionStatus(_ call: CAPPluginCall) {
        DispatchQueue.main.async {
            let recognizer = self.recognizer(call.getString("language"))
            call.resolve([
                "available": recognizer?.isAvailable ?? false,
                "onDevice": recognizer?.supportsOnDeviceRecognition ?? false,
                "permission": Self.permissionName(SFSpeechRecognizer.authorizationStatus()),
                "language": recognizer?.locale.identifier ?? ""
            ])
        }
    }

    @objc func transcribe(_ call: CAPPluginCall) {
        guard let base64 = call.getString("data"), let data = Data(base64Encoded: base64), !data.isEmpty else {
            call.reject("There is no recording to transcribe.", "failed")
            return
        }
        let language = call.getString("language")
        let durationMs = call.getDouble("durationMs") ?? 0
        DispatchQueue.main.async {
            self.authorize { granted in
                guard granted else {
                    call.reject("Olano isn't allowed to use speech recognition.", "denied")
                    return
                }
                self.recognize(call, data: data, language: language, durationMs: durationMs)
            }
        }
    }

    private func recognize(_ call: CAPPluginCall, data: Data, language: String?, durationMs: Double) {
        guard let recognizer = recognizer(language), recognizer.isAvailable else {
            call.reject("Speech recognition isn't available right now.", "unavailable")
            return
        }
        // One at a time: a newer voice note wins.
        endRecognition(reject: ("Replaced by a newer recording.", "failed"))

        let file = FileManager.default.temporaryDirectory.appendingPathComponent("olano-voice-\(UUID().uuidString).wav")
        do {
            try data.write(to: file, options: .atomic)
        } catch {
            call.reject("The recording could not be read.", "failed")
            return
        }
        let request = SFSpeechURLRecognitionRequest(url: file)
        request.shouldReportPartialResults = false
        request.taskHint = .dictation
        let onDevice = recognizer.supportsOnDeviceRecognition
        if onDevice {
            request.requiresOnDeviceRecognition = true
        }
        if #available(iOS 16, *) {
            request.addsPunctuation = true
        }
        let languageId = recognizer.locale.identifier
        recognitionCall = call
        recognitionFile = file
        recognition = recognizer.recognitionTask(with: request) { [weak self] result, error in
            guard let self, self.recognitionCall === call else { return }
            if let result, result.isFinal {
                self.endRecognition(resolve: [
                    "text": result.bestTranscription.formattedString,
                    "onDevice": onDevice,
                    "language": languageId
                ])
            } else if let error {
                let code = (error as NSError).code
                // 1110: "No speech detected", which is an answer, not a failure.
                if code == 1110 {
                    self.endRecognition(resolve: ["text": "", "onDevice": onDevice, "language": languageId])
                } else {
                    self.endRecognition(reject: (error.localizedDescription, "failed"))
                }
            }
        }
        // Recognition runs faster than the recording played; past this it is stuck.
        let timer = DispatchWorkItem { [weak self] in
            guard let self, self.recognitionCall === call else { return }
            self.recognition?.cancel()
            self.endRecognition(reject: ("Transcribing took too long.", "timeout"))
        }
        recognitionTimer = timer
        DispatchQueue.main.asyncAfter(deadline: .now() + max(20, durationMs / 1000 * 1.5 + 10), execute: timer)
    }

    private func endRecognition(resolve: [String: Any]? = nil, reject: (String, String)? = nil) {
        let call = recognitionCall
        recognitionCall = nil
        recognitionTimer?.cancel()
        recognitionTimer = nil
        if reject != nil {
            recognition?.cancel()
        }
        recognition = nil
        if let file = recognitionFile {
            try? FileManager.default.removeItem(at: file)
        }
        recognitionFile = nil
        if let resolve {
            call?.resolve(resolve)
        } else if let reject {
            call?.reject(reject.0, reject.1)
        }
    }

    private func authorize(_ done: @escaping (Bool) -> Void) {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized:
            done(true)
        case .notDetermined:
            SFSpeechRecognizer.requestAuthorization { status in
                DispatchQueue.main.async { done(status == .authorized) }
            }
        default:
            done(false)
        }
    }

    /// The recognizer for `language`, else for the first of the phone's languages it supports.
    private func recognizer(_ language: String?) -> SFSpeechRecognizer? {
        var candidates: [String] = []
        if let language, !language.isEmpty {
            candidates.append(language)
        }
        candidates.append(contentsOf: Locale.preferredLanguages)
        for id in candidates {
            if let recognizer = SFSpeechRecognizer(locale: Locale(identifier: id)) {
                return recognizer
            }
        }
        return SFSpeechRecognizer()
    }

    private static func permissionName(_ status: SFSpeechRecognizerAuthorizationStatus) -> String {
        switch status {
        case .authorized: return "granted"
        case .denied: return "denied"
        case .restricted: return "restricted"
        case .notDetermined: return "prompt"
        @unknown default: return "prompt"
        }
    }

    // MARK: Live dictation

    @objc func dictationStatus(_ call: CAPPluginCall) {
        DispatchQueue.main.async {
            let recognizer = self.recognizer(call.getString("language"))
            call.resolve([
                "available": (recognizer?.isAvailable ?? false) && (recognizer?.supportsOnDeviceRecognition ?? false),
                "permission": Self.combinedPermission(SFSpeechRecognizer.authorizationStatus(), Self.microphonePermission()),
                "language": recognizer?.locale.identifier ?? ""
            ])
        }
    }

    @objc func startDictation(_ call: CAPPluginCall) {
        let language = call.getString("language")
        DispatchQueue.main.async {
            self.authorize { granted in
                guard granted else {
                    call.reject("Olano isn't allowed to use speech recognition.", "denied")
                    return
                }
                self.authorizeMicrophone { allowed in
                    guard allowed else {
                        call.reject("Olano isn't allowed to use the microphone.", "denied")
                        return
                    }
                    self.beginDictation(call, language: language)
                }
            }
        }
    }

    @objc func stopDictation(_ call: CAPPluginCall) {
        DispatchQueue.main.async {
            guard self.dictationActive else {
                call.resolve()
                return
            }
            if let earlier = self.dictationStopCall {
                earlier.resolve()
            }
            self.dictationStopCall = call
            // No more sound; the recognizer finishes what it heard and sends
            // a final result, which ends the stop (`segmentDone`).
            self.stopMicrophone()
            self.dictationRequest?.endAudio()
            let timer = DispatchWorkItem { [weak self] in
                self?.finishStop()
            }
            self.dictationStopTimer = timer
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.5, execute: timer)
        }
    }

    @objc func cancelDictation(_ call: CAPPluginCall) {
        DispatchQueue.main.async {
            self.teardownDictation()
            if let stop = self.dictationStopCall {
                self.dictationStopCall = nil
                stop.resolve()
            }
            call.resolve()
        }
    }

    private func beginDictation(_ call: CAPPluginCall, language: String?) {
        guard let recognizer = recognizer(language), recognizer.isAvailable, recognizer.supportsOnDeviceRecognition else {
            call.reject("Dictation isn't available for this language on this iPhone.", "unavailable")
            return
        }
        // One at a time: a new dictation replaces one still running.
        teardownDictation()
        if synthesizer?.isSpeaking == true {
            synthesizer?.stopSpeaking(at: .immediate)
        }
        finishSpeaking(false)

        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(.playAndRecord, mode: .measurement, options: [.duckOthers, .defaultToSpeaker, .allowBluetooth])
            try session.setActive(true, options: .notifyOthersOnDeactivation)
        } catch {
            call.reject("The microphone could not be started.", "failed")
            return
        }
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            try? session.setActive(false, options: .notifyOthersOnDeactivation)
            call.reject("There is no microphone to listen with.", "unavailable")
            return
        }
        dictationGeneration += 1
        dictationActive = true
        dictationRecognizer = recognizer
        dictationEngine = engine
        dictationRestarts = []
        startSegment()

        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            self?.heard(buffer)
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            teardownDictation()
            call.reject("The microphone could not be started.", "failed")
            return
        }
        call.resolve(["language": recognizer.locale.identifier])
    }

    /// A new recognition task fed by the microphone: at the start, and after
    /// each segment the recognizer closes (a long pause) while still listening.
    private func startSegment() {
        guard let recognizer = dictationRecognizer else { return }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = true
        request.taskHint = .dictation
        if #available(iOS 16, *) {
            request.addsPunctuation = true
        }
        tapLock.lock()
        tapRequest = request
        tapLock.unlock()
        dictationRequest = request
        dictationSegment = ""
        let generation = dictationGeneration
        dictationTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
            DispatchQueue.main.async {
                guard let self, self.dictationGeneration == generation, self.dictationRequest === request else { return }
                if let result {
                    let text = result.bestTranscription.formattedString
                    self.dictationSegment = result.isFinal ? "" : text
                    self.notifyListeners("dictationText", data: ["text": text, "final": result.isFinal])
                    if result.isFinal {
                        self.segmentDone()
                        return
                    }
                }
                guard let error else { return }
                // What the segment had so far is kept, whatever ends it.
                self.closeSegment()
                if self.dictationStopCall != nil {
                    self.finishStop()
                    return
                }
                let code = (error as NSError).code
                // 1110: no speech heard; 216 and 301: the task was ended. Listen on.
                if code == 1110 || code == 216 || code == 301 {
                    self.restartSegment()
                } else {
                    self.endDictation(reason: "error", message: error.localizedDescription)
                }
            }
        }
    }

    private func segmentDone() {
        if dictationStopCall != nil || !dictationActive {
            finishStop()
        } else {
            restartSegment()
        }
    }

    private func restartSegment() {
        // A recognizer that fails at once, over and over, is not going to
        // recover: stop rather than spin.
        let now = Date()
        dictationRestarts = dictationRestarts.filter { now.timeIntervalSince($0) < 3 } + [now]
        if dictationRestarts.count > 5 {
            endDictation(reason: "error", message: "Speech recognition stopped responding.")
            return
        }
        startSegment()
    }

    /// Sends the open segment's words as final, if it has any.
    private func closeSegment() {
        guard !dictationSegment.isEmpty else { return }
        let text = dictationSegment
        dictationSegment = ""
        notifyListeners("dictationText", data: ["text": text, "final": true])
    }

    private func finishStop() {
        closeSegment()
        let call = dictationStopCall
        dictationStopCall = nil
        teardownDictation()
        call?.resolve()
    }

    /// It stopped by itself: keep the words, say why.
    private func endDictation(reason: String, message: String) {
        guard dictationActive else { return }
        closeSegment()
        teardownDictation()
        notifyListeners("dictationEnd", data: ["reason": reason, "message": message])
    }

    private func stopMicrophone() {
        if let engine = dictationEngine {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
        }
        tapLock.lock()
        tapRequest = nil
        tapLock.unlock()
    }

    private func teardownDictation() {
        let wasActive = dictationActive || dictationEngine != nil
        dictationActive = false
        dictationGeneration += 1
        dictationStopTimer?.cancel()
        dictationStopTimer = nil
        stopMicrophone()
        dictationEngine = nil
        dictationTask?.cancel()
        dictationTask = nil
        dictationRequest = nil
        dictationRecognizer = nil
        dictationSegment = ""
        if wasActive {
            // Let music that was playing before carry on.
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        }
    }

    /// On the audio thread: feeds the recognizer and, about ten times a
    /// second, sends the loudest moment since the last reading.
    private func heard(_ buffer: AVAudioPCMBuffer) {
        tapLock.lock()
        let request = tapRequest
        tapLock.unlock()
        request?.append(buffer)

        guard let samples = buffer.floatChannelData?[0], buffer.frameLength > 0 else { return }
        var sum: Float = 0
        for i in 0..<Int(buffer.frameLength) {
            sum += samples[i] * samples[i]
        }
        let rms = sqrt(sum / Float(buffer.frameLength))
        let decibels = 20 * log10(max(rms, 0.000_001))
        let level = max(0, min(1, (decibels + 55) / 45))
        let now = Date().timeIntervalSince1970
        tapLock.lock()
        tapLoudest = max(tapLoudest, level)
        let due = now - tapLevelAt >= 0.1
        let loudest = tapLoudest
        if due {
            tapLevelAt = now
            tapLoudest = 0
        }
        tapLock.unlock()
        if due {
            DispatchQueue.main.async { [weak self] in
                guard let self, self.dictationActive else { return }
                self.notifyListeners("dictationLevel", data: ["level": Double(loudest)])
            }
        }
    }

    @objc private func audioInterrupted(_ notification: Notification) {
        guard let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              AVAudioSession.InterruptionType(rawValue: raw) == .began else { return }
        DispatchQueue.main.async {
            self.endDictation(reason: "interrupted", message: "Dictation stopped: the phone needed the microphone.")
        }
    }

    private func authorizeMicrophone(_ done: @escaping (Bool) -> Void) {
        if #available(iOS 17, *) {
            switch AVAudioApplication.shared.recordPermission {
            case .granted:
                done(true)
            case .undetermined:
                AVAudioApplication.requestRecordPermission { granted in
                    DispatchQueue.main.async { done(granted) }
                }
            default:
                done(false)
            }
        } else {
            switch AVAudioSession.sharedInstance().recordPermission {
            case .granted:
                done(true)
            case .undetermined:
                AVAudioSession.sharedInstance().requestRecordPermission { granted in
                    DispatchQueue.main.async { done(granted) }
                }
            default:
                done(false)
            }
        }
    }

    private static func microphonePermission() -> String {
        if #available(iOS 17, *) {
            switch AVAudioApplication.shared.recordPermission {
            case .granted: return "granted"
            case .denied: return "denied"
            default: return "prompt"
            }
        }
        switch AVAudioSession.sharedInstance().recordPermission {
        case .granted: return "granted"
        case .denied: return "denied"
        default: return "prompt"
        }
    }

    private static func combinedPermission(_ speech: SFSpeechRecognizerAuthorizationStatus, _ microphone: String) -> String {
        let recognition = permissionName(speech)
        if recognition == "restricted" { return "restricted" }
        if recognition == "denied" || microphone == "denied" { return "denied" }
        if recognition == "granted" && microphone == "granted" { return "granted" }
        return "prompt"
    }

    // MARK: Reading aloud

    @objc func speak(_ call: CAPPluginCall) {
        let text = (call.getString("text") ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            call.reject("There is nothing to read.", "failed")
            return
        }
        let language = call.getString("language")
        DispatchQueue.main.async {
            let synthesizer: AVSpeechSynthesizer
            if let existing = self.synthesizer {
                synthesizer = existing
            } else {
                synthesizer = AVSpeechSynthesizer()
                synthesizer.delegate = self
                self.synthesizer = synthesizer
            }
            self.finishSpeaking(false)
            if synthesizer.isSpeaking {
                synthesizer.stopSpeaking(at: .immediate)
            }
            // Heard with the ringer switch off too, like a voice message.
            try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio)
            try? AVAudioSession.sharedInstance().setActive(true)
            let utterance = AVSpeechUtterance(string: text)
            utterance.voice = Self.voice(for: text, language: language)
            self.speakCall = call
            synthesizer.speak(utterance)
        }
    }

    @objc func stopSpeaking(_ call: CAPPluginCall) {
        DispatchQueue.main.async {
            if self.synthesizer?.isSpeaking == true {
                self.synthesizer?.stopSpeaking(at: .immediate)
            }
            self.finishSpeaking(false)
            call.resolve()
        }
    }

    public func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        finishSpeaking(true)
    }

    public func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        finishSpeaking(false)
    }

    private func finishSpeaking(_ finished: Bool) {
        guard let call = speakCall else { return }
        speakCall = nil
        call.resolve(["finished": finished])
        // Let music that was playing before carry on.
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    /// The best voice the phone has for the words' language: the person's own
    /// variant of it first (en-GB for someone in the UK), then the highest quality.
    private static func voice(for text: String, language: String?) -> AVSpeechSynthesisVoice? {
        var code = language ?? ""
        if code.isEmpty {
            let detector = NLLanguageRecognizer()
            detector.processString(String(text.prefix(1000)))
            code = detector.dominantLanguage?.rawValue ?? ""
        }
        if code.isEmpty {
            code = AVSpeechSynthesisVoice.currentLanguageCode()
        }
        let base = String(code.split(separator: "-").first ?? Substring(code)).lowercased()
        let voices = AVSpeechSynthesisVoice.speechVoices().filter {
            $0.language.lowercased().hasPrefix(base)
        }
        guard !voices.isEmpty else {
            return AVSpeechSynthesisVoice(language: code)
        }
        let preferred = Locale.preferredLanguages.map { $0.lowercased() }
        func rank(_ voice: AVSpeechSynthesisVoice) -> (Int, Int) {
            let own = preferred.firstIndex(of: voice.language.lowercased()).map { 100 - $0 } ?? 0
            return (own, voice.quality.rawValue)
        }
        return voices.max { rank($0) < rank($1) }
    }
}
