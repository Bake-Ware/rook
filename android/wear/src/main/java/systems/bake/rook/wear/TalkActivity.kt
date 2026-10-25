package systems.bake.rook.wear

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.wear.compose.material.Button
import androidx.wear.compose.material.ButtonDefaults
import androidx.wear.compose.material.MaterialTheme
import androidx.wear.compose.material.Text

/**
 * Opening Rook on the watch starts listening: from the launcher, the tile, or the home key's
 * double press (Settings > Advanced features > Customize keys). The big button ends the
 * conversation, or stops a reply while one plays.
 */
class TalkActivity : ComponentActivity() {
    private val askMic = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) TalkService.start(this)
        else Talk.update { Talk.State(Talk.Phase.ERROR, error = "Rook needs the microphone") }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            val state by Talk.state.collectAsState()
            LaunchedEffect(state.active) {
                if (state.active) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            }
            TalkScreen(state, onTap = ::tapped)
        }
        if (savedInstanceState == null) startTalking()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (!Talk.state.value.active) startTalking()
    }

    private fun tapped() {
        if (Talk.state.value.active) TalkService.tap(this) else startTalking()
    }

    private fun startTalking() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) TalkService.start(this)
        else askMic.launch(Manifest.permission.RECORD_AUDIO)
    }
}

@Composable
private fun TalkScreen(state: Talk.State, onTap: () -> Unit) {
    val (label, color) = when (state.phase) {
        Talk.Phase.IDLE -> "Talk" to Color(0xFF3A3A3C)
        Talk.Phase.CONNECTING -> "…" to Color(0xFF5E5CE6)
        Talk.Phase.LISTENING -> "Listening" to Color(0xFF30B0C7)
        Talk.Phase.THINKING -> "Thinking" to Color(0xFFBF5AF2)
        Talk.Phase.SPEAKING -> "Stop" to Color(0xFF34C759)
        Talk.Phase.ERROR -> "Retry" to Color(0xFFFF453A)
    }
    val line = when {
        state.phase == Talk.Phase.ERROR || (state.error.isNotEmpty() && !state.active) -> state.error
        state.reply.isNotEmpty() -> state.reply
        state.heard.isNotEmpty() -> state.heard
        state.phase == Talk.Phase.IDLE -> "Tap to talk"
        else -> ""
    }
    MaterialTheme {
        Box(Modifier.fillMaxSize().background(Color.Black), contentAlignment = Alignment.Center) {
            Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Center) {
                Button(onClick = onTap, modifier = Modifier.size(104.dp),
                    colors = ButtonDefaults.buttonColors(backgroundColor = color)) {
                    Text(label, textAlign = TextAlign.Center)
                }
                Text(line, modifier = Modifier.padding(start = 24.dp, end = 24.dp, top = 8.dp),
                    style = MaterialTheme.typography.caption2, textAlign = TextAlign.Center,
                    maxLines = 3, overflow = TextOverflow.Ellipsis)
            }
        }
    }
}
