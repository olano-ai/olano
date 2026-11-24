package ai.olano.app;

import android.app.Activity;
import android.app.KeyguardManager;
import android.content.Context;
import android.content.ContentResolver;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.provider.OpenableColumns;
import android.util.Base64;
import androidx.biometric.BiometricManager;
import androidx.biometric.BiometricPrompt;
import androidx.core.content.ContextCompat;
import androidx.core.content.pm.PackageInfoCompat;
import androidx.core.content.pm.ShortcutInfoCompat;
import androidx.core.content.pm.ShortcutManagerCompat;
import androidx.core.graphics.drawable.IconCompat;
import androidx.fragment.app.FragmentActivity;
import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.util.InternalUtils;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Which Olano deployment the app talks to.
 *
 * JS name {@code OlanoShell}. The saved origin is also the only one
 * OlanoHttpPlugin will call. The contract (mirrored by OlanoShellPlugin.swift):
 *
 * <ul>
 *   <li>{@code getServer()} resolves {@code { url: string | null }}</li>
 *   <li>{@code setServer({ url })} validates, saves and resolves {@code { url }}, the origin</li>
 *   <li>{@code clearServer()} forgets it</li>
 *   <li>{@code getAppInfo()} resolves {@code { version, build, platform }}</li>
 *   <li>{@code lockStatus()} resolves {@code { available, kind }}: whether the phone has a screen
 *       lock, and "biometric" or "passcode" for how it unlocks</li>
 *   <li>{@code unlock({ reason })} asks for a fingerprint or face with the screen lock as the
 *       fallback (BiometricPrompt); resolves {@code { ok: true }} or rejects with code
 *       "cancelled", "unavailable" or "failed" (the app lock, src/native/appLock.ts)</li>
 *   <li>{@code takeShared()} (on iOS, what the share extension kept) resolves {@code { text, files: [{ name, mime, data }] }}
 *       for what another app shared into Olano, then forgets it; {@code data} is base64</li>
 *   <li>{@code setAgents({ agents: [{ name, display }] })} publishes the roster: here it becomes
 *       the launcher's "Talk to ..." shortcuts (a long press on the app icon), the Android
 *       counterpart of the iPhone's Siri phrases and Action button</li>
 *   <li>{@code takeTalk()} resolves {@code { agent: string | null }}: the agent a "Talk to"
 *       shortcut asked for (the activity's intent, read once and cleared), then forgets it</li>
 * </ul>
 */
@CapacitorPlugin(name = "OlanoShell")
public class OlanoShellPlugin extends Plugin {

    @PluginMethod
    public void getServer(PluginCall call) {
        String url = ServerStore.get(getContext());
        JSObject result = new JSObject();
        result.put("url", url == null ? JSONObject.NULL : url);
        call.resolve(result);
    }

    @PluginMethod
    public void setServer(PluginCall call) {
        String origin;
        try {
            origin = ServerUrl.normalize(call.getString("url"), ServerStore.isDebuggable(getContext()));
        } catch (IllegalArgumentException e) {
            call.reject(e.getMessage(), "INVALID_URL");
            return;
        }
        if (!ServerStore.set(getContext(), origin)) {
            call.reject("Could not save the deployment address on this device.", "STORAGE");
            return;
        }
        JSObject result = new JSObject();
        result.put("url", origin);
        call.resolve(result);
    }

    @PluginMethod
    public void clearServer(PluginCall call) {
        if (!ServerStore.clear(getContext())) {
            call.reject("Could not forget the deployment address on this device.", "STORAGE");
            return;
        }
        call.resolve();
    }

    @PluginMethod
    public void getAppInfo(PluginCall call) {
        JSObject result = new JSObject();
        try {
            PackageInfo info = InternalUtils.getPackageInfo(getContext().getPackageManager(), getContext().getPackageName());
            result.put("version", info.versionName == null ? "" : info.versionName);
            result.put("build", Long.toString(PackageInfoCompat.getLongVersionCode(info)));
        } catch (PackageManager.NameNotFoundException e) {
            result.put("version", "");
            result.put("build", "");
        }
        result.put("platform", "android");
        call.resolve(result);
    }

    private static final int UNLOCK_WITH =
            BiometricManager.Authenticators.BIOMETRIC_WEAK | BiometricManager.Authenticators.DEVICE_CREDENTIAL;

    @PluginMethod
    public void lockStatus(PluginCall call) {
        KeyguardManager keyguard = (KeyguardManager) getContext().getSystemService(Context.KEYGUARD_SERVICE);
        boolean secure = keyguard != null && keyguard.isDeviceSecure();
        boolean biometric = BiometricManager.from(getContext())
                .canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_WEAK) == BiometricManager.BIOMETRIC_SUCCESS;
        JSObject result = new JSObject();
        result.put("available", secure);
        result.put("kind", !secure ? "none" : biometric ? "biometric" : "passcode");
        call.resolve(result);
    }

    @PluginMethod
    public void unlock(PluginCall call) {
        String reason = call.getString("reason", "Unlock Olano");
        Activity activity = getActivity();
        if (!(activity instanceof FragmentActivity)) {
            call.reject("This screen cannot ask to unlock.", "unavailable");
            return;
        }
        activity.runOnUiThread(() -> {
            BiometricPrompt prompt = new BiometricPrompt(
                    (FragmentActivity) activity,
                    ContextCompat.getMainExecutor(getContext()),
                    new BiometricPrompt.AuthenticationCallback() {
                        @Override
                        public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult result) {
                            JSObject ok = new JSObject();
                            ok.put("ok", true);
                            call.resolve(ok);
                        }

                        @Override
                        public void onAuthenticationError(int code, CharSequence message) {
                            String reason;
                            switch (code) {
                                case BiometricPrompt.ERROR_USER_CANCELED:
                                case BiometricPrompt.ERROR_NEGATIVE_BUTTON:
                                case BiometricPrompt.ERROR_CANCELED:
                                    reason = "cancelled";
                                    break;
                                case BiometricPrompt.ERROR_NO_DEVICE_CREDENTIAL:
                                case BiometricPrompt.ERROR_HW_NOT_PRESENT:
                                    reason = "unavailable";
                                    break;
                                default:
                                    reason = "failed";
                            }
                            call.reject(message == null ? "Not unlocked." : message.toString(), reason);
                        }
                        // onAuthenticationFailed (a finger not recognised) keeps the prompt open.
                    });
            BiometricPrompt.PromptInfo info = new BiometricPrompt.PromptInfo.Builder()
                    .setTitle(reason)
                    .setAllowedAuthenticators(UNLOCK_WITH)
                    .build();
            prompt.authenticate(info);
        });
    }

    /** Larger files are refused here, before they are read: a message carries at most 10 MB. */
    private static final long MAX_SHARED_BYTES = 10L * 1024 * 1024;

    @PluginMethod
    public void takeShared(PluginCall call) {
        Activity activity = getActivity();
        Intent intent = activity == null ? null : activity.getIntent();
        JSObject result = new JSObject();
        JSArray files = new JSArray();
        result.put("files", files);
        if (intent == null) {
            call.resolve(result);
            return;
        }
        String action = intent.getAction();
        if (!Intent.ACTION_SEND.equals(action) && !Intent.ACTION_SEND_MULTIPLE.equals(action)) {
            call.resolve(result);
            return;
        }
        // Read once: a resume with the same intent must not share it again.
        activity.setIntent(new Intent(Intent.ACTION_MAIN));

        String subject = intent.getStringExtra(Intent.EXTRA_SUBJECT);
        CharSequence text = intent.getCharSequenceExtra(Intent.EXTRA_TEXT);
        StringBuilder body = new StringBuilder();
        if (subject != null && !subject.isEmpty() && (text == null || !text.toString().contains(subject))) {
            body.append(subject);
        }
        if (text != null && text.length() > 0) {
            if (body.length() > 0) body.append('\n');
            body.append(text);
        }
        result.put("text", body.toString());

        ContentResolver resolver = getContext().getContentResolver();
        for (Uri uri : sharedUris(intent)) {
            JSObject file = readShared(resolver, uri);
            if (file != null) files.put(file);
        }
        call.resolve(result);
    }

    @SuppressWarnings("deprecation")
    private static List<Uri> sharedUris(Intent intent) {
        List<Uri> uris = new ArrayList<>();
        if (Intent.ACTION_SEND_MULTIPLE.equals(intent.getAction())) {
            ArrayList<Uri> list = Build.VERSION.SDK_INT >= 33
                ? intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri.class)
                : intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM);
            if (list != null) uris.addAll(list);
        } else {
            Uri uri = Build.VERSION.SDK_INT >= 33
                ? intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri.class)
                : intent.getParcelableExtra(Intent.EXTRA_STREAM);
            if (uri != null) uris.add(uri);
        }
        return uris;
    }

    /** One shared file as {@code { name, mime, data }}, or null when it cannot be read. */
    private static JSObject readShared(ContentResolver resolver, Uri uri) {
        String name = null;
        long size = -1;
        try (Cursor cursor = resolver.query(uri, null, null, null, null)) {
            if (cursor != null && cursor.moveToFirst()) {
                int nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                int sizeIndex = cursor.getColumnIndex(OpenableColumns.SIZE);
                if (nameIndex >= 0) name = cursor.getString(nameIndex);
                if (sizeIndex >= 0 && !cursor.isNull(sizeIndex)) size = cursor.getLong(sizeIndex);
            }
        } catch (RuntimeException e) {
            // A provider without the columns: fall back to the path.
        }
        if (name == null || name.isEmpty()) {
            String last = uri.getLastPathSegment();
            name = last == null || last.isEmpty() ? "shared file" : last;
        }
        String mime = resolver.getType(uri);
        JSObject file = new JSObject();
        file.put("name", name);
        file.put("mime", mime == null ? "" : mime);
        if (size > MAX_SHARED_BYTES) {
            // The page reports it as too large from the size alone.
            file.put("size", size);
            file.put("data", "");
            return file;
        }
        try (InputStream in = resolver.openInputStream(uri)) {
            if (in == null) return null;
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[64 * 1024];
            long total = 0;
            int read;
            while ((read = in.read(buffer)) != -1) {
                total += read;
                if (total > MAX_SHARED_BYTES) {
                    file.put("size", total);
                    file.put("data", "");
                    return file;
                }
                out.write(buffer, 0, read);
            }
            file.put("data", Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP));
            return file;
        } catch (IOException | SecurityException e) {
            return null;
        }
    }

    /** The intent a "Talk to ..." launcher shortcut starts the activity with. */
    static final String TALK_ACTION = "ai.olano.app.TALK";
    static final String TALK_AGENT = "agent";
    /** The launcher shows at most four or five; the first agents of the app's order get them. */
    private static final int MAX_TALK_SHORTCUTS = 4;

    @PluginMethod
    public void setAgents(PluginCall call) {
        JSArray agents = call.getArray("agents");
        Context context = getContext();
        List<ShortcutInfoCompat> shortcuts = new ArrayList<>();
        try {
            int max = Math.min(MAX_TALK_SHORTCUTS, ShortcutManagerCompat.getMaxShortcutCountPerActivity(context));
            for (int i = 0; agents != null && i < agents.length() && shortcuts.size() < max; i++) {
                JSONObject agent = agents.getJSONObject(i);
                String name = agent.optString("name", "").trim();
                if (name.isEmpty()) continue;
                String display = agent.optString("display", "").trim();
                if (display.isEmpty()) display = name;
                Intent intent = new Intent(context, MainActivity.class)
                    .setAction(TALK_ACTION)
                    .putExtra(TALK_AGENT, name)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                shortcuts.add(
                    new ShortcutInfoCompat.Builder(context, "talk:" + name)
                        .setShortLabel("Talk to " + display)
                        .setLongLabel("Talk to " + display + " hands-free")
                        .setIcon(IconCompat.createWithResource(context, R.mipmap.ic_launcher))
                        .setIntent(intent)
                        .build()
                );
            }
            ShortcutManagerCompat.setDynamicShortcuts(context, shortcuts);
        } catch (JSONException | RuntimeException e) {
            // A shortcut is a convenience: the app works without them.
        }
        call.resolve();
    }

    @PluginMethod
    public void takeTalk(PluginCall call) {
        Activity activity = getActivity();
        Intent intent = activity == null ? null : activity.getIntent();
        JSObject result = new JSObject();
        if (intent != null && TALK_ACTION.equals(intent.getAction())) {
            String agent = intent.getStringExtra(TALK_AGENT);
            // Read once: a resume with the same intent must not ask again.
            activity.setIntent(new Intent(Intent.ACTION_MAIN));
            if (agent != null && !agent.trim().isEmpty()) {
                result.put("agent", agent.trim());
                call.resolve(result);
                return;
            }
        }
        result.put("agent", JSONObject.NULL);
        call.resolve(result);
    }

}
