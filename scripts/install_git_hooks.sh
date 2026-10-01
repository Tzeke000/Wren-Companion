#!/bin/sh
# Install the repo's git hooks (round-2 fix 3.13, 2026-10-01). Idempotent. Run from Git Bash:
#   sh scripts/install_git_hooks.sh
top="$(git rev-parse --show-toplevel)"
hook="$top/.git/hooks/pre-commit"
cat > "$hook" <<'HOOK'
#!/bin/sh
# pre-commit: compile every STAGED .py before the commit lands (scripts/precommit_pycompile.py)
top="$(git rev-parse --show-toplevel)"
exec "$top/.venv/Scripts/python.exe" "$top/scripts/precommit_pycompile.py"
HOOK
chmod +x "$hook"
echo "installed $hook"
