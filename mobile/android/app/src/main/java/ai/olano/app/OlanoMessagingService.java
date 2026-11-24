package ai.olano.app;

import androidx.annotation.NonNull;
import com.capacitorjs.plugins.pushnotifications.MessagingService;
import com.google.firebase.messaging.RemoteMessage;

/**
 * Capacitor's push service, plus the notifications that carry buttons.
 *
 * A push with buttons (Approve / Decline, or a Reply field) arrives as data
 * only: Android's own drawing of a notification message has no buttons (the
 * relay's fcm.ts sends the {@code olano_*} keys instead). This draws those
 * ({@link NotificationActionReceiver#show}); everything else, and the hand-off
 * to the app's screens, stays Capacitor's. Registered in place of Capacitor's
 * service in AndroidManifest.xml.
 */
public class OlanoMessagingService extends MessagingService {

    @Override
    public void onMessageReceived(@NonNull RemoteMessage message) {
        if (message.getNotification() == null && message.getData().containsKey(NotificationActionReceiver.KEY_CATEGORY)) {
            NotificationActionReceiver.show(this, message.getMessageId(), message.getData());
        }
        super.onMessageReceived(message);
    }
}
