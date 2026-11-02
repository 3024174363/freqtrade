# QR 开发环境与 A 股图表

本目录保存 QR 根目录的开发环境文件。前后端分别使用 `freqtrade` 与 `frequi` Fork 的 `qr-dev` 分支；恢复目录和首次启动步骤见 [RESTORE.md](RESTORE.md)。

## 入口与启停

- 唯一页面：http://127.0.0.1:5173
- Chart：http://127.0.0.1:5173/graph
- API：http://127.0.0.1:8090/api/v1
- API 文档：http://127.0.0.1:8090/docs
- 用户名：`qr`。随机密码由初始化脚本生成，只保存在运行目录的 `user_data/local-access.txt`。

恢复本目录中的文件到 QR 根目录后，启动 Docker Desktop，在 QR 根目录执行：

```powershell
.\start.ps1
docker compose ps
docker compose logs --tail 80 backend frontend
.\stop.ps1
```

只启动研究 webserver，未配置交易所密钥，使用 `dry_run=true`，单边手续费 0.1%。`8090` 的业务页面和静态资源返回 JSON 404，API、WebSocket 与 `/docs` 保留。`5173/api` 代理到后端。现有本机配置重启不会被初始化脚本覆盖。

## A 股 Chart

在 Chart 顶部选择 **A 股**，按中文名称、六位代码或完整代码搜索，再从结果中选择标的。例如 `000001` 显示上证指数 `000001.SH` 和平安银行 `000001.SZ`，不混用市场。无需新增 Bot、选择加密货币交易所或加载策略。

- 支持沪深 A 股、主要指数、场内 ETF 的 1、5、15、30 分钟、1 小时、日 K、周 K、月 K，默认最近 320 根；接口上限 500 根。
- 最新价、涨跌幅、真实 OHLCV、成交量、行情时间与实际来源；北京时间、不复权、红涨绿跌。
- 股票成交量为股，ETF 为份，指数保留供应商汇总手数。分钟成交量与当日累计快照独立。
- 支持缩放、平移和十字光标。自动更新保留缩放，当前 K 线可覆盖更新，非交易时间不补虚拟 K 线。
- 连续交易时段快照约 5 秒、K 线约 15 秒；午休、收盘、周末约 60 秒检查一次。上一轮未完成不叠加轮询，隐藏标签页暂停自动请求，退出 A 股清理请求与定时器。
- 没有完整节假日交易日历，状态根据北京时间星期和时段，停牌、休市以行情时间为准。

## 行情源与缓存

搜索使用有效本地缓存，缺少结果时调用东方财富 suggest；完整代码也可直接校验。快照顺序为腾讯 → 新浪 → 最近成功缓存，失败时明确显示过期状态。K 线优先腾讯，5、15、30 分钟、1 小时、日 K 支持新浪备用；1 分钟、周 K、月 K 仅腾讯。

在图表上方分别选择 **K 线数据源** 和 **报价数据源**：自动、腾讯、新浪。状态条分别显示实际来源；手动指定时仅使用指定源及其缓存，失败不自动换源。切到新浪时，若当前周期不支持，会提示并切到 5 分钟，周期列表随来源调整。指数成交量单位会归一化，但各源统计范围可能不同，切源后成交量数值不保证一致。

**东财 K 线备用尚未启用。** 当前 Docker 网络访问 `push2his.eastmoney.com` 多次被断开连接，适配器与固定样本测试已保留，恢复可达后须按股票 / 指数 / ETF、1 分钟 / 日 K、不复权口径及单位重新验证。不能把保留的适配器算作可用容灾。BaoStock、AKShare、mootdx、Tushare 和聚源未接入默认链。

已有 HTTPX 依赖承担第三方请求，无需新增行情包。按供应商限流和复用连接；临时网络错误最多重试一次，排队和重试计入单源预算。连续失败冷却约 30 秒。上游故障保留最后成功数据，不会将整个 Bot 标记离线。

内存缓存有容量限制，重启清空：搜索约 6 小时、快照约 3 秒、当前 K 线约 8 秒，过去交易日分钟 / 日 K 单独缓存约 6 小时。周 K / 月 K 刷新整个窗口，替换尚未结束的周期，避免日期变化后重复。缓存区分来源、市场、标的、周期、窗口、复权和结构版本；相同并发请求会合并，手动和自动请求不共享切源决策。没有新增持久行情仓库。

所有 A 股接口沿用现有 Basic / JWT 认证：

```text
GET /api/v1/ashare/capabilities
GET /api/v1/ashare/search?q=000001
GET /api/v1/ashare/quote?symbol=600519.SH&source=sina
GET /api/v1/ashare/candles?symbol=600519.SH&timeframe=30m&limit=320&source=sina
```

## 开发与验证

后端修改 Python 后执行 `docker compose restart backend`。依赖变更需要重新构建镜像；editable 安装使用 `--no-deps`。前端源码绑定挂载，Vite 自动刷新；Node 依赖保存在 Docker 卷中。

```powershell
docker compose exec backend python /workspace/scripts/check_environment.py
docker compose exec backend pytest tests/data/test_ashare.py tests/rpc/test_api_ashare.py -q
docker compose exec frontend pnpm run typecheck
docker compose exec frontend pnpm run build
docker compose exec frontend pnpm test:unit --run
```

2026-11-02 已完成前端 64 项单元 / 组件测试、后端相关 52 项测试、类型检查、构建及修改文件静态检查。浏览器验证了股票 / 指数 / ETF、分钟 / 日 K、快速切换、故障保留旧数据、刷新保留缩放、退出停止轮询和手机宽度。三个验收标的在主源可控失败后也通过真实新浪快照联调。观察到了盘中到 15:00 的更新，但没有测量相对交易所直连的延迟或验证多交易日稳定性。

同日扩展周期与手动选源后，前端累计 68 项测试、后端相关 93 项测试通过，类型检查、构建和修改文件静态检查通过。股票 / 指数 / ETF 分别验证了腾讯 8 个周期与新浪 5 个周期；手动选源、来源状态、不支持周期切换、失败保留所选源数据及缩放保持已在浏览器验证。手动改选周期后会清除此前的自动切换提示。新浪指数快照按沪深市场分别转换成交量，K 线统一从股转换为汇总手数；深市指数的不同源统计范围仍可能不同。

北交所、B 股、可转债、场外基金和 A 股下单 / 回测 / 策略不在当前范围。不要把 `user_data`、密码、日志或行情结果同步到 Git。
