package com.sunmuxin.readinglog

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.PowerManager
import android.provider.Settings
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import java.util.concurrent.Executors

class SettingsActivity : Activity() {
    private val io = Executors.newSingleThreadExecutor()
    private lateinit var prefs: Prefs

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        setContentView(R.layout.activity_settings)
        val base = findViewById<EditText>(R.id.baseUrl)
        val interval = findViewById<EditText>(R.id.interval)
        val lock = findViewById<CheckBox>(R.id.updateOnLock)
        val result = findViewById<TextView>(R.id.testResult)
        base.setText(prefs.baseUrl)
        interval.setText(prefs.intervalMinutes.toString())
        lock.isChecked = prefs.updateOnLock

        findViewById<Button>(R.id.testConnection).setOnClickListener {
            val url = Server.normalize(base.text.toString())
            result.text = "正在连接…"
            io.execute {
                val message = try {
                    Server.fetchStatus(url)
                    "已连上 Mac，预览和壁纸用同一套彩色数据"
                } catch (e: Exception) {
                    e.message ?: "连接失败"
                }
                runOnUiThread { result.text = message }
            }
        }
        findViewById<Button>(R.id.save).setOnClickListener {
            save(base, interval, lock)
            Toast.makeText(this, "已保存", Toast.LENGTH_SHORT).show()
            finish()
        }
        findViewById<Button>(R.id.generate).setOnClickListener {
            save(base, interval, lock)
            if (!HiddenCapture.overlayAllowed(this)) {
                startActivity(HiddenCapture.overlaySettingsIntent(this))
                Toast.makeText(this, "请允许显示在其他应用的上层", Toast.LENGTH_LONG).show()
                return@setOnClickListener
            }
            RefreshService.start(this, "now")
            Toast.makeText(this, "正在后台生成壁纸", Toast.LENGTH_SHORT).show()
        }
        findViewById<Button>(R.id.grantFiles).setOnClickListener { requestAllFiles() }
        findViewById<Button>(R.id.grantOverlay).setOnClickListener {
            if (HiddenCapture.overlayAllowed(this)) {
                Toast.makeText(this, "已经可以在后台生成壁纸", Toast.LENGTH_SHORT).show()
            } else {
                startActivity(HiddenCapture.overlaySettingsIntent(this))
            }
        }
        findViewById<Button>(R.id.grantBattery).setOnClickListener { requestBattery() }
    }

    override fun onDestroy() {
        io.shutdownNow()
        super.onDestroy()
    }

    private fun save(
        base: EditText,
        interval: EditText,
        lock: CheckBox
    ) {
        prefs.baseUrl = base.text.toString()
        prefs.intervalMinutes = interval.text.toString().toIntOrNull() ?: 0
        prefs.updateOnLock = lock.isChecked
        if (prefs.wantsBackground()) RefreshService.start(this, null)
        else RefreshService.stop(this)
    }

    private fun requestAllFiles() {
        if (Build.VERSION.SDK_INT < 30 || Environment.isExternalStorageManager()) {
            Toast.makeText(this, "已经可以写入壁纸文件", Toast.LENGTH_SHORT).show()
            return
        }
        try {
            startActivity(
                Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION).apply {
                    data = Uri.parse("package:$packageName")
                }
            )
        } catch (_: Exception) {
            startActivity(Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
        }
    }

    private fun requestBattery() {
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) {
            Toast.makeText(this, "已经允许后台更新", Toast.LENGTH_SHORT).show()
            return
        }
        try {
            startActivity(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                    data = Uri.parse("package:$packageName")
                }
            )
        } catch (_: Exception) {
            Toast.makeText(this, "请在系统设置里允许书历后台运行", Toast.LENGTH_LONG).show()
        }
    }
}
