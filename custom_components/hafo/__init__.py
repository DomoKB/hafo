"""The Home Assistant Forecaster integration."""

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .battery_soc_migration import async_finish_soc_migration, async_prepare_soc_migration, async_rollback_soc_migration
from .coordinator import ForecasterCoordinator, create_forecaster

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

type HafoConfigEntry = ConfigEntry[ForecasterCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: HafoConfigEntry) -> bool:
    """Set up Home Assistant Forecaster from a config entry."""
    _LOGGER.info("Setting up forecaster: %s", entry.title)

    migration = await async_prepare_soc_migration(hass, entry)
    coordinator = None
    platforms_started = False
    try:
        coordinator = create_forecaster(hass, entry)
        entry.runtime_data = coordinator
        await coordinator.async_config_entry_first_refresh()
        platforms_started = True
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        await async_finish_soc_migration(hass, entry)
    except (asyncio.CancelledError, Exception):
        # Platform forwarding can swallow sensor-add errors. Unload any partial
        # replacement before returning ownership to the original writer.
        if platforms_started:
            try:
                if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
                    _LOGGER.exception("Could not unload the incomplete forecast sensor for %s", entry.entry_id)
            except (asyncio.CancelledError, Exception):
                _LOGGER.exception("Could not clean up the incomplete forecast platform for %s", entry.entry_id)
        if coordinator is not None:
            try:
                coordinator.cleanup()
            except Exception:
                _LOGGER.exception("Could not clean up the incomplete forecaster for %s", entry.entry_id)
        if migration is not None:
            try:
                await async_rollback_soc_migration(hass, migration)
            except (asyncio.CancelledError, Exception):
                # The migration API refuses a transfer while an entity is still
                # loaded. Keep the old writer disabled if platform unload failed.
                _LOGGER.exception("Could not restore the standalone SOC helper for %s", entry.entry_id)
        raise

    # Register update listener for config changes
    entry.async_on_unload(entry.add_update_listener(async_update_listener))

    _LOGGER.info("Forecaster setup complete: %s", entry.title)
    return True


async def async_update_listener(hass: HomeAssistant, entry: HafoConfigEntry) -> None:
    """Handle options update."""
    _LOGGER.info("Configuration changed, reloading: %s", entry.title)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HafoConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading forecaster: %s", entry.title)

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        # Clean up coordinator resources
        coordinator = entry.runtime_data
        coordinator.cleanup()

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: HafoConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
