#!/usr/bin/env python3
import curses
import pygame
import os
import time
import sys
import random
import json
import logging
import threading
import sqlite3
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen import MutagenError

# --- XDG DIRECTORIES ---
def _xdg(env: str, fallback: str) -> str:
    base = os.environ.get(env) or os.path.join(os.path.expanduser("~"), fallback)
    path = os.path.join(base, "bbmp")
    os.makedirs(path, exist_ok=True)
    return path

XDG_CONFIG = _xdg("XDG_CONFIG_HOME", ".config")   # ~/.config/bbmp/
XDG_CACHE  = _xdg("XDG_CACHE_HOME",  ".cache")    # ~/.cache/bbmp/
XDG_DATA   = _xdg("XDG_DATA_HOME",   ".local/share")  # ~/.local/share/bbmp/

# --- LOGGING SETUP ---
LOG_FILE = os.path.join(XDG_DATA, "bbmp.log")
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("bbmp")

# --- UTILITIES ---
def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


# --- 1. CONFIG & THEMES ---
class Config:
    FILE_PATH = os.path.join(XDG_CONFIG, "config.json")

    THEMES = [
        "Native (Terminal Default)",
        "Magma (Warm - Red/Orange)",
        "Ocean (Cold - Blue/Cyan)",
        "Lime (Forest - Green)",
        "Monochrome (High Contrast)",
    ]

    def __init__(self):
        self.theme_index = 0
        self.volume = 0.5
        self.repeat_mode = "none"   # "none" | "all" | "one"
        self.last_folder = os.path.join(os.path.expanduser("~"), "Music")
        self.load()

    def load(self):
        if not os.path.exists(self.FILE_PATH):
            return
        try:
            with open(self.FILE_PATH, "r") as f:
                data = json.load(f)
            self.theme_index = int(data.get("theme_index", 0)) % len(self.THEMES)
            self.volume = max(0.0, min(1.0, float(data.get("volume", 0.5))))
            self.repeat_mode = data.get("repeat_mode", "none")
            self.last_folder = data.get("last_folder", self.last_folder)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            log.warning(f"Could not load config (resetting to defaults): {e}")

    def save(self):
        try:
            with open(self.FILE_PATH, "w") as f:
                json.dump({
                    "theme_index": self.theme_index,
                    "volume": self.volume,
                    "repeat_mode": self.repeat_mode,
                    "last_folder": self.last_folder,
                }, f, indent=2)
        except OSError as e:
            log.warning(f"Could not save config: {e}")

    def next_theme(self):
        self.theme_index = (self.theme_index + 1) % len(self.THEMES)
        self.save()
        self.apply_theme()

    def set_theme(self, index):
        self.theme_index = index % len(self.THEMES)
        self.save()
        self.apply_theme()

    def cycle_repeat(self):
        modes = ["none", "all", "one"]
        idx = modes.index(self.repeat_mode) if self.repeat_mode in modes else 0
        self.repeat_mode = modes[(idx + 1) % len(modes)]
        self.save()

    def apply_theme(self):
        try:
            curses.start_color()
        except curses.error:
            pass
        curses.use_default_colors()

        name = self.THEMES[self.theme_index]

        if "Native" in name:
            main_col, alt_col = curses.COLOR_BLUE, curses.COLOR_GREEN
        elif "Magma" in name:
            main_col, alt_col = curses.COLOR_RED, curses.COLOR_YELLOW
        elif "Ocean" in name:
            main_col, alt_col = curses.COLOR_BLUE, curses.COLOR_CYAN
        elif "Lime" in name:
            main_col, alt_col = curses.COLOR_GREEN, curses.COLOR_GREEN
        elif "Monochrome" in name:
            main_col, alt_col = curses.COLOR_WHITE, curses.COLOR_WHITE
        else:
            main_col, alt_col = curses.COLOR_WHITE, curses.COLOR_WHITE

        curses.init_pair(1, main_col, -1)
        curses.init_pair(2, alt_col, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_WHITE, -1)
        curses.init_pair(5, curses.COLOR_BLACK, main_col)


# --- 2. METADATA CACHE ---
class MetadataCache:
    def __init__(self):
        self.db_path = os.path.join(XDG_CACHE, "metadata.db")
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS tracks (
                    path TEXT PRIMARY KEY,
                    mtime REAL,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    duration REAL
                )
            """)
            self._conn.commit()

    def get(self, path: str) -> dict | None:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT mtime, title, artist, album, duration FROM tracks WHERE path=?",
                (path,)
            ).fetchone()
        if row and abs(row[0] - mtime) < 1.0:
            return {"title": row[1], "artist": row[2], "album": row[3], "duration": row[4]}
        return None

    def set(self, path: str, data: dict):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tracks VALUES (?,?,?,?,?,?)",
                (path, mtime, data["title"], data["artist"], data["album"], data["duration"])
            )
            self._conn.commit()

    def close(self):
        self._conn.close()


_cache = MetadataCache()


# --- 3. DATA MODEL ---
class Track:
    def __init__(self, path: str, filename: str):
        self.path = path
        self.filename = filename
        self.title = filename
        self.artist = "Unknown Artist"
        self.album = "Unknown Album"
        self.duration = 0.0
        self._parse_metadata()

    def _parse_metadata(self):
        cached = _cache.get(self.path)
        if cached:
            self.title = cached["title"]
            self.artist = cached["artist"]
            self.album = cached["album"]
            self.duration = cached["duration"]
            return

        self._read_tags()
        self._read_duration()
        _cache.set(self.path, {
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration": self.duration,
        })

    def _read_tags(self):
        try:
            if self.path.lower().endswith(".flac"):
                tags = FLAC(self.path)
            else:
                tags = EasyID3(self.path)
            if "title" in tags:
                self.title = tags["title"][0]
            if "artist" in tags:
                self.artist = tags["artist"][0]
            if "album" in tags:
                self.album = tags["album"][0]
            return
        except MutagenError as e:
            log.debug(f"Mutagen could not read tags for {self.path}: {e}")
        except Exception as e:
            log.warning(f"Unexpected error reading tags for {self.path}: {e}")

        # Fallback: parse from filename
        ext_len = 5 if self.filename.lower().endswith(".flac") else 4
        clean_name = self.filename[:-ext_len]
        parts = clean_name.split(" - ")
        if len(parts) >= 3:
            self.artist = parts[-2]
            self.title = parts[-1]
        elif len(parts) == 2:
            self.artist = parts[0]
            self.title = parts[1]
        else:
            self.title = clean_name

    def _read_duration(self):
        try:
            if self.path.lower().endswith(".flac"):
                audio = FLAC(self.path)
            else:
                audio = MP3(self.path)
            self.duration = float(audio.info.length)
        except MutagenError as e:
            log.debug(f"Could not read duration for {self.path}: {e}")
        except Exception as e:
            log.warning(f"Unexpected error reading duration for {self.path}: {e}")

    def get_duration(self) -> float:
        return self.duration


class Library:
    SUPPORTED = (".mp3", ".flac")

    def __init__(self, folder: str):
        self.folder = folder
        self.tracks: list[Track] = []
        self._loading = True
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._scan_bg, daemon=True)
        self._thread.start()

    def _scan_bg(self):
        tracks = []
        try:
            files = sorted(
                f for f in os.listdir(self.folder)
                if f.lower().endswith(self.SUPPORTED)
            )
            for f in files:
                tracks.append(Track(os.path.join(self.folder, f), f))
        except OSError as e:
            log.error(f"Could not scan folder {self.folder}: {e}")
        with self._lock:
            self.tracks = tracks
            self._loading = False

    @property
    def is_loading(self) -> bool:
        return self._loading

    def get_tracks(self) -> list[Track]:
        with self._lock:
            return list(self.tracks)

    def search_strict(self, query: str):
        query = query.lower()
        tracks = self.get_tracks()
        for field, mode in [
            (lambda t: t.artist.lower(), "artist"),
            (lambda t: t.album.lower(), "album"),
            (lambda t: t.title.lower(), "title"),
        ]:
            matches = [t for t in tracks if field(t) == query]
            if matches:
                return matches, mode
        return [], None

    def search_fuzzy(self, query: str) -> list[Track]:
        query = query.lower()
        return [
            t for t in self.get_tracks()
            if query in t.title.lower()
            or query in t.artist.lower()
            or query in t.album.lower()
        ]


# --- 4. AUDIO ENGINE ---
class AudioPlayer:
    REPEAT_NONE = "none"
    REPEAT_ALL  = "all"
    REPEAT_ONE  = "one"

    def __init__(self, config: Config):
        pygame.mixer.init()
        self.config = config
        self.queue: list[Track] = []
        self.index = 0
        self.current_track: Track | None = None
        self.is_paused = False
        self._start_time = 0.0
        self._paused_pos = 0.0
        pygame.mixer.music.set_volume(self.config.volume)

    def load_queue(self, tracks: list[Track], start_index: int = 0):
        self.queue = list(tracks)
        self.index = start_index
        self.play_current()

    def play_current(self):
        if not (0 <= self.index < len(self.queue)):
            return
        self.current_track = self.queue[self.index]
        try:
            pygame.mixer.music.load(self.current_track.path)
            pygame.mixer.music.play()
            self.is_paused = False
            self._start_time = time.time()
            self._paused_pos = 0.0
        except pygame.error as e:
            log.error(f"pygame could not play {self.current_track.path}: {e}")

    def toggle_pause(self):
        if not self.current_track:
            return
        if self.is_paused:
            pygame.mixer.music.unpause()
            self.is_paused = False
            self._start_time = time.time() - self._paused_pos
        else:
            self._paused_pos = self.get_position()
            pygame.mixer.music.pause()
            self.is_paused = True

    def update(self):
        if not self.current_track or self.is_paused:
            return
        if pygame.mixer.music.get_busy():
            return

        repeat = self.config.repeat_mode
        if repeat == self.REPEAT_ONE:
            self.play_current()
        elif self.index < len(self.queue) - 1:
            self.index += 1
            self.play_current()
        elif repeat == self.REPEAT_ALL and self.queue:
            self.index = 0
            self.play_current()
        # else: end of queue, stop naturally

    def next(self):
        if self.index < len(self.queue) - 1:
            self.index += 1
            self.play_current()
        elif self.config.repeat_mode == self.REPEAT_ALL and self.queue:
            self.index = 0
            self.play_current()

    def prev(self):
        # If more than 3s in, restart current track; otherwise go to previous
        if self.get_position() > 3.0:
            self.play_current()
        elif self.index > 0:
            self.index -= 1
            self.play_current()

    def seek(self, amount: float):
        if not self.current_track:
            return
        current_pos = self.get_position()
        new_pos = max(0.0, min(self.current_track.duration, current_pos + amount))
        try:
            pygame.mixer.music.set_pos(new_pos)
            self._start_time = time.time() - new_pos
            if self.is_paused:
                self._paused_pos = new_pos
        except pygame.error as e:
            log.warning(f"Seek failed: {e}")

    def change_volume(self, amount: float):
        self.config.volume = max(0.0, min(1.0, self.config.volume + amount))
        pygame.mixer.music.set_volume(self.config.volume)
        self.config.save()

    def get_position(self) -> float:
        if not self.current_track:
            return 0.0
        if self.is_paused:
            return self._paused_pos
        # pygame.mixer.music.get_pos() is authoritative when playing
        raw = pygame.mixer.music.get_pos()
        if raw >= 0:
            return raw / 1000.0
        return max(0.0, time.time() - self._start_time)


# --- 5. INTERFACE (VIEW) ---
class Interface:
    REPEAT_ICONS = {"none": " ", "all": "🔁", "one": "🔂"}
    # Fallback for terminals that can't render unicode
    REPEAT_ASCII = {"none": "[ ]", "all": "[A]", "one": "[1]"}

    def __init__(self, stdscr, config: Config):
        self.stdscr = stdscr
        self.config = config
        curses.start_color()
        curses.curs_set(0)
        self.stdscr.timeout(100)
        self.config.apply_theme()

    def _safe_addstr(self, y: int, x: int, text: str, attr: int = 0):
        h, w = self.stdscr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        max_len = w - x
        if max_len <= 0:
            return
        try:
            self.stdscr.addstr(y, x, text[:max_len], attr)
        except curses.error:
            pass

    def draw_progress_bar(self, y: int, x: int, width: int, current: float, total: float):
        pct = min(current / total, 1.0) if total > 0 else 0.0
        time_str = f" {format_time(current)} / {format_time(total)}"
        bar_width = max(5, width - len(time_str) - 2)

        o_pos = min(int(bar_width * pct), bar_width - 1)
        rem = bar_width - o_pos - 1

        self._safe_addstr(y, x, "[", curses.color_pair(1))
        if o_pos > 0:
            self._safe_addstr(y, x + 1, "-" * o_pos, curses.color_pair(1) | curses.A_BOLD)
        self._safe_addstr(y, x + 1 + o_pos, "O", curses.color_pair(1) | curses.A_BOLD)
        if rem > 0:
            self._safe_addstr(y, x + 2 + o_pos, "-" * rem, curses.color_pair(1))
        self._safe_addstr(y, x + 1 + bar_width, "]" + time_str, curses.color_pair(1))

    def draw_header(self, height: int, width: int, player: "AudioPlayer"):
        if height < 4:
            return
        track = player.current_track

        title  = (track.title  if track else "None")[:40]
        artist = (track.artist if track else "-")[:40]
        album  = (track.album  if track else "-")[:40]

        self._safe_addstr(0, 1, "Track:  ", curses.color_pair(1))
        self._safe_addstr(0, 9, title, curses.color_pair(4) | curses.A_BOLD)
        self._safe_addstr(1, 1, "Artist: ", curses.color_pair(1))
        self._safe_addstr(1, 9, artist, curses.color_pair(2))
        self._safe_addstr(2, 1, "Album:  ", curses.color_pair(1))
        self._safe_addstr(2, 9, album, curses.color_pair(2))

        # Right panel: only draw if there's room
        right_start = min(55, width // 2)
        avail_w = width - right_start - 2
        if avail_w < 20:
            return

        total_time = track.duration if track else 0.0
        curr_time = player.get_position()
        self.draw_progress_bar(1, right_start, avail_w, curr_time, total_time)

        icon = "||" if player.is_paused else " >"
        txt  = " PAUSED"  if player.is_paused else " PLAYING"
        color = curses.color_pair(3) if player.is_paused else curses.color_pair(2)
        repeat_str = self.REPEAT_ASCII.get(player.config.repeat_mode, "[ ]")
        status_str = f"{icon}{txt}  Vol:{int(player.config.volume * 100):3d}%  {repeat_str}"
        center_x = max(right_start, right_start + (avail_w // 2) - (len(status_str) // 2))
        self._safe_addstr(2, center_x, f"{icon}{txt}", color | curses.A_BOLD)
        self._safe_addstr(2, center_x + len(icon) + len(txt),
                          f"  Vol:{int(player.config.volume * 100):3d}%  {repeat_str}",
                          curses.color_pair(4))

    def draw_list(self, height: int, width: int, tracks: list, selected_idx: int,
                  scroll_offset: int, current_track, view_mode: str, is_loading: bool):
        self._safe_addstr(4, 0, "-" * width, curses.color_pair(1))
        label = " [ Loading... ] " if is_loading else f" [ {view_mode} ] "
        self._safe_addstr(4, 2, label, curses.color_pair(1) | curses.A_BOLD)

        w_t  = max(10, int((width - 6) * 0.40))
        w_a  = max(8,  int((width - 6) * 0.28))
        w_alb = max(8, width - 6 - w_t - w_a)

        header = f"{'#'.ljust(4)} {'TITLE'.ljust(w_t)} {'ARTIST'.ljust(w_a)} {'ALBUM'.ljust(w_alb)}"
        self._safe_addstr(5, 0, header, curses.color_pair(1) | curses.A_BOLD | curses.A_UNDERLINE)

        max_rows = height - 8
        if max_rows <= 0:
            return
        visible = tracks[scroll_offset: scroll_offset + max_rows]

        for i, t in enumerate(visible):
            row_y = 6 + i
            abs_idx = i + scroll_offset
            is_sel     = (abs_idx == selected_idx)
            is_playing = (current_track and t.path == current_track.path)

            s_num   = (str(abs_idx + 1) + ".").ljust(4)
            s_title  = t.title[:w_t - 1].ljust(w_t)
            s_artist = t.artist[:w_a - 1].ljust(w_a)
            s_album  = t.album[:w_alb - 1].ljust(w_alb)
            full_line = f"{s_num} {s_title} {s_artist} {s_album}"

            if is_sel:
                self._safe_addstr(row_y, 0, full_line, curses.color_pair(5) | curses.A_BOLD)
            elif is_playing:
                self._safe_addstr(row_y, 0, full_line, curses.color_pair(2) | curses.A_BOLD)
            else:
                self._safe_addstr(row_y, 0, s_num, curses.color_pair(1))
                self._safe_addstr(row_y, 5, s_title, curses.color_pair(4))
                self._safe_addstr(row_y, 5 + w_t + 1, s_artist, curses.color_pair(2))
                self._safe_addstr(row_y, 5 + w_t + 1 + w_a + 1, s_album, curses.color_pair(2))

    def draw_footer(self, height: int, width: int, search_mode: bool,
                    query: str, view_mode: str):
        if height < 2:
            return
        if search_mode:
            self._safe_addstr(height - 1, 0,
                              f"SEARCH: {query}_",
                              curses.color_pair(1) | curses.A_REVERSE)
        else:
            txt = (f"[{view_mode}] "
                   "SPACE:Pause | /:Search | ←→:Seek | S+←→:Skip | "
                   "+/-:Vol | r:Repeat | v:View | s:Shuffle | x:Quit")
            self._safe_addstr(height - 1, 0, txt, curses.color_pair(4))

    def draw_theme_menu(self, height: int, width: int, selected: int):
        box_h, box_w = 10, 44
        sy = max(0, (height // 2) - (box_h // 2))
        sx = max(0, (width  // 2) - (box_w // 2))
        for i in range(box_h):
            self._safe_addstr(sy + i, sx, " " * box_w, curses.A_REVERSE)
        self._safe_addstr(sy, sx + 14, " SELECT THEME ", curses.A_REVERSE | curses.A_BOLD)
        for i, name in enumerate(self.config.THEMES):
            pre = "> " if i == selected else "  "
            style = curses.A_REVERSE | curses.A_BOLD if i == selected else curses.A_REVERSE
            self._safe_addstr(sy + 2 + i, sx + 2, (pre + name).ljust(box_w - 4), style)
        self._safe_addstr(sy + box_h - 1, sx + 2,
                          "ENTER: Apply | ESC: Cancel", curses.A_REVERSE)


# --- 6. APP CONTROLLER ---
class App:
    def __init__(self, stdscr, library: Library, initial_queue=None, initial_song=None):
        self.stdscr = stdscr
        self.config = Config()
        self.library = library
        self.player = AudioPlayer(self.config)
        self.ui = Interface(stdscr, self.config)

        self.view_mode = "LIBRARY"
        self.selection = 0
        self.scroll = 0
        self.search_mode = False
        self.search_query = ""
        self._last_search = ""
        self._search_results: list[Track] = []
        self.theme_mode = False
        self.theme_sel = self.config.theme_index
        self._shuffle_on = False

        if initial_queue:
            self.player.load_queue(initial_queue)
            if initial_song:
                try:
                    idx = initial_queue.index(initial_song)
                    self.player.index = idx
                    self.player.play_current()
                except ValueError:
                    pass
            self.view_mode = "QUEUE"

    def _get_search_results(self) -> list[Track]:
        if self.search_query != self._last_search:
            self._last_search = self.search_query
            self._search_results = self.library.search_fuzzy(self.search_query) if self.search_query else []
        return self._search_results

    def get_visible_list(self) -> list[Track]:
        if self.view_mode == "QUEUE":
            return self.player.queue
        if self.search_query and not self.search_mode:
            return self._get_search_results()
        return self.library.get_tracks()

    def _clamp_selection(self, tracks: list):
        if not tracks:
            self.selection = 0
            self.scroll = 0
            return
        self.selection = max(0, min(self.selection, len(tracks) - 1))

    def _scroll_to_selection(self, height: int):
        max_rows = height - 8
        if max_rows <= 0:
            return
        if self.selection >= self.scroll + max_rows:
            self.scroll = self.selection - max_rows + 1
        if self.selection < self.scroll:
            self.scroll = self.selection

    def run(self):
        while True:
            self.stdscr.erase()
            h, w = self.stdscr.getmaxyx()
            self.player.update()

            visible = self.get_visible_list()
            self._clamp_selection(visible)

            self.ui.draw_header(h, w, self.player)
            self.ui.draw_list(h, w, visible, self.selection, self.scroll,
                               self.player.current_track, self.view_mode,
                               self.library.is_loading)
            self.ui.draw_footer(h, w, self.search_mode, self.search_query, self.view_mode)

            if self.theme_mode:
                self.ui.draw_theme_menu(h, w, self.theme_sel)

            self.stdscr.refresh()

            try:
                key = self.stdscr.getch()
            except curses.error:
                key = -1

            if key == -1:
                continue

            if self.theme_mode:
                self._handle_theme(key)
            elif self.search_mode:
                self._handle_search(key, h)
            else:
                if not self._handle_normal(key, h, visible):
                    break  # 'x' was pressed

        _cache.close()
        self.config.save()

    def _handle_theme(self, key: int):
        if key == 27:
            self.theme_mode = False
        elif key == curses.KEY_UP:
            self.theme_sel = max(0, self.theme_sel - 1)
        elif key == curses.KEY_DOWN:
            self.theme_sel = min(len(self.config.THEMES) - 1, self.theme_sel + 1)
        elif key in (10, curses.KEY_ENTER):
            self.config.set_theme(self.theme_sel)
            self.ui = Interface(self.stdscr, self.config)
            self.theme_mode = False

    def _handle_search(self, key: int, height: int):
        if key in (10, curses.KEY_ENTER):
            self.search_mode = False
            self.selection = 0
            self.scroll = 0
        elif key == 27:
            self.search_mode = False
            self.search_query = ""
            self._last_search = ""
            self._search_results = []
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self.search_query = self.search_query[:-1]
        elif 32 <= key <= 126:
            self.search_query += chr(key)
            self.selection = 0
            self.scroll = 0

    def _handle_normal(self, key: int, height: int, visible: list) -> bool:
        """Returns False if the app should quit."""
        if key == ord("x"):
            return False

        elif key == ord(" "):
            self.player.toggle_pause()

        elif key == ord("s"):
            # Toggle shuffle
            self._shuffle_on = not self._shuffle_on
            tracks = self.library.get_tracks()
            if self._shuffle_on:
                current = self.player.current_track
                others = [t for t in tracks if t != current]
                random.shuffle(others)
                new_queue = ([current] + others) if current else others
                self.player.queue = new_queue
                self.player.index = 0
            else:
                # Restore library order
                self.player.queue = list(tracks)
                if self.player.current_track:
                    try:
                        self.player.index = self.player.queue.index(self.player.current_track)
                    except ValueError:
                        self.player.index = 0

        elif key == ord("r"):
            self.config.cycle_repeat()

        elif key == ord("v"):
            self.view_mode = "QUEUE" if self.view_mode == "LIBRARY" else "LIBRARY"
            self.selection = 0
            self.scroll = 0

        elif key == ord("/"):
            self.search_mode = True
            self.search_query = ""

        elif key == ord("T"):
            self.theme_mode = True
            self.theme_sel = self.config.theme_index

        elif key in (curses.KEY_DOWN, ord("j")):
            if self.selection < len(visible) - 1:
                self.selection += 1
                self._scroll_to_selection(height)

        elif key in (curses.KEY_UP, ord("k")):
            if self.selection > 0:
                self.selection -= 1
                self._scroll_to_selection(height)

        elif key == curses.KEY_RIGHT:
            self.player.seek(5)
        elif key == curses.KEY_LEFT:
            self.player.seek(-5)
        elif key == curses.KEY_SRIGHT:
            self.player.next()
        elif key == curses.KEY_SLEFT:
            self.player.prev()

        elif key in (ord("+"), ord("=")):
            self.player.change_volume(0.05)
        elif key == ord("-"):
            self.player.change_volume(-0.05)

        elif key in (10, curses.KEY_ENTER):
            if visible:
                t = visible[self.selection]
                if self.view_mode == "QUEUE":
                    self.player.index = self.selection
                    self.player.play_current()
                else:
                    self.player.load_queue(visible[self.selection:])

        return True


# --- 7. ENTRY POINT ---
def main():
    default_folder = os.path.join(os.path.expanduser("~"), "Music")
    folder = default_folder
    query = None

    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if os.path.isdir(arg):
            folder = arg
        else:
            query = " ".join(sys.argv[1:]).lower()

    if not os.path.isdir(folder):
        print(f"Error: music folder not found: {folder}", file=sys.stderr)
        print("Usage: bbmp [/path/to/music | search query]", file=sys.stderr)
        sys.exit(1)

    if not os.access(folder, os.R_OK):
        print(f"Error: cannot read folder: {folder}", file=sys.stderr)
        sys.exit(1)

    lib = Library(folder)

    # Wait briefly for fast libraries; UI shows "Loading..." for slow ones
    lib._thread.join(timeout=2.0)

    tracks = lib.get_tracks()

    q: list[Track] | None = None
    s: Track | None = None

    if query:
        # Need full scan for search-on-launch
        lib._thread.join()
        matches, mode = lib.search_strict(query)
        if not matches:
            print(f"No artist, album, or title found for '{query}'", file=sys.stderr)
            sys.exit(1)
        if mode in ("artist", "album"):
            q = matches[:]
            random.shuffle(q)
            s = q[0]
        elif mode == "title":
            s = matches[0]
            others = [t for t in lib.get_tracks() if t.artist == s.artist and t != s]
            random.shuffle(others)
            q = [s] + others
    elif not lib.is_loading and not tracks:
        print(f"No .mp3 or .flac files found in: {folder}", file=sys.stderr)
        sys.exit(1)

    try:
        curses.wrapper(lambda stdscr: App(stdscr, lib, q, s).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
