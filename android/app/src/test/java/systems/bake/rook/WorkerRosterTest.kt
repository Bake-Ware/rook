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

    private val account = """{"running":true,"self_id":"me","identity":true,"band_id":"b1","band_name":"Home",
        "workers":[
            {"worker_id":"me","name":"pixel","last_seen_age_secs":0},
            {"worker_id":"w1","name":"desk","last_seen_age_secs":2},
            {"worker_id":"heard","name":"new laptop","last_seen_age_secs":4}
        ],
        "hub":{"scope":"account","age_secs":10,"bands":[
            {"id":"b3","name":"attic","current":false,"workers":[]},
            {"id":"b2","name":"Lab","current":false,"workers":[
                {"worker_id":"p1","name":"pi","caps":2,"last_seen_age_secs":300,"build":119,"hb":{"battery":{"percent":20}}},
                {"worker_id":"p2","name":"gpu","caps":9,"last_seen_age_secs":5}]},
            {"id":"b1","name":"Home","current":true,"workers":[
                {"worker_id":"w1","name":"desk","caps":4,"last_seen_age_secs":40},
                {"worker_id":"me","name":"pixel","caps":30,"last_seen_age_secs":12},
                {"worker_id":"far","name":"server","caps":7,"last_seen_age_secs":20}]},
            {"name":"no id"}, "junk"
        ]}}"""

    @Test fun accountRosterGroupsByBandCurrentFirst() {
        val r = WorkerRoster.parse(account)
        assertEquals(RosterSource.ACCOUNT, r.source); assertNull(r.notice)
        assertEquals(listOf("Home", "attic", "Lab"), r.bands.map { it.name })   // current, then by name
        val home = r.bands[0]
        assertTrue(home.current)
        // Local and hub rows merged: each worker once, freshest sighting, self first.
        assertEquals(listOf("pixel", "desk", "new laptop", "server"), home.workers.map { it.name })
        assertEquals(2.0, home.workers[1].ageSecs, 0.0)
        assertTrue(home.workers[0].self)
        assertEquals(listOf("gpu", "pi"), r.bands[2].workers.map { it.name })    // online first
        assertEquals("Lab", r.bands[2].workers[1].band)
        assertEquals(6, r.workers.size); assertEquals(5, r.onlineCount)
    }

    @Test fun fallsBackToTheLocalRosterAndSaysWhy() {
        val noHub = account.replace("\"hub\":{", "\"hub_was\":{")
        val failed = WorkerRoster.parse(noHub)
        assertEquals(RosterSource.LOCAL_HUB_UNAVAILABLE, failed.source)
        assertEquals(listOf("Home"), failed.bands.map { it.name })
        assertEquals(listOf("pixel", "desk", "new laptop"), failed.workers.map { it.name })
        assertTrue(failed.notice!!.contains("couldn't be loaded"))

        val psk = WorkerRoster.parse(account.replace("\"identity\":true", "\"identity\":false"))
        assertEquals(RosterSource.LOCAL_NOT_ENROLLED, psk.source)     // a hub copy is ignored
        assertEquals(1, psk.bands.size); assertEquals(3, psk.workers.size)
        assertTrue(psk.notice!!.contains("band key"))

        val paired = WorkerRoster.parse(account.replace("\"scope\":\"account\"", "\"scope\":\"band\""))
        assertEquals(RosterSource.HUB_BAND_ONLY, paired.source)
        assertTrue(paired.notice!!.contains("paired"))

        val unnamed = WorkerRoster.parse("""{"running":true,"workers":[{"worker_id":"x","last_seen_age_secs":1}]}""")
        assertEquals("This band", unnamed.bands.single().name)
    }

    @Test fun agoLabels() {
        assertEquals("5s ago", WorkerRoster.ago(5.9))
        assertEquals("1m ago", WorkerRoster.ago(60.0))
        assertEquals("2h ago", WorkerRoster.ago(7300.0))
        assertEquals("3d ago", WorkerRoster.ago(3 * 86400.0 + 5))
    }
}
