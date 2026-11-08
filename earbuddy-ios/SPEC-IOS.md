# 耳机助手 iOS 版 — 方案与平台边界（SPEC-IOS）

结论先行：**翻译功能可 1:1 移植；通知播报在 iOS 上做不了**（系统级限制，不是开发量问题）。

## 功能映射

| Android 功能 | iOS 可行性 | 说明 |
|---|---|---|
| 实时同传（麦克风→识别→翻译→字幕/朗读） | ✅ 可做 | Speech 框架（SFSpeechRecognizer，支持端上识别中/英）+ AVAudioEngine 收音 + AVSpeechSynthesizer 朗读；翻译接口与 Android 版相同（谷歌免费接口 / DeepL 填 Key） |
| 收音来源切换（手机麦/蓝牙耳机麦） | ✅ 可做 | AVAudioSession 蓝牙 HFP 路由 |
| 翻译记录保存/回看 | ✅ 可做 | 本地 JSON 存储 |
| 悬浮窗按钮（跨 App 触发） | ❌ | iOS 无悬浮窗权限；只能 App 内操作（可加桌面小组件/快捷指令入口作弱替代） |
| 通知播报（读其他 App 通知→规则引擎→耳机播报） | ❌ **系统禁止** | iOS 不向任何 App 开放读取其他 App 通知的接口（无 Android NotificationListenerService 对应物）。苹果自家「Siri 播报通知」是系统功能，仅支持 AirPods/Beats，**不认华为 FreeBuds 等第三方耳机**，且无自定义规则 |
| 蓝牙耳机门闸/绑定设备选择 | ⚠️ 无对象 | 播报功能本身不存在，门闸无意义 |
| 保活自检 | ⚠️ 无对象 | iOS 后台模型不同，且无通知监听可保活 |
| App 内 OTA 更新 | ❌ | iOS 只允许 App Store/TestFlight 更新 |

**iOS 版定位**：同传工具（面对面翻译 + 记录），通知播报继续由 Android 版承担。若团队里 iPhone 用户要通知播报，唯一路径是换 AirPods 并开系统「播报通知」（系统规则，无白名单/时段等自定义能力）。

## 构建与分发（硬前提）

- iOS App **只能在 macOS + Xcode 上编译签名**，本 VM（Linux）做不了。构建机 = 王震的 MacBook Pro（Muse 配对通道上线后可远程驱动构建）。
- 签名/分发三选一：
  1. **免费个人签名**：用 Apple ID 在 Xcode 免账号费，App 7 天过期需重签，只适合自测。
  2. **Apple Developer 账号（$99/年）+ TestFlight**：团队内部分发正道，审核较轻，适合团队试用。
  3. App Store 上架：与「团队内部」定位不符，不推荐。

## 本目录内容

- `Sources/`：SwiftUI 源码（同传核心已按 Android v0.3.x 同等逻辑实现：端上识别、停顿断句、双语字幕、译文朗读、方向切换、记录保存）。
- `EarBuddyIOS.xcodeproj/`：Xcode 16 工程文件（文件系统同步组，Sources 放进文件即自动入工程）。
- 首次构建步骤：Mac 上打开 xcodeproj → 选签名团队（个人 Apple ID 即可）→ 连 iPad/iPhone 运行。Info.plist 已含麦克风/语音识别权限文案。

## 待王震裁决

签名分发方式（免费签名先自测 / 开发者账号走 TestFlight）。
