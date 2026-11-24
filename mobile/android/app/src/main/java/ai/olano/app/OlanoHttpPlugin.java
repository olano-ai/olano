package ai.olano.app;

import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Base64;
import android.webkit.CookieManager;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okio.ByteString;

/**
 * HTTP to the chosen deployment, from native code.
 *
 * <p>The app's screens are bundled (https://localhost) and the deployment is
 * another origin, so the page cannot call it with fetch(): the deployment sends
 * no CORS headers and its session cookie is first-party to it alone. Native
 * code has neither limit, keeps the session cookie in the WebView's persistent
 * {@link CookieManager} and, unlike CapacitorHttp, can hand a response over
 * while it is still arriving, which the chat's server-sent events need.
 *
 * <p>JS name {@code OlanoHttp}. The contract (mirrored by OlanoHttpPlugin.swift):
 *
 * <ul>
 *   <li>{@code request({ url, method?, headers?, body?, responseType?, timeout? })}
 *       resolves {@code { status, headers, body, url }}; {@code body} is text, or
 *       base64 when {@code responseType} is "base64" (images)</li>
 *   <li>{@code stream({ id, url, method?, headers?, body? })} resolves
 *       {@code { status, headers }} once the response starts, then emits
 *       {@code chunk} events {@code { id, data }} (base64) and one {@code end}
 *       event {@code { id, error?, cancelled? }}</li>
 *   <li>{@code cancel({ id })} ends a stream; its {@code end} event still follows</li>
 *   <li>{@code clearCookies()} forgets every cookie of the saved deployment</li>
 *   <li>{@code socketOpen({ id, url, headers? })} opens a WebSocket to an
 *       {@code /api/} path of the deployment ({@code url} is its https://
 *       address; the session cookie goes with the handshake), then emits
 *       {@code socketOpen} {@code { id }}, {@code socketMessage}
 *       {@code { id, binary, data }} (text, or base64 when {@code binary}) and one
 *       {@code socketClose} {@code { id, code, reason, error? }}. The live
 *       browser view is the one user (its picture arrives as binary JPEG frames)</li>
 *   <li>{@code socketSend({ id, text })} sends a text message;
 *       {@code socketClose({ id })} closes it (its {@code socketClose} event
 *       still follows)</li>
 * </ul>
 *
 * <p>Every URL must be on the deployment saved by OlanoShell. Two exceptions:
 * {@code GET <origin>/api/status} on any address, which the connect screen
 * uses to check an address before saving it, and {@code GET} of the app's own
 * public config, {@link ServerUrl#APP_CONFIG_URL} exactly, sent without cookies.
 */
@CapacitorPlugin(name = "OlanoHttp")
public class OlanoHttpPlugin extends Plugin {

    private static final int BUFFER = 8192;

    private final ExecutorService requests = Executors.newFixedThreadPool(4);
    private final ExecutorService streams = Executors.newCachedThreadPool();
    private final Map<String, HttpURLConnection> open = new ConcurrentHashMap<>();
    private final Map<String, Boolean> cancelled = new ConcurrentHashMap<>();
    private final Map<String, WebSocket> sockets = new ConcurrentHashMap<>();

    /**
     * The live browser view's frames arrive as binary messages, as many a second as the page
     * changes. Each crosses to the page as a base64 string of a few hundred kilobytes that the
     * web view evaluates on its main thread, the thread the person's taps need: at the rate a
     * video feed produces frames the page spent every moment unpacking pictures and the taps
     * went nowhere. So a socket's binary messages reach the page at most once per
     * {@link #BINARY_INTERVAL_MS}, the newest one once the interval has passed; nothing but the
     * newest picture is ever wanted. Text messages (the page's state, the pointer) are small and
     * go at once. All three maps are read and written under {@code binaryLock}.
     */
    private static final long BINARY_INTERVAL_MS = 80;
    private final Object binaryLock = new Object();
    private final Map<String, byte[]> binaryHeld = new HashMap<>();
    private final Map<String, Long> binaryLastAt = new HashMap<>();
    private final Map<String, Runnable> binaryTimers = new HashMap<>();
    private final Handler binaryHandler = new Handler(Looper.getMainLooper());

    private void deliverBinary(String id, byte[] bytes) {
        synchronized (binaryLock) {
            long now = SystemClock.uptimeMillis();
            Long last = binaryLastAt.get(id);
            long since = last == null ? BINARY_INTERVAL_MS : now - last;
            if (!binaryTimers.containsKey(id) && since >= BINARY_INTERVAL_MS) {
                binaryLastAt.put(id, now);
            } else {
                binaryHeld.put(id, bytes);
                if (!binaryTimers.containsKey(id)) {
                    Runnable flush = () -> flushBinary(id);
                    binaryTimers.put(id, flush);
                    binaryHandler.postDelayed(flush, Math.max(0, BINARY_INTERVAL_MS - since));
                }
                return;
            }
        }
        emitBinary(id, bytes);
    }

    private void flushBinary(String id) {
        byte[] bytes;
        synchronized (binaryLock) {
            binaryTimers.remove(id);
            bytes = binaryHeld.remove(id);
            if (bytes == null) return;
            binaryLastAt.put(id, SystemClock.uptimeMillis());
        }
        emitBinary(id, bytes);
    }

    private void emitBinary(String id, byte[] bytes) {
        JSObject event = new JSObject();
        event.put("id", id);
        event.put("binary", true);
        event.put("data", Base64.encodeToString(bytes, Base64.NO_WRAP));
        notifyListeners("socketMessage", event);
    }
    /** WebSockets only: no read timeout (a quiet page sends nothing), a ping to keep NAT paths open. */
    private final OkHttpClient socketClient =
        new OkHttpClient.Builder()
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .pingInterval(25, TimeUnit.SECONDS)
            .followRedirects(false)
            .followSslRedirects(false)
            .build();

    @PluginMethod
    public void request(PluginCall call) {
        final HttpURLConnection connection;
        try {
            connection = connect(call, call.getInt("timeout", 30000));
        } catch (Refused e) {
            call.reject(e.getMessage(), "REFUSED");
            return;
        } catch (IOException e) {
            call.reject(e.getMessage(), "NETWORK", e);
            return;
        }
        final boolean base64 = "base64".equals(call.getString("responseType"));
        requests.execute(() -> {
            try {
                send(call, connection);
                int status = connection.getResponseCode();
                storeCookies(connection);
                byte[] bytes = readAll(status >= 400 ? connection.getErrorStream() : connection.getInputStream());
                JSObject result = new JSObject();
                result.put("status", status);
                result.put("headers", headers(connection));
                result.put("body", base64 ? Base64.encodeToString(bytes, Base64.NO_WRAP) : new String(bytes, StandardCharsets.UTF_8));
                result.put("url", connection.getURL().toString());
                call.resolve(result);
            } catch (IOException e) {
                call.reject(e.getMessage() == null ? "No response from the deployment." : e.getMessage(), "NETWORK", e);
            } finally {
                connection.disconnect();
            }
        });
    }

    @PluginMethod
    public void stream(PluginCall call) {
        final String id = call.getString("id");
        if (id == null || id.isEmpty()) {
            call.reject("A stream needs an id.", "REFUSED");
            return;
        }
        final HttpURLConnection connection;
        try {
            // A turn can sit on a slow tool for minutes between two frames.
            connection = connect(call, 600000);
        } catch (Refused e) {
            call.reject(e.getMessage(), "REFUSED");
            return;
        } catch (IOException e) {
            call.reject(e.getMessage(), "NETWORK", e);
            return;
        }
        open.put(id, connection);
        streams.execute(() -> {
            boolean resolved = false;
            String error = null;
            try {
                send(call, connection);
                int status = connection.getResponseCode();
                storeCookies(connection);
                JSObject head = new JSObject();
                head.put("status", status);
                head.put("headers", headers(connection));
                call.resolve(head);
                resolved = true;
                InputStream in = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
                if (in != null) {
                    try (InputStream body = in) {
                        byte[] buffer = new byte[BUFFER];
                        int read;
                        while ((read = body.read(buffer)) != -1) {
                            if (read == 0) continue;
                            JSObject chunk = new JSObject();
                            chunk.put("id", id);
                            chunk.put("data", Base64.encodeToString(buffer, 0, read, Base64.NO_WRAP));
                            notifyListeners("chunk", chunk);
                        }
                    }
                }
            } catch (IOException e) {
                error = e.getMessage() == null ? "The connection was lost." : e.getMessage();
            } finally {
                connection.disconnect();
                open.remove(id);
                boolean wasCancelled = cancelled.remove(id) != null;
                if (!resolved) {
                    call.reject(wasCancelled ? "Cancelled." : (error == null ? "No response." : error), wasCancelled ? "CANCELLED" : "NETWORK");
                }
                JSObject end = new JSObject();
                end.put("id", id);
                if (wasCancelled) {
                    end.put("cancelled", true);
                } else if (error != null) {
                    end.put("error", error);
                }
                notifyListeners("end", end);
            }
        });
    }

    @PluginMethod
    public void cancel(PluginCall call) {
        String id = call.getString("id", "");
        HttpURLConnection connection = open.get(id);
        if (connection != null) {
            cancelled.put(id, Boolean.TRUE);
            // disconnect() from another thread makes the reader's read() throw.
            streams.execute(connection::disconnect);
        }
        call.resolve();
    }

    @PluginMethod
    public void clearCookies(PluginCall call) {
        String saved = ServerStore.get(getContext());
        CookieManager cookies = CookieManager.getInstance();
        if (saved != null) {
            String header = cookies.getCookie(saved);
            if (header != null) {
                for (String pair : header.split(";")) {
                    String name = pair.split("=", 2)[0].trim();
                    if (name.isEmpty()) continue;
                    // Expiring the cookie is the only per-cookie delete CookieManager offers.
                    String attributes = name.startsWith("__Host-") || name.startsWith("__Secure-") ? "; Path=/; Secure" : "; Path=/";
                    cookies.setCookie(saved, name + "=; Max-Age=0" + attributes);
                }
            }
            cookies.flush();
        }
        call.resolve();
    }

    @PluginMethod
    public void socketOpen(PluginCall call) {
        final String id = call.getString("id");
        if (id == null || id.isEmpty()) {
            call.reject("A socket needs an id.", "REFUSED");
            return;
        }
        String raw = call.getString("url");
        URL url;
        try {
            url = new URL(raw);
        } catch (Exception e) {
            call.reject("Not a valid address.", "REFUSED");
            return;
        }
        try {
            check(url, "GET");
        } catch (Refused e) {
            call.reject(e.getMessage(), "REFUSED");
            return;
        }
        if (url.getPath() == null || !url.getPath().startsWith("/api/") || ServerUrl.isAppConfig(url.toString(), "GET")) {
            call.reject("The app only opens sockets to the deployment's API.", "REFUSED");
            return;
        }
        // OkHttp takes the https:// address and upgrades it to a WebSocket.
        Request.Builder request = new Request.Builder().url(url.toString());
        String cookie = CookieManager.getInstance().getCookie(url.toString());
        if (cookie != null && !cookie.isEmpty()) {
            request.header("Cookie", cookie);
        }
        JSObject headers = call.getObject("headers", new JSObject());
        Iterator<String> names = headers.keys();
        while (names.hasNext()) {
            String name = names.next();
            request.header(name, headers.optString(name));
        }
        WebSocket socket = socketClient.newWebSocket(request.build(), new WebSocketListener() {
            @Override
            public void onOpen(WebSocket webSocket, Response response) {
                JSObject event = new JSObject();
                event.put("id", id);
                notifyListeners("socketOpen", event);
            }

            @Override
            public void onMessage(WebSocket webSocket, String text) {
                JSObject event = new JSObject();
                event.put("id", id);
                event.put("binary", false);
                event.put("data", text);
                notifyListeners("socketMessage", event);
            }

            @Override
            public void onMessage(WebSocket webSocket, ByteString bytes) {
                deliverBinary(id, bytes.toByteArray());
            }

            @Override
            public void onClosing(WebSocket webSocket, int code, String reason) {
                webSocket.close(code == 1005 ? 1000 : code, null);
            }

            @Override
            public void onClosed(WebSocket webSocket, int code, String reason) {
                socketEnded(id, code, reason, null);
            }

            @Override
            public void onFailure(WebSocket webSocket, Throwable t, Response response) {
                String message = t.getMessage() == null ? "The connection was lost." : t.getMessage();
                socketEnded(id, response != null ? response.code() : 1006, "", message);
            }
        });
        sockets.put(id, socket);
        call.resolve();
    }

    @PluginMethod
    public void socketSend(PluginCall call) {
        WebSocket socket = sockets.get(call.getString("id", ""));
        String text = call.getString("text");
        if (socket == null) {
            call.reject("The socket is closed.", "CLOSED");
            return;
        }
        if (text == null || !socket.send(text)) {
            call.reject("The message could not be sent.", "NETWORK");
            return;
        }
        call.resolve();
    }

    @PluginMethod
    public void socketClose(PluginCall call) {
        WebSocket socket = sockets.get(call.getString("id", ""));
        if (socket != null) {
            socket.close(1000, null);
        }
        call.resolve();
    }

    /** Reports a socket's end once, however it ended. */
    private void socketEnded(String id, int code, String reason, String error) {
        if (sockets.remove(id) == null) return;
        synchronized (binaryLock) {
            binaryHeld.remove(id);
            binaryLastAt.remove(id);
            Runnable timer = binaryTimers.remove(id);
            if (timer != null) binaryHandler.removeCallbacks(timer);
        }
        JSObject event = new JSObject();
        event.put("id", id);
        event.put("code", code);
        event.put("reason", reason == null ? "" : reason);
        if (error != null) {
            event.put("error", error);
        }
        notifyListeners("socketClose", event);
    }

    @Override
    protected void handleOnDestroy() {
        for (HttpURLConnection connection : open.values()) {
            connection.disconnect();
        }
        for (WebSocket socket : sockets.values()) {
            socket.cancel();
        }
        requests.shutdownNow();
        streams.shutdownNow();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    static final class Refused extends Exception {
        Refused(String message) {
            super(message);
        }
    }

    private HttpURLConnection connect(PluginCall call, int timeoutMs) throws Refused, IOException {
        String raw = call.getString("url");
        String method = call.getString("method", "GET").toUpperCase(java.util.Locale.ROOT);
        URL url;
        try {
            url = new URL(raw);
        } catch (Exception e) {
            throw new Refused("Not a valid address.");
        }
        check(url, method);
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setRequestMethod(method);
        connection.setConnectTimeout(Math.min(timeoutMs, 30000));
        connection.setReadTimeout(timeoutMs);
        connection.setUseCaches(false);
        connection.setInstanceFollowRedirects(false);
        JSObject headers = call.getObject("headers", new JSObject());
        Iterator<String> names = headers.keys();
        while (names.hasNext()) {
            String name = names.next();
            connection.setRequestProperty(name, headers.optString(name));
        }
        // Cookies are the deployment's session; the app config is public and gets none.
        if (!ServerUrl.isAppConfig(url.toString(), method)) {
            String cookie = CookieManager.getInstance().getCookie(url.toString());
            if (cookie != null && !cookie.isEmpty()) {
                connection.setRequestProperty("Cookie", cookie);
            }
        }
        return connection;
    }

    /** Writes the request body, if any. Runs on a worker thread: it touches the network. */
    private static void send(PluginCall call, HttpURLConnection connection) throws IOException {
        String body = call.getString("body");
        if (body == null) return;
        // "base64": the bytes of a file uploaded to an agent's workspace.
        byte[] bytes = "base64".equals(call.getString("bodyEncoding"))
                ? Base64.decode(body, Base64.DEFAULT)
                : body.getBytes(StandardCharsets.UTF_8);
        connection.setDoOutput(true);
        connection.setFixedLengthStreamingMode(bytes.length);
        try (OutputStream out = connection.getOutputStream()) {
            out.write(bytes);
        }
    }

    /** The deployment saved by OlanoShell, or a status probe of any address. */
    private void check(URL url, String method) throws Refused {
        boolean debug = ServerStore.isDebuggable(getContext());
        String scheme = url.getProtocol() == null ? "" : url.getProtocol().toLowerCase(java.util.Locale.ROOT);
        if (!"https".equals(scheme) && !("http".equals(scheme) && debug)) {
            throw new Refused("Only https:// addresses can be used.");
        }
        String origin;
        try {
            origin = ServerUrl.normalize(url.toString(), debug);
        } catch (IllegalArgumentException e) {
            throw new Refused(e.getMessage());
        }
        String saved = ServerStore.get(getContext());
        if (saved != null && saved.equals(origin)) return;
        if ("GET".equals(method) && "/api/status".equals(url.getPath())) return;
        if (ServerUrl.isAppConfig(url.toString(), method)) return;
        throw new Refused("The app only talks to the deployment it is connected to.");
    }

    private static void storeCookies(HttpURLConnection connection) {
        if (ServerUrl.isAppConfig(connection.getURL().toString(), connection.getRequestMethod())) return;
        CookieManager cookies = CookieManager.getInstance();
        boolean changed = false;
        for (Map.Entry<String, List<String>> header : connection.getHeaderFields().entrySet()) {
            if (header.getKey() == null || !"set-cookie".equalsIgnoreCase(header.getKey())) continue;
            for (String value : header.getValue()) {
                cookies.setCookie(connection.getURL().toString(), value);
                changed = true;
            }
        }
        if (changed) cookies.flush();
    }

    private static JSObject headers(HttpURLConnection connection) {
        JSObject out = new JSObject();
        for (Map.Entry<String, List<String>> header : connection.getHeaderFields().entrySet()) {
            if (header.getKey() == null || header.getValue() == null || header.getValue().isEmpty()) continue;
            out.put(header.getKey().toLowerCase(java.util.Locale.ROOT), String.join(", ", header.getValue()));
        }
        return out;
    }

    private static byte[] readAll(InputStream in) throws IOException {
        if (in == null) return new byte[0];
        try (InputStream body = in; ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[BUFFER];
            int read;
            while ((read = body.read(buffer)) != -1) {
                out.write(buffer, 0, read);
            }
            return out.toByteArray();
        }
    }
}
