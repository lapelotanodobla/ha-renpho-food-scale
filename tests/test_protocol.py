"""Protocol tests against real captures from an ES-SNG01 (rev N3, 2026-11-19)."""

import importlib.util
import pathlib
import re

ROOT = pathlib.Path(__file__).parent
_spec = importlib.util.spec_from_file_location(
    "protocol", ROOT.parent / "custom_components/renpho_food_scale/protocol.py"
)
protocol = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(protocol)

FRAME_RE = re.compile(r"(55 aa(?: [0-9a-f]{2})+)")


def frames(name):
    """Every 55 AA notification in a capture log, in order."""
    out = []
    for line in (ROOT / "fixtures" / name).read_text().splitlines():
        if "->" in line:  # frames we wrote, not notifications
            continue
        m = FRAME_RE.search(line)
        if m and ("1a12" in line or "<-" in line):
            out.append(bytes.fromhex(m.group(1)))
    return out


def events(name):
    state = protocol.ScaleState()
    evs = []
    for raw in frames(name):
        frame = protocol.parse(raw)
        assert frame is not None, raw.hex(" ")
        evs += state.feed(frame)
    return state, evs


def test_every_captured_frame_parses():
    for name in ("dump-1318.log", "units-1328.log", "tare-app.log"):
        assert frames(name), name
        assert all(protocol.parse(f) for f in frames(name))


def test_parse_rejects_bad_checksum_and_length():
    good = bytes.fromhex("55aa1000040000004457")
    assert protocol.parse(good) == protocol.Frame(0x10, bytes.fromhex("00000044"))
    assert protocol.parse(good[:-1] + b"\x00") is None
    assert protocol.parse(good[:-2]) is None
    assert protocol.parse(b"\x00" + good[1:]) is None


def test_commands_match_the_ones_the_scale_accepted():
    assert protocol.tare_command().hex(" ") == "55 aa 97 00 01 01 98"
    assert protocol.set_unit_command("oz").hex(" ") == "55 aa 93 00 01 00 93"
    assert protocol.set_unit_command("g").hex(" ") == "55 aa 93 00 01 03 96"


def test_first_session_weighings_and_scale_tares():
    state, evs = events("dump-1318.log")
    weighed = [e.data["grams"] for e in evs if e.type == "weighed"]
    # glass 68 g, glass+corkscrew 169/170 g, negatives after taring with things removed
    assert 68 in weighed and 170 in weighed and -69 in weighed and -170 in weighed
    assert 0 not in weighed
    tares = [e for e in evs if e.type == "tare"]
    assert len(tares) == 3 and all(e.data["source"] == "scale" for e in tares)
    assert state.stable_grams == 0 and state.unit == "g"


def test_unit_cycle_and_weight_stays_grams():
    state, evs = events("units-1328.log")
    units = [e.data["unit"] for e in evs if e.type == "unit_changed"]
    assert units[:5] == ["lb_oz", "fl_oz", "oz", "ml", "g"]
    # 170 g in every unit: the weight payload never changes with the display unit
    assert {e.data["grams"] for e in evs if e.type == "weighed"} <= {169, 170}


def test_tare_from_app_is_marked_app():
    _, evs = events("tare-app.log")
    assert [e.data for e in evs if e.type == "tare"] == [{"source": "app"}]


def test_repeated_stable_frames_are_one_weighing():
    state = protocol.ScaleState()
    stable = protocol.parse(bytes.fromhex("55aa10000400000044 57".replace(" ", "")))
    assert [e.type for e in state.feed(stable)] == ["weighed"]
    assert state.feed(stable) == []
    state.disconnected()
    assert state.live_grams is None
    assert [e.type for e in state.feed(stable)] == ["weighed"]  # new session counts again
