package systems.bake.rook

import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test
import java.net.InetAddress
import java.net.ServerSocket
import kotlin.concurrent.thread

class HubDoorTest {
    /** A one-route HTTP/1.1 server on a plain socket (android.jar has no com.sun.net.httpserver). */
    private val server = ServerSocket(0, 50, InetAddress.getByName("127.0.0.1")).apply { soTimeout = 0 }
    @Volatile private var seen: Pair<String?, JSONObject>? = null
    @Volatile private var answer: Pair<Int, String> = 200 to "{}"

    init {
        thread(isDaemon = true) {
            while (!server.isClosed) {
                val sock = try { server.accept() } catch (_: Exception) { break }
                sock.use { s ->
                    s.soTimeout = 5_000
                    val input = s.getInputStream().buffered()
                    val headers = generateSequence { readLine(input) }.takeWhile { it.isNotEmpty() }.toList()
                    val length = headers.firstOrNull { it.lowercase().startsWith("content-length:") }?.substringAfter(':')?.trim()?.toInt() ?: 0
                    val body = ByteArray(length).also { var off = 0; while (off < length) off += input.read(it, off, length - off) }
                    seen = headers.firstOrNull { it.lowercase().startsWith("authorization:") }?.substringAfter(':')?.trim() to
                        JSONObject(String(body, Charsets.UTF_8))
                    val (code, text) = answer
                    val bytes = text.toByteArray()
                    s.getOutputStream().apply {
                        write("HTTP/1.1 $code X\r\nContent-Type: application/json\r\nContent-Length: ${bytes.size}\r\nConnection: close\r\n\r\n".toByteArray())
                        write(bytes); flush()
                    }
                }
            }
        }
    }

    private fun readLine(input: java.io.InputStream): String? {
        val sb = StringBuilder()
        while (true) {
            val c = input.read()
            if (c < 0) return if (sb.isEmpty()) null else sb.toString()
            if (c == '\n'.code) return sb.toString().trimEnd('\r')
            sb.append(c.toChar())
        }
    }

    @After fun stop() = server.close()

    private fun door() = HubDoor("http://127.0.0.1:${server.localPort}/", "tok-1")

    @Test(timeout = 10_000) fun callsAHubCapAsThePerson() {
        answer = 200 to """{"ok": true, "id": "c1", "result": {"stream": "s1"}}"""
        val r = door().call("speech.open", JSONObject().put("device", "phone"), "because", 5_000)
        assertEquals("s1", r.getString("stream"))
        val (auth, body) = seen!!
        assertEquals("Bearer tok-1", auth)
        assertEquals("rook", body.getString("worker"))
        assertEquals("speech.open", body.getString("cap"))
        assertEquals("because", body.getString("why"))
        assertEquals("phone", body.getJSONObject("args").getString("device"))
    }

    @Test(timeout = 10_000) fun aRefusalSaysWhy() {
        answer = 200 to """{"ok": false, "error": "no voice session s9"}"""
        val e = assertThrows(HubDoor.Refused::class.java) { door().call("speech.read", JSONObject(), "w", 5_000) }
        assertEquals("no voice session s9", e.message)
        answer = 200 to """{"ok": false, "held": true, "hold": "brave-otter"}"""
        val h = assertThrows(HubDoor.Refused::class.java) { door().call("speech.read", JSONObject(), "w", 5_000) }
        assertEquals("waiting for your approval (hold brave-otter)", h.message)
        answer = 401 to """{"ok": false, "error": "a valid bearer token is required"}"""
        assertThrows(HubDoor.Refused::class.java) { door().call("speech.read", JSONObject(), "w", 5_000) }
        answer = 502 to "bad gateway"
        assertThrows(java.io.IOException::class.java) { door().call("speech.read", JSONObject(), "w", 5_000) }
    }

    @Test fun theTokenOnlyTravelsOverHttps() {
        assertEquals("https://rook.example.com", HubDoor.check("https://rook.example.com/"))
        assertEquals("http://localhost:8740", HubDoor.check("http://localhost:8740"))
        assertThrows(IllegalArgumentException::class.java) { HubDoor.check("http://rook.example.com") }
        assertThrows(IllegalArgumentException::class.java) { HubDoor.check("rook.example.com") }
        assertThrows(IllegalArgumentException::class.java) { HubDoor.check("") }
    }
}
