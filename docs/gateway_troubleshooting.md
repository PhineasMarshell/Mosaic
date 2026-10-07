# Market Gateway 排障手册

> 适用版本：market-gateway 3.2.1 / iiix CLI ≥ 0.8.4。2026-10-06 全工具失败事故的复盘沉淀。

## 一、三步自检（按顺序执行）

```bash
# 1. 登录态
iiix whoami
#   正常：{"email":"…","status":"ACTIVE",…}

# 2. 本地插件版本（读本地 manifest，不走网络）
iiix plugin status
#   正常：[{"project_code":"market-gateway","current":{"version":"3.2.1",…}}]

# 3. 登录态 + 上游连通性一起验（推荐自检命令）
iiix plugin verify market-gateway
#   正常：{"status":"passed","timing":{"client_auth_ms":5090,"gateway_upstream_ms":183.9,…}}
```

Mosaic 自身（mcp 模式）启动时也会做同样的事：spawn + `initialize()` 握手 +
`list_tools()`（默认开启，`GATEWAY_STARTUP_SELFCHECK=false` 可关）。失败不阻塞启动，
但会打 **ERROR 级日志**，且 `/health` 的 `gateway_channel` 字段变为 `mcp_unavailable`：

```bash
curl -s http://127.0.0.1:8000/health | jq .gateway_channel
# ok / mcp_unavailable / not_checked（自检关闭）/ not_applicable（http 模式）
```

## 二、失败文案 → 处置（原文匹配）

| 场景 | 实际报错（原文） | 处置 |
|---|---|---|
| iiix 版本过旧 / 用了已删除的子命令 | `iiix: MCP 已停用: 服务器目录已移除或当前账号无权使用`；或 `iiix: iiix mcp 已在 0.8.0 删除；请改用 iiix plugin` | iiix CLI ≥ 0.8.0 用 `plugin serve`，确认 `MCP_ARGS=plugin serve market-gateway` |
| 登录失效（网络可达） | `iiix: 登录已失效，请重新执行 iiix login` | 执行 `iiix login`，再用 `iiix plugin verify market-gateway` 确认 |
| OAuth 发现文档超时（**看似登录问题，实为网络路径**） | `读取 OAuth 发现文档: Get "https://logto.x.iiix.dev/…": context deadline exceeded` | 见第三节「代理三件套」——这不是登录问题，别去反复 login |
| OAuth Token 刷新超时（工具调用时） | `刷新 OAuth Token: … context deadline exceeded` | 同上 |
| 工具调用时登录失效 | `{"code":"gateway_error","message":"登录已失效，请重新执行 iiix login"}` | `iiix login`（注意：参数校验先于鉴权，参数错误仍先返回 422） |
| 握手超时 | `MCP initialize (handshake) timed out` | 检查 `RESEARCH_TIMEOUT_SECONDS` 与网络（代理变量） |

## 三、代理三件套（Windows 实测踩坑）

**症状**：握手能过、`list_tools` 正常，但**任何真实 tool call** 都报
`gateway_error: context deadline exceeded`；`iiix plugin verify` 时好时坏。

**根因**：
1. 本机系统代理开着（注册表 `ProxyServer='127.0.0.1:7897'`），但环境变量里没有
   `HTTP_PROXY`/`HTTPS_PROXY`。Python httpx 读系统代理，**iiix（Go）只认环境变量**。
2. `logto.x.iiix.dev` 有 AAAA 记录而本机 IPv6 不通 → Go 直连先试 IPv6、耗尽超时预算。
3. `mcp` SDK 的 `stdio_client` 默认只给子进程继承 12 个白名单变量，**代理变量会被剥掉**
   （已在 `app/gateway/mcp_client.py` 的 `_child_env()` 修复：显式透传 8 个代理变量）。

**处置**（运维侧，新开终端生效）：

```powershell
setx HTTP_PROXY  "http://127.0.0.1:7897"
setx HTTPS_PROXY "http://127.0.0.1:7897"
setx NO_PROXY    "localhost,127.0.0.1"
```

实测：只给 `HTTPS_PROXY` 时 `plugin verify` 仍 TLS 超时（`req=5178ms`），**必须同时有
`HTTP_PROXY`**；加 `NO_PROXY` 后从 `req=6543ms` 降到 `1128ms`。

**另一个静默失败源（已由代码兜底）**：`NO_PROXY` 里形如 `[::1]` 的方括号 IPv6 会让
httpx **构造 client 就崩**（`InvalidURL: Invalid port: ':1]'`），HTTP 模式与内部直连工具
全线失败。`app/net_env.py` 在进程内归一化（不写注册表），`/health` 的
`proxy_env_warnings` 非空即说明发生过归一化。

## 四、operationId 与版本对齐

- **MCP 工具名 = OpenAPI operationId**，逐字匹配；上游 3.2.1 全量改名过一次
  （40 条），注册表已同步。再遇 `Tool is not allowed by registry: <name>` →
  上游又改名了，对照新 spec（`%LOCALAPPDATA%\iiix\plugins\market-gateway\versions\<v>\extracted\api\openapi.json`）
  更新 `app/gateway/tool_registry.py`。
- 回归闸门：`tests/test_gateway_tool_names_321.py`（旧名黑名单 + 新名可解析）。
- 真调探针：`.venv/Scripts/python.exe scripts/verify_gateway_321.py`。

## 五、已知边界（2026-10-06 实测）

- A 股筹码/股东数据域（`/ashare/chips/*`，8 个端点）在 Gateway 3.2.1 已暴露且参数/结构正确，
  但实测所有标的 `coverage_status=not_collected`（采集侧未入库）。Mosaic 暂不注册，
  待上游数据可用后再接入。
- `POST /market/snapshot` 对 `exchange=xueqiu` 不支持（422 `exchange='xueqiu' 不支持实时快照`）——
  美股/港股无实时行情快照，行情用 `us_klines`/`us_window`。
- 雪球系工具（`list_stock_discussions` / `get_stock_longhu` / `search_stocks` /
  `get_stock_abnormal_reasons`）依赖雪球上游；上游 Cloudflare 故障时返回
  504/502/TLS handshake timeout，属**上游源站故障**，不是本机通道问题——
  用 `iiix plugin verify`（binance 系端点）区分两者。
