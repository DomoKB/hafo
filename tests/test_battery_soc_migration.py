"""SOC migration preserves identity and has only one writer after failures."""

import asyncio
from collections.abc import Generator
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityInfo, entity_sources
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hafo import async_setup_entry
from custom_components.hafo.battery_soc_migration import (
    LEGACY_DOMAIN,
    MIGRATION_RECOVERY,
    MIGRATION_SOURCE,
    async_finish_soc_migration,
    async_prepare_soc_migration,
    async_rollback_soc_migration,
)
from custom_components.hafo.const import DOMAIN


@dataclass
class MigrationCase:
    """Use real HA registries with controlled legacy/platform lifecycle calls."""

    hass: HomeAssistant
    registry: er.EntityRegistry
    original: er.RegistryEntry
    legacy: MockConfigEntry
    target: MockConfigEntry
    coordinator: MagicMock
    stop: AsyncMock
    forward: AsyncMock
    unload: AsyncMock

    def load_target(self) -> None:
        """Emulate a completed sensor add, including its HA source ownership."""
        entity_sources(self.hass)[self.original.entity_id] = {
            "domain": DOMAIN,
            "config_entry": self.target.entry_id,
        }
        # Unknown is a valid loaded sensor when an upstream input is unavailable.
        self.hass.states.async_set(self.original.entity_id, "unknown", {"unit_of_measurement": "%"})

    def assert_original_restored(self) -> None:
        """Check stable recorder identity, measured source and a single writer."""
        registered = self.registry.async_get(self.original.entity_id)
        assert registered is not None
        assert registered.id == self.original.id
        assert registered.entity_id == self.original.entity_id
        assert registered.unique_id == self.original.unique_id
        assert registered.platform == LEGACY_DOMAIN
        assert registered.config_entry_id == self.legacy.entry_id
        assert registered.disabled_by == self.original.disabled_by
        assert self.legacy.disabled_by is None
        assert self.target.data["source_entity"] == "sensor.measured_soc"
        assert entity_sources(self.hass)[registered.entity_id].get("config_entry") == self.legacy.entry_id


@pytest.fixture
def migration_case(hass: HomeAssistant, entity_registry: er.EntityRegistry) -> Generator[MigrationCase]:
    """Create an active legacy sensor without importing the retired integration."""
    legacy = MockConfigEntry(
        domain=LEGACY_DOMAIN,
        title="Original SOC",
        entry_id="legacy_soc_entry",
        data={"soc_entity": "sensor.measured_soc"},
    )
    target = MockConfigEntry(
        domain=DOMAIN,
        title="Battery SOC",
        entry_id="hafo_soc_entry",
        data={
            "source_entity": "sensor.measured_soc",
            "forecast_type": "battery_soc",
            MIGRATION_SOURCE: legacy.entry_id,
        },
    )
    legacy.add_to_hass(hass)
    target.add_to_hass(hass)
    original = entity_registry.async_get_or_create(
        "sensor",
        LEGACY_DOMAIN,
        f"{legacy.entry_id}_soc_forecast",
        config_entry=legacy,
        suggested_object_id="sigen_soc_forecast",
    )
    entity_sources(hass)[original.entity_id] = {"domain": LEGACY_DOMAIN, "config_entry": legacy.entry_id}
    hass.states.async_set(original.entity_id, "45.0", {"unit_of_measurement": "%"})
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()

    async def set_disabled(entry_id: str, disabled_by: ConfigEntryDisabler | None) -> bool:
        assert entry_id == legacy.entry_id
        legacy.disabled_by = disabled_by
        if disabled_by is not None:
            entity_sources(hass).pop(original.entity_id, None)
            hass.states.async_remove(original.entity_id)
            for entity in er.async_entries_for_config_entry(entity_registry, legacy.entry_id):
                if not entity.disabled_by:
                    entity_registry.async_update_entity(
                        entity.entity_id, disabled_by=er.RegistryEntryDisabler.CONFIG_ENTRY
                    )
        else:
            entity_sources(hass)[original.entity_id] = {"domain": LEGACY_DOMAIN, "config_entry": legacy.entry_id}
            hass.states.async_set(original.entity_id, "45.0", {"unit_of_measurement": "%"})
        return True

    async def forward(*_args: object) -> None:
        case.load_target()

    async def unload(*_args: object) -> bool:
        source = entity_sources(hass).get(original.entity_id)
        if source and source.get("config_entry") == target.entry_id:
            entity_sources(hass).pop(original.entity_id)
            hass.states.async_remove(original.entity_id)
        return True

    stop = AsyncMock(side_effect=set_disabled)
    add = AsyncMock(side_effect=forward)
    remove = AsyncMock(side_effect=unload)
    case = MigrationCase(hass, entity_registry, original, legacy, target, coordinator, stop, add, remove)
    with (
        patch.object(hass.config_entries, "async_set_disabled_by", stop),
        patch.object(hass.config_entries, "async_forward_entry_setups", add),
        patch.object(hass.config_entries, "async_unload_platforms", remove),
        # The real config-entry removal still runs registry cleanup. Only its
        # integration-specific hook is stubbed because the old package is absent.
        patch.object(legacy, "async_remove", new_callable=AsyncMock),
        patch("custom_components.hafo.create_forecaster", return_value=coordinator),
    ):
        yield case
    entity_sources(hass).pop(original.entity_id, None)


async def test_success_preserves_entity_and_registry_identity(migration_case: MigrationCase) -> None:
    """Removing the old entry leaves the original recorder/statistic key intact."""
    case = migration_case
    assert await async_setup_entry(case.hass, case.target)
    migrated = case.registry.async_get(case.original.entity_id)
    assert migrated is not None
    assert migrated.id == case.original.id
    assert migrated.entity_id == case.original.entity_id
    assert migrated.platform == DOMAIN
    assert migrated.config_entry_id == case.target.entry_id
    assert migrated.unique_id == f"{case.target.entry_id}_forecast"
    assert case.hass.config_entries.async_get_entry(case.legacy.entry_id) is None
    assert MIGRATION_SOURCE not in case.target.data
    assert MIGRATION_RECOVERY not in case.target.data
    assert case.target.data["source_entity"] == "sensor.measured_soc"
    assert len(er.async_entries_for_config_entry(case.registry, case.target.entry_id)) == 1


async def test_missing_loaded_sensor_keeps_original_helper(migration_case: MigrationCase) -> None:
    """HA can swallow sensor-add errors, so registry transfer alone cannot commit."""
    case = migration_case
    case.forward.side_effect = None
    with pytest.raises(ConfigEntryError, match="has not loaded successfully"):
        await async_setup_entry(case.hass, case.target)
    case.assert_original_restored()
    case.coordinator.cleanup.assert_called_once()
    case.unload.assert_awaited_once()


@pytest.mark.parametrize("bad_source", [{"domain": LEGACY_DOMAIN}, {"domain": DOMAIN, "config_entry": "other"}])
async def test_source_ownership_must_match_before_removal(
    migration_case: MigrationCase, bad_source: EntityInfo
) -> None:
    """An old/restored state or another helper cannot masquerade as the new sensor."""
    case = migration_case
    migration = await async_prepare_soc_migration(case.hass, case.target)
    assert migration is not None
    case.load_target()
    entity_sources(case.hass)[case.original.entity_id] = bad_source
    with pytest.raises(ConfigEntryError, match="has not loaded successfully"):
        await async_finish_soc_migration(case.hass, case.target)
    assert case.hass.config_entries.async_get_entry(case.legacy.entry_id) is case.legacy
    entity_sources(case.hass).pop(case.original.entity_id)
    await async_rollback_soc_migration(case.hass, migration)
    case.assert_original_restored()


async def test_cancel_during_disable_waits_and_restores(migration_case: MigrationCase) -> None:
    """Cancellation cannot interrupt HA halfway through unloading the old writer."""
    case = migration_case
    entered, release = asyncio.Event(), asyncio.Event()
    original_stop = case.stop.side_effect

    async def delayed_disable(entry_id: str, disabled_by: ConfigEntryDisabler | None) -> bool:
        result = await original_stop(entry_id, disabled_by)
        if disabled_by is not None:
            entered.set()
            await release.wait()
        return result

    case.stop.side_effect = delayed_disable
    task = asyncio.create_task(async_prepare_soc_migration(case.hass, case.target))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    case.assert_original_restored()


async def test_cancel_during_first_refresh_restores(migration_case: MigrationCase) -> None:
    """A cancelled refresh cleans up its subscription and re-enables the old helper."""
    case = migration_case
    entered = asyncio.Event()

    async def wait_for_cancel() -> None:
        entered.set()
        await asyncio.Event().wait()

    case.coordinator.async_config_entry_first_refresh.side_effect = wait_for_cancel
    task = asyncio.create_task(async_setup_entry(case.hass, case.target))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    case.assert_original_restored()
    case.coordinator.cleanup.assert_called_once()
    case.forward.assert_not_awaited()


async def test_cancel_after_sensor_add_unloads_before_restoring(migration_case: MigrationCase) -> None:
    """Never re-enable the old writer while the new sensor is still loaded."""
    case = migration_case
    entered = asyncio.Event()

    async def interrupted_forward(*_args: object) -> None:
        case.load_target()
        entered.set()
        await asyncio.Event().wait()

    case.forward.side_effect = interrupted_forward
    task = asyncio.create_task(async_setup_entry(case.hass, case.target))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    case.unload.assert_awaited_once()
    case.assert_original_restored()
    case.coordinator.cleanup.assert_called_once()


async def test_removal_failure_restores_then_retry_succeeds(migration_case: MigrationCase) -> None:
    """A failed final removal is covered by the same platform/coordinator cleanup."""
    case = migration_case
    with (
        patch.object(case.hass.config_entries, "async_remove", side_effect=RuntimeError("removal failed")),
        pytest.raises(RuntimeError, match="removal failed"),
    ):
        await async_setup_entry(case.hass, case.target)
    case.assert_original_restored()
    case.coordinator.cleanup.assert_called_once()
    assert MIGRATION_RECOVERY in case.target.data
    assert await async_setup_entry(case.hass, case.target)
    assert case.hass.config_entries.async_get_entry(case.legacy.entry_id) is None


async def test_failure_after_removal_commits_identity_and_retry_resumes(migration_case: MigrationCase) -> None:
    """A removed legacy entry is never resurrected with a second writer."""
    case = migration_case
    actual_remove = case.hass.config_entries.async_remove

    async def removed_then_failed(entry_id: str) -> None:
        await actual_remove(entry_id)
        message = "post-removal cleanup failed"
        raise RuntimeError(message)

    with (
        patch.object(case.hass.config_entries, "async_remove", side_effect=removed_then_failed),
        pytest.raises(RuntimeError, match="post-removal cleanup failed"),
    ):
        await async_setup_entry(case.hass, case.target)
    assert case.hass.config_entries.async_get_entry(case.legacy.entry_id) is None
    transferred = case.registry.async_get(case.original.entity_id)
    assert transferred is not None
    assert transferred.config_entry_id == case.target.entry_id
    assert transferred.id == case.original.id
    assert case.original.entity_id not in entity_sources(case.hass)
    assert await async_setup_entry(case.hass, case.target)
    assert MIGRATION_RECOVERY not in case.target.data
    assert len(er.async_entries_for_config_entry(case.registry, case.target.entry_id)) == 1


async def test_restart_after_transfer_reuses_saved_recovery(migration_case: MigrationCase) -> None:
    """A new setup invocation recovers original settings from persisted entry data."""
    case = migration_case
    first = await async_prepare_soc_migration(case.hass, case.target)
    assert first is not None
    assert case.legacy.disabled_by is ConfigEntryDisabler.USER
    # A fresh invocation has no in-memory migration object from the first setup.
    resumed = await async_prepare_soc_migration(case.hass, case.target)
    assert resumed is not None
    assert resumed.disabled_by is None
    assert resumed.registry_id == first.registry_id
    await async_rollback_soc_migration(case.hass, resumed)
    case.assert_original_restored()
    assert await async_setup_entry(case.hass, case.target)


async def test_unload_failure_does_not_start_duplicate_writer(migration_case: MigrationCase) -> None:
    """If the new sensor cannot unload, retain its ownership and stop the old one."""
    case = migration_case

    async def loaded_then_failed(*_args: object) -> None:
        case.load_target()
        message = "forward failed after add"
        raise RuntimeError(message)

    case.forward.side_effect = loaded_then_failed
    case.unload.side_effect = None
    case.unload.return_value = False
    with pytest.raises(RuntimeError, match="forward failed after add"):
        await async_setup_entry(case.hass, case.target)
    assert case.legacy.disabled_by is ConfigEntryDisabler.USER
    assert entity_sources(case.hass)[case.original.entity_id].get("config_entry") == case.target.entry_id
    registered = case.registry.async_get(case.original.entity_id)
    assert registered is not None
    assert registered.config_entry_id == case.target.entry_id
    assert case.stop.await_count == 1
    case.coordinator.cleanup.assert_called_once()


async def test_coordinator_cleanup_failure_still_restores(migration_case: MigrationCase) -> None:
    """A secondary cleanup exception does not hide the cause or prevent rollback."""
    case = migration_case
    case.forward.side_effect = RuntimeError("primary setup error")
    case.coordinator.cleanup.side_effect = RuntimeError("cleanup error")
    with pytest.raises(RuntimeError, match="primary setup error"):
        await async_setup_entry(case.hass, case.target)
    case.assert_original_restored()


async def test_different_battery_cannot_take_existing_history(migration_case: MigrationCase) -> None:
    """Identity migration is restricted to the same original measured SOC source."""
    case = migration_case
    case.hass.config_entries.async_update_entry(case.target, options={"source_entity": "sensor.another_soc"})
    with pytest.raises(ConfigEntryError, match="different battery"):
        await async_prepare_soc_migration(case.hass, case.target)
    case.stop.assert_not_awaited()
    assert case.registry.async_get(case.original.entity_id) == case.original
