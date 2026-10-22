# 金价观察 · gold-watch

每天定时抓一次金价，价格达到你设定的阈值时，通过微信发一条提醒。

- 零第三方依赖，只需要 Python 3.11+（用到标准库 `tomllib` / `zoneinfo`）
- 行情支持：国际现货 XAU/USD、上海黄金交易所 Au99.99、COMEX 黄金
- 规则支持：低于 / 高于目标价、人民币或美元计价、冷却时间、持续触发重复提醒
- 微信推送支持：Server酱、PushPlus、WxPusher、企业微信群机器人；也可以只打印到终端
- macOS 用 LaunchAgent 开机自启；Linux 可用 cron / systemd

> 这是价格提醒工具，不执行交易，也不构成投资建议。

---

## 1. 五分钟上手

```bash
cd gold-watch
cp config.example.toml config.toml
cp .env.example .env
chmod 600 .env

# 编辑 config.toml：改规则、检查时间
# 编辑 .env：填入你选的微信通道凭据
```

先不推送，看看当前行情和消息长什么样：

```bash
python3 gold_watch.py check --config config.toml --dry-run
```

再发一条测试消息到微信：

```bash
python3 gold_watch.py test-push --config config.toml
```

前台常驻运行：

```bash
python3 gold_watch.py run --config config.toml
```

日常使用建议安装成 LaunchAgent（开机自启、崩溃自动拉起）：

```bash
python3 gold_watch.py install-launchd --config config.toml
```

---

## 2. 微信推送怎么配

`.env` 里只填你实际使用的那一个通道即可，`config.toml` 的 `[push] provider` 要和它对应。

| provider | 收到消息的地方 | 需要什么 | 适合 |
|---|---|---|---|
| `serverchan` | 个人微信（Server酱服务号） | `SERVERCHAN_SENDKEY` | 最省事，个人用 |
| `pushplus` | 个人微信（PushPlus 服务号） | `PUSHPLUS_TOKEN` | 个人用，免费额度较多 |
| `wxpusher` | 个人微信（WxPusher 服务号） | `WXPUSHER_APP_TOKEN` + `WXPUSHER_UID` | 需要稳定 App 通道 |
| `wecom` | 企业微信群 | `WECOM_WEBHOOK_URL` | 团队/群提醒 |
| `console` | 终端 | 无 | 调试、dry-run |

### 2.1 Server酱（个人微信，推荐）

1. 打开 <https://sct.ftqq.com/>，用微信扫码登录。
2. 复制页面上方的 **SendKey**。
3. 写入 `.env`：

   ```dotenv
   SERVERCHAN_SENDKEY=SCTxxxxxxxxxxxxxxxx
   ```

4. `config.toml`：

   ```toml
   [push]
   provider = "serverchan"
   ```

5. 测试：

   ```bash
   python3 gold_watch.py test-push --config config.toml
   ```

Server酱免费版有每日条数限制；本工具默认只在触发阈值时发送，够用。如果一直触发，把 `repeat_hours` 调大或设为 0。

### 2.2 PushPlus（个人微信）

1. 打开 <https://www.pushplus.plus/>，微信扫码登录。
2. 复制 **token**。
3. `.env`：

   ```dotenv
   PUSHPLUS_TOKEN=xxxxxxxxxxxxxxxx
   ```

4. `config.toml`：

   ```toml
   [push]
   provider = "pushplus"
   ```

### 2.3 WxPusher（个人微信）

1. 打开 <https://wxpusher.zjiecode.com/>，创建一个应用，拿到 **APP_TOKEN**。
2. 关注应用二维码，拿到自己的 **UID**。
3. `.env`：

   ```dotenv
   WXPUSHER_APP_TOKEN=AT_xxxxxxxx
   WXPUSHER_UID=UID_xxxxxxxx
   ```

4. `config.toml`：

   ```toml
   [push]
   provider = "wxpusher"
   # 也可以写在这里，二选一：
   # uids = ["UID_xxxxxxxx"]
   ```

### 2.4 企业微信群机器人

1. 在企业微信群里打开 **群设置 → 群机器人 → 添加机器人**。
2. 复制 Webhook 地址（包含 `key=`）。
3. `.env`：

   ```dotenv
   WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx
   ```

4. `config.toml`：

   ```toml
   [push]
   provider = "wecom"
   ```

企业微信机器人是发到群里的，不是个人微信；如果只想自己收，建一个只有自己的群。

### 2.5 只打印，不推送

```toml
[push]
provider = "console"
```

配合 `check --dry-run` 可以完整预览消息，不会写状态。

---

## 3. 规则怎么写

```toml
[[rules]]
name = "国内金价跌破 870"
direction = "below"      # below = 价格 ≤ price；above = 价格 ≥ price
price = 870
unit = "CNY/g"
repeat_hours = 24        # 持续触发时 24 小时再提醒一次；0 = 只在穿越时提醒
cooldown_hours = 2       # 两次提醒至少间隔 2 小时
enabled = true
```

### 单位

| unit | 含义 | 典型来源 |
|---|---|---|
| `CNY/g` | 人民币/克 | 上金所 Au99.99 |
| `USD/oz` | 美元/金衡盎司 | 国际现货、COMEX |
| `CNY/oz` | 人民币/盎司 | 换算 |
| `USD/g` | 美元/克 | 换算 |

行情源单位和你规则单位不一致时，工具会自动用 USD/CNY 汇率换算；同一货币内（如 `CNY/g` ↔ `CNY/oz`）不需要汇率。

### 触发逻辑

- **穿越提醒**：上一次检查没触发，这次触发 → 发一条。
- **持续提醒**：价格一直在阈值内，`repeat_hours` 到了 → 再发一条；`0` 表示不再发。
- **冷却**：同一规则两次提醒之间至少隔 `cooldown_hours`。
- **首次运行**：如果价格已经在阈值内，会直接提醒一次（当作穿越）。
- **多条规则同时触发**：合并成一条消息发送，不会刷屏。
- **发送失败**：状态不会标记为已提醒，下一次检查会重试，避免漏报。

规则名要唯一；规则按 `name` 保存状态，改名会被当成一条新规则。

---

## 4. 定时

### 4.1 每天固定时间

```toml
[schedule]
daily_at = ["09:30", "21:30"]
```

`run` 会按这个时间常驻检查；`install-launchd` 会把 `run` 交给 LaunchAgent，开机自启。

如果希望**每个检查点都收到一条消息**（不管价格有没有触价）：

```toml
[schedule]
daily_at = ["10:00", "14:30"]
daily_report = true
```

- 价格跌破阈值时，标题是「金价提醒」，正文列出已触发的规则；
- 没跌破时，标题是「金价日报」，正文列出当前价、规则状态和距离目标的百分比；
- 默认 `daily_report = false`，只在触发阈值时发；
- 不要和 `interval_minutes > 0` 一起用，否则每个间隔都会发一条。

### 4.2 盘中每 N 分钟

```toml
[schedule]
interval_minutes = 30
active_hours = ["08:00", "23:00"]
weekdays_only = true
```

- `interval_minutes > 0` 时按间隔检查，`active_hours` 之外不检查。
- `weekdays_only = true` 跳过周六、周日（金市休市）。
- 支持跨夜窗口，例如 `active_hours = ["20:00", "02:00"]`。

### 4.3 macOS LaunchAgent

```bash
# 安装并启动
python3 gold_watch.py install-launchd --config config.toml

# 查看 launchd 状态
launchctl print gui/$(id -u)/com.goldwatch.agent

# 卸载
python3 gold_watch.py uninstall-launchd
```

- plist 写到 `~/Library/LaunchAgents/com.goldwatch.agent.plist`
- `KeepAlive` 会在进程崩溃后自动拉起
- 日志：`logs/gold-watch.log`、`logs/launchd.out.log`、`logs/launchd.err.log`
- 只打印 plist 不安装：`--print-only`

> 笔记本合盖睡眠时不会检查；醒来后常驻进程会补跑一次，然后回到日程。如果应用完全没运行，错过的时间点不会补跑。

### 4.4 Linux cron

```cron
30 9 * * * cd /path/to/gold-watch && /usr/bin/python3 gold_watch.py check --config config.toml >> logs/cron.log 2>&1
```

cron 只适合每天一次；要盘中每 N 分钟，用 `run` + systemd，或把 cron 写成 `*/30 * * * *` 并设置 `interval_minutes = 0`（每次调用只检查一次）。

---

## 5. 行情源

| provider | 数据 | 单位 | 说明 |
|---|---|---|---|
| `goldapi` | 国际现货 XAU/USD | USD/oz | <https://api.gold-api.com>，免密钥，更新快 |
| `sge` | 上金所 Au99.99 | CNY/g | 新浪财经接口，国内交易时段更新 |
| `comex` | COMEX 黄金 | USD/oz | 新浪财经接口 |

`[source] fallbacks` 按顺序回退。任何一个源失败都会自动试下一个，消息里会写明实际使用的来源。

汇率源：

| provider | 说明 |
|---|---|
| `sina` | 新浪在岸 USD/CNY，盘中较新 |
| `frankfurter` | 欧洲央行日更 |
| `erapi` | open.er-api 日更 |
| `fixed` | 固定值，配合 `fx.fixed_rate` |

汇率在进程内缓存 15 分钟，避免每次检查都多打一次接口。

---

## 6. 命令

```bash
python3 gold_watch.py check --config config.toml [--dry-run]
python3 gold_watch.py run --config config.toml [--run-now]
python3 gold_watch.py test-push --config config.toml [--dry-run]
python3 gold_watch.py status --config config.toml [--json]
python3 gold_watch.py install-launchd --config config.toml [--label com.goldwatch.agent] [--print-only]
python3 gold_watch.py uninstall-launchd [--label com.goldwatch.agent]
```

- `check`：检查一次，退出码 `0` 正常、`1` 行情/推送失败、`2` 配置或状态错误。
- `run`：常驻运行；`--run-now` 启动时先检查一次。
- `test-push`：发测试消息，不读规则、不写状态。
- `status`：看上次检查、上次行情和每条规则的状态。

`--config` 可以放在子命令前或后，两种写法都行。

---

## 7. 文件与状态

```text
gold-watch/
├── config.toml          # 你的配置（从 example 复制，已被 .gitignore）
├── .env                 # 密钥（已被 .gitignore，建议 chmod 600）
├── state.json           # 上次检查/规则状态，自动生成，权限 600
├── logs/gold-watch.log  # 滚动日志（1MB × 3）
├── gold_watch.py        # 入口
├── goldwatch/           # 配置、行情、规则、推送、调度、状态
└── tests/               # 单元测试
```

`.env` 支持 `KEY=VALUE` 和 `export KEY=VALUE`；已经存在的系统环境变量优先，不会被 `.env` 覆盖。

---

## 8. 开发与测试

```bash
# 全部单元测试（不联网）
python3 -m unittest discover -s tests -v

# 语法检查
python3 -m compileall -q goldwatch gold_watch.py
```

测试覆盖：单位换算、阈值穿越/冷却/重复提醒、发送失败重试状态、配置校验与环境变量展开、每日/间隔调度、状态文件原子写与文件锁、消息内容。

---

## 9. 常见问题

**收不到消息？**

1. 先跑 `python3 gold_watch.py test-push --config config.toml`，确认通道本身通。
2. 检查 `.env` 里的变量名是否和 `[push] provider` 对应。
3. Server酱免费版有每日额度；看 <https://sct.ftqq.com/> 的发送记录。
4. 如果 `check` 日志显示“无需推送”，说明价格没有穿越阈值，或还在冷却/重复间隔内。

**`CERTIFICATE_VERIFY_FAILED`？**

macOS 的 python.org 安装包有时没有系统 CA。工具会优先使用 `certifi`，再回退到 `/etc/ssl/cert.pem`。如果仍然失败：

```bash
python3 -m pip install --user certifi
# 或者
export SSL_CERT_FILE=/etc/ssl/cert.pem
```

**行情接口失败？**

公开接口可能限流或临时不可用。配置 `fallbacks`，例如 `["sge", "comex"]`；日志里会列出每个源失败的原因。

**推送重复太多？**

- 把 `repeat_hours` 设为 `0`：只在穿越时提醒。
- 调大 `cooldown_hours`。
- 多条规则同时触发会合并成一条，不会每条单独发。

**时间不对？**

`app.timezone` 默认 `Asia/Shanghai`；`daily_at` 和 `active_hours` 都按这个时区解释。

---

## 10. 安全

- `.env` 和 `config.toml` 已在 `.gitignore` 中；不要把 SendKey / token / webhook 发到公开仓库。
- 工具不会把密钥写进日志；推送失败时会尽量把 URL 里的 key 替换成 `***`。
- `state.json` 以 `600` 权限写入。
- 公开行情接口只读，不需要账号；余额、交易类接口不在本工具范围内。

---

## License

MIT（仅本工具代码；行情数据归各数据源所有）。
