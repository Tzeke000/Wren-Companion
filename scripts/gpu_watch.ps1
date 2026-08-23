$log = "D:\Wren-Companion\state\gpu_watch.csv"
"ts,pid,name,engine,util" | Out-File -FilePath $log -Encoding utf8
$end = (Get-Date).AddSeconds(300)
while ((Get-Date) -lt $end) {
  $t = (Get-Date).ToString("HH:mm:ss")
  $s = (Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction SilentlyContinue).CounterSamples |
       Where-Object { $_.CookedValue -gt 3 }
  foreach ($c in $s) {
    if ($c.InstanceName -match 'pid_(\d+).*engtype_(\w+)') {
      $p = $Matches[1]; $e = $Matches[2]
      $n = (Get-Process -Id $p -ErrorAction SilentlyContinue).ProcessName
      "$t,$p,$n,$e,$([math]::Round($c.CookedValue,1))" | Add-Content -Path $log -Encoding utf8
    }
  }
  Start-Sleep -Seconds 2
}
"DONE" | Add-Content -Path $log -Encoding utf8
