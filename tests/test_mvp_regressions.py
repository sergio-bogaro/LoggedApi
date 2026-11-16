"""Regressões do MVP local: datas, logs, tags e importação MAL."""

import asyncio
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Precisa rodar antes de importar config/database/main.
_TMP_DIR = Path(tempfile.mkdtemp(prefix="logged-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DIR.as_posix()}/test.db"
os.environ["UPLOAD_DIR"] = str(_TMP_DIR / "uploads")
# Os testes controlam o worker manualmente; o loop global fica desligado.
os.environ["IMPORT_JOB_WORKER_ENABLED"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402
from schemas.media import MediaCreate  # noqa: E402
from services.importer.providers.letterboxd import LetterboxdProvider
from services.importer.providers.mal import MalProvider  # noqa: E402
from services.openlibrary_service import OpenLibraryService  # noqa: E402
from models.enums import MediaTypeEnum  # noqa: E402


class ApiRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app, raise_server_exceptions=False)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)

    def _user(self) -> int:
        username = f"user_{uuid.uuid4().hex[:10]}"
        response = self.client.post(
            "/auth/register", json={"username": username, "password": "secret"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def test_partial_and_empty_dates_are_accepted(self) -> None:
        user_id = self._user()

        year_only = self.client.post(
            "/api/media/",
            json={
                "userId": user_id,
                "title": "Book with year",
                "type": "book",
                "externalId": f"book-{uuid.uuid4().hex}",
                "releaseDate": "1995",
            },
        )
        self.assertEqual(year_only.status_code, 201, year_only.text)
        self.assertTrue(year_only.json()["releaseDate"].startswith("1995-01-01"))

        empty = self.client.post(
            "/api/media/",
            json={
                "userId": user_id,
                "title": "Unreleased",
                "type": "movies",
                "externalId": f"movie-{uuid.uuid4().hex}",
                "releaseDate": "",
            },
        )
        self.assertEqual(empty.status_code, 201, empty.text)
        self.assertIsNone(empty.json()["releaseDate"])

    def test_log_uses_received_date_and_orders_newest_first(self) -> None:
        user_id = self._user()
        media = self.client.post(
            "/api/media/",
            json={
                "userId": user_id,
                "title": "Series",
                "type": "series",
                "externalId": f"series-{uuid.uuid4().hex}",
            },
        ).json()

        for day in ("2026-05-01", "2026-01-01", "2026-09-01"):
            response = self.client.post(
                "/api/media-logs/",
                json={
                    "userId": user_id,
                    "mediaId": media["id"],
                    "status": "finished",
                    "date": day,
                },
            )
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["date"], day)

        response = self.client.get(
            f"/api/media/external/{media['externalId']}/with-logs"
            f"?type=series&user_id={user_id}"
        )
        dates = [log["date"] for log in response.json()["logs"]]
        self.assertEqual(dates, ["2026-09-01", "2026-05-01", "2026-01-01"])

    def test_multi_tag_and_has_logs_filters(self) -> None:
        user_id = self._user()
        media = self.client.post(
            "/api/media/",
            json={
                "userId": user_id,
                "title": "Tagged",
                "type": "movies",
                "externalId": f"tagged-{uuid.uuid4().hex}",
                "tags": ["a", "b"],
            },
        ).json()

        both = self.client.get(f"/api/media/?user_id={user_id}&tags=a&tags=b")
        self.assertEqual(len(both.json()), 1)

        only_a = self.client.get(f"/api/media/?user_id={user_id}&tags=a")
        self.assertEqual(len(only_a.json()), 1)

        self.client.post(
            "/api/media-logs/",
            json={
                "userId": user_id,
                "mediaId": media["id"],
                "status": "finished",
                "date": "2026-01-01",
            },
        )
        with_logs = self.client.get(
            f"/api/media/?user_id={user_id}&has_logs=true"
        )
        without_logs = self.client.get(
            f"/api/media/?user_id={user_id}&has_logs=false"
        )
        self.assertEqual(len(with_logs.json()), 1)
        self.assertEqual(len(without_logs.json()), 0)

    def test_cross_user_log_is_rejected(self) -> None:
        owner_id = self._user()
        other_id = self._user()
        media = self.client.post(
            "/api/media/",
            json={
                "userId": owner_id,
                "title": "Owned",
                "type": "movies",
                "externalId": f"owned-{uuid.uuid4().hex}",
            },
        ).json()

        response = self.client.post(
            "/api/media-logs/",
            json={
                "userId": other_id,
                "mediaId": media["id"],
                "status": "finished",
                "date": "2026-01-01",
            },
        )
        self.assertEqual(response.status_code, 404, response.text)

    def test_null_non_nullable_updates_return_422(self) -> None:
        user_id = self._user()
        media = self.client.post(
            "/api/media/",
            json={
                "userId": user_id,
                "title": "Title",
                "type": "movies",
                "externalId": f"null-{uuid.uuid4().hex}",
            },
        ).json()

        self.assertEqual(
            self.client.put(
                f"/auth/users/{user_id}", json={"username": None}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/media/{media['id']}?user_id={user_id}",
                json={"title": None},
            ).status_code,
            422,
        )

    def test_import_parse_endpoint_accepts_file(self) -> None:
        user_id = self._user()
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <myanimelist>
          <anime>
            <entry>
              <series_animedb_id>1</series_animedb_id>
              <series_title>Anime A</series_title>
              <my_status>2</my_status>
            </entry>
          </anime>
          <manga>
            <entry>
              <manga_mangadb_id>2</manga_mangadb_id>
              <manga_title>Manga B</manga_title>
              <my_status>2</my_status>
            </entry>
          </manga>
        </myanimelist>"""
        response = self.client.post(
            "/api/import/mal/parse",
            data={"user_id": str(user_id), "media_type": "anime"},
            files={"file": ("list.xml", xml, "application/xml")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["entries"]), 2)

    def test_invalid_text_date_returns_422(self) -> None:
        user_id = self._user()
        response = self.client.post(
            "/api/media/",
            json={
                "userId": user_id,
                "title": "Bad date",
                "type": "book",
                "externalId": f"bad-{uuid.uuid4().hex}",
                "releaseDate": "September 1, 1995",
            },
        )
        self.assertEqual(response.status_code, 422)


class ImportProviderTests(unittest.TestCase):
    def test_mal_imports_anime_and_manga(self) -> None:
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <myanimelist>
          <anime>
            <entry>
              <series_animedb_id>1</series_animedb_id>
              <series_title>Anime A</series_title>
              <my_score>8</my_score>
              <my_status>2</my_status>
            </entry>
          </anime>
          <manga>
            <entry>
              <manga_mangadb_id>2</manga_mangadb_id>
              <manga_title>Manga B</manga_title>
              <my_score>9</my_score>
              <my_status>2</my_status>
            </entry>
          </manga>
        </myanimelist>"""

        preview = asyncio.run(MalProvider().parse_file("list.xml", xml))
        self.assertEqual(len(preview.entries), 2)
        self.assertEqual(
            {entry.media_type for entry in preview.entries},
            {MediaTypeEnum.ANIME, MediaTypeEnum.MANGA},
        )

    def test_letterboxd_invalid_zip_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(
                LetterboxdProvider().parse_file("bad.zip", b"not a zip")
            )

    def test_openlibrary_work_uses_json_endpoint(self) -> None:
        service = OpenLibraryService()
        seen: dict[str, str] = {}

        async def fake_get(path: str, params=None):
            seen["path"] = path
            return {}

        service._get = fake_get  # type: ignore[method-assign]
        asyncio.run(service.get_work("/works/OL123W"))
        self.assertEqual(seen["path"], "/works/OL123W.json")


class SchemaTests(unittest.TestCase):
    def test_release_date_normalization(self) -> None:
        media = MediaCreate(
            user_id=1,
            title="Book",
            type=MediaTypeEnum.BOOK,
            external_id="book-1",
            release_date="2024-05",
        )
        self.assertEqual(media.release_date.year, 2024)
        self.assertEqual(media.release_date.month, 5)


class SuwayomiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app, raise_server_exceptions=False)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)

    def _user(self) -> int:
        username = f"suwayomi_{uuid.uuid4().hex[:10]}"
        response = self.client.post(
            "/auth/register", json={"username": username, "password": "secret"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def test_category_resolution_uses_order_and_ignores_default(self) -> None:
        from models.enums import MediaStatusEnum
        from services.suwayomi.categories import resolve_action

        mappings = {
            1: SimpleNamespace(action="status", status=MediaStatusEnum.IN_PROGRESS, category_order=2),
            2: SimpleNamespace(action="backlog", status=None, category_order=3),
            3: SimpleNamespace(action="ignore", status=None, category_order=1),
            7: SimpleNamespace(action="status", status=MediaStatusEnum.FOLLOWING, category_order=1),
        }
        meta = {
            1: {"order": 2},
            2: {"order": 3},
            3: {"order": 1},
            7: {"order": 1},
        }

        action, status = resolve_action(
            [{"id": 3}, {"id": 2}, {"id": 1}], mappings, meta
        )
        self.assertEqual((action, status), ("status", MediaStatusEnum.IN_PROGRESS))

        action, status = resolve_action(
            [{"id": 1}, {"id": 7}], mappings, meta
        )
        self.assertEqual((action, status), ("status", MediaStatusEnum.FOLLOWING))

        action, status = resolve_action([], {}, {})
        self.assertEqual((action, status), (None, None))

        default_mapping = {
            0: SimpleNamespace(action="backlog", status=None, category_order=0)
        }
        action, status = resolve_action([], default_mapping, {})
        self.assertEqual((action, status), ("backlog", None))

    def test_provider_imports_only_missing_titles(self) -> None:
        from database import SessionLocal
        from models.enums import MediaStatusEnum
        from models.media import Media
        from models.suwayomi import SuwayomiCategoryMapping
        from services.importer.providers import suwayomi as provider_module
        from services.importer.providers.suwayomi import SuwayomiProvider

        user_id = self._user()

        class FakeClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args) -> None:
                return None

            async def get_categories(self):
                return [
                    {"id": 1, "name": "Reading", "order": 2, "default": False},
                    {"id": 2, "name": "Plan To Read", "order": 3, "default": False},
                ]

            async def get_library(self):
                return [
                    {
                        "id": 10,
                        "title": "Already Logged",
                        "categories": {"nodes": [{"id": 1}]},
                    },
                    {
                        "id": 11,
                        "title": "New Manga",
                        "description": "desc",
                        "realUrl": "https://example.com/manga",
                        "categories": {"nodes": [{"id": 2}]},
                    },
                ]

        db = SessionLocal()
        try:
            db.add_all(
                [
                    SuwayomiCategoryMapping(
                        user_id=user_id,
                        category_id=1,
                        category_name="Reading",
                        category_order=2,
                        action="status",
                        status=MediaStatusEnum.IN_PROGRESS,
                    ),
                    SuwayomiCategoryMapping(
                        user_id=user_id,
                        category_id=2,
                        category_name="Plan To Read",
                        category_order=3,
                        action="backlog",
                    ),
                    Media(
                        user_id=user_id,
                        external_id="already",
                        title="Already Logged",
                        type=MediaTypeEnum.MANGA,
                    ),
                ]
            )
            db.commit()

            with patch.object(provider_module, "SuwayomiClient", FakeClient):
                preview = asyncio.run(
                    SuwayomiProvider().parse_url(
                        "http://suwayomi.test", MediaTypeEnum.MANGA, db, user_id
                    )
                )

            self.assertEqual(preview.counts.total, 1)
            self.assertEqual(preview.counts.backlog, 1)
            entry = preview.entries[0]
            self.assertEqual(entry.title, "New Manga")
            self.assertIsNone(entry.status)
            self.assertTrue(entry.in_backlog)
            self.assertEqual(entry.source, "suwayomi")
            self.assertEqual(entry.external_refs["suwayomiId"], "11")
        finally:
            db.close()

    def test_sync_applies_category_transitions(self) -> None:
        from datetime import datetime

        from database import SessionLocal
        from models.enums import MediaStatusEnum
        from models.media import Media
        from models.media_list_item import MediaListItem
        from models.suwayomi import (
            SuwayomiAccount,
            SuwayomiCategoryMapping,
            SuwayomiMangaLink,
        )
        from services.suwayomi.sync import service

        user_id = self._user()
        db = SessionLocal()
        try:
            account = SuwayomiAccount(
                user_id=user_id,
                base_url="http://suwayomi.test",
                enabled=False,
            )
            db.add(account)
            db.add_all(
                [
                    SuwayomiCategoryMapping(
                        user_id=user_id,
                        category_id=7,
                        category_name="Waiting Chapters",
                        category_order=1,
                        action="status",
                        status=MediaStatusEnum.FOLLOWING,
                    ),
                    SuwayomiCategoryMapping(
                        user_id=user_id,
                        category_id=1,
                        category_name="Reading",
                        category_order=2,
                        action="status",
                        status=MediaStatusEnum.IN_PROGRESS,
                    ),
                    SuwayomiCategoryMapping(
                        user_id=user_id,
                        category_id=2,
                        category_name="Plan To Read",
                        category_order=3,
                        action="backlog",
                    ),
                    SuwayomiCategoryMapping(
                        user_id=user_id,
                        category_id=4,
                        category_name="Completed",
                        category_order=4,
                        action="status",
                        status=MediaStatusEnum.FINISHED,
                    ),
                ]
            )
            media = Media(
                user_id=user_id,
                external_id="dandadan",
                title="Dandadan",
                type=MediaTypeEnum.MANGA,
                status=MediaStatusEnum.ON_HOLD,
            )
            db.add(media)
            db.commit()
            db.refresh(media)

            categories = [
                {"id": 0, "name": "Default", "order": 0, "default": True},
                {"id": 7, "name": "Waiting Chapters", "order": 1, "default": False},
                {"id": 1, "name": "Reading", "order": 2, "default": False},
                {"id": 2, "name": "Plan To Read", "order": 3, "default": False},
                {"id": 4, "name": "Completed", "order": 4, "default": False},
            ]

            def run(category_id: int):
                library = [
                    {
                        "id": 787,
                        "title": "Dandadan",
                        "categories": {"nodes": [{"id": category_id}]},
                    }
                ]
                summary = service._empty_summary(datetime.now())
                asyncio.run(
                    service._run(db, account, categories, library, summary)
                )
                db.commit()
                db.refresh(media)
                return summary

            run(7)
            self.assertEqual(media.status, MediaStatusEnum.FOLLOWING)

            run(4)
            self.assertEqual(media.status, MediaStatusEnum.FINISHED)

            run(2)
            self.assertIsNone(media.status)
            backlog = db.query(MediaListItem).filter_by(
                user_id=user_id, media_id=media.id, list_type="backlog"
            ).first()
            self.assertIsNotNone(backlog)

            run(1)
            self.assertEqual(media.status, MediaStatusEnum.IN_PROGRESS)
            db.refresh(media)
            backlog = db.query(MediaListItem).filter_by(
                user_id=user_id, media_id=media.id, list_type="backlog"
            ).first()
            self.assertIsNone(backlog)

            link = db.query(SuwayomiMangaLink).filter_by(
                user_id=user_id, suwayomi_manga_id=787
            ).first()
            self.assertIsNotNone(link)
            self.assertEqual(link.media_id, media.id)
            self.assertEqual(link.last_action, "status")
            self.assertEqual(link.last_status, MediaStatusEnum.IN_PROGRESS)
        finally:
            db.close()

    def test_config_never_exposes_password(self) -> None:
        user_id = self._user()

        class FakeClient:
            async def get_categories(self):
                raise ValueError("offline")

            async def aclose(self) -> None:
                return None

        with patch("routers.suwayomi._account_client", return_value=FakeClient()):
            response = self.client.put(
                "/api/suwayomi/config",
                json={
                    "userId": user_id,
                    "baseUrl": "http://suwayomi.test",
                    "username": "user",
                    "password": "secret",
                    "enabled": False,
                    "updatesPerDay": 4,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["hasPassword"])
        self.assertEqual(body["username"], "user")
        self.assertNotIn("password", body)

        response = self.client.get(f"/api/suwayomi/config?user_id={user_id}")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("password", response.json())

    def test_pending_link_resolves_manually_added_media(self) -> None:
        from datetime import datetime

        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.media import Media
        from models.suwayomi import (
            SuwayomiAccount,
            SuwayomiCategoryMapping,
            SuwayomiMangaLink,
        )
        from services.suwayomi.sync import service

        user_id = self._user()
        db = SessionLocal()
        try:
            account = SuwayomiAccount(
                user_id=user_id, base_url="http://suwayomi.test", enabled=False
            )
            db.add(account)
            db.add(
                SuwayomiCategoryMapping(
                    user_id=user_id,
                    category_id=1,
                    category_name="Reading",
                    category_order=2,
                    action="status",
                    status=MediaStatusEnum.IN_PROGRESS,
                )
            )
            media = Media(
                user_id=user_id,
                external_id="dandadan",
                title="Dandadan",
                type=MediaTypeEnum.MANGA,
                status=MediaStatusEnum.FINISHED,
            )
            db.add(media)
            db.commit()
            db.refresh(media)

            link = SuwayomiMangaLink(
                user_id=user_id,
                suwayomi_manga_id=787,
                media_id=None,
                title="Dandadan",
            )
            db.add(link)
            db.commit()

            categories = [
                {"id": 0, "name": "Default", "order": 0, "default": True},
                {"id": 1, "name": "Reading", "order": 2, "default": False},
            ]
            library = [
                {
                    "id": 787,
                    "title": "Dandadan",
                    "categories": {"nodes": [{"id": 1}]},
                }
            ]
            summary = service._empty_summary(datetime.now())
            asyncio.run(service._run(db, account, categories, library, summary))
            db.commit()
            db.refresh(media)
            db.refresh(link)

            self.assertEqual(link.media_id, media.id)
            self.assertEqual(media.status, MediaStatusEnum.IN_PROGRESS)
            self.assertEqual(link.last_action, "status")
            self.assertEqual(link.last_status, MediaStatusEnum.IN_PROGRESS)
            self.assertIn("Dandadan", summary["pendingResolved"])
        finally:
            db.close()

    def test_pending_link_retries_anilist_and_records_attempt(self) -> None:
        from datetime import datetime

        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.media import Media
        from models.suwayomi import (
            SuwayomiAccount,
            SuwayomiCategoryMapping,
            SuwayomiMangaLink,
        )
        from schemas.import_schemas import ImportCandidate, ImportMatchResult
        from services.suwayomi import sync as sync_module

        user_id = self._user()

        async def fake_resolve(_db, _uid, title, query=None):
            candidate = ImportCandidate(
                provider="anilist",
                external_id="unknown-1",
                media_type=MediaTypeEnum.MANGA,
                title="Unknown Manga",
                score=1.0,
            )
            return ImportMatchResult(
                key="suwayomi",
                status="matched",
                match=candidate,
                candidates=[candidate],
            )

        db = SessionLocal()
        try:
            account = SuwayomiAccount(
                user_id=user_id, base_url="http://suwayomi.test", enabled=False
            )
            db.add(account)
            db.add(
                SuwayomiCategoryMapping(
                    user_id=user_id,
                    category_id=1,
                    category_name="Reading",
                    category_order=2,
                    action="status",
                    status=MediaStatusEnum.IN_PROGRESS,
                )
            )
            link = SuwayomiMangaLink(
                user_id=user_id,
                suwayomi_manga_id=999,
                media_id=None,
                title="Unknown Manga",
            )
            db.add(link)
            db.commit()

            categories = [
                {"id": 0, "name": "Default", "order": 0, "default": True},
                {"id": 1, "name": "Reading", "order": 2, "default": False},
            ]
            library = [
                {
                    "id": 999,
                    "title": "Unknown Manga",
                    "categories": {"nodes": [{"id": 1}]},
                }
            ]
            summary = sync_module.service._empty_summary(datetime.now())
            with patch.object(sync_module.matcher, "resolve_manga", fake_resolve):
                asyncio.run(
                    sync_module.service._run(db, account, categories, library, summary)
                )
            db.commit()
            db.refresh(link)

            media = (
                db.query(Media)
                .filter_by(user_id=user_id, external_id="unknown-1")
                .first()
            )
            self.assertIsNotNone(media)
            self.assertEqual(link.media_id, media.id)
            self.assertEqual(link.match_attempts, 1)
            self.assertIsNotNone(link.last_match_attempt_at)
            self.assertEqual(summary["created"], 1)
            self.assertIn("Unknown Manga", summary["pendingResolved"])
        finally:
            db.close()

    def test_pending_link_respects_anilist_backoff(self) -> None:
        from datetime import datetime

        from database import SessionLocal
        from models.enums import MediaStatusEnum
        from models.suwayomi import (
            SuwayomiAccount,
            SuwayomiCategoryMapping,
            SuwayomiMangaLink,
        )
        from services.suwayomi import sync as sync_module

        user_id = self._user()
        called = {"value": False}

        async def fake_resolve(*_args, **_kwargs):
            called["value"] = True
            raise AssertionError("AniList não deveria ser chamado durante o backoff.")

        db = SessionLocal()
        try:
            account = SuwayomiAccount(
                user_id=user_id, base_url="http://suwayomi.test", enabled=False
            )
            db.add(account)
            db.add(
                SuwayomiCategoryMapping(
                    user_id=user_id,
                    category_id=1,
                    category_name="Reading",
                    category_order=2,
                    action="status",
                    status=MediaStatusEnum.IN_PROGRESS,
                )
            )
            link = SuwayomiMangaLink(
                user_id=user_id,
                suwayomi_manga_id=1000,
                media_id=None,
                title="Still Unknown",
                last_match_attempt_at=sync_module.utcnow(),
                match_attempts=1,
            )
            db.add(link)
            db.commit()

            categories = [
                {"id": 0, "name": "Default", "order": 0, "default": True},
                {"id": 1, "name": "Reading", "order": 2, "default": False},
            ]
            library = [
                {
                    "id": 1000,
                    "title": "Still Unknown",
                    "categories": {"nodes": [{"id": 1}]},
                }
            ]
            summary = sync_module.service._empty_summary(datetime.now())
            with patch.object(sync_module.matcher, "resolve_manga", fake_resolve):
                asyncio.run(
                    sync_module.service._run(db, account, categories, library, summary)
                )
            db.commit()
            db.refresh(link)

            self.assertFalse(called["value"])
            self.assertIsNone(link.media_id)
            self.assertEqual(link.match_attempts, 1)
            self.assertTrue(
                any(item["title"] == "Still Unknown" for item in summary["unmatched"])
            )
        finally:
            db.close()


class ImportJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app, raise_server_exceptions=False)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)

    def _user(self) -> int:
        username = f"importjob_{uuid.uuid4().hex[:10]}"
        response = self.client.post(
            "/auth/register", json={"username": username, "password": "secret"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def test_job_api_create_list_cancel_delete(self) -> None:
        user_id = self._user()

        response = self.client.post(
            "/api/import/jobs",
            data={
                "user_id": user_id,
                "provider_id": "anilist",
                "username": "someone",
                "media_type": "manga",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        job = response.json()
        job_id = job["id"]
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["provider"], "anilist")

        listed = self.client.get(f"/api/import/jobs?user_id={user_id}")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()[0]["id"], job_id)

        detail = self.client.get(f"/api/import/jobs/{job_id}?user_id={user_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["progressTotal"], 0)

        cancelled = self.client.post(
            f"/api/import/jobs/{job_id}/cancel?user_id={user_id}"
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "cancelled")

        deleted = self.client.delete(
            f"/api/import/jobs/{job_id}?user_id={user_id}"
        )
        self.assertEqual(deleted.status_code, 204, deleted.text)

        missing = self.client.get(f"/api/import/jobs/{job_id}?user_id={user_id}")
        self.assertEqual(missing.status_code, 404)

    def test_worker_matches_and_commit_uses_selected_items(self) -> None:
        from database import SessionLocal
        from models.enums import MediaTypeEnum
        from models.import_job import ImportJob
        from models.media import Media
        from schemas.import_schemas import (
            ImportCandidate,
            ImportEntry,
            ImportMatchResponse,
            ImportMatchResult,
            ImportPreviewCounts,
            ImportPreviewResponse,
        )
        from services.import_jobs import worker as worker_module
        from services.import_jobs.service import service as import_job_service

        user_id = self._user()

        class FakeProvider:
            skip_existing = False

            async def parse_username(self, username, media_type):
                return ImportPreviewResponse(
                    media_type=MediaTypeEnum.MANGA,
                    entries=[
                        ImportEntry(
                            key="a",
                            media_type=MediaTypeEnum.MANGA,
                            title="Alpha",
                            source="fake",
                        ),
                        ImportEntry(
                            key="b",
                            media_type=MediaTypeEnum.MANGA,
                            title="Beta",
                            source="fake",
                        ),
                    ],
                    counts=ImportPreviewCounts(total=2),
                )

        def fake_candidate(external_id: str, title: str):
            return ImportCandidate(
                provider="anilist",
                external_id=external_id,
                media_type=MediaTypeEnum.MANGA,
                title=title,
                score=1.0,
            )

        async def fake_match(_db, request, _tmdb_key, _igdb_credentials):
            results = []
            for item in request.items:
                if item.key == "a":
                    candidate = fake_candidate("alpha-1", "Alpha")
                    results.append(
                        ImportMatchResult(
                            key="a",
                            status="matched",
                            match=candidate,
                            candidates=[candidate],
                        )
                    )
                else:
                    results.append(ImportMatchResult(key="b", status="not_found"))
            return ImportMatchResponse(results=results)

        db = SessionLocal()
        try:
            job = import_job_service.create(
                db,
                user_id=user_id,
                provider="fake",
                input_type="username",
                media_type=MediaTypeEnum.MANGA,
                source_username="someone",
            )

            with patch.object(worker_module, "get_provider", return_value=FakeProvider()), patch.object(
                worker_module.matcher, "match", fake_match
            ):
                asyncio.run(worker_module.worker.process(job.id))

            db.expire_all()
            job = db.get(ImportJob, job.id)
            self.assertEqual(job.status, "ready", job.error)
            self.assertEqual(job.progress_total, 2)
            self.assertEqual(job.progress_done, 2)

            items = self.client.get(
                f"/api/import/jobs/{job.id}/items?user_id={user_id}"
            )
            self.assertEqual(items.status_code, 200, items.text)
            body = items.json()
            self.assertEqual(body["total"], 2)
            by_key = {item["key"]: item for item in body["items"]}
            self.assertEqual(by_key["a"]["matchStatus"], "matched")
            self.assertEqual(by_key["b"]["matchStatus"], "not_found")

            beta_id = by_key["b"]["id"]
            selected = self.client.patch(
                f"/api/import/jobs/{job.id}/items/{beta_id}?user_id={user_id}",
                json={
                    "selected": {
                        "provider": "anilist",
                        "externalId": "beta-1",
                        "mediaType": "manga",
                        "title": "Beta",
                        "score": 1.0,
                    }
                },
            )
            self.assertEqual(selected.status_code, 200, selected.text)
            self.assertEqual(selected.json()["selected"]["externalId"], "beta-1")

            committed = self.client.post(
                f"/api/import/jobs/{job.id}/commit?user_id={user_id}",
                json={"addToBacklog": False},
            )
            self.assertEqual(committed.status_code, 200, committed.text)
            self.assertEqual(committed.json()["imported"], 2)

            db.expire_all()
            job = db.get(ImportJob, job.id)
            self.assertEqual(job.status, "done")
            self.assertIsNotNone(job.summary_json)
            import json as json_module

            stored_summary = json_module.loads(job.summary_json)
            self.assertIn("logsCreated", stored_summary)
            self.assertNotIn("logs_created", stored_summary)

            external_ids = {
                media.external_id
                for media in db.query(Media).filter(Media.user_id == user_id).all()
            }
            self.assertIn("alpha-1", external_ids)
            self.assertIn("beta-1", external_ids)
        finally:
            db.close()

    def test_anilist_limiter_adapts_to_headers(self) -> None:
        from services import anilist_service as anilist

        original_limit = anilist._anilist_limit
        original_interval = anilist._anilist_interval
        try:
            anilist._observe_rate_limit(
                {"X-RateLimit-Limit": "90", "X-RateLimit-Remaining": "50"}
            )
            self.assertEqual(anilist._anilist_limit, 90)
            self.assertAlmostEqual(anilist._anilist_interval, 60.0 / 90, places=3)

            anilist._observe_rate_limit(
                {"X-RateLimit-Limit": "30", "X-RateLimit-Remaining": "20"}
            )
            self.assertEqual(anilist._anilist_limit, 30)
            self.assertAlmostEqual(anilist._anilist_interval, 2.0, places=3)
        finally:
            anilist._anilist_limit = original_limit
            anilist._anilist_interval = original_interval

    def test_worker_reads_file_job_and_cleans_temp_file(self) -> None:
        from pathlib import Path as FilePath

        from database import SessionLocal
        from models.import_job import ImportJob
        from schemas.import_schemas import ImportMatchResponse, ImportMatchResult
        from services.import_jobs import worker as worker_module

        user_id = self._user()
        csv = (
            "Date,Name,Year,Letterboxd URI\n"
            "2024-01-01,Alpha,2024,https://boxd.it/abc\n"
        )

        response = self.client.post(
            "/api/import/jobs",
            data={
                "user_id": user_id,
                "provider_id": "letterboxd",
                "media_type": "movies",
            },
            files={"file": ("watched.csv", csv.encode(), "text/csv")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        job_id = response.json()["id"]

        db = SessionLocal()
        try:
            job = db.get(ImportJob, job_id)
            self.assertIsNotNone(job)
            file_path = job.file_path
            self.assertTrue(file_path)
            self.assertTrue(FilePath(file_path).exists())

            async def fake_match(_db, request, *_args):
                return ImportMatchResponse(
                    results=[
                        ImportMatchResult(key=item.key, status="not_found")
                        for item in request.items
                    ]
                )

            with patch.object(worker_module.matcher, "match", fake_match):
                asyncio.run(worker_module.worker.process(job_id))

            db.expire_all()
            job = db.get(ImportJob, job_id)
            self.assertEqual(job.status, "ready", job.error)
            self.assertEqual(job.progress_total, 1)
            self.assertEqual(job.progress_done, 1)
            self.assertFalse(FilePath(file_path).exists())
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
