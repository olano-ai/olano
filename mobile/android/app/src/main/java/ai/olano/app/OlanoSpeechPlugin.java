package ai.olano.app;

import android.Manifest;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.UUID;

/**
 * Speech on the phone itself. JS name {@code OlanoSpeech}; the contract is
 * written out in OlanoSpeechPlugin.swift.
 *
 * <p>Replies are read with the phone's text-to-speech engine. Its speech
 * recognizer listens to the microphone itself and cannot be handed a recording
 * made by the screens, so voice notes keep going to the deployment to be
 * transcribed: {@code recognitionStatus} reports unavailable and
 * {@code transcribe} rejects with "unavailable".
 *
 * <p>Listening to the microphone is exactly what live dictation needs, so that
 * half is here: {@code dictationStatus}, {@code startDictation},
 * {@code stopDictation} and {@code cancelDictation}, with the
 * {@code dictationText}, {@code dictationLevel} and {@code dictationEnd} events.
 * It is offered only with the on-device recognizer (Android 12 and later, when
 * the phone has one), so no audio leaves the phone. Android's recognizer ends
 * after each pause; while dictation is on it is started again at once, and each
 * pause closes a segment.
 */
@CapacitorPlugin(
    name = "OlanoSpeech",
    permissions = { @Permission(alias = "microphone", strings = { Manifest.permission.RECORD_AUDIO }) }
)
public class OlanoSpeechPlugin extends Plugin {

    private TextToSpeech tts;
    private boolean ttsReady = false;
    /** Calls that arrived while the engine was still starting. */
    private final List<PluginCall> waiting = new ArrayList<>();
    private PluginCall speakCall;
    private String speakId;

    // Dictation: every field below is touched on the main thread only, which
    // is where SpeechRecognizer must be used. `generation` moves on every
    // teardown, so a callback from a finished recognizer finds itself stale.
    private final Handler main = new Handler(Looper.getMainLooper());
    private SpeechRecognizer recognizer;
    private Intent dictationIntent;
    private boolean dictating = false;
    private int generation = 0;
    private String segment = "";
    private PluginCall stopCall;
    private Runnable stopTimeout;
    private final ArrayDeque<Long> restarts = new ArrayDeque<>();
    private long levelAt = 0;
    private float loudest = 0;

    @PluginMethod
    public void recognitionStatus(PluginCall call) {
        JSObject result = new JSObject();
        result.put("available", false);
        result.put("onDevice", false);
        result.put("permission", "prompt");
        result.put("language", "");
        call.resolve(result);
    }

    @PluginMethod
    public void transcribe(PluginCall call) {
        call.reject("Voice notes are transcribed by the deployment on Android.", "unavailable");
    }

    // MARK: Live dictation

    @PluginMethod
    public void dictationStatus(PluginCall call) {
        main.post(() -> {
            JSObject result = new JSObject();
            result.put("available", onDeviceRecognizer());
            result.put("permission", microphonePermission());
            result.put("language", language(call));
            call.resolve(result);
        });
    }

    @PluginMethod
    public void startDictation(PluginCall call) {
        if (getPermissionState("microphone") != PermissionState.GRANTED) {
            requestPermissionForAlias("microphone", call, "afterMicrophone");
            return;
        }
        main.post(() -> beginDictation(call));
    }

    @PermissionCallback
    private void afterMicrophone(PluginCall call) {
        if (getPermissionState("microphone") != PermissionState.GRANTED) {
            call.reject("Olano isn't allowed to use the microphone.", "denied");
            return;
        }
        main.post(() -> beginDictation(call));
    }

    @PluginMethod
    public void stopDictation(PluginCall call) {
        main.post(() -> {
            if (!dictating || recognizer == null) {
                call.resolve();
                return;
            }
            if (stopCall != null) {
                stopCall.resolve();
            }
            stopCall = call;
            // The recognizer finishes what it heard: onResults (or onError
            // when it heard nothing) ends the stop.
            recognizer.stopListening();
            stopTimeout = this::finishStop;
            main.postDelayed(stopTimeout, 2500);
        });
    }

    @PluginMethod
    public void cancelDictation(PluginCall call) {
        main.post(() -> {
            teardownDictation();
            if (stopCall != null) {
                stopCall.resolve();
                stopCall = null;
            }
            call.resolve();
        });
    }

    private boolean onDeviceRecognizer() {
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && SpeechRecognizer.isOnDeviceRecognitionAvailable(getContext());
    }

    private String microphonePermission() {
        PermissionState state = getPermissionState("microphone");
        if (state == PermissionState.GRANTED) return "granted";
        if (state == PermissionState.DENIED) return "denied";
        return "prompt";
    }

    private static String language(PluginCall call) {
        String language = call.getString("language", "");
        return language == null || language.isEmpty() ? Locale.getDefault().toLanguageTag() : language;
    }

    private void beginDictation(PluginCall call) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S || !onDeviceRecognizer()) {
            call.reject("Dictation needs the phone's own speech recognition, which this phone doesn't have.", "unavailable");
            return;
        }
        // One at a time: a new dictation replaces one still running.
        teardownDictation();
        synchronized (this) {
            if (tts != null && ttsReady) {
                tts.stop();
            }
            finishSpeaking(false);
        }
        String language = language(call);
        Intent intent = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
        intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE, language);
        intent.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
        intent.putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true);
        intent.putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1);
        // A breath mid-sentence should not end the segment.
        intent.putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 2500L);
        intent.putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, 2500L);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.putExtra(RecognizerIntent.EXTRA_ENABLE_FORMATTING, RecognizerIntent.FORMATTING_OPTIMIZE_QUALITY);
        }
        try {
            recognizer = SpeechRecognizer.createOnDeviceSpeechRecognizer(getContext());
        } catch (RuntimeException e) {
            call.reject("Dictation isn't available on this phone.", "unavailable");
            return;
        }
        generation++;
        dictating = true;
        dictationIntent = intent;
        segment = "";
        restarts.clear();
        recognizer.setRecognitionListener(listener(generation));
        recognizer.startListening(intent);
        JSObject result = new JSObject();
        result.put("language", language);
        call.resolve(result);
    }

    private RecognitionListener listener(int forGeneration) {
        return new RecognitionListener() {
            private boolean stale() {
                return forGeneration != generation;
            }

            @Override
            public void onReadyForSpeech(Bundle params) {}

            @Override
            public void onBeginningOfSpeech() {}

            @Override
            public void onRmsChanged(float rmsdB) {
                if (stale()) return;
                // Roughly -2 (quiet) to 10 (loud speech) on most recognizers.
                loudest = Math.max(loudest, Math.max(0f, Math.min(1f, (rmsdB + 2f) / 12f)));
                long now = SystemClock.uptimeMillis();
                if (now - levelAt < 100) return;
                levelAt = now;
                JSObject data = new JSObject();
                data.put("level", (double) loudest);
                loudest = 0;
                notifyListeners("dictationLevel", data);
            }

            @Override
            public void onBufferReceived(byte[] buffer) {}

            @Override
            public void onEndOfSpeech() {}

            @Override
            public void onPartialResults(Bundle partialResults) {
                if (stale()) return;
                String text = firstResult(partialResults);
                if (text.isEmpty()) return;
                segment = text;
                sendText(text, false);
            }

            @Override
            public void onResults(Bundle results) {
                if (stale()) return;
                String text = firstResult(results);
                if (text.isEmpty()) text = segment;
                segment = "";
                if (!text.isEmpty()) sendText(text, true);
                if (stopCall != null || !dictating) {
                    finishStop();
                } else {
                    restartSegment();
                }
            }

            @Override
            public void onError(int error) {
                if (stale()) return;
                closeSegment();
                if (stopCall != null) {
                    finishStop();
                    return;
                }
                switch (error) {
                    case SpeechRecognizer.ERROR_NO_MATCH:
                    case SpeechRecognizer.ERROR_SPEECH_TIMEOUT:
                        // A pause with nothing said: listen on.
                        restartSegment();
                        break;
                    case SpeechRecognizer.ERROR_RECOGNIZER_BUSY:
                        main.postDelayed(() -> {
                            if (!stale()) restartSegment();
                        }, 300);
                        break;
                    case SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS:
                        endDictation("error", "Olano isn't allowed to use the microphone.");
                        break;
                    case SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED:
                    case SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE:
                        endDictation("error", "Your phone's speech recognition doesn't have your language downloaded yet.");
                        break;
                    default:
                        endDictation("error", "Dictation stopped (speech recognition error " + error + ").");
                }
            }

            @Override
            public void onEvent(int eventType, Bundle params) {}
        };
    }

    private static String firstResult(Bundle bundle) {
        if (bundle == null) return "";
        ArrayList<String> matches = bundle.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
        if (matches == null || matches.isEmpty() || matches.get(0) == null) return "";
        return matches.get(0).trim();
    }

    private void sendText(String text, boolean isFinal) {
        JSObject data = new JSObject();
        data.put("text", text);
        data.put("final", isFinal);
        notifyListeners("dictationText", data);
    }

    /** Sends the open segment's words as final, if it has any. */
    private void closeSegment() {
        if (segment.isEmpty()) return;
        String text = segment;
        segment = "";
        sendText(text, true);
    }

    private void restartSegment() {
        if (!dictating || recognizer == null) return;
        // A recognizer that fails at once, over and over, is not going to
        // recover: stop rather than spin.
        long now = SystemClock.uptimeMillis();
        while (!restarts.isEmpty() && now - restarts.peekFirst() > 3000) {
            restarts.pollFirst();
        }
        restarts.addLast(now);
        if (restarts.size() > 5) {
            endDictation("error", "Speech recognition stopped responding.");
            return;
        }
        recognizer.startListening(dictationIntent);
    }

    private void finishStop() {
        closeSegment();
        PluginCall call = stopCall;
        stopCall = null;
        teardownDictation();
        if (call != null) {
            call.resolve();
        }
    }

    /** It stopped by itself: keep the words, say why. */
    private void endDictation(String reason, String message) {
        if (!dictating) return;
        closeSegment();
        teardownDictation();
        JSObject data = new JSObject();
        data.put("reason", reason);
        data.put("message", message);
        notifyListeners("dictationEnd", data);
    }

    private void teardownDictation() {
        dictating = false;
        generation++;
        if (stopTimeout != null) {
            main.removeCallbacks(stopTimeout);
            stopTimeout = null;
        }
        if (recognizer != null) {
            recognizer.cancel();
            recognizer.destroy();
            recognizer = null;
        }
        dictationIntent = null;
        segment = "";
        loudest = 0;
    }

    // MARK: Reading aloud

    @PluginMethod
    public void speak(PluginCall call) {
        String text = call.getString("text", "");
        if (text == null || text.trim().isEmpty()) {
            call.reject("There is nothing to read.", "failed");
            return;
        }
        synchronized (this) {
            if (ttsReady) {
                say(call);
                return;
            }
            waiting.add(call);
            if (tts == null) {
                tts = new TextToSpeech(getContext(), this::onEngineReady);
            }
        }
    }

    @PluginMethod
    public void stopSpeaking(PluginCall call) {
        synchronized (this) {
            if (tts != null && ttsReady) {
                tts.stop();
            }
            finishSpeaking(false);
        }
        call.resolve();
    }

    private void onEngineReady(int status) {
        synchronized (this) {
            if (status != TextToSpeech.SUCCESS || tts == null) {
                if (tts != null) {
                    tts.shutdown();
                }
                tts = null;
                for (PluginCall call : waiting) {
                    call.reject("This phone has no voice to read with.", "unavailable");
                }
                waiting.clear();
                return;
            }
            ttsReady = true;
            tts.setOnUtteranceProgressListener(
                new UtteranceProgressListener() {
                    @Override
                    public void onStart(String utteranceId) {}

                    @Override
                    public void onDone(String utteranceId) {
                        ended(utteranceId, true);
                    }

                    @Override
                    @Deprecated
                    public void onError(String utteranceId) {
                        ended(utteranceId, false);
                    }

                    @Override
                    public void onError(String utteranceId, int errorCode) {
                        ended(utteranceId, false);
                    }

                    @Override
                    public void onStop(String utteranceId, boolean interrupted) {
                        ended(utteranceId, false);
                    }
                }
            );
            // Only the newest request is read; earlier ones were replaced while waiting.
            PluginCall latest = waiting.isEmpty() ? null : waiting.get(waiting.size() - 1);
            for (PluginCall call : waiting) {
                if (call != latest) {
                    JSObject replaced = new JSObject();
                    replaced.put("finished", false);
                    call.resolve(replaced);
                }
            }
            waiting.clear();
            if (latest != null) {
                say(latest);
            }
        }
    }

    /** Reads the call's text; holds the lock. */
    private void say(PluginCall call) {
        finishSpeaking(false);
        String text = call.getString("text", "").trim();
        String language = call.getString("language", "");
        if (language != null && !language.isEmpty()) {
            Locale locale = Locale.forLanguageTag(language);
            if (tts.isLanguageAvailable(locale) >= TextToSpeech.LANG_AVAILABLE) {
                tts.setLanguage(locale);
            }
        }
        int max = TextToSpeech.getMaxSpeechInputLength();
        if (text.length() > max) {
            text = text.substring(0, max);
        }
        String id = UUID.randomUUID().toString();
        speakCall = call;
        speakId = id;
        if (tts.speak(text, TextToSpeech.QUEUE_FLUSH, null, id) != TextToSpeech.SUCCESS) {
            speakCall = null;
            speakId = null;
            call.reject("The phone could not read this aloud.", "failed");
        }
    }

    private synchronized void ended(String utteranceId, boolean finished) {
        if (speakId != null && speakId.equals(utteranceId)) {
            finishSpeaking(finished);
        }
    }

    /** Settles the reading in progress, if any; holds the lock. */
    private void finishSpeaking(boolean finished) {
        PluginCall call = speakCall;
        speakCall = null;
        speakId = null;
        if (call != null) {
            JSObject result = new JSObject();
            result.put("finished", finished);
            call.resolve(result);
        }
    }

    @Override
    protected void handleOnDestroy() {
        main.post(this::teardownDictation);
        synchronized (this) {
            if (tts != null) {
                tts.stop();
                tts.shutdown();
            }
            tts = null;
            ttsReady = false;
            finishSpeaking(false);
        }
    }
}
