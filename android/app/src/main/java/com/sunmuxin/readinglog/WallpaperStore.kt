package com.sunmuxin.readinglog

import android.content.ContentUris
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.media.MediaScannerConnection
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.MediaStore
import android.util.Log
import java.io.File
import java.io.FileOutputStream

object WallpaperStore {
    const val DIR_NAME = "书历"
    const val FILE_NAME = "书历.png"
    const val REL_PATH = "Pictures/书历/书历.png"

    /** Hibreak / Bigme standby & lock image path (not the gallery copy). */
    const val HIBREAK_STANDBY_PATH = "/data/xrz/standby.png"
    private const val TAG = "WallpaperStore"
    private const val STANDBY_PKG = "com.xrz.standby"
    /**
     * Reject test seeds / truncated writes. Full calendar PNGs are ~hundreds of KB
     * at device resolution; 64×64 green seeds are ~180B and must never replace standby.
     */
    private const val MIN_STANDBY_BYTES = 20_000
    private const val MIN_STANDBY_EDGE = 800
    /** Hibreak system provider; show_logo restarts ScreenSaveActivity (loads standby.png). */
    private val LOGO_PROVIDER: Uri = Uri.parse("content://com.xrz.LogoProvider")
    private const val METHOD_SHOW_LOGO = "show_logo"
    private const val SCREEN_SAVE_ACTIVITY =
        "com.xrz.standby/com.xrz.settings.screensaver.ScreenSaveActivity"
    private val mainHandler = Handler(Looper.getMainLooper())

    /**
     * True between SCREEN_OFF and the next SCREEN_ON / USER_PRESENT.
     * LockPngFetch may finish after unlock; only show_logo while this is true
     * and the panel is still non-interactive (Asleep / ScreenSave).
     */
    @Volatile
    private var lockSessionActive: Boolean = false

    fun savePng(context: Context, bytes: ByteArray, forceStandbyReload: Boolean = false): String {
        requireValidStandbyPng(bytes)
        val path = try {
            savePublicFile(context, bytes)
        } catch (e: Exception) {
            if (Build.VERSION.SDK_INT >= 29) saveMediaStore(context, bytes) else throw e
        }
        publishHibreakStandby(context, bytes, forceStandbyReload)
        return path
    }

    /**
     * PNG signature + IHDR size gate. Throws if empty/tiny/seed so callers keep
     * the previous standby.png instead of blanking the lock screen.
     */
    fun requireValidStandbyPng(bytes: ByteArray) {
        if (bytes.size < 32 || bytes[0] != 0x89.toByte() || bytes[1] != 0x50.toByte()) {
            throw RuntimeException("生成的不是 PNG")
        }
        if (bytes.size < MIN_STANDBY_BYTES) {
            throw RuntimeException("壁纸过小（${bytes.size} bytes），拒绝覆盖待机图")
        }
        val (w, h) = pngIhdrSize(bytes)
            ?: throw RuntimeException("PNG 头损坏，拒绝覆盖待机图")
        if (w < MIN_STANDBY_EDGE || h < MIN_STANDBY_EDGE) {
            throw RuntimeException("壁纸尺寸过小（${w}x${h}），拒绝覆盖待机图")
        }
    }

    /** Read width/height from PNG IHDR without a full decoder. */
    private fun pngIhdrSize(bytes: ByteArray): Pair<Int, Int>? {
        if (bytes.size < 24) return null
        // bytes 12..15 must be "IHDR"
        if (bytes[12] != 'I'.code.toByte() || bytes[13] != 'H'.code.toByte() ||
            bytes[14] != 'D'.code.toByte() || bytes[15] != 'R'.code.toByte()
        ) {
            return null
        }
        fun be32(i: Int): Int =
            ((bytes[i].toInt() and 0xff) shl 24) or
                ((bytes[i + 1].toInt() and 0xff) shl 16) or
                ((bytes[i + 2].toInt() and 0xff) shl 8) or
                (bytes[i + 3].toInt() and 0xff)
        val w = be32(16)
        val h = be32(20)
        if (w <= 0 || h <= 0) return null
        return w to h
    }

    /** Force LogoProvider show_logo without dismissing keyguard / waking the panel. */
    fun reloadStandbyUi(context: Context) {
        Log.i(TAG, "reloadStandbyUi")
        reloadHibreakScreenSave(context, force = true)
    }

    /** SCREEN_OFF / panel-off: allow asleep show_logo for this lock session. */
    fun markLockSession() {
        lockSessionActive = true
        Log.i(TAG, "markLockSession")
    }

    /**
     * SCREEN_ON / USER_PRESENT: always drop delayed show_logo (never pop over UI).
     * Only USER_PRESENT ends the lock session + optional hide_logo — a brief
     * SCREEN_ON peek must not permanently block a later Asleep reload.
     */
    fun onUserAwake(context: Context, reason: String, dismissScreenSave: Boolean = false) {
        mainHandler.removeCallbacksAndMessages(null)
        if (dismissScreenSave) {
            lockSessionActive = false
            Log.i(TAG, "onUserAwake endSession+cancelPending ($reason)")
            hideLogo(context)
        } else {
            Log.i(TAG, "onUserAwake cancelPending keepSession ($reason)")
        }
    }

    /** Drop delayed show_logo retries without ending the lock session. */
    fun cancelPendingScreenSaveReload(reason: String = "unlock") {
        mainHandler.removeCallbacksAndMessages(null)
        Log.i(TAG, "cancelPendingScreenSaveReload ($reason)")
    }

    /** Dismiss Hibreak ScreenSave if it is covering the UI (recovery / unlock). */
    fun hideLogo(context: Context) {
        try {
            context.contentResolver.call(LOGO_PROVIDER, "hide_logo", null, null as Bundle?)
            Log.i(TAG, "LogoProvider.hide_logo ok")
        } catch (e: Exception) {
            Log.w(TAG, "LogoProvider.hide_logo 失败: ${e.message}")
        }
    }

    private fun isDeviceInteractive(context: Context): Boolean {
        return try {
            context.getSystemService(PowerManager::class.java)?.isInteractive == true
        } catch (_: Exception) {
            true
        }
    }

    /** True only while Asleep in an active lock session — never after unlock. */
    private fun mayReloadScreenSave(context: Context): Boolean {
        if (!lockSessionActive) return false
        if (isDeviceInteractive(context)) return false
        return true
    }

    /** Touch mtime + changeWallpaper so Glide/OEM cache notices the new PNG. */
    private fun pokeStandbyCache(context: Context) {
        try {
            val target = File(HIBREAK_STANDBY_PATH)
            if (target.isFile) {
                target.setLastModified(System.currentTimeMillis())
            }
        } catch (e: Exception) {
            Log.w(TAG, "touch standby mtime: ${e.message}")
        }
        try {
            context.sendBroadcast(Intent("com.xrz.standby.changeWallpaper"))
        } catch (e: Exception) {
            Log.w(TAG, "changeWallpaper: ${e.message}")
        }
    }

    /**
     * Apply the last gallery capture to Hibreak standby and reload ScreenSave.
     * Used when lock-path WebView capture fails after the panel is off.
     */
    fun publishLastCaptureToStandby(context: Context): Boolean {
        val file = File(
            Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_PICTURES),
            "$DIR_NAME/$FILE_NAME"
        )
        if (!file.isFile || file.length() < MIN_STANDBY_BYTES) {
            Log.w(TAG, "publishLastCapture: 无有效相册壁纸 ${file.absolutePath}")
            return false
        }
        val bytes = try {
            file.readBytes()
        } catch (e: Exception) {
            Log.w(TAG, "publishLastCapture: 读取失败 ${e.message}")
            return false
        }
        try {
            requireValidStandbyPng(bytes)
        } catch (e: Exception) {
            Log.w(TAG, "publishLastCapture: ${e.message}")
            return false
        }
        Log.i(TAG, "publishLastCapture → standby (${bytes.size} bytes)")
        publishHibreakStandby(context, bytes, forceStandbyReload = true)
        return true
    }

    private fun savePublicFile(context: Context, bytes: ByteArray): String {
        val pictures = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_PICTURES)
        val dir = File(pictures, DIR_NAME)
        if (!dir.exists() && !dir.mkdirs()) {
            throw RuntimeException("无法创建 " + dir.absolutePath)
        }
        val file = File(dir, FILE_NAME)
        val tmp = File(dir, "$FILE_NAME.tmp")
        FileOutputStream(tmp).use { it.write(bytes) }
        if (file.exists() && !file.delete()) {
            FileOutputStream(file).use { it.write(bytes) }
            tmp.delete()
        } else if (!tmp.renameTo(file)) {
            FileOutputStream(file).use { it.write(bytes) }
            tmp.delete()
        }
        MediaScannerConnection.scanFile(
            context,
            arrayOf(file.absolutePath),
            arrayOf("image/png"),
            null
        )
        return file.absolutePath
    }

    private fun saveMediaStore(context: Context, bytes: ByteArray): String {
        val resolver = context.contentResolver
        val collection = MediaStore.Images.Media.EXTERNAL_CONTENT_URI
        val existing = findExisting(context)
        val uri = existing ?: run {
            val values = ContentValues().apply {
                put(MediaStore.Images.Media.DISPLAY_NAME, FILE_NAME)
                put(MediaStore.Images.Media.MIME_TYPE, "image/png")
                put(MediaStore.Images.Media.RELATIVE_PATH, Environment.DIRECTORY_PICTURES + "/" + DIR_NAME)
                put(MediaStore.Images.Media.IS_PENDING, 1)
            }
            resolver.insert(collection, values) ?: throw RuntimeException("无法写入相册")
        }
        resolver.openOutputStream(uri, "wt")?.use { it.write(bytes) }
            ?: throw RuntimeException("无法覆盖壁纸文件")
        if (existing == null) {
            val done = ContentValues().apply { put(MediaStore.Images.Media.IS_PENDING, 0) }
            resolver.update(uri, done, null, null)
        }
        return REL_PATH
    }

    private fun findExisting(context: Context): android.net.Uri? {
        val resolver = context.contentResolver
        val collection = MediaStore.Images.Media.EXTERNAL_CONTENT_URI
        val projection = arrayOf(MediaStore.Images.Media._ID)
        val selection = "${MediaStore.Images.Media.DISPLAY_NAME}=? AND ${MediaStore.Images.Media.RELATIVE_PATH}=?"
        val args = arrayOf(FILE_NAME, Environment.DIRECTORY_PICTURES + "/" + DIR_NAME + "/")
        resolver.query(collection, projection, selection, args, null)?.use { cursor ->
            if (cursor.moveToFirst()) {
                return ContentUris.withAppendedId(collection, cursor.getLong(0))
            }
        }
        return null
    }

    /**
     * Hibreak lock/standby reads /data/xrz/standby.png. Gallery Pictures/书历/书历.png
     * alone does not refresh the lock image.
     *
     * Do NOT use screensaver AlarmReceiver: it calls updateLogo then immediately
     * gotoSleep, freezing the e-ink before Glide finishes. LogoProvider.show_logo
     * restarts ScreenSaveActivity so it re-decodes standby.png.
     */
    private fun publishHibreakStandby(
        context: Context,
        bytes: ByteArray,
        forceStandbyReload: Boolean = false
    ) {
        try {
            requireValidStandbyPng(bytes)
        } catch (e: Exception) {
            // Never blank an existing good standby with a seed / empty write.
            Log.w(TAG, "拒绝写入 standby: ${e.message}（保留旧图）")
            return
        }
        val target = File(HIBREAK_STANDBY_PATH)
        val wrote = writeStandbyFile(target, bytes) ||
            copyViaShell(bytes, target)
        if (!wrote) {
            Log.w(TAG, "无法写入 $HIBREAK_STANDBY_PATH（保留旧图，不 show_logo）")
            return
        }
        // Only reload UI after a verified full-size write.
        if (!target.isFile || target.length() < MIN_STANDBY_BYTES) {
            Log.w(TAG, "写入后 standby 无效（${target.length()} bytes），跳过 show_logo")
            return
        }
        try {
            target.setReadable(true, false)
            target.setWritable(true, false)
            target.setLastModified(System.currentTimeMillis())
        } catch (e: Exception) {
            Log.w(TAG, "chmod/mtime standby.png 失败", e)
        }
        Log.i(TAG, "已覆盖 Hibreak 待机图 $HIBREAK_STANDBY_PATH (${bytes.size} bytes)")
        try {
            context.sendBroadcast(Intent("com.xrz.standby.changeWallpaper"))
        } catch (e: Exception) {
            Log.w(TAG, "changeWallpaper broadcast: ${e.message}")
        }
        reloadHibreakScreenSave(context, forceStandbyReload)
    }

    /**
     * Recreate / poke ScreenSave while locked so the new standby.png is painted.
     *
     * ONLY while [lockSessionActive] and non-interactive (Asleep). NEVER after
     * USER_PRESENT / SCREEN_ON — even with [force]=true — or ScreenSave covers
     * the launcher. File write still happens; visual reload waits for next lock.
     */
    private fun reloadHibreakScreenSave(context: Context, force: Boolean = false) {
        // Drop any prior delayed show_logo before deciding — unlock may race.
        mainHandler.removeCallbacksAndMessages(null)
        if (!mayReloadScreenSave(context)) {
            val why = when {
                !lockSessionActive -> "无锁屏会话/已解锁"
                isDeviceInteractive(context) -> "屏幕亮着"
                else -> "不可刷新"
            }
            Log.i(TAG, "已写 standby.png，跳过 show_logo（$why） force=$force")
            return
        }
        val appCtx = context.applicationContext
        val tag = if (force) "force" else "lock"
        // Immediate + delayed retries so Glide re-decodes while still Asleep.
        // Do NOT hide_logo first (dismisses ScreenSave → launcher).
        // Never screensaver_alarm / KEYCODE_SLEEP / gotoSleep (强制锁屏).
        pokeStandbyCache(appCtx)
        recreateScreenSave(appCtx, "reload-0ms-$tag")
        mainHandler.postDelayed({
            if (!mayReloadScreenSave(appCtx)) {
                Log.i(TAG, "取消 reload ($tag-0.5s) — awake/unlocked")
                return@postDelayed
            }
            pokeStandbyCache(appCtx)
            recreateScreenSave(appCtx, "reload-0.5s-$tag")
        }, 500L)
        mainHandler.postDelayed({
            if (!mayReloadScreenSave(appCtx)) {
                Log.i(TAG, "取消 reload ($tag-1.8s) — awake/unlocked")
                return@postDelayed
            }
            pokeStandbyCache(appCtx)
            recreateScreenSave(appCtx, "reload-1.8s-$tag")
        }, 1_800L)
    }

    private fun recreateScreenSave(context: Context, reason: String) {
        // Last-chance gate: USER_PRESENT can land between postDelayed and run.
        if (!mayReloadScreenSave(context)) {
            Log.i(TAG, "recreateScreenSave aborted — awake/unlocked ($reason)")
            return
        }
        val resolver = context.contentResolver
        // show_logo alone restarts ScreenSave; hide_logo first unlocks to home.
        try {
            resolver.call(LOGO_PROVIDER, METHOD_SHOW_LOGO, null, null as Bundle?)
            Log.i(TAG, "LogoProvider.$METHOD_SHOW_LOGO ok ($reason)")
        } catch (e: Exception) {
            Log.w(TAG, "LogoProvider.$METHOD_SHOW_LOGO ($reason) 失败: ${e.message}")
            if (!mayReloadScreenSave(context)) {
                Log.i(TAG, "跳过 startActivity ScreenSave — awake ($reason)")
                return
            }
            tryStartScreenSave(context)
        }
    }

    private fun tryStartScreenSave(context: Context) {
        try {
            val intent = Intent().setClassName(
                STANDBY_PKG,
                "com.xrz.settings.screensaver.ScreenSaveActivity"
            ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            context.startActivity(intent)
            Log.i(TAG, "startActivity $SCREEN_SAVE_ACTIVITY")
        } catch (e: Exception) {
            Log.w(TAG, "无法直接启动 ScreenSave（未导出）: ${e.message}")
            try {
                Runtime.getRuntime().exec(
                    arrayOf(
                        "sh", "-c",
                        "am start -n $SCREEN_SAVE_ACTIVITY -f 0x10008000"
                    )
                )
            } catch (e2: Exception) {
                Log.w(TAG, "am start ScreenSave 失败: ${e2.message}")
            }
        }
    }

    private fun writeStandbyFile(target: File, bytes: ByteArray): Boolean {
        return try {
            val parent = target.parentFile
            if (parent != null && !parent.exists() && !parent.mkdirs()) {
                Log.w(TAG, "无法创建 ${parent.absolutePath}")
            }
            // SELinux (xrz_data_file) denies rename/create of .tmp here; overwrite in place.
            // Only call after requireValidStandbyPng — never open the file for a seed.
            FileOutputStream(target).use { it.write(bytes) }
            val ok = target.exists() && target.length() == bytes.size.toLong()
            if (!ok) {
                Log.w(
                    TAG,
                    "standby 写入后长度异常 expect=${bytes.size} actual=${target.length()}（勿 show_logo）"
                )
            }
            ok
        } catch (e: Exception) {
            Log.w(TAG, "直接写入 standby.png 失败: ${e.message}")
            false
        }
    }

    private fun copyViaShell(bytes: ByteArray, target: File): Boolean {
        // App process is still untrusted_app; kept as last-resort fallback only.
        return try {
            val staging = File(
                Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS),
                "readinglog_standby_stage.png"
            )
            FileOutputStream(staging).use { it.write(bytes) }
            val proc = Runtime.getRuntime().exec(
                arrayOf(
                    "sh", "-c",
                    "cp '${staging.absolutePath}' '${target.absolutePath}' && chmod 666 '${target.absolutePath}'"
                )
            )
            val code = proc.waitFor()
            staging.delete()
            code == 0 && target.exists() && target.length() == bytes.size.toLong()
        } catch (e: Exception) {
            Log.w(TAG, "shell 复制 standby.png 失败: ${e.message}")
            false
        }
    }
}
