package systems.bake.rook.wear

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.animateColorAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.focusable
import androidx.compose.foundation.gestures.scrollBy
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.rotary.onRotaryScrollEvent
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.wear.compose.material.Button
import androidx.wear.compose.material.ButtonDefaults
import androidx.wear.compose.material.Icon
import androidx.wear.compose.material.MaterialTheme
import androidx.wear.compose.material.Text
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.launch
import systems.bake.rook.wearlink.Transcript

/**
 * Opening Rook on the watch starts listening: from the launcher, the tile, or the home key's
 * double press (Settings > Advanced features > Customize keys). The screen is the conversation
 * as it goes (the phone sends what was said before) with the Rook button below it: its color
 * is the state ([Talk.Phase]). The button ends the conversation, or stops a reply while one plays.
 */
class TalkActivity : ComponentActivity() {
    private val askMic = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) TalkService.start(this)
        else Talk.update { it.ended("Rook needs the microphone") }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        TalkTile.follow(this)
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
    val phase = state.phase
    val lines = state.transcript.lines
    MaterialTheme {
        Box(Modifier.fillMaxSize().background(Color.Black)) {
            if (lines.isEmpty()) RookButton(phase, onTap, 88.dp, Modifier.align(Alignment.Center))
            else {
                Conversation(lines, Modifier.fillMaxSize())
                Box(Modifier.align(Alignment.BottomCenter).fillMaxWidth().height(64.dp)
                    .background(Brush.verticalGradient(listOf(Color.Transparent, Color.Black))))
                RookButton(phase, onTap, 52.dp, Modifier.align(Alignment.BottomCenter).padding(bottom = 8.dp))
            }
        }
    }
}

/** The talk button: a small Rook in the state's color, no words (the state is said to TalkBack). */
@Composable
private fun RookButton(phase: Talk.Phase, onTap: () -> Unit, size: Dp, modifier: Modifier) {
    val tint by animateColorAsState(Color(phase.color), label = "rook")
    Button(onClick = onTap, modifier = modifier.size(size),
        colors = ButtonDefaults.buttonColors(backgroundColor = Color(0xFF202124))) {
        Icon(painterResource(R.drawable.ic_rook_glyph), contentDescription = phase.spoken, tint = tint,
            modifier = Modifier.size(size * 0.5f))
    }
}

/** The conversation, newest at the bottom; it follows new words unless scrolled back (bezel or touch). */
@Composable
private fun Conversation(lines: List<Transcript.Line>, modifier: Modifier) {
    val scroll = rememberScrollState()
    val focus = remember { FocusRequester() }
    val scope = rememberCoroutineScope()
    LaunchedEffect(Unit) {
        focus.requestFocus()
        var lastMax = 0
        snapshotFlow { scroll.maxValue }.filter { it != Int.MAX_VALUE }.collect { max ->
            if (scroll.value >= lastMax - 40) scroll.animateScrollTo(max)
            lastMax = max
        }
    }
    Column(modifier
        .onRotaryScrollEvent { scope.launch { scroll.scrollBy(it.verticalScrollPixels) }; true }
        .focusRequester(focus).focusable()
        .verticalScroll(scroll)
        .padding(start = 26.dp, end = 26.dp, top = 34.dp, bottom = 68.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp)) {
        for (line in lines) when (line.who) {
            Transcript.Who.USER -> Text(line.text,
                modifier = Modifier.align(Alignment.End).background(Color(0xFF2C2C2E), RoundedCornerShape(10.dp))
                    .padding(horizontal = 8.dp, vertical = 4.dp),
                style = MaterialTheme.typography.caption1, color = Color(0xFFE5E5EA))
            Transcript.Who.ROOK -> Text(line.text, modifier = Modifier.fillMaxWidth(),
                style = MaterialTheme.typography.body2, color = Color.White)
            Transcript.Who.NOTE -> Text(line.text, modifier = Modifier.fillMaxWidth(),
                style = MaterialTheme.typography.caption2, color = Color(Talk.Phase.ERROR.color), textAlign = TextAlign.Center)
        }
    }
}
