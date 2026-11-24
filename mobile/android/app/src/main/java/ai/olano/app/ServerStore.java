package ai.olano.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.ApplicationInfo;
import androidx.annotation.Nullable;

/**
 * Where the chosen deployment lives: SharedPreferences ({@value #PREFS}, key
 * {@value #KEY_SERVER_URL}). OlanoHttpPlugin calls no other origin.
 */
final class ServerStore {

    static final String PREFS = "olano_shell";
    static final String KEY_SERVER_URL = "server_url";

    private ServerStore() {}

    private static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static boolean isDebuggable(Context context) {
        return (context.getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0;
    }

    @Nullable
    static String get(Context context) {
        String value = prefs(context).getString(KEY_SERVER_URL, null);
        return value == null || value.isEmpty() ? null : value;
    }

    /** Synchronous, so a request sent right after saving already sees the new origin. */
    static boolean set(Context context, String origin) {
        return prefs(context).edit().putString(KEY_SERVER_URL, origin).commit();
    }

    static boolean clear(Context context) {
        return prefs(context).edit().remove(KEY_SERVER_URL).commit();
    }
}
