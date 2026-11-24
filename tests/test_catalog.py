"""Check catalog queries against isolated history and unmounted media."""

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from media_archive.catalog import search_catalog
from media_archive.config import load_config
from media_archive.devices import registered_volume
from media_archive.state import DeviceKey, history, open_database, transaction
from media_archive.state.registry import initialize_database
from tests.config_fixtures import registry_values, write_config


class CatalogTests(unittest.TestCase):
    def test_search_filters_the_saved_inventory_without_a_connected_archive(self) -> None:
        root = Path(self.enterContext(TemporaryDirectory()))
        settings = root / "settings.env"
        write_config(settings, registry_values(root))
        config = load_config(settings)
        database = config.app.database_file
        database.parent.mkdir(parents=True)
        initialize_database(config)
        phone = registered_volume(config, DeviceKey("phone", "main"))
        archive = registered_volume(config, DeviceKey("archive", "a", "data"))
        now = datetime.now(timezone.utc)
        with open_database(database, writable=True) as connection, transaction(connection):
            run = history.create_run(connection)
            content = history.record_content(connection, sha256(b"x").hexdigest(), 1)
            history.record_occurrence(connection, "source", "/camera/original.jpg", content_id=content,
                                      device_id=phone.device_id)
            path = "Data/Photos/2025/02.2025/Ło\u0301dz\u0301.jpg"
            known = history.record_occurrence(connection, "archive", path, content_id=content,
                                              volume_id=archive.volume_id)
            quick = history.record_occurrence(connection, "archive", "Data/Photos/2025/02.2025/quick.jpg",
                                              volume_id=archive.volume_id)
            for occurrence in (known, quick):
                history.record_observation(connection, occurrence, run, observed_at=now, present=True, size=1)
        original = database.read_bytes()
        result, = search_catalog(config, source=("phone", "main"), name="ŁÓDŹ", month="2025-02")
        self.assertEqual(result["ścieżka"], path)
        self.assertEqual(result["nośnik"], "archive:data:a")
        self.assertIn("ostatnia znana lokalizacja", result["dostępność"])
        self.assertEqual(result["sha256"], sha256(b"x").hexdigest())
        self.assertEqual(search_catalog(config, source=("phone", "secondary")), [])
        self.assertEqual(search_catalog(config, month="2025-03"), [])
        unknown, = search_catalog(config, name="quick", month="2025-02")
        self.assertIsNone(unknown["sha256"])
        self.assertEqual(database.read_bytes(), original)
