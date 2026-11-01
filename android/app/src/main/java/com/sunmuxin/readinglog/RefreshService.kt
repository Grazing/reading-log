package com.sunmuxin.readinglog

import android.app.AlarmManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.hardware.display.DisplayManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import android.view.Display
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

class RefreshService : Service() {
    private var screenReceiver: BroadcastReceiver? = null
    private var displayListener: DisplayManager.DisplayListener? = null
    private var lastCaptureAt = 0L
    private var capture: HiddenCapture? = null
    private var pendingLockCapture = false
    /** Current in-flight job was started from a lock / screen-off path. */
    private var captureLockPath = false
    private var lastInteractive = true
    private var lastLockSource = "broadcast"
    /** ElapsedRealtime until which SCREEN_ON must not start a live capture. */
    private var suppressScreenOnCaptureUntil = 0L
    private val main = Handler(Looper.getMainLooper())
    private val lockIo = Executors.newSingleThreadExecutor()
    private val lockFetchBusy = AtomicBoolean(false)

    private val debouncedLockCapture = Runnable {
        // Stay on ScreenSave: HTTP-GET PNG → standby.png → show_logo only.
        // Never screensaver_alarm / KEYCODE_SLEEP (OEM gotoSleep = 强制锁屏).
        // Cover fresh=1 Mac wait (~55s) + show_logo retries so USER_PRESENT
        // cannot start HiddenCapture and make overwrite look unlock-gated.
        suppressScreenOnCaptureUntil = SystemClock.elapsedRealtime() + 80_000L
        if (!lockFetchBusy.compareAndSet(false, true)) {
            Log.i(TAG, "lock fetch already running ($lastLockSource)")
            return@Runnable
        }
        startInForeground("锁屏拉取月历…")
        val source = lastLockSource
        lockIo.execute {
            try {
                // fresh=true while locked: nudge Mac → wallpaper-b7-capture.png →
                // standby.png + show_logo under PARTIAL_WAKE_LOCK / WifiLock.
                // fresh=false often 404s when no B7 cache yet; unlock HiddenCapture
                // then looked like “overwrite only after unlock”.
                val (ok, message) = LockPngFetch.publishFromMac(this, fresh = true)
                Log.i(TAG, "lock Mac PNG ok=$ok msg=$message ($source)")
                main.post {
                    suppressScreenOnCaptureUntil =
                        maxOf(suppressScreenOnCaptureUntil, SystemClock.elapsedRealtime() + 20_000L)
                    // savePng already poked show_logo when force; no ensure-locked.
                    startInForeground(message)
                }
            } finally {
                // Hold busy so DisplayListener + SCREEN_OFF debounce cannot double-fetch.
                main.postDelayed({ lockFetchBusy.set(false) }, 4_000L)
            }
        }
    }

    private val interactiveWatchdog = object : Runnable {
        override fun run() {
            val nowInteractive = isInteractive()
            if (lastInteractive && !nowInteractive) {
                Log.i(TAG, "watchdog: interactive→off（补 SCREEN_OFF）")
                onScreenOff("watchdog")
            }
            lastInteractive = nowInteractive
            main.postDelayed(this, 1_500L)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        startInForeground()
        lastInteractive = isInteractive()
        listenForScreen()
        listenForDisplay()
        main.post(interactiveWatchdog)
        Log.i(TAG, "RefreshService onCreate interactive=$lastInteractive")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startInForeground("彩色壁纸会在后台覆盖，不打开预览")
        val prefs = Prefs(this)
        val reason = intent?.getStringExtra(EXTRA_REASON)
        Log.i(TAG, "onStartCommand reason=$reason updateOnLock=${prefs.updateOnLock} interval=${prefs.intervalMinutes}")
        if (prefs.wantsBackground()) {
            AlarmReceiver.schedule(this, prefs.intervalMinutes)
        } else if (reason != "now") {
            stopSelf()
            return START_NOT_STICKY
        }
        when (reason) {
            "interval" -> launchCapture(force = false, why = "interval")
            "lock" -> launchCapture(force = true, why = "lock-intent")
            "now" -> launchCapture(force = true, why = "now")
        }
        return START_STICKY
    }

    override fun onDestroy() {
        main.removeCallbacks(interactiveWatchdog)
        main.removeCallbacks(debouncedLockCapture)
        capture?.abandon()
        capture = null
        lockIo.shutdownNow()
        screenReceiver?.let {
            try {
                unregisterReceiver(it)
            } catch (_: Exception) {
            }
        }
        screenReceiver = null
        displayListener?.let {
            try {
                getSystemService(DisplayManager::class.java)?.unregisterDisplayListener(it)
            } catch (_: Exception) {
            }
        }
        displayListener = null
        super.onDestroy()
    }

    private fun listenForScreen() {
        if (screenReceiver != null) return
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context, intent: Intent) {
                when (intent.action) {
                    Intent.ACTION_SCREEN_OFF -> onScreenOff("broadcast")
                    Intent.ACTION_SCREEN_ON, Intent.ACTION_USER_PRESENT -> {
                        val action = intent.action ?: "awake"
                        // End lock session + cancel delayed show_logo so a late
                        // LockPngFetch cannot cover launcher after unlock.
                        val dismiss = action == Intent.ACTION_USER_PRESENT
                        WallpaperStore.onUserAwake(this@RefreshService, action, dismiss)
                        if (capture != null) {
                            Log.i(TAG, "awake: abandon HiddenCapture overlay ($action)")
                            capture?.abandon()
                            capture = null
                        }
                        // When updateOnLock: standby is owned by LockPngFetch on
                        // SCREEN_OFF. USER_PRESENT HiddenCapture used to publish
                        // standby on unlock — that matched “只有解锁才覆盖”.
                        if (Prefs(this@RefreshService).updateOnLock) {
                            Log.i(TAG, "screen-on: skip（锁屏路径负责覆盖 standby）")
                            return
                        }
                        // Interval-only mode: optional awake gallery refresh.
                        if (MainActivity.isPreviewResumed) {
                            Log.i(TAG, "screen-on: skip capture（书历预览在前台）")
                            return
                        }
                        if (SystemClock.elapsedRealtime() < suppressScreenOnCaptureUntil) {
                            Log.i(TAG, "screen-on: skip capture（冷却）")
                            return
                        }
                        if (intent.action == Intent.ACTION_SCREEN_ON) {
                            Log.i(TAG, "screen-on: skip live capture（等 USER_PRESENT）")
                            return
                        }
                        launchCapture(force = false, why = "user-present")
                    }
                }
            }
        }
        val filter = IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_OFF)
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_USER_PRESENT)
        }
        // Hibreak/Android 14: EXPORTED so protected SCREEN_OFF is not dropped by OEM filters.
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(receiver, filter, Context.RECEIVER_EXPORTED)
        } else {
            registerReceiver(receiver, filter)
        }
        screenReceiver = receiver
    }

    private fun listenForDisplay() {
        if (displayListener != null) return
        val dm = getSystemService(DisplayManager::class.java) ?: return
        val listener = object : DisplayManager.DisplayListener {
            override fun onDisplayAdded(displayId: Int) {}
            override fun onDisplayRemoved(displayId: Int) {}
            override fun onDisplayChanged(displayId: Int) {
                if (displayId != Display.DEFAULT_DISPLAY) return
                val state = dm.getDisplay(displayId)?.state ?: return
                if (state == Display.STATE_OFF || state == Display.STATE_DOZE ||
                    state == Display.STATE_DOZE_SUSPEND
                ) {
                    Log.i(TAG, "DisplayListener state=$state")
                    onScreenOff("display")
                }
            }
        }
        dm.registerDisplayListener(listener, main)
        displayListener = listener
    }

    private fun onScreenOff(source: String) {
        val prefs = Prefs(this)
        val preview = MainActivity.isPreviewResumed
        Log.i(
            TAG,
            "onScreenOff source=$source updateOnLock=${prefs.updateOnLock} " +
                "previewResumed=$preview interactive=${isInteractive()}"
        )
        if (!prefs.updateOnLock) return
        lastInteractive = false
        lastLockSource = source
        // Open lock session so savePng may show_logo only while still Asleep.
        WallpaperStore.markLockSession()
        // Always LockPngFetch on true panel-off — do not wait for unlock /
        // USER_PRESENT, and do not skip because 书历 was recently resumed.
        // Coalesce watchdog + DisplayListener + SCREEN_OFF into one capture.
        main.removeCallbacks(debouncedLockCapture)
        main.postDelayed(debouncedLockCapture, 400L)
    }

    private fun isInteractive(): Boolean {
        return try {
            getSystemService(PowerManager::class.java)?.isInteractive == true
        } catch (_: Exception) {
            true
        }
    }

    private fun launchCapture(force: Boolean, why: String) {
        val now = SystemClock.elapsedRealtime()
        val lockPath = why.startsWith("screen-off") || why == "queued-lock" || why == "lock-intent"
        if (HiddenCapture.running.get()) {
            if (force && lockPath && captureLockPath) {
                Log.i(TAG, "launchCapture busy (already lock) → drop ($why)")
                return
            }
            if (force) {
                pendingLockCapture = true
                Log.i(TAG, "launchCapture busy → queue ($why)")
            } else {
                Log.i(TAG, "launchCapture skip: already running ($why)")
            }
            return
        }
        if (!force && now - lastCaptureAt < 20_000) {
            Log.i(TAG, "launchCapture skip: cooldown ${now - lastCaptureAt}ms ($why)")
            return
        }
        if (!HiddenCapture.overlayAllowed(this)) {
            Log.w(TAG, "launchCapture skip: no overlay ($why)")
            startInForeground("请允许显示在其他应用的上层，才能在后台生成壁纸")
            return
        }
        lastCaptureAt = now
        pendingLockCapture = false
        captureLockPath = lockPath
        Log.i(TAG, "launchCapture start force=$force lockPath=$lockPath ($why)")
        startInForeground("正在后台生成壁纸…")
        lateinit var job: HiddenCapture
        job = HiddenCapture(this) { ok, message ->
            if (capture !== job) {
                Log.w(TAG, "stale capture callback ignored ($why)")
                return@HiddenCapture
            }
            capture = null
            captureLockPath = false
            Log.i(TAG, "capture done ok=$ok msg=$message ($why)")
            if (!ok && lockPath) {
                val published = WallpaperStore.publishLastCaptureToStandby(this)
                Log.i(TAG, "lock fallback publishLastCapture=$published")
            }
            // Success already wrote standby; do not start a second capture while awake.
            if (!ok && pendingLockCapture && Prefs(this).updateOnLock) {
                pendingLockCapture = false
                Log.i(TAG, "flush queued lock capture")
                main.post { launchCapture(force = true, why = "queued-lock") }
                return@HiddenCapture
            }
            pendingLockCapture = false
            // savePng / publish already show_logo; do not poke screensaver_alarm
            // or ensure-locked (OEM AlarmReceiver → gotoSleep = 强制锁屏).
            val text = if (ok) "已覆盖壁纸" else message
            if (Prefs(this).wantsBackground()) {
                startInForeground(text)
            } else {
                stopSelf()
            }
        }
        capture = job
        job.start()
    }

    private fun startInForeground() {
        startInForeground("彩色壁纸会在后台覆盖，不打开预览")
    }

    private fun startInForeground(text: String) {
        val manager = getSystemService(NotificationManager::class.java)
        try {
            manager.deleteNotificationChannel("wallpaper")
        } catch (_: Exception) {
        }
        val channel = NotificationChannel(
            CHANNEL,
            "壁纸更新",
            NotificationManager.IMPORTANCE_LOW
        )
        channel.description = "按分钟或锁屏覆盖同一张彩色壁纸"
        channel.lockscreenVisibility = Notification.VISIBILITY_SECRET
        channel.setShowBadge(false)
        manager.createNotificationChannel(channel)
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val notification = Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_menu_gallery)
            .setContentTitle("书历")
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .setVisibility(Notification.VISIBILITY_SECRET)
            .build()
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(1, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(1, notification)
        }
    }

    companion object {
        private const val TAG = "RefreshService"
        private const val CHANNEL = "wallpaper_secret"
        const val EXTRA_REASON = "reason"

        fun start(context: Context, reason: String?) {
            val intent = Intent(context, RefreshService::class.java)
            if (reason != null) intent.putExtra(EXTRA_REASON, reason)
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent)
            else context.startService(intent)
        }

        fun stop(context: Context) {
            AlarmReceiver.schedule(context, 0)
            context.stopService(Intent(context, RefreshService::class.java))
        }
    }
}

class AlarmReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        RefreshService.start(context, "interval")
    }

    companion object {
        fun schedule(context: Context, minutes: Int) {
            val alarm = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
            val pending = pending(context)
            if (minutes <= 0) {
                alarm.cancel(pending)
                return
            }
            val trigger = System.currentTimeMillis() + minutes * 60_000L
            val canExact = if (Build.VERSION.SDK_INT >= 31) alarm.canScheduleExactAlarms() else true
            if (canExact) {
                alarm.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, trigger, pending)
            } else {
                alarm.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, trigger, pending)
            }
        }

        private fun pending(context: Context): PendingIntent {
            val intent = Intent(context, AlarmReceiver::class.java)
            return PendingIntent.getBroadcast(
                context,
                7,
                intent,
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
            )
        }
    }
}

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (!Prefs(context).wantsBackground()) return
        RefreshService.start(context, null)
    }
}
