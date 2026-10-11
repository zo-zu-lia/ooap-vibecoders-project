#!/usr/bin/env python3
"""Install the local 12-hour recovery-set timer for the checkout owner."""
import json
import os
from pathlib import Path
import pwd
import subprocess

root=Path(__file__).resolve().parents[1]
python=root/'.venv/bin/python'
if not python.is_file(): raise SystemExit('Create .venv and install requirements-dev.txt first.')
user=pwd.getpwuid(os.getuid()).pw_name
service=f'''[Unit]
Description=FileFlow encrypted database and object recovery set
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User={user}
WorkingDirectory={json.dumps(str(root))}
ExecStart={json.dumps(str(python))} {json.dumps(str(root/'scripts/backup.py'))} create
Nice=10
MemoryMax=512M
TimeoutStartSec=1h
'''
timer='''[Unit]
Description=FileFlow recovery set every 12 hours

[Timer]
OnCalendar=*-*-* 00,12:00:00 UTC
RandomizedDelaySec=5m
Persistent=true
Unit=fileflow-backup.service

[Install]
WantedBy=timers.target
'''
for name,text in [('fileflow-backup.service',service),('fileflow-backup.timer',timer)]:
    path=root/'.local'/name
    path.write_text(text)
    subprocess.run(['sudo','install','-m','644',str(path),'/etc/systemd/system/'+name],check=True)
subprocess.run(['sudo','systemctl','daemon-reload'],check=True)
subprocess.run(['sudo','systemctl','enable','--now','fileflow-backup.timer'],check=True)
print('Enabled FileFlow backup timer every 12 hours. Archives: backups/; key: .local/backup.key')
