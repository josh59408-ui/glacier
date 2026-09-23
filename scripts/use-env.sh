#!/usr/bin/env bash
# 本機多環境切換工具：切換「應用設定(.env) + GitHub 目標 repo」
# （同一個 GitHub 帳號、不同 repo 的情境，不需切換 token/帳號）
#
# 用法（務必用 source，不能直接執行，否則設定不會留在當前 shell）：
#   source scripts/use-env.sh <env名稱>
#   例：  source scripts/use-env.sh self      → 讀 .env.self，操作你自己的 repo
#         source scripts/use-env.sh client    → 讀 .env.client，操作甲方開的 repo
#
# 各環境設定放在專案根目錄的 .env.<名稱>（已被 .gitignore 擋住，不會上 git）。
# 每個 .env.<名稱> 內除了應用設定，可額外放 GitHub 區塊：
#   GH_REPO=owner/repo     # 此環境的目標 repo（gh 指令會自動對它操作）
#   GIT_REMOTE=origin      # git push 用的 remote 名稱（你的=origin，甲方的=client）

_envname="${1:-}"
if [ -z "$_envname" ]; then
  echo "用法: source scripts/use-env.sh <env名稱>   (例: self / client)"
  return 2 2>/dev/null || exit 2
fi

_src="${BASH_SOURCE[0]:-$0}"
_root="$(cd "$(dirname "$_src")/.." && pwd)"
_envfile="$_root/.env.$_envname"

if [ ! -f "$_envfile" ]; then
  echo "❌ 找不到 $_envfile"
  echo "   請先建立：cp .env.example .env.$_envname  然後填入該環境的值"
  return 1 2>/dev/null || exit 1
fi

# 載入該環境所有變數到當前 shell
set -a
# shellcheck disable=SC1090
source "$_envfile"
set +a
export APP_ENV="$_envname"

echo "✅ 已切換到環境 [$_envname]"
echo "   APP_ENV=$APP_ENV  →  Python 會自動讀 .env.$_envname"

# GitHub：同帳號免切認證，只需把 gh 的預設目標 repo 指向此環境
if [ -n "${GH_REPO:-}" ]; then
  export GH_REPO
  echo "   gh 目標 repo = $GH_REPO   （之後 gh run list / gh workflow run 都自動對它）"
fi

# git push 目標 remote 提示
_remote="${GIT_REMOTE:-origin}"
if git -C "$_root" remote get-url "$_remote" >/dev/null 2>&1; then
  echo "   git push 用： git push $_remote main   （→ $(git -C "$_root" remote get-url "$_remote")）"
else
  echo "   ⚠️  尚未設定名為 '$_remote' 的 git remote"
  [ "$_remote" != "origin" ] && echo "      新增方式： git remote add $_remote https://github.com/甲方帳號/repo.git"
fi
