# apple-sniper

Apple 产品**到货监控与抢购辅助**工具：对公开商品页做低频轮询，判定「有货 / 无货」，
在**补货的瞬间**通过桌面通知、手机推送或群机器人提醒你，并可选一键打开购买页。

> 一句话：**它负责盯，你负责买。** 它不会替你下单。

---

## 能力边界与合规（请先读这一节）

这个工具的定位是「可用性监控 + 提醒」，据此**刻意不做**下面这些事：

| 明确不做 | 原因 |
|---|---|
| 绕过验证码 / 风控 | 属于对抗网站安全机制，不做 |
| 自动登录、代填账号密码 | 涉及账号安全，交给人工 |
| 伪造请求、批量压测、代理 IP 池轮换 | 属于滥用与反爬对抗，不做 |
| 自动提交订单 / 自动付款 | 支付与下单必须由人工确认 |

它**只做**：按较低频率请求**公开可访问**的商品/库存接口或页面 → 判断状态变化 →
发通知 → （可选）用系统默认浏览器打开购买页。

因此：

- 轮询频率默认**保守**（30s ± 抖动，单主机最短 3s，上限 20 次/分），可在 `settings.yaml` 调，
  但请自行确认节奏**不违反目标站点的使用条款与 robots 约定**。
- 请只监控**你自己有权访问**的公开页面；内网/需登录页面请自备合规授权。
- 目标站点改版随时可能让字段路径失效——这正是提供 `probe` 子命令的原因，请用它核对。

---

## 快速开始

无需联网、无需账号，先跑离线演示（全程 mock 来源，约 20 秒）：

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'        # 或最小依赖：.venv/bin/pip install httpx PyYAML

bash scripts/demo.sh                      # 离线端到端演示
```

演示会依次制造：首检有货 → 转无货 → 补货 → 降价 → 抓取失败 → 恢复，
最后打印监控实时输出与落库的事件历史。

### 配置自己的目标

```bash
cp config/settings.example.yaml config/settings.yaml
cp config/targets.example.yaml  config/targets.yaml
```

`config/settings.yaml` 与 `config/targets.yaml` 已在 `.gitignore` 中（可能含个人 webhook key）。

### 常用命令

```bash
.venv/bin/python -m apple_sniper targets                  # 列出目标与运行参数
.venv/bin/python -m apple_sniper probe <目标id> --chars 1200   # 抓一次并打印真实响应，核对字段路径
.venv/bin/python -m apple_sniper check                    # 跑一轮并落库/通知（适合 cron）
.venv/bin/python -m apple_sniper drill --act              # 发一条合成「已到货」，验证通知链路与命中动作
.venv/bin/python -m apple_sniper watch                    # 持续轮询，命中即通知（Ctrl-C 退出）
.venv/bin/python -m apple_sniper history -n 30            # 查看事件历史与最新状态
.venv/bin/python -m apple_sniper forget --target <目标id> # 清空该目标历史，重新开始
.venv/bin/python -m apple_sniper serve --port 8787        # 本地只读看板（需 dashboard 额外依赖）
```

安装为命令后可直接用 `apple-sniper ...`；不安装也可用 `python -m apple_sniper ...`。
所有子命令都支持 `--config-dir demo` 之类指定配置目录。

### 定时跑（macOS 用 launchd，别用 crontab）

`check` 就是为「跑一轮就退出」设计的（状态存 SQLite，跨轮去重）。仓库里的
`scripts/cron-check.sh` 已封装好虚拟环境路径、日志与防重入锁，交给定时器调用即可。

macOS 上 **crontab 不可靠**：`/usr/sbin/cron` 需要「完全磁盘访问权限」才会执行任务，
没授权时任务**静默不跑**（本机实测：连 `/bin/date` 探针都不执行）。改用 launchd：

```bash
cp scripts/com.dgjin.apple-sniper.check.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dgjin.apple-sniper.check.plist
launchctl print   gui/$(id -u)/com.dgjin.apple-sniper.check | grep -E 'state|runs'  # 看状态
launchctl bootout gui/$(id -u)/com.dgjin.apple-sniper.check                         # 停止
```

间隔由 plist 里的 `StartInterval`（默认 60s）决定，改完重新 bootout/bootstrap 生效。
LaunchAgent 跑在你的**图形登录会话**里，所以 `open_url_on_hit` 与 `macos` 桌面通知**都有效**。
日志追加到 `data/check.log`（launchd 自身的 stdout/stderr 在 `data/launchd.*.log`）。
`cron-check.sh` 会在日志超过阈值时自动轮转（只保留末尾若干行，避免无上限增长）：
默认阈值 1 MiB / 保留 2000 行，可用 `APPLE_SNIPER_LOG_MAX_BYTES`、`APPLE_SNIPER_LOG_KEEP_LINES` 覆盖。

#### 窗口期一键收紧（`scripts/window-mode.sh`）

`StartInterval` 是「两轮之间的**空闲**时间」，**不是轮次周期**——实际周期 ≈ `StartInterval` + 单轮耗时
（6 店单轮约 18s；受 `max_requests_per_minute` 约束，6 店 ~20s 已是下限）。所以「想 ~20s 一轮」要填 `2`，
填 `20` 只会得到 ~38s。

```bash
scripts/window-mode.sh on       # 进窗口：间隔 60→2（≈20s 周期）+ caffeinate 防睡眠
scripts/window-mode.sh off      # 出窗口：恢复 60，停 caffeinate
scripts/window-mode.sh status   # 当前模式 / 已装载与仓库的间隔 / job / caffeinate / 最近轮次
```

脚本按「停调度 → 等旧 job 注销 → 清残留锁 → 落新 plist → bootstrap → 校验 → 确认新轮次」的顺序做
（`bootout` 是**异步**的，紧接着 `bootstrap` 会静默失败把监控搞丢）；仓库里的 plist 始终是「常态」基准、
不会被改。`on` 会启动**无限期** `caffeinate`，窗口结束务必 `off`。

Linux（或已给 cron 授权）就直接 crontab：

```bash
* * * * * /绝对路径/apple-sniper/scripts/cron-check.sh
```

想在人不在电脑前也收到提醒（点一下直接跳购买页），接一个 webhook 渠道（飞书 / Bark），
见下面「通知渠道」。

---

## 它是怎么判断「有货」的

每个目标（`targets.yaml` 里的一项）绑定一个**来源类型**，来源负责「抓取 + 判定」：

| 来源 `kind` | 适用 | 关键配置（`match`） |
|---|---|---|
| `apple_fulfillment` | Apple 官网「到店取货」类接口（**仅非中国区**） | `paths`（候选字段路径）、`store_availability_path`、`store_availability_key`、`store_name_path` |
| `generic_json` | 任意返回 JSON 的库存/比价接口 | `path`（点路径，支持 `a.b[0].c`）、`when`（判定表达式）、`price_path` |
| `html_regex` | 只能抓 HTML 的场景 | `present` / `absent`（正则；`absent` 命中优先）、`price_regex` |
| `mock` | 演示与自测，读本地 JSON，**不联网** | `inventory_file` |

判定表达式（`when`）：`exists`、`not_exists`、`non_empty`、`truthy`、
`equals:值`、`not_equals:值`、`contains:子串`、`not_contains:子串`、
`gt:数`、`gte:数`、`lt:数`、`lte:数`。

`apple_fulfillment` 与 `generic_json` 需要的字段路径**因站点而异、会随改版变化**，
所以代码里只保留候选缺省值。正确做法是：

> 中国区不要用 `apple_fulfillment`：`/shop/fulfillment-messages` 在中国区实测返回 **541**（已失效）。
> 中国区的库存走 `/shop/sba/availability-message`，按 `generic_json` 配，见 `iphone-duo-cn`。

```bash
.venv/bin/python -m apple_sniper probe <目标id> --chars 2000   # 看真实响应长什么样
# 把看到的字段路径填进 targets.yaml 的 match，再 probe 一次确认判定正确
```

`config/targets.example.yaml` 里带了一个**已联网核对过**的真实样例
`iphone-duo-cn`（iPhone Duo 1TB 夜空色 · 北京西单大悦城）：库存走 Apple 公开接口
`/shop/sba/availability-message`，`buy_url` 是预选型号的「加入购物袋」深链。
换机型/换门店时按上面的 probe 流程重新核对即可。

配置写错（未知 kind、未知 `when`、缺 `path`、`paths` 为空列表等）会在**启动时**报错，
不会静默失效。

---

## 状态机语义（为什么它不误报）

状态存进 SQLite（`state_db`），由「检查结果流」压成「状态转移事件流」：

- **`ok=False` 表示「没拿到可信结论」**（网络抖动、解析失败），与「确认无货」严格区分。
- **抓取失败不改写「已知库存」**：只累加 `consecutive_errors`，库存保留上一次成功判定的值。
  否则一次抖动就会把状态刷成未知，等恢复时又被误判成「已到货」——假阳性比漏报更糟。
  看板/`history` 会把这类目标标成「数据可能陈旧」。
- **事件靠状态转移去重，不靠计时器**：到货/无货是转移，天然不会重复；
  只有「持续抓取失败」这类持续状态用 `renotify_cooldown_seconds` 抑制刷屏。

事件类型：`back_in_stock`（已到货）、`out_of_stock`（已无货）、`price_drop`（降价）、
`error`（抓取异常）、`recovered`（恢复正常）。

哪些事件要通知，由 `settings.yaml` 的 `notify_kinds` 决定（默认不含 `recovered`）。

---

## 通知渠道

可叠加多个渠道，**任何一个渠道失败都不影响其它渠道**：

- `console`：打印到终端。
- `macos`：macOS 桌面通知 + 可选提示音（非 macOS 自动降级为空操作）。
- `webhook`：内置 `generic` / `bark` / `feishu` / `wecom` / `dingtalk` / `slack` 模板。
- `command`：执行任意命令；事件字段通过 `APPLE_SNIPER_*` **环境变量**传入
  （用 `create_subprocess_exec`，**不经 shell**，避免把页面内容拼成注入）。

`webhook` 会核对响应体里的业务码（`feishu` 的 `code`、`wecom`/`dingtalk` 的 `errcode`、
`bark` 的 `code`、`slack` 的 `ok`）——这些平台**失败时也返回 HTTP 200**，
只看状态码会把「没发出去」当成功。判为失败会记进失败列表并打 WARNING 日志。

### 接飞书群机器人（手机推送）

1. 在目标群里「设置 → 群机器人 → 添加机器人 → 自定义机器人」，拿到 Webhook 地址。
2. 把 channel 加进 `config/settings.yaml` 的 `channels`（URL 等同密钥，别提交）：

```yaml
channels:
  - kind: console
  - kind: webhook
    template: feishu
    url: "https://open.feishu.cn/open-apis/bot/v2/hook/<你的 token>"
```

安全设置三种都能用：

- 「自定义关键词」或「IP 白名单」：照上面填 `url` 就行。
- 「签名校验（加签）」：把机器人页面给出的密钥填进 `secret`，发送时会自动带上 `timestamp` + `sign`；
  若漏填，飞书会回 `code=19021`（webhook 渠道会如实报为该次通知失败）：

```yaml
  - kind: webhook
    template: feishu
    url: "https://open.feishu.cn/open-apis/bot/v2/hook/<你的 token>"
    secret: "<加签密钥>"
```

### 命中动作（默认全关）

「已到货」时可选：

- `open_url_on_hit: true`：用系统默认浏览器打开购买页（`buy_url` 优先于 `url`）。
- `command_on_hit: "..."`：执行命令，例如播提示音 `afplay`。
- **语音播报**：仓库自带 `scripts/say-hit.sh`，命中时用 macOS `say` 念出「哪个门店 · 已到货」，
  人不在屏幕前也能听见（后台念，不阻塞后续的升级提醒）：

```yaml
  command_on_hit: "/绝对路径/apple-sniper/scripts/say-hit.sh"
```

  换音色用环境变量 `APPLE_SNIPER_VOICE`（默认 `Tingting`，macOS 自带中文女声；`say -v '?'` 看全部）。
- **升级提醒**：`escalate_repeats`（含首次，默认 1）+ `escalate_interval_seconds`（默认 5）
  把同一条到货通知重复推 N 次、每次隔几秒。这是「重复提醒」而非「反复探测」——
  只多推几次通知，**不改请求频率**。
- **心跳通知**：`notify_heartbeat_seconds`（默认 0 = 关）到点推一条「监控仍在运行」的汇总，
  用来区分「还没到货」和「监控悄悄挂了」。节流时间记在状态库里，所以每轮一个新进程的 `check`
  也能按间隔推送。开启时**记得把 `heartbeat` 加进 `notify_kinds`**，否则事件产生但不推送。

#### 直达「加入购物袋」页（预选型号）

`buy_url` 和 `url` 一样支持 `{参数}` 模板，取值来自 `match.params`（外加隐式 `{id}`/`{name}`）。
把型号/颜色/容量写进 `params`，命中时就能直接打开**已预选好规格**的购买页，你只需点「放入购物袋 → 结账」：

```yaml
buy_url: "https://www.apple.com.cn/shop/buy-iphone/iphone-duo?color={color}&capacity={capacity}"
match:
  params:
    color: "深空灰"
    capacity: "1TB"
```

命中后会打开 `.../iphone-duo?color=深空灰&capacity=1TB`。**URL 是模板，Apple 改版会变**，
请用 `probe` 核对真实页面/接口后再启用。注意：**这里只做提醒和跳转，不会替你下单或付款。**

### 命中演练（上真货前先自测）

真到货那一刻只有一次机会，通知链路要提前验通。`drill` 会发一条**合成**的「已到货」
（不抓页面、不下单），逐个渠道把成功/失败摊开打印：

```bash
.venv/bin/python -m apple_sniper drill            # 逐渠道验证通知
.venv/bin/python -m apple_sniper drill --act      # 连命中动作一起验（真的打开购买页 + 语音）
.venv/bin/python -m apple_sniper drill --target iphone-duo-cn --message "自测"
```

等价脚本 `bash scripts/hit-drill.sh [--act] [--target <id>]`（自动用仓库虚拟环境）。
它**刻意绕开 `notify_kinds` 过滤**逐渠道试，这样「渠道没配好」和「事件没订阅」两类问题不会混在一起。
有渠道失败时退出码为 1，方便脚本里判断。

---

## 目录结构

```
apple_sniper/
  models.py        领域模型：Target / CheckResult / Event（时间统一带时区 UTC）
  config.py        YAML + 环境变量加载与校验（失败信息带文件名 + 字段名）
  ratelimit.py     单主机限速：串行 + 最小间隔 + 令牌桶 + 只加不减的抖动
  http.py          抓取层：统一 UA/超时 + 仅对 429/408/425/5xx 重试 + 尊重 Retry-After
  extract.py       点路径取值 + 判定表达式编译
  sources/         来源适配器：mock / generic_json / html_regex / apple_fulfillment
  store.py         SQLite 状态机（target_state / snapshots / events）
  notify/          通知渠道与 fan-out
  engine.py        调度循环 + 命中动作（时钟/睡眠/随机数/打开链接/命令全部可注入）
  cli.py           子命令：targets / probe / check / drill / watch / history / forget / serve
  server.py + static/dashboard.html   本地只读看板（FastAPI）
config/            配置样例（settings.example.yaml / targets.example.yaml）
demo/              离线演示用的 mock 配置与库存文件
scripts/demo.sh          离线端到端演示脚本
scripts/hit-drill.sh     命中演练：用真实渠道发合成「已到货」
scripts/cron-check.sh    定时入口（给 launchd/cron 调用，含防重入锁与日志轮转）
scripts/window-mode.sh   窗口期一键收紧/恢复（on / off / status）
scripts/say-hit.sh       命中语音播报（command_on_hit 调用）
tests/             pytest 用例（全部离线：mock 来源 + MockTransport + 假时钟）
```

---

## 测试与校验

```bash
.venv/bin/python -m pytest          # 全量单测（离线，秒级）
bash scripts/demo.sh                # 离线端到端演示
```

测试全程**不联网、不睡眠、不弹窗、不起真实进程**：网络用 `httpx.MockTransport`，
时间用可推进的假时钟，睡眠/打开链接/命令执行都是注入的假实现。
因此上面这些用例可以在 CI 与无网环境下稳定复现。

---

## 常见问题

- **判定总是「无货」**：多半是字段路径过期。用 `probe` 打印真实响应，更新 `match` 后重试。
- **不想被刷屏**：调大 `renotify_cooldown_seconds`；或把 `notify_kinds` 收窄到只留 `back_in_stock`。
- **想确认状态库长什么样**：`sqlite3 data/apple_sniper.db '.tables'`，
  或起看板 `serve` 图形化查看。
- **重来一遍**：`forget --target <id>` 只清该目标；直接删 `data/apple_sniper.db` 会清空全部。
- **webhook 发不出去、curl 却通**：多半是走了本机 macOS 系统代理（如 `127.0.0.1:7890`），
  它会打断部分域名的 TLS 握手（`ConnectError(EndOfStream())`）。程序默认**直连**、刻意忽略系统代理；
  确实要走代理时在 `settings.yaml` 显式填 `http_proxy: "http://127.0.0.1:7890"`。

