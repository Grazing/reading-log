package com.sunmuxin.readinglog

import android.content.Context
import android.util.Log
import android.webkit.JavascriptInterface

/**
 * WebView → Prefs: persist 书历 wallW/wallH/wallMag/orient so lock PNG
 * can request the same canvas without reading Display metrics.
 */
class WallSettingsBridge(context: Context) {
    private val prefs = Prefs(context.applicationContext)

    @JavascriptInterface
    fun syncWallSettings(json: String) {
        try {
            prefs.applyWallSettingsJson(json)
            Log.i(
                TAG,
                "synced wall ${prefs.canvasWidth}x${prefs.canvasHeight} " +
                    "mag=${prefs.canvasMag} orient=${prefs.wallOrient}"
            )
        } catch (e: Exception) {
            Log.w(TAG, "syncWallSettings: ${e.message}")
        }
    }

    companion object {
        private const val TAG = "WallSettingsBridge"
    }
}
