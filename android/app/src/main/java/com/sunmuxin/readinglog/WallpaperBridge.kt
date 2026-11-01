package com.sunmuxin.readinglog

import android.content.Context
import android.util.Base64
import android.webkit.JavascriptInterface
import java.util.concurrent.atomic.AtomicBoolean

class WallpaperBridge(
    private val context: Context,
    private val forceStandbyReload: Boolean,
    private val onDone: (Boolean, String) -> Unit
) {
    private val buf = StringBuilder()
    private val finished = AtomicBoolean(false)

    @JavascriptInterface
    fun beginWallpaper(width: Int, height: Int, length: Int) {
        synchronized(buf) { buf.setLength(0) }
    }

    @JavascriptInterface
    fun wallpaperChunk(part: String) {
        synchronized(buf) { buf.append(part) }
    }

    @JavascriptInterface
    fun endWallpaper() {
        if (!finished.compareAndSet(false, true)) return
        val b64 = synchronized(buf) { buf.toString() }
        try {
            val bytes = Base64.decode(b64, Base64.DEFAULT)
            val path = WallpaperStore.savePng(context, bytes, forceStandbyReload)
            onDone(true, path)
        } catch (e: Exception) {
            onDone(false, e.message ?: "保存失败")
        }
    }

    @JavascriptInterface
    fun wallpaperError(message: String) {
        if (!finished.compareAndSet(false, true)) return
        onDone(false, message.ifEmpty { "生成失败" })
    }
}
