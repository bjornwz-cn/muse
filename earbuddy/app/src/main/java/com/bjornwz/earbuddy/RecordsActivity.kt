package com.bjornwz.earbuddy

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.os.Bundle
import android.widget.ScrollView
import android.widget.Toast
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** 翻译记录回看：点开看全文，可分享文本/录音、删除单条；顶部可导出全部与清空。 */
class RecordsActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.statusBarColor = Ui.ACCENT_DARK
        val root = Ui.pageRoot(this)
        val card = Ui.card(this)
        card.addView(Ui.title(this, "翻译记录", 20f))
        val records = TranslateStore.listRecords(this)
        if (records.isEmpty()) {
            card.addView(Ui.body(this, "还没有翻译记录。用过一次实时翻译后，这里会保存每次会话的双语全文。", 14f, Ui.SUBTEXT))
        } else {
            card.addView(Ui.button(this, "导出全部记录（分享文本）") { exportAll(records) })
            card.addView(Ui.button(this, "清空全部记录（含录音）") { confirmClearAll() })
            card.addView(Ui.body(this, "点开任意一条可看全文、分享或删除。", 13f, Ui.SUBTEXT))
        }
        val fmt = SimpleDateFormat("MM-dd HH:mm", Locale.CHINA)
        records.forEach { rec ->
            val hasAudio = SessionRecorder.audioFileFor(this, rec.audioId) != null
            val dirTxt = when {
                rec.from == "auto" && rec.to == "auto" -> "双向自动"
                rec.from == "auto" -> "自动→${TranslateStore.langName(rec.to)}"
                else -> "${TranslateStore.langName(rec.from)}→${TranslateStore.langName(rec.to)}"
            }
            val label = "${fmt.format(Date(rec.startedAt))} · " +
                "$dirTxt · ${rec.entries.size} 句" +
                if (hasAudio) " · 🎙含录音" else ""
            card.addView(Ui.button(this, label) { showRecord(rec, fmt) })
        }
        root.addView(card)
        val sv = ScrollView(this).apply {
            setBackgroundColor(Ui.BG)
            addView(root)
        }
        setContentView(sv)
        Ui.fitSystemBars(sv)
    }

    private fun showRecord(rec: TranslateStore.Record, fmt: SimpleDateFormat) {
        val sb = StringBuilder()
        rec.entries.forEachIndexed { i, e ->
            sb.append("${i + 1}. ${e.src}\n    ${e.dst}\n\n")
        }
        val audio = SessionRecorder.audioFileFor(this, rec.audioId)
        val builder = AlertDialog.Builder(this)
            .setTitle(fmt.format(Date(rec.startedAt)))
            .setMessage(sb.toString().trim())
            .setPositiveButton("分享文本") { _, _ -> shareText(TranslateStore.recordText(rec)) }
            .setNegativeButton("删除本条") { _, _ -> confirmDelete(rec) }
        if (audio != null) {
            val sizeMb = "%.1f".format(audio.length() / 1024.0 / 1024.0)
            builder.setNeutralButton("分享录音（${sizeMb}MB）") { _, _ -> shareAudio(audio) }
        }
        builder.show()
    }

    private fun confirmDelete(rec: TranslateStore.Record) {
        AlertDialog.Builder(this)
            .setTitle("删除这条记录？")
            .setMessage("记录文本与本场录音文件都会删除，不可恢复。")
            .setPositiveButton("删除") { _, _ ->
                TranslateStore.deleteRecord(this, rec)
                Toast.makeText(this, "已删除", Toast.LENGTH_SHORT).show()
                recreate()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun confirmClearAll() {
        AlertDialog.Builder(this)
            .setTitle("清空全部翻译记录？")
            .setMessage("所有记录文本与录音文件都会删除，不可恢复。")
            .setPositiveButton("全部清空") { _, _ ->
                TranslateStore.clearAllRecords(this)
                Toast.makeText(this, "已清空", Toast.LENGTH_SHORT).show()
                recreate()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun exportAll(records: List<TranslateStore.Record>) {
        val sb = StringBuilder("耳机助手翻译记录导出（共 ${records.size} 场）\n")
        records.forEach { sb.append("\n").append(TranslateStore.recordText(it)) }
        shareText(sb.toString())
    }

    private fun shareText(text: String) {
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_TEXT, text)
            putExtra(Intent.EXTRA_SUBJECT, "耳机助手翻译记录")
        }
        try {
            startActivity(Intent.createChooser(intent, "分享/导出到…"))
        } catch (e: Exception) {
            Toast.makeText(this, "没有可分享的应用", Toast.LENGTH_SHORT).show()
        }
    }

    private fun shareAudio(file: java.io.File) {
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = if (file.name.endsWith(".m4a")) "audio/mp4" else "audio/wav"
            putExtra(Intent.EXTRA_STREAM, AudioProvider.uriFor(file))
            putExtra(Intent.EXTRA_SUBJECT, "会议录音 ${file.name}")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        try {
            startActivity(Intent.createChooser(intent, "分享会议录音到…"))
        } catch (e: Exception) {
            Toast.makeText(this, "没有可分享的应用", Toast.LENGTH_SHORT).show()
        }
    }
}
