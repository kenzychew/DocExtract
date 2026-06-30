$prompt = @'
Read PROGRESS.md, CLAUDE.md, and the specs in docs/. Do the NEXT unchecked task in the TONIGHT section only - exactly one. Implement it per docs/05_build_plan.md. Run that task's Check command and paste the output. If it passes: commit just that task's changes with its Commit message, then tick its box in PROGRESS.md and commit that. If it fails after reasonable attempts: add a one-line note under BLOCKED in PROGRESS.md, commit, and move on. NEVER start a TOMORROW task. NEVER add a dependency outside task N1's night set. NEVER edit files in docs/.
'@

for ($i = 1; $i -le 20; $i++) {
  Write-Host "=== iteration $i ==="
  claude -p $prompt `
    --model sonnet `
    --allowedTools "Read,Edit,Write,Bash,PowerShell" `
    --permission-mode acceptEdits `
    --max-turns 30 --max-budget-usd 0.50 `
    --output-format json | Tee-Object "run_$i.json"

  # Completion = no unchecked TONIGHT boxes before the TOMORROW section
  $tonight = ((Get-Content PROGRESS.md -Raw) -split '## TOMORROW')[0]
  if ($tonight -notmatch '- \[ \]') { Write-Host "All TONIGHT tasks complete."; break }
}