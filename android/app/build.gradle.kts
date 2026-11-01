plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.sunmuxin.readinglog"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.sunmuxin.readinglog"
        minSdk = 26
        targetSdk = 34
        versionCode = 3
        versionName = "1.2"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

fun copyBuiltApk(variantName: String, apkName: String) {
    val destDir = rootProject.file("../dist")
    destDir.mkdirs()
    val out = File(destDir, "reading-log-b7.apk")
    if (out.exists()) out.delete()
    copy {
        from(layout.buildDirectory.dir("outputs/apk/$variantName"))
        include(apkName)
        into(destDir)
        rename { "reading-log-b7.apk" }
    }
}

afterEvaluate {
    tasks.named("assembleDebug").configure {
        doLast { copyBuiltApk("debug", "app-debug.apk") }
    }
    tasks.named("assembleRelease").configure {
        doLast { copyBuiltApk("release", "app-release.apk") }
    }
}
