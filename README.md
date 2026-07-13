# Trading platform — setup guide

This assumes zero prior coding experience. Follow it top to bottom once; after that it's the same handful of commands every time.

## 1. Install the tools (one-time)

1. **Python** — download from python.org (3.11 or newer). On Windows, check "Add Python to PATH" during install.
2. **Git** — download from git-scm.com. This is what tracks changes and talks to GitHub.
3. **Docker Desktop** — download from docker.com. This runs Postgres for you without installing a database by hand.
4. **VS Code** (recommended editor) — download from code.visualstudio.com.
5. **A GitHub account** — sign up at github.com if you don't have one.

## 2. Get the project running

Open a terminal (VS Code has one built in: Terminal → New Terminal) and run:

```bash
cd trading_platform          # go into the project folder
python -m venv .venv         # create an isolated Python environment
source .venv/bin/activate    # activate it (Windows: .venv\Scripts\activate)
pip install -e ".[dev]"      # install all dependencies
docker compose up -d         # start Postgres in the background
```

You now have everything installed and a database running locally.

## 3. Run the tests

```bash
pytest -v
```

You should see a list of tests with `PASSED` next to each. This is how you check "did I break anything" after any change — run this command, all green means safe.

## 4. Save your work with Git + GitHub

Do this once per project:

```bash
git init                                   # start tracking this folder
git add .                                  # stage all files
git commit -m "Phase 1: strategy engine, feature registry, tests"
```

Then create an empty repository on github.com (click the "+" top right → "New repository", don't initialize it with a README), and it will show you two lines like:

```bash
git remote add origin https://github.com/YOUR_USERNAME/trading-platform.git
git branch -M main
git push -u origin main
```

Run those. From then on, whenever you want to save progress:

```bash
git add .
git commit -m "describe what changed"
git push
```

That's the entire loop: write code → run `pytest` → if green, `git add . && git commit -m "..." && git push`.

## 5. A much easier path

Given you're starting from zero, honestly consider doing this project inside **Claude Code** (Anthropic's coding tool) rather than copy-pasting files by hand from this chat. It runs in your terminal or as a desktop app, can create/edit/test files directly in a real project folder, run the commands above for you, and handle git commits — you'd mostly just describe what you want in plain English and review the results. It removes almost all of the "where do I even type this" friction this guide is trying to solve.
