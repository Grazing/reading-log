package com.sunmuxin.readinglog

import android.content.Context
import android.net.wifi.WifiManager
import android.os.PowerManager
import android.util.Log

/**
 * Lock-path calendar refresh without overlay WebView.
 *
 * Pull a fresh PNG from Mac over HTTP (already rendered), overwrite
 * standby.png, then show_logo only — never screensaver_alarm / gotoSleep /
 * KEYCODE_SLEEP (those force-lock), and never SCREEN_BRIGHT /
 * ACQUIRE_CAUSES_WAKEUP (those dismiss ScreenSave to home).
 *
 * Canvas size/mag/orient come from Prefs synced from the B7 书历 WebView UI
 * (wallW/wallH/wallMag/orient) — not from Display.getRealSize / wm size.
 */
object LockPngFetch {
    private const val TAG = "LockPngFetch"

    fun publishFromMac(context: Context, fresh: Boolean = true): Pair<Boolean, String> {
        val app = context.applicationContext
        val pm = app.getSystemService(Context.POWER_SERVICE) as PowerManager
        @Suppress("DEPRECATION")
        val wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "readinglog:lockpng").apply {
            setReferenceCounted(false)
            acquire(75_000L)
        }
        @Suppress("DEPRECATION")
        val wifiLock = try {
            val wm = app.getSystemService(Context.WIFI_SERVICE) as WifiManager
            wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "readinglog:lockpng").apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (e: Exception) {
            Log.w(TAG, "wifiLock: ${e.message}")
            null
        }
        return try {
            val prefs = Prefs(app)
            val w = prefs.canvasWidth
            val h = prefs.canvasHeight
            val mag = prefs.canvasMag
            val orient = prefs.wallOrient
            Log.i(
                TAG,
                "GET /api/wallpaper/png device=b7 ${w}x${h} mag=$mag orient=$orient " +
                    "synced=${prefs.hasWallSettingsSync} fresh=$fresh base=${prefs.baseUrl}"
            )
            val bytes = try {
                Server.fetchWallpaperPng(
                    prefs.baseUrl,
                    fresh = fresh,
                    width = w,
                    height = h,
                    mag = mag,
                    orient = orient,
                )
            } catch (e: Exception) {
                if (!fresh) throw e
                Log.w(TAG, "fresh PNG failed (${e.message}), retry without wait")
                Server.fetchWallpaperPng(
                    prefs.baseUrl,
                    fresh = false,
                    width = w,
                    height = h,
                    mag = mag,
                    orient = orient,
                )
            }
            WallpaperStore.requireValidStandbyPng(bytes)
            WallpaperStore.savePng(app, bytes, forceStandbyReload = true)
            Log.i(TAG, "published ${bytes.size} bytes → standby")
            true to "已覆盖壁纸"
        } catch (e: Exception) {
            Log.w(TAG, "Mac PNG failed: ${e.message}")
            // Keep previous standby if gallery fallback also fails / is tiny.
            val published = WallpaperStore.publishLastCaptureToStandby(app)
            if (published) {
                false to "Mac 不可用，已用上次相册壁纸"
            } else {
                false to (e.message ?: "锁屏拉取失败（已保留原待机图）")
            }
        } finally {
            try {
                if (wifiLock?.isHeld == true) wifiLock.release()
            } catch (_: Exception) {
            }
            try {
                if (wake.isHeld) wake.release()
            } catch (_: Exception) {
            }
        }
    }
}
