"""Transfer the former standalone SOC sensor through HA's registry API."""

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import entity_sources

from .const import CONF_SOURCE_ENTITY, DOMAIN

LEGACY_DOMAIN = "sigen_soc_forecast"
MIGRATION_SOURCE = "migration_source_entry_id"
MIGRATION_RECOVERY = "migration_recovery"
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SocMigration:
    """Persistable identity and settings needed to restore the former helper."""

    legacy_entry: ConfigEntry
    target_entry_id: str
    entity_id: str
    registry_id: str
    unique_id: str
    entity_disabled_by: er.RegistryEntryDisabler | None
    disabled_by: ConfigEntryDisabler | None


async def _async_complete_operation[T](hass: HomeAssistant, operation: Coroutine[Any, Any, T], name: str) -> T:
    """Finish a registry/config transaction before propagating cancellation."""
    task = hass.async_create_task(operation, name)
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancelled = error
    result = task.result()
    if cancelled is not None:
        raise cancelled
    return result


def _restore_details(
    hass: HomeAssistant, entry: ConfigEntry, legacy: ConfigEntry, entity: er.RegistryEntry
) -> SocMigration:
    """Reuse the saved pre-transfer state after a restart or an interrupted retry."""
    recovery = entry.data.get(MIGRATION_RECOVERY)
    if recovery is None:
        if entity.platform != LEGACY_DOMAIN or entity.config_entry_id != legacy.entry_id:
            message = "The SOC migration has no recovery record for its transferred sensor"
            raise ConfigEntryError(message)
        recovery = {
            "entity_id": entity.entity_id,
            "registry_id": entity.id,
            "unique_id": entity.unique_id,
            "entity_disabled_by": entity.disabled_by,
            "disabled_by": legacy.disabled_by,
        }
        # Save before stopping the original writer, so a restart can resume safely.
        hass.config_entries.async_update_entry(entry, data={**entry.data, MIGRATION_RECOVERY: recovery})
    if (
        not isinstance(recovery, dict)
        or recovery.get("entity_id") != entity.entity_id
        or recovery.get("registry_id") != entity.id
        or recovery.get("unique_id") != f"{legacy.entry_id}_soc_forecast"
    ):
        message = "The SOC migration recovery record does not match the original sensor"
        raise ConfigEntryError(message)
    try:
        entity_disabled_by = recovery.get("entity_disabled_by")
        disabled_by = recovery.get("disabled_by")
        return SocMigration(
            legacy_entry=legacy,
            target_entry_id=entry.entry_id,
            entity_id=entity.entity_id,
            registry_id=entity.id,
            unique_id=f"{legacy.entry_id}_soc_forecast",
            entity_disabled_by=er.RegistryEntryDisabler(entity_disabled_by) if entity_disabled_by else None,
            disabled_by=ConfigEntryDisabler(disabled_by) if disabled_by else None,
        )
    except ValueError as error:
        message = "The SOC migration recovery record has invalid disabled settings"
        raise ConfigEntryError(message) from error


async def async_prepare_soc_migration(hass: HomeAssistant, entry: ConfigEntry) -> SocMigration | None:
    """Stop the old writer and transfer ownership without changing entity ID."""
    legacy_id = entry.data.get(MIGRATION_SOURCE)
    if not legacy_id:
        return None
    registry = er.async_get(hass)
    target_unique = f"{entry.entry_id}_forecast"
    existing = registry.async_get_entity_id("sensor", DOMAIN, target_unique)
    existing_entity = registry.async_get(existing) if existing else None
    if existing and (existing_entity is None or existing_entity.config_entry_id != entry.entry_id):
        message = "The migrated SOC sensor belongs to another helper"
        raise ConfigEntryError(message)
    legacy = hass.config_entries.async_get_entry(legacy_id)
    if legacy is None:
        if existing:
            # Removal committed before the prior setup/restart finished.
            return None
        message = "The standalone SOC helper to migrate no longer exists"
        raise ConfigEntryError(message)
    if legacy.domain != LEGACY_DOMAIN:
        message = "The SOC migration source is not a standalone SOC helper"
        raise ConfigEntryError(message)
    settings = {**entry.data, **entry.options}
    if {**legacy.data, **legacy.options}.get("soc_entity") != settings[CONF_SOURCE_ENTITY]:
        message = "The standalone SOC helper uses a different battery"
        raise ConfigEntryError(message)
    if existing:
        old_entity = existing_entity
    else:
        entities = er.async_entries_for_config_entry(registry, legacy.entry_id)
        if len(entities) != 1 or entities[0].unique_id != f"{legacy.entry_id}_soc_forecast":
            message = "The standalone SOC helper does not have exactly one expected forecast sensor"
            raise ConfigEntryError(message)
        old_entity = entities[0]
    if old_entity is None:
        message = "The SOC forecast sensor registry entry is missing"
        raise ConfigEntryError(message)
    migration = _restore_details(hass, entry, legacy, old_entity)
    try:
        stopped = await _async_complete_operation(
            hass,
            hass.config_entries.async_set_disabled_by(legacy.entry_id, ConfigEntryDisabler.USER),
            "Stop standalone SOC helper for migration",
        )
        if not stopped:
            message = "The standalone SOC helper could not be stopped"
            raise ConfigEntryError(message)
        if not existing:
            registry.async_update_entity_platform(
                old_entity.entity_id, DOMAIN, new_config_entry_id=entry.entry_id, new_unique_id=target_unique
            )
        registry.async_update_entity(old_entity.entity_id, disabled_by=migration.entity_disabled_by)
    except (asyncio.CancelledError, Exception):
        await async_rollback_soc_migration(hass, migration)
        raise
    return migration


async def async_rollback_soc_migration(hass: HomeAssistant, migration: SocMigration) -> None:
    """Restore an unloaded sensor, unless removal of the old entry committed."""
    if hass.config_entries.async_get_entry(migration.legacy_entry.entry_id) is None:
        return
    registry = er.async_get(hass)
    current = registry.async_get(migration.entity_id)
    if current is None or current.id != migration.registry_id:
        message = "SOC rollback refused: the original sensor identity is no longer present"
        raise ConfigEntryError(message)
    if current.platform == DOMAIN and current.config_entry_id == migration.target_entry_id:
        if migration.entity_id in entity_sources(hass):
            message = "SOC rollback refused: the replacement forecast sensor is still loaded"
            raise ConfigEntryError(message)
        registry.async_update_entity_platform(
            migration.entity_id,
            LEGACY_DOMAIN,
            new_config_entry_id=migration.legacy_entry.entry_id,
            new_unique_id=migration.unique_id,
        )
    elif (
        current.platform != LEGACY_DOMAIN
        or current.config_entry_id != migration.legacy_entry.entry_id
        or current.unique_id != migration.unique_id
    ):
        message = "SOC rollback refused: another helper owns the original sensor"
        raise ConfigEntryError(message)
    registry.async_update_entity(migration.entity_id, disabled_by=migration.entity_disabled_by)
    restored = await _async_complete_operation(
        hass,
        hass.config_entries.async_set_disabled_by(migration.legacy_entry.entry_id, migration.disabled_by),
        "Restore standalone SOC helper after failed migration",
    )
    if not restored:
        message = "The standalone SOC helper could not be restarted after migration failed"
        raise ConfigEntryError(message)


async def async_finish_soc_migration(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the stopped legacy entry only after its replacement really loaded."""
    legacy_id = entry.data.get(MIGRATION_SOURCE)
    if not legacy_id:
        return
    registry = er.async_get(hass)
    target = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_forecast")
    registered = registry.async_get(target) if target else None
    source = entity_sources(hass).get(target) if target else None
    state = hass.states.get(target) if target else None
    recovery = entry.data.get(MIGRATION_RECOVERY)
    if (
        registered is None
        or registered.config_entry_id != entry.entry_id
        or not source
        or source.get("domain") != DOMAIN
        or source.get("config_entry") != entry.entry_id
        or state is None
        or state.attributes.get("restored")
        or (recovery and (registered.id != recovery["registry_id"] or target != recovery["entity_id"]))
    ):
        message = "SOC migration refused: the transferred forecast sensor has not loaded successfully"
        raise ConfigEntryError(message)
    legacy = hass.config_entries.async_get_entry(legacy_id)
    if legacy is not None:
        if (
            legacy.domain != LEGACY_DOMAIN
            or legacy.disabled_by is None
            or er.async_entries_for_config_entry(registry, legacy.entry_id)
            or any(info.get("config_entry") == legacy.entry_id for info in entity_sources(hass).values())
        ):
            message = "Legacy SOC helper removal refused: its sensor ownership has not been transferred"
            raise ConfigEntryError(message)
        await _async_complete_operation(
            hass, hass.config_entries.async_remove(legacy.entry_id), "Remove migrated standalone SOC helper"
        )
        if hass.config_entries.async_get_entry(legacy.entry_id) is not None:
            message = "The standalone SOC helper was not removed"
            raise ConfigEntryError(message)
    data = {key: value for key, value in entry.data.items() if key not in (MIGRATION_SOURCE, MIGRATION_RECOVERY)}
    hass.config_entries.async_update_entry(entry, data=data)
    _LOGGER.info("Migrated SOC helper to HAFO and retained entity %s", target)
