package com.bjornwz.earbuddy

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast

/**
 * 实时翻译（M2 一期，面对面麦克风）：Vosk 离线流式识别 → 在线翻译 → 双语字幕 + 可选朗读译文。
 * 仲裁：会话期间通知只存不播（NotifyListener 处理），结束时汇总；来电自动暂停识别。
 */
class TranslateActivity : Activity() {

    private lateinit var statusView: TextView
    private lateinit var configView: TextView
    private lateinit var liveView: TextView
    private lateinit var errorView: TextView
    private lateinit var partialView: TextView
    private lateinit var listBox: LinearLayout
    private lateinit var scroll: ScrollView
    private lateinit var dirValue: TextView
    private lateinit var providerValue: TextView
    private lateinit var engineKeyValue: TextView
    private lateinit var startBtn: TextView
    private lateinit var minimizeBtn: TextView
    private lateinit var silenceValue: TextView
    private lateinit var levelBar: android.widget.ProgressBar

    private var from = "zh"
    private var to = "en"
    /** 本地镜像：会话实际在 TranslateService 里跑，这里只跟随其状态刷新 UI。 */
    private var running = false
    /** 悬浮按钮借道本页启动（缺权限/未授权时）：会话起来后退回后台，不打扰原 App。 */
    private var backgroundStart = false
    private val entries = mutableListOf<TranslateStore.Entry>()
    private val handler = Handler(Looper.getMainLooper())
    private var liveTicking = false
    private val liveTicker = object : Runnable {
        override fun run() {
            if (!running) { liveTicking = false; return }
            refreshLiveLine()
            handler.postDelayed(this, 1000)
        }
    }

    private fun volcMode() = TranslateStore.asrEngine(this) == "volc" &&
        TranslateStore.asrKey(this, "volc").isNotEmpty()

    /** 腾讯云实时 ASR 已选且三值（AppId/SecretId/SecretKey）都已填。 */
    private fun txMode() = TranslateStore.asrEngine(this) == "tx" &&
        TranslateStore.txConfigured(this)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.statusBarColor = Ui.ACCENT_DARK
        from = TranslateStore.dirFrom(this)
        to = TranslateStore.dirTo(this)
        val root = Ui.pageRoot(this)

        val head = Ui.card(this)
        head.addView(Ui.title(this, "🎙 实时翻译", 20f))
        head.addView(Ui.body(this, "边说边翻 · 字幕同步到悬浮条 · 翻译中通知只存不播", 12.5f, Ui.SUBTEXT))
        // v0.9.2：开始前就显示实际生效的配置（所见即所得）——旧版只显示「选了什么引擎」，
        // 选了腾讯但没填 Key 时服务会悄悄改走火山/离线，用户以为在用腾讯。
        configView = Ui.body(this, "", 13.5f, Ui.SUBTEXT)
        head.addView(configView)
        statusView = Ui.body(this, "未开始", 15f)
        head.addView(statusView)
        // v0.9.2：进行中实时行（方向/引擎/已耗时/已出句数），每秒刷新。
        liveView = Ui.body(this, "", 13.5f)
        liveView.visibility = android.view.View.GONE
        head.addView(liveView)
        // v0.9.2：识别/引擎错误红字显示（旧版只记日志，界面静默出 0 行）。
        errorView = Ui.body(this, "", 13.5f)
        errorView.setTextColor(android.graphics.Color.parseColor("#B00020"))
        errorView.visibility = android.view.View.GONE
        head.addView(errorView)
        head.addView(Ui.body(this, "收音电平", 12f, Ui.SUBTEXT))
        levelBar = android.widget.ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100
            progress = 0
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, Ui.dp(this@TranslateActivity, 8)
            ).apply { bottomMargin = Ui.dp(this@TranslateActivity, 2) }
        }
        head.addView(levelBar)
        root.addView(head)

        // ——— 主操作：开始/停止 + 最小化 + 记录入口（脱离设置卡，突出显示）———
        startBtn = Ui.button(this, "开始翻译", primary = true) { if (running) stopSession() else startSession() }
        startBtn.textSize = 17f
        startBtn.setPadding(0, Ui.dp(this, 10), 0, Ui.dp(this, 10))
        root.addView(startBtn)
        // v0.9.2：最小化是用户主动点的才切后台（旧版点开始就自动 moveTaskToBack，
        // App 缩到后台、看起来像闪退）。点通知可回到本页。
        minimizeBtn = Ui.button(this, "⬇ 最小化到后台（翻译继续，字幕在屏幕下方）") {
            Toast.makeText(this, "翻译在后台继续，点通知可回到这里", Toast.LENGTH_SHORT).show()
            moveTaskToBack(true)
        }
        minimizeBtn.visibility = android.view.View.GONE
        root.addView(minimizeBtn)
        root.addView(Ui.button(this, "📜 翻译记录") {
            startActivity(Intent(this, RecordsActivity::class.java))
        })

        // ——— 双语字幕（会话内容，置于设置之上）———
        val subs = Ui.card(this)
        subs.addView(Ui.title(this, "双语字幕", 18f))
        listBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        subs.addView(listBox)
        partialView = Ui.body(this, "", 15f, Ui.SUBTEXT)
        subs.addView(partialView)
        root.addView(subs)

        // ——— 设置：按功能分四组，每组一张卡；密钥收进二级入口，不全摊在主页面 ———
        val isVosk = TranslateStore.asrEngine(this) == "vosk"

        // ① 识别：引擎、引擎密钥、语言方向、收音来源；精度与模型导入只对离线引擎有意义
        val asrCard = Ui.card(this)
        asrCard.addView(Ui.title(this, "识别", 18f))
        val engRow = Ui.settingRow(this, "识别引擎", engineLabel()) { pickEngine() }
        asrCard.addView(engRow.view)
        asrCard.addView(Ui.divider(this))
        val keyRow = Ui.settingRow(this, "引擎密钥", keyStatusLabel()) { pickEngineKey() }
        engineKeyValue = keyRow.value
        asrCard.addView(keyRow.view)
        asrCard.addView(Ui.divider(this))
        val dirRow = Ui.settingRow(this, "语言方向", dirLabel()) { toggleDirection() }
        dirValue = dirRow.value
        asrCard.addView(dirRow.view)
        asrCard.addView(Ui.divider(this))
        val micRow = Ui.settingRow(this, "收音来源", micLabel()) { toggleMic() }
        asrCard.addView(micRow.view)
        if (isVosk) {
            asrCard.addView(Ui.divider(this))
            val tierRow = Ui.settingRow(this, "识别精度", tierLabel()) { pickTier() }
            asrCard.addView(tierRow.view)
            asrCard.addView(Ui.divider(this))
            val impRow = Ui.settingRow(this, "导入模型包", "zip 文件") { pickImport() }
            asrCard.addView(impRow.view)
        }
        root.addView(asrCard)

        // ② 翻译：翻译服务、朗读输出
        val mtCard = Ui.card(this)
        mtCard.addView(Ui.title(this, "翻译", 18f))
        val provRow = Ui.settingRow(this, "翻译服务", providerLabel()) { pickProvider() }
        providerValue = provRow.value
        mtCard.addView(provRow.view)
        mtCard.addView(Ui.divider(this))
        val speakRow = Ui.settingRow(this, "朗读输出", speakDestLabel()) { pickSpeakDest() }
        mtCard.addView(speakRow.view)
        root.addView(mtCard)

        // ③ 会话：录音存档、静音自动停
        val sessCard = Ui.card(this)
        sessCard.addView(Ui.title(this, "会话", 18f))
        val recRow = Ui.switchRow(this, "录音存档（会后可分享）", TranslateStore.recordAudio(this)) { c ->
            TranslateStore.setRecordAudio(this, c)
        }
        sessCard.addView(recRow.view)
        sessCard.addView(Ui.divider(this))
        val silRow = Ui.settingRow(this, "静音自动停", silenceLabel()) { pickSilence() }
        silenceValue = silRow.value
        sessCard.addView(silRow.view)
        root.addView(sessCard)

        // ④ 其他：检查更新、诊断与设置
        val curVer = try {
            @Suppress("DEPRECATION")
            packageManager.getPackageInfo(packageName, 0).versionName ?: ""
        } catch (e: Exception) { "" }
        val miscCard = Ui.card(this)
        miscCard.addView(Ui.title(this, "其他", 18f))
        miscCard.addView(Ui.settingRow(this, "检查更新", if (curVer.isEmpty()) "点此检查" else "当前 $curVer") {
            UpdateManager.check(this, true)
        }.view)
        miscCard.addView(Ui.divider(this))
        miscCard.addView(Ui.settingRow(this, "诊断与设置", "初始化、保活自检、诊断日志") {
            startActivity(Intent(this, SetupActivity::class.java))
        }.view)
        root.addView(miscCard)

        scroll = ScrollView(this).apply {
            setBackgroundColor(Ui.BG)
            addView(root)
        }
        setContentView(scroll)
        Ui.fitSystemBars(scroll)
        refreshConfigView()
        if (intent?.getBooleanExtra("auto_start", false) == true) {
            // 借道授权/权限或主界面打开的：自动开始，但留在前台显示实时状态，
            // 不再自动退后台（v0.9.2 修复「点翻译 App 缩小像闪退」）。
            // 只有悬浮按钮借道（background_start）才在启动后退回后台。
            backgroundStart = intent.getBooleanExtra("background_start", false)
            scroll.post { if (!running) startSession() }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (intent.getBooleanExtra("auto_start", false) && !running) {
            backgroundStart = intent.getBooleanExtra("background_start", false)
            startSession()
        }
    }

    override fun onPause() {
        super.onPause()
        TranslateControl.lineListener = null
        TranslateControl.partialListener = null
        TranslateControl.statusListener = null
        TranslateControl.levelListener = null
        TranslateControl.errorListener = null
        handler.removeCallbacks(liveTicker)
        liveTicking = false
        // 切到其他 App 时字幕条接管显示（服务已在显示则幂等）
        if (TranslateControl.active) SubtitleOverlay.show(this)
    }

    /** 开始/停止按钮随状态换字换色：运行中变红，一眼可辨；最小化按钮只在运行中显示。 */
    private fun refreshStartBtn() {
        if (!::startBtn.isInitialized) return
        startBtn.text = if (running) "⏹ 停止翻译" else "▶ 开始翻译"
        startBtn.background = Ui.rounded(if (running) Ui.DANGER else Ui.ACCENT, 12, this)
        if (::minimizeBtn.isInitialized) {
            minimizeBtn.visibility = if (running) android.view.View.VISIBLE else android.view.View.GONE
        }
        if (::liveView.isInitialized) {
            liveView.visibility = if (running) android.view.View.VISIBLE else android.view.View.GONE
            if (running) {
                refreshLiveLine()
                if (!liveTicking) {
                    liveTicking = true
                    handler.postDelayed(liveTicker, 1000)
                }
            } else {
                handler.removeCallbacks(liveTicker)
                liveTicking = false
            }
        }
    }

    override fun onResume() {
        super.onResume()
        SubtitleOverlay.hide()
        from = TranslateStore.dirFrom(this)
        to = TranslateStore.dirTo(this)
        if (::dirValue.isInitialized) dirValue.text = dirLabel()
        if (::engineKeyValue.isInitialized) engineKeyValue.text = keyStatusLabel()
        running = TranslateControl.active
        refreshStartBtn()
        refreshConfigView()
        if (running && TranslateControl.statusText.isNotEmpty()) status(TranslateControl.statusText)
        // 回放本场已完成条目（服务在后台跑时打开本页也能看到完整列表）
        val buf = TranslateControl.entriesBuffer.toList()
        if (buf.size != entries.size) {
            entries.clear()
            if (::listBox.isInitialized) listBox.removeAllViews()
            buf.forEach { renderLine(it.src, it.dst) }
        }
        if (::partialView.isInitialized) {
            partialView.text = if (TranslateControl.partialText.isEmpty()) ""
                else "… ${TranslateControl.partialText}"
        }
        TranslateControl.lineListener = { src, dst -> runOnUiThread { renderLine(src, dst) } }
        TranslateControl.partialListener = { p ->
            runOnUiThread {
                if (::partialView.isInitialized) partialView.text = if (p.isEmpty()) "" else "… $p"
            }
        }
        TranslateControl.statusListener = { s ->
            runOnUiThread {
                status(s)
                val nowActive = TranslateControl.active
                if (nowActive != running && ::startBtn.isInitialized) {
                    running = nowActive
                    refreshStartBtn()
                }
            }
        }
        TranslateControl.levelListener = { pct ->
            runOnUiThread { if (::levelBar.isInitialized) levelBar.progress = pct }
        }
        TranslateControl.errorListener = { msg ->
            runOnUiThread { showError(msg) }
        }
    }

    /** 实际生效的识别引擎（与 TranslateService.startSessionInner 同一套判定，所见即所得）。 */
    private fun effectiveEngineDesc(): String {
        val eng = TranslateStore.asrEngine(this)
        // 服务侧 effFrom：from=auto 且引擎不支持自动检测时回落 zh；日韩判定据此。
        val autoCapable = eng == "volc" && TranslateStore.asrKey(this, "volc").isNotEmpty()
        val eff = if (from == "auto") (if (autoCapable) "auto" else "zh") else from
        val multiLang = eff == "ja" || eff == "ko"
        if (eng == "tx") {
            return if (TranslateStore.txConfigured(this))
                "腾讯·实时识别（${TencentAsr.engineFor(eff)}）"
            else "⚠️ 腾讯引擎未配置密钥（点开始会提示去填，不会悄悄换引擎）"
        }
        if (eng == "volc") {
            return if (TranslateStore.asrKey(this, "volc").isNotEmpty()) {
                if (multiLang) "火山·一句话识别（日韩多语种）" else "火山·流式识别 2.0"
            } else "⚠️ 火山 Key 未填（日韩走云端渠道，中英走离线识别）"
        }
        val ch = CloudAsr.activeChannel(this)
        if (ch != null) return "云端·${ch.name}（离线切分 + 云端重识别）"
        if (eng != "vosk") return "⚠️ ${engineLabel()} Key 未填（将用离线识别）"
        return "离线·${if (TranslateStore.modelTier(this) == "high") "高精度大模型" else "标准小模型"}"
    }

    private fun refreshConfigView() {
        if (!::configView.isInitialized) return
        configView.text = "实际生效：${effectiveEngineDesc()} · 方向：${dirLabel()} · 翻译：${mtName()}"
    }

    private fun refreshLiveLine() {
        if (!::liveView.isInitialized || !running) return
        val startAt = TranslateControl.sessionStartedAt
        val secs = if (startAt > 0) ((System.currentTimeMillis() - startAt) / 1000).coerceAtLeast(0) else 0
        val mm = "%02d:%02d".format(secs / 60, secs % 60)
        val lines = TranslateControl.entriesBuffer.size
        liveView.text = "翻译进行中 · ${dirLabel()} · ${effectiveEngineDesc()} · 已 $mm · 已出 $lines 句"
    }

    private fun showError(msg: String) {
        if (!::errorView.isInitialized) return
        errorView.text = msg
        errorView.visibility = android.view.View.VISIBLE
        Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
    }

    private fun clearError() {
        if (!::errorView.isInitialized) return
        errorView.text = ""
        errorView.visibility = android.view.View.GONE
    }

    private fun dirLabel(): String = when {
        from == "auto" && to == "auto" -> "双向自动·中英互译"
        from == "auto" -> "自动识别 → ${TranslateStore.langName(to)}"
        else -> "${TranslateStore.langName(from)} → ${TranslateStore.langName(to)}"
    }
    private fun mtName(): String = when (TranslateStore.provider(this)) {
        "llm" -> "大模型·带上下文（最聪明）"
        "volc" -> "火山（国内直连）"
        "deepl" -> "DeepL"
        "google" -> "谷歌（国内需代理）"
        else -> "微软（国内可用）"
    }

    private fun providerLabel(): String = mtName()

    private fun toggleDirection() {
        if (running) { Toast.makeText(this, "请先停止再切换方向", Toast.LENGTH_SHORT).show(); return }
        val pairs = listOf(
            "en" to "zh", "zh" to "en",
            "ja" to "zh", "zh" to "ja",
            "ko" to "zh", "zh" to "ko",
            "auto" to "zh", "auto" to "en", "auto" to "auto"
        )
        val labels = arrayOf(
            "English → 中文",
            "中文 → English",
            "日本語 → 中文",
            "中文 → 日本語",
            "한국어 → 中文",
            "中文 → 한국어",
            "自动识别 → 中文（只听中文，源语言自动）",
            "自动识别 → English（只听英文，源语言自动）",
            "双向自动（中英互译，按每句语言自动定方向）"
        )
        val curIdx = pairs.indexOf(from to to).coerceAtLeast(0)
        android.app.AlertDialog.Builder(this)
            .setTitle("翻译方向（日语/韩语走火山多语种识别，用现有火山 Key）")
            .setSingleChoiceItems(labels, curIdx) { dlg, which ->
                val (f, t) = pairs[which]
                from = f; to = t
                TranslateStore.setDir(this, from, to)
                dirValue.text = dirLabel()
                dlg.dismiss()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun pickProvider() {
        val cur = TranslateStore.provider(this)
        val keys = listOf("llm", "volc", "microsoft", "google", "deepl")
        val labels = arrayOf(
            "大模型翻译（带上下文+领域感知，最聪明，需 Key）",
            "火山翻译（国内直连，需 API Key）",
            "微软翻译（免 Key，国内连通性不稳）",
            "谷歌翻译（国内需代理/出境网络）",
            "DeepL（需填 Key，质量更好）"
        )
        android.app.AlertDialog.Builder(this)
            .setTitle("翻译服务")
            .setSingleChoiceItems(labels, keys.indexOf(cur).coerceAtLeast(0)) { dlg, which ->
                when (keys[which]) {
                    "deepl" -> { dlg.dismiss(); askDeeplKey() }
                    "volc" -> { dlg.dismiss(); askVolcMtKeys() }
                    "llm" -> { dlg.dismiss(); askLlmPreset() }
                    else -> {
                        TranslateStore.setProvider(this, keys[which])
                        refreshProviderBtn()
                        dlg.dismiss()
                    }
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun askLlmPreset() {
        val presets = LlmTranslate.PRESETS
        val labels = presets.map { "${it.name} — ${it.note}" }.toTypedArray()
        val curIdx = presets.indexOfFirst { it.id == TranslateStore.llmPreset(this) }.coerceAtLeast(0)
        android.app.AlertDialog.Builder(this)
            .setTitle("选大模型渠道")
            .setSingleChoiceItems(labels, curIdx) { dlg, which ->
                dlg.dismiss()
                askLlmConfig(presets[which])
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun askLlmConfig(preset: LlmTranslate.Preset) {
        val samePreset = TranslateStore.llmPreset(this) == preset.id
        val keyInput = android.widget.EditText(this).apply {
            hint = if (preset.id == "siliconflow") "API Key（识别渠道填过可留空自动复用）" else "API Key"
            if (samePreset) setText(TranslateStore.llmKey(this@TranslateActivity))
            setSingleLine(true)
        }
        val modelInput = android.widget.EditText(this).apply {
            hint = "模型（留空用默认：${preset.model.ifBlank { "需填接入点 ID" }}）"
            if (samePreset) setText(TranslateStore.llmModel(this@TranslateActivity))
            setSingleLine(true)
        }
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 16, 48, 0)
            addView(keyInput)
            addView(modelInput)
        }
        android.app.AlertDialog.Builder(this)
            .setTitle("${preset.name} · 大模型翻译")
            .setView(box)
            .setPositiveButton("保存并启用") { _, _ ->
                TranslateStore.setLlmConfig(
                    this,
                    preset.id,
                    keyInput.text.toString().trim(),
                    modelInput.text.toString().trim()
                )
                if (LlmTranslate.available(this)) {
                    TranslateStore.setProvider(this, "llm")
                    Toast.makeText(this, "已启用大模型翻译（带上下文）", Toast.LENGTH_SHORT).show()
                } else {
                    Toast.makeText(this, "还缺 Key 或模型，先填好再启用", Toast.LENGTH_LONG).show()
                }
                refreshProviderBtn()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun askVolcMtKeys() {
        val keyInput = android.widget.EditText(this).apply {
            hint = "粘贴 API Key（语音技术控制台那把）"
            setText(TranslateStore.volcMtAk(this@TranslateActivity))
            setSingleLine(true)
        }
        android.app.AlertDialog.Builder(this)
            .setTitle("火山翻译 API Key")
            .setView(keyInput)
            .setPositiveButton("保存并启用") { _, _ ->
                val k = keyInput.text.toString().trim()
                if (k.isEmpty()) {
                    Toast.makeText(this, "Key 为空，已取消", Toast.LENGTH_SHORT).show()
                } else {
                    TranslateStore.setVolcMtKey(this, k)
                    TranslateStore.setProvider(this, "volc")
                    Toast.makeText(this, "已启用火山翻译", Toast.LENGTH_SHORT).show()
                }
                refreshProviderBtn()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun askDeeplKey() {
        val input = android.widget.EditText(this).apply {
            hint = "粘贴 DeepL API Key"
            setText(TranslateStore.deeplKey(this@TranslateActivity))
            setSingleLine(true)
        }
        android.app.AlertDialog.Builder(this)
            .setTitle("DeepL API Key")
            .setView(input)
            .setPositiveButton("保存") { _, _ ->
                val k = input.text.toString().trim()
                if (k.isEmpty()) {
                    Toast.makeText(this, "Key 为空，仍使用微软翻译", Toast.LENGTH_SHORT).show()
                } else {
                    TranslateStore.setDeeplKey(this, k)
                    TranslateStore.setProvider(this, "deepl")
                }
                refreshProviderBtn()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun refreshProviderBtn() {
        if (::providerValue.isInitialized) providerValue.text = providerLabel()
        status("翻译服务：${mtName()}")
    }

    private fun micLabel(): String {
        return when (TranslateStore.micSource(this)) {
            "bt" -> "蓝牙耳机麦"
            "internal" -> "内部声音（视频/会议）"
            else -> "手机麦克风"
        }
    }

    private fun speakDestLabel(): String {
        return when (TranslateStore.speakDest(this)) {
            "earbuds" -> "仅耳机"
            "off" -> "关"
            else -> "自动"
        }
    }

    private fun pickSpeakDest() {
        val cur = TranslateStore.speakDest(this)
        val labels = arrayOf(
            "自动（有耳机走耳机，没耳机手机外放）",
            "仅耳机（没连耳机不朗读）",
            "关（不朗读译文）"
        )
        val keys = arrayOf("auto", "earbuds", "off")
        android.app.AlertDialog.Builder(this)
            .setTitle("朗读输出")
            .setSingleChoiceItems(labels, keys.indexOf(cur).coerceAtLeast(0)) { dlg, which ->
                TranslateStore.setSpeakDest(this, keys[which])
                dlg.dismiss()
                recreate()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun toggleMic() {
        if (running) { Toast.makeText(this, "请先停止再切换收音", Toast.LENGTH_SHORT).show(); return }
        val cur = TranslateStore.micSource(this)
        val labels = arrayOf(
            "手机麦克风（面对面，音质最好）",
            "蓝牙耳机麦克风（离嘴近，适合自己说话）",
            "手机内部声音（翻译手机里播放的视频/会议/播客）"
        )
        val keys = arrayOf("phone", "bt", "internal")
        android.app.AlertDialog.Builder(this)
            .setTitle("收音来源")
            .setSingleChoiceItems(labels, keys.indexOf(cur).coerceAtLeast(0)) { dlg, which ->
                TranslateStore.setMicSource(this, keys[which])
                dlg.dismiss()
                recreate()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun silenceLabel(): String {
        val m = TranslateStore.silenceStopMinutes(this)
        return if (m <= 0) "关闭" else "$m 分钟无声"
    }

    private fun pickSilence() {
        val options = listOf(0, 3, 5, 10, 20)
        val labels = options.map {
            if (it == 0) "关闭（一直开着，直到手动停）" else "$it 分钟无声音/无识别结果 → 自动停止"
        }.toTypedArray()
        android.app.AlertDialog.Builder(this)
            .setTitle("静音自动停阈值")
            .setItems(labels) { _, which ->
                TranslateStore.setSilenceStopMinutes(this, options[which])
                if (::silenceValue.isInitialized) silenceValue.text = silenceLabel()
                Toast.makeText(this, "已设定：${labels[which]}", Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun tierLabel() =
        if (TranslateStore.modelTier(this) == "high") "高精度大模型" else "标准小模型"

    private fun engineLabel(): String {
        val engine = TranslateStore.asrEngine(this)
        return when (engine) {
            "vosk" -> if (TranslateStore.modelTier(this) == "high") "离线·高精度大模型" else "离线·标准小模型"
            "groq" -> "Groq Whisper"
            "siliconflow" -> "硅基流动 SenseVoice"
            "openai" -> "OpenAI Whisper"
            "custom" -> "自定义渠道"
            "volc" -> "火山流式识别 2.0"
            "tx" -> "腾讯云实时识别"
            else -> "离线"
        }
    }

    /** 当前识别引擎的密钥状态（只显示已填/未填，不显示密钥本身）。 */
    private fun keyStatusLabel(): String {
        return when (TranslateStore.asrEngine(this)) {
            "vosk" -> "无需密钥"
            "tx" -> if (TranslateStore.txConfigured(this)) "已填" else "未填"
            else -> if (TranslateStore.asrKey(this, TranslateStore.asrEngine(this)).isNotEmpty()) "已填" else "未填"
        }
    }

    /** 引擎密钥二级入口：按当前引擎打开对应的填 Key 弹窗；离线引擎无需密钥。 */
    private fun pickEngineKey() {
        if (running) { Toast.makeText(this, "请先停止再改密钥", Toast.LENGTH_SHORT).show(); return }
        when (TranslateStore.asrEngine(this)) {
            "vosk" -> Toast.makeText(this, "离线引擎无需填密钥", Toast.LENGTH_SHORT).show()
            "tx" -> askTxKeys()
            "volc" -> askVolcKey()
            "custom" -> askCustomAsr()
            else -> {
                val ch = CloudAsr.channelOf(this, TranslateStore.asrEngine(this))
                if (ch == null) Toast.makeText(this, "该引擎无需填密钥", Toast.LENGTH_SHORT).show()
                else askAsrKey(ch)
            }
        }
    }

    private fun engineDesc(): String {
        if (txMode()) return "腾讯·实时识别（中日韩真流式）"
        if (TranslateStore.asrEngine(this) == "tx") return "腾讯 Key 未填（点开始会提示填 Key，不悄悄换引擎）"
        if (volcMode()) return "火山·流式识别2.0（真流式）"
        if (TranslateStore.asrEngine(this) == "volc") return "火山 Key 未填（日韩走云端渠道，中英走离线识别）"
        val ch = CloudAsr.activeChannel(this)
        if (ch != null) return "云端·${ch.name}（离线切分+云端重识别）"
        if (TranslateStore.asrEngine(this) != "vosk") return "云端 Key 未填，暂用离线识别"
        return "离线·${if (TranslateStore.modelTier(this) == "high") "高精度大模型" else "标准小模型"}"
    }

    private fun pickEngine() {
        if (running) { Toast.makeText(this, "请先停止再切换引擎", Toast.LENGTH_SHORT).show(); return }
        val labels = arrayOf(
            "离线 · 标准小模型（免流量，准确率一般）",
            "离线 · 高精度大模型（需下载/导入 1.36GB，准确率较好）",
            "云端 · Groq Whisper（免费额度，音乐耐受强）",
            "云端 · 硅基流动 SenseVoice（国内直连，中文强）",
            "云端 · OpenAI Whisper（付费，约 $0.006/分钟）",
            "云端 · 自定义渠道（OpenAI 兼容接口）",
            "云端 · 火山流式识别 2.0（真流式，识别最强；免费试用 20 小时）",
            "云端 · 腾讯实时识别（中日韩真流式；每月免费 5 小时）"
        )
        android.app.AlertDialog.Builder(this)
            .setTitle("识别引擎（渠道可预设，Key 自己填）")
            .setItems(labels) { _, which ->
                when (which) {
                    0 -> { TranslateStore.setAsrEngine(this, "vosk"); TranslateStore.setModelTier(this, "std"); recreate() }
                    1 -> { TranslateStore.setAsrEngine(this, "vosk"); TranslateStore.setModelTier(this, "high"); recreate() }
                    2 -> selectCloud(CloudAsr.GROQ)
                    3 -> selectCloud(CloudAsr.SILICONFLOW)
                    4 -> selectCloud(CloudAsr.OPENAI)
                    5 -> askCustomAsr()
                    6 -> selectVolc()
                    7 -> selectTx()
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun selectCloud(ch: CloudAsr.Channel) {
        // v0.9.2：Key 没填完就取消时，引擎保持原值——旧版先把引擎切过去再弹 Key 框，
        // 取消后界面显示新引擎、实际跑的却是旧引擎/离线，「模型对不上」的根源之一。
        if (TranslateStore.asrKey(this, ch.id).isEmpty()) askAsrKey(ch, TranslateStore.asrEngine(this))
        else { TranslateStore.setAsrEngine(this, ch.id); recreate() }
    }

    private fun askAsrKey(ch: CloudAsr.Channel, prevEngine: String? = null) {
        val input = android.widget.EditText(this).apply {
            hint = "粘贴 ${ch.name} 的 API Key"
            setText(TranslateStore.asrKey(this@TranslateActivity, ch.id))
            setSingleLine(true)
        }
        android.app.AlertDialog.Builder(this)
            .setTitle("${ch.name} 的 Key")
            .setMessage(ch.note + "\nKey 只存本机，只发往该渠道。")
            .setView(input)
            .setPositiveButton("保存") { _, _ ->
                TranslateStore.setAsrKey(this, ch.id, input.text.toString().trim())
                TranslateStore.setAsrEngine(this, ch.id)
                Toast.makeText(this, "已保存，识别引擎：${ch.name}", Toast.LENGTH_SHORT).show()
                recreate()
            }
            .setNeutralButton("获取 Key") { _, _ ->
                try { startActivity(Intent(Intent.ACTION_VIEW, android.net.Uri.parse(ch.keyUrl))) }
                catch (e: Exception) { Toast.makeText(this, "打开失败，地址：${ch.keyUrl}", Toast.LENGTH_LONG).show() }
                askAsrKey(ch, prevEngine)
            }
            .setNegativeButton("取消") { _, _ ->
                if (prevEngine != null && prevEngine != ch.id) {
                    TranslateStore.setAsrEngine(this, prevEngine)
                    recreate()
                }
            }
            .show()
    }

    private fun askCustomAsr() {
        val box = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val baseIn = android.widget.EditText(this).apply {
            hint = "接口地址，如 https://api.example.com/v1"
            setText(TranslateStore.customAsrBase(this@TranslateActivity)); setSingleLine(true)
        }
        val modelIn = android.widget.EditText(this).apply {
            hint = "模型名，如 whisper-large-v3"
            setText(TranslateStore.customAsrModel(this@TranslateActivity)); setSingleLine(true)
        }
        val keyIn = android.widget.EditText(this).apply {
            hint = "API Key"
            setText(TranslateStore.asrKey(this@TranslateActivity, "custom")); setSingleLine(true)
        }
        box.addView(baseIn); box.addView(modelIn); box.addView(keyIn)
        android.app.AlertDialog.Builder(this)
            .setTitle("自定义云端渠道（OpenAI 兼容）")
            .setView(box)
            .setPositiveButton("保存") { _, _ ->
                TranslateStore.setCustomAsr(this, baseIn.text.toString().trim(), modelIn.text.toString().trim())
                TranslateStore.setAsrKey(this, "custom", keyIn.text.toString().trim())
                TranslateStore.setAsrEngine(this, "custom")
                recreate()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun selectVolc() {
        // v0.9.2：同 selectCloud，Key 框取消时引擎保持原值，不留半选中的引擎。
        if (TranslateStore.asrKey(this, "volc").isEmpty()) askVolcKey(TranslateStore.asrEngine(this))
        else { TranslateStore.setAsrEngine(this, "volc"); recreate() }
    }

    private fun askVolcKey(prevEngine: String? = null) {
        val input = android.widget.EditText(this).apply {
            hint = "粘贴火山引擎 API Key（新版控制台）"
            setText(TranslateStore.asrKey(this@TranslateActivity, "volc"))
            setSingleLine(true)
        }
        android.app.AlertDialog.Builder(this)
            .setTitle("火山流式识别 2.0 的 Key")
            .setMessage("豆包流式语音识别模型 2.0，真流式（边说边出字、定稿二遍纠错）。免费试用 20 小时（半年有效），后付费约 4.5 元/小时，资源包更低。\nKey 只存本机，只发往火山官方接口。")
            .setView(input)
            .setPositiveButton("保存") { _, _ ->
                TranslateStore.setAsrKey(this, "volc", input.text.toString().trim())
                TranslateStore.setAsrEngine(this, "volc")
                Toast.makeText(this, "已保存，识别引擎：火山流式识别 2.0", Toast.LENGTH_SHORT).show()
                recreate()
            }
            .setNeutralButton("获取 Key") { _, _ ->
                try { startActivity(Intent(Intent.ACTION_VIEW, android.net.Uri.parse("https://console.volcengine.com/iam/keymanage"))) }
                catch (e: Exception) { Toast.makeText(this, "打开失败：console.volcengine.com → API Key 管理", Toast.LENGTH_LONG).show() }
                askVolcKey(prevEngine)
            }
            .setNegativeButton("取消") { _, _ ->
                if (prevEngine != null && prevEngine != "volc") {
                    TranslateStore.setAsrEngine(this, prevEngine)
                    recreate()
                }
            }
            .show()
    }

    private fun selectTx() {
        // v0.9.2：同 selectCloud，Key 框取消时引擎保持原值，不留「选了腾讯、Key 为空」的半选中状态。
        if (!TranslateStore.txConfigured(this)) askTxKeys(TranslateStore.asrEngine(this))
        else { TranslateStore.setAsrEngine(this, "tx"); recreate() }
    }

    /** 腾讯云实时 ASR 三值（AppId / SecretId / SecretKey），仿 askVolcKey 模式，只存本机。
     *  prevEngine：从引擎选择进来的上一引擎，取消时恢复；从密钥入口进来的传 null（引擎已是 tx）。 */
    private fun askTxKeys(prevEngine: String? = null) {
        val box = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val appIdIn = android.widget.EditText(this).apply {
            hint = "AppId（纯数字）"
            setText(TranslateStore.txAppId(this@TranslateActivity)); setSingleLine(true)
        }
        val secretIdIn = android.widget.EditText(this).apply {
            hint = "SecretId（AKID 开头）"
            setText(TranslateStore.txSecretId(this@TranslateActivity)); setSingleLine(true)
        }
        val secretKeyIn = android.widget.EditText(this).apply {
            hint = "SecretKey"
            setText(TranslateStore.txSecretKey(this@TranslateActivity)); setSingleLine(true)
        }
        box.addView(appIdIn); box.addView(secretIdIn); box.addView(secretKeyIn)
        android.app.AlertDialog.Builder(this)
            .setTitle("腾讯云实时识别的 Key")
            .setMessage("腾讯云实时语音识别（16k_ja/16k_ko/16k_zh），中日韩都走真流式。每月免费 5 小时，超量境内约 3 元/小时。\nAppId 在腾讯云账号中心查；SecretId/SecretKey 在控制台 → 访问管理 → API 密钥管理 创建。\n三值只存本机，只发往腾讯云官方接口。")
            .setView(box)
            .setPositiveButton("保存") { _, _ ->
                TranslateStore.setTxCreds(
                    this,
                    appIdIn.text.toString().trim(),
                    secretIdIn.text.toString().trim(),
                    secretKeyIn.text.toString().trim()
                )
                TranslateStore.setAsrEngine(this, "tx")
                Toast.makeText(this, "已保存，识别引擎：腾讯云实时识别", Toast.LENGTH_SHORT).show()
                recreate()
            }
            .setNeutralButton("获取 Key") { _, _ ->
                try { startActivity(Intent(Intent.ACTION_VIEW, android.net.Uri.parse("https://console.cloud.tencent.com/cam/capi"))) }
                catch (e: Exception) { Toast.makeText(this, "打开失败：console.cloud.tencent.com → 访问管理 → API 密钥管理", Toast.LENGTH_LONG).show() }
                askTxKeys(prevEngine)
            }
            .setNegativeButton("取消") { _, _ ->
                if (prevEngine != null && prevEngine != "tx") {
                    TranslateStore.setAsrEngine(this, prevEngine)
                    recreate()
                }
            }
            .show()
    }

    private fun pickTier() {
        if (running) { Toast.makeText(this, "请先停止再切换精度", Toast.LENGTH_SHORT).show(); return }
        val cur = TranslateStore.modelTier(this)
        val labels = arrayOf(
            "标准小模型（中 42MB / 英 40MB，已够日常短句）",
            "高精度大模型（中 1.36GB / 英 128MB，识别明显更准，建议 Wi-Fi 下载）"
        )
        android.app.AlertDialog.Builder(this)
            .setTitle("识别精度")
            .setSingleChoiceItems(labels, if (cur == "high") 1 else 0) { dlg, which ->
                TranslateStore.setModelTier(this, if (which == 1) "high" else "std")
                dlg.dismiss()
                recreate()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private var pendingImport: VoskManager.Lang? = null

    // 流式同传：partial 累积文本里已切分提交的长度、当前段起始时间、上次 partial
    // （流式切分状态已随会话迁入 TranslateService，本页只渲染结果）

    private fun pickImport() {
        if (running) { Toast.makeText(this, "请先停止再导入", Toast.LENGTH_SHORT).show(); return }
        val options = listOf(
            VoskManager.ZH to "中文·标准小模型（vosk-model-small-cn-0.22.zip）",
            VoskManager.ZH_BIG to "中文·高精度（vosk-model-cn-0.22.zip）",
            VoskManager.EN to "English·标准小模型（vosk-model-small-en-us-0.15.zip）",
            VoskManager.EN_BIG to "English·高精度（vosk-model-en-us-0.22-lgraph.zip）"
        )
        android.app.AlertDialog.Builder(this)
            .setTitle("导入哪个模型的 zip 包？")
            .setItems(options.map { it.second }.toTypedArray()) { _, which ->
                pendingImport = options[which].first
                val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                    type = "*/*"
                    addCategory(Intent.CATEGORY_OPENABLE)
                }
                startActivityForResult(intent, 91)
            }
            .setNegativeButton("取消", null)
            .show()
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == 92) {
            if (resultCode != RESULT_OK || data == null) {
                status("❌ 未获得系统授权，内部声音翻译已取消")
                return
            }
            status("授权成功，正在启动内部声音采集…")
            val svc = Intent(this, ProjectionService::class.java).apply {
                action = ProjectionService.ACTION_BEGIN
                putExtra(ProjectionService.EXTRA_CODE, resultCode)
                putExtra(ProjectionService.EXTRA_DATA, data)
            }
            try {
                if (android.os.Build.VERSION.SDK_INT >= 26) startForegroundService(svc) else startService(svc)
            } catch (e: Exception) {
                status("❌ 采集服务启动失败：${e.message}")
                return
            }
            Thread {
                var ok = false
                for (i in 0 until 20) {
                    if (InternalCapture.isRunning()) { ok = true; break }
                    try { Thread.sleep(150) } catch (e: Exception) { }
                }
                runOnUiThread {
                    if (ok) startServiceSession()
                    else status("❌ 内部声音采集未启动（系统限制或机型不兼容），请重试")
                }
            }.start()
            return
        }
        if (requestCode != 91 || resultCode != RESULT_OK) return
        val uri = data?.data ?: return
        val lang = pendingImport ?: return
        status("正在导入${lang.label}模型包…")
        try {
            val ins = contentResolver.openInputStream(uri)
            if (ins == null) { status("❌ 打不开所选文件"); return }
            VoskManager.importModel(this, lang, ins) { state, _ ->
                when {
                    state == "ready" -> status("✅ ${lang.label}模型导入完成，可开始翻译")
                    state == "unzipping" -> status("正在解压模型包…")
                    state.startsWith("error") -> status("❌ 导入失败：${state.removePrefix("error:")}")
                }
            }
        } catch (e: Exception) {
            status("❌ 导入失败：${e.message}")
        }
    }

    private fun status(s: String) { if (::statusView.isInitialized) statusView.text = s }

    private fun startSession() {
        // v0.9.2：选了腾讯引擎但密钥没填满，点开始也不悄悄换引擎——先弹填 Key 对话框。
        // 服务侧还有一道同样的拦截（悬浮按钮直启时兜底）。
        if (TranslateStore.asrEngine(this) == "tx" && !TranslateStore.txConfigured(this)) {
            Toast.makeText(this, "腾讯引擎还没填密钥，先填 AppId、SecretId、SecretKey", Toast.LENGTH_LONG).show()
            status("⚠️ 腾讯引擎未配置密钥，请先填好再开始")
            askTxKeys()
            return
        }
        clearError()
        if (TranslateStore.micSource(this) != "internal" &&
            checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 7)
            Toast.makeText(this, "请允许录音权限后再点开始", Toast.LENGTH_SHORT).show()
            return
        }
        // 内部声音且投影不在（首次或已失效）→ 必须借系统授权弹窗，授权后服务接管
        if (TranslateStore.micSource(this) == "internal" &&
            !InternalCapture.isRunning() && !InternalCapture.hasProjection()) {
            status("内部声音翻译需系统授权：在弹窗中选择「整个屏幕」并允许声音录制…")
            try {
                val mpm = getSystemService(android.media.projection.MediaProjectionManager::class.java)
                @Suppress("DEPRECATION")
                startActivityForResult(mpm.createScreenCaptureIntent(), 92)
            } catch (e: Exception) {
                status("❌ 无法调起系统授权：${e.message}")
            }
            return
        }
        startServiceSession()
    }

    /** 启动后台翻译服务并跟随其状态；悬浮按钮直启时根本不会打开本页。 */
    private fun startServiceSession() {
        try {
            TranslateService.start(this)
        } catch (t: Throwable) {
            DiagLog.logSync(this, "ui_start_fail", mapOf("error" to t.toString()))
            status("❌ 启动失败：${t.message ?: "未知错误"}，已记入诊断日志")
            return
        }
        running = true
        refreshStartBtn()
        refreshConfigView()
        status("🎙 正在听（${engineDesc()}）… 边说边翻，译文滚动输出")
        // v0.9.2：只在悬浮按钮借道启动（background_start）时退回后台；
        // 主界面点「开始翻译」一律留在前台，显示实时状态，不再自动缩小。
        if (backgroundStart) {
            backgroundStart = false
            Toast.makeText(this, "翻译已开始，字幕在屏幕下方", Toast.LENGTH_SHORT).show()
            moveTaskToBack(true)
        }
    }

    /** 服务已完成一句（识别+翻译都在 TranslateService），本页只负责渲染成一行。 */
    private fun renderLine(src: String, dst: String) {
        val (pos, srcView, dstView) = addRow(src)
        srcView.text = src
        dstView.text = dst
        if (pos < entries.size) entries[pos] = TranslateStore.Entry(src, dst)
    }

    private fun addRow(src: String): Triple<Int, android.widget.TextView, android.widget.TextView> {
        partialView.text = ""
        val entryPos = entries.size
        entries.add(TranslateStore.Entry(src, "…"))
        val srcView = Ui.body(this, src, 15f, Ui.SUBTEXT)
        val dstView = Ui.title(this, "…", 17f)
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply { bottomMargin = Ui.dp(this@TranslateActivity, 10) }
        }
        box.addView(srcView); box.addView(dstView)
        listBox.addView(box)
        scroll.post { scroll.fullScroll(ScrollView.FOCUS_DOWN) }
        return Triple(entryPos, srcView, dstView)
    }

    private fun stopSession() {
        // 会话在服务里：记录保存、尾句处理、通知汇总都由服务收尾，本页只同步 UI
        TranslateService.stop(this)
        running = false
        refreshStartBtn()
        clearError()
        partialView.text = ""
        if (::levelBar.isInitialized) levelBar.progress = 0
        status("已停止")
    }

    override fun onDestroy() {
        TranslateControl.lineListener = null
        TranslateControl.partialListener = null
        TranslateControl.statusListener = null
        TranslateControl.levelListener = null
        TranslateControl.errorListener = null
        handler.removeCallbacks(liveTicker)
        liveTicking = false
        super.onDestroy()
    }
}
