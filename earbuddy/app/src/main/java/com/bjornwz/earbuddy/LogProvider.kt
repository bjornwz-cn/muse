package com.bjornwz.earbuddy

import android.content.ContentProvider
import android.content.ContentValues
import android.database.Cursor
import android.net.Uri
import android.os.ParcelFileDescriptor
import java.io.File
import java.io.FileNotFoundException

/** 诊断日志导出文件提供者：只递 cacheDir/diag-export.jsonl（一键发送用），模式同 AudioProvider。 */
class LogProvider : ContentProvider() {

    override fun onCreate(): Boolean = true

    private fun fileFor(uri: Uri): File {
        val f = File(context!!.cacheDir, "diag-export.jsonl")
        if (!f.exists()) throw FileNotFoundException("diag-export")
        return f
    }

    override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor =
        ParcelFileDescriptor.open(fileFor(uri), ParcelFileDescriptor.MODE_READ_ONLY)

    override fun getType(uri: Uri): String = "application/x-ndjson"

    override fun query(uri: Uri, p: Array<String>?, s: String?, a: Array<String>?, o: String?): Cursor? = null
    override fun insert(uri: Uri, values: ContentValues?): Uri? = null
    override fun delete(uri: Uri, s: String?, a: Array<String>?): Int = 0
    override fun update(uri: Uri, v: ContentValues?, s: String?, a: Array<String>?): Int = 0

    companion object {
        const val AUTHORITY = "com.bjornwz.earbuddy.logprovider"
        fun uri(): Uri = Uri.parse("content://$AUTHORITY/diag-export.jsonl")
    }
}
