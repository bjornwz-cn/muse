# EarBuddy — 蓝牙耳机智能助手（Android）

常驻后台的 Android App：把普通蓝牙耳机变成「通知播报员 + 随身同传」。
中英走火山流式 ASR 2.0；日/韩走流式化的一句话识别（能量 VAD 切段，见 `TranslateService.kt`）。

## 构建

```bash
./build-apk.sh   # 产出 earbuddy-<版本>.apk，验签通过才算成功
```

构建脚本是手工管线（本机 Gradle 守护进程不可用时期的等价做法）：
依赖 JDK 17、Android SDK（build-tools 36）、Kotlin 编译器 2.3.0，
脚本内路径 `~/tools`、`~/android-sdk` 是开发机上的位置，换机器要改。
`vendor/` 里是构建要用的 jar（Vosk / JNA / OkHttp / Okio）和 arm64 `.so`，
缺了编不出包。

## 签名

`debug.keystore` **不在仓库里**。`build-apk.sh` 找不到它会自动生成一把，
但重新生成的 key 签出的包无法覆盖安装已装的旧版（签名不一致）。
要保持 OTA 升级不断档，须用原来那把 keystore 构建；它只保存在开发机本地。

## OTA 更新

`update/earbuddy-latest.apk` + `update/latest.json` 是 App 内检查更新的来源。
发新版时：构建 → 复制 APK 到 `update/` → 改 `latest.json` 的
versionCode / versionName / notes → 提交推送。

## 凭据

仓库里没有任何密钥。火山 ASR / 翻译等凭据在 App 设置页里填，
只存在手机本地，不进代码、不进仓库。

## 版本记录

- v0.8.9：方向 A——日/韩流式化一句话识别（VAD 切段 + 聚合器按音频时间计时 + 识别失败重试 1 次）。实测火山流式 ASR 不支持日语（只返回不准的罗马音），故未走真流式。
- v0.8.8：「播报时降低媒体音量」做成功能开关（默认关），音频焦点全流程日志 + 看门狗防焦点泄漏。
- v0.8.7：播报不再压低媒体音量（混音），修媒体音量播完回不来的问题。

更早的版本历史见 `update/latest.json` 的提交记录。
