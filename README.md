# 竞速飞行证据补齐与成绩锁定

服务接收脱敏飞行证据并提供成绩处理 HTTP 入口，领域持久化将在后续模块中接入。

## 开发命令

- 安装依赖：`python3 -m pip install -r requirements.txt`
- 运行测试：`python3 -m pytest -q tests/test_health.py`
- 编译或构建检查：`python3 -m compileall -q app.py tests`
- 启动服务：`python3 -m uvicorn app:app --host 0.0.0.0 --port 8000`

测试和构建只使用仓库内数据，不需要连接外部业务服务。
