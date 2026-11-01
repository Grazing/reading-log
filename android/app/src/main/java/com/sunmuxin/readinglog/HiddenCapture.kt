package com.sunmuxin.readinglog

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.PixelFormat
import android.net.Uri
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import android.view.Gravity
import android.view.WindowManager
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Draws the wallpaper in an off-screen WebView. The visible preview is never
 * loaded with the wallpaper page and the app is not brought forward.
 *
 * Never use SCREEN_BRIGHT / ACQUIRE_CAUSES_WAKEUP / FLAG_TURN_SCREEN_ON —
 * those dismiss Hibreak ScreenSave and leave the launcher interactive.
 * Lock-path refresh uses [LockPngFetch] (Mac HTTP PNG → standby.png + show_logo);
 * awake captures keep the CPU with PARTIAL_WAKE_LOCK only.
 */
class HiddenCapture(
    private val context: Context,
    private val onDone: (Boolean, String) -> Unit
) {
    private val main = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()
    private val captureStarted = AtomicBoolean(false)
    private var web: WebView? = null
    private var added = false
    private var closed = false
    private var wake: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    /** True when capture began while the screen was already off (lock path). */
    private var forceStandbyReload = false
    /** Hibreak panel CSS size for capture (synced at start). */
    private var captureW = Prefs.DEFAULT_WALL_W
    private var captureH = Prefs.DEFAULT_WALL_H

    private val timeoutAt = Runnable {
        Log.w(TAG, "timeout ${TIMEOUT_MS}ms — abort")
        finish(false, "生成超时")
    }

    @SuppressLint("SetJavaScriptEnabled", "WakelockTimeout")
    fun start() {
        if (!running.compareAndSet(false, true)) {
            onDone(false, "正在生成壁纸")
            return
        }
        val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        forceStandbyReload = try {
            pm.isInteractive != true
        } catch (_: Exception) {
            false
        }
        // PARTIAL keeps the process scheduling without turning the panel on
        // or dismissing keyguard / ScreenSave.
        wake = pm.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "readinglog:wallpaper"
        ).apply {
            setReferenceCounted(false)
            acquire(TIMEOUT_MS + 5_000L)
        }
        @Suppress("DEPRECATION")
        wifiLock = try {
            val wm = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "readinglog:wallpaper").apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (e: Exception) {
            Log.w(TAG, "wifiLock failed: ${e.message}")
            null
        }
        Log.i(TAG, "start forceStandbyReload=$forceStandbyReload interactive=${pm.isInteractive}")
        if (Build.VERSION.SDK_INT >= 30 && !Environment.isExternalStorageManager()) {
            finish(false, "请先在设置里允许写入壁纸文件")
            return
        }
        if (!Settings.canDrawOverlays(context)) {
            finish(false, "请允许显示在其他应用的上层，壁纸才能在后台生成")
            return
        }
        val prefs = Prefs(context)
        // Wall canvas from B7 书历 UI sync — not physical panel size.
        captureW = prefs.canvasWidth
        captureH = prefs.canvasHeight
        val view = WebView(context)
        web = view
        view.setBackgroundColor(0x00000000)
        view.setLayerType(android.view.View.LAYER_TYPE_HARDWARE, null)
        view.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = true
            useWideViewPort = true
            loadWithOverviewMode = false
            setSupportZoom(false)
            textZoom = 100
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            cacheMode = WebSettings.LOAD_NO_CACHE
            // Keep timers/JS alive while the display was off.
            offscreenPreRaster = true
        }
        view.addJavascriptInterface(
            WallpaperBridge(context, forceStandbyReload) { ok, message ->
                main.post { finish(ok, message) }
            },
            "ReadingLogDevice"
        )
        view.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView, url: String) {
                Log.i(TAG, "onPageFinished $url")
                pollReady(view, 0)
            }
        }
        val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        // No KEEP_SCREEN_ON / TURN_SCREEN_ON / SHOW_WHEN_LOCKED — those wake
        // the panel and pop ScreenSave on Hibreak.
        val params = WindowManager.LayoutParams(
            640,
            960,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS or
                WindowManager.LayoutParams.FLAG_HARDWARE_ACCELERATED,
            PixelFormat.TRANSLUCENT
        )
        params.gravity = Gravity.START or Gravity.TOP
        params.x = -8000
        params.y = -8000
        params.alpha = 0.02f
        try {
            wm.addView(view, params)
            added = true
            Log.i(TAG, "overlay added")
        } catch (e: Exception) {
            Log.e(TAG, "addView failed", e)
            finish(false, e.message ?: "无法在后台生成壁纸")
            return
        }
        main.postDelayed(timeoutAt, TIMEOUT_MS)
        io.execute {
            val page = try {
                Log.i(TAG, "fetchStatus…")
                val st = Server.fetchStatus(prefs.baseUrl, "b7")
                Server.wallpaperUrl(
                    prefs.baseUrl,
                    st,
                    canvasWidth = captureW,
                    canvasHeight = captureH,
                )
            } catch (e: Exception) {
                Log.w(TAG, "fetchStatus failed: ${e.message}")
                main.post { finish(false, e.message ?: "连不上 Mac") }
                return@execute
            }
            Log.i(TAG, "loadUrl $page")
            main.post {
                if (!closed) view.loadUrl(page)
            }
        }
    }

    private fun pollReady(view: WebView, attempt: Int) {
        if (closed || captureStarted.get()) return
        if (attempt > 120) {
            finish(false, "月历加载超时")
            return
        }
        view.evaluateJavascript(
            "(function(){var d=document.documentElement.dataset;return (d.wallpaperError||'')+'|'+(d.wallpaperReady||'');})()"
        ) { raw ->
            if (closed || captureStarted.get()) return@evaluateJavascript
            val text = raw?.trim()?.trim('"')?.replace("\\\"", "") ?: ""
            val parts = text.split("|", limit = 2)
            val err = parts.getOrNull(0).orEmpty()
            val ready = parts.getOrNull(1) == "1"
            if (err.isNotEmpty() && err != "null") {
                finish(false, err)
                return@evaluateJavascript
            }
            if (ready) {
                Log.i(TAG, "wallpaper ready attempt=$attempt")
                beginCapture(view)
            } else {
                if (attempt % 10 == 0) {
                    Log.i(TAG, "pollReady attempt=$attempt raw=$text")
                }
                main.postDelayed({ pollReady(view, attempt + 1) }, 300)
            }
        }
    }

    private fun beginCapture(view: WebView) {
        if (!captureStarted.compareAndSet(false, true)) return
        // Hibreak panel size — never Mac Studio / design defaults from the page.
        val w = captureW
        val h = captureH
        val js = """
            (async function() {
              try {
                if (typeof window.capturePlannerPngBase64 !== 'function') {
                  throw new Error('capturePlannerPngBase64 missing');
                }
                await window.capturePlannerPngBase64(1, $w, $h);
              } catch (e) {
                if (window.ReadingLogDevice) {
                  window.ReadingLogDevice.wallpaperError(String(e && e.message || e));
                }
              }
            })();
        """.trimIndent()
        view.evaluateJavascript(js, null)
    }

    /** Drop overlay without waiting; always notifies [onDone] so callers leave busy. */
    fun abandon() {
        finish(false, "已取消")
    }

    private fun finish(ok: Boolean, message: String) {
        if (!release()) return
        Log.i(TAG, "finish ok=$ok msg=$message")
        onDone(ok, message)
    }

    private fun release(): Boolean {
        if (closed) return false
        closed = true
        running.set(false)
        main.removeCallbacks(timeoutAt)
        val view = web
        web = null
        val wasAdded = added
        added = false
        if (view != null) {
            // Tear down overlay BEFORE destroy. Hibreak leaves a white
            // TYPE_APPLICATION_OVERLAY if destroy() runs while still attached.
            val tearDown = Runnable {
                try {
                    view.stopLoading()
                    view.loadUrl("about:blank")
                } catch (_: Exception) {
                }
                if (wasAdded || view.parent != null) {
                    detachOverlay(view)
                }
                // Destroy on next loop tick only after detach.
                main.post {
                    try {
                        if (view.parent != null) detachOverlay(view)
                    } catch (_: Exception) {
                    }
                    try {
                        view.destroy()
                    } catch (e: Exception) {
                        Log.w(TAG, "WebView.destroy: ${e.message}")
                    }
                }
            }
            if (Looper.myLooper() == Looper.getMainLooper()) tearDown.run()
            else main.post(tearDown)
        }
        try {
            if (wake?.isHeld == true) wake?.release()
        } catch (_: Exception) {
        }
        wake = null
        try {
            if (wifiLock?.isHeld == true) wifiLock?.release()
        } catch (_: Exception) {
        }
        wifiLock = null
        io.shutdownNow()
        return true
    }

    /** Best-effort remove of the off-screen overlay; must not leave a white layer. */
    private fun detachOverlay(view: WebView) {
        val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        try {
            val lp = view.layoutParams
            if (lp is WindowManager.LayoutParams) {
                lp.flags = WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                    WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                    WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS
                lp.alpha = 0f
                lp.width = 1
                lp.height = 1
                lp.x = -10000
                lp.y = -10000
                try {
                    wm.updateViewLayout(view, lp)
                } catch (_: Exception) {
                }
            }
        } catch (_: Exception) {
        }
        try {
            view.visibility = android.view.View.GONE
        } catch (_: Exception) {
        }
        var removed = false
        try {
            wm.removeViewImmediate(view)
            removed = true
            Log.i(TAG, "overlay removeViewImmediate ok")
        } catch (e: Exception) {
            Log.w(TAG, "removeViewImmediate: ${e.message}")
        }
        if (!removed) {
            try {
                wm.removeView(view)
                removed = true
                Log.i(TAG, "overlay removeView ok")
            } catch (e: Exception) {
                Log.e(TAG, "overlay remove failed", e)
            }
        }
        if (!removed && view.parent != null) {
            try {
                (view.parent as? android.view.ViewGroup)?.removeView(view)
                Log.i(TAG, "overlay parent.removeView ok")
            } catch (e: Exception) {
                Log.e(TAG, "overlay parent detach failed", e)
            }
        }
    }

    companion object {
        private const val TAG = "HiddenCapture"
        /** Hard cap so SCREEN_OFF never leaves running=true for minutes. */
        private const val TIMEOUT_MS = 55_000L
        val running = AtomicBoolean(false)

        fun overlayAllowed(context: Context): Boolean = Settings.canDrawOverlays(context)

        fun overlaySettingsIntent(context: Context) =
            android.content.Intent(
                Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                Uri.parse("package:${context.packageName}")
            )
    }
}
