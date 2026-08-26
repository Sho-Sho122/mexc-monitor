name: MEXC Hybrid Lab

on:
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: mexc-hybrid-lab
  cancel-in-progress: false

jobs:
  lab:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install packages
        run: pip install -r requirements.txt

      - name: Run MEXC scanner
        run: python mexc_scanner.py

      - name: Run HYBRID LAB
        env:
          DISCORD_LAB_WEBHOOK_URL: ${{ secrets.DISCORD_LAB_WEBHOOK_URL }}
          TZ: Asia/Tokyo
        run: python hybrid_lab.py

      - name: Save LAB state robustly
        shell: bash
        run: |
          set -e

          git config user.name "mexc-lab-bot"
          git config user.email "mexc-lab-bot@users.noreply.github.com"

          mkdir -p /tmp/mexc-lab-save

          for file in \
            hybrid_lab_state.json \
            hybrid_lab_trades.csv \
            hybrid_lab_model_summary.csv
          do
            if [ -f "$file" ]; then
              cp "$file" "/tmp/mexc-lab-save/$file"
            fi
          done

          for attempt in 1 2 3 4 5
          do
            echo "Save attempt $attempt"

            git fetch origin main
            git reset --hard origin/main

            for file in /tmp/mexc-lab-save/*
            do
              [ -e "$file" ] || continue
              cp "$file" .
              git add "$(basename "$file")"
            done

            if git diff --cached --quiet; then
              echo "No LAB state changes"
              exit 0
            fi

            git commit -m "Update HYBRID LAB state"

            if git push origin HEAD:main; then
              echo "LAB state saved"
              exit 0
            fi

            echo "Push raced with another workflow. Retrying..."
            sleep 3
          done

          echo "Could not save LAB state after 5 attempts"
          exit 1
