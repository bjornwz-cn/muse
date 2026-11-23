#!/usr/bin/env bash
# EarBuddy 手工构建脚本（Gradle 守护进程在本 VM 环境不可用期间的等价管线）
# 依赖：JDK17、Android SDK(build-tools 36)、Kotlin 编译器 2.3.0，均在本机 ~/tools 与 ~/android-sdk。
# 三方组件：Vosk 离线识别（vendor/vosk.jar + jna.jar + arm64 .so），手工集成。
# 腾讯云实时 ASR（vendor/tx-asr.jar + libqcloud_asr_realtime.so，官方 AAR standard 版解包），手工集成。
set -euo pipefail
export JAVA_HOME=/home/hatch/tools/jdk17
export PATH="$JAVA_HOME/bin:$PATH"
BT=/home/hatch/android-sdk/build-tools/36.0.0
AJ=/home/hatch/android-sdk/platforms/android-36/android.jar
KOTLINC=/home/hatch/tools/kotlinc/bin/kotlinc
PROJ="$(cd "$(dirname "$0")" && pwd)"
OUT="$PROJ/build-manual"
SRC="$PROJ/app/src/main"
VENDOR="$PROJ/vendor"
rm -rf "$OUT"; mkdir -p "$OUT/dex"

# 1) 资源编译与链接（生成 base.apk，含 manifest 与 strings）
"$BT/aapt2" compile --dir "$SRC/res" -o "$OUT/compiled.zip"
"$BT/aapt2" link -o "$OUT/base.apk" -I "$AJ" \
  --manifest "$SRC/AndroidManifest.xml" \
  --min-sdk-version 26 --target-sdk-version 36 \
  --version-code 50 --version-name "0.9.2" \
  "$OUT/compiled.zip"

# 2) Kotlin 编译（android.jar + vosk/jna + tx-asr；-include-runtime 打包 kotlin-stdlib）
JAVA_OPTS="-Xmx768m" "$KOTLINC" $(find "$SRC/java" -name '*.kt') \
  -classpath "$AJ:$VENDOR/vosk.jar:$VENDOR/jna.jar:$VENDOR/okhttp-4.12.0.jar:$VENDOR/okio-jvm-3.6.0.jar:$VENDOR/tx-asr.jar" -include-runtime -d "$OUT/app.jar"

# 3) 转 dex（应用 + vosk + jna + tx-asr 一并 dex）
"$BT/d8" --lib "$AJ" --output "$OUT/dex" "$OUT/app.jar" "$VENDOR/vosk.jar" "$VENDOR/jna.jar" "$VENDOR/okhttp-4.12.0.jar" "$VENDOR/okio-jvm-3.6.0.jar" "$VENDOR/tx-asr.jar"

# 4) 合包：classes.dex + 原生库（.so 须存储不压缩，随后 zipalign -p 对齐）
cp "$OUT/base.apk" "$OUT/unsigned.apk"
(cd "$OUT/dex" && zip -q "$OUT/unsigned.apk" classes.dex)
(cd "$VENDOR/so" && zip -q -0 -r "$OUT/unsigned.apk" lib)
"$BT/zipalign" -p -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"
KS="$PROJ/debug.keystore"
if [ ! -f "$KS" ]; then
  "$JAVA_HOME/bin/keytool" -genkeypair -keystore "$KS" -storepass android -keypass android \
    -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=EarBuddy Debug,O=Bjorn,C=CN" >/dev/null 2>&1
fi
"$BT/apksigner" sign --ks "$KS" --ks-pass pass:android --key-pass pass:android \
  --out "$PROJ/earbuddy-0.9.2.apk" "$OUT/aligned.apk"

# 5) 验签与信息
"$BT/apksigner" verify "$PROJ/earbuddy-0.9.2.apk"
"$BT/aapt2" dump badging "$PROJ/earbuddy-0.9.2.apk" | head -3
ls -la "$PROJ/earbuddy-0.9.2.apk"
echo "BUILD_OK"
