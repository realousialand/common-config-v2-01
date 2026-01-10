name: Daily Email Scanner

on:
  schedule:
    # 北京时间 7, 11, 18, 24 点
    - cron: '0 3,10,16,23 * * *'
  workflow_dispatch:

permissions:
  contents: write

jobs:
  scan_and_report:
    runs-on: ubuntu-latest
    
    steps:
      - name: Checkout code
        uses: actions/checkout@v3

      # 🟢 第一步：安装并启动 Cloudflare WARP (黑魔法)
      # 这会改变运行环境的出口 IP，试图绕过 403/五秒盾
      - name: Set up Cloudflare WARP
        uses: fscarmen/warp-on-actions@v1.3
        with:
          stack: dual  # 启用双栈 (IPv4 + IPv6)

      - name: Check New IP (Optional)
        run: |
          echo "查看当前伪装后的 IP："
          curl -s https://ip.gs

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.9'
          cache: 'pip'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Syntax Check
        run: python -m py_compile src/email_bot.py

      - name: Run Email Bot
        env:
          EMAIL_USER: ${{ secrets.EMAIL_USER }}
          EMAIL_PASS: ${{ secrets.EMAIL_PASS }}
          LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
          LLM_MODEL_NAME: ${{ secrets.LLM_MODEL_NAME }}
          PYTHONUNBUFFERED: "1"
        run: python src/email_bot.py

      - name: Commit and Push Data
        run: |
          git config --global user.name 'Paper-Bot-Action'
          git config --global user.email 'action@github.com'
          git pull origin main || echo "No remote changes"
          git add data/*.json || echo "No data files found"
          if [ -n "$(git status --porcelain)" ]; then
            git commit -m "📝 Update bot history & queue [skip ci]"
            git push
            echo "✅ Data pushed to repository."
          else
            echo "☕ No changes to commit."
          fi
