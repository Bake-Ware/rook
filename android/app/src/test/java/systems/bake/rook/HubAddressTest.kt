package systems.bake.rook

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class HubAddressTest {
    @Test fun localHosts() {
        for (h in listOf("10.0.0.5", "10.255.255.255", "172.16.0.1", "172.31.255.254", "192.168.1.20",
                "127.0.0.1", "169.254.10.10", "100.64.0.1", "100.127.255.255", "localhost", "LOCALHOST",
                "hub.localhost", "rook.local", "rook.local.", "Hub.LAN", "nas.home.arpa", "rook.internal",
                "[::1]", "::1", "[fd12:3456::1]", "fc00::1", "[fe80::1]", "fe80::1%wlan0", "febf::1",
                "::ffff:192.168.1.20", "::ffff:c0a8:114", "0:0:0:0:0:0:0:1"))
            assertTrue(h, HubAddress.isLocalHost(h))
    }

    @Test fun publicHosts() {
        for (h in listOf("8.8.8.8", "172.15.0.1", "172.32.0.1", "192.169.0.1", "100.63.0.1", "100.128.0.1",
                "11.0.0.1", "169.255.0.1", "256.1.1.1", "10.1", "167772161", "rook.example.com",
                "local", "lan.example.com", "localhost.example.com", "evil-local", "internal.example.org",
                "2001:db8::1", "[2606:4700::1111]", "fec0::1", "::ffff:8.8.8.8", "::", "fe80:::1", "1::2::3",
                "zz::1", "", " ", null))
            assertFalse(h.toString(), HubAddress.isLocalHost(h))
    }

    @Test fun serverOriginAllowsHttpOnlyOnTheLocalNetwork() {
        assertEquals("https://rook.example.com", HubAddress.serverOrigin(" https://rook.example.com/ "))
        assertEquals("https://rook.example.com:8443", HubAddress.serverOrigin("https://rook.example.com:8443"))
        assertEquals("http://192.168.1.20:7005", HubAddress.serverOrigin("http://192.168.1.20:7005"))
        assertEquals("http://10.0.2.2:17605", HubAddress.serverOrigin("http://10.0.2.2:17605/"))
        assertEquals("http://rook.local:7005", HubAddress.serverOrigin("http://rook.local:7005"))
        assertEquals("http://[fd00::5]:7005", HubAddress.serverOrigin("http://[fd00::5]:7005"))
        assertEquals("HTTP://192.168.1.20:7005", HubAddress.serverOrigin("HTTP://192.168.1.20:7005"))
        for (bad in listOf("http://rook.example.com", "http://8.8.8.8:7005", "http://[2001:db8::1]:7005",
                "ftp://192.168.1.20", "192.168.1.20:7005", "rook.example.com", "",
                "https://user@rook.example.com", "http://user:pw@192.168.1.20:7005",
                "https://rook.example.com/path", "http://192.168.1.20:7005/enroll",
                "https://rook.example.com?x=1", "http://192.168.1.20:7005#frag",
                "http://192.168.1.20.evil.com", "http://10.0.0.1.nip.io:7005")) {
            try { HubAddress.serverOrigin(bad); fail(bad) } catch (_: IllegalArgumentException) {}
        }
    }

    @Test fun refusalSaysWhatToDo() {
        val e = try { HubAddress.serverOrigin("http://rook.example.com"); null } catch (e: IllegalArgumentException) { e }
        assertTrue(e!!.message!!.contains("local network"))
    }

    @Test fun cleartextOnlyToLocalHosts() {
        assertTrue(HubAddress.cleartextAllowed("wss://voice.example.com/ws"))
        assertTrue(HubAddress.cleartextAllowed("https://voice.example.com/api/voice"))
        assertTrue(HubAddress.cleartextAllowed("ws://192.168.1.30:8080/ws"))
        assertTrue(HubAddress.cleartextAllowed("WS://voice.lan/ws"))
        assertTrue(HubAddress.cleartextAllowed("http://[fe80::2]:8080/"))
        assertFalse(HubAddress.cleartextAllowed("ws://voice.example.com/ws"))
        assertFalse(HubAddress.cleartextAllowed("Ws://voice.example.com/ws"))
        assertFalse(HubAddress.cleartextAllowed("http://8.8.8.8/"))
        assertFalse(HubAddress.cleartextAllowed("ws://192.168.1.30@voice.example.com/ws"))
    }

    @Test fun serverBandsUseTheWebSocketBridge() {
        assertTrue(HubAddress.bandUsesWebSocket(JSONObject("""{"hub":"192.168.1.20:7005"}""")))
        assertTrue(HubAddress.bandUsesWebSocket(JSONObject("""{"hub":"192.168.1.20:7005","transport":"ws"}""")))
        assertFalse(HubAddress.bandUsesWebSocket(JSONObject("""{"hub":"192.168.1.20:7474","transport":"udp"}""")))
    }
}
