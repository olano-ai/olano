package ai.olano.app;

import android.content.Context;
import androidx.core.app.NotificationChannelCompat;
import androidx.core.app.NotificationManagerCompat;
import java.util.Arrays;

/**
 * The notification channels a deployment's pushes are filed under.
 *
 * The ids are a contract with the box and the cloud relay: a push carries
 * {@code channel} (olano/push/events.py), which the relay sends to FCM as
 * {@code channel_id}. A channel that does not exist on the phone falls back to
 * FCM's generic one, so these are created at every launch (creating an existing
 * channel only refreshes its name and description; the importance a person
 * chose in Settings is kept).
 */
final class NotificationChannels {

    static final String REPLIES = "replies";
    static final String APPROVALS = "approvals";
    static final String CORTEX = "cortex";
    static final String ALERTS = "alerts";

    private NotificationChannels() {}

    static void ensure(Context context) {
        NotificationManagerCompat.from(context).createNotificationChannelsCompat(
            Arrays.asList(
                channel(context, REPLIES, NotificationManagerCompat.IMPORTANCE_DEFAULT, R.string.channel_replies_name, R.string.channel_replies_description),
                channel(context, APPROVALS, NotificationManagerCompat.IMPORTANCE_HIGH, R.string.channel_approvals_name, R.string.channel_approvals_description),
                channel(context, CORTEX, NotificationManagerCompat.IMPORTANCE_DEFAULT, R.string.channel_cortex_name, R.string.channel_cortex_description),
                channel(context, ALERTS, NotificationManagerCompat.IMPORTANCE_DEFAULT, R.string.channel_alerts_name, R.string.channel_alerts_description)
            )
        );
    }

    private static NotificationChannelCompat channel(Context context, String id, int importance, int name, int description) {
        return new NotificationChannelCompat.Builder(id, importance)
            .setName(context.getString(name))
            .setDescription(context.getString(description))
            .build();
    }
}
