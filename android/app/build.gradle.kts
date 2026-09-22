import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// One permanent key for every build. Android only lets an app update itself
// when the new APK is signed by the same key as the installed one, so a
// throwaway debug key means "uninstall and lose your data" on the first
// machine change. keystore/ is git-ignored; keep a copy somewhere safe.
val signingProps = Properties().apply {
    val f = rootProject.file("keystore/signing.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

android {
    namespace = "dev.flightpath.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "dev.flightpath.app"
        minSdk = 29            // WifiNetworkSpecifier needs API 29; Chaquopy needs 24
        targetSdk = 34
        versionCode = 34
        versionName = "0.1.33"
        // Baked-in default; the app lets the user change it. Pass
        // -PupdateUrl=https://your-service.up.railway.app at build time.
        buildConfigField("String", "UPDATE_URL",
            "\"" + (project.findProperty("updateUrl") as String? ?: "") + "\"")
        ndk {
            // Phones only. Drop x86_64 from a release build; keep it if you want an emulator.
            abiFilters += listOf("arm64-v8a")
        }
    }

    signingConfigs {
        if (signingProps.isNotEmpty()) {
            create("flightpath") {
                storeFile = rootProject.file(signingProps.getProperty("storeFile"))
                storePassword = signingProps.getProperty("storePassword")
                keyAlias = signingProps.getProperty("keyAlias")
                keyPassword = signingProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        debug {
            if (signingProps.isNotEmpty()) signingConfig = signingConfigs.getByName("flightpath")
        }
        release {
            isMinifyEnabled = false
            if (signingProps.isNotEmpty()) signingConfig = signingConfigs.getByName("flightpath")
        }
    }
    buildFeatures { buildConfig = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

chaquopy {
    defaultConfig {
        version = "3.10"
        pip {
            // Same two dependencies as the Termux build. Chaquopy ships Android
            // wheels for both from its own repository.
            install("numpy==1.26.2")
            install("opencv-python==4.5.1.48")
        }
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.3")
    implementation("androidx.webkit:webkit:1.12.1")
    // Live view: the GoPro pushes MPEG-TS/H.264 over UDP; Media3 plays it
    // with the phone's hardware decoder, which OpenCV on Android cannot.
    implementation("androidx.media3:media3-exoplayer:1.4.1")
    implementation("androidx.media3:media3-ui:1.4.1")
}
