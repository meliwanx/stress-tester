# Agent 说明

本目录 `/Users/liwanx/Documents/Python/生图压测平台` 是“生图压测平台”的当前主项目目录。后续处理 116 服务器上的生图压测平台时，应打开并使用本目录。

不要把本项目和 `/Users/liwanx/Documents/Python/stress-tester` 混用。`stress-tester` 是另一套“API 图片生成压测工具”，页面标题、接口结构和线上 116 的生图压测平台不同，不能作为 116 生产服务的部署来源。

## 项目身份

- 项目名称：生图压测平台
- 本地最新目录：`/Users/liwanx/Documents/Python/生图压测平台`
- 线上正确入口：`http://116.142.250.54:8000`
- 线上代码目录：`/opt/stress_test`
- 前台供应商提交页：`/`
- 管理后台：`/admin`

## 当前代码状态

本目录是 116 服务器上 `stress_test_v4.tar.gz` 同源代码的后续更新版，已经包含：

- 供应商前台提交压测任务
- 管理员登录后台
- 管理员手动创建测试任务
- 管理员自定义并发数
- 管理员选择单次并发或阶梯式并发
- SQLite 旧库字段兼容迁移

116 服务器当前运行版本可能仍是旧版。部署前必须先核对线上进程、备份线上目录和数据库，再同步本目录代码。

## 工作规则

- 修改本项目时优先阅读 `main.py`、`static/index.html`、`static/admin.html`、`README.md`、`SERVER.md`。
- 不要提交或覆盖运行时数据：`data.db`、`.env`、`static/images/`。
- 不要把 API Key、管理员密码、SSH 密码写进代码或文档。
- 管理员密码必须通过环境变量 `ADMIN_PASSWORD` 配置。
- 部署本项目不要使用 `129.204.245.50`，除非用户明确说明那台服务器就是目标。
- 如果用户说“116 服务器”“生图压测平台”“申屠压测平台”，默认指本目录和 `116.142.250.54:8000`。

## 验证命令

本地代码变更后至少运行：

```bash
python3 -m unittest tests/test_manual_admin_tasks.py
```

文档或部署说明变更后至少检查：

```bash
git diff --check
```
