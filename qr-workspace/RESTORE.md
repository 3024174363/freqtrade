# 恢复 QR 开发环境

`freqtrade/` 和 `frequi/` 是两个独立 Git 仓库，均使用 `qr-dev` 分支。QR 根目录本身没有 Git 仓库；本目录保存根目录的运行文件副本，便于从 Git 恢复开发环境。

## 目录位置

把两个 Fork 的 `qr-dev` 分支克隆为同一父目录中的 `freqtrade` 和 `frequi`：

```text
QR/
  freqtrade/                 后端仓库
    qr-workspace/           本目录
  frequi/                    前端仓库
```

在 QR 根目录执行以下 PowerShell 命令，将保存的环境文件复制到运行位置：

```powershell
$qrSnapshot = Join-Path $PWD 'freqtrade/qr-workspace'
Copy-Item "$qrSnapshot/compose.yaml" .
Copy-Item "$qrSnapshot/start.ps1" .
Copy-Item "$qrSnapshot/stop.ps1" .
Copy-Item "$qrSnapshot/.dockerignore" .
Copy-Item "$qrSnapshot/.gitignore" .
Copy-Item "$qrSnapshot/README.md" .
Copy-Item "$qrSnapshot/docker" . -Recurse
Copy-Item "$qrSnapshot/scripts" . -Recurse
```

如果运行位置已有自己的文件，先比较差异再复制。更新运行文件后，也应把对应副本同步回本目录并提交。

## 首次启动

安装 Python 和 Docker Desktop，并启动 Docker Desktop。在 QR 根目录执行：

```powershell
python scripts/initialize.py
Copy-Item freqtrade/freqtrade/templates/sample_strategy.py user_data/strategies/SampleStrategy.py
.\start.ps1
```

初始化会生成本机专用的研究配置及随机登录密码；现有 `user_data/config.json` 不会被覆盖。账号密码在 `user_data/local-access.txt`，不保存在本目录。首次恢复没有历史行情或回测结果，可使用 Chart 的 A 股模式从网络读取实时行情。

- 唯一页面入口：http://127.0.0.1:5173
- 后端 API：http://127.0.0.1:8090/api/v1
- 文档：http://127.0.0.1:8090/docs

使用方式、支持的 A 股范围和当前上游限制见 [README](README.md)。环境文件不包含本机认证配置、交易所密钥、行情数据、日志、浏览器状态或截图。
