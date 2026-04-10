#!/usr/bin/env python3
"""
Karaoke Player for Linux
========================
Supports: .cdg + .mp3/.ogg karaoke files, .mp4/.mkv video karaoke, plain audio

Dependencies:
    pip install pygame mutagen python-vlc
    sudo apt install vlc   # VLC must be installed for mp4/mkv support

Usage:
    python karaoke_player.py                  # Launch with built-in library browser
    python karaoke_player.py song.cdg         # Play a CDG+MP3 karaoke file directly
    python karaoke_player.py /path/to/library # Open library browser in that folder

Controls (player):
    SPACE       Pause / Resume
    LEFT/RIGHT  Seek -10s / +10s
    UP/DOWN     Volume up / down
    F           Toggle fullscreen
    L / ESC     Open library browser
    Q           Quit

Controls (library browser):
    UP/DOWN     Navigate songs
    PAGE UP/DN  Scroll by page
    ENTER       Play selected song
    TAB         Change folder
    TYPE        Search / filter songs
    BACKSPACE   Clear last search character
    ESC         Back to player (if song loaded)
"""

import sys
import os
import re
import time
import math
from pathlib import Path
from dataclasses import dataclass

try:
    import pygame
except ImportError:
    print("pygame not found. Install with:  pip install pygame")
    sys.exit(1)

try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3
    from mutagen.oggvorbis import OggVorbis
    from mutagen.flac import FLAC
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False


def _load_vlc():
    """
    Import python-vlc, helping it find libvlc.so if it isn't on LD_LIBRARY_PATH.
    Returns the vlc module or None.
    """
    import os, ctypes, glob

    # Common locations for libvlc on Debian/Ubuntu/Mint/Arch
    search_paths = [
        "/usr/lib/x86_64-linux-gnu",
        "/usr/lib/aarch64-linux-gnu",
        "/usr/lib",
        "/usr/local/lib",
        "/snap/vlc/current/lib",
    ]

    # Try to pre-load libvlc.so so python-vlc can find it
    for base in search_paths:
        for pattern in ["libvlc.so.5", "libvlc.so"]:
            path = os.path.join(base, pattern)
            if os.path.exists(path):
                try:
                    ctypes.CDLL(path)
                    os.environ.setdefault("VLC_PLUGIN_PATH",
                        os.path.join(base, "vlc", "plugins"))
                    break
                except OSError:
                    continue

    try:
        import vlc
        return vlc
    except ImportError:
        return None

_vlc = _load_vlc()
HAS_VLC = _vlc is not None
if HAS_VLC:
    import vlc

# ──────────────────────────────────────────────
# CDG Parser
# ──────────────────────────────────────────────

CDG_PACKET_SIZE  = 24
CDG_COMMAND      = 0x09
CDG_CMD_MEMORY_PRESET     = 1
CDG_CMD_BORDER_PRESET     = 2
CDG_CMD_TILE_BLOCK        = 6
CDG_CMD_SCROLL_PRESET     = 20
CDG_CMD_SCROLL_COPY       = 24
CDG_CMD_LOAD_COL_TBL_LOW  = 30
CDG_CMD_LOAD_COL_TBL_HIGH = 31
CDG_CMD_TILE_BLOCK_XOR    = 38

CDG_WIDTH  = 300
CDG_HEIGHT = 216
TILE_W = 6
TILE_H = 12


class CDGParser:
    def __init__(self, cdg_path: str):
        with open(cdg_path, "rb") as f:
            self.data = f.read()
        self.num_packets  = len(self.data) // CDG_PACKET_SIZE
        self.color_table  = [(0, 0, 0)] * 16
        self.pixels       = [[0] * CDG_WIDTH for _ in range(CDG_HEIGHT)]
        self.bg_color     = 0
        self.packet_index = 0
        self.surface      = pygame.Surface((CDG_WIDTH, CDG_HEIGHT))

    def render_to(self, seconds: float):
        target = min(int(seconds * 300), self.num_packets)
        while self.packet_index < target:
            self._process_packet(self.packet_index)
            self.packet_index += 1
        self._blit_surface()
        return self.surface

    def seek(self, seconds: float):
        self.color_table  = [(0, 0, 0)] * 16
        self.pixels       = [[0] * CDG_WIDTH for _ in range(CDG_HEIGHT)]
        self.packet_index = 0
        self.render_to(seconds)

    def _process_packet(self, idx: int):
        offset = idx * CDG_PACKET_SIZE
        pkt    = self.data[offset:offset + CDG_PACKET_SIZE]
        if len(pkt) < CDG_PACKET_SIZE:
            return
        command     = pkt[0] & 0x3F
        instruction = pkt[1] & 0x3F
        data        = pkt[4:20]
        if command != CDG_COMMAND:
            return

        if instruction == CDG_CMD_MEMORY_PRESET:
            color = data[0] & 0x0F
            if (data[1] & 0x0F) == 0:
                self.pixels   = [[color] * CDG_WIDTH for _ in range(CDG_HEIGHT)]
                self.bg_color = color

        elif instruction == CDG_CMD_BORDER_PRESET:
            color = data[0] & 0x0F
            for x in range(CDG_WIDTH):
                for y in range(6):
                    self.pixels[y][x]              = color
                    self.pixels[CDG_HEIGHT-1-y][x] = color
            for y in range(CDG_HEIGHT):
                for x in range(6):
                    self.pixels[y][x]             = color
                    self.pixels[y][CDG_WIDTH-1-x] = color

        elif instruction in (CDG_CMD_TILE_BLOCK, CDG_CMD_TILE_BLOCK_XOR):
            xor_mode  = (instruction == CDG_CMD_TILE_BLOCK_XOR)
            color0    = data[0] & 0x0F
            color1    = data[1] & 0x0F
            row, col  = data[2] & 0x1F, data[3] & 0x3F
            tile_data = data[4:16]
            x0, y0    = col * TILE_W, row * TILE_H
            for y in range(TILE_H):
                byte = tile_data[y] if y < len(tile_data) else 0
                for x in range(TILE_W):
                    bit   = (byte >> (5 - x)) & 1
                    color = color1 if bit else color0
                    px, py = x0 + x, y0 + y
                    if 0 <= px < CDG_WIDTH and 0 <= py < CDG_HEIGHT:
                        if xor_mode:
                            self.pixels[py][px] ^= color
                        else:
                            self.pixels[py][px]  = color

        elif instruction in (CDG_CMD_LOAD_COL_TBL_LOW, CDG_CMD_LOAD_COL_TBL_HIGH):
            base = 0 if instruction == CDG_CMD_LOAD_COL_TBL_LOW else 8
            for i in range(8):
                high = data[i * 2]     & 0x3F
                low  = data[i * 2 + 1] & 0x3F
                r    = (high >> 2) & 0x0F
                g    = ((high & 0x03) << 2) | ((low >> 4) & 0x03)
                b    = low & 0x0F
                self.color_table[base + i] = (r * 17, g * 17, b * 17)

        elif instruction in (CDG_CMD_SCROLL_PRESET, CDG_CMD_SCROLL_COPY):
            color    = data[0] & 0x0F
            h_cmd    = (data[1] & 0x30) >> 4
            v_cmd    = (data[2] & 0x30) >> 4
            dx       = -TILE_W if h_cmd == 2 else (TILE_W if h_cmd == 1 else 0)
            dy       = -TILE_H if v_cmd == 2 else (TILE_H if v_cmd == 1 else 0)
            fill     = color if instruction == CDG_CMD_SCROLL_PRESET else None
            new_pix  = [[color] * CDG_WIDTH for _ in range(CDG_HEIGHT)]
            for y in range(CDG_HEIGHT):
                for x in range(CDG_WIDTH):
                    sx, sy = x - dx, y - dy
                    if 0 <= sx < CDG_WIDTH and 0 <= sy < CDG_HEIGHT:
                        new_pix[y][x] = self.pixels[sy][sx]
                    elif fill is not None:
                        new_pix[y][x] = fill
            self.pixels = new_pix

    def _blit_surface(self):
        pxarray = pygame.PixelArray(self.surface)
        for y in range(CDG_HEIGHT):
            for x in range(CDG_WIDTH):
                c = self.color_table[self.pixels[y][x]]
                pxarray[x][y] = self.surface.map_rgb(c)
        del pxarray



# ──────────────────────────────────────────────
# VLC Video Player wrapper
# ──────────────────────────────────────────────

class VLCPlayer:
    """
    Wraps python-vlc to play mp4/mkv into a pygame window.
    VLC renders directly into the window's native X11/Wayland handle,
    so pygame draws its UI overlay on top each frame.
    """

    def __init__(self):
        self._instance  = None
        self._player    = None
        self._active    = False

    def _ensure_instance(self):
        if self._instance is not None:
            return
        import os, glob, sys

        # Find libvlc plugin directory so VLC can initialise properly
        plugin_dirs = []
        for base in ["/usr/lib/x86_64-linux-gnu/vlc",
                     "/usr/lib/vlc",
                     "/usr/local/lib/vlc",
                     "/snap/vlc/current/lib/vlc"]:
            if os.path.isdir(os.path.join(base, "plugins")):
                plugin_dirs.append(os.path.join(base, "plugins"))

        if plugin_dirs:
            os.environ["VLC_PLUGIN_PATH"] = plugin_dirs[0]

        for args in [
            ["--no-xlib", "--quiet", "--no-video-title-show"],
            ["--quiet", "--no-video-title-show"],
            ["--no-video-title-show"],
            [],
        ]:
            try:
                inst = vlc.Instance(*args)
                if inst is not None:
                    self._instance = inst
                    return
            except Exception as e:
                print(f"[VLC] Instance attempt {args} failed: {e}", file=sys.stderr)
                continue

        # Last resort: try with plugin path as argument
        if plugin_dirs:
            try:
                inst = vlc.Instance(f"--plugin-path={plugin_dirs[0]}")
                if inst is not None:
                    self._instance = inst
                    return
            except Exception:
                pass

        raise RuntimeError(
            "vlc.Instance() returned None.\n"
            "Make sure VLC is installed: sudo apt install vlc\n"
            f"VLC_PLUGIN_PATH tried: {plugin_dirs}"
        )

    def load(self, path: str, hwnd: int) -> bool:
        """Load a video file and bind to the given window handle. Returns True on success."""
        if not HAS_VLC:
            return False
        try:
            self._ensure_instance()
            if self._player:
                self._player.stop()
                self._player.release()
            media          = self._instance.media_new(path)
            self._player   = self._instance.media_player_new()
            self._player.set_media(media)
            self._player.set_xwindow(hwnd)   # embed in pygame window
            self._player.play()
            self._active   = True
            return True
        except Exception as e:
            print(f"[VLC] load error: {e}", file=__import__("sys").stderr)
            return False

    def stop(self):
        if self._player:
            self._player.stop()
        self._active = False

    def detach(self):
        """Stop playback and release the embedded window so VLC stops painting."""
        if self._player:
            self._player.stop()
            # Setting xwindow to 0 tells VLC to detach from the surface entirely
            try:
                self._player.set_xwindow(0)
            except Exception:
                pass
        self._active = False

    def pause(self):
        if self._player and self._active:
            self._player.pause()

    def unpause(self):
        if self._player and self._active:
            self._player.pause()   # VLC pause() is a toggle

    def seek(self, seconds: float):
        if self._player and self._active:
            self._player.set_time(int(seconds * 1000))

    def set_volume(self, volume_0_to_1: float):
        if self._player:
            self._player.audio_set_volume(int(volume_0_to_1 * 100))

    def get_position(self) -> float:
        if self._player and self._active:
            ms = self._player.get_time()
            return ms / 1000.0 if ms >= 0 else 0.0
        return 0.0

    def get_duration(self) -> float:
        if self._player and self._active:
            ms = self._player.get_length()
            return ms / 1000.0 if ms > 0 else 0.0
        return 0.0

    def is_playing(self) -> bool:
        return bool(self._player and self._player.is_playing())

    def is_ended(self) -> bool:
        if not self._player or not self._active:
            return False
        state = self._player.get_state()
        return state in (vlc.State.Ended, vlc.State.Error, vlc.State.Stopped)

    def release(self):
        if self._player:
            self._player.stop()
            self._player.release()
            self._player = None
        if self._instance:
            self._instance.release()
            self._instance = None
        self._active = False

# ──────────────────────────────────────────────
# Song metadata + library scanner
# ──────────────────────────────────────────────

@dataclass
class Song:
    title:     str
    artist:    str
    play_path: str        # .cdg path (if CDG) or audio path
    has_cdg:   bool = False
    is_video:  bool = False
    duration:  float = 0.0

    @property
    def display(self) -> str:
        if self.artist:
            return f"{self.title}  —  {self.artist}"
        return self.title

    @property
    def sort_key(self) -> tuple:
        def norm(s: str) -> str:
            s = s.lower().strip()
            return s[4:] if s.startswith("the ") else s
        return (norm(self.artist), norm(self.title))


_SEPARATORS   = re.compile(r'\s*[-\u2013\u2014]\s*')
_TRACK_PREFIX = re.compile(r'^\d+[\s._\-]+')
_JUNK_SUFFIX  = re.compile(
    r'\s*[\(\[](karaoke|instrumental|lyrics?|backing|track|cdg|version|hd|4k)[^\)\]]*[\)\]]',
    re.IGNORECASE
)


def _clean(s: str) -> str:
    s = _JUNK_SUFFIX.sub('', s)
    s = _TRACK_PREFIX.sub('', s)
    return s.strip(' _.-')


def _title_case(s: str) -> str:
    if s == s.upper() and len(s) > 3:
        s = s.lower()
    SMALL = {'a','an','the','and','but','or','for','nor','on','at','to','by','in','of','up'}
    words = s.split()
    out   = []
    for i, w in enumerate(words):
        out.append(w.capitalize() if i == 0 or w.lower() not in SMALL else w.lower())
    return ' '.join(out)


def _parse_filename(stem: str) -> tuple:
    """Return (title, artist) from a bare filename stem."""
    stem  = stem.replace('_', ' ')
    stem  = _clean(stem)
    parts = _SEPARATORS.split(stem, maxsplit=1)
    if len(parts) == 2:
        # "Artist - Title" is the dominant karaoke convention
        return _title_case(_clean(parts[1])), _title_case(_clean(parts[0]))
    return _title_case(_clean(stem)), ''


def _read_metadata(audio_path: str) -> tuple:
    """Return (title, artist) from file tags, or ('','') on failure."""
    if not HAS_MUTAGEN:
        return '', ''
    try:
        ext = Path(audio_path).suffix.lower()
        if ext == '.mp3':
            tags   = ID3(audio_path)
            title  = str(tags.get('TIT2', '')).strip()
            artist = str(tags.get('TPE1', '')).strip()
            return title, artist
        elif ext == '.ogg':
            tags   = OggVorbis(audio_path)
            title  = (tags.get('title',  [''])[0] or '').strip()
            artist = (tags.get('artist', [''])[0] or '').strip()
            return title, artist
        elif ext == '.flac':
            tags   = FLAC(audio_path)
            title  = (tags.get('title',  [''])[0] or '').strip()
            artist = (tags.get('artist', [''])[0] or '').strip()
            return title, artist
    except Exception:
        pass
    return '', ''


def _get_duration(path: str) -> float:
    if not HAS_MUTAGEN:
        return 0.0
    try:
        ext = Path(path).suffix.lower()
        if ext == '.mp3':
            return MP3(path).info.length
        elif ext == '.ogg':
            return OggVorbis(path).info.length
        elif ext == '.flac':
            return FLAC(path).info.length
    except Exception:
        pass
    return 0.0


def scan_library(folder: str) -> list:
    """
    Recursively scan `folder` and return a sorted list of Song objects.
    CDG + audio pairs are merged into one entry; standalone audio is included too.
    """
    folder     = Path(folder)
    audio_exts = {'.mp3', '.ogg', '.wav', '.flac'}
    video_exts = {'.mp4', '.mkv', '.avi', '.webm'}
    songs: dict = {}

    # Collect all .cdg files keyed by lower-case stem
    cdg_map: dict = {}
    for cdg in folder.rglob('*.cdg'):
        cdg_map[cdg.stem.lower()] = cdg

    # Walk audio + video files
    seen: set = set()
    for audio in sorted(folder.rglob('*')):
        if audio.suffix.lower() not in audio_exts | video_exts:
            continue
        key = audio.stem.lower()
        if key in seen:
            continue
        seen.add(key)

        is_video  = audio.suffix.lower() in video_exts
        has_cdg   = key in cdg_map and not is_video
        play_path = str(cdg_map[key]) if has_cdg else str(audio)

        title, artist = _read_metadata(str(audio))
        if not title:
            title, artist = _parse_filename(audio.stem)

        songs[key] = Song(
            title     = title or audio.stem,
            artist    = artist,
            play_path = play_path,
            has_cdg   = has_cdg,
            is_video  = is_video,
            duration  = _get_duration(str(audio)),
        )

    # CDG files with no matching audio (rare edge case)
    for key, cdg in cdg_map.items():
        if key not in songs:
            title, artist = _parse_filename(cdg.stem)
            songs[key] = Song(
                title     = title or cdg.stem,
                artist    = artist,
                play_path = str(cdg),
                has_cdg   = True,
            )

    return sorted(songs.values(), key=lambda s: s.sort_key)


# ──────────────────────────────────────────────
# Karaoke Player
# ──────────────────────────────────────────────

class KaraokePlayer:
    FPS       = 30
    SEEK_STEP = 10
    MODE_PLAYER  = 'player'
    MODE_LIBRARY = 'library'

    def __init__(self):
        pygame.init()
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)

        self.screen_w, self.screen_h = 1920, 1080
        self.fullscreen = False
        self.screen = pygame.display.set_mode(
            (self.screen_w, self.screen_h), pygame.RESIZABLE)
        pygame.display.set_caption("🎤 Karaoke Player")
        self.clock = pygame.time.Clock()

        # Fonts — sized for 1080p, rescaled dynamically when window is resized
        self._base_h       = 1080
        self._last_font_h  = self.screen_h
        self._FONT_FACE    = "dejavusans"   # clean bold sans-serif, widely available
        self.font_large    = pygame.font.SysFont(self._FONT_FACE, 38, bold=True)
        self.font_med      = pygame.font.SysFont(self._FONT_FACE, 28, bold=True)
        self.font_small    = pygame.font.SysFont(self._FONT_FACE, 22, bold=True)
        self.font_search   = pygame.font.SysFont(self._FONT_FACE, 26, bold=True)

        # Palette
        self.COL_BG      = (8,   4,  20)
        self.COL_CYAN    = (0,  240, 220)
        self.COL_MAGENTA = (220,  0, 200)
        self.COL_YELLOW  = (255, 220,   0)
        self.COL_WHITE   = (240, 240, 255)
        self.COL_DARK    = (30,  20,  50)
        self.COL_MID     = (60,  40, 100)
        self.COL_SEL_BG  = (20,  60,  80)
        self.COL_SEL_FG  = (0,  255, 220)
        self.COL_DIM     = (120, 100, 160)

        # Playback state
        self.running            = True
        self.paused             = False
        self.volume             = 0.8
        self.cdg                = None
        self.audio_path         = None
        self.duration           = 0.0
        self.seek_offset        = 0.0
        self.elapsed_at_pause   = 0.0
        self.loaded             = False
        self.song_title         = ""
        self.song_artist        = ""
        self.is_video_mode      = False     # True when playing mp4/mkv via VLC
        self.vlc                = VLCPlayer() if HAS_VLC else None

        # Library state
        self.mode           = self.MODE_LIBRARY
        self.library        = []
        self.lib_folder     = str(Path.home())
        self.lib_sel        = 0
        self.lib_scroll     = 0
        self.lib_search     = ""
        self.lib_filtered   = []

        # Starfield
        import random
        random.seed(42)
        self.stars = [(random.randint(0, 1920), random.randint(0, 580),
                       random.random()) for _ in range(120)]

    # ─────────────────────────────────────────
    # Font scaling
    # ─────────────────────────────────────────

    def _update_fonts(self, H: int):
        """Regenerate fonts scaled to the current window height."""
        if H == self._last_font_h:
            return
        self._last_font_h = H
        scale = H / self._base_h
        self.font_large  = pygame.font.SysFont(self._FONT_FACE, max(14, int(38 * scale)), bold=True)
        self.font_med    = pygame.font.SysFont(self._FONT_FACE, max(11, int(28 * scale)), bold=True)
        self.font_small  = pygame.font.SysFont(self._FONT_FACE, max(9,  int(22 * scale)), bold=True)
        self.font_search = pygame.font.SysFont(self._FONT_FACE, max(11, int(26 * scale)), bold=True)
        # Recompute library layout constants
        self.HEADER_H = max(40, int(70  * scale))
        self.SEARCH_H = max(32, int(56  * scale))
        self.FOOTER_H = max(28, int(48  * scale))
        self.ROW_H    = max(28, int(54  * scale))
        self.PADDING  = max(10, int(24  * scale))

    # ─────────────────────────────────────────
    # Library helpers
    # ─────────────────────────────────────────

    def open_folder_dialog(self):
        """In-pygame folder browser — no tkinter dependency."""
        chosen = self._pygame_folder_browser(self.lib_folder)
        if chosen:
            self.lib_folder = chosen
            self._reload_library()

    def _pygame_folder_browser(self, start: str) -> str:
        """
        A simple in-pygame folder picker.
        Returns the chosen folder path, or empty string if cancelled.
        Navigate with UP/DOWN, ENTER to descend or select,
        BACKSPACE to go up, ESC to cancel.
        """
        current   = Path(start).resolve()
        sel       = 0
        scroll    = 0
        clock     = pygame.time.Clock()
        search    = ""

        def list_dirs(path):
            try:
                entries = sorted(
                    [p for p in path.iterdir() if p.is_dir() and not p.name.startswith(".")],
                    key=lambda p: p.name.lower()
                )
            except PermissionError:
                entries = []
            return entries

        dirs = list_dirs(current)

        while True:
            W, H = self.screen.get_size()
            ROW  = self.ROW_H
            PAD  = self.PADDING
            HEADER = self.HEADER_H
            FOOTER = self.FOOTER_H
            LIST_TOP = HEADER + 10
            LIST_BOT = H - FOOTER
            visible  = max(1, (LIST_BOT - LIST_TOP) // ROW)

            self.screen.fill(self.COL_BG)
            t = time.time()
            self._draw_starfield(W, H, t)

            # Header
            pygame.draw.rect(self.screen, self.COL_DARK, (0, 0, W, HEADER))
            pygame.draw.line(self.screen, self.COL_MAGENTA, (0, HEADER), (W, HEADER), 2)
            hs = self.font_large.render("📁  SELECT FOLDER", True, self.COL_CYAN)
            self.screen.blit(hs, (PAD, (HEADER - hs.get_height()) // 2))

            # Current path
            path_surf = self.font_small.render(str(current), True, self.COL_DIM)
            self.screen.blit(path_surf, (PAD, LIST_TOP))

            # Clamp scroll / sel
            sel  = max(0, min(sel,  len(dirs) - 1))
            if sel < scroll:
                scroll = sel
            elif sel >= scroll + visible:
                scroll = sel - visible + 1

            # Dir list
            list_y = LIST_TOP + self.font_small.get_height() + 6
            visible2 = max(1, (LIST_BOT - list_y) // ROW)

            for i in range(visible2):
                idx = scroll + i
                if idx >= len(dirs):
                    break
                d     = dirs[idx]
                ry    = list_y + i * ROW
                issel = (idx == sel)
                if issel:
                    p2 = int(15 * math.sin(t * 3))
                    bg = tuple(max(0, min(255, v + p2)) for v in self.COL_SEL_BG)
                    pygame.draw.rect(self.screen, bg, (0, ry, W, ROW))
                    pygame.draw.rect(self.screen, self.COL_CYAN, (0, ry, 4, ROW))
                elif i % 2 == 0:
                    pygame.draw.rect(self.screen, (12, 8, 28), (0, ry, W, ROW))
                ic   = "📂 " if issel else "📁 "
                tc   = self.COL_SEL_FG if issel else self.COL_WHITE
                name = self.font_med.render(ic + d.name, True, tc)
                self.screen.blit(name, (PAD + 4, ry + (ROW - name.get_height()) // 2))
                pygame.draw.line(self.screen, self.COL_DARK, (0, ry + ROW - 1), (W, ry + ROW - 1), 1)

            if not dirs:
                ms = self.font_med.render("(no subdirectories)", True, self.COL_DIM)
                self.screen.blit(ms, (W // 2 - ms.get_width() // 2, list_y + 20))

            # Footer
            pygame.draw.rect(self.screen, self.COL_DARK, (0, H - FOOTER, W, FOOTER))
            pygame.draw.line(self.screen, self.COL_CYAN, (0, H - FOOTER), (W, H - FOOTER), 1)
            hint = "↑↓ navigate   ENTER select this folder   → open subfolder   ← go up   ESC cancel"
            fhs  = self.font_small.render(hint, True, self.COL_MID)
            self.screen.blit(fhs, (W // 2 - fhs.get_width() // 2,
                                   H - FOOTER + (FOOTER - fhs.get_height()) // 2))

            # "Use this folder" button
            btn_text = f"✔  Use:  {current.name or str(current)}"
            btn_surf = self.font_med.render(btn_text, True, self.COL_BG)
            btn_w    = btn_surf.get_width() + 32
            btn_h    = btn_surf.get_height() + 12
            btn_x    = W - btn_w - PAD
            btn_y    = H - FOOTER - btn_h - 8
            pygame.draw.rect(self.screen, self.COL_CYAN, (btn_x, btn_y, btn_w, btn_h), border_radius=6)
            self.screen.blit(btn_surf, (btn_x + 16, btn_y + 6))

            pygame.display.flip()
            clock.tick(30)

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return ""
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        return ""
                    elif event.key == pygame.K_RETURN:
                        return str(current)
                    elif event.key in (pygame.K_RIGHT, pygame.K_l):
                        if dirs:
                            current = dirs[sel]
                            dirs    = list_dirs(current)
                            sel, scroll = 0, 0
                    elif event.key in (pygame.K_LEFT, pygame.K_BACKSPACE):
                        parent = current.parent
                        if parent != current:
                            old_name = current.name
                            current  = parent
                            dirs     = list_dirs(current)
                            # Try to re-select the folder we came from
                            names = [d.name for d in dirs]
                            sel   = names.index(old_name) if old_name in names else 0
                            scroll = 0
                    elif event.key == pygame.K_UP:
                        sel = max(0, sel - 1)
                    elif event.key == pygame.K_DOWN:
                        sel = min(len(dirs) - 1, sel + 1)
                    elif event.key == pygame.K_PAGEUP:
                        sel = max(0, sel - visible2)
                    elif event.key == pygame.K_PAGEDOWN:
                        sel = min(len(dirs) - 1, sel + visible2)
                    elif event.key == pygame.K_HOME:
                        sel = 0
                    elif event.key == pygame.K_END:
                        sel = max(0, len(dirs) - 1)
                elif event.type in (pygame.MOUSEBUTTONDOWN,):
                    if event.button in (4, 5):
                        sel = max(0, min(len(dirs) - 1,
                            sel + (-1 if event.button == 4 else 1)))
                    elif event.button == 1:
                        mx, my = event.pos
                        # Check "Use this folder" button
                        if btn_x <= mx <= btn_x + btn_w and btn_y <= my <= btn_y + btn_h:
                            return str(current)
                        # Check list click
                        if my >= list_y:
                            clicked = scroll + (my - list_y) // ROW
                            if 0 <= clicked < len(dirs):
                                if clicked == sel:
                                    # Double-click descend
                                    current = dirs[sel]
                                    dirs    = list_dirs(current)
                                    sel, scroll = 0, 0
                                else:
                                    sel = clicked
                elif event.type == pygame.MOUSEWHEEL:
                    sel = max(0, min(len(dirs) - 1, sel - event.y))

    def _reload_library(self):
        self.library    = scan_library(self.lib_folder)
        self.lib_search = ""
        self._apply_filter()
        self.lib_sel    = 0
        self.lib_scroll = 0

    def _apply_filter(self):
        q = self.lib_search.lower()
        self.lib_filtered = (
            [s for s in self.library if q in s.title.lower() or q in s.artist.lower()]
            if q else list(self.library)
        )
        self.lib_sel = min(self.lib_sel, max(0, len(self.lib_filtered) - 1))

    def _lib_play_selected(self):
        if not self.lib_filtered:
            return
        song = self.lib_filtered[self.lib_sel]
        self.load(song.play_path, song)
        self.mode = self.MODE_PLAYER

    # ─────────────────────────────────────────
    # Playback
    # ─────────────────────────────────────────

    def load(self, path: str, song=None):
        self.loaded = False
        pygame.mixer.music.stop()
        if self.vlc:
            self.vlc.detach()
        self.is_video_mode = False
        self.cdg           = None

        p   = Path(path)
        ext = p.suffix.lower()

        if song:
            self.song_title  = song.title
            self.song_artist = song.artist
        else:
            title, artist    = _read_metadata(str(p))
            if not title:
                title, artist = _parse_filename(p.stem)
            self.song_title  = title or p.stem
            self.song_artist = artist

        VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.webm'}

        if ext in VIDEO_EXTS:
            # ── VLC video path ──
            if not HAS_VLC or not self.vlc:
                print("[ERROR] python-vlc not installed. Run: pip install python-vlc", file=sys.stderr)
                return
            # Get the native X11 window ID from pygame
            try:
                import ctypes
                wm_info = pygame.display.get_wm_info()
                hwnd    = wm_info.get("window", 0)
            except Exception:
                hwnd = 0
            ok = self.vlc.load(str(p), hwnd)
            if not ok:
                return
            # Give VLC a moment to open the media and read its duration
            time.sleep(0.3)
            self.vlc.set_volume(self.volume)
            self.duration      = self.vlc.get_duration() or _get_duration(str(p))
            self.is_video_mode = True
            self.seek_offset      = 0.0
            self.elapsed_at_pause = 0.0
            self.paused           = False
            self.loaded           = True
            return

        if ext == '.cdg':
            audio_path = None
            for aext in ('.mp3', '.ogg', '.wav', '.flac'):
                c = p.with_suffix(aext)
                if c.exists():
                    audio_path = str(c)
                    break
            if not audio_path:
                print(f"[ERROR] No audio found for {p.name}", file=sys.stderr)
                return
            try:
                self.cdg = CDGParser(str(p))
            except Exception as e:
                print(f"[ERROR] CDG: {e}", file=sys.stderr)
                return
            self.audio_path = audio_path
        elif ext in ('.mp3', '.ogg', '.wav', '.flac'):
            self.audio_path = path
            self.cdg        = None
        else:
            print(f"[ERROR] Unsupported format: {ext}", file=sys.stderr)
            return

        self.duration = _get_duration(self.audio_path)

        try:
            pygame.mixer.music.load(self.audio_path)
            pygame.mixer.music.set_volume(self.volume)
            pygame.mixer.music.play()
        except Exception as e:
            print(f"[ERROR] Audio: {e}", file=sys.stderr)
            return

        self.seek_offset      = 0.0
        self.elapsed_at_pause = 0.0
        self.paused           = False
        self.loaded           = True

    def get_position(self) -> float:
        if not self.loaded:
            return 0.0
        if self.is_video_mode and self.vlc:
            return self.vlc.get_position()
        if self.paused:
            return self.elapsed_at_pause
        ms = pygame.mixer.music.get_pos()
        return self.elapsed_at_pause if ms < 0 else self.seek_offset + ms / 1000.0

    def toggle_pause(self):
        if not self.loaded:
            return
        if self.is_video_mode and self.vlc:
            if self.paused:
                self.vlc.unpause()
                self.paused = False
            else:
                self.vlc.pause()
                self.paused = True
            return
        if self.paused:
            pygame.mixer.music.unpause()
            self.seek_offset = self.elapsed_at_pause
            self.paused      = False
        else:
            self.elapsed_at_pause = self.get_position()
            pygame.mixer.music.pause()
            self.paused           = True

    def seek(self, delta: float):
        if not self.loaded:
            return
        pos = max(0.0, min(self.get_position() + delta, self.duration))
        if self.is_video_mode and self.vlc:
            self.vlc.seek(pos)
            return
        pygame.mixer.music.stop()
        pygame.mixer.music.play(start=pos)
        if self.cdg:
            self.cdg.seek(pos)
        self.seek_offset = self.elapsed_at_pause = pos
        if self.paused:
            pygame.mixer.music.pause()

    def set_volume(self, delta: float):
        self.volume = max(0.0, min(1.0, self.volume + delta))
        if self.is_video_mode and self.vlc:
            self.vlc.set_volume(self.volume)
        else:
            pygame.mixer.music.set_volume(self.volume)

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        if self.fullscreen:
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            self.screen = pygame.display.set_mode(
                (self.screen_w, self.screen_h), pygame.RESIZABLE)

    # ─────────────────────────────────────────
    # Drawing — shared
    # ─────────────────────────────────────────

    def _draw_starfield(self, W, H, t):
        for sx, sy, spd in self.stars:
            b = int(80 + 60 * math.sin(t * spd * 2))
            pygame.draw.circle(self.screen, (b, b, b + 30), (sx % W, sy % H), 1)

    # ─────────────────────────────────────────
    # Drawing — Player
    # ─────────────────────────────────────────

    def draw_player(self):
        W, H = self.screen.get_size()
        self._update_fonts(H)
        self.screen.fill(self.COL_BG)
        t = time.time()
        self._draw_starfield(W, H, t)

        cdg_rect = pygame.Rect(0, 40, W, H - 120)

        if self.loaded and self.is_video_mode:
            # VLC is rendering video directly into the window beneath pygame.
            # Fill the video area with transparent black so VLC shows through,
            # then draw the UI bars on top.
            self.screen.fill((0, 0, 0), cdg_rect)
        elif self.loaded and self.cdg:
            pos   = self.get_position()
            surf  = self.cdg.render_to(pos)
            scale = min(cdg_rect.width / CDG_WIDTH, cdg_rect.height / CDG_HEIGHT)
            sw, sh = int(CDG_WIDTH * scale), int(CDG_HEIGHT * scale)
            scaled = pygame.transform.scale(surf, (sw, sh))
            bx = cdg_rect.x + (cdg_rect.width  - sw) // 2
            by = cdg_rect.y + (cdg_rect.height - sh) // 2
            pygame.draw.rect(self.screen, self.COL_MAGENTA,
                             (bx - 3, by - 3, sw + 6, sh + 6), 3, border_radius=4)
            self.screen.blit(scaled, (bx, by))
        elif self.loaded:
            self._draw_visualizer(cdg_rect, t)
        else:
            self._draw_welcome(cdg_rect, t, W, H)

        self._draw_top_bar(W)
        self._draw_bottom_bar(W, H, t)

    def _draw_visualizer(self, rect, t):
        cx  = rect.x + rect.width  // 2
        cy  = rect.y + rect.height // 2
        pos = self.get_position()
        bw  = rect.width // 64
        for i in range(64):
            phase = (i / 64) * math.pi * 2
            h     = max(4, int(80 * abs(math.sin(t * 2 + phase + pos * 0.5)) *
                               (0.5 + 0.5 * math.sin(t * 0.7 + phase))))
            hue   = (i / 64 + t * 0.1) % 1.0
            r     = int(128 + 127 * math.sin(hue * math.pi * 2))
            g     = int(128 + 127 * math.sin(hue * math.pi * 2 + 2.1))
            b     = int(128 + 127 * math.sin(hue * math.pi * 2 + 4.2))
            pygame.draw.rect(self.screen, (r, g, b),
                             (rect.x + i * bw + 1, cy - h, bw - 2, h * 2),
                             border_radius=2)
        ts = self.font_large.render(self.song_title, True, self.COL_WHITE)
        self.screen.blit(ts, (cx - ts.get_width() // 2, cy - 120))
        if self.song_artist:
            ar = self.font_med.render(self.song_artist, True, self.COL_DIM)
            self.screen.blit(ar, (cx - ar.get_width() // 2, cy - 88))

    def _draw_welcome(self, rect, t, W, H):
        lines = [
            ("🎤  KARAOKE PLAYER",         self.font_large, self.COL_CYAN),
            ("",                            self.font_med,   self.COL_WHITE),
            ("Press  L  to open library",   self.font_med,   self.COL_WHITE),
            ("or pass a folder on the command line", self.font_small, self.COL_MID),
        ]
        cy = rect.y + rect.height // 2 - 60
        for text, font, color in lines:
            pulse = int(30 * math.sin(t * 2))
            c = tuple(min(255, max(0, v + (pulse if color == self.COL_CYAN else 0)))
                      for v in color)
            s = font.render(text, True, c)
            self.screen.blit(s, (W // 2 - s.get_width() // 2, cy))
            cy += s.get_height() + 8

    def _draw_top_bar(self, W):
        pygame.draw.rect(self.screen, self.COL_DARK, (0, 0, W, 40))
        pygame.draw.line(self.screen, self.COL_MAGENTA, (0, 40), (W, 40), 2)
        label = self.song_title or "No song loaded"
        if self.song_artist:
            label += f"  —  {self.song_artist}"
        s = self.font_med.render(f"♪  {label}", True, self.COL_CYAN)
        self.screen.blit(s, (10, 11))
        vs = self.font_small.render(f"VOL {int(self.volume * 100)}%", True, self.COL_YELLOW)
        self.screen.blit(vs, (W - vs.get_width() - 10, 12))

    def _draw_bottom_bar(self, W, H, t):
        bar_y = H - 80
        pygame.draw.rect(self.screen, self.COL_DARK, (0, bar_y, W, 80))
        pygame.draw.line(self.screen, self.COL_CYAN, (0, bar_y), (W, bar_y), 2)

        pos    = self.get_position() if self.loaded else 0
        py     = bar_y + 14
        margin = 20
        pw     = W - margin * 2
        pygame.draw.rect(self.screen, self.COL_MID, (margin, py, pw, 8), border_radius=4)
        fill = int(pw * min(pos / self.duration, 1.0)) if self.duration > 0 else 0
        if fill > 0:
            pygame.draw.rect(self.screen, self.COL_CYAN, (margin, py, fill, 8), border_radius=4)
        glow = int(200 + 55 * math.sin(t * 4))
        pygame.draw.circle(self.screen, (glow, glow, 255), (margin + fill, py + 4), 7)

        def fmt(s):
            s = max(0, int(s))
            return f"{s // 60}:{s % 60:02d}"
        self.screen.blit(self.font_small.render(fmt(pos), True, self.COL_WHITE),
                         (margin, py + 14))
        te = self.font_small.render(fmt(self.duration), True, self.COL_MID)
        self.screen.blit(te, (W - margin - te.get_width(), py + 14))

        hint = "SPACE pause  ←→ seek  ↑↓ vol  L library  F fullscreen  Q quit"
        hs = self.font_small.render(hint, True, self.COL_MID)
        self.screen.blit(hs, (W // 2 - hs.get_width() // 2, bar_y + 55))
        if self.paused:
            ps = self.font_large.render("⏸ PAUSED", True, self.COL_YELLOW)
            self.screen.blit(ps, (W // 2 - ps.get_width() // 2, bar_y + 28))

    # ─────────────────────────────────────────
    # Drawing — Library Browser
    # ─────────────────────────────────────────

    HEADER_H = 70
    SEARCH_H = 56
    FOOTER_H = 48
    ROW_H    = 54
    PADDING  = 24

    def draw_library(self):
        W, H = self.screen.get_size()
        self._update_fonts(H)
        self.screen.fill(self.COL_BG)
        t = time.time()
        self._draw_starfield(W, H, t)

        LIST_TOP = self.HEADER_H + self.SEARCH_H
        LIST_BOT = H - self.FOOTER_H
        LIST_H   = LIST_BOT - LIST_TOP
        visible  = max(1, LIST_H // self.ROW_H)

        # ── Header ──
        pygame.draw.rect(self.screen, self.COL_DARK, (0, 0, W, self.HEADER_H))
        pygame.draw.line(self.screen, self.COL_MAGENTA,
                         (0, self.HEADER_H), (W, self.HEADER_H), 2)
        pulse = int(20 * math.sin(t * 1.5))
        hc = tuple(max(0, min(255, v + pulse)) for v in self.COL_CYAN)
        hs = self.font_large.render("🎤  SONG LIBRARY", True, hc)
        self.screen.blit(hs, (self.PADDING, (self.HEADER_H - hs.get_height()) // 2))
        cs = self.font_small.render(
            f"{len(self.lib_filtered)} of {len(self.library)} songs",
            True, self.COL_DIM)
        self.screen.blit(cs, (W - cs.get_width() - self.PADDING,
                               (self.HEADER_H - cs.get_height()) // 2))

        # ── Search bar ──
        sy = self.HEADER_H
        pygame.draw.rect(self.screen, (15, 10, 35), (0, sy, W, self.SEARCH_H))
        pygame.draw.line(self.screen, self.COL_MID,
                         (0, sy + self.SEARCH_H), (W, sy + self.SEARCH_H), 1)
        icon = self.font_search.render("🔍", True, self.COL_DIM)
        icon_y = sy + (self.SEARCH_H - icon.get_height()) // 2
        self.screen.blit(icon, (self.PADDING, icon_y))
        display_q  = (self.lib_search + ("▋" if int(t * 2) % 2 == 0 else "")) \
                     if self.lib_search else "Type to search…"
        query_col  = self.COL_WHITE if self.lib_search else self.COL_MID
        qs = self.font_search.render(display_q, True, query_col)
        self.screen.blit(qs, (self.PADDING + icon.get_width() + 8,
                               sy + (self.SEARCH_H - qs.get_height()) // 2))
        fh = self.font_small.render(
            f"TAB: change folder  [{Path(self.lib_folder).name}]",
            True, self.COL_MID)
        self.screen.blit(fh, (W - fh.get_width() - self.PADDING,
                               sy + (self.SEARCH_H - fh.get_height()) // 2))

        # ── Song list ──
        if not self.lib_filtered:
            msg = ("No songs found — press TAB to choose a folder."
                   if not self.library else "No results for that search.")
            ms = self.font_med.render(msg, True, self.COL_DIM)
            self.screen.blit(ms, (W // 2 - ms.get_width() // 2,
                                  LIST_TOP + LIST_H // 2 - 10))
        else:
            # Keep selection visible
            if self.lib_sel < self.lib_scroll:
                self.lib_scroll = self.lib_sel
            elif self.lib_sel >= self.lib_scroll + visible:
                self.lib_scroll = self.lib_sel - visible + 1

            for i in range(visible):
                idx   = self.lib_scroll + i
                if idx >= len(self.lib_filtered):
                    break
                song  = self.lib_filtered[idx]
                ry    = LIST_TOP + i * self.ROW_H
                issel = (idx == self.lib_sel)

                if issel:
                    p2 = int(15 * math.sin(t * 3))
                    bg = tuple(max(0, min(255, v + p2)) for v in self.COL_SEL_BG)
                    pygame.draw.rect(self.screen, bg, (0, ry, W, self.ROW_H))
                    pygame.draw.rect(self.screen, self.COL_CYAN, (0, ry, 4, self.ROW_H))
                elif i % 2 == 0:
                    pygame.draw.rect(self.screen, (12, 8, 28), (0, ry, W, self.ROW_H))

                # Format badge (CDG / MP4 / MKV) — centred vertically in row
                bx       = self.PADDING
                badge_h  = max(18, int(self.ROW_H * 0.5))
                badge_y  = ry + (self.ROW_H - badge_h) // 2
                if song.has_cdg or song.is_video:
                    badge_label = "CDG" if song.has_cdg else Path(song.play_path).suffix[1:].upper()
                    badge_col   = self.COL_MAGENTA if song.has_cdg else (0, 160, 220)
                    badge       = self.font_small.render(badge_label, True, self.COL_BG)
                    bw2         = badge.get_width() + 8
                    pygame.draw.rect(self.screen, badge_col,
                                     (bx, badge_y, bw2, badge_h), border_radius=3)
                    self.screen.blit(badge, (bx + 4, badge_y + (badge_h - badge.get_height()) // 2))
                    bx += bw2 + 8
                else:
                    bx += 4

                # Title — vertically centred
                tc = self.COL_SEL_FG if issel else self.COL_WHITE
                ts = self.font_med.render(song.title, True, tc)
                self.screen.blit(ts, (bx, ry + (self.ROW_H - ts.get_height()) // 2))

                # Artist + duration on the right — vertically centred
                right_x = W - self.PADDING
                if song.duration > 0:
                    d   = int(song.duration)
                    dur = self.font_small.render(
                        f"{d // 60}:{d % 60:02d}", True, self.COL_MID)
                    right_x -= dur.get_width()
                    self.screen.blit(dur, (right_x, ry + (self.ROW_H - dur.get_height()) // 2))
                    right_x -= 16
                if song.artist:
                    ac = self.COL_CYAN if issel else self.COL_DIM
                    ar = self.font_small.render(song.artist, True, ac)
                    right_x -= ar.get_width()
                    self.screen.blit(ar, (right_x, ry + (self.ROW_H - ar.get_height()) // 2))

                pygame.draw.line(self.screen, self.COL_DARK,
                                 (0, ry + self.ROW_H - 1), (W, ry + self.ROW_H - 1), 1)

            # Scrollbar
            if len(self.lib_filtered) > visible:
                sbx  = W - 4
                sbth = max(30, int(LIST_H * visible / len(self.lib_filtered)))
                sby  = LIST_TOP + int(
                    (LIST_H - sbth) * self.lib_scroll /
                    max(1, len(self.lib_filtered) - visible))
                pygame.draw.rect(self.screen, self.COL_MID,
                                 (sbx, LIST_TOP, 4, LIST_H), border_radius=2)
                pygame.draw.rect(self.screen, self.COL_CYAN,
                                 (sbx, sby, 4, sbth), border_radius=2)

        # ── Footer ──
        fy = H - self.FOOTER_H
        pygame.draw.rect(self.screen, self.COL_DARK, (0, fy, W, self.FOOTER_H))
        pygame.draw.line(self.screen, self.COL_CYAN, (0, fy), (W, fy), 1)
        hint = "↑↓ navigate   ENTER play   TAB folder   TYPE to search   ESC back"
        fhs  = self.font_small.render(hint, True, self.COL_MID)
        self.screen.blit(fhs, (W // 2 - fhs.get_width() // 2,
                                fy + (self.FOOTER_H - fhs.get_height()) // 2))

    # ─────────────────────────────────────────
    # Event handlers
    # ─────────────────────────────────────────

    def _handle_library_key(self, event):
        n = len(self.lib_filtered)
        W, H = self.screen.get_size()
        visible = max(1, (H - self.HEADER_H - self.SEARCH_H - self.FOOTER_H) // self.ROW_H)

        if event.key == pygame.K_ESCAPE:
            if self.loaded:
                self.mode = self.MODE_PLAYER
        elif event.key == pygame.K_RETURN:
            self._lib_play_selected()
        elif event.key == pygame.K_TAB:
            self.open_folder_dialog()
        elif event.key == pygame.K_UP:
            self.lib_sel = max(0, self.lib_sel - 1)
        elif event.key == pygame.K_DOWN:
            self.lib_sel = min(n - 1, self.lib_sel + 1)
        elif event.key == pygame.K_PAGEUP:
            self.lib_sel = max(0, self.lib_sel - visible)
        elif event.key == pygame.K_PAGEDOWN:
            self.lib_sel = min(n - 1, self.lib_sel + visible)
        elif event.key == pygame.K_HOME:
            self.lib_sel = 0
        elif event.key == pygame.K_END:
            self.lib_sel = max(0, n - 1)
        elif event.key == pygame.K_BACKSPACE:
            self.lib_search = self.lib_search[:-1]
            self._apply_filter()
        else:
            char = event.unicode
            if char and char.isprintable():
                self.lib_search += char
                self._apply_filter()
                self.lib_sel = 0

    def _stop_video(self):
        """Detach VLC and blank the screen so no video frame bleeds through."""
        if self.vlc:
            self.vlc.detach()
        self.is_video_mode = False
        # Force a full black frame so the library draws over a clean surface
        self.screen.fill((0, 0, 0))
        pygame.display.flip()

    def _handle_player_key(self, event):
        if event.key == pygame.K_q:
            self.running = False
        elif event.key in (pygame.K_ESCAPE, pygame.K_l):
            if not self.library:
                self.open_folder_dialog()
            if self.is_video_mode:
                self._stop_video()
            self.mode = self.MODE_LIBRARY
        elif event.key == pygame.K_SPACE:
            self.toggle_pause()
        elif event.key == pygame.K_LEFT:
            self.seek(-self.SEEK_STEP)
        elif event.key == pygame.K_RIGHT:
            self.seek(self.SEEK_STEP)
        elif event.key == pygame.K_UP:
            self.set_volume(0.05)
        elif event.key == pygame.K_DOWN:
            self.set_volume(-0.05)
        elif event.key == pygame.K_f:
            self.toggle_fullscreen()

    # ─────────────────────────────────────────
    # Main loop
    # ─────────────────────────────────────────

    def run(self):
        if len(sys.argv) > 1:
            arg = sys.argv[1]
            if Path(arg).is_dir():
                self.lib_folder = arg
                self._reload_library()
                self.mode = self.MODE_LIBRARY
            else:
                self.load(arg)
                self.mode = self.MODE_PLAYER
        else:
            self.open_folder_dialog()
            if self.library:
                self.mode = self.MODE_LIBRARY

        while self.running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False

                elif event.type == pygame.KEYDOWN:
                    if self.mode == self.MODE_LIBRARY:
                        self._handle_library_key(event)
                    else:
                        self._handle_player_key(event)

                elif event.type == pygame.VIDEORESIZE:
                    self.screen_w, self.screen_h = event.w, event.h

                elif event.type == pygame.MOUSEBUTTONDOWN:
                    # Buttons 4/5 are legacy scroll wheel — handle as scroll, not click
                    if event.button in (4, 5):
                        if self.mode == self.MODE_LIBRARY:
                            n = len(self.lib_filtered)
                            self.lib_sel = max(0, min(n - 1,
                                self.lib_sel + (-1 if event.button == 4 else 1)))
                    elif self.mode == self.MODE_PLAYER:
                        W, H   = self.screen.get_size()
                        py2    = H - 80 + 14
                        margin = 20
                        mx, my = event.pos
                        if margin <= mx <= W - margin and abs(my - py2) < 20:
                            ratio = (mx - margin) / (W - margin * 2)
                            self.seek(ratio * self.duration - self.get_position())
                    elif self.mode == self.MODE_LIBRARY:
                        W, H   = self.screen.get_size()
                        lt     = self.HEADER_H + self.SEARCH_H
                        mx, my = event.pos
                        if my >= lt:
                            idx = self.lib_scroll + (my - lt) // self.ROW_H
                            if 0 <= idx < len(self.lib_filtered):
                                if idx == self.lib_sel:
                                    self._lib_play_selected()
                                else:
                                    self.lib_sel = idx

                elif event.type == pygame.MOUSEWHEEL:
                    # Modern pygame scroll wheel event (pygame 2.x)
                    if self.mode == self.MODE_LIBRARY:
                        n = len(self.lib_filtered)
                        self.lib_sel = max(0, min(n - 1,
                            self.lib_sel - event.y))
            # Auto-return to library when song ends
            if self.loaded and not self.paused:
                ended = False
                if self.is_video_mode and self.vlc:
                    ended = self.vlc.is_ended()
                elif not pygame.mixer.music.get_busy() and self.get_position() > 5:
                    ended = True
                if ended:
                    self.loaded = False
                    self._stop_video()
                    self.mode   = self.MODE_LIBRARY

            if self.mode == self.MODE_PLAYER:
                self.draw_player()
            else:
                self.draw_library()

            pygame.display.flip()
            self.clock.tick(self.FPS)

        if self.vlc:
            self.vlc.release()
        pygame.quit()
        sys.exit(0)


# ──────────────────────────────────────────────
# Entry Point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    KaraokePlayer().run()
