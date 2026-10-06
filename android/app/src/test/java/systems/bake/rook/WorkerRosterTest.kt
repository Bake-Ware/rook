package systems.bake.rook

import org.junit.Assert.*
import org.junit.Test

class WorkerRosterTest {
    private val json = """{"running":true,"self_id":"me","workers":[
        {"worker_id":"b2","name":"zeta","caps":4,"version":"120.a.b","build":120,"last_seen_age_secs":200,"hb":{"battery":{"percent":55,"charging":false}}},
        {"worker_id":"a1","name":"Alpha","caps":10,"version":"121.c.d","build":121,"last_seen_age_secs":12.5,"description":"desk"},
        {"worker_id":"me","name":"pixel","self":true,"caps":30,"build":121,"app_release":{"platform":"android","version":"0.4.10","code":14},"last_seen_age_secs":0,"hb":{"battery":{"percent":81,"charging":true}}},
        {"worker_id":"c3","name":"beta","caps":-2,"last_seen_age_secs":30},
        {"name":"no id"},
        "junk"
    ]}"""

    @Test fun parsesAndSortsSelfThenOnlineThenName() {
        val r = WorkerRoster.parse(json)
        assertTrue(r.running)
        assertEquals(listOf("pixel", "Alpha", "beta", "zeta"), r.workers.map { it.name })
        assertEquals(3, r.onlineCount)
        val me = r.workers[0]
        assertTrue(me.self); assertEquals("81% ⚡", me.batteryLabel); assertEquals("build 121 · app 0.4.10", me.buildLabel)
        val zeta = r.workers[3]
        assertFalse(zeta.online); assertEquals("55%", zeta.batteryLabel)
        assertEquals("offline · seen 3m ago", WorkerRoster.presence(zeta))
        assertEquals(0, r.workers[2].caps)                 // negative clamped
        assertNull(r.workers[2].batteryLabel); assertEquals("", r.workers[2].buildLabel)
        assertEquals("desk", r.workers[1].description)
    }

    @Test fun onlineBoundaryMatchesTheHub() {
        val r = WorkerRoster.parse("""{"running":true,"workers":[{"worker_id":"x","last_seen_age_secs":65},{"worker_id":"y","last_seen_age_secs":65.1}]}""")
        assertEquals(listOf(true, false), r.workers.map { it.online })
        assertEquals("x", r.workers[0].name)                // falls back to the id
    }

    @Test fun malformedOrStoppedIsEmptyAndNotRunning() {
        assertEquals(WorkerRoster(false, emptyList()), WorkerRoster.parse("not json"))
        assertEquals(WorkerRoster(false, emptyList()), WorkerRoster.parse("""{"running":false,"workers":[]}"""))
        val missingAge = WorkerRoster.parse("""{"running":true,"workers":[{"worker_id":"x"}]}""").workers[0]
        assertFalse(missingAge.online); assertEquals("offline · seen never", WorkerRoster.presence(missingAge))
    }

    @Test fun agoLabels() {
        assertEquals("5s ago", WorkerRoster.ago(5.9))
        assertEquals("1m ago", WorkerRoster.ago(60.0))
        assertEquals("2h ago", WorkerRoster.ago(7300.0))
        assertEquals("3d ago", WorkerRoster.ago(3 * 86400.0 + 5))
    }
}
