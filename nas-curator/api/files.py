#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api.files —— 文件预览 / 下载（0.1.5）：复核队列里的文件点开即看。

复核卡片里的文件路径改为可点击链接，指向本模块的端点；图片与 PDF
内联显示，其余按附件下载；不要用 file://（浏览器打不开 NAS 路径）。

路径穿越防护：只允许 CURATOR_FILE_ROOT（缺省 /vol1/1000）下的文件；
realpath 校验（符号链接也算），越界/不存在/目录一律 4xx。
"""
from __future__ import annotations

import mimetypes
import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api/file", tags=["files"])

_INLINE_TYPES = ("image/",)  # 前缀匹配：图片内联；PDF 单独判断
_INLINE_EXACT = {"application/pdf"}


def file_root() -> str:
    """允许预览的根目录：环境变量 CURATOR_FILE_ROOT 覆盖（供测试），缺省 /vol1/1000。"""
    return os.environ.get("CURATOR_FILE_ROOT") or "/vol1/1000"


def resolve(path: str) -> str:
    """把请求的路径解析为根目录下的真实文件；越界/不存在/目录抛 HTTPException。"""
    if not path:
        raise HTTPException(status_code=400, detail="path 不能为空")
    root = os.path.realpath(file_root())
    target = os.path.realpath(path)
    if target != root and not target.startswith(root + os.sep):
        raise HTTPException(status_code=403,
                            detail="只允许预览 /vol1/1000 下的文件")
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="文件不存在")
    return target


def _serve(path: str, download: bool) -> FileResponse:
    target = resolve(path)
    media_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
    inline = (not download and
              (media_type.startswith(_INLINE_TYPES) or media_type in _INLINE_EXACT))
    return FileResponse(
        target, media_type=media_type,
        filename=os.path.basename(target),
        content_disposition_type="inline" if inline else "attachment",
    )


@router.get("/preview")
def preview(path: str = Query(..., description="NAS 上的绝对路径"),
            download: bool = Query(False, description="为真时强制下载")):
    """预览一个文件：图片/PDF 内联显示，其余下载。"""
    return _serve(path, download)


@router.get("/download")
def download(path: str = Query(..., description="NAS 上的绝对路径")):
    """强制下载一个文件。"""
    return _serve(path, True)
