# Personal XMLTV guide

A small XMLTV guide rebuilt daily by GitHub Actions and served via GitHub Pages
at `epg.xml.gz`. Playlist credentials are supplied only through the
`PLAYLIST_INI` repository secret (same format as `secrets/playlist.txt`) and are
never stored in this repo.

- `config.json` - groups to include, guide sources, time window
- `aliases.csv` - manual channel-name to guide-id mappings
- `watchlist.txt` - optional: restrict output to these channels/groups
- `python build_epg.py` - run locally (needs `secrets/playlist.txt`)
