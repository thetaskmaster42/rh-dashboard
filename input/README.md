# input/

Drop your Robinhood statement CSV exports here, then run `./rh-dashboard
build`. Every `*.csv` in this folder is read.

`*.csv` in this folder is gitignored (see `../.gitignore`) — this is real
account activity and shouldn't end up in version control. Only this
placeholder file is tracked, so the folder exists after a fresh clone.

Want to see the tool work first without your own data? Point it at the
bundled fixtures instead:

```bash
./rh-dashboard build -i sample_data -o /tmp/preview
```
