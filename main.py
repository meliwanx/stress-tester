"""
生图压测平台 - 后端服务
基于 FastAPI 构建，提供图像生成 API 压力测试功能
支持多供应商管理、阶梯式并发压测、压测报告生成、图片自动保存
"""

import asyncio
import base64
import json
import os
import sqlite3
import time
from datetime import datetime
from typing import List
from urllib.parse import urlparse

import aiohttp
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ==================== 管理员配置 ====================
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "liwanx")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_COOKIE_NAME = "admin_token"
ADMIN_COOKIE_VALUE = os.environ.get("ADMIN_COOKIE_VALUE", "authenticated")


class LoginRequest(BaseModel):
    """登录请求体"""
    username: str
    password: str

# ==================== 配置 ====================

app = FastAPI(title="生图压测平台", version="1.1.0")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
IMAGES_DIR = os.path.join(STATIC_DIR, "images")

# 压测并发阶梯
CONCURRENCY_LEVELS = [20, 50, 100, 500]

# 压测使用的提示词（完整测试）
STRESS_PROMPT = (
    "请生成一张高清图片：城市天际线在日落时分，金色阳光映照在现代化摩天大楼的玻璃幕墙上，"
    "天空呈现绚丽的橙红色渐变，远处有几朵云彩被染成金色，整体画面大气磅礴，充满震撼力"
)

# 连接测试使用的提示词（快速验证）
TEST_PROMPT = "生成一张简单的图片"

# 默认 API 路径后缀
DEFAULT_API_PATH = "/v1/images/generations"

# 全局超时配置（秒）
GLOBAL_TIMEOUT = 1200


# ==================== 数据库 ====================

def get_db():
    """
    获取 SQLite 数据库连接，返回支持字典访问的连接对象
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table, column, definition):
    """兼容旧库：如果字段不存在，则添加字段。"""
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db(conn=None):
    """
    初始化数据库，创建供应商表和压测任务表
    同时修复之前版本中遗漏的 image_path 字段
    """
    own_conn = conn is None
    if conn is None:
        conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            url TEXT NOT NULL,
            api_key TEXT NOT NULL,
            model TEXT DEFAULT 'gpt-image-2',
            custom_url INTEGER DEFAULT 0,
            source TEXT DEFAULT 'supplier',
            test_mode TEXT DEFAULT 'step',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS stress_tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_id INTEGER NOT NULL,
            concurrency INTEGER NOT NULL,
            source TEXT DEFAULT 'supplier',
            test_mode TEXT DEFAULT 'step',
            status TEXT DEFAULT 'pending',
            total_requests INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            avg_response_time REAL DEFAULT 0,
            min_response_time REAL DEFAULT 0,
            max_response_time REAL DEFAULT 0,
            p50_response_time REAL DEFAULT 0,
            p90_response_time REAL DEFAULT 0,
            p95_response_time REAL DEFAULT 0,
            p99_response_time REAL DEFAULT 0,
            qps REAL DEFAULT 0,
            total_time REAL DEFAULT 0,
            error_details TEXT DEFAULT '[]',
            request_details TEXT DEFAULT '[]',
            image_path TEXT,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
        );
    """)
    ensure_column(conn, "suppliers", "custom_url", "INTEGER DEFAULT 0")
    ensure_column(conn, "suppliers", "phone", "TEXT")
    ensure_column(conn, "suppliers", "source", "TEXT DEFAULT 'supplier'")
    ensure_column(conn, "suppliers", "test_mode", "TEXT DEFAULT 'step'")
    ensure_column(conn, "stress_tests", "source", "TEXT DEFAULT 'supplier'")
    ensure_column(conn, "stress_tests", "test_mode", "TEXT DEFAULT 'step'")
    ensure_column(conn, "stress_tests", "request_details", "TEXT DEFAULT '[]'")
    # 服务重启时，将之前未完成的 running 任务标记为 failed，防止状态永久卡住
    conn.execute(
        "UPDATE stress_tests SET status='failed', completed_at=datetime('now','localtime') WHERE status='running'"
    )
    conn.commit()
    if own_conn:
        conn.close()


# 启动时初始化
init_db()
os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ==================== 数据模型 ====================

def build_api_url(url: str, custom_url: bool = False) -> str:
    """
    根据是否自定义 URL 拼接默认 API 路径
    :param url: 用户输入的 URL
    :param custom_url: 是否使用完整自定义 URL
    :return: 最终请求用的 API URL
    """
    if custom_url:
        return url
    # 自动拼接默认路径，避免重复拼接
    if url.rstrip("/").endswith(DEFAULT_API_PATH.rstrip("/")):
        return url
    return url.rstrip("/") + DEFAULT_API_PATH


def parse_concurrency_setting(raw: str, stepped: bool) -> List[int]:
    """
    解析管理员填写的并发设置。
    - 非阶梯：只执行一个并发数，若输入逗号序列则取第一个。
    - 阶梯：输入逗号序列时按序列执行；输入单个数值时使用默认阶梯中不超过目标值的级别，并包含目标值。
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError("请填写并发数")

    parts = [p.strip() for p in text.replace("，", ",").split(",") if p.strip()]
    if not parts:
        raise ValueError("请填写并发数")

    levels = []
    for part in parts:
        try:
            value = int(part)
        except ValueError as exc:
            raise ValueError("并发数必须为正整数") from exc
        if value <= 0:
            raise ValueError("并发数必须大于 0")
        levels.append(value)

    if not stepped:
        return [levels[0]]

    if len(levels) > 1:
        return list(dict.fromkeys(levels))

    target = levels[0]
    stepped_levels = [level for level in CONCURRENCY_LEVELS if level <= target]
    if target not in stepped_levels:
        stepped_levels.append(target)
    return stepped_levels


def create_test_records(conn, supplier_id: int, concurrency_levels: List[int], source: str, mode: str) -> List[int]:
    """为一个供应商创建一组待执行的压测任务。"""
    test_ids = []
    for concurrency in concurrency_levels:
        cur = conn.execute(
            "INSERT INTO stress_tests (supplier_id, concurrency, source, test_mode) VALUES (?, ?, ?, ?)",
            (supplier_id, concurrency, source, mode)
        )
        test_ids.append(cur.lastrowid)
    return test_ids


def create_manual_test_records(
    conn,
    name: str,
    phone: str,
    url: str,
    api_key: str,
    model: str,
    custom_url: bool,
    concurrency_levels: List[int],
    mode: str,
):
    """管理员手动创建供应商和压测任务。"""
    cur = conn.execute(
        """
        INSERT INTO suppliers (name, phone, url, api_key, model, custom_url, source, test_mode)
        VALUES (?, ?, ?, ?, ?, ?, 'admin', ?)
        """,
        (name, phone, url, api_key, model, int(custom_url), mode)
    )
    supplier_id = cur.lastrowid
    test_ids = create_test_records(conn, supplier_id, concurrency_levels, "admin", mode)
    conn.commit()
    return supplier_id, test_ids


class TestRequest(BaseModel):
    """连接测试请求体"""
    url: str
    api_key: str
    model: str = "gpt-image-2"
    custom_url: bool = False


class SubmitRequest(BaseModel):
    """压测提交请求体"""
    name: str
    phone: str = ""
    url: str
    api_key: str
    model: str = "gpt-image-2"
    custom_url: bool = False


class ManualTaskRequest(BaseModel):
    """管理员手动创建压测任务请求体"""
    name: str
    phone: str = ""
    url: str
    api_key: str
    model: str = "gpt-image-2"
    custom_url: bool = False
    concurrency: str = "20"
    stepped: bool = False


# ==================== 核心逻辑 ====================

def calc_percentile(sorted_data, p):
    """
    计算百分位数
    :param sorted_data: 已排序的数据列表
    :param p: 百分位 (0-100)
    :return: 对应百分位的值
    """
    if not sorted_data:
        return 0
    k = (len(sorted_data) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_data) - 1)
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


def image_extension(content_type: str) -> str:
    """根据常见图片 MIME 类型选择文件后缀。"""
    mime = (content_type or "").split(";")[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(mime, ".png")


def save_image_bytes(img_data: bytes, prefix: str = "", extension: str = ".png"):
    """将图片二进制保存到静态目录，返回可由浏览器访问的路径。"""
    if not img_data:
        return None
    safe_extension = extension if extension.startswith(".") else f".{extension}"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"{prefix}{timestamp}{safe_extension}"
    filepath = os.path.join(IMAGES_DIR, filename)
    os.makedirs(IMAGES_DIR, exist_ok=True)
    with open(filepath, "wb") as f:
        f.write(img_data)
    return f"/static/images/{filename}"


def save_data_url_image(url: str, prefix: str = ""):
    """保存 data:image/...;base64,... 形式的图片，避免浏览器新标签页拦截 data URL。"""
    header, separator, encoded = url.partition(",")
    if not separator or not header.lower().startswith("data:image/") or ";base64" not in header.lower():
        return None
    content_type = header[5:].split(";")[0]
    return save_image_bytes(base64.b64decode(encoded), prefix, image_extension(content_type))


async def save_image(data_item, prefix="", session=None):
    """
    保存图片到本地，支持 b64_json 和 data URL；HTTP(S) URL 保留为安全外链。
    :param data_item: API 返回的 data 数组中的单个元素
    :param prefix: 文件名前缀
    :param session: 兼容旧调用的可选参数
    :return: 保存后的相对路径；HTTP(S) 返回原始外链；其他失败返回 None
    """
    try:
        if data_item.get("b64_json"):
            img_data = base64.b64decode(data_item["b64_json"])
            return save_image_bytes(img_data, prefix, ".png")

        raw_url = str(data_item.get("url") or "").strip()
        if not raw_url:
            return None

        if raw_url.startswith("/static/images/"):
            return raw_url

        if raw_url.lower().startswith("data:image/"):
            return save_data_url_image(raw_url, prefix)

        parsed = urlparse(raw_url)
        if parsed.scheme in {"http", "https"}:
            return raw_url
    except Exception:
        pass
    return None


async def verify_connection(url, api_key, model, custom_url=False):
    """
    验证 API 连接是否可用
    发送一次低质量生图请求，能返回图片即视为通过
    :param url: 用户输入的 URL
    :param api_key: API 密钥
    :param model: 模型名称
    :param custom_url: 是否使用完整自定义 URL
    :return: (是否成功, 消息, 图片路径)
    """
    api_url = build_api_url(url, custom_url)
    payload = {
        "model": model,
        "prompt": TEST_PROMPT,
        "quality": "low",
        "response_format": "url",
        "size": "1024x1024"
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                api_url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=GLOBAL_TIMEOUT)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    items = data.get("data", [])
                    if items and len(items) > 0:
                        img_path = await save_image(items[0], prefix="test_", session=session)
                        return True, "测试通过，成功生成图片", img_path
                    return False, "响应中未包含图片数据", None
                text = await resp.text()
                return False, f"HTTP {resp.status}: {text[:200]}", None
    except asyncio.TimeoutError:
        return False, f"请求超时（{GLOBAL_TIMEOUT}秒）", None
    except Exception as e:
        return False, f"连接失败: {str(e)}", None


async def run_single_test(test_id, url, api_key, model, concurrency, custom_url=False):
    """
    执行单个并发级别的压测任务
    同时发起 concurrency 个请求，记录每个请求的响应时间和成功/失败状态
    :param test_id: 压测任务 ID
    :param url: 用户输入的 URL
    :param api_key: API 密钥
    :param model: 模型名称
    :param concurrency: 并发数
    :param custom_url: 是否使用完整自定义 URL
    """
    api_url = build_api_url(url, custom_url)
    conn = get_db()
    conn.execute(
        """
        UPDATE stress_tests SET
            status='running',
            started_at=datetime('now','localtime'),
            total_requests=?,
            error_details='[]',
            request_details='[]',
            image_path=NULL
        WHERE id=?
        """,
        (concurrency, test_id)
    )
    conn.commit()
    conn.close()

    payload = {
        "model": model,
        "prompt": STRESS_PROMPT,
        "quality": "high",
        "response_format": "url",
        "size": "1024x1024"
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    async def single_request(session, idx):
        """
        发起单个 HTTP 请求并记录结果
        返回 (request_detail, image_data_item)
        """
        request_detail = {
            "index": idx + 1,
            "success": False,
            "status_code": None,
            "response_time": 0,
            "message": "",
            "image_path": None,
        }
        start = time.time()
        try:
            async with session.post(
                api_url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=GLOBAL_TIMEOUT)
            ) as resp:
                elapsed = time.time() - start
                request_detail["status_code"] = resp.status
                request_detail["response_time"] = round(elapsed, 3)
                if resp.status == 200:
                    try:
                        data = await resp.json()
                    except Exception:
                        request_detail["message"] = "响应不是有效 JSON"
                        return request_detail, None
                    items = data.get("data", [])
                    if items and len(items) > 0:
                        request_detail["success"] = True
                        request_detail["message"] = "成功"
                        return request_detail, items[0]
                    request_detail["message"] = "无图片数据"
                    return request_detail, None
                text = await resp.text()
                request_detail["message"] = f"HTTP {resp.status}: {text[:300]}"
                return request_detail, None
        except asyncio.TimeoutError:
            request_detail["response_time"] = round(time.time() - start, 3)
            request_detail["message"] = f"超时（{GLOBAL_TIMEOUT}秒）"
            return request_detail, None
        except Exception as e:
            request_detail["response_time"] = round(time.time() - start, 3)
            request_detail["message"] = str(e)[:200]
            return request_detail, None

    # 执行并发请求，connector 控制连接池大小
    t0 = time.time()
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = await asyncio.gather(*[single_request(session, i) for i in range(concurrency)])
    total_time = time.time() - t0

    # 解析结果，安全地分离成功与失败
    success_times = []
    errors = []
    first_success_item = None

    request_details = []
    first_success_detail = None

    for detail, image_item in results:
        request_details.append(detail)
        if detail["success"]:
            success_times.append(detail["response_time"])
            if first_success_item is None:
                first_success_item = image_item
                first_success_detail = detail
        else:
            errors.append(f"#{detail['index']}: {detail['message']}")

    # 保存第一张成功的图片
    image_path = None
    if first_success_item:
        image_path = await save_image(first_success_item, prefix=f"stress_{concurrency}_")
        if first_success_detail is not None:
            first_success_detail["image_path"] = image_path

    # 计算统计数据
    success_times.sort()
    n = len(success_times)

    conn = get_db()
    conn.execute("""
        UPDATE stress_tests SET
            status=?, success_count=?, failure_count=?,
            avg_response_time=?, min_response_time=?, max_response_time=?,
            p50_response_time=?, p90_response_time=?, p95_response_time=?, p99_response_time=?,
            qps=?, total_time=?, error_details=?, request_details=?, image_path=?,
            completed_at=datetime('now','localtime')
        WHERE id=?
    """, (
        'completed' if n > 0 else 'failed',
        n, len(errors),
        round(sum(success_times) / n, 3) if n else 0,
        round(min(success_times), 3) if n else 0,
        round(max(success_times), 3) if n else 0,
        round(calc_percentile(success_times, 50), 3) if n else 0,
        round(calc_percentile(success_times, 90), 3) if n else 0,
        round(calc_percentile(success_times, 95), 3) if n else 0,
        round(calc_percentile(success_times, 99), 3) if n else 0,
        round((n + len(errors)) / total_time, 2) if total_time > 0 else 0,
        round(total_time, 3),
        json.dumps(errors[:50], ensure_ascii=False),
        json.dumps(request_details, ensure_ascii=False),
        image_path,
        test_id
    ))
    conn.commit()
    conn.close()


async def run_task_sequence(test_ids, concurrency_levels, url, api_key, model, custom_url=False):
    """按给定任务 ID 和并发级别顺序执行压测任务。"""
    for tid, c in zip(test_ids, concurrency_levels):
        try:
            await run_single_test(tid, url, api_key, model, c, custom_url)
        except Exception as e:
            request_details = [{
                "index": None,
                "success": False,
                "status_code": None,
                "response_time": 0,
                "message": f"任务异常: {str(e)}",
                "image_path": None,
            }]
            conn = get_db()
            conn.execute(
                """
                UPDATE stress_tests SET
                    status='failed',
                    error_details=?,
                    request_details=?,
                    completed_at=datetime('now','localtime')
                WHERE id=?
                """,
                (
                    json.dumps([f"任务异常: {str(e)}"], ensure_ascii=False),
                    json.dumps(request_details, ensure_ascii=False),
                    tid,
                )
            )
            conn.commit()
            conn.close()


async def run_batch(supplier_id, url, api_key, model, custom_url=False):
    """
    按阶梯顺序执行所有并发级别的压测任务
    依次执行 20 → 50 → 100 → 500 并发，每个级别完成后再执行下一个
    :param supplier_id: 供应商 ID
    :param url: 用户输入的 URL
    :param api_key: API 密钥
    :param model: 模型名称
    :param custom_url: 是否使用完整自定义 URL
    """
    conn = get_db()
    test_ids = create_test_records(conn, supplier_id, CONCURRENCY_LEVELS, "supplier", "step")
    conn.commit()
    conn.close()

    # 顺序执行每个并发级别
    await run_task_sequence(test_ids, CONCURRENCY_LEVELS, url, api_key, model, custom_url)


# ==================== API 路由 ====================

def require_admin(request: Request):
    """校验管理员登录态。"""
    token = request.cookies.get(ADMIN_COOKIE_NAME)
    if token != ADMIN_COOKIE_VALUE:
        raise HTTPException(status_code=401, detail="未登录")

@app.get("/")
async def page_index():
    """首页 - 压测表单页面"""
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/admin")
async def page_admin(request: Request):
    """管理页 - 压测报告查看页面（始终返回 admin.html，登录检查由前端完成）"""
    return FileResponse(os.path.join(STATIC_DIR, "admin.html"))


@app.post("/api/login")
async def api_login(req: LoginRequest):
    """
    管理员登录
    验证账号密码，通过后在 cookie 中设置登录标识
    """
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=500, detail="管理员密码未配置")
    if req.username == ADMIN_USERNAME and req.password == ADMIN_PASSWORD:
        resp = JSONResponse({"success": True, "message": "登录成功"})
        resp.set_cookie(
            key=ADMIN_COOKIE_NAME,
            value=ADMIN_COOKIE_VALUE,
            httponly=True,
            max_age=86400 * 7,
            samesite="lax"
        )
        return resp
    raise HTTPException(status_code=401, detail="账号或密码错误")


@app.post("/api/logout")
async def api_logout():
    """退出登录，清除 cookie"""
    resp = JSONResponse({"success": True, "message": "已退出登录"})
    resp.delete_cookie(key=ADMIN_COOKIE_NAME)
    return resp


@app.get("/api/me")
async def api_me(request: Request):
    """检查当前登录状态"""
    token = request.cookies.get(ADMIN_COOKIE_NAME)
    if token == ADMIN_COOKIE_VALUE:
        return {"success": True, "authenticated": True}
    raise HTTPException(status_code=401, detail="未登录")


@app.post("/api/test")
async def api_test_connection(req: TestRequest):
    """
    测试 API 连接
    使用低质量单次请求验证 URL 和 Key 是否可用
    根据 custom_url 决定是否拼接默认路径
    """
    ok, msg, img_path = await verify_connection(req.url, req.api_key, req.model, req.custom_url)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg, "image_path": img_path}


@app.post("/api/submit")
async def api_submit(req: SubmitRequest, bg: BackgroundTasks):
    """
    提交压测任务
    创建供应商记录，并在后台启动阶梯式压测
    根据 custom_url 决定是否拼接默认路径
    """
    conn = get_db()
    cur = conn.execute(
        """
        INSERT INTO suppliers (name, phone, url, api_key, model, custom_url, source, test_mode)
        VALUES (?, ?, ?, ?, ?, ?, 'supplier', 'step')
        """,
        (req.name, req.phone, req.url, req.api_key, req.model, int(req.custom_url))
    )
    supplier_id = cur.lastrowid
    conn.commit()
    conn.close()

    # 后台启动压测任务
    bg.add_task(run_batch, supplier_id, req.url, req.api_key, req.model, req.custom_url)

    return {
        "success": True,
        "supplier_id": supplier_id,
        "message": "压测任务已提交，将按 20→50→100→500 并发阶梯执行"
    }


@app.post("/api/admin/tasks")
async def api_admin_create_task(req: ManualTaskRequest, request: Request, bg: BackgroundTasks):
    """管理员手动创建压测任务。"""
    require_admin(request)

    name = req.name.strip()
    url = req.url.strip()
    api_key = req.api_key.strip()
    model = (req.model or "gpt-image-2").strip()
    if not name:
        raise HTTPException(status_code=400, detail="请填写任务/供应商名称")
    if not url:
        raise HTTPException(status_code=400, detail="请填写 API 地址")
    if not api_key:
        raise HTTPException(status_code=400, detail="请填写 API Key")

    try:
        concurrency_levels = parse_concurrency_setting(req.concurrency, req.stepped)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    mode = "step" if req.stepped else "single"

    conn = get_db()
    supplier_id, test_ids = create_manual_test_records(
        conn,
        name=name,
        phone=req.phone,
        url=url,
        api_key=api_key,
        model=model,
        custom_url=req.custom_url,
        concurrency_levels=concurrency_levels,
        mode=mode,
    )
    conn.close()

    bg.add_task(run_task_sequence, test_ids, concurrency_levels, url, api_key, model, req.custom_url)

    return {
        "success": True,
        "supplier_id": supplier_id,
        "test_ids": test_ids,
        "concurrency_levels": concurrency_levels,
        "mode": mode,
        "source": "admin",
        "message": "管理员手动压测任务已创建"
    }


@app.get("/api/suppliers")
async def api_get_suppliers(request: Request):
    """获取所有供应商列表"""
    require_admin(request)
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, phone, url, model, source, test_mode, created_at FROM suppliers ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/tasks/{supplier_id}")
async def api_get_tasks(supplier_id: int, request: Request):
    """获取指定供应商的所有压测任务"""
    require_admin(request)
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM stress_tests WHERE supplier_id=? ORDER BY id ASC",
        (supplier_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/report/{supplier_id}")
async def api_get_report(supplier_id: int, request: Request):
    """获取供应商的完整压测报告（含供应商信息和所有任务详情）"""
    require_admin(request)
    conn = get_db()
    supplier = conn.execute(
        "SELECT id, name, phone, url, model, source, test_mode, created_at FROM suppliers WHERE id=?",
        (supplier_id,)
    ).fetchone()
    if not supplier:
        conn.close()
        raise HTTPException(status_code=404, detail="供应商不存在")

    tasks = conn.execute(
        "SELECT * FROM stress_tests WHERE supplier_id=? ORDER BY id ASC",
        (supplier_id,)
    ).fetchall()
    conn.close()

    return {
        "supplier": dict(supplier),
        "tasks": [dict(t) for t in tasks]
    }


# ==================== 启动入口 ====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000"))
    )
