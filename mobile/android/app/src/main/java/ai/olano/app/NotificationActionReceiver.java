package ai.olano.app;

import android.Manifest;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.webkit.CookieManager;
import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import androidx.core.app.RemoteInput;
import androidx.core.content.ContextCompat;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.Map;
import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Approve, Decline and Reply straight from a notification, without opening the
 * app (an approval waits five minutes by default; a reply should not need the
 * app to load). The Android half of NotificationActions.swift.
 *
 * {@link #show} draws a push that came as data ({@link OlanoMessagingService}):
 * its title and text, its channel, a tap that opens the app the way a tap on
 * any push does, and the buttons its category names. A button press arrives
 * here and is answered with the session cookie OlanoHttp keeps in the web
 * view's CookieManager:
 *
 * <ul>
 *   <li>Approve / Decline a conversation's approval: {@code POST /api/chat/approval}</li>
 *   <li>Approve / Decline an Approvals inbox item: {@code POST /api/hitl/resolve}</li>
 *   <li>Reply: {@code POST /api/chat/stream}, let go once the turn has started (it runs on, and
 *       its reply comes as a push)</li>
 * </ul>
 *
 * <p>When answering fails, the notification says so, and tapping it opens the app.
 */
public class NotificationActionReceiver extends BroadcastReceiver {

    static final String KEY_CATEGORY = "olano_category";
    private static final String KEY_TITLE = "olano_title";
    private static final String KEY_BODY = "olano_body";
    private static final String KEY_CHANNEL = "olano_channel";
    private static final String KEY_TAG = "olano_tag";

    private static final String ACTION = "ai.olano.app.NOTIFICATION_ACTION";
    private static final String EXTRA_ANSWER = "olano_answer";
    private static final String EXTRA_DATA = "olano_data";
    private static final String REPLY_KEY = "olano_reply";
    private static final int ID = 1;

    /** Draws a push that carries buttons. */
    static void show(Context context, String messageId, Map<String, String> data) {
        if (!canNotify(context)) return;
        String category = value(data, KEY_CATEGORY);
        String tag = tagOf(data);
        Bundle bundle = new Bundle();
        for (Map.Entry<String, String> entry : data.entrySet()) {
            if (!entry.getKey().startsWith("olano_")) bundle.putString(entry.getKey(), entry.getValue());
        }

        String channel = value(data, KEY_CHANNEL);
        NotificationCompat.Builder builder = new NotificationCompat.Builder(
                context,
                channel.isEmpty() ? NotificationChannels.REPLIES : channel)
            .setSmallIcon(R.drawable.ic_stat_olano)
            .setColor(ContextCompat.getColor(context, R.color.olano_accent))
            .setContentTitle(value(data, KEY_TITLE))
            .setContentText(value(data, KEY_BODY))
            .setStyle(new NotificationCompat.BigTextStyle().bigText(value(data, KEY_BODY)))
            .setAutoCancel(true)
            .setContentIntent(openApp(context, messageId, bundle, tag));

        if ("approval".equals(category)) {
            builder.setPriority(NotificationCompat.PRIORITY_HIGH);
            builder.addAction(0, context.getString(R.string.notification_approve), answer(context, "approve", bundle, tag, false));
            builder.addAction(0, context.getString(R.string.notification_decline), answer(context, "decline", bundle, tag, false));
        } else if ("reply".equals(category)) {
            RemoteInput input = new RemoteInput.Builder(REPLY_KEY).setLabel(context.getString(R.string.notification_reply_hint)).build();
            builder.addAction(
                new NotificationCompat.Action.Builder(0, context.getString(R.string.notification_reply), answer(context, "reply", bundle, tag, true))
                    .addRemoteInput(input)
                    .setAllowGeneratedReplies(false)
                    .build());
        }
        notify(context, tag, builder);
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        if (!ACTION.equals(intent.getAction())) return;
        String answer = intent.getStringExtra(EXTRA_ANSWER);
        Bundle data = intent.getBundleExtra(EXTRA_DATA);
        if (answer == null || data == null) return;
        String tag = data.getString(KEY_TAG, "olano");
        data.remove(KEY_TAG);
        String reply = "";
        if ("reply".equals(answer)) {
            Bundle typed = RemoteInput.getResultsFromIntent(intent);
            CharSequence text = typed == null ? null : typed.getCharSequence(REPLY_KEY);
            reply = text == null ? "" : text.toString().trim();
            if (reply.isEmpty()) {
                NotificationManagerCompat.from(context).cancel(tag, ID);
                return;
            }
        }
        String origin = ServerStore.get(context);
        String host = data.getString("host", "");
        if (origin == null || !sameHost(origin, host)) {
            failed(context, data, tag, answer, null);
            return;
        }

        String path;
        JSONObject body = new JSONObject();
        try {
            if ("reply".equals(answer)) {
                path = "/api/chat/stream";
                body.put("agent", data.getString("agent", ""));
                body.put("thread_id", data.getString("thread_id", ""));
                body.put("message", reply);
                body.put("client", "mobile");
            } else if ("inbox_approval".equals(data.getString("kind"))) {
                path = "/api/hitl/resolve";
                body.put("id", data.getString("item_id", ""));
                body.put("approved", "approve".equals(answer));
            } else {
                path = "/api/chat/approval";
                int count = 1;
                try {
                    count = Math.max(1, Integer.parseInt(data.getString("count", "1")));
                } catch (NumberFormatException ignored) {
                    // One decision.
                }
                JSONArray decisions = new JSONArray();
                for (int i = 0; i < count; i++) {
                    decisions.put(new JSONObject().put("type", "approve".equals(answer) ? "approve" : "reject"));
                }
                body.put("thread_id", data.getString("thread_id", ""));
                body.put("decisions", decisions);
            }
        } catch (Exception e) {
            failed(context, data, tag, answer, null);
            return;
        }

        String url = origin + path;
        // Read here, on the main thread, where the web view's cookie store lives.
        String cookie = CookieManager.getInstance().getCookie(url);
        PendingResult pending = goAsync();
        boolean stream = "reply".equals(answer);
        new Thread(() -> {
            try {
                Integer status = post(url, cookie, body.toString(), stream);
                if (status != null && status >= 200 && status < 300) {
                    NotificationManagerCompat.from(context).cancel(tag, ID);
                } else if (!stream && status != null && (status == 404 || status == 409)) {
                    // Gone, or answered elsewhere already.
                    NotificationManagerCompat.from(context).cancel(tag, ID);
                } else {
                    failed(context, data, tag, answer, status);
                }
            } finally {
                pending.finish();
            }
        }, "olano-notification-action").start();
    }

    /** POSTs JSON; for a chat turn, lets go once the response starts. Null when it never answered. */
    private static Integer post(String url, String cookie, String json, boolean stream) {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(url).openConnection();
            connection.setRequestMethod("POST");
            connection.setConnectTimeout(15000);
            connection.setReadTimeout(20000);
            connection.setUseCaches(false);
            connection.setInstanceFollowRedirects(false);
            connection.setRequestProperty("Content-Type", "application/json");
            connection.setRequestProperty("Accept", stream ? "text/event-stream" : "application/json");
            if (cookie != null && !cookie.isEmpty()) connection.setRequestProperty("Cookie", cookie);
            byte[] bytes = json.getBytes(StandardCharsets.UTF_8);
            connection.setDoOutput(true);
            connection.setFixedLengthStreamingMode(bytes.length);
            try (OutputStream out = connection.getOutputStream()) {
                out.write(bytes);
            }
            return connection.getResponseCode();
        } catch (Exception e) {
            return null;
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    private static void failed(Context context, Bundle data, String tag, String answer, Integer status) {
        if (!canNotify(context)) return;
        boolean reply = "reply".equals(answer);
        String text;
        if (reply && status != null && status == 409) {
            text = context.getString(R.string.notification_failed_busy);
        } else if (status != null && status == 401) {
            text = context.getString(R.string.notification_failed_signin);
        } else if (status == null) {
            text = context.getString(R.string.notification_failed_unreachable);
        } else {
            text = context.getString(R.string.notification_failed_other);
        }
        NotificationCompat.Builder builder = new NotificationCompat.Builder(context, NotificationChannels.REPLIES)
            .setSmallIcon(R.drawable.ic_stat_olano)
            .setColor(ContextCompat.getColor(context, R.color.olano_accent))
            .setContentTitle(context.getString(reply ? R.string.notification_reply_failed : R.string.notification_answer_failed))
            .setContentText(text)
            .setStyle(new NotificationCompat.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setContentIntent(openApp(context, "olano-failed-" + System.currentTimeMillis(), data, tag));
        notify(context, tag, builder);
    }

    /** A tap opens the app with the push's data, the way Capacitor hands a tapped push to the screens. */
    private static PendingIntent openApp(Context context, String messageId, Bundle data, String tag) {
        Intent intent = new Intent(context, MainActivity.class);
        intent.putExtras(data);
        intent.putExtra("google.message_id", messageId == null ? "olano-" + tag : messageId);
        intent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        return PendingIntent.getActivity(context, requestCode(tag, "open"), intent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    private static PendingIntent answer(Context context, String answer, Bundle data, String tag, boolean typed) {
        Intent intent = new Intent(context, NotificationActionReceiver.class);
        intent.setAction(ACTION);
        intent.putExtra(EXTRA_ANSWER, answer);
        Bundle carried = new Bundle(data);
        carried.putString(KEY_TAG, tag);
        intent.putExtra(EXTRA_DATA, carried);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        // A Reply field writes its text into the intent, so that one must stay mutable.
        if (typed && Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) flags |= PendingIntent.FLAG_MUTABLE;
        else if (!typed) flags |= PendingIntent.FLAG_IMMUTABLE;
        return PendingIntent.getBroadcast(context, requestCode(tag, answer), intent, flags);
    }

    private static void notify(Context context, String tag, NotificationCompat.Builder builder) {
        try {
            NotificationManagerCompat.from(context).notify(tag, ID, builder.build());
        } catch (SecurityException ignored) {
            // Notifications turned off for the app.
        }
    }

    private static boolean canNotify(Context context) {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU
            || ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
    }

    private static int requestCode(String tag, String what) {
        return (tag + "|" + what).hashCode();
    }

    private static String tagOf(Map<String, String> data) {
        String tag = value(data, KEY_TAG);
        if (!tag.isEmpty()) return tag;
        String item = value(data, "item_id");
        return item.isEmpty() ? "olano" : "inbox:" + item;
    }

    private static String value(Map<String, String> data, String key) {
        String value = data.get(key);
        return value == null ? "" : value;
    }

    /** A push names the deployment it came from; one the app is no longer pointed at is not answered. */
    private static boolean sameHost(String origin, String host) {
        if (host == null || host.isEmpty()) return true;
        try {
            String theirs = host.contains("://") ? new URL(host).getHost() : host;
            String ours = new URL(origin).getHost();
            return theirs.toLowerCase(Locale.ROOT).equals(ours.toLowerCase(Locale.ROOT));
        } catch (Exception e) {
            return false;
        }
    }
}
