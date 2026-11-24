# Encrypted Yandex Disk backups

This optional systemd job takes consistent SQLite snapshots without stopping
the portal, encrypts them with `age`, uploads to a specific Yandex Disk folder,
and checks the stored file's length and MD5 through the official API. It does
not delete old backups or perform an automatic restore. Snapshots cover the
auth and vault databases, scoped MCP keys, calculator and action databases,
and marketplace history when present. The encrypted archive also contains the
vault and session keys, and the history key if present. No standalone SMTP or
Yandex OAuth credential files are included. The vault snapshot can contain the
encrypted Yandex OAuth token, which is recoverable with the archived vault key;
the entire archive is protected by the independent `age` recipient.

The destination may be public: only encrypted files are uploaded. Keep the
recipient's private key independently of both the VPS and the public folder.
Loss of that key makes these backups unreadable. The copy's creation time and
size remain visible to people who can see the folder.

Installation requires the official Ubuntu `age` and `python3-cryptography`
packages and a dedicated Yandex OAuth application with Disk read and write
permissions. The administrator enters the public folder link, the matching
Disk path, and the OAuth token in the portal. The token is encrypted in the
existing vault; it is never echoed, committed, or passed in arguments. Before
each upload, the job checks that the authenticated folder has the same
resource ID as the public link. The link must remain published for this check.
Put a public age recipient in a root-owned file. Create a root-owned `0600`
JSON file at `/etc/wbozon-yandex-backup.json` with these fields:

```json
{
  "recipient_file": "/etc/wbozon-yandex-backup/recipients.txt",
  "data_root": "/var/lib/private/3dprinta-portal",
  "credentials": "/etc/3dprinta-portal/credentials",
  "state_dir": "/var/lib/wbozon-yandex-backup",
  "runtime_dir": "/run/wbozon-yandex-backup",
  "deploy_lock": "/var/lib/3dprinta-native-deployer/deploy.lock"
}
```

Install `backup.py` at `/usr/local/libexec/wbozon-yandex-backup/backup.py`
and the included systemd files in `/etc/systemd/system`. Configure and enable
backups in the portal, run the service once, verify `uploaded_and_verified`
in the journal, then enable the timer. The timer runs daily at 04:30 Moscow time
with a randomized delay of up to 10 minutes. A failed or uncertain upload
leaves the encrypted local file for investigation and is never reported as a
verified backup. Temporary plaintext snapshots use `/run` and disappear when
the unit stops. No backup is made if there is insufficient temporary space.

The script briefly locks SQLite writers across the discovered databases while
creating snapshots and serializes against the deployment controller. A busy
database or deployment causes the job to fail closed; it does not restart the
portal. Schema, network, and permission changes can cause future runs to fail,
so monitor the systemd journal and regularly verify backup accessibility.
