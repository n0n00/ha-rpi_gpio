from __future__ import annotations

from . import DOMAIN

import logging
_LOGGER = logging.getLogger(__name__)

from homeassistant.core import HomeAssistant
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_HOMEASSISTANT_START
from homeassistant.exceptions import HomeAssistantError

from typing import Dict
from datetime import timedelta
import gpiod

from gpiod.line import Direction, Value, Bias, Drive, Edge, Clock
EventType = gpiod.EdgeEvent.Type

BIAS = { 
    "UP": Bias.PULL_UP, 
    "DOWN": Bias.PULL_DOWN,
    "DISABLED": Bias.DISABLED,
    "AS_IS": Bias.AS_IS,
}
DRIVE = { 
    "OPEN_DRAIN": Drive.OPEN_DRAIN, 
    "OPEN_SOURCE": Drive.OPEN_SOURCE, 
    "PUSH_PULL": Drive.PUSH_PULL, 
} 

class Hub:

    def __init__(self, hass: HomeAssistant, path: str) -> None:
        """GPIOD Hub"""

        self._path = path
        self._chip :  gpiod.Chip
        self._name = path
        self._id = path
        self._hass = hass
        self._online = False
        self._lines : gpiod.LineRequest = None
        self._config : Dict[int, gpiod.LineSettings] = {}
        self._edge_events = False
        self._entities = {}

        if path:
            # use config
            _LOGGER.debug(f"trying to use configured device: {path}")
            if self.verify_gpiochip(path):
                self._online = True
                self._path = path
        else:
            # discover
            _LOGGER.debug(f"auto discovering gpio device")
            for d in [0,4,1,2,3,5]:
                # rpi3,4 using 0. rpi5 using 4
                path = f"/dev/gpiochip{d}"
                if self.verify_gpiochip(path):
                    self._online = True
                    self._path = path
                    break

        self.verify_online()

        _LOGGER.debug(f"using gpio_device: {self._path}")

        # startup and shutdown triggers of hass
        self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, self.startup)
        self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.cleanup)

    def verify_online(self):
        if not self._online:
            _LOGGER.error("No gpio device detected, bailing out")
            raise HomeAssistantError("No gpio device detected")

    def verify_gpiochip(self, path):
        if not gpiod.is_gpiochip_device(path):
            _LOGGER.debug(f"verify_gpiochip: {path} not a gpiochip_device")
            return False

        _LOGGER.debug(f"verify_gpiochip: {path} is a gpiochip_device")
        self._chip = gpiod.Chip(path)
        info = self._chip.get_info()
        if not "pinctrl" in info.label:
            _LOGGER.debug(f"verify_gpiochip: {path} no pinctrl {info.label}")
            return False

        _LOGGER.debug(f"verify_gpiochip gpiodevice: {path} has pinctrl")
        return True

    def verify_port_ready(self, port: int):
        info = self._chip.get_line_info(port)
        _LOGGER.debug(f"original port info: {info}")
        if info.used and info.consumer != DOMAIN:
            _LOGGER.error(f"Port {port} already in use by {info.consumer}")
            raise HomeAssistantError(f"Port {port} already in use by {info.consumer}")


    async def startup(self, _):
        """Stuff to do after starting."""
        _LOGGER.debug(f"startup {DOMAIN} hub")
        if not self._online:
            _LOGGER.debug(f"integration is not online")
            return
        if not self._config:
            _LOGGER.debug(f"gpiod config is empty")
            return
        
        # setup lines
        try:
            self.update_lines()
        except Exception as e:
            _LOGGER.error(f"Failed to update lines: {e}")
            return    

        if not self._edge_events:
            return

        _LOGGER.debug("Start listener")
        self._hass.loop.add_reader(self._lines.fd, self.handle_events)

    def cleanup(self, _):
        """Stuff to do before stopping."""
        _LOGGER.debug(f"cleanup {DOMAIN} hub")
        if self._config:
            self._config.clear()
        if self._lines:
            self._lines.release()
        if self._chip:
            self._chip.close()
        self._online = False

    @property
    def hub_id(self) -> str:
        """ID for hub"""
        return self._id

    def update_lines(self) -> None:
        if self._lines:
            self._lines.release()

        _LOGGER.debug(f"updating lines: {self._config}")
        self._lines = self._chip.request_lines(
            consumer = DOMAIN,
            config = self._config
        )
        _LOGGER.debug(f"update_lines new lines: {self._lines}")

    def handle_events(self):
        for event in self._lines.read_edge_events():
            _LOGGER.debug(f"Event: {event}")
            self._entities[event.line_offset].handle_event()

    def add_switch(self, entity, port, active_low, bias, drive_mode, init_output_value = True) -> None:
        _LOGGER.debug(f"in add_switch {port}")
        self.verify_online()
        self.verify_port_ready(port)

        self._entities[port] = entity
        self._config[port] = gpiod.LineSettings(
            direction = Direction.OUTPUT,
            bias = BIAS[bias],
            drive = DRIVE[drive_mode],
            active_low = active_low,
            output_value = Value.ACTIVE if init_output_value and entity.is_on else Value.INACTIVE
        )

    def turn_on(self, port) -> None:
        _LOGGER.debug(f"in turn_on {port}")
        self.verify_online()
        self._lines.set_value(port, Value.ACTIVE)

    def turn_off(self, port) -> None:
        _LOGGER.debug(f"in turn_off {port}")
        self.verify_online()
        self._lines.set_value(port, Value.INACTIVE)

    def add_sensor(self, entity, port, active_low, bias, debounce) -> None:
        _LOGGER.debug(f"in add_sensor {port}")
        self.verify_online()
        self.verify_port_ready(port)

        # read current status of the sensor
        line = self._chip.request_lines({ port: {} })
        value = True if line.get_value(port) == Value.ACTIVE else False
        entity.is_on = True if value ^ active_low else False
        line.release()
        _LOGGER.debug(f"current value for port {port}: {entity.is_on}")

        self._entities[port] = entity
        self._config[port] = gpiod.LineSettings(
            direction = Direction.INPUT,
            edge_detection = Edge.BOTH,
            bias = BIAS[bias],
            active_low = active_low,
            debounce_period = timedelta(milliseconds=debounce),
            event_clock = Clock.REALTIME,
            output_value = Value.ACTIVE if entity.is_on else Value.INACTIVE,
        )
        self._edge_events = True

    def get_line_value(self, port, **kwargs):
        return self._lines.get_value(port) == Value.ACTIVE

    def add_cover(self, entity, relay_port, relay_active_low, relay_bias, relay_drive, 
                  state_port, state_bias, state_active_low) -> None:
        _LOGGER.debug(f"in add_cover {relay_port} {state_port}")
        self.add_switch(entity, relay_port, relay_active_low, relay_bias, relay_drive, init_output_value = False)
        self.add_sensor(entity, state_port, state_active_low, state_bias, 50)

