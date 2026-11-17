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
os.environ["LIBRARY_SYNC_SCHEDULER_ENABLED"] = "false"

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
        from models.library_sync import LibrarySyncSuggestion
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
            suggestion = (
                db.query(LibrarySyncSuggestion)
                .filter_by(
                    user_id=user_id,
                    provider="suwayomi",
                    source_id="999",
                )
                .one()
            )
            self.assertEqual(suggestion.review_state, "approved")
            self.assertEqual(suggestion.origin, "auto")
            self.assertIsNotNone(suggestion.sync_run_at)
            self.assertEqual(suggestion.media_id, media.id)
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

    def test_ambiguous_suwayomi_match_stages_and_approves_suggestion(self) -> None:
        from datetime import datetime

        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.library_sync import LibrarySyncSuggestion
        from models.media import Media
        from models.media_log import MediaLog
        from models.suwayomi import (
            SuwayomiAccount,
            SuwayomiCategoryMapping,
            SuwayomiMangaLink,
        )
        from schemas.import_schemas import ImportCandidate, ImportMatchResult
        from services.library_sync.suggestions import (
            approve_suggestions,
            set_suggestion_state,
        )
        from services.suwayomi import sync as sync_module

        user_id = self._user()

        async def fake_resolve(*_args, **_kwargs):
            candidate = ImportCandidate(
                provider="anilist",
                external_id="180511",
                media_type=MediaTypeEnum.MANGA,
                title="Ambiguous Manga",
                year=2024,
                score=0.5,
            )
            return ImportMatchResult(
                key="suwayomi",
                status="ambiguous",
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
            db.commit()

            categories = [
                {"id": 0, "name": "Default", "order": 0, "default": True},
                {"id": 1, "name": "Reading", "order": 2, "default": False},
            ]
            library = [
                {
                    "id": 2001,
                    "title": "Ambiguous Manga",
                    "realUrl": "http://suwayomi.test/manga/2001",
                    "categories": {"nodes": [{"id": 1}]},
                }
            ]
            summary = sync_module.service._empty_summary(datetime.now())
            with patch.object(sync_module.matcher, "resolve_manga", fake_resolve):
                asyncio.run(
                    sync_module.service._run(
                        db, account, categories, library, summary
                    )
                )
            db.commit()

            link = (
                db.query(SuwayomiMangaLink)
                .filter_by(user_id=user_id, suwayomi_manga_id=2001)
                .one()
            )
            suggestion = (
                db.query(LibrarySyncSuggestion)
                .filter_by(user_id=user_id, provider="suwayomi", source_id="2001")
                .one()
            )
            self.assertIsNone(link.media_id)
            self.assertEqual(suggestion.review_state, "pending")
            self.assertIsNotNone(suggestion.candidate_json)
            self.assertEqual(suggestion.action, "in_progress")
            self.assertEqual(
                suggestion.target_status, MediaStatusEnum.IN_PROGRESS
            )
            self.assertIn('"suwayomiId": "2001"', suggestion.source_refs_json)
            self.assertNotIn("syncProvider", suggestion.source_refs_json)
            self.assertEqual(summary["reviewStaged"], 1)
            self.assertEqual(
                db.query(Media)
                .filter_by(user_id=user_id, external_id="180511")
                .count(),
                0,
            )

            set_suggestion_state(db, user_id, suggestion.id, "dismissed")
            db.refresh(link)
            self.assertTrue(link.ignored)
            set_suggestion_state(db, user_id, suggestion.id, "pending")
            db.refresh(link)
            self.assertFalse(link.ignored)

            result = approve_suggestions(db, user_id, [suggestion.id])
            self.assertEqual(result["approved"], 1)
            self.assertEqual(result["failed"], 0)

            media = (
                db.query(Media)
                .filter_by(user_id=user_id, external_id="180511")
                .one()
            )
            db.refresh(link)
            db.refresh(suggestion)
            self.assertEqual(link.media_id, media.id)
            self.assertEqual(suggestion.review_state, "approved")
            self.assertEqual(suggestion.media_id, media.id)
            self.assertEqual(media.status, MediaStatusEnum.IN_PROGRESS)
            self.assertEqual(
                db.query(MediaLog).filter_by(media_id=media.id).count(), 1
            )
        finally:
            db.close()

    def test_no_match_suwayomi_suggestion_requires_candidate(self) -> None:
        from datetime import datetime

        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.library_sync import LibrarySyncSuggestion
        from models.media import Media
        from models.suwayomi import SuwayomiAccount, SuwayomiCategoryMapping
        from schemas.import_schemas import ImportMatchResult
        from services.library_sync.suggestions import approve_suggestions
        from services.suwayomi import sync as sync_module

        user_id = self._user()

        async def fake_resolve(*_args, **_kwargs):
            return ImportMatchResult(
                key="suwayomi",
                status="not_found",
                match=None,
                candidates=[],
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
            db.commit()

            categories = [
                {"id": 0, "name": "Default", "order": 0, "default": True},
                {"id": 1, "name": "Reading", "order": 2, "default": False},
            ]
            library = [
                {
                    "id": 2002,
                    "title": "Missing Manga",
                    "categories": {"nodes": [{"id": 1}]},
                }
            ]
            summary = sync_module.service._empty_summary(datetime.now())
            with patch.object(sync_module.matcher, "resolve_manga", fake_resolve):
                asyncio.run(
                    sync_module.service._run(
                        db, account, categories, library, summary
                    )
                )
            db.commit()

            suggestion = (
                db.query(LibrarySyncSuggestion)
                .filter_by(user_id=user_id, provider="suwayomi", source_id="2002")
                .one()
            )
            self.assertIsNone(suggestion.candidate_json)
            self.assertIn("não encontrado", suggestion.error or "")
            self.assertEqual(summary["reviewStaged"], 1)

            result = approve_suggestions(db, user_id, [suggestion.id])
            self.assertEqual(result["approved"], 0)
            self.assertEqual(result["failed"], 1)
            db.refresh(suggestion)
            self.assertEqual(suggestion.review_state, "pending")
            self.assertIn("Escolha um match", suggestion.error or "")
            self.assertEqual(
                db.query(Media)
                .filter_by(user_id=user_id, title="Missing Manga")
                .count(),
                0,
            )
        finally:
            db.close()

    def test_suggestion_history_and_last_sync_filter(self) -> None:
        from datetime import datetime

        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.library_sync import LibrarySyncSuggestion
        from services.library_sync.suggestions import (
            candidate_to_json,
            candidates_to_json,
        )
        from schemas.import_schemas import ImportCandidate

        user_id = self._user()
        candidate = ImportCandidate(
            provider="anilist",
            external_id="777001",
            media_type=MediaTypeEnum.MANGA,
            title="History Manga",
            score=0.8,
        )
        db = SessionLocal()
        try:
            db.add_all(
                [
                    LibrarySyncSuggestion(
                        user_id=user_id,
                        provider="suwayomi",
                        source_id="9001",
                        media_type=MediaTypeEnum.MANGA,
                        title="Old Auto",
                        source_status="finished",
                        action="finished",
                        target_status=MediaStatusEnum.FINISHED,
                        review_state="approved",
                        origin="auto",
                        sync_run_at=datetime(2026, 1, 1, 0, 0),
                    ),
                    LibrarySyncSuggestion(
                        user_id=user_id,
                        provider="suwayomi",
                        source_id="9002",
                        media_type=MediaTypeEnum.MANGA,
                        title="Old Review",
                        source_status="finished",
                        action="finished",
                        target_status=MediaStatusEnum.FINISHED,
                        review_state="approved",
                        origin="review",
                        sync_run_at=datetime(2026, 1, 1, 0, 0),
                    ),
                    LibrarySyncSuggestion(
                        user_id=user_id,
                        provider="suwayomi",
                        source_id="9003",
                        media_type=MediaTypeEnum.MANGA,
                        title="Latest Auto",
                        source_status="in_progress",
                        action="in_progress",
                        target_status=MediaStatusEnum.IN_PROGRESS,
                        review_state="approved",
                        origin="auto",
                        candidate_json=candidate_to_json(candidate),
                        candidates_json=candidates_to_json([candidate]),
                        sync_run_at=datetime(2026, 1, 2, 0, 0),
                    ),
                    LibrarySyncSuggestion(
                        user_id=user_id,
                        provider="suwayomi",
                        source_id="9004",
                        media_type=MediaTypeEnum.MANGA,
                        title="Pending Candidate",
                        source_status="in_progress",
                        action="in_progress",
                        target_status=MediaStatusEnum.IN_PROGRESS,
                        review_state="pending",
                        candidate_json=candidate_to_json(candidate),
                        candidates_json=candidates_to_json([candidate]),
                        sync_run_at=datetime(2026, 1, 2, 0, 0),
                    ),
                ]
            )
            db.commit()
        finally:
            db.close()

        approved = self.client.get(
            f"/api/integrations/suggestions?user_id={user_id}&state=approved"
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        body = approved.json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["counts"]["approved"], 3)
        self.assertEqual(body["counts"]["lastSync"], 1)
        self.assertEqual(body["counts"]["attention"], 1)

        latest = self.client.get(
            f"/api/integrations/suggestions?user_id={user_id}&last_sync=true"
        )
        self.assertEqual(latest.status_code, 200, latest.text)
        latest_body = latest.json()
        self.assertEqual(latest_body["total"], 1)
        self.assertEqual(latest_body["items"][0]["title"], "Latest Auto")
        self.assertEqual(latest_body["items"][0]["origin"], "auto")
        self.assertEqual(len(latest_body["items"][0]["candidates"]), 1)

        everything = self.client.get(
            f"/api/integrations/suggestions?user_id={user_id}&state=all"
        )
        self.assertEqual(everything.status_code, 200, everything.text)
        self.assertEqual(everything.json()["total"], 4)

    def test_suggestion_candidate_endpoint(self) -> None:
        from database import SessionLocal
        from models.enums import MediaTypeEnum
        from models.library_sync import LibrarySyncSuggestion

        user_id = self._user()
        db = SessionLocal()
        try:
            suggestion = LibrarySyncSuggestion(
                user_id=user_id,
                provider="suwayomi",
                source_id="2003",
                media_type=MediaTypeEnum.MANGA,
                title="Candidate Manga",
                source_status="in_progress",
                source_label="in_progress",
                action="in_progress",
                review_state="pending",
                source_refs_json='{"suwayomiId": "2003"}',
            )
            db.add(suggestion)
            db.commit()
            suggestion_id = suggestion.id
        finally:
            db.close()

        response = self.client.patch(
            f"/api/integrations/suggestions/{suggestion_id}/candidate",
            json={
                "userId": user_id,
                "candidate": {
                    "provider": "anilist",
                    "externalId": "999001",
                    "mediaType": MediaTypeEnum.MANGA,
                    "title": "Candidate Manga",
                    "year": 2024,
                    "score": 0.9,
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["candidate"]["externalId"], "999001")
        self.assertEqual(len(body["candidates"]), 1)
        self.assertEqual(body["candidates"][0]["externalId"], "999001")
        self.assertIsNone(body["error"])

    def test_letterboxd_rss_item_fields(self) -> None:
        from services.library_sync.providers.letterboxd import LetterboxdProvider
        from defusedxml.ElementTree import fromstring

        xml = b"""<?xml version='1.0' encoding='utf-8'?>
        <rss version="2.0" xmlns:letterboxd="https://letterboxd.com"
             xmlns:tmdb="https://themoviedb.org">
          <channel><item>
            <title>Dune: Part Two, 2024 - \xe2\x98\x85\xe2\x98\x85\xe2\x98\x85\xe2\x98\x85</title>
            <link>https://letterboxd.com/user/film/dune-part-two-2024/</link>
            <guid>letterboxd-review-123</guid>
            <pubDate>Mon, 01 Jan 2024 12:00:00 +0000</pubDate>
            <description>&lt;p&gt;&lt;img src="poster.jpg"/&gt;Great film.&lt;/p&gt;</description>
            <letterboxd:filmTitle>Dune: Part Two</letterboxd:filmTitle>
            <letterboxd:filmYear>2024</letterboxd:filmYear>
            <letterboxd:memberRating>4.0</letterboxd:memberRating>
            <letterboxd:watchedDate>2024-01-01</letterboxd:watchedDate>
            <tmdb:movieId>693134</tmdb:movieId>
          </item></channel>
        </rss>"""
        item = LetterboxdProvider()._to_item(fromstring(xml).find(".//item"))
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.title, "Dune: Part Two")
        self.assertEqual(item.external_id, "693134")
        self.assertEqual(item.rating, 8.0)
        self.assertEqual(item.review, "Great film.")
        self.assertEqual(item.year, 2024)

    def test_connection_import_preview_uses_mapping(self) -> None:
        import asyncio
        import json

        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.library_sync import LibraryConnection
        from services.importer.providers.library_sync import LibrarySyncImportProvider
        from services.library_sync.providers.base import (
            LibraryProvider,
            SourceItem,
            SourceSnapshot,
            SourceStatus,
        )

        class FakeProvider(LibraryProvider):
            id = "kitsu"
            label = "Fake"
            media_types = [MediaTypeEnum.ANIME]
            auth_kind = "username"

            def statuses(self):
                return [
                    SourceStatus("completed", "Completo", "finished", MediaStatusEnum.FINISHED),
                    SourceStatus("planned", "Planejado", "backlog", None),
                ]

            async def fetch(self, connection, cursor):
                return SourceSnapshot(
                    items=[
                        SourceItem(
                            source_id="1",
                            media_type=MediaTypeEnum.ANIME,
                            title="Anime A",
                            status="completed",
                            external_id="100",
                            rating=9.0,
                            review="Bom",
                            external_refs={"anilistId": "100"},
                        ),
                        SourceItem(
                            source_id="2",
                            media_type=MediaTypeEnum.ANIME,
                            title="Anime B",
                            status="planned",
                            external_id="200",
                        ),
                    ],
                    statuses=self.statuses(),
                )

        user_id = self._user()
        db = SessionLocal()
        try:
            db.add(
                LibraryConnection(
                    user_id=user_id,
                    provider="kitsu",
                    enabled=True,
                    updates_per_day=4,
                    auth_json=json.dumps({"username": "x"}),
                    cursor_json="{}",
                )
            )
            db.commit()
            provider = LibrarySyncImportProvider("kitsu")
            provider.sync_provider = FakeProvider()
            preview = asyncio.run(provider.parse_connection(db, user_id, None))
            self.assertEqual(len(preview.entries), 2)
            by_key = {entry.key: entry for entry in preview.entries}
            self.assertEqual(by_key["kitsu:1"].status, MediaStatusEnum.FINISHED)
            self.assertFalse(by_key["kitsu:1"].in_backlog)
            self.assertIsNone(by_key["kitsu:2"].status)
            self.assertTrue(by_key["kitsu:2"].in_backlog)
            self.assertEqual(
                by_key["kitsu:2"].external_refs["syncProvider"], "kitsu"
            )
        finally:
            db.close()

    def test_commit_registers_library_sync_link(self) -> None:
        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.library_sync import LibrarySyncLink
        from schemas.import_schemas import ImportCommitEntry, ImportCommitRequest
        from services.importer.commit import committer

        user_id = self._user()
        db = SessionLocal()
        try:
            response = committer.commit(
                db,
                ImportCommitRequest(
                    user_id=user_id,
                    entries=[
                        ImportCommitEntry(
                            key="kitsu:9",
                            media_type=MediaTypeEnum.ANIME,
                            external_id="999",
                            title="Linked",
                            status=MediaStatusEnum.FINISHED,
                            source="kitsu",
                            external_refs={
                                "syncProvider": "kitsu",
                                "syncSourceId": "kitsu:9",
                                "syncStatus": "completed",
                            },
                        )
                    ],
                ),
            )
            self.assertEqual(response.imported, 1)
            link = (
                db.query(LibrarySyncLink)
                .filter_by(user_id=user_id, provider="kitsu", source_id="kitsu:9")
                .one()
            )
            self.assertIsNotNone(link.media_id)
            self.assertEqual(link.last_source_status, "completed")
            self.assertEqual(link.last_action, "finished")
        finally:
            db.close()

    def test_anilist_provider_parses_public_list(self) -> None:
        import asyncio
        import json
        from unittest.mock import patch

        from models.library_sync import LibraryConnection
        from services.anilist_service import service as anilist_service
        from services.library_sync.providers.anilist import AniListProvider

        connection = LibraryConnection(
            user_id=1,
            provider="anilist",
            auth_json=json.dumps({"username": "someone"}),
        )
        payload = {
            "MediaListCollection": {
                "lists": [
                    {
                        "entries": [
                            {
                                "status": "CURRENT",
                                "score": 8.5,
                                "notes": "nota",
                                "updatedAt": "2026-01-01T00:00:00Z",
                                "media": {
                                    "id": 123,
                                    "title": {"romaji": "Romaji", "english": "English"},
                                    "coverImage": {"large": "cover.jpg"},
                                    "description": "desc",
                                    "startDate": {"year": 2020, "month": 4, "day": 2},
                                },
                            }
                        ]
                    }
                ]
            }
        }

        async def fake_query(_query, _variables):
            return payload

        with patch.object(anilist_service, "query", fake_query):
            snapshot = asyncio.run(
                AniListProvider().fetch(connection, {})
            )
        self.assertEqual(len(snapshot.items), 1)
        item = snapshot.items[0]
        self.assertEqual(item.external_id, "123")
        self.assertEqual(item.title, "English")
        self.assertEqual(item.status, "CURRENT")
        self.assertEqual(item.rating, 8.5)
        self.assertEqual(item.year, 2020)

    def test_integration_api_config_and_mappings(self) -> None:
        user_id = self._user()
        response = self.client.get(f"/api/integrations?user_id={user_id}")
        self.assertEqual(response.status_code, 200)
        providers = {item["id"]: item for item in response.json()}
        self.assertIn("anilist", providers)
        self.assertIn("letterboxd", providers)
        self.assertIn("trakt", providers)

        response = self.client.put(
            "/api/integrations/anilist/config",
            json={
                "userId": user_id,
                "enabled": True,
                "updatesPerDay": 6,
                "username": "someone",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["connected"])
        self.assertEqual(response.json()["updatesPerDay"], 6)

        response = self.client.get(
            f"/api/integrations/anilist/statuses?user_id={user_id}"
        )
        self.assertEqual(response.status_code, 200)
        statuses = response.json()["statuses"]
        self.assertEqual(len(statuses), 6)

        response = self.client.put(
            "/api/integrations/anilist/statuses",
            json={
                "userId": user_id,
                "statuses": [
                    {"sourceStatus": "PLANNING", "action": "finished"},
                    {"sourceStatus": "CURRENT", "action": "ignore"},
                ],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        by_key = {
            item["sourceStatus"]: item["action"]
            for item in response.json()["statuses"]
        }
        self.assertEqual(by_key["PLANNING"], "finished")
        self.assertEqual(by_key["CURRENT"], "ignore")

        response = self.client.delete(
            f"/api/integrations/anilist?user_id={user_id}"
        )
        self.assertEqual(response.status_code, 204)

    def test_sync_records_read_and_library_dates(self) -> None:
        from datetime import datetime, timezone

        from database import SessionLocal
        from models.enums import MediaStatusEnum
        from models.media import Media
        from models.media_list_item import MediaListItem
        from models.media_log import MediaLog
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
            reading = Media(
                user_id=user_id,
                external_id="reading-dated",
                title="Reading Dated",
                type=MediaTypeEnum.MANGA,
            )
            planned = Media(
                user_id=user_id,
                external_id="planned-dated",
                title="Planned Dated",
                type=MediaTypeEnum.MANGA,
            )
            db.add_all([reading, planned])
            db.commit()
            db.refresh(reading)
            db.refresh(planned)

            in_library = "1748018141"
            last_read = "1748023530"
            expected_start = datetime.fromtimestamp(
                int(in_library), tz=timezone.utc
            ).replace(tzinfo=None)
            expected_read = datetime.fromtimestamp(
                int(last_read), tz=timezone.utc
            ).replace(tzinfo=None)
            categories = [
                {"id": 1, "name": "Reading", "order": 2, "default": False},
                {"id": 2, "name": "Plan To Read", "order": 3, "default": False},
                {"id": 4, "name": "Completed", "order": 4, "default": False},
            ]
            library = [
                {
                    "id": 101,
                    "title": "Reading Dated",
                    "inLibraryAt": in_library,
                    "lastReadChapter": {"lastReadAt": last_read},
                    "categories": {"nodes": [{"id": 1}]},
                },
                {
                    "id": 102,
                    "title": "Planned Dated",
                    "inLibraryAt": in_library,
                    "categories": {"nodes": [{"id": 2}]},
                },
            ]

            summary = service._empty_summary(datetime.now())
            asyncio.run(service._run(db, account, categories, library, summary))
            db.commit()

            log = (
                db.query(MediaLog)
                .filter_by(user_id=user_id, media_id=reading.id)
                .one()
            )
            self.assertEqual(log.date, expected_read.date())
            self.assertEqual(log.status, MediaStatusEnum.IN_PROGRESS)
            self.assertEqual(log.start_date, expected_start)
            self.assertIsNone(log.end_date)
            self.assertEqual(summary["logsCreated"], 1)
            self.assertEqual(summary["datedItems"], 2)

            backlog = (
                db.query(MediaListItem)
                .filter_by(user_id=user_id, media_id=planned.id, list_type="backlog")
                .one()
            )
            self.assertEqual(backlog.date_log, expected_start)

            # Um capítulo novo (nova data) sem mudança de status não cria log.
            next_read = "1748110000"
            expected_next = datetime.fromtimestamp(
                int(next_read), tz=timezone.utc
            ).replace(tzinfo=None)
            library[0]["lastReadChapter"] = {"lastReadAt": next_read}
            summary = service._empty_summary(datetime.now())
            asyncio.run(service._run(db, account, categories, library, summary))
            db.commit()
            self.assertEqual(
                db.query(MediaLog)
                .filter_by(user_id=user_id, media_id=reading.id)
                .count(),
                1,
            )
            self.assertEqual(summary["logsCreated"], 0)

            # Já uma mudança de status cria um novo log com a data da leitura.
            library[0]["categories"] = {"nodes": [{"id": 4}]}
            summary = service._empty_summary(datetime.now())
            asyncio.run(service._run(db, account, categories, library, summary))
            db.commit()
            logs = (
                db.query(MediaLog)
                .filter_by(user_id=user_id, media_id=reading.id)
                .order_by(MediaLog.id)
                .all()
            )
            self.assertEqual(len(logs), 2)
            self.assertEqual(logs[1].status, MediaStatusEnum.FINISHED)
            self.assertEqual(logs[1].date, expected_next.date())
            self.assertEqual(logs[1].end_date, expected_next)
            self.assertEqual(summary["logsCreated"], 1)
            self.assertEqual(reading.status, MediaStatusEnum.FINISHED)
        finally:
            db.close()

    def test_provider_import_adds_log_dates(self) -> None:
        from datetime import datetime, timezone

        from database import SessionLocal
        from models.enums import MediaStatusEnum
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
                    {"id": 1, "name": "Reading", "order": 2, "default": False}
                ]

            async def get_library(self):
                return [
                    {
                        "id": 201,
                        "title": "Dated Import",
                        "inLibraryAt": "1748018141",
                        "lastReadChapter": {"lastReadAt": "1748023530"},
                        "categories": {"nodes": [{"id": 1}]},
                    }
                ]

        db = SessionLocal()
        try:
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
            db.commit()
            with patch.object(provider_module, "SuwayomiClient", FakeClient):
                preview = asyncio.run(
                    SuwayomiProvider().parse_url(
                        "http://suwayomi.test", MediaTypeEnum.MANGA, db, user_id
                    )
                )
            entry = preview.entries[0]
            self.assertEqual(preview.counts.with_logs, 1)
            self.assertEqual(len(entry.logs), 1)
            expected_read = datetime.fromtimestamp(
                1748023530, tz=timezone.utc
            ).replace(tzinfo=None)
            self.assertEqual(entry.logs[0].date, expected_read.date())
            self.assertEqual(entry.logs[0].status, MediaStatusEnum.IN_PROGRESS)
        finally:
            db.close()

    def test_ignored_import_item_is_skipped_by_sync(self) -> None:
        import json
        from datetime import datetime

        from database import SessionLocal
        from models.enums import MediaStatusEnum
        from models.import_job import ImportJob, ImportJobItem
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
            job = ImportJob(
                user_id=user_id,
                provider="suwayomi",
                media_type=MediaTypeEnum.MANGA,
                input_type="url",
                status="ready",
            )
            db.add(job)
            db.flush()
            item = ImportJobItem(
                job_id=job.id,
                key="suwayomi:301",
                media_type=MediaTypeEnum.MANGA,
                title="Ignored Manga",
                source="suwayomi",
                external_refs_json=json.dumps({"suwayomiId": "301"}),
                match_status="not_found",
            )
            db.add(item)
            db.commit()
            job_id, item_id = job.id, item.id
        finally:
            db.close()

        response = self.client.patch(
            f"/api/import/jobs/{job_id}/items/{item_id}?user_id={user_id}",
            json={"ignored": True},
        )
        self.assertEqual(response.status_code, 200, response.text)

        db = SessionLocal()
        try:
            link = (
                db.query(SuwayomiMangaLink)
                .filter_by(user_id=user_id, suwayomi_manga_id=301)
                .one()
            )
            self.assertTrue(link.ignored)
            link_id = link.id

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
            db.commit()

            categories = [
                {"id": 1, "name": "Reading", "order": 2, "default": False}
            ]
            library = [
                {
                    "id": 301,
                    "title": "Ignored Manga",
                    "categories": {"nodes": [{"id": 1}]},
                }
            ]
            summary = service._empty_summary(datetime.now())
            asyncio.run(service._run(db, account, categories, library, summary))
            db.commit()

            self.assertEqual(summary["ignoredSkipped"], 1)
            self.assertEqual(summary["unmatched"], [])
            self.assertEqual(
                db.query(Media)
                .filter_by(user_id=user_id, title="Ignored Manga")
                .count(),
                0,
            )
        finally:
            db.close()

        response = self.client.patch(
            f"/api/suwayomi/links/{link_id}?user_id={user_id}",
            json={"ignored": False},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["ignored"])

        db = SessionLocal()
        try:
            media = Media(
                user_id=user_id,
                external_id=f"ignored-{uuid.uuid4().hex}",
                title="Ignored Manga",
                type=MediaTypeEnum.MANGA,
            )
            db.add(media)
            db.commit()

            account = (
                db.query(SuwayomiAccount).filter_by(user_id=user_id).one()
            )
            categories = [
                {"id": 1, "name": "Reading", "order": 2, "default": False}
            ]
            library = [
                {
                    "id": 301,
                    "title": "Ignored Manga",
                    "categories": {"nodes": [{"id": 1}]},
                }
            ]
            summary = service._empty_summary(datetime.now())
            asyncio.run(service._run(db, account, categories, library, summary))
            db.commit()

            db.refresh(media)
            self.assertEqual(media.status, MediaStatusEnum.IN_PROGRESS)
            self.assertEqual(summary["ignoredSkipped"], 0)
            self.assertEqual(summary["statusUpdated"], 1)
            self.assertEqual(summary["unmatched"], [])
        finally:
            db.close()

    def test_suwayomi_link_endpoints(self) -> None:
        import uuid as uuid_module

        from database import SessionLocal
        from models.media import Media
        from models.suwayomi import SuwayomiMangaLink

        user_id = self._user()
        db = SessionLocal()
        try:
            pending = SuwayomiMangaLink(
                user_id=user_id,
                suwayomi_manga_id=401,
                title="Pending A",
                match_error="Título não encontrado.",
                match_attempts=2,
            )
            ignored = SuwayomiMangaLink(
                user_id=user_id,
                suwayomi_manga_id=402,
                title="Pending B",
                ignored=True,
            )
            manga = Media(
                user_id=user_id,
                external_id=f"manga-{uuid_module.uuid4().hex}",
                title="Existing Manga",
                type=MediaTypeEnum.MANGA,
            )
            movie = Media(
                user_id=user_id,
                external_id=f"movie-{uuid_module.uuid4().hex}",
                title="Existing Movie",
                type=MediaTypeEnum.MOVIES,
            )
            db.add_all([pending, ignored, manga, movie])
            db.commit()
            pending_id, ignored_id = pending.id, ignored.id
            manga_id, movie_id = manga.id, movie.id
        finally:
            db.close()

        response = self.client.get(
            f"/api/suwayomi/links?user_id={user_id}&status=pending"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([link["title"] for link in response.json()], ["Pending A"])

        response = self.client.get(
            f"/api/suwayomi/links?user_id={user_id}&status=ignored"
        )
        self.assertEqual([link["id"] for link in response.json()], [ignored_id])

        response = self.client.patch(
            f"/api/suwayomi/links/{pending_id}?user_id={user_id}",
            json={"ignored": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ignored"])
        self.assertIsNone(response.json()["matchError"])
        self.assertEqual(response.json()["matchAttempts"], 0)

        response = self.client.patch(
            f"/api/suwayomi/links/{pending_id}?user_id={user_id}",
            json={"mediaId": manga_id},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["mediaId"], manga_id)
        self.assertFalse(response.json()["ignored"])

        response = self.client.patch(
            f"/api/suwayomi/links/{ignored_id}?user_id={user_id}",
            json={"mediaId": movie_id},
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_commit_registers_ignored_suwayomi_items(self) -> None:
        import json

        from database import SessionLocal
        from models.import_job import ImportJob, ImportJobItem
        from models.suwayomi import SuwayomiMangaLink
        from schemas.import_schemas import ImportCandidate
        from services.import_jobs.serializers import candidate_to_json

        user_id = self._user()
        db = SessionLocal()
        try:
            job = ImportJob(
                user_id=user_id,
                provider="suwayomi",
                media_type=MediaTypeEnum.MANGA,
                input_type="url",
                status="ready",
            )
            db.add(job)
            db.flush()
            ignored_item = ImportJobItem(
                job_id=job.id,
                key="suwayomi:501",
                media_type=MediaTypeEnum.MANGA,
                title="Ignored On Review",
                source="suwayomi",
                external_refs_json=json.dumps({"suwayomiId": "501"}),
                match_status="not_found",
                ignored=True,
            )
            candidate = ImportCandidate(
                provider="anilist",
                external_id=f"anilist-{uuid.uuid4().hex}",
                media_type=MediaTypeEnum.MANGA,
                title="Selected Manga",
            )
            selected_item = ImportJobItem(
                job_id=job.id,
                key="suwayomi:502",
                media_type=MediaTypeEnum.MANGA,
                title="Selected Manga",
                source="suwayomi",
                external_refs_json=json.dumps({"suwayomiId": "502"}),
                match_status="matched",
                selected_json=candidate_to_json(candidate),
            )
            db.add_all([ignored_item, selected_item])
            db.commit()
            job_id = job.id
        finally:
            db.close()

        response = self.client.post(
            f"/api/import/jobs/{job_id}/commit?user_id={user_id}",
            json={"addToBacklog": False},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["imported"], 1)

        db = SessionLocal()
        try:
            link = (
                db.query(SuwayomiMangaLink)
                .filter_by(user_id=user_id, suwayomi_manga_id=501)
                .one()
            )
            self.assertTrue(link.ignored)
            self.assertIsNone(link.media_id)
            selected_link = (
                db.query(SuwayomiMangaLink)
                .filter_by(user_id=user_id, suwayomi_manga_id=502)
                .one()
            )
            self.assertIsNotNone(selected_link.media_id)
            self.assertFalse(selected_link.ignored)
        finally:
            db.close()

    def test_backfill_marks_ignored_import_items(self) -> None:
        import json

        from database import SessionLocal
        from models.import_job import ImportJob, ImportJobItem
        from models.suwayomi import SuwayomiMangaLink
        from services.suwayomi.backfill import backfill

        user_id = self._user()
        db = SessionLocal()
        try:
            job = ImportJob(
                user_id=user_id,
                provider="suwayomi",
                media_type=MediaTypeEnum.MANGA,
                input_type="url",
                status="done",
            )
            db.add(job)
            db.flush()
            db.add(
                ImportJobItem(
                    job_id=job.id,
                    key="suwayomi:601",
                    media_type=MediaTypeEnum.MANGA,
                    title="Old Ignored",
                    source="suwayomi",
                    external_refs_json=json.dumps({"suwayomiId": "601"}),
                    match_status="not_found",
                    ignored=True,
                )
            )
            pending = SuwayomiMangaLink(
                user_id=user_id,
                suwayomi_manga_id=601,
                title="Old Ignored",
                match_error="Título não encontrado.",
            )
            restored = SuwayomiMangaLink(
                user_id=user_id,
                suwayomi_manga_id=602,
                title="Restored",
                match_error=None,
            )
            db.add_all([pending, restored])
            db.commit()

            created, ignored = backfill(db)
            self.assertEqual(created, 0)
            self.assertEqual(ignored, 1)
            db.refresh(pending)
            db.refresh(restored)
            self.assertTrue(pending.ignored)
            self.assertIsNone(pending.match_error)
            self.assertFalse(restored.ignored)

            created, ignored = backfill(db)
            self.assertEqual((created, ignored), (0, 0))
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


class LibrarySyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app, raise_server_exceptions=False)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)

    def _user(self) -> int:
        username = f"libsync_{uuid.uuid4().hex[:10]}"
        response = self.client.post(
            "/auth/register", json={"username": username, "password": "secret"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def _provider(self, items):
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from services.library_sync.providers.base import (
            LibraryProvider,
            SourceSnapshot,
            SourceStatus,
        )

        class FakeProvider(LibraryProvider):
            id = "fake"
            label = "Fake"
            media_types = [MediaTypeEnum.MOVIES]
            auth_kind = "username"

            def statuses(self):
                return [
                    SourceStatus(
                        "watched", "Assistido", "finished", MediaStatusEnum.FINISHED
                    ),
                    SourceStatus("watchlist", "Watchlist", "backlog", None),
                ]

            async def fetch(self, connection, cursor):
                return SourceSnapshot(
                    items=list(items),
                    statuses=self.statuses(),
                    cursor={"seen": True},
                )

        return FakeProvider()

    def _connection(self, db, user_id):
        import json

        from models.library_sync import LibraryConnection

        connection = LibraryConnection(
            user_id=user_id,
            provider="fake",
            enabled=True,
            updates_per_day=4,
            auth_json=json.dumps({"username": "tester"}),
            cursor_json="{}",
        )
        db.add(connection)
        db.commit()
        db.refresh(connection)
        return connection

    @staticmethod
    def _exact_matcher(external_id: str, title: str, media_type):
        from schemas.import_schemas import (
            ImportCandidate,
            ImportMatchResponse,
            ImportMatchResult,
        )

        async def fake(db, request, tmdb_key, igdb):
            candidate = ImportCandidate(
                provider="tmdb",
                external_id=external_id,
                media_type=media_type,
                title=title,
                score=1.0,
            )
            return ImportMatchResponse(
                results=[
                    ImportMatchResult(
                        key=request.items[0].key,
                        status="matched",
                        match=candidate,
                        candidates=[candidate],
                    )
                ]
            )

        return fake

    def test_baseline_links_existing_and_does_not_overwrite(self) -> None:
        import asyncio
        import json
        from unittest.mock import patch

        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.library_sync import LibrarySyncLink
        from models.media import Media
        from services.library_sync import engine as engine_module
        from services.library_sync.providers.base import SourceItem

        user_id = self._user()
        db = SessionLocal()
        try:
            media = Media(
                user_id=user_id,
                external_id="tmdb-1",
                title="Existing",
                type=MediaTypeEnum.MOVIES,
                status=MediaStatusEnum.DROPPED,
            )
            db.add(media)
            db.commit()
            db.refresh(media)
            connection = self._connection(db, user_id)
            provider = self._provider(
                [
                    SourceItem(
                        source_id="s1",
                        media_type=MediaTypeEnum.MOVIES,
                        title="Existing",
                        status="watched",
                        external_id="tmdb-1",
                        rating=8.0,
                    ),
                    SourceItem(
                        source_id="s2",
                        media_type=MediaTypeEnum.MOVIES,
                        title="Missing",
                        status="watchlist",
                        external_id="tmdb-2",
                    ),
                ]
            )
            with patch.object(engine_module, "get_provider", return_value=provider):
                summary = asyncio.run(engine_module.engine.sync(db, connection))
            db.expire_all()
            media = db.get(Media, media.id)
            self.assertEqual(media.status, MediaStatusEnum.DROPPED)
            self.assertEqual(media.rating, 8.0)
            self.assertEqual(summary["missing"], 1)
            self.assertEqual(summary["created"], 0)
            self.assertTrue(summary["baseline"])
            links = db.query(LibrarySyncLink).all()
            self.assertEqual(len(links), 1)
            self.assertEqual(links[0].source_id, "s1")
            self.assertEqual(links[0].media_id, media.id)
            self.assertIn(
                "s2", json.loads(connection.cursor_json)["baseline_missing"]
            )
        finally:
            db.close()

    def test_new_item_created_backlog_and_status_change(self) -> None:
        import asyncio
        from unittest.mock import patch

        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.media import Media
        from models.media_list_item import MediaListItem
        from services.library_sync import engine as engine_module
        from services.library_sync.providers.base import SourceItem

        user_id = self._user()
        db = SessionLocal()
        try:
            connection = self._connection(db, user_id)
            first = self._provider([])
            with patch.object(engine_module, "get_provider", return_value=first):
                asyncio.run(engine_module.engine.sync(db, connection))

            item = SourceItem(
                source_id="new-1",
                media_type=MediaTypeEnum.MOVIES,
                title="New One",
                status="watchlist",
                external_id="tmdb-3",
            )
            provider = self._provider([item])
            with (
                patch.object(engine_module, "get_provider", return_value=provider),
                patch.object(
                    engine_module.matcher,
                    "match",
                    new=self._exact_matcher(
                        "tmdb-3", "New One", MediaTypeEnum.MOVIES
                    ),
                ),
            ):
                summary = asyncio.run(engine_module.engine.sync(db, connection))
            db.expire_all()
            media = (
                db.query(Media)
                .filter_by(user_id=user_id, external_id="tmdb-3")
                .one()
            )
            self.assertEqual(summary["created"], 1)
            self.assertEqual(summary["backlogAdded"], 1)
            self.assertIsNone(media.status)
            self.assertEqual(
                db.query(MediaListItem)
                .filter_by(user_id=user_id, media_id=media.id, list_type="backlog")
                .count(),
                1,
            )

            item.status = "watched"
            provider = self._provider([item])
            with (
                patch.object(engine_module, "get_provider", return_value=provider),
                patch.object(
                    engine_module.matcher,
                    "match",
                    new=self._exact_matcher(
                        "tmdb-3", "New One", MediaTypeEnum.MOVIES
                    ),
                ),
            ):
                summary = asyncio.run(engine_module.engine.sync(db, connection))
            db.expire_all()
            media = db.get(Media, media.id)
            self.assertEqual(media.status, MediaStatusEnum.FINISHED)
            self.assertEqual(summary["statusUpdated"], 1)
            self.assertEqual(summary["backlogRemoved"], 1)
            self.assertEqual(
                db.query(MediaListItem)
                .filter_by(user_id=user_id, media_id=media.id)
                .count(),
                0,
            )
        finally:
            db.close()

    def test_manual_status_is_preserved_until_source_changes(self) -> None:
        import asyncio
        from unittest.mock import patch

        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.media import Media
        from services.library_sync import engine as engine_module
        from services.library_sync.providers.base import SourceItem

        user_id = self._user()
        db = SessionLocal()
        try:
            connection = self._connection(db, user_id)
            first = self._provider([])
            with patch.object(engine_module, "get_provider", return_value=first):
                asyncio.run(engine_module.engine.sync(db, connection))
            item = SourceItem(
                source_id="manual-1",
                media_type=MediaTypeEnum.MOVIES,
                title="Manual",
                status="watched",
                external_id="tmdb-4",
            )
            provider = self._provider([item])
            with (
                patch.object(engine_module, "get_provider", return_value=provider),
                patch.object(
                    engine_module.matcher,
                    "match",
                    new=self._exact_matcher(
                        "tmdb-4", "Manual", MediaTypeEnum.MOVIES
                    ),
                ),
            ):
                asyncio.run(engine_module.engine.sync(db, connection))
            media = (
                db.query(Media)
                .filter_by(user_id=user_id, external_id="tmdb-4")
                .one()
            )
            media.status = MediaStatusEnum.ON_HOLD
            db.commit()

            with (
                patch.object(engine_module, "get_provider", return_value=provider),
                patch.object(
                    engine_module.matcher,
                    "match",
                    new=self._exact_matcher(
                        "tmdb-4", "Manual", MediaTypeEnum.MOVIES
                    ),
                ),
            ):
                summary = asyncio.run(engine_module.engine.sync(db, connection))
            db.expire_all()
            media = db.get(Media, media.id)
            self.assertEqual(media.status, MediaStatusEnum.ON_HOLD)
            self.assertEqual(summary["statusUpdated"], 0)

            item.status = "watchlist"
            provider = self._provider([item])
            with (
                patch.object(engine_module, "get_provider", return_value=provider),
                patch.object(
                    engine_module.matcher,
                    "match",
                    new=self._exact_matcher(
                        "tmdb-4", "Manual", MediaTypeEnum.MOVIES
                    ),
                ),
            ):
                asyncio.run(engine_module.engine.sync(db, connection))
            db.expire_all()
            media = db.get(Media, media.id)
            self.assertEqual(media.status, MediaStatusEnum.ON_HOLD)
        finally:
            db.close()

    def _review_provider(self, items):
        provider = self._provider(items)
        provider.review_baseline = True
        provider.creates_logs = True
        return provider

    def _match_response(self, request, *, status, external_id, title, media_type, error=None):
        from schemas.import_schemas import (
            ImportCandidate,
            ImportMatchResponse,
            ImportMatchResult,
        )

        candidate = (
            ImportCandidate(
                provider="tmdb",
                external_id=external_id,
                media_type=media_type,
                title=title,
                year=2026,
                cover_url="https://example.test/cover.jpg",
            )
            if external_id
            else None
        )
        return ImportMatchResponse(
            results=[
                ImportMatchResult(
                    key=request.items[0].key,
                    status=status,
                    match=candidate,
                    candidates=[candidate] if candidate else [],
                    error=error,
                )
            ]
        )

    def test_review_provider_auto_creates_exact_match_with_log(self) -> None:
        import asyncio
        from datetime import date
        from unittest.mock import patch

        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.library_sync import LibrarySyncSuggestion
        from models.media import Media
        from models.media_log import MediaLog
        from services.library_sync import engine as engine_module
        from services.library_sync.providers.base import SourceItem

        user_id = self._user()
        db = SessionLocal()
        try:
            connection = self._connection(db, user_id)
            source_id = f"perfect-{uuid.uuid4().hex}"
            external_id = f"tmdb-{uuid.uuid4().hex}"
            item = SourceItem(
                source_id=source_id,
                media_type=MediaTypeEnum.MOVIES,
                title="Perfect Movie",
                status="watched",
                external_id=external_id,
                rating=9.0,
                review="Great.",
                watched_at=date(2026, 11, 1),
            )
            provider = self._review_provider([item])

            async def fake_match(db_session, request, tmdb_key, igdb):
                return self._match_response(
                    request,
                    status="matched",
                    external_id=external_id,
                    title="Perfect Movie",
                    media_type=MediaTypeEnum.MOVIES,
                )

            with (
                patch.object(engine_module, "get_provider", return_value=provider),
                patch.object(engine_module.matcher, "match", new=fake_match),
            ):
                summary = asyncio.run(engine_module.engine.sync(db, connection))

            db.expire_all()
            media = (
                db.query(Media)
                .filter_by(user_id=user_id, external_id=external_id)
                .one()
            )
            self.assertEqual(media.status, MediaStatusEnum.FINISHED)
            self.assertEqual(media.rating, 9.0)
            self.assertEqual(media.review, "Great.")
            self.assertEqual(summary["created"], 1)
            self.assertEqual(summary["missing"], 0)
            self.assertEqual(summary["logsCreated"], 1)
            self.assertEqual(summary["pendingReview"], 0)
            log = db.query(MediaLog).filter_by(media_id=media.id).one()
            self.assertEqual(log.date, date(2026, 11, 1))
            self.assertEqual(log.status, MediaStatusEnum.FINISHED)
            suggestion = (
                db.query(LibrarySyncSuggestion)
                .filter_by(user_id=user_id)
                .one()
            )
            self.assertEqual(suggestion.review_state, "approved")
            self.assertEqual(suggestion.origin, "auto")
            self.assertEqual(suggestion.media_id, media.id)
            self.assertIsNotNone(suggestion.sync_run_at)
            self.assertIsNotNone(suggestion.candidates_json)
        finally:
            db.close()

    def test_review_provider_stages_non_exact_and_approves(self) -> None:
        import asyncio
        from datetime import date
        from unittest.mock import patch

        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.library_sync import LibrarySyncSuggestion
        from models.media import Media
        from models.media_log import MediaLog
        from services.library_sync import engine as engine_module
        from services.library_sync.providers.base import SourceItem

        user_id = self._user()
        db = SessionLocal()
        try:
            connection = self._connection(db, user_id)
            source_id = f"maybe-{uuid.uuid4().hex}"
            source_external = f"tmdb-{uuid.uuid4().hex}"
            candidate_external = f"tmdb-{uuid.uuid4().hex}"
            item = SourceItem(
                source_id=source_id,
                media_type=MediaTypeEnum.MOVIES,
                title="Maybe Movie",
                status="watched",
                external_id=source_external,
                rating=7.0,
                watched_at=date(2026, 10, 2),
            )
            provider = self._review_provider([item])

            async def fake_match(db_session, request, tmdb_key, igdb):
                return self._match_response(
                    request,
                    status="matched",
                    external_id=candidate_external,
                    title="Maybe Movie (Other)",
                    media_type=MediaTypeEnum.MOVIES,
                )

            with (
                patch.object(engine_module, "get_provider", return_value=provider),
                patch.object(engine_module.matcher, "match", new=fake_match),
            ):
                summary = asyncio.run(engine_module.engine.sync(db, connection))

            db.expire_all()
            self.assertEqual(summary["created"], 0)
            self.assertEqual(summary["missing"], 1)
            self.assertEqual(summary["reviewStaged"], 1)
            self.assertEqual(summary["pendingReview"], 1)
            suggestion = (
                db.query(LibrarySyncSuggestion)
                .filter_by(user_id=user_id, source_id=source_id)
                .one()
            )
            self.assertEqual(suggestion.review_state, "pending")
            self.assertIn("Match não exato", suggestion.error or "")
            self.assertEqual(
                db.query(Media)
                .filter_by(user_id=user_id, external_id=candidate_external)
                .count(),
                0,
            )

            response = self.client.post(
                "/api/integrations/suggestions/approve",
                json={"userId": user_id, "ids": [suggestion.id]},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["approved"], 1)

            db.expire_all()
            suggestion = db.get(LibrarySyncSuggestion, suggestion.id)
            self.assertEqual(suggestion.review_state, "approved")
            self.assertIsNotNone(suggestion.media_id)
            media = (
                db.query(Media)
                .filter_by(user_id=user_id, external_id=candidate_external)
                .one()
            )
            self.assertEqual(media.status, MediaStatusEnum.FINISHED)
            self.assertEqual(
                db.query(MediaLog).filter_by(media_id=media.id).count(), 1
            )
            self.assertEqual(suggestion.media_id, media.id)
        finally:
            db.close()

    def test_review_suggestion_dismiss_and_restore(self) -> None:
        import asyncio
        from unittest.mock import patch

        from database import SessionLocal
        from models.enums import MediaTypeEnum
        from models.library_sync import LibrarySyncSuggestion
        from models.media import Media
        from services.library_sync import engine as engine_module
        from services.library_sync.providers.base import SourceItem

        user_id = self._user()
        db = SessionLocal()
        try:
            connection = self._connection(db, user_id)
            source_id = f"dismiss-{uuid.uuid4().hex}"
            item = SourceItem(
                source_id=source_id,
                media_type=MediaTypeEnum.MOVIES,
                title="Dismiss Movie",
                status="watched",
            )
            provider = self._review_provider([item])
            calls = {"value": 0}

            async def fake_match(db_session, request, tmdb_key, igdb):
                calls["value"] += 1
                return self._match_response(
                    request,
                    status="not_found",
                    external_id=None,
                    title="",
                    media_type=MediaTypeEnum.MOVIES,
                    error="Nada encontrado.",
                )

            with (
                patch.object(engine_module, "get_provider", return_value=provider),
                patch.object(engine_module.matcher, "match", new=fake_match),
            ):
                asyncio.run(engine_module.engine.sync(db, connection))
            suggestion = (
                db.query(LibrarySyncSuggestion)
                .filter_by(user_id=user_id, source_id=source_id)
                .one()
            )
            self.assertEqual(calls["value"], 1)

            response = self.client.patch(
                f"/api/integrations/suggestions/{suggestion.id}",
                json={"userId": user_id, "state": "dismissed"},
            )
            self.assertEqual(response.status_code, 200, response.text)

            with (
                patch.object(engine_module, "get_provider", return_value=provider),
                patch.object(engine_module.matcher, "match", new=fake_match),
            ):
                summary = asyncio.run(engine_module.engine.sync(db, connection))
            db.expire_all()
            self.assertEqual(calls["value"], 1)
            self.assertEqual(summary["missing"], 0)
            suggestion = db.get(LibrarySyncSuggestion, suggestion.id)
            self.assertEqual(suggestion.review_state, "dismissed")
            self.assertEqual(
                db.query(Media)
                .filter_by(user_id=user_id, title="Dismiss Movie")
                .count(),
                0,
            )

            response = self.client.patch(
                f"/api/integrations/suggestions/{suggestion.id}",
                json={"userId": user_id, "state": "pending"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["reviewState"], "pending")
        finally:
            db.close()

    def test_letterboxd_feed_is_reprocessed_after_baseline(self) -> None:
        import asyncio
        from datetime import datetime
        from unittest.mock import AsyncMock, patch

        from models.library_sync import LibraryConnection
        from services.library_sync.providers.letterboxd import LetterboxdProvider

        xml = b"""<?xml version='1.0' encoding='utf-8'?>
        <rss version="2.0" xmlns:letterboxd="https://letterboxd.com"
             xmlns:tmdb="https://themoviedb.org">
          <channel><item>
            <title>Dune: Part Two</title>
            <link>https://letterboxd.com/user/film/dune-part-two-2024/</link>
            <guid>letterboxd-watch-123</guid>
            <letterboxd:filmTitle>Dune: Part Two</letterboxd:filmTitle>
            <letterboxd:filmYear>2024</letterboxd:filmYear>
            <letterboxd:memberRating>4.0</letterboxd:memberRating>
            <letterboxd:watchedDate>2024-01-01</letterboxd:watchedDate>
            <tmdb:movieId>693134</tmdb:movieId>
          </item></channel>
        </rss>"""
        connection = LibraryConnection(
            user_id=1,
            provider="letterboxd",
            baseline_completed_at=datetime(2026, 1, 1),
            auth_json='{"username": "tester"}',
            cursor_json='{"seen_guids": ["letterboxd-watch-123"]}',
        )
        provider = LetterboxdProvider()
        with patch.object(provider, "_feed", new=AsyncMock(return_value=xml)):
            first = asyncio.run(provider.fetch(connection, {"seen_guids": ["letterboxd-watch-123"]}))
            second = asyncio.run(provider.fetch(connection, first.cursor))
        self.assertEqual(len(first.items), 1)
        self.assertEqual(len(second.items), 1)
        self.assertEqual(first.items[0].watched_at.isoformat(), "2024-01-01")

    def test_suggestions_list_count_and_provider_info(self) -> None:
        from datetime import date

        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.library_sync import LibrarySyncSuggestion

        user_id = self._user()
        db = SessionLocal()
        try:
            db.add(
                LibrarySyncSuggestion(
                    user_id=user_id,
                    provider="letterboxd",
                    source_id=f"letterboxd:{uuid.uuid4().hex}",
                    media_type=MediaTypeEnum.MOVIES,
                    title="Review Movie",
                    year=2026,
                    source_status="watched",
                    source_label="Assistido",
                    action="finished",
                    target_status=MediaStatusEnum.FINISHED,
                    rating=8.0,
                    review="Good.",
                    watched_date=date(2026, 11, 3),
                    review_state="pending",
                )
            )
            db.commit()
        finally:
            db.close()

        response = self.client.get(
            f"/api/integrations/suggestions?user_id={user_id}&state=pending"
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["counts"]["pending"], 1)
        item = body["items"][0]
        self.assertEqual(item["title"], "Review Movie")
        self.assertEqual(item["targetStatus"], "finished")
        self.assertEqual(item["reviewState"], "pending")

        response = self.client.get(
            f"/api/integrations/suggestions/count?user_id={user_id}"
        )
        self.assertEqual(response.json()["pending"], 1)

        response = self.client.get(
            f"/api/integrations/letterboxd?user_id={user_id}"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["pendingReview"], 1)

    def test_manual_media_resolves_pending_suggestion(self) -> None:
        import asyncio
        from datetime import date
        from unittest.mock import AsyncMock, patch

        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.library_sync import LibrarySyncSuggestion
        from models.media import Media
        from models.media_log import MediaLog
        from services.library_sync import engine as engine_module
        from services.library_sync.providers.base import SourceItem

        user_id = self._user()
        db = SessionLocal()
        try:
            connection = self._connection(db, user_id)
            source_id = f"manual-resolve-{uuid.uuid4().hex}"
            external_id = f"tmdb-{uuid.uuid4().hex}"
            item = SourceItem(
                source_id=source_id,
                media_type=MediaTypeEnum.MOVIES,
                title="Resolved Movie",
                status="watched",
                external_id=external_id,
                watched_at=date(2026, 9, 9),
            )
            provider = self._review_provider([item])

            async def fake_match(db_session, request, tmdb_key, igdb):
                return self._match_response(
                    request,
                    status="matched",
                    external_id=f"tmdb-{uuid.uuid4().hex}",
                    title="Other Match",
                    media_type=MediaTypeEnum.MOVIES,
                )

            with (
                patch.object(engine_module, "get_provider", return_value=provider),
                patch.object(engine_module.matcher, "match", new=fake_match),
            ):
                asyncio.run(engine_module.engine.sync(db, connection))
            suggestion = (
                db.query(LibrarySyncSuggestion)
                .filter_by(user_id=user_id, source_id=source_id)
                .one()
            )
            self.assertEqual(suggestion.review_state, "pending")

            media = Media(
                user_id=user_id,
                external_id=external_id,
                title="Resolved Movie",
                type=MediaTypeEnum.MOVIES,
            )
            db.add(media)
            db.commit()

            with (
                patch.object(engine_module, "get_provider", return_value=provider),
                patch.object(
                    engine_module.matcher,
                    "match",
                    new=AsyncMock(side_effect=AssertionError("não deveria casar")),
                ),
            ):
                summary = asyncio.run(engine_module.engine.sync(db, connection))

            db.expire_all()
            suggestion = db.get(LibrarySyncSuggestion, suggestion.id)
            media = db.get(Media, media.id)
            self.assertEqual(suggestion.review_state, "approved")
            self.assertEqual(suggestion.media_id, media.id)
            self.assertEqual(media.status, MediaStatusEnum.FINISHED)
            self.assertEqual(summary["created"], 0)
            self.assertEqual(summary["pendingReview"], 0)
            self.assertEqual(
                db.query(MediaLog).filter_by(media_id=media.id).count(), 1
            )
        finally:
            db.close()

    def test_global_sync_settings_endpoints(self) -> None:
        user_id = self._user()

        response = self.client.get(f"/api/integrations/global?user_id={user_id}")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["enabled"])
        self.assertEqual(body["updatesPerDay"], 4)
        self.assertEqual(body["startTime"], "00:00")

        response = self.client.put(
            "/api/integrations/global",
            json={
                "userId": user_id,
                "enabled": True,
                "updatesPerDay": 8,
                "startTime": "09:30",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["updatesPerDay"], 8)
        self.assertEqual(body["startTime"], "09:30")
        self.assertIsNotNone(body["nextSyncAt"])

        response = self.client.get(f"/api/integrations/global?user_id={user_id}")
        self.assertTrue(response.json()["enabled"])
        self.assertEqual(response.json()["updatesPerDay"], 8)
        self.assertEqual(response.json()["startTime"], "09:30")

    def test_global_sync_schedule_is_anchored_to_start_time(self) -> None:
        from datetime import datetime

        from services.library_sync.orchestrator import (
            next_slot_local,
            normalize_start_time,
            schedule_slots,
        )

        self.assertEqual(
            schedule_slots("00:00", 4),
            ["00:00", "06:00", "12:00", "18:00"],
        )
        self.assertEqual(
            schedule_slots("00:00", 3),
            ["00:00", "08:00", "16:00"],
        )
        self.assertEqual(
            schedule_slots("00:00", 5),
            ["00:00", "04:48", "09:36", "14:24", "19:12"],
        )
        self.assertEqual(normalize_start_time("9:05"), "00:00")
        self.assertEqual(normalize_start_time("09:05"), "09:05")

        self.assertEqual(
            next_slot_local(datetime(2026, 1, 1, 0, 0, 1), "00:00", 4),
            datetime(2026, 1, 1, 6, 0),
        )
        self.assertEqual(
            next_slot_local(datetime(2026, 1, 1, 23, 0), "00:00", 4),
            datetime(2026, 1, 2, 0, 0),
        )
        self.assertEqual(
            next_slot_local(datetime(2026, 1, 1, 8, 0), "09:00", 4),
            datetime(2026, 1, 1, 9, 0),
        )
        self.assertEqual(
            next_slot_local(datetime(2026, 1, 1, 22, 0), "09:00", 4),
            datetime(2026, 1, 2, 3, 0),
        )
        self.assertEqual(
            next_slot_local(datetime(2026, 1, 1, 9, 0, 1), "09:00", 1),
            datetime(2026, 1, 2, 9, 0),
        )

        from database import SessionLocal
        from models.library_sync import LibrarySyncSettings
        from services.library_sync.orchestrator import next_due_at

        user_id = self._user()
        db = SessionLocal()
        try:
            settings = LibrarySyncSettings(
                user_id=user_id,
                enabled=True,
                updates_per_day=4,
                start_time="00:00",
                next_sync_at=None,
            )
            db.add(settings)
            db.commit()
            self.assertIsNotNone(next_due_at(db))
            db.delete(settings)
            db.commit()
        finally:
            db.close()

    def test_sync_all_endpoint_returns_aggregate(self) -> None:
        from unittest.mock import patch

        user_id = self._user()

        with patch(
            "routers.library_sync.sync_all_for_user",
            return_value={
                "status": "partial",
                "results": [
                    {"provider": "anilist", "status": "ok", "summary": {}},
                    {
                        "provider": "letterboxd",
                        "status": "failed",
                        "error": "RSS indisponível.",
                    },
                ],
                "ok": 1,
                "failed": 1,
            },
        ):
            response = self.client.post(
                "/api/integrations/sync-all", json={"userId": user_id}
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "partial")
        self.assertEqual(body["ok"], 1)
        self.assertEqual(body["failed"], 1)
        self.assertEqual(body["results"][1]["provider"], "letterboxd")
        self.assertEqual(body["results"][1]["error"], "RSS indisponível.")

    def test_only_included_providers_run_in_global_sync(self) -> None:
        import json
        from unittest.mock import patch

        from database import SessionLocal
        from models.library_sync import LibraryConnection, LibrarySyncSettings
        from models.suwayomi import SuwayomiAccount
        from services.library_sync import orchestrator

        user_id = self._user()
        db = SessionLocal()
        try:
            included = LibraryConnection(
                user_id=user_id,
                provider="anilist",
                enabled=True,
                updates_per_day=4,
                auth_json=json.dumps({"username": "tester"}),
                cursor_json="{}",
            )
            excluded = LibraryConnection(
                user_id=user_id,
                provider="mal",
                enabled=False,
                updates_per_day=4,
                auth_json=json.dumps({"username": "tester"}),
                cursor_json="{}",
            )
            unconnected = LibraryConnection(
                user_id=user_id,
                provider="trakt",
                enabled=True,
                updates_per_day=4,
                auth_json="{}",
                cursor_json="{}",
            )
            account = SuwayomiAccount(
                user_id=user_id,
                base_url="http://suwayomi.test",
                enabled=True,
            )
            settings = LibrarySyncSettings(
                user_id=user_id, enabled=True, updates_per_day=4
            )
            db.add_all([included, excluded, unconnected, account, settings])
            db.commit()
        finally:
            db.close()

        calls = {"generic": [], "suwayomi": 0}

        def fake_sync(user_id_arg, provider_id, _run_at=None):
            calls["generic"].append(provider_id)
            return {"status": "ok", "created": 1}

        def fake_suwayomi(user_id_arg, _run_at=None):
            calls["suwayomi"] += 1
            return {"status": "ok", "created": 2}

        with (
            patch.object(orchestrator, "sync_user", new=fake_sync),
            patch.object(orchestrator.suwayomi_sync, "sync_user", new=fake_suwayomi),
        ):
            result = orchestrator.sync_all_for_user(user_id)

        self.assertEqual(calls["generic"], ["anilist"])
        self.assertEqual(calls["suwayomi"], 1)
        self.assertEqual(result["ok"], 2)
        self.assertEqual(result["failed"], 0)

        db = SessionLocal()
        try:
            settings = (
                db.query(LibrarySyncSettings)
                .filter_by(user_id=user_id)
                .one()
            )
            self.assertEqual(settings.last_sync_status, "ok")
            self.assertIsNotNone(settings.last_sync_at)
            self.assertIsNotNone(settings.next_sync_at)
        finally:
            db.close()

    def test_non_review_provider_stages_imperfect_new_item(self) -> None:
        import asyncio
        from unittest.mock import patch

        from database import SessionLocal
        from models.enums import MediaTypeEnum
        from models.library_sync import LibrarySyncSuggestion
        from models.media import Media
        from services.library_sync import engine as engine_module
        from services.library_sync.providers.base import SourceItem

        user_id = self._user()
        db = SessionLocal()
        try:
            connection = self._connection(db, user_id)
            provider = self._provider([])
            with patch.object(engine_module, "get_provider", return_value=provider):
                asyncio.run(engine_module.engine.sync(db, connection))

            item = SourceItem(
                source_id="stage-1",
                media_type=MediaTypeEnum.MOVIES,
                title="Uncertain Movie",
                status="watched",
                external_id=f"tmdb-{uuid.uuid4().hex}",
            )
            provider = self._provider([item])

            async def fake_match(db_session, request, tmdb_key, igdb):
                return self._match_response(
                    request,
                    status="matched",
                    external_id=f"tmdb-{uuid.uuid4().hex}",
                    title="Uncertain Movie (Other)",
                    media_type=MediaTypeEnum.MOVIES,
                )

            with (
                patch.object(engine_module, "get_provider", return_value=provider),
                patch.object(engine_module.matcher, "match", new=fake_match),
            ):
                summary = asyncio.run(engine_module.engine.sync(db, connection))

            db.expire_all()
            self.assertEqual(summary["created"], 0)
            self.assertEqual(summary["reviewStaged"], 1)
            self.assertEqual(summary["pendingReview"], 1)
            self.assertEqual(
                db.query(Media).filter_by(user_id=user_id).count(), 0
            )
            self.assertEqual(
                db.query(LibrarySyncSuggestion)
                .filter_by(user_id=user_id, source_id="stage-1")
                .count(),
                1,
            )
        finally:
            db.close()

    def test_baseline_missing_is_not_staged_later(self) -> None:
        import asyncio
        from unittest.mock import patch

        from database import SessionLocal
        from models.enums import MediaTypeEnum
        from models.library_sync import LibrarySyncSuggestion
        from services.library_sync import engine as engine_module
        from services.library_sync.providers.base import SourceItem

        user_id = self._user()
        db = SessionLocal()
        try:
            connection = self._connection(db, user_id)
            item = SourceItem(
                source_id="baseline-missing",
                media_type=MediaTypeEnum.MOVIES,
                title="Old Missing",
                status="watched",
                external_id=f"tmdb-{uuid.uuid4().hex}",
            )
            provider = self._provider([item])
            calls = {"value": 0}

            async def fake_match(db_session, request, tmdb_key, igdb):
                calls["value"] += 1
                return self._match_response(
                    request,
                    status="not_found",
                    external_id=None,
                    title="",
                    media_type=MediaTypeEnum.MOVIES,
                )

            with (
                patch.object(engine_module, "get_provider", return_value=provider),
                patch.object(engine_module.matcher, "match", new=fake_match),
            ):
                first = asyncio.run(engine_module.engine.sync(db, connection))
                second = asyncio.run(engine_module.engine.sync(db, connection))

            self.assertEqual(first["missing"], 1)
            self.assertEqual(first["reviewStaged"], 0)
            self.assertEqual(second["missing"], 1)
            self.assertEqual(second["reviewStaged"], 0)
            self.assertEqual(calls["value"], 0)
            self.assertEqual(
                db.query(LibrarySyncSuggestion)
                .filter_by(user_id=user_id, source_id="baseline-missing")
                .count(),
                0,
            )
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
