# CLAUDE.md

## What this is
A personal XMLTV (EPG) guide for an Xtream-codes IPTV playlist watched in TiviMate on a
Fire TV. A GitHub Actions job rebuilds the guide daily and publishes it with GitHub Pages
as `epg.xml.gz` (about 2 MB). No always-on PC is needed. TiviMate is pointed at the Pages URL
(Settings > Playlists > EPG sources > custom source).

The repo is **public**. Only code, config and the generated guide are in it. The playlist
login lives in a GitHub secret and in a git-ignored local file.

## Rules (read first)
- **Never print, log, echo, commit or paste credentials**: the playlist server, username,
  password, or any URL containing them. This includes tracebacks, debug output and test data.
- Never read `secrets/` with the Read tool (blocked by `.claude/settings.json`). Scripts may
  open `secrets/playlist.txt`, but must print only counts, percentages and group names.
- `build_epg.py` catches errors and prints only the exception type, because urllib errors can
  contain the URL. Keep it that way; don't add `traceback.print_exc()` or print raw exceptions.
- Stage files explicitly (`git add <file>`), never `git add .` / `-A`.
- Before committing, scan changed files for the server host, username and password
  (compare against `secrets/playlist.txt` programmatically and print only yes/no).
- Commit with a generic identity, not a personal one; keep personal email and account details
  out of every file.

## How the pipeline works (`build_epg.py`, stdlib only)
1. **Playlist**: reads credentials from env `PLAYLIST_INI` (CI) or `secrets/playlist.txt`
   (local), then pulls live categories and streams from the Xtream `player_api.php`.
   Only channels in the groups listed in `config.json` are kept (plus provider-only PK/INR).
2. **Keys**: playlist channels are grouped into output channels by `epg_channel_id`
   (or a slug of the name when empty).
3. **Sources**: downloads the provider's own `xmltv.php` plus public XMLTV files listed in
   `config.json` (epgshare01, epg.pw, open-epg). Downloads are cached in `.cache/` for 12 h.
4. **Matching**, per source and only within the channel's region, first hit wins:
   exact id, id stem, `aliases.csv`, normalized name, loose name, fuzzy (difflib >= 0.9 with
   identical digits). Fuzzy is disabled for the Arab/beIN region (aliases only).
5. **Programmes**: filtered to the window (`hours_back` .. `days_ahead`), converted to UTC
   using each timestamp's offset (naive timestamps are assumed UTC unless a source sets
   `naive_tz_hours`), merged across sources in priority order without overlapping time slots,
   with descriptions truncated.
6. **Output**: `out/epg.xml.gz` and `out/index.html`. Local audit files go to `reports/`
   (`mapping.csv`, `unmatched.csv`); these contain channel names only.
7. **CI** (`.github/workflows/epg.yml`): daily 03:17 UTC + manual run. Jobs: `build` (runs the
   script with the secret, uploads `out/`), `deploy` (GitHub Pages), `keepalive` (empty bot
   commit if the repo has been quiet for 45+ days, so GitHub doesn't disable the schedule).

## File map
| Path | Purpose |
|---|---|
| `build_epg.py` | The whole pipeline |
| `config.json` | Included playlist groups (by exact title), sources + priorities, time window |
| `aliases.csv` | Manual playlist-name to guide-channel mappings |
| `watchlist.txt` | Optional filter to shrink the output to favourite channels |
| `.github/workflows/epg.yml` | Daily build, Pages deploy, keep-alive |
| `secrets/playlist.txt` | **Git-ignored.** Local credentials (see format below) |
| `.cache/`, `out/`, `reports/` | Git-ignored build artefacts |

## Credentials file format (`secrets/playlist.txt` and the `PLAYLIST_INI` secret)
INI-style; the first section with a `server` key is used:
```
[name]
server=<host or http://host:port>
username=<user>
password=<pass>
```
The GitHub repository secret is named **`PLAYLIST_INI`** and holds the same text. To update it,
pipe the file straight into `gh secret set PLAYLIST_INI --repo <owner>/<repo>` from a script
that prints nothing.

## Run locally
```
python build_epg.py              # uses secrets/playlist.txt, caches downloads for 12 h
python build_epg.py --no-fetch   # cache only, no network except the playlist API
```
Prints per-group coverage. A full cold run takes a few minutes (one guide is ~200 MB).
Check `reports/unmatched.csv` for channels without a guide and `reports/mapping.csv` to audit
matches (look at the `fuzzy` method first).

## Editing `watchlist.txt`
Empty (or comments only) means publish every channel in the configured groups. Otherwise only
matching channels are published. One entry per line:
- `Sky Sports Main Event`: channel name substring, case-insensitive, matched after normalizing
  (quality tags and country prefixes ignored)
- `group: UK | Sky Sports`: keep a whole playlist group (exact group title)
- `# comment`

## Editing `aliases.csv`
`playlist_name_regex,guide_channel_id_or_name`. The regex must match the whole raw playlist
channel name (case-insensitive); `\1`, `\2` expand regex groups into the target. The target is
a guide channel id or display name, looked up only in sources of the channel's region. Lines
starting with `#` are comments. Example:
```
BEIN SPORTS? ([1-7])(?: HD)?,beIN_SPORTS\1_DIGITAL_Mono_EN.bein
```
Workflow: run the build, open `reports/unmatched.csv`, find the guide name/id in the source
file, add an alias, rebuild, confirm coverage rose and `mapping.csv` shows `alias`.

## Changing groups or sources
Edit `config.json`. Group titles must equal the playlist's category names exactly (spacing is
normalized). `provider_only_prefixes` channels are included only when the provider's own guide
has data for them. Source `regions` limit which channel regions may use that file.

## Deploy
Push to `main`. The workflow runs on schedule; trigger it manually with
`gh workflow run epg.yml --repo <owner>/<repo>` or the Actions tab. Pages source is
"GitHub Actions". The keep-alive job pushes occasional empty commits to `main`, so run
`git pull` before pushing local changes.
