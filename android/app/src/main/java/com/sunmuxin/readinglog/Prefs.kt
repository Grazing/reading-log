package com.sunmuxin.readinglog

import android.content.Context
import org.json.JSONObject

/**
 * App prefs. Lock PNG uses 书历 wall canvas settings synced from the B7 WebView
 * (localStorage wallW/H/mag + orient) — never Display.getRealSize / wm size.
 */
class Prefs(context: Context) {
    private val app = context.applicationContext
    private val prefs = app.getSharedPreferences("reading-log", Context.MODE_PRIVATE)

    var baseUrl: String
        get() = Server.normalize(prefs.getString(KEY_URL, DEFAULT_URL) ?: DEFAULT_URL)
        set(value) {
            prefs.edit().putString(KEY_URL, Server.normalize(value)).apply()
        }

    var intervalMinutes: Int
        get() = prefs.getInt(KEY_INTERVAL, 30).coerceIn(0, 24 * 60)
        set(value) {
            prefs.edit().putInt(KEY_INTERVAL, value.coerceIn(0, 24 * 60)).apply()
        }

    var updateOnLock: Boolean
        get() = prefs.getBoolean(KEY_LOCK, true)
        set(value) {
            prefs.edit().putBoolean(KEY_LOCK, value).apply()
        }

    /** Logical wallpaper canvas width from B7 书历 UI (wallW / readingLogWallSize). */
    var canvasWidth: Int
        get() {
            // Until WebView syncs, ignore stale wm-size leftovers (e.g. 1264).
            if (!hasWallSettingsSync) return DEFAULT_WALL_W
            return prefs.getInt(KEY_W, DEFAULT_WALL_W).coerceIn(320, 8192)
        }
        set(value) {
            prefs.edit().putInt(KEY_W, value.coerceIn(320, 8192)).apply()
        }

    /** Logical wallpaper canvas height from B7 书历 UI (wallH). */
    var canvasHeight: Int
        get() {
            if (!hasWallSettingsSync) return DEFAULT_WALL_H
            return prefs.getInt(KEY_H, DEFAULT_WALL_H).coerceIn(200, 8192)
        }
        set(value) {
            prefs.edit().putInt(KEY_H, value.coerceIn(200, 8192)).apply()
        }

    /** Magnification from #wallMag / readingLogWallMag (1…4). */
    var canvasMag: Int
        get() {
            if (!hasWallSettingsSync) return DEFAULT_WALL_MAG
            return prefs.getInt(KEY_MAG, DEFAULT_WALL_MAG).coerceIn(1, 4)
        }
        set(value) {
            prefs.edit().putInt(KEY_MAG, value.coerceIn(1, 4)).apply()
        }

    /** "portrait" or "landscape" from B7 orient pick. */
    var wallOrient: String
        get() {
            if (!hasWallSettingsSync) return DEFAULT_WALL_ORIENT
            val raw = (prefs.getString(KEY_ORIENT, DEFAULT_WALL_ORIENT) ?: DEFAULT_WALL_ORIENT)
                .trim().lowercase()
            return if (raw == "landscape") "landscape" else "portrait"
        }
        set(value) {
            val raw = value.trim().lowercase()
            val o = when (raw) {
                "landscape", "横版", "horizontal" -> "landscape"
                else -> "portrait"
            }
            prefs.edit().putString(KEY_ORIENT, o).apply()
        }

    /** True after at least one WebView UI sync of wall size. */
    val hasWallSettingsSync: Boolean
        get() = prefs.getBoolean(KEY_WALL_SYNCED, false)

    fun wantsBackground(): Boolean = intervalMinutes > 0 || updateOnLock

    /**
     * Apply wall canvas settings from the B7 WebView (localStorage / #wallW/H/Mag).
     * Does not touch physical panel resolution.
     */
    fun applyWallSettingsFromUi(
        width: Int,
        height: Int,
        mag: Int = canvasMag,
        orient: String? = null,
    ) {
        val w = width.coerceIn(320, 8192)
        val h = height.coerceIn(200, 8192)
        if (w < 320 || h < 200) return
        val m = mag.coerceIn(1, 4)
        val ed = prefs.edit()
            .putInt(KEY_W, w)
            .putInt(KEY_H, h)
            .putInt(KEY_MAG, m)
            .putBoolean(KEY_WALL_SYNCED, true)
        val o = orient?.trim()?.lowercase().orEmpty()
        when (o) {
            "portrait", "竖版", "vertical" -> ed.putString(KEY_ORIENT, "portrait")
            "landscape", "横版", "horizontal" -> ed.putString(KEY_ORIENT, "landscape")
        }
        ed.apply()
    }

    fun applyWallSettingsJson(raw: String) {
        val obj = JSONObject(raw.ifBlank { "{}" })
        val w = obj.optInt("width", obj.optInt("canvasWidth", 0))
        val h = obj.optInt("height", obj.optInt("canvasHeight", 0))
        val m = obj.optInt(
            "mag",
            obj.optInt("scale", obj.optInt("canvasScale", canvasMag))
        )
        val orient = obj.optString("orient", obj.optString("orientation", ""))
        if (w >= 320 && h >= 200) {
            applyWallSettingsFromUi(w, h, m, orient.ifBlank { null })
        } else if (orient.isNotBlank()) {
            wallOrient = orient
            prefs.edit().putBoolean(KEY_WALL_SYNCED, true).apply()
        }
    }

    companion object {
        const val DEFAULT_URL = "http://192.168.3.120:18766"
        /**
         * 书历竖版设计默认（与 index.html PORTRAIT_PIXEL_* 一致）。
         * Not Hibreak wm size — lock PNG must not use physical panel pixels.
         */
        const val DEFAULT_WALL_W = 2160
        const val DEFAULT_WALL_H = 2880
        const val DEFAULT_WALL_MAG = 1
        const val DEFAULT_WALL_ORIENT = "portrait"

        /** @deprecated Use DEFAULT_WALL_W — kept for call sites that still say B7_WIDTH. */
        const val B7_WIDTH = DEFAULT_WALL_W
        /** @deprecated Use DEFAULT_WALL_H */
        const val B7_HEIGHT = DEFAULT_WALL_H

        private const val KEY_URL = "baseUrl"
        private const val KEY_INTERVAL = "interval"
        private const val KEY_LOCK = "lock"
        private const val KEY_W = "canvasW"
        private const val KEY_H = "canvasH"
        private const val KEY_MAG = "canvasMag"
        private const val KEY_ORIENT = "wallOrient"
        private const val KEY_WALL_SYNCED = "wallSynced"
    }
}
