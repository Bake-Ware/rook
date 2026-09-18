package systems.bake.rook
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class VoiceCatalogTest {
    @Test fun parsesVoicesAndDefault() {
        val c = VoiceCatalog.parse(JSONObject("""{"voices":["af_heart",null,3,"bm_george","af_heart",""],"default":"bm_george"}"""))
        assertEquals(listOf("af_heart", "bm_george"), c.voices)
        assertEquals("bm_george", c.default)
        assertEquals(listOf("saved_custom", "af_heart", "bm_george"), c.choices("saved_custom"))
        assertEquals(listOf("bm_george", "af_heart"), c.choices(null))
    }
    @Test fun labelsKnownPrefixesAndPreservesUnknownIds() {
        assertEquals("Heart (US female)", VoiceCatalog.label("af_heart"))
        assertEquals("Adam (US male)", VoiceCatalog.label("am_adam"))
        assertEquals("Emma (UK female)", VoiceCatalog.label("bf_emma"))
        assertEquals("George (UK male)", VoiceCatalog.label("bm_george"))
        assertEquals("Alpha (Japanese female)", VoiceCatalog.label("jf_alpha"))
        assertEquals("custom_voice", VoiceCatalog.label("custom_voice"))
    }
    @Test fun missingDefaultAndSavedFallback() {
        assertEquals("am_adam", VoiceCatalog.parse(JSONObject("""{"voices":["am_adam"],"default":null}""")).default)
        assertEquals(listOf("af_heart"), VoiceCatalog(emptyList(), VoiceCatalog.FALLBACK).choices(null))
        assertEquals(listOf("custom"), VoiceCatalog(emptyList(), VoiceCatalog.FALLBACK).choices("custom"))
    }
    @Test(expected = IllegalArgumentException::class) fun emptyListFailsForRetry() {
        VoiceCatalog.parse(JSONObject("""{"voices":[]}"""))
    }
    @Test fun endpointUsesHttpsAndDropsSocketPathAndQuery() {
        assertEquals("https://voice.example:8443/voices", VoiceCatalog.endpoint("wss://voice.example:8443/ws?token=secret#part"))
        assertEquals("https://voice.example/voices", VoiceCatalog.endpoint("ws://voice.example/ws"))
    }
}
