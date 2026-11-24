package ai.olano.app;

import android.annotation.SuppressLint;
import android.content.Intent;
import android.os.Bundle;
import com.getcapacitor.BridgeActivity;
import com.getcapacitor.Logger;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginLoadException;
import com.getcapacitor.PluginManager;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * The app's only activity: a Capacitor bridge over the app's bundled screens
 * (webDir {@code www}). The deployment is never loaded into the web view; the
 * screens call it through OlanoHttpPlugin.
 */
public class MainActivity extends BridgeActivity {

    private static final String TAG = Logger.tags("OlanoShell");

    /** Plugins that are part of this app rather than of an npm package. */
    private static final List<Class<? extends Plugin>> APP_PLUGINS = Arrays.asList(
        OlanoShellPlugin.class,
        OlanoHttpPlugin.class,
        OlanoSpeechPlugin.class
    );

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        for (Class<? extends Plugin> plugin : APP_PLUGINS) {
            registerPlugin(plugin);
        }
        super.onCreate(savedInstanceState);
        NotificationChannels.ensure(this);
    }

    /**
     * A share, or a "Talk to ..." launcher shortcut, that arrives while the
     * app is already open: keep it as the activity's intent so
     * OlanoShellPlugin.takeShared / takeTalk read it on resume.
     */
    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        String action = intent.getAction();
        if (Intent.ACTION_SEND.equals(action) || Intent.ACTION_SEND_MULTIPLE.equals(action) || OlanoShellPlugin.TALK_ACTION.equals(action)) {
            setIntent(intent);
        }
    }

    /**
     * Called by BridgeActivity.onCreate once the npm plugins are on the builder.
     * A build without google-services.json has no Firebase app, and the push
     * plugin's register() then throws on a background thread, which kills the
     * process. Leaving the plugin out instead makes it report as unavailable
     * (Capacitor.isPluginAvailable("PushNotifications") is false), which the
     * app's settings screen reports.
     */
    @Override
    protected void load() {
        if (!isFirebaseConfigured()) {
            try {
                List<Class<? extends Plugin>> plugins = new ArrayList<>(APP_PLUGINS);
                for (Class<? extends Plugin> plugin : new PluginManager(getAssets()).loadPluginClasses()) {
                    CapacitorPlugin annotation = plugin.getAnnotation(CapacitorPlugin.class);
                    if (annotation == null || !"PushNotifications".equals(annotation.name())) {
                        plugins.add(plugin);
                    }
                }
                bridgeBuilder.setPlugins(plugins);
                Logger.warn(TAG, "No google-services.json in this build: push notifications are disabled.");
            } catch (PluginLoadException e) {
                Logger.error(TAG, "Could not list plugins; keeping the default set", e);
            }
        }
        super.load();
    }

    /** The google-services Gradle plugin turns google-services.json into this resource. */
    @SuppressLint("DiscouragedApi")
    private boolean isFirebaseConfigured() {
        return getResources().getIdentifier("google_app_id", "string", getPackageName()) != 0;
    }

}
