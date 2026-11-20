"""api.main —— NAS Curator FastAPI 入口。

挂载 7 个路由模块（dashboard / review / jobs / browse / rules / settings /
files：0.1.5 文件预览）。
Jinja2 模板目录 ../web/templates、静态目录 ../web/static：P2 前端未落盘时
不存在，届时根路由 / 返回内置极简总览页；模板到位后自动改走模板。
DB 路径：环境变量 CURATOR_DB，否则项目 data/nas-curator.db（与 CLI 默认一致）。
启动时 DB.init_db()。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from common import db as DB
from . import browse, dashboard, files, jobs, review, rules, settings, upgrade, wizard
from .deps import ensure_config, get_db_path, is_setup_done

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "web", "templates")
STATIC_DIR = os.path.join(PROJECT_ROOT, "web", "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_config()  # 容器首次启动：数据卷缺配置时从镜像自带 config/ 复制默认
    DB.init_db(get_db_path())
    yield


PAGE_PATHS = {"/", "/review", "/jobs", "/browse", "/rules", "/settings"}


class SetupRedirectMiddleware(BaseHTTPMiddleware):
    """未完成首次安装向导时，页面路由跳到 /setup；API、静态文件与向导本身放行。"""

    async def dispatch(self, request, call_next):
        if request.url.path in PAGE_PATHS and not is_setup_done():
            return RedirectResponse(url="/setup", status_code=303)
        return await call_next(request)


def create_app() -> FastAPI:
    app = FastAPI(title="NAS Curator", version="0.2.0", lifespan=lifespan)
    app.add_middleware(SetupRedirectMiddleware)

    for mod in (dashboard, review, jobs, browse, rules, settings, upgrade,
                wizard, files):
        app.include_router(mod.router)

    if os.path.isdir(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    templates = (Jinja2Templates(directory=TEMPLATES_DIR)
                 if os.path.isdir(TEMPLATES_DIR) else None)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if templates is not None and os.path.exists(
                os.path.join(TEMPLATES_DIR, "index.html")):
            return templates.TemplateResponse(request, "index.html", {})
        # 前端模板未落盘时的内置极简总览页（P2 前端到位后自动改走模板）。
        return HTMLResponse(
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>NAS Curator（文件管家）</title></head><body>"
            "<h1>NAS Curator（文件管家）</h1>"
            "<p>总览页模板尚未落盘（web/templates/index.html），先用 API：</p>"
            "<ul>"
            "<li><a href='/api/dashboard'>总览 /api/dashboard</a></li>"
            "<li><a href='/api/review'>复核队列 /api/review</a></li>"
            "<li><a href='/api/jobs'>任务 /api/jobs</a></li>"
            "<li><a href='/api/browse'>浏览 /api/browse</a></li>"
            "<li><a href='/api/rules'>规则 /api/rules</a></li>"
            "<li><a href='/api/settings'>设置 /api/settings</a></li>"
            "<li><a href='/docs'>接口文档 /docs</a></li>"
            "</ul></body></html>")

    # 五个子页面：模板在 web/templates/<name>.html；未落盘时 404（见 NOTES）。
    for _page in ("review", "jobs", "browse", "rules", "settings"):
        def _page_view(request: Request, _name=_page):
            if templates is not None and os.path.exists(
                    os.path.join(TEMPLATES_DIR, _name + ".html")):
                return templates.TemplateResponse(request, _name + ".html", {})
            return HTMLResponse(
                f"<h1>{_name} 模板尚未落盘</h1>", status_code=404)
        app.get("/" + _page, response_class=HTMLResponse)(_page_view)

    return app


app = create_app()
