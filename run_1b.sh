#!/bin/bash
# 便捷腳本：自動激活虛擬環境並執行 Step 1b

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 激活虛擬環境
source venv/bin/activate

# 解析參數
WORKERS=${1:-4}  # 預設 4 worker，可由第一個參數覆蓋

echo "🚀 啟動 Layer 1b 籌碼資料下載"
echo "   並行度：$WORKERS"
echo ""

# 執行下載
python run.py --step 1b --force --workers "$WORKERS"
