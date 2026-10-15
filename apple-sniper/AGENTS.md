# AGENTS.md

`apple-sniper`：Apple 产品到货监控与抢购辅助工具。详细用法见 [README.md](README.md)。

## 交付工作台（wb）

本项目已接入 `wb`（coding-workbench），**起服务、校验、发布不要自己拼命令**：

| 场景 | 命令 |
|---|---|
| 起本地看板做验证 | `wb dev`（打印真实 URL）/ `wb stop` |
| 提交前校验 | `wb verify`（本仓库只跑 pytest） |
| 发版本 | `wb release patch\|minor\|major` |
| 配置双远程 | `wb mirror --github <owner/repo> --gitee <owner/repo>` |
| 推 GitHub + Gitee | `wb push all` |
| 环境/网络自检 | `wb doctor` |

**约定（agent 必须遵守）**

- 网络动作（git push / fetch / gh）必须走本地代理 `socks5h://127.0.0.1:7890`；wb 已自动注入，
  不要绕过，也不要手改全局 git 代理配置。
- `.wb/config.json` **提交进仓库**（团队共享）；`.wb/state.json`、`.wb/logs/`、`.wb/reports/` **不提交**。
- 验证结果一律看 `.wb/reports/verify-*.md`，不要凭「看起来没问题」下结论。
- 发布前工作区必须干净；版本提升 + CHANGELOG + tag 由 `wb release` 一次完成，不要手动 `git tag`。
- gitee 若不可达，先跑 `wb doctor` 看是不是 DNS 污染，不要反复重试。

## 本仓库特定约定

- 依赖用 venv：`python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`；
  跑命令一律用 `.venv/bin/python -m apple_sniper ...`（无需先安装为命令）。
- 测试必须**离线**：网络走 `httpx.MockTransport`，时间走假时钟，睡眠/打开链接/命令执行全部注入。
  新增用例不得发起真实网络请求、不得 `sleep`、不得弹窗、不得起真实进程。
- 改动抓取/判定逻辑后，先对真实页面用 `probe <目标id> --chars 2000` 核对字段路径，再更新配置。
- **不要引入**绕过验证码/风控、自动登录、代理池轮换、自动下单付款之类的能力（见 README「能力边界与合规」）。
- `config/settings.yaml`、`config/targets.yaml`、`data/`、`tmp/` 含个人配置与运行数据，已被 `.gitignore` 忽略，不要提交。
