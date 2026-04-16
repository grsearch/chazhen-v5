#!/bin/bash
# 一键安装 / 更新脚本
# 用法：bash install.sh
# 数据目录 /opt/chazhen4/data/ 永远不会被覆盖

set -e
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

INSTALL_DIR=/opt/chazhen4
DATA_DIR=$INSTALL_DIR/data

echo -e "${GREEN}[1/5] 安装 Python 依赖...${NC}"
pip3 install flask flask-cors aiohttp websockets --break-system-packages -q \
  || pip3 install flask flask-cors aiohttp websockets -q

echo -e "${GREEN}[2/5] 复制代码文件...${NC}"
mkdir -p $INSTALL_DIR/live
mkdir -p $DATA_DIR   # 确保数据目录存在（首次安装创建，后续什么都不做）

# 只更新代码文件，data/ 目录完全不碰
cp -r ./live/config.py  $INSTALL_DIR/live/
cp -r ./live/binance.py $INSTALL_DIR/live/
cp -r ./live/bot.py     $INSTALL_DIR/live/
cp -r ./live/engine.py  $INSTALL_DIR/live/
cp -r ./live/server.py  $INSTALL_DIR/live/
cp -r ./live/requirements.txt $INSTALL_DIR/live/
cp ./dashboard.html $INSTALL_DIR/

echo -e "${YELLOW}  数据目录 $DATA_DIR 已保留（不覆盖）${NC}"

echo -e "${GREEN}[3/5] 安装 systemd 服务...${NC}"
cp ./chazhen.service /etc/systemd/system/chazhen.service
systemctl daemon-reload
systemctl enable chazhen.service

echo -e "${GREEN}[4/5] 重启服务...${NC}"
systemctl restart chazhen.service
sleep 2

echo -e "${GREEN}[5/5] 检查状态...${NC}"
if systemctl is-active --quiet chazhen.service; then
  echo -e "${GREEN}✅ 服务已启动！${NC}"
  echo ""
  echo "  Dashboard : http://$(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_IP'):8888"
  echo "  数据目录  : $DATA_DIR"
  echo "    config.json  — 配置（API Key、仓位等）"
  echo "    state.json   — 运行状态（监控列表、PnL）"
  echo "    trades.json  — 全部历史交易"
  echo "    bot.log      — 运行日志"
  echo ""
  echo "  查看日志  : journalctl -u chazhen -f"
  echo "  停止服务  : systemctl stop chazhen"
  echo "  重启服务  : systemctl restart chazhen"
  echo "  备份数据  : cp -r $DATA_DIR ~/chazhen_backup_\$(date +%Y%m%d)"
else
  echo -e "${RED}❌ 服务启动失败，查看日志：${NC}"
  journalctl -u chazhen.service -n 40 --no-pager
fi
