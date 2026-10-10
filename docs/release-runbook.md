# 发布与回滚运行手册

以下命令在仓库根目录执行。先用合成数据或脱敏副本演练；数据库备份可能包含真实问题和模型输出，按生产数据保护，不能加入 Git 或公开测试产物。

## 发布前

1. 确认 `.env` 中的 `OPENAI_API_KEY`、`MARKET_GATEWAY_API_KEY` 与 `MARKET_GATEWAY_MODE=http` 可用，并设置 `RESEARCH_BUDGET_SECONDS`、`GRAPH_RECURSION_LIMIT`、`SQLITE_BUSY_TIMEOUT_MS`、`PERSISTENCE_MAX_RETRIES`、`PERSISTENCE_RETRY_BACKOFF_MS`。勿打印 `.env` 或密钥值。Docker 容器使用 UID 10001 和 `/data` 命名卷。
2. 保存当前镜像版本或 digest 与 compose 配置。升级前运行 `docker compose config --quiet`、`python -m pytest tests/test_stage8_release.py tests/test_stage9_release.py -q`、`python -m compileall -q app`。
3. 在停服务前用 SQLite backup API 获取一致性备份，包括 WAL 中已提交的数据。使用容器内可写的 `/data/memory.pre-upgrade.db`，然后把该文件复制到独立于生产卷的位置保存。不要直接复制正在使用的 `memory.db` 单文件。

```bash
docker compose exec -T mosaic python -c "import sqlite3; src=sqlite3.connect('/data/memory.db'); dst=sqlite3.connect('/data/memory.pre-upgrade.db'); src.backup(dst); dst.close(); src.close()"
docker compose cp mosaic:/data/memory.pre-upgrade.db ../memory.pre-upgrade.db
docker compose stop mosaic
docker tag mosaic-market-intelligence:0.1.0 mosaic-market-intelligence:previous
docker compose build mosaic
docker compose up -d --no-build mosaic
curl --fail http://127.0.0.1:8000/health
```

备份文件应移至受控备份位置，按保留政策管理。旧镜像应保留到验证完成。若首次部署没有容器，先用当前镜像运行一次临时容器并挂载目标数据卷，通过相同 backup API 生成备份。

## 升级验收

`/health` 应显示 `status=ok`、`brief_scheduler=running`、`gateway_mode=http`，并回显预算、递归、SQLite busy timeout 和持久化重试设置。该端点不证明模型或 Gateway 凭据可用；再用不含敏感内容的受控问题验证同步 API、SSE 与 CLI 的 `delivery_status`、`final_audit_status`、`persisted`。只有 `verified` 且 `persisted=true` 可作为写入成功。`blocked`、`failed`、`timeout` 均不得写入研究记录。

升级后在运行容器中检查 schema 与历史行数，记录升级前后的计数并确认历史样本可读：

```bash
docker compose exec -T mosaic python -c "import sqlite3; c=sqlite3.connect('/data/memory.db'); print('integrity',c.execute('PRAGMA integrity_check').fetchone()[0]); print('journal',c.execute('PRAGMA journal_mode').fetchone()[0]); print('tables',c.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\").fetchall()); print('index',c.execute(\"SELECT name,sql FROM sqlite_master WHERE name='idx_research_persistence_key'\").fetchone()); print('counts',[(t,c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]) for t in ('daily_states','conversations','research_records')]); c.close()"
```

`integrity` 必须为 `ok`，`journal` 为 `wal`，索引 SQL 必须是 `persistence_key` 上的非空部分唯一索引。若异常，停止服务并恢复备份；不要在生产库上临时删除索引或修改历史记录。

## 日志与指标

收集服务 stdout 的 JSON 日志，保留 `app.graph.run_log` 事件的 `run_id`、`delivery_status`、`final_audit_status`、`error_category` 和 `persistence` 事件。`run_id` 只用于内部关联，不出现在 API/SSE/CLI 对外结果。将日志以 JSONL 形式交给 `python -m app.graph.metrics collected.jsonl`；它可读原始事件 JSONL 与应用 JSON formatter 包装后的 JSONL。汇总报告包括交付、审计、持久化重试、持久化错误类别及失败类别。不要将完整日志提交到仓库。

## 失败恢复与版本回滚

迁移在服务启动时运行。启动失败或检查不通过时，先停止服务。用升级前保存的镜像版本恢复应用代码，并用受控备份恢复整个数据库。以下示例假设备份已放回 `/data/memory.pre-upgrade.db`，且 `mosaic-market-intelligence:previous` 是已保存的旧镜像：

```bash
docker compose stop mosaic
docker compose run --rm --no-deps --entrypoint python mosaic -c "import sqlite3; src=sqlite3.connect('/data/memory.pre-upgrade.db'); dst=sqlite3.connect('/data/memory.db'); src.backup(dst); dst.close(); src.close()"
docker tag mosaic-market-intelligence:previous mosaic-market-intelligence:0.1.0
docker compose up -d --no-build mosaic
curl --fail http://127.0.0.1:8000/health
```

恢复时服务必须已停止，且不能有其它进程写同一数据库。若备份只在宿主机，先使用 `docker compose cp ../memory.pre-upgrade.db mosaic:/data/memory.pre-upgrade.db` 放回容器数据卷；若容器启动失败，可用 `docker compose run --rm --no-deps --entrypoint sh mosaic` 进入挂卷的一次性容器，或用临时容器挂同一命名卷。恢复后复查 `PRAGMA integrity_check`、历史行数和应用健康状态。回滚后新增的研究记录会丢失，这是恢复到升级前快照的预期结果。
