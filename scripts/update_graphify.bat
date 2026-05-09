@echo off
REM Update the Graphify knowledge graph for the Ava repo, then mirror the
REM queryable artifacts into Claude Code's external memory vault.
REM
REM Run after significant code changes. Manual invocation; can also be
REM wired into a git post-commit hook via:
REM   graphify hook install
REM
REM Output:
REM   D:\AvaAgentv2\graphify-out\graph.json   (gitignored — local cache + build artifact)
REM   D:\ClaudeCodeMemory\graphify\ava-agent-v2\graph.json   (vault mirror, queryable)

setlocal
echo [graphify] running AST extraction on D:\AvaAgentv2 ...
graphify update D:\AvaAgentv2
if errorlevel 1 (
  echo [graphify] AST extraction failed
  exit /b 1
)

echo [graphify] mirroring graph.json + GRAPH_REPORT.md to vault ...
copy /Y "D:\AvaAgentv2\graphify-out\graph.json"        "D:\ClaudeCodeMemory\graphify\ava-agent-v2\graph.json"        >nul
copy /Y "D:\AvaAgentv2\graphify-out\graph.html"        "D:\ClaudeCodeMemory\graphify\ava-agent-v2\graph.html"        >nul
copy /Y "D:\AvaAgentv2\graphify-out\GRAPH_REPORT.md"   "D:\ClaudeCodeMemory\graphify\ava-agent-v2\GRAPH_REPORT.md"   >nul
echo [graphify] done
endlocal
