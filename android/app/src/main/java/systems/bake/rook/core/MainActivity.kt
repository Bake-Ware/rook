package systems.bake.rook.core

import android.annotation.SuppressLint
import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.view.WindowInsets
import android.view.inputmethod.EditorInfo
import android.webkit.CookieManager
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import com.google.zxing.integration.android.IntentIntegrator
import java.io.File
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The app (brief AP1): the hub's site in a full-screen web view. The hub comes from the address
 * stamped into this APK by the hub that served it; with no stamp (a sideloaded build), one screen
 * asks for it: scan the QR code on the hub's /app page, or type the address. The site's own login
 * signs the person in, and once they are, the phone joins the hub as a worker (Hub.join).
 */
class MainActivity : Activity() {
    private var web: WebView? = null
    private var hub: String? = null
    private var picking: ValueCallback<Array<Uri>>? = null
    private val joining = AtomicBoolean(false)
    private val background = Executors.newSingleThreadExecutor()

    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        if (Build.VERSION.SDK_INT >= 30) window.setDecorFitsSystemWindows(false)
        hub = Hub.saved(this) ?: HubStamp.read(File(applicationInfo.sourceDir))?.let(Hub::origin)?.also { Hub.save(this, it) }
        val at = hub
        if (at == null) showAddress() else showSite(at, saved)
    }

    /** The one native screen besides the web view: which hub, for a build that carries none. */
    private fun showAddress() {
        val pad = (24 * resources.displayMetrics.density).toInt()
        val field = EditText(this).apply {
            hint = "rook.example.com"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            imeOptions = EditorInfo.IME_ACTION_GO
            isSingleLine = true
        }
        val note = TextView(this)
        val open = {
            val origin = Hub.origin(field.text.toString())
            if (origin == null) note.text = "That is not a web address. Type the hub's address, like https://rook.example.com."
            else chose(origin)
        }
        field.setOnEditorActionListener { _, _, _ -> open(); true }
        val page = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(pad, pad, pad, pad)
            addView(TextView(context).apply { text = "Rook"; textSize = 28f })
            addView(TextView(context).apply {
                text = "Which hub? Scan the QR code on the hub's /app page, or type its address."
                setPadding(0, pad / 2, 0, pad / 2)
            })
            addView(Button(context).apply {
                text = "Scan QR code"
                setOnClickListener {
                    IntentIntegrator(this@MainActivity).setDesiredBarcodeFormats(IntentIntegrator.QR_CODE)
                        .setPrompt("Scan the QR code on the hub's /app page").setOrientationLocked(false)
                        .setBeepEnabled(false).initiateScan()
                }
            }, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
            addView(field, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT).apply { topMargin = pad })
            addView(Button(context).apply { text = "Open"; setOnClickListener { open() } },
                LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
            addView(note)
        }
        setContentView(inset(FrameLayout(this).apply { addView(page, MATCH_PARENT, MATCH_PARENT) }))
    }

    private fun chose(origin: String) {
        Hub.save(this, origin)
        hub = origin
        showSite(origin, null)
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun showSite(at: String, saved: Bundle?) {
        Hub.startWorker(this)
        CookieManager.getInstance().setAcceptCookie(true)
        val view = WebView(this)
        view.settings.javaScriptEnabled = true
        view.settings.domStorageEnabled = true
        view.webViewClient = object : WebViewClient() {
            /** Links to other hosts open in the browser; the hub's own stay here. */
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url.toString()
                if (Hub.origin(url) == at) return false
                outside(url)
                return true
            }

            /**
             * The site asks /api/session once it has a session (just after signing in) and on every
             * load: the cookie store is written to disk then, so a force-stop, which skips onPause,
             * keeps the person signed in. And each request to the hub's API, until the phone has
             * joined: try to join (Hub.join).
             */
            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
                val url = request.url.toString()
                if (url.startsWith("$at/api/session")) CookieManager.getInstance().flush()
                if (url.startsWith("$at/api/") && !Hub.joined(this@MainActivity) && joining.compareAndSet(false, true)) {
                    background.execute {
                        try { Hub.join(this@MainActivity, at) } catch (e: Exception) { /* again at the next request */ }
                        joining.set(false)
                    }
                }
                return null
            }
        }
        view.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>,
                                           params: FileChooserParams): Boolean {
                picking?.onReceiveValue(null)
                picking = callback
                try {
                    startActivityForResult(params.createIntent(), PICK)
                } catch (e: ActivityNotFoundException) {
                    picking = null
                    return false
                }
                return true
            }
        }
        view.setDownloadListener { url, _, _, _, _ -> outside(url) }
        web = view
        setContentView(inset(FrameLayout(this).apply { addView(view, MATCH_PARENT, MATCH_PARENT) }))
        if (saved?.let(view::restoreState) == null) view.loadUrl("$at/")
    }

    private fun outside(url: String) {
        try { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) } catch (e: ActivityNotFoundException) { }
    }

    /** Android 15 draws apps edge to edge: keep the content clear of the system bars and the keyboard. */
    private fun inset(content: View): View {
        content.setOnApplyWindowInsetsListener { v, insets ->
            if (Build.VERSION.SDK_INT >= 30) {
                val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.ime())
                v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
                WindowInsets.CONSUMED
            } else insets
        }
        return content
    }

    @Deprecated("Activity's own result API; this app has no AndroidX activity")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode == PICK) {
            picking?.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data))
            picking = null
            return
        }
        val scanned = IntentIntegrator.parseActivityResult(requestCode, resultCode, data)?.contents
        if (scanned != null) Hub.origin(scanned)?.let(::chose)
        else super.onActivityResult(requestCode, resultCode, data)
    }

    @Deprecated("Back is the web view's back; predictive back is not opted into")
    override fun onBackPressed() {
        val view = web
        if (view != null && view.canGoBack()) view.goBack() else super.onBackPressed()
    }

    override fun onSaveInstanceState(out: Bundle) {
        super.onSaveInstanceState(out)
        web?.saveState(out)
    }

    override fun onPause() {
        super.onPause()
        CookieManager.getInstance().flush()     // the session survives the app being stopped
    }

    companion object {
        private const val PICK = 1
    }
}
