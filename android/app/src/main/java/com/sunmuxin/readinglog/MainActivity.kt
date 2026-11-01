package com.sunmuxin.readinglog

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceError
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.Toast

class MainActivity : Activity() {
    private lateinit var web: WebView
    private lateinit var prefs: Prefs

    companion object {
        /** True while MainActivity is resumed — used to skip show_logo / screen-on capture. */
        @Volatile
        var isPreviewResumed: Boolean = false
            private set
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        setContentView(R.layout.activity_main)
        if (Build.VERSION.SDK_INT >= 33) {
            requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        web = findViewById(R.id.web)
        web.setBackgroundColor(0xFFFCFBF8.toInt())
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = true
            allowContentAccess = true
            useWideViewPort = true
            loadWithOverviewMode = false
            setSupportZoom(false)
            builtInZoomControls = false
            displayZoomControls = false
            textZoom = 100
            mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
            cacheMode = WebSettings.LOAD_NO_CACHE
        }
        // Persist wallW/H/mag/orient from 书历 UI → Prefs for lock PNG.
        web.addJavascriptInterface(WallSettingsBridge(this), "ReadingLogNative")
        web.webChromeClient = WebChromeClient()
        web.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView, url: String) {
                // Pull localStorage wall settings even if inputs were not edited this session.
                view.evaluateJavascript(
                    """
                    (function(){
                      try {
                        if (window.syncWallSettingsToAndroid) window.syncWallSettingsToAndroid();
                      } catch (e) {}
                    })();
                    """.trimIndent(),
                    null
                )
            }

            override fun onReceivedError(
                view: WebView,
                request: WebResourceRequest,
                error: WebResourceError
            ) {
                if (request.isForMainFrame) {
                    toast("连不上 Mac：" + (error.description ?: "请在设置里填写地址"))
                }
            }
        }
        findViewById<Button>(R.id.updateWallpaper).setOnClickListener {
            if (!HiddenCapture.overlayAllowed(this)) {
                startActivity(HiddenCapture.overlaySettingsIntent(this))
                toast("请允许显示在其他应用的上层。允许后，壁纸在后台生成，不会打乱预览。")
                return@setOnClickListener
            }
            RefreshService.start(this, "now")
            toast("正在后台生成壁纸")
        }
        findViewById<Button>(R.id.openSettings).setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        if (prefs.wantsBackground()) RefreshService.start(this, null)
    }

    override fun onResume() {
        super.onResume()
        isPreviewResumed = true
        web.onResume()
        // Re-assert FGS after install / process death so SCREEN_OFF stays registered.
        if (prefs.wantsBackground()) RefreshService.start(this, null)
        // Always reload: lock-path wake / show_logo / HiddenCapture can leave the
        // WebView attached but blank (cream/white) even when url host still matches.
        loadPreview()
    }

    override fun onPause() {
        isPreviewResumed = false
        web.onPause()
        super.onPause()
    }

    override fun onStop() {
        // ScreenSave may cover us without a clean pause race; lock show_logo
        // must not stay blocked after leaving the preview.
        isPreviewResumed = false
        super.onStop()
    }

    private fun loadPreview() {
        val base = prefs.baseUrl
        if (base.isEmpty()) {
            startActivity(Intent(this, SettingsActivity::class.java))
            return
        }
        web.loadUrl(Server.previewUrl(base))
    }

    private fun toast(text: String) {
        Toast.makeText(this, text, Toast.LENGTH_LONG).show()
    }
}
