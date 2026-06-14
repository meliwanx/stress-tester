# 生图压测平台

FastAPI 生图压测后台，支持供应商提交压测任务，也支持管理员登录后台后手动创建测试任务。

## 本地运行

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export ADMIN_USERNAME=liwanx
export ADMIN_PASSWORD=your-password
export ADMIN_COOKIE_VALUE=your-cookie-token
python3 main.py
```

访问：

- 前台提交页：`http://127.0.0.1:8000/`
- 管理后台：`http://127.0.0.1:8000/admin`

## 测试

```bash
python3 -m unittest tests/test_manual_admin_tasks.py
```

## 运行数据

`data.db` 和 `static/images/` 是运行时数据，不提交到 Git。
