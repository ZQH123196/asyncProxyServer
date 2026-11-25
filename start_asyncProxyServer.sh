#!/usr/bin/env bash
# 启动异步 TTS 代理服务，交给 pm2 托管。
#
# 这是本项目**唯一**的生产入口：装依赖与启动进程都在这里，没有独立的安装脚本。
# 生产环境只允许执行本脚本，不要手工 uv pip install，也不要手工 python main.py。
set -euo pipefail

cd "$(dirname "$0")"

# 装依赖。生产只跑这一个脚本，所以它必须能把环境装成 pyproject.toml 声明的样子。
#
# 用不带 --locked / --frozen 的 uv sync：锁与 pyproject.toml 不一致时，
# uv 先把锁重新对齐再同步，装出来的版本永远等于 pyproject.toml 的声明。
#
# 不用 --locked：它会让「改过 pyproject.toml 但没跑 uv lock」这种仓库内疏忽
# 变成生产启动失败 —— 生产该做的是把服务装起来，不该承担仓库的整理工作。
#
# 不用 --frozen：它不改锁，但按旧锁装，最终装出与 pyproject.toml 不符的版本，
# 且退出码为 0，错得毫无声息。
uv sync --index-url https://pypi.tuna.tsinghua.edu.cn/simple

# 依赖：pm2 与 pm2-logrotate。node 若由 apt 安装、无写权限，需先手动
# sudo npm install -g pm2。
command -v pm2 >/dev/null || npm install -g pm2
pm2 describe pm2-logrotate >/dev/null 2>&1 || pm2 install pm2-logrotate

pm2 delete asyncProxyServer >/dev/null 2>&1 || true
pm2 start main.py --interpreter "$PWD/.venv/bin/python" --name asyncProxyServer --cwd "$PWD"

echo
echo "日志：~/.pm2/logs/asyncProxyServer-out.log    stdout，业务错误也在此文件"
echo "      ~/.pm2/logs/asyncProxyServer-error.log  stderr，Python 崩溃堆栈"
echo
pm2 logs asyncProxyServer
