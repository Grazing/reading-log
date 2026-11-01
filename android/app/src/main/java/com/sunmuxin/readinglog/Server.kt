package com.sunmuxin.readinglog

import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

object Server {
    fun normalize(raw: String): String {
        var url = raw.trim()
        if (url.isEmpty()) return ""
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            url = "http://$url"
        }
        return url.trimEnd('/')
    }

    fun fetchStatus(base: String, device: String? = null): JSONObject {
        val root = normalize(base)
        if (root.isEmpty()) throw IllegalStateException("还没有填写 Mac 地址")
        val suffix = device?.trim().orEmpty().let { id ->
            if (id.isEmpty()) "" else "?device=" + URLEncoder.encode(id, "UTF-8")
        }
        val conn = (URL("$root/api/status$suffix").openConnection() as HttpURLConnection).apply {
            connectTimeout = 8000
            readTimeout = 8000
            instanceFollowRedirects = true
            requestMethod = "GET"
        }
        try {
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
            if (code !in 200..299) {
                throw IllegalStateException("Mac 没有响应（$code）")
            }
            val obj = JSONObject(text.ifEmpty { "{}" })
            if (obj.has("ok") && !obj.optBoolean("ok", true)) {
                throw IllegalStateException(obj.optString("error", "Mac 上的书历没有就绪"))
            }
            return obj
        } finally {
            conn.disconnect()
        }
    }

    fun previewUrl(base: String): String = normalize(base) + "/?device=1"

    /**
     * Color calendar for the wallpaper file. Width, height, magnification, and
     * 横版/竖版 come from the size the user set on the page, not a fixed screen.
     */
    fun wallpaperUrl(
        base: String,
        status: JSONObject?,
        canvasWidth: Int = Prefs.DEFAULT_WALL_W,
        canvasHeight: Int = Prefs.DEFAULT_WALL_H,
    ): String {
        val q = mutableListOf(
            "wallpaper" to "1",
            "device" to "b7",
            "canvasWidth" to canvasWidth.coerceIn(320, 8192).toString(),
            "canvasHeight" to canvasHeight.coerceIn(200, 8192).toString()
        )
        val orient = status?.optJSONObject("plannerOrient")?.optString("orient")
            ?.ifEmpty { null }
            ?: status?.optString("plannerOrient")?.takeIf { it == "portrait" || it == "landscape" }
        if (!orient.isNullOrBlank()) {
            q += "orient" to orient
        }
        val value = status?.optString("wallpaperValue").orEmpty().ifEmpty {
            status?.optString("wallpaperMode").orEmpty()
        }
        if (value == "recent" || value == "prev-recent") {
            q += "mode" to "recent"
            q += "offset" to if (value == "prev-recent") "1" else "0"
        } else if (status != null) {
            val year = status.optInt("wallpaperYear", 0)
            val month = status.optInt("wallpaperMonth", 0)
            if (year > 0 && month in 1..12) {
                q += "year" to year.toString()
                q += "month" to month.toString()
            }
        }
        val query = q.joinToString("&") { (k, v) ->
            URLEncoder.encode(k, "UTF-8") + "=" + URLEncoder.encode(v, "UTF-8")
        }
        return normalize(base) + "/?" + query
    }

    /**
     * Ready calendar PNG from Mac (html2canvas / WKWebView there). Used on Hibreak
     * lock so the phone never needs an overlay WebView while ScreenSave is up.
     *
     * Always asks for device=b7 size/mode — Mac desktop wall settings are ignored.
     */
    fun fetchWallpaperPng(
        base: String,
        fresh: Boolean = false,
        width: Int = Prefs.DEFAULT_WALL_W,
        height: Int = Prefs.DEFAULT_WALL_H,
        mag: Int = Prefs.DEFAULT_WALL_MAG,
        orient: String? = null,
    ): ByteArray {
        val root = normalize(base)
        if (root.isEmpty()) throw IllegalStateException("还没有填写 Mac 地址")
        val parts = mutableListOf(
            "device" to "b7",
            "width" to width.coerceIn(320, 8192).toString(),
            "height" to height.coerceIn(200, 8192).toString(),
            "mag" to mag.coerceIn(1, 4).toString()
        )
        if (fresh) parts += "fresh" to "1"
        val o = orient?.trim()?.lowercase().orEmpty()
        if (o == "portrait" || o == "landscape") {
            parts += "orient" to o
        }
        val q = "?" + parts.joinToString("&") { (k, v) ->
            URLEncoder.encode(k, "UTF-8") + "=" + URLEncoder.encode(v, "UTF-8")
        }
        val conn = (URL("$root/api/wallpaper/png$q").openConnection() as HttpURLConnection).apply {
            connectTimeout = 12_000
            // fresh=1 may wait for Mac re-render (~55s server-side).
            readTimeout = if (fresh) 70_000 else 20_000
            instanceFollowRedirects = true
            requestMethod = "GET"
        }
        try {
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val bytes = stream?.readBytes() ?: ByteArray(0)
            if (code !in 200..299) {
                val err = bytes.toString(Charsets.UTF_8).ifBlank { "HTTP $code" }
                throw IllegalStateException("Mac 壁纸 PNG 失败（$err）")
            }
            // Same gate as WallpaperStore: never accept 64×64 test seeds (~180B).
            if (bytes.size < 20_000 || bytes[0] != 0x89.toByte() || bytes[1] != 0x50.toByte()) {
                throw IllegalStateException("Mac 返回的壁纸无效（${bytes.size} bytes）")
            }
            return bytes
        } finally {
            conn.disconnect()
        }
    }
}
