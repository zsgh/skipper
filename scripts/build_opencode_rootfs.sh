#!/bin/bash
# 构建带 opencode 的 berth rootfs（Linux + root 权限）。
#
# berth 通过「BERTH_ROOTFS 的同级 rootfs-* 目录」自动发现镜像：
#   /opt/berth/rootfs-opencode  →  image 名 "opencode"
#
# 用法：
#   sudo scripts/build_opencode_rootfs.sh [ROOTFS] [SUITE]
# 默认：
#   ROOTFS=/opt/berth/rootfs-opencode  SUITE=bookworm
#
# macOS（Lima VM 里跑 berth）：
#   limactl shell <vm> -- sudo bash /Users/<you>/github/skipper/scripts/build_opencode_rootfs.sh
#
# 注意：opencode 的 API key / provider 配置 **不写进 rootfs**，
# 由 OpencodeDriver 在运行时通过环境变量 + OPENCODE_CONFIG_CONTENT 注入。
set -euo pipefail

ROOTFS="${1:-${ROOTFS:-/opt/berth/rootfs-opencode}}"
SUITE="${2:-${SUITE:-bookworm}}"
MIRROR="${MIRROR:-http://deb.debian.org/debian}"
NODE_MAJOR="${NODE_MAJOR:-20}"
OPENCODE_NPM_PKG="${OPENCODE_NPM_PKG:-opencode-ai@latest}"

if [ "$(id -u)" -ne 0 ]; then
    echo "需要 root：sudo $0" >&2
    exit 1
fi

if [ ! -e "$ROOTFS" ]; then
    echo "==> debootstrap $SUITE → $ROOTFS"
    debootstrap --variant=minbase --include=ca-certificates "$SUITE" "$ROOTFS" "$MIRROR"
fi

echo "==> 拷贝 DNS 配置进 chroot"
cp /etc/resolv.conf "$ROOTFS/etc/resolv.conf"

# npm 依赖 /proc 做 libc 检测（少了它会误判成 musl，装错 opencode 的发行版）
cleanup_mounts() {
    umount "$ROOTFS/proc" 2>/dev/null || true
    umount "$ROOTFS/sys" 2>/dev/null || true
}
trap cleanup_mounts EXIT
mount -t proc proc "$ROOTFS/proc"
mount -t sysfs sys "$ROOTFS/sys"

echo "==> chroot 里安装 python3 / git / node${NODE_MAJOR} / opencode"
cat > "$ROOTFS/tmp/setup-opencode.sh" <<EOS
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends python3 curl git ca-certificates gnupg

# bookworm 自带 node 18，opencode 需要 node >= 20
curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
apt-get install -y nodejs

npm install -g "${OPENCODE_NPM_PKG}"

echo "node: \$(node --version)"
echo "npm:  \$(npm --version)"
echo "opencode: \$(opencode --version)"
EOS

chroot "$ROOTFS" /bin/bash /tmp/setup-opencode.sh
rm -f "$ROOTFS/tmp/setup-opencode.sh"
cleanup_mounts

echo "==> 完成：$ROOTFS（berth image 名: opencode）"
echo "验证："
echo "  curl -s -X POST http://localhost:9000/scripts/run -H 'Content-Type: application/json' \\"
echo "    -d '{\"cmd\":[\"/usr/bin/opencode\",\"--version\"],\"image\":\"opencode\",\"task_id\":\"opencode-smoke\"}'"
