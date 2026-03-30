#!/usr/bin/env python3
"""ETHER FM — Terminal Radio Receiver"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import random
from typing import Optional

import httpx
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Footer, Label, ListItem, ListView, Static

# ─── Audio player detection ───────────────────────────────────────────────────

MPV = shutil.which("mpv")
FFPLAY = shutil.which("ffplay")

if not MPV and not FFPLAY:
    print("\033[33mWARNING: Neither mpv nor ffplay found. Audio will not work.\033[0m")
    print("Install mpv: https://mpv.io/installation/")
    input("Press Enter to continue anyway…")

# ─── Pixel font ───────────────────────────────────────────────────────────────

PIXEL_FONT: dict[str, list[str]] = {
    "0": ["█▀█", "█ █", "▀▀▀"],
    "1": ["▀█ ", " █ ", "▀▀▀"],
    "2": ["▀▀█", " ▀▄", "▀▀▀"],
    "3": ["▀▀█", " ▀█", "▀▀▀"],
    "4": ["█ █", "▀▀█", "  ▀"],
    "5": ["█▀▀", "▀▀▄", "▀▀▀"],
    "6": ["█▀▀", "█▀▄", "▀▀▀"],
    "7": ["▀▀█", "  █", "  ▀"],
    "8": ["█▀█", "█▀█", "▀▀▀"],
    "9": ["█▀█", "▀▀█", "▀▀▀"],
    ".": ["   ", "  ▁", "   "],
    " ": ["   ", "   ", "   "],
}


def render_pixel_text(text: str) -> list[str]:
    rows = ["", "", ""]
    for ch in text:
        glyph = PIXEL_FONT.get(ch, PIXEL_FONT[" "])
        for i, row in enumerate(glyph):
            rows[i] += row + " "
    return rows


# ─── Dial widget ──────────────────────────────────────────────────────────────

FREQ_MIN = 87.5
FREQ_MAX = 108.0


class DialWidget(Widget):
    """FM dial with click+drag and arrow key scrubbing."""

    DEFAULT_CSS = """
    DialWidget {
        height: 4;
        background: #0e0c09;
    }
    """

    current_freq: reactive[float] = reactive(98.0)
    stations: reactive[list] = reactive([])

    class FreqChanged(Message):
        def __init__(self, freq: float) -> None:
            super().__init__()
            self.freq = freq

    class SnapRequested(Message):
        def __init__(self, freq: float) -> None:
            super().__init__()
            self.freq = freq

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._dragging = False
        self._snap_timer = None

    def _freq_to_x(self, freq: float, width: int) -> int:
        ratio = (freq - FREQ_MIN) / (FREQ_MAX - FREQ_MIN)
        return int(ratio * (width - 1))

    def _x_to_freq(self, x: int, width: int) -> float:
        ratio = x / max(width - 1, 1)
        freq = FREQ_MIN + ratio * (FREQ_MAX - FREQ_MIN)
        return round(max(FREQ_MIN, min(FREQ_MAX, freq)), 1)

    def render(self) -> str:
        width = self.size.width or 80
        freq = self.current_freq
        stations = self.stations

        # Row 1: frequency labels
        label_freqs = [88, 92, 96, 100, 104, 108]
        label_row = [" "] * width
        for lf in label_freqs:
            x = self._freq_to_x(lf, width)
            lbl = str(lf)
            start = max(0, x - len(lbl) // 2)
            for i, ch in enumerate(lbl):
                if start + i < width:
                    label_row[start + i] = ch

        # Row 2: ticks + station diamonds
        tick_row = []
        for px in range(width):
            f = self._x_to_freq(px, width)
            # Find nearest 0.1 MHz
            f_rounded = round(f * 10) / 10
            frac = f_rounded - int(f_rounded)
            if abs(frac) < 0.05 or abs(frac - 1.0) < 0.05:
                tick_row.append("┃")
            elif abs(frac - 0.5) < 0.07:
                tick_row.append("│")
            else:
                tick_row.append("╎")

        # Place station diamonds
        station_freqs = {s["freq"] for s in stations}
        active_freq = freq
        for s in stations:
            x = self._freq_to_x(s["freq"], width)
            if 0 <= x < width:
                if abs(s["freq"] - active_freq) < 0.05:
                    tick_row[x] = "◆"
                else:
                    tick_row[x] = "◇"

        # Row 3: needle
        needle_row = [" "] * width
        nx = self._freq_to_x(freq, width)
        if 0 <= nx < width:
            needle_row[nx] = "▼"

        # Row 4: stem
        stem_row = [" "] * width
        if 0 <= nx < width:
            stem_row[nx] = "┃"

        lines = [
            "".join(label_row),
            "".join(tick_row),
            "".join(needle_row),
            "".join(stem_row),
        ]
        return "\n".join(lines)

    def on_mouse_down(self, event) -> None:
        self._dragging = True
        self.capture_mouse()
        width = self.size.width or 80
        freq = self._x_to_freq(event.x, width)
        self.current_freq = freq
        self.post_message(self.FreqChanged(freq))

    def on_mouse_move(self, event) -> None:
        if self._dragging:
            width = self.size.width or 80
            freq = self._x_to_freq(event.x, width)
            self.current_freq = freq
            self.post_message(self.FreqChanged(freq))

    def on_mouse_up(self, event) -> None:
        if self._dragging:
            self._dragging = False
            self.release_mouse()
            self.post_message(self.SnapRequested(self.current_freq))


# ─── Big frequency display ────────────────────────────────────────────────────

class FreqDisplay(Static):
    """Pixel-font frequency display."""

    DEFAULT_CSS = """
    FreqDisplay {
        height: 3;
        color: #f5a623;
        background: #0e0c09;
        text-align: center;
        content-align: center middle;
    }
    """

    freq_text: reactive[str] = reactive("--.-")

    def render(self) -> str:
        rows = render_pixel_text(self.freq_text)
        return "\n".join(rows)


# ─── Signal meter ─────────────────────────────────────────────────────────────

BARS = " ▁▂▃▄▅▆▇█"

class SignalMeter(Static):
    """Animated 8-bar signal strength meter."""

    DEFAULT_CSS = """
    SignalMeter {
        width: 12;
        color: #39ff14;
        background: #0e0c09;
    }
    """

    base_strength: reactive[int] = reactive(0)
    _displayed: str = ""

    def on_mount(self) -> None:
        self.set_interval(0.4, self._animate)

    def _animate(self) -> None:
        strength = self.base_strength
        if strength == 0:
            self._displayed = " " * 8
        else:
            bars = []
            for _ in range(8):
                jitter = random.randint(-1, 1)
                val = max(0, min(8, strength + jitter))
                bars.append(BARS[val])
            self._displayed = "".join(bars)
        self.refresh()

    def render(self) -> str:
        return self._displayed or " " * 8


# ─── Volume bar ───────────────────────────────────────────────────────────────

class VolumeBar(Static):
    DEFAULT_CSS = """
    VolumeBar {
        width: 14;
        color: #f5a623;
        background: #0e0c09;
    }
    """

    volume: reactive[float] = reactive(0.8)

    def render(self) -> str:
        filled = round(self.volume * 10)
        bar = "█" * filled + "░" * (10 - filled)
        return f"VOL {bar}"


# ─── Station data helpers ─────────────────────────────────────────────────────

def assign_frequencies(stations: list[dict]) -> list[dict]:
    """Spread stations evenly across 87.5–108.0 MHz."""
    n = len(stations)
    if n == 0:
        return []
    if n == 1:
        stations[0]["freq"] = round((FREQ_MIN + FREQ_MAX) / 2, 1)
        return stations
    step = (FREQ_MAX - FREQ_MIN) / (n - 1)
    for i, s in enumerate(stations):
        s["freq"] = round(FREQ_MIN + i * step, 1)
    return stations


def deduplicate(stations: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for s in stations:
        key = s.get("name", "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(s)
    return result


# ─── Main App ─────────────────────────────────────────────────────────────────

APP_CSS = """
Screen {
    background: #0e0c09;
}

#frame {
    border: heavy #3a3428;
    background: #0e0c09;
    margin: 0;
    padding: 0;
}

/* Brand bar */
#brand-bar {
    height: 3;
    background: #1a1610;
    border-bottom: solid #3a3428;
    padding: 0 1;
    align: left middle;
}

#brand-left {
    color: #c8c0b0;
    text-style: bold;
    width: auto;
}

#brand-center {
    color: #5a5548;
    width: 1fr;
    text-align: center;
    content-align: center middle;
}

#location-pill {
    border: tall #f5a623;
    background: #1a1610;
    color: #f5a623;
    padding: 0 1;
    width: auto;
    min-width: 10;
}

/* Display area */
#display-area {
    height: 8;
    background: #0e0c09;
    border-bottom: solid #3a3428;
    padding: 0 1;
}

#display-top-row {
    height: 1;
    align: left middle;
}

#fm-stereo-label {
    color: #f5a623;
    width: auto;
    text-style: bold;
}

#signal-spacer {
    width: 1fr;
}

#freq-display-container {
    height: 3;
    align: center middle;
}

#freq-display {
    width: auto;
    color: #f5a623;
    background: #0e0c09;
    text-align: center;
}

#station-info-line {
    height: 1;
    align: left middle;
    padding: 0 1;
}

#station-name-label {
    color: #f5a623;
    width: 1fr;
}

#station-genre-label {
    color: #7a5010;
    width: auto;
}

/* Dial area */
#dial-area {
    height: 7;
    background: #0e0c09;
    border: tall #3a3428;
    margin: 0 1;
    padding: 1 1;
}

/* Controls */
#controls-row {
    height: 3;
    background: #1a1610;
    border-top: solid #3a3428;
    border-bottom: solid #3a3428;
    align: center middle;
    padding: 0 2;
}

Button {
    background: #211e18;
    border: tall #3a3428;
    color: #c8c0b0;
    margin: 0 1;
    min-width: 8;
}

Button:hover {
    border: tall #7a5010;
    background: #211e18;
}

#play-btn {
    min-width: 10;
}

#play-btn.playing {
    border: tall #f5a623;
    background: #211a08;
    color: #f5a623;
}

#vol-container {
    align: right middle;
    width: auto;
    padding: 0 1;
}

/* Station list */
#stations-header {
    height: 1;
    background: #211e18;
    color: #c8c0b0;
    padding: 0 1;
    border-bottom: solid #3a3428;
}

#station-count {
    color: #7a5010;
    width: auto;
}

#station-list-label {
    color: #c8c0b0;
    width: 1fr;
    text-style: bold;
}

#station-listview {
    background: #0e0c09;
    height: 1fr;
    border: none;
}

ListView > ListItem {
    height: 1;
    padding: 0 1;
    background: #0e0c09;
    color: #c8c0b0;
}

ListView > ListItem:hover {
    background: #1a1610;
}

ListView > ListItem.--highlight {
    background: #211a08;
    color: #f5a623;
}

/* Status bar */
#status-bar {
    height: 1;
    background: #0e0c09;
    border-top: solid #3a3428;
    padding: 0 1;
    align: left middle;
}

#status-msg {
    width: 1fr;
    color: #39ff14;
}

#status-msg.error {
    color: #ff4444;
}

#on-air-label {
    color: #39ff14;
    width: auto;
}

Footer {
    background: #0e0c09;
    color: #5a5548;
    border-top: solid #3a3428;
}
"""


class EtherFM(App):
    """ETHER FM — Terminal Radio Receiver"""

    CSS = APP_CSS

    BINDINGS = [
        Binding("left", "tune_left", "◄ Tune"),
        Binding("right", "tune_right", "Tune ►"),
        Binding("up", "prev_station", "Prev"),
        Binding("down", "next_station", "Next"),
        Binding("space", "play_pause", "Play/Pause"),
        Binding("plus", "vol_up", "Vol+"),
        Binding("equal", "vol_up", "Vol+", show=False),
        Binding("minus", "vol_down", "Vol-"),
        Binding("q", "quit", "Quit"),
    ]

    stations: reactive[list] = reactive([])
    current_station: reactive[Optional[dict]] = reactive(None)
    playing: reactive[bool] = reactive(False)
    volume: reactive[float] = reactive(0.8)
    current_freq: reactive[float] = reactive(98.0)
    status_msg: reactive[str] = reactive("Initializing…")
    status_error: reactive[bool] = reactive(False)
    location: reactive[str] = reactive("??")

    def __init__(self):
        super().__init__()
        self._proc: Optional[subprocess.Popen] = None
        self._snap_handle = None  # asyncio.TimerHandle from call_later

    def compose(self) -> ComposeResult:
        with Container(id="frame"):
            # Brand bar
            with Horizontal(id="brand-bar"):
                yield Label("✦ ETHER", id="brand-left")
                yield Label("FM RECEIVER · MODEL T-88", id="brand-center")
                yield Label("◉ EARTH", id="location-pill")

            # Display area
            with Vertical(id="display-area"):
                with Horizontal(id="display-top-row"):
                    yield Label("FM STEREO", id="fm-stereo-label")
                    yield Label("", id="signal-spacer")
                    yield SignalMeter(id="signal-meter")
                with Horizontal(id="freq-display-container"):
                    yield FreqDisplay(id="freq-display")
                with Horizontal(id="station-info-line"):
                    yield Label("── NO STATION ──", id="station-name-label")
                    yield Label("", id="station-genre-label")

            # Dial
            with Container(id="dial-area"):
                yield DialWidget(id="dial")

            # Controls
            with Horizontal(id="controls-row"):
                yield Button("◄ PREV", id="prev-btn")
                yield Button("▶ PLAY", id="play-btn")
                yield Button("NEXT ►", id="next-btn")
                with Horizontal(id="vol-container"):
                    yield VolumeBar(id="vol-bar")

            # Station list
            with Vertical(id="stations-panel"):
                with Horizontal(id="stations-header"):
                    yield Label("LOCAL STATIONS", id="station-list-label")
                    yield Label("0 stations", id="station-count")
                yield ListView(id="station-listview")

            # Status bar
            with Horizontal(id="status-bar"):
                yield Label("Initializing…", id="status-msg")
                yield Label("", id="on-air-label")

        yield Footer()

    def on_mount(self) -> None:
        self.load_stations()

    @work(exclusive=True, thread=False)
    async def load_stations(self) -> None:
        self._set_status("Fetching location…")
        cc = await self._get_location()
        self.location = cc.upper() if cc else "??"
        try:
            self.query_one("#location-pill", Label).update(f"◉ {self.location}")
        except NoMatches:
            pass

        self._set_status(f"Fetching stations for {self.location}…")
        stations = await self._fetch_stations(cc)

        if not stations:
            self._set_status("API ERROR — no stations found", error=True)
            try:
                self.query_one("#station-count", Label).update("ERROR")
            except NoMatches:
                pass
            lv = self.query_one("#station-listview", ListView)
            lv.clear()
            lv.append(ListItem(Label("  Could not load stations. Check connection.")))
            return

        stations = assign_frequencies(stations)
        self.stations = stations

        # Update dial
        dial = self.query_one("#dial", DialWidget)
        dial.stations = stations

        # Populate list
        lv = self.query_one("#station-listview", ListView)
        lv.clear()
        for s in stations:
            freq_tag = f"{s['freq']:.1f}"
            name = s.get("name", "Unknown")[:36]
            genre = (s.get("tags") or "").split(",")[0].strip()[:16] or "—"
            bitrate = s.get("bitrate", 0)
            br_str = f"{bitrate}k" if bitrate else "—"
            item = ListItem(
                Label(f"[{freq_tag}] {name:<36} {genre:<16} {br_str}")
            )
            item._station_uuid = s["stationuuid"]
            lv.append(item)

        count = len(stations)
        try:
            self.query_one("#station-count", Label).update(f"{count} stations")
        except NoMatches:
            pass

        # Tune to first station
        if stations:
            self._tune_to(stations[0], play=True)

        self._set_status(f"Loaded {count} stations", error=False)

    async def _get_location(self) -> str:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get("http://ip-api.com/json")
                data = r.json()
                return data.get("countryCode", "").lower()
        except Exception:
            return ""

    async def _fetch_stations(self, cc: str) -> list[dict]:
        stations = []
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                if cc:
                    url = f"https://de1.api.radio-browser.info/json/stations/bycountrycodeexact/{cc.upper()}"
                    r = await client.get(url, params={
                        "limit": 40, "hidebroken": "true",
                        "order": "clickcount", "reverse": "true"
                    })
                    stations = r.json()

                if len(stations) < 3:
                    r2 = await client.get(
                        "https://de1.api.radio-browser.info/json/stations/topclick",
                        params={"limit": 30, "hidebroken": "true"}
                    )
                    extra = r2.json()
                    stations = stations + extra

        except Exception as e:
            self._set_status(f"API ERROR: {e}", error=True)
            return []

        stations = deduplicate(stations)
        # Sort by clickcount desc
        stations.sort(key=lambda s: int(s.get("clickcount", 0) or 0), reverse=True)
        return stations

    def _tune_to(self, station: dict, play: bool = False) -> None:
        self.current_station = station
        freq = station["freq"]
        self.current_freq = freq

        # Update dial
        try:
            dial = self.query_one("#dial", DialWidget)
            dial.current_freq = freq
        except NoMatches:
            pass

        # Update freq display
        self._update_freq_display(freq)

        # Update station info
        name = station.get("name", "Unknown")
        genre = (station.get("tags") or "").split(",")[0].strip() or "—"
        try:
            self.query_one("#station-name-label", Label).update(name[:50])
            self.query_one("#station-genre-label", Label).update(genre)
        except NoMatches:
            pass

        # Scroll list to station
        self._highlight_list_item(station)

        if play:
            self._start_playback(station)

    def _update_freq_display(self, freq: float) -> None:
        try:
            fd = self.query_one("#freq-display", FreqDisplay)
            fd.freq_text = f"{freq:.1f}"
            fd.refresh()
        except NoMatches:
            pass

    def _highlight_list_item(self, station: dict) -> None:
        try:
            lv = self.query_one("#station-listview", ListView)
            idx = next((i for i, s in enumerate(self.stations)
                       if s["stationuuid"] == station["stationuuid"]), None)
            if idx is not None:
                lv.index = idx
        except (NoMatches, Exception):
            pass

    def _start_playback(self, station: dict) -> None:
        self._stop_playback()
        url = station.get("url_resolved") or station.get("url", "")
        if not url:
            self._set_status("No stream URL", error=True)
            return

        vol_int = int(self.volume * 100)
        try:
            if MPV:
                self._proc = subprocess.Popen(
                    [MPV, "--no-video", "--no-terminal", f"--volume={vol_int}", url],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            elif FFPLAY:
                self._proc = subprocess.Popen(
                    [FFPLAY, "-nodisp", "-autoexit", "-loglevel", "quiet", url],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            else:
                self._set_status("No audio player found", error=True)
                return

            self.playing = True
            self._update_play_state(True)
            self._set_status(f"Playing: {station.get('name', 'Unknown')}")
        except Exception as e:
            self._set_status(f"STREAM FAILED: {e}", error=True)

    def _stop_playback(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
                self._proc = None
            except Exception:
                pass
        self.playing = False
        self._update_play_state(False)

    def _update_play_state(self, playing: bool) -> None:
        try:
            btn = self.query_one("#play-btn", Button)
            if playing:
                btn.label = "⏸ PAUSE"
                btn.add_class("playing")
            else:
                btn.label = "▶ PLAY"
                btn.remove_class("playing")
        except NoMatches:
            pass

        try:
            meter = self.query_one("#signal-meter", SignalMeter)
            if playing:
                meter.base_strength = 6
            elif self.current_station:
                meter.base_strength = 3
            else:
                meter.base_strength = 0
        except NoMatches:
            pass

        try:
            on_air = self.query_one("#on-air-label", Label)
            on_air.update("ON AIR ◉" if playing else "")
        except NoMatches:
            pass

    def _set_status(self, msg: str, error: bool = False) -> None:
        self.status_msg = msg
        self.status_error = error
        try:
            lbl = self.query_one("#status-msg", Label)
            lbl.update(msg)
            if error:
                lbl.add_class("error")
            else:
                lbl.remove_class("error")
        except NoMatches:
            pass

    def _snap_to_nearest(self, freq: float) -> None:
        if not self.stations:
            return
        nearest = min(self.stations, key=lambda s: abs(s["freq"] - freq))
        if abs(nearest["freq"] - freq) <= 0.35:
            self._tune_to(nearest, play=True)
        else:
            self._update_freq_display(freq)

    # ── Dial events ───────────────────────────────────────────────────────────

    def on_dial_widget_freq_changed(self, event: DialWidget.FreqChanged) -> None:
        self.current_freq = event.freq
        self._update_freq_display(event.freq)

    def on_dial_widget_snap_requested(self, event: DialWidget.SnapRequested) -> None:
        self._snap_to_nearest(event.freq)

    # ── List events ───────────────────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        try:
            uuid = getattr(item, "_station_uuid", None)
            if uuid:
                station = next((s for s in self.stations if s["stationuuid"] == uuid), None)
                if station:
                    self._tune_to(station, play=True)
        except Exception:
            pass

    # ── Button events ─────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "play-btn":
            self.action_play_pause()
        elif event.button.id == "prev-btn":
            self.action_prev_station()
        elif event.button.id == "next-btn":
            self.action_next_station()

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_play_pause(self) -> None:
        if self.playing:
            self._stop_playback()
            self._set_status("Paused")
        else:
            if self.current_station:
                self._start_playback(self.current_station)
            else:
                self._set_status("No station selected", error=True)

    def action_prev_station(self) -> None:
        if not self.stations or not self.current_station:
            return
        idx = next((i for i, s in enumerate(self.stations)
                   if s["stationuuid"] == self.current_station["stationuuid"]), 0)
        new_idx = (idx - 1) % len(self.stations)
        self._tune_to(self.stations[new_idx], play=True)

    def action_next_station(self) -> None:
        if not self.stations or not self.current_station:
            return
        idx = next((i for i, s in enumerate(self.stations)
                   if s["stationuuid"] == self.current_station["stationuuid"]), 0)
        new_idx = (idx + 1) % len(self.stations)
        self._tune_to(self.stations[new_idx], play=True)

    def action_tune_left(self) -> None:
        new_freq = round(max(FREQ_MIN, self.current_freq - 0.1), 1)
        self.current_freq = new_freq
        try:
            self.query_one("#dial", DialWidget).current_freq = new_freq
        except NoMatches:
            pass
        self._update_freq_display(new_freq)
        self._schedule_snap(new_freq)

    def action_tune_right(self) -> None:
        new_freq = round(min(FREQ_MAX, self.current_freq + 0.1), 1)
        self.current_freq = new_freq
        try:
            self.query_one("#dial", DialWidget).current_freq = new_freq
        except NoMatches:
            pass
        self._update_freq_display(new_freq)
        self._schedule_snap(new_freq)

    def _schedule_snap(self, freq: float) -> None:
        if self._snap_handle is not None:
            self._snap_handle.cancel()
        loop = asyncio.get_event_loop()
        self._snap_handle = loop.call_later(0.45, self._snap_to_nearest, freq)

    def action_vol_up(self) -> None:
        self.volume = round(min(1.0, self.volume + 0.1), 1)
        self._update_volume()

    def action_vol_down(self) -> None:
        self.volume = round(max(0.0, self.volume - 0.1), 1)
        self._update_volume()

    def _update_volume(self) -> None:
        try:
            self.query_one("#vol-bar", VolumeBar).volume = self.volume
        except NoMatches:
            pass
        # Update mpv volume if running
        if self._proc and MPV:
            try:
                self._proc.stdin  # mpv IPC not available in this mode; restart would be needed
            except Exception:
                pass

    def on_unmount(self) -> None:
        self._stop_playback()


if __name__ == "__main__":
    app = EtherFM()
    app.run()
