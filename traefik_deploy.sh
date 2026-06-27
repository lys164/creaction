#!/bin/bash
set -e

# ========== 部署配置 ==========
SERVER="${DEPLOY_HOST:-ecs@14.103.95.77}"
REMOTE_PATH="${DEPLOY_PATH:-/home/ecs/popop-pipeline}"

# 域名配置（仅内网域名，配置后立即可用）
INTERNAL_DOMAIN="popop-pipeline.internal-app.imaginewithu.com"
PUBLIC_DOMAIN=""

IMAGE_NAME="popop-pipeline:latest"
TAR_NAME="popop-pipeline-image.tar.gz"

echo "=========================================="
echo "POPOP Pipeline Traefik 一键部署"
echo "=========================================="
echo "  服务器:   $SERVER"
echo "  远程路径: $REMOTE_PATH"
echo "  内网域名: $INTERNAL_DOMAIN"
echo ""

# 步骤 1: 构建镜像
echo "步骤 1/6: 构建 Docker 镜像..."
docker build --platform linux/amd64 -t "$IMAGE_NAME" .

# 步骤 2: 保存镜像
echo "步骤 2/6: 保存镜像..."
docker save "$IMAGE_NAME" | gzip > "$TAR_NAME"

# 步骤 3: 上传镜像与编排文件（不上传 data/，服务器上的数据卷需保留）
echo "步骤 3/6: 上传到服务器..."
ssh "$SERVER" "mkdir -p $REMOTE_PATH"
scp "$TAR_NAME" "$SERVER:$REMOTE_PATH/"
scp docker-compose.yml "$SERVER:$REMOTE_PATH/"
# 如本地有 .env 则一并上传（首次部署用），已存在则不覆盖
if [ -f .env ]; then
  ssh "$SERVER" "test -f $REMOTE_PATH/.env" || scp .env "$SERVER:$REMOTE_PATH/"
fi

# 步骤 4: 加载镜像
echo "步骤 4/6: 服务器加载镜像..."
ssh "$SERVER" "cd $REMOTE_PATH && gunzip -c $TAR_NAME | docker load"

# 步骤 5: 部署
echo "步骤 5/6: 重启容器..."
ssh "$SERVER" "cd $REMOTE_PATH && docker compose down 2>/dev/null || true && docker compose up -d && sleep 3 && docker compose ps"

# 步骤 6: 清理
echo "步骤 6/6: 清理本地临时文件..."
rm -f "$TAR_NAME"

echo ""
echo "✅ 部署完成！"
echo "内网访问地址: http://$INTERNAL_DOMAIN/"
