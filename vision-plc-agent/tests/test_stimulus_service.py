import pytest

from agent.io_map import load_io_map
from agent.stimulus import HttpStimulus, StimulusError
from pi.gpio import FakeGpio
from pi.stimulus_service.service import StimulusService, serve


@pytest.fixture()
def live():
    gpio = FakeGpio()
    server = serve(StimulusService(load_io_map(), gpio), "127.0.0.1", 0)
    try:
        port = server.server_address[1]
        with HttpStimulus(f"http://127.0.0.1:{port}") as stim:
            yield stim, gpio
    finally:
        server.shutdown()
        server.server_close()


def test_press_drives_pin_low_and_release_lets_go(live) -> None:
    stim, gpio = live
    stim.press("start_btn")
    assert gpio.modes[26] == "output"
    assert gpio.levels[26] is False
    stim.release("start_btn")
    assert gpio.modes[26] == "input"


def test_unknown_button_is_a_clean_error(live) -> None:
    stim, _gpio = live
    with pytest.raises(StimulusError, match="unknown button"):
        stim.press("nope")
