# bbmp

A minimal terminal music player for MP3 and FLAC files.

```
Track:  Sinnerman                        [--------O-----------]  03:12 / 09:58
Artist: Nina Simone                       > PLAYING  Vol: 75%  [ ]
Album:  Pastel Blues

────────────────────────────── [ LIBRARY ] ───────────────────────────────────
#    TITLE                       ARTIST              ALBUM
1.   Feeling Good                Nina Simone          I Put A Spell On You
2.   Sinnerman                   Nina Simone          Pastel Blues
3.   Black Is The Color          Nina Simone          Nina Simone at Town Hall
```

## Features

- Plays **MP3** and **FLAC** files
- Curses TUI — works in any terminal
- Fuzzy search (`/`) across title, artist, album
- Shuffle, and repeat modes (none / all / one)
- Queue view — see and navigate what's playing next
- Launch with a search query: `bbmp miles davis`
- 5 colour themes (Native, Magma, Ocean, Lime, Monochrome)
- SQLite metadata cache — fast startup on large libraries
- Persists volume, theme, repeat mode, and last folder

## Requirements

- Python ≥ 3.11
- Linux or macOS (Windows not supported — no `curses`)

## Installation

### pipx (recommended)

```bash
pipx install bbmp
```

### pip

```bash
pip install bbmp
```

### Arch Linux (AUR)

```bash
yay -S bbmp
# or
paru -S bbmp
```

## Usage

```bash
bbmp                        # open ~/Music
bbmp /path/to/music         # open a specific folder
bbmp miles davis            # search and play matching tracks
bbmp kind of blue           # search by album
```

## Keybindings

| Key | Action |
|-----|--------|
| `Space` | Play / Pause |
| `Enter` | Play selected track |
| `↑` / `↓` or `j` / `k` | Navigate list |
| `←` / `→` | Seek ±5 seconds |
| `Shift+←` / `Shift+→` | Previous / Next track |
| `+` / `-` | Volume up / down |
| `/` | Search (type to filter, `Enter` to confirm, `Esc` to cancel) |
| `s` | Toggle shuffle |
| `r` | Cycle repeat mode (off → all → one) |
| `v` | Toggle Library / Queue view |
| `T` | Open theme picker |
| `x` | Quit |

## Files written to `$HOME`

| File | Purpose |
|------|---------|
| `~/.bbmp_config.json` | Volume, theme, repeat mode, last folder |
| `~/.bbmp_cache.db` | SQLite metadata cache |
| `~/.bbmp.log` | Warning/error log |

## License

MIT
