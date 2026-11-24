package ai.olano.app;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class ServerUrlTest {

    @Test
    public void reducesToTheOrigin() {
        assertEquals("https://acme.olano.run", ServerUrl.normalize("https://acme.olano.run", false));
        assertEquals("https://acme.olano.run", ServerUrl.normalize("  https://acme.olano.run/  ", false));
        assertEquals("https://agents.example.com", ServerUrl.normalize("HTTPS://Agents.Example.COM/chat?x=1#y", false));
        assertEquals("https://acme.olano.run", ServerUrl.normalize("https://acme.olano.run:443", false));
        assertEquals("https://agents.example.com:8443", ServerUrl.normalize("https://agents.example.com:8443/", false));
    }

    @Test
    public void plainHttpOnlyInDebugBuilds() {
        assertThrows(IllegalArgumentException.class, () -> ServerUrl.normalize("http://192.168.1.20:8765", false));
        assertEquals("http://192.168.1.20:8765", ServerUrl.normalize("http://192.168.1.20:8765/", true));
        assertEquals("http://box.local", ServerUrl.normalize("http://box.local:80", true));
    }

    @Test
    public void refusesWhatIsNotAnAddress() {
        String[] bad = {
            null,
            "",
            "   ",
            "acme.olano.run",
            "ftp://example.com",
            "javascript:alert(1)",
            "mailto:someone@example.com",
            "https://user:secret@example.com",
            "https:///path-only",
            "https://exa mple.com",
        };
        for (String input : bad) {
            assertThrows(String.valueOf(input), IllegalArgumentException.class, () -> ServerUrl.normalize(input, true));
        }
    }

    @Test
    public void theAppConfigExceptionIsOneExactGet() {
        assertTrue(ServerUrl.isAppConfig("https://cloud.olano.ai/api/v1/mobile/config", "GET"));
        assertFalse(ServerUrl.isAppConfig("https://cloud.olano.ai/api/v1/mobile/config", "POST"));
        assertFalse(ServerUrl.isAppConfig("https://cloud.olano.ai/api/v1/mobile/config?x=1", "GET"));
        assertFalse(ServerUrl.isAppConfig("https://cloud.olano.ai/api/v1/mobile/configs", "GET"));
        assertFalse(ServerUrl.isAppConfig("https://cloud.olano.ai/api/v1/push", "GET"));
        assertFalse(ServerUrl.isAppConfig("http://cloud.olano.ai/api/v1/mobile/config", "GET"));
        assertFalse(ServerUrl.isAppConfig("https://cloud.olano.ai.evil.example/api/v1/mobile/config", "GET"));
    }
}
