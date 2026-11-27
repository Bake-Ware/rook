package systems.bake.rook.core

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * The app reads back the address the hub stamps (brief AP1). The two APKs are made by the hub's
 * own test (tests/test_phone_app.py), which checks that the hub's stamp_apk turns the first into
 * the second; here the app's reader finds the address in the second and none in the first.
 */
class HubStampTest {
    private fun fixture(name: String) = File(javaClass.classLoader!!.getResource("stamp/$name")!!.toURI())

    @Test fun readsTheAddressTheHubStamped() =
        assertEquals("http://10.0.2.2:8123", HubStamp.read(fixture("stamped.apk")))

    @Test fun aBuildWithNoStampHasNoAddress() = assertNull(HubStamp.read(fixture("unstamped.apk")))

    @Test fun aHubIsItsOrigin() {
        assertEquals("http://10.0.2.2:8123", Hub.origin("http://10.0.2.2:8123/app/rook.apk"))   // the /app QR code
        assertEquals("https://rook.example.com", Hub.origin("rook.example.com"))
        assertEquals("https://rook.example.com", Hub.origin(" https://Rook.example.com/ "))
        assertEquals("http://192.168.1.5:8740", Hub.origin("http://192.168.1.5:8740/workers?x=1"))
        assertNull(Hub.origin(""))
        assertNull(Hub.origin("ftp://rook.example.com"))
        assertNull(Hub.origin("not an address"))
    }
}
