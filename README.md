# Ubuntu Backup APT repository (retired)

The debs are now published from the Launchpad PPA:

```bash
sudo add-apt-repository ppa:toomanypatterns/ubuntu-backup
sudo apt update
sudo apt install ubuntu-backup
```

- PPA key fingerprint: `DD690D4A9810E29DE3824FF2FBD3F68940827DC3`
- Source: <https://github.com/scripting-drafts/Ubuntu-Backup>

The `pool/` and `dists/` trees here are a historical archive and no longer
receive updates. If `/etc/apt/sources.list.d/ubuntu-backup.list` still points
at this site, remove it.
