package ai.olano.app;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.Locale;

/**
 * Validates a deployment address and reduces it to its origin
 * ({@code scheme://host[:port]}, no path, no trailing slash), which is the only
 * form the shell stores and hands to the bridge as {@code server.url}.
 *
 * Plain Java (no android.* types) so the rules are covered by a JVM unit test.
 * The iOS twin is {@code ServerURL} in OlanoShellPlugin.swift.
 */
final class ServerUrl {

    /**
     * The app's remote config (src/appConfig.ts, APP_CONFIG_URL): the one URL
     * besides a status probe that OlanoHttp fetches off the saved deployment,
     * always with GET and never with cookies. The iOS twin is
     * {@code OlanoHttpPlugin.appConfigURL}.
     */
    static final String APP_CONFIG_URL = "https://cloud.olano.ai/api/v1/mobile/config";

    private ServerUrl() {}

    /** True for exactly {@code GET APP_CONFIG_URL}: no other method, path, query or host. */
    static boolean isAppConfig(String url, String method) {
        return "GET".equals(method) && APP_CONFIG_URL.equals(url);
    }

    /**
     * @param raw what the page passed to {@code OlanoShell.setServer({ url })}
     * @param allowHttp true only in debug builds, for a deployment on the local network
     * @return the normalised origin
     * @throws IllegalArgumentException with a message fit to show a person
     */
    static String normalize(String raw, boolean allowHttp) {
        String text = raw == null ? "" : raw.trim();
        if (text.isEmpty()) {
            throw new IllegalArgumentException("Enter the address of an Olano deployment.");
        }

        URI uri;
        try {
            uri = new URI(text);
        } catch (URISyntaxException e) {
            throw new IllegalArgumentException("That is not a valid web address.");
        }

        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.ROOT);
        if (scheme.isEmpty() || uri.isOpaque()) {
            throw new IllegalArgumentException("The address must start with https://.");
        }
        if (scheme.equals("http")) {
            if (!allowHttp) {
                throw new IllegalArgumentException("Only https:// addresses can be opened in this build of the app.");
            }
        } else if (!scheme.equals("https")) {
            throw new IllegalArgumentException("Only https:// addresses can be opened.");
        }
        if (uri.getRawUserInfo() != null) {
            throw new IllegalArgumentException("Leave the user name and password out of the address.");
        }

        String host = uri.getHost();
        if (host == null || host.isEmpty()) {
            throw new IllegalArgumentException("The address is missing a valid host name.");
        }
        host = host.toLowerCase(Locale.ROOT);

        int port = uri.getPort();
        boolean defaultPort = port == -1 || (scheme.equals("https") && port == 443) || (scheme.equals("http") && port == 80);
        return scheme + "://" + host + (defaultPort ? "" : ":" + port);
    }
}
