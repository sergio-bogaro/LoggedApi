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
from database import Base, engine  # noqa: E402
from schemas.media import MediaCreate  # noqa: E402
from services.importer.providers.letterboxd import LetterboxdProvider
from services.importer.providers.mal import MalProvider  # noqa: E402
from services.openlibrary_service import OpenLibraryService  # noqa: E402
from models.enums import MediaTypeEnum  # noqa: E402

# O startup da API não cria mais schema (isso é do Alembic). Nos testes, o banco
# temporário nasce direto do metadata, incluindo os índices únicos.
Base.metadata.create_all(bind=engine)


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

    def test_delete_user_removes_media_and_import_files(self) -> None:
        from config import settings
        from database import SessionLocal
        from models.import_job import ImportJob
        from models.media import Media
        from services.import_jobs.storage import imports_dir

        user_id = self._user()
        external_id = f"delete-user-{uuid.uuid4().hex}"
        response = self.client.post(
            "/api/media/",
            json={
                "userId": user_id,
                "title": "Delete Me",
                "type": "movies",
                "externalId": external_id,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        media_id = response.json()["id"]

        image_name = f"delete-{uuid.uuid4().hex}.png"
        image_path = settings.upload_path / image_name
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
        import_file = imports_dir() / f"delete-{uuid.uuid4().hex}.bin"
        import_file.write_bytes(b"temp")

        db = SessionLocal()
        try:
            media = db.get(Media, media_id)
            media.image_path = image_name
            db.add(
                ImportJob(
                    user_id=user_id,
                    provider="letterboxd",
                    input_type="file",
                    status="ready",
                    file_path=str(import_file),
                )
            )
            db.commit()
        finally:
            db.close()

        response = self.client.delete(f"/auth/users/{user_id}")
        self.assertEqual(response.status_code, 204, response.text)
        self.assertFalse(image_path.exists())
        self.assertFalse(import_file.exists())

        db = SessionLocal()
        try:
            self.assertIsNone(db.get(Media, media_id))
        finally:
            db.close()

    def test_database_cascades_media_logs_and_list_items(self) -> None:
        from sqlalchemy import delete, func, select

        from database import SessionLocal
        from models.media import Media
        from models.media_list_item import MediaListItem
        from models.media_log import MediaLog

        user_id = self._user()
        external_id = f"cascade-{uuid.uuid4().hex}"
        media = self.client.post(
            "/api/media/",
            json={
                "userId": user_id,
                "title": "Cascade Movie",
                "type": "movies",
                "externalId": external_id,
            },
        )
        self.assertEqual(media.status_code, 201, media.text)
        media_id = media.json()["id"]

        log = self.client.post(
            "/api/media-logs/",
            json={
                "userId": user_id,
                "mediaId": media_id,
                "status": "finished",
                "date": "2026-10-01",
            },
        )
        self.assertEqual(log.status_code, 201, log.text)

        favorite = self.client.post(
            "/api/favorites/",
            json={
                "userId": user_id,
                "mediaType": "movies",
                "externalId": external_id,
                "title": "Cascade Movie",
            },
        )
        self.assertEqual(favorite.status_code, 201, favorite.text)

        db = SessionLocal()
        try:
            db.execute(delete(Media).where(Media.id == media_id))
            db.commit()
            self.assertEqual(
                db.execute(
                    select(func.count())
                    .select_from(MediaLog)
                    .where(MediaLog.media_id == media_id)
                ).scalar(),
                0,
            )
            self.assertEqual(
                db.execute(
                    select(func.count())
                    .select_from(MediaListItem)
                    .where(MediaListItem.media_id == media_id)
                ).scalar(),
                0,
            )
        finally:
            db.close()

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

    def test_letterboxd_zip_ignores_nested_duplicate_files(self) -> None:
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "watched.csv",
                "Date,Name,Year,Letterboxd URI\n"
                "2024-01-01,My Film,2024,https://boxd.it/abc\n",
            )
            archive.writestr(
                "diary.csv",
                "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n"
                "2024-01-03,My Film,2024,https://boxd.it/abc,4.5,,,2024-01-01\n",
            )
            archive.writestr(
                "reviews.csv",
                "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Review,Tags,Watched Date\n"
                "2024-01-02,My Film,2024,https://boxd.it/abc,4.5,,"
                "Minha review escrita,,2024-01-01\n",
            )
            # Estrutura real do export: essas cópias vêm depois da raiz no ZIP.
            archive.writestr("likes/reviews.csv", "Date,Content\n2024-01-01,https://boxd.it/xyz\n")
            archive.writestr(
                "deleted/reviews.csv",
                "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Review,Tags,Watched Date\n",
            )
            archive.writestr(
                "orphaned/reviews.csv",
                "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Review,Tags,Watched Date\n",
            )
            archive.writestr(
                "orphaned/diary.csv",
                "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n",
            )

        preview = asyncio.run(
            LetterboxdProvider().parse_file("export.zip", buffer.getvalue())
        )

        self.assertEqual(preview.counts.reviewed, 1)
        self.assertEqual(preview.counts.with_logs, 1)
        entry = preview.entries[0]
        self.assertEqual(entry.review, "Minha review escrita")
        self.assertEqual(len(entry.logs), 1)
        self.assertEqual(entry.logs[0].date.isoformat(), "2024-01-01")
        self.assertEqual(entry.logs[0].review, "Minha review escrita")

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


class HttpClientTests(unittest.TestCase):
    def test_client_and_lock_are_scoped_per_event_loop(self) -> None:
        from services.http_client import (
            close_loop_clients,
            get_async_client,
            get_loop_lock,
        )

        async def same_loop():
            first = get_async_client("test", timeout=1)
            second = get_async_client("test", timeout=1)
            lock_a = get_loop_lock("test")
            lock_b = get_loop_lock("test")
            await close_loop_clients()
            return first, second, lock_a, lock_b

        first, second, lock_a, lock_b = asyncio.run(same_loop())
        self.assertIs(first, second)
        self.assertIs(lock_a, lock_b)
        self.assertTrue(first.is_closed)

        async def other_loop():
            client = get_async_client("test", timeout=1)
            lock = get_loop_lock("test")
            await close_loop_clients()
            return client, lock

        other, other_lock = asyncio.run(other_loop())
        # Loops diferentes nunca compartilham objeto: era a causa do
        # "Event loop is closed" / "TCPTransport ... handler is closed".
        self.assertIsNot(first, other)
        self.assertIsNot(lock_a, other_lock)


class ExternalServiceErrorTests(unittest.TestCase):
    def test_igdb_oauth_error_does_not_expose_secret(self) -> None:
        import httpx

        from domain_errors import DomainError
        from services.igdb_service import IgdbService

        secret = "super-secret-value"
        request = httpx.Request("POST", "https://id.twitch.tv/oauth2/token")
        response = httpx.Response(429, request=request)

        class FakeClient:
            async def post(self, *args, **kwargs):
                return response

        service = IgdbService()
        with patch(
            "services.igdb_service.get_async_client",
            return_value=FakeClient(),
        ):
            with self.assertRaises(DomainError) as ctx:
                asyncio.run(service._ensure_token("client-id", secret))
        self.assertNotIn(secret, str(ctx.exception))

    def test_tmdb_error_does_not_expose_api_key(self) -> None:
        import httpx

        from domain_errors import DomainError
        from services.tmdb_service import TmdbService

        secret = "tmdb-secret-key"
        request = httpx.Request(
            "GET",
            f"https://api.themoviedb.org/3/search/movie?api_key={secret}",
        )
        response = httpx.Response(500, request=request)

        class FakeClient:
            async def get(self, *args, **kwargs):
                return response

        service = TmdbService()
        with patch(
            "services.tmdb_service.get_async_client",
            return_value=FakeClient(),
        ):
            with self.assertRaises(DomainError) as ctx:
                asyncio.run(
                    service.search_movies("movie", secret)
                )
        self.assertNotIn(secret, str(ctx.exception))

    def test_igdb_search_escapes_quotes(self) -> None:
        import httpx

        from services.igdb_service import IgdbService

        captured: dict[str, str] = {}
        oauth_request = httpx.Request(
            "POST", "https://id.twitch.tv/oauth2/token"
        )
        api_request = httpx.Request("POST", "https://api.igdb.com/v4/games")

        class FakeClient:
            async def post(self, url, **kwargs):
                if "oauth2" in url:
                    return httpx.Response(
                        200,
                        request=oauth_request,
                        json={"access_token": "token", "expires_in": 3600},
                    )
                captured["body"] = kwargs["content"]
                return httpx.Response(200, request=api_request, json=[])

        service = IgdbService()
        with patch(
            "services.igdb_service.get_async_client",
            return_value=FakeClient(),
        ):
            asyncio.run(service.search_games('foo"; limit 100;', "id", "secret"))

        self.assertIn('search "foo ; limit 100;"', captured["body"])


class ImageStorageSecurityTests(unittest.TestCase):
    def test_rejects_private_and_invalid_image_urls(self) -> None:
        from domain_errors import DomainError
        from services.image_storage import _validate_remote_url

        for url in (
            "http://127.0.0.1/cover.png",
            "http://[::1]/cover.png",
            "http://169.254.169.254/latest/meta-data/",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url):
                with self.assertRaises(DomainError):
                    asyncio.run(_validate_remote_url(url))

    def test_detects_image_by_magic_bytes(self) -> None:
        from domain_errors import DomainError
        from services.image_storage import _detect_image_extension

        self.assertEqual(
            _detect_image_extension(b"\xff\xd8\xff\xe0" + b"0" * 8), ".jpg"
        )
        self.assertEqual(
            _detect_image_extension(b"\x89PNG\r\n\x1a\n" + b"0" * 8), ".png"
        )
        self.assertEqual(_detect_image_extension(b"GIF89a" + b"0" * 8), ".gif")
        self.assertEqual(
            _detect_image_extension(b"RIFF" + b"0" * 4 + b"WEBP" + b"0" * 8),
            ".webp",
        )
        with self.assertRaises(DomainError):
            _detect_image_extension(b"<html>not an image</html>")


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

    def test_network_calls_run_outside_write_transaction(self) -> None:
        import asyncio
        from datetime import datetime

        from database import SessionLocal
        from models.enums import MediaStatusEnum
        from models.suwayomi import SuwayomiAccount, SuwayomiCategoryMapping
        from schemas.import_schemas import ImportMatchResult
        from services.suwayomi import sync as sync_module

        user_id = self._user()
        db = SessionLocal()
        try:
            account = SuwayomiAccount(
                user_id=user_id,
                base_url="http://suwayomi.test",
                enabled=True,
            )
            db.add(account)
            db.add(
                SuwayomiCategoryMapping(
                    user_id=user_id,
                    category_id=1,
                    category_name="Reading",
                    category_order=1,
                    action="status",
                    status=MediaStatusEnum.IN_PROGRESS,
                )
            )
            db.commit()

            transaction_states: list[bool] = []

            async def fake_resolve(_db, _user_id, _title, _query=None):
                transaction_states.append(_db.in_transaction())
                return ImportMatchResult(key="suwayomi", status="not_found")

            summary = sync_module.service._empty_summary(datetime.now())
            with patch.object(
                sync_module.matcher,
                "resolve_manga",
                new=fake_resolve,
            ):
                asyncio.run(
                    sync_module.service._run(
                        db,
                        account,
                        [{"id": 1, "name": "Reading", "order": 1}],
                        [
                            {
                                "id": 900,
                                "title": "Outside Transaction",
                                "categories": {"nodes": [{"id": 1}]},
                            }
                        ],
                        summary,
                    )
                )
        finally:
            db.close()

        self.assertTrue(transaction_states)
        self.assertEqual(transaction_states, [False])

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

            self.assertIsNone(link.media_id)
            self.assertEqual(link.match_attempts, 1)
            self.assertIsNotNone(link.last_match_attempt_at)
            self.assertEqual(summary["created"], 0)
            self.assertEqual(summary["reviewStaged"], 1)
            self.assertNotIn("Unknown Manga", summary["pendingResolved"])
            self.assertEqual(
                db.query(Media)
                .filter_by(user_id=user_id, external_id="unknown-1")
                .count(),
                0,
            )
            suggestion = (
                db.query(LibrarySyncSuggestion)
                .filter_by(
                    user_id=user_id,
                    provider="suwayomi",
                    source_id="999",
                )
                .one()
            )
            self.assertEqual(suggestion.review_state, "pending")
            self.assertIsNotNone(suggestion.candidate_json)
            self.assertIsNone(suggestion.media_id)

            response = self.client.post(
                "/api/integrations/suggestions/approve",
                json={"userId": user_id, "ids": [suggestion.id]},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["approved"], 1)

            db.expire_all()
            media = (
                db.query(Media)
                .filter_by(user_id=user_id, external_id="unknown-1")
                .one()
            )
            link = db.get(SuwayomiMangaLink, link.id)
            suggestion = db.get(LibrarySyncSuggestion, suggestion.id)
            self.assertEqual(link.media_id, media.id)
            self.assertEqual(suggestion.review_state, "approved")
            self.assertEqual(suggestion.origin, "review")
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
                    LibrarySyncSuggestion(
                        user_id=user_id,
                        provider="suwayomi",
                        source_id="9005",
                        media_type=MediaTypeEnum.MANGA,
                        title="Pending Validation",
                        source_status="in_progress",
                        action="in_progress",
                        target_status=MediaStatusEnum.IN_PROGRESS,
                        review_state="pending",
                        candidate_json=candidate_to_json(candidate),
                        candidates_json=candidates_to_json([candidate]),
                        error="Match não exato; revise antes de adicionar.",
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
        self.assertEqual(body["counts"]["pending"], 2)
        self.assertEqual(body["counts"]["ready"], 1)
        self.assertEqual(body["counts"]["attention"], 1)
        self.assertEqual(body["counts"]["noMatch"], 0)

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
        self.assertEqual(everything.json()["total"], 5)

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

    def test_letterboxd_rss_watch_without_review_has_no_review(self) -> None:
        from defusedxml.ElementTree import fromstring

        from services.library_sync.providers.letterboxd import LetterboxdProvider

        def item_for(description: str):
            xml = f"""<?xml version='1.0' encoding='utf-8'?>
            <rss version="2.0" xmlns:letterboxd="https://letterboxd.com"
                 xmlns:tmdb="https://themoviedb.org">
              <channel><item>
                <title>Backrooms, 2026</title>
                <guid>letterboxd-watch-999</guid>
                <description>{description}</description>
                <letterboxd:filmTitle>Backrooms</letterboxd:filmTitle>
                <letterboxd:filmYear>2026</letterboxd:filmYear>
                <letterboxd:watchedDate>2026-11-08</letterboxd:watchedDate>
                <tmdb:movieId>1083381</tmdb:movieId>
              </item></channel>
            </rss>"""
            return LetterboxdProvider()._to_item(fromstring(xml.encode()).find(".//item"))

        only_watched = item_for("Watched on Sunday November 8, 2026.")
        assert only_watched is not None
        self.assertIsNone(only_watched.review)

        watched_with_review = item_for(
            "Watched on Sunday November 8, 2026.&lt;p&gt;Ótimo filme.&lt;/p&gt;"
        )
        assert watched_with_review is not None
        self.assertEqual(watched_with_review.review, "Ótimo filme.")

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
        from datetime import date
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
                        "isCustomList": False,
                        "entries": [
                            {
                                "status": "CURRENT",
                                "score": 8.5,
                                "notes": "nota",
                                "updatedAt": "2026-01-01T00:00:00Z",
                                "startedAt": {"year": 2020, "month": 5, "day": 1},
                                "completedAt": {"year": 2020, "month": 5, "day": 3},
                                "media": {
                                    "id": 123,
                                    "title": {"romaji": "Romaji", "english": "English"},
                                    "coverImage": {"large": "cover.jpg"},
                                    "description": "desc",
                                    "startDate": {"year": 2020, "month": 4, "day": 2},
                                },
                            }
                        ],
                    },
                    {
                        "isCustomList": True,
                        "entries": [
                            {
                                "status": "COMPLETED",
                                "media": {
                                    "id": 999,
                                    "title": {"romaji": "Custom"},
                                },
                            }
                        ],
                    },
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
        self.assertEqual(item.watched_at, date(2020, 5, 3))

    def test_simkl_anime_only_trusts_anilist_id(self) -> None:
        from datetime import date

        from models.enums import MediaTypeEnum
        from services.library_sync.providers.simkl import SimklProvider

        provider = SimklProvider()
        items = {}
        provider._add(
            items,
            {
                "status": "watching",
                "last_watched_at": "2024-01-02T10:00:00Z",
                "anime": {
                    "title": "Anime via TMDB",
                    "ids": {"simkl": 1, "tmdb": 22, "mal": 33},
                },
            },
            MediaTypeEnum.ANIME,
            "anime",
        )
        item = items["simkl:anime:1"]
        self.assertIsNone(item.external_id)
        self.assertEqual(item.external_refs["tmdbId"], "22")
        self.assertEqual(item.external_refs["malId"], "33")
        self.assertEqual(item.watched_at, date(2024, 1, 2))

        items = {}
        provider._add(
            items,
            {
                "status": "completed",
                "anime": {
                    "title": "Anime via AniList",
                    "ids": {"simkl": 2, "anilist": 44, "tmdb": 22},
                },
            },
            MediaTypeEnum.ANIME,
            "anime",
        )
        self.assertEqual(items["simkl:anime:2"].external_id, "44")

    def test_integration_api_config_and_mappings(self) -> None:
        user_id = self._user()
        response = self.client.get(f"/api/integrations?user_id={user_id}")
        self.assertEqual(response.status_code, 200)
        providers = {item["id"]: item for item in response.json()}
        self.assertIn("anilist", providers)
        self.assertIn("letterboxd", providers)
        self.assertIn("trakt", providers)
        self.assertTrue(providers["letterboxd"]["syncPartial"])
        self.assertFalse(providers["letterboxd"]["statusMapping"])
        self.assertFalse(providers["anilist"]["syncPartial"])
        self.assertTrue(providers["anilist"]["statusMapping"])
        self.assertFalse(providers["trakt"]["syncPartial"])

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

    def test_provider_test_failure_marks_needs_reauth(self) -> None:
        import json

        from database import SessionLocal
        from models.library_sync import LibraryConnection

        class FailingProvider:
            async def test(self, connection):
                raise ValueError("busca falhou")

        class WorkingProvider:
            async def test(self, connection):
                return "someone"

        user_id = self._user()
        db = SessionLocal()
        try:
            db.add(
                LibraryConnection(
                    user_id=user_id,
                    provider="anilist",
                    enabled=True,
                    updates_per_day=4,
                    auth_json=json.dumps({"username": "someone"}),
                    cursor_json="{}",
                )
            )
            db.commit()
        finally:
            db.close()

        def needs_reauth() -> bool:
            db = SessionLocal()
            try:
                connection = (
                    db.query(LibraryConnection)
                    .filter_by(user_id=user_id, provider="anilist")
                    .one()
                )
                return bool(connection.needs_reauth)
            finally:
                db.close()

        with patch(
            "routers.library_sync.get_provider", return_value=FailingProvider()
        ):
            response = self.client.post(
                "/api/integrations/anilist/test", json={"userId": user_id}
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["ok"])
        self.assertTrue(needs_reauth())

        with patch(
            "routers.library_sync.get_provider", return_value=WorkingProvider()
        ):
            response = self.client.post(
                "/api/integrations/anilist/test", json={"userId": user_id}
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])
        self.assertFalse(needs_reauth())

    def test_letterboxd_partial_sync_hidden_after_manual_import(self) -> None:
        from database import SessionLocal
        from models.import_job import ImportJob

        user_id = self._user()

        def sync_partial() -> bool:
            response = self.client.get(f"/api/integrations?user_id={user_id}")
            self.assertEqual(response.status_code, 200, response.text)
            providers = {item["id"]: item for item in response.json()}
            return providers["letterboxd"]["syncPartial"]

        self.assertTrue(sync_partial())

        db = SessionLocal()
        try:
            job = ImportJob(
                user_id=user_id,
                provider="letterboxd",
                input_type="file",
                status="ready",
            )
            db.add(job)
            db.commit()
            job_id = job.id
        finally:
            db.close()

        # Pronto para revisão ainda não é um import concluído.
        self.assertTrue(sync_partial())

        db = SessionLocal()
        try:
            job = db.get(ImportJob, job_id)
            self.assertIsNotNone(job)
            job.status = "done"
            db.commit()
        finally:
            db.close()

        self.assertFalse(sync_partial())

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
            f"/api/suwayomi/links/{pending_id}?user_id={user_id}",
            json={"mediaId": None},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["mediaId"])

        response = self.client.get(
            f"/api/suwayomi/links?user_id={user_id}&status=pending"
        )
        self.assertIn(
            pending_id, [link["id"] for link in response.json()]
        )

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

    def test_cancel_matching_job_removes_temp_file(self) -> None:
        from database import SessionLocal
        from models.import_job import ImportJob
        from services.import_jobs.storage import imports_dir

        user_id = self._user()
        path = imports_dir() / f"cancel-{uuid.uuid4().hex}.bin"
        path.write_bytes(b"temp")

        db = SessionLocal()
        try:
            job = ImportJob(
                user_id=user_id,
                provider="letterboxd",
                input_type="file",
                status="matching",
                file_path=str(path),
                progress_total=1,
            )
            db.add(job)
            db.commit()
            job_id = job.id
        finally:
            db.close()

        response = self.client.post(
            f"/api/import/jobs/{job_id}/cancel?user_id={user_id}"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertFalse(path.exists())

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

    def test_commit_backfills_review_on_existing_log(self) -> None:
        from datetime import date

        from sqlalchemy import select

        from database import SessionLocal
        from models.enums import MediaStatusEnum
        from models.media import Media
        from models.media_log import MediaLog
        from schemas.import_schemas import (
            ImportCommitEntry,
            ImportCommitRequest,
            ImportLogEntry,
        )
        from services.importer.commit import committer

        user_id = self._user()
        external_id = f"review-{uuid.uuid4().hex}"
        media = self.client.post(
            "/api/media/",
            json={
                "userId": user_id,
                "title": "Review Film",
                "type": "movies",
                "externalId": external_id,
            },
        )
        self.assertEqual(media.status_code, 201, media.text)
        media_id = media.json()["id"]

        db = SessionLocal()
        try:
            db.add(
                MediaLog(
                    user_id=user_id,
                    media_id=media_id,
                    date=date(2024, 1, 1),
                    status=MediaStatusEnum.FINISHED,
                    rating=4.0,
                    review=None,
                )
            )
            db.commit()
        finally:
            db.close()

        db = SessionLocal()
        try:
            response = committer.commit(
                db,
                ImportCommitRequest(
                    user_id=user_id,
                    entries=[
                        ImportCommitEntry(
                            key="review-key",
                            media_type=MediaTypeEnum.MOVIES,
                            external_id=external_id,
                            title="Review Film",
                            status=MediaStatusEnum.FINISHED,
                            review="Minha review",
                            logs=[
                                ImportLogEntry(
                                    date=date(2024, 1, 1),
                                    status=MediaStatusEnum.FINISHED,
                                    review="Minha review",
                                )
                            ],
                        )
                    ],
                ),
            )
            self.assertEqual(response.failures, [])
            self.assertEqual(response.merged, 1)
            self.assertEqual(response.logs_created, 0)

            logs = (
                db.execute(select(MediaLog).where(MediaLog.media_id == media_id))
                .scalars()
                .all()
            )
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0].review, "Minha review")
            self.assertEqual(logs[0].rating, 4.0)  # nota manual preservada
            self.assertEqual(db.get(Media, media_id).review, "Minha review")
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
            trusted_external_id = True

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

    def test_sync_user_uses_database_engine(self) -> None:
        """Regressão: `engine` do módulo é o singleton, não o Engine do SQLAlchemy."""
        from database import SessionLocal
        from services.library_sync import engine as engine_module

        user_id = self._user()
        db = SessionLocal()
        try:
            self._connection(db, user_id)
        finally:
            db.close()

        summary = {"provider": "fake", "processed": 1}

        async def fake_sync(*_args, **_kwargs):
            return summary

        with patch.object(engine_module.engine, "sync", new=fake_sync):
            result = engine_module.sync_user(user_id, "fake")

        self.assertEqual(result, summary)

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
        from models.library_sync import LibrarySyncLink, LibrarySyncSuggestion
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
            self.assertEqual(summary["reviewStaged"], 1)
            self.assertEqual(summary["created"], 0)
            self.assertTrue(summary["baseline"])
            links = db.query(LibrarySyncLink).all()
            self.assertEqual(len(links), 1)
            self.assertEqual(links[0].source_id, "s1")
            self.assertEqual(links[0].media_id, media.id)
            self.assertNotIn("baseline_missing", json.loads(connection.cursor_json))
            suggestion = (
                db.query(LibrarySyncSuggestion)
                .filter_by(user_id=user_id, source_id="s2")
                .one()
            )
            self.assertEqual(suggestion.review_state, "pending")
            self.assertEqual(
                db.query(Media)
                .filter_by(user_id=user_id, external_id="tmdb-2")
                .count(),
                0,
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
            self.assertEqual(summary["created"], 0)
            self.assertEqual(summary["reviewStaged"], 1)
            self.assertEqual(
                db.query(Media)
                .filter_by(user_id=user_id, external_id="tmdb-3")
                .count(),
                0,
            )

            from models.library_sync import LibrarySyncSuggestion
            from services.library_sync.suggestions import approve_suggestions

            suggestion = (
                db.query(LibrarySyncSuggestion)
                .filter_by(user_id=user_id, source_id="new-1")
                .one()
            )
            result = approve_suggestions(db, user_id, [suggestion.id])
            self.assertEqual(result["approved"], 1)

            db.expire_all()
            media = (
                db.query(Media)
                .filter_by(user_id=user_id, external_id="tmdb-3")
                .one()
            )
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

            from models.library_sync import LibrarySyncSuggestion
            from services.library_sync.suggestions import approve_suggestions

            suggestion = (
                db.query(LibrarySyncSuggestion)
                .filter_by(user_id=user_id, source_id="manual-1")
                .one()
            )
            approve_suggestions(db, user_id, [suggestion.id])

            db.expire_all()
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
        provider.trusted_external_id = False
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

    def test_review_provider_stages_exact_match_and_approves_with_log(self) -> None:
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
            self.assertEqual(summary["created"], 0)
            self.assertEqual(summary["reviewStaged"], 1)
            self.assertEqual(summary["missing"], 1)
            self.assertEqual(summary["logsCreated"], 0)
            self.assertEqual(summary["pendingReview"], 1)
            self.assertEqual(
                db.query(Media)
                .filter_by(user_id=user_id, external_id=external_id)
                .count(),
                0,
            )
            suggestion = (
                db.query(LibrarySyncSuggestion)
                .filter_by(user_id=user_id)
                .one()
            )
            self.assertEqual(suggestion.review_state, "pending")
            self.assertIsNotNone(suggestion.sync_run_at)
            self.assertIsNotNone(suggestion.candidates_json)

            response = self.client.post(
                "/api/integrations/suggestions/approve",
                json={"userId": user_id, "ids": [suggestion.id]},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["approved"], 1)

            db.expire_all()
            media = (
                db.query(Media)
                .filter_by(user_id=user_id, external_id=external_id)
                .one()
            )
            self.assertEqual(media.status, MediaStatusEnum.FINISHED)
            self.assertEqual(media.rating, 9.0)
            self.assertEqual(media.review, "Great.")
            log = db.query(MediaLog).filter_by(media_id=media.id).one()
            self.assertEqual(log.date, date(2026, 11, 1))
            self.assertEqual(log.status, MediaStatusEnum.FINISHED)
            suggestion = db.get(LibrarySyncSuggestion, suggestion.id)
            self.assertEqual(suggestion.review_state, "approved")
            self.assertEqual(suggestion.origin, "review")
            self.assertEqual(suggestion.media_id, media.id)
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

    def test_oauth_device_flow_expires_and_is_pruned(self) -> None:
        from datetime import timedelta

        from routers import library_sync as library_sync_router
        from services.library_sync.engine import utcnow

        class FakeProvider:
            async def start_device(self):
                return {
                    "device_code": "device-123",
                    "user_code": "ABCD",
                    "verification_url": "https://example.test/device",
                    "expires_in": 60,
                    "interval": 5,
                }

            async def poll_device(self, _device_code):
                return {"status": "pending"}

        user_id = self._user()
        provider_key = (user_id, "trakt")
        try:
            with patch.object(
                library_sync_router,
                "get_provider",
                return_value=FakeProvider(),
            ):
                response = self.client.post(
                    "/api/integrations/trakt/oauth/start",
                    json={"userId": user_id},
                )
            self.assertEqual(response.status_code, 200, response.text)
            flow = library_sync_router._DEVICE_FLOWS[provider_key]
            self.assertGreater(flow["expires_at"], utcnow())

            # Fluxo vencido é removido antes de qualquer poll.
            flow["expires_at"] = utcnow() - timedelta(seconds=1)
            response = self.client.post(
                "/api/integrations/trakt/oauth/poll",
                json={"userId": user_id},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "expired")
            self.assertNotIn(provider_key, library_sync_router._DEVICE_FLOWS)

            # Um fluxo vencido de outro provedor também é podado no start.
            stale_key = (user_id, "simkl")
            library_sync_router._DEVICE_FLOWS[stale_key] = {
                "device_code": "stale",
                "expires_at": utcnow() - timedelta(seconds=1),
            }
            with patch.object(
                library_sync_router,
                "get_provider",
                return_value=FakeProvider(),
            ):
                response = self.client.post(
                    "/api/integrations/trakt/oauth/start",
                    json={"userId": user_id},
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(stale_key, library_sync_router._DEVICE_FLOWS)
        finally:
            library_sync_router._DEVICE_FLOWS.pop(provider_key, None)
            library_sync_router._DEVICE_FLOWS.pop((user_id, "simkl"), None)

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

    def test_sync_run_starts_in_background_and_reports_progress(self) -> None:
        import threading
        import time
        from unittest.mock import patch

        user_id = self._user()
        gate = threading.Event()

        def fake_sync_all(_user_id, progress=None):
            progress(
                {
                    "phase": "syncing",
                    "label": "AniList",
                    "provider": "anilist",
                    "provider_index": 1,
                    "provider_total": 1,
                    "done": 1,
                    "total": 2,
                }
            )
            gate.wait(timeout=3)
            return {"status": "ok", "results": [], "ok": 1, "failed": 0}

        with patch("services.sync_runs.sync_all_for_user", new=fake_sync_all):
            first = self.client.post(
                "/api/integrations/sync-runs",
                json={"userId": user_id, "scope": "all"},
            )
            self.assertEqual(first.status_code, 200, first.text)
            first_body = first.json()
            self.assertTrue(first_body["started"])
            run_id = first_body["run"]["id"]

            second = self.client.post(
                "/api/integrations/sync-runs",
                json={"userId": user_id, "scope": "all"},
            )
            self.assertEqual(second.status_code, 200, second.text)
            self.assertFalse(second.json()["started"])
            self.assertEqual(second.json()["run"]["id"], run_id)

            active = self.client.get(
                f"/api/integrations/sync-runs/active?user_id={user_id}"
            )
            self.assertEqual(active.status_code, 200, active.text)
            self.assertEqual(active.json()["id"], run_id)

            status = self.client.get(
                f"/api/integrations/sync-runs/{run_id}?user_id={user_id}"
            )
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["provider"], "anilist")
            self.assertEqual(status.json()["done"], 1)
            self.assertEqual(status.json()["total"], 2)

            gate.set()
            finished = None
            for _ in range(100):
                response = self.client.get(
                    f"/api/integrations/sync-runs/{run_id}?user_id={user_id}"
                )
                self.assertEqual(response.status_code, 200, response.text)
                if response.json()["status"] != "running":
                    finished = response.json()
                    break
                time.sleep(0.02)

            self.assertIsNotNone(finished)
            self.assertEqual(finished["status"], "success")
            self.assertEqual(finished["percent"], 100)
            self.assertEqual(finished["summary"]["status"], "ok")

            active_after = self.client.get(
                f"/api/integrations/sync-runs/active?user_id={user_id}"
            )
            self.assertIsNone(active_after.json())

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

        def fake_sync(user_id_arg, provider_id, _run_at=None, _progress=None):
            calls["generic"].append(provider_id)
            return {"status": "ok", "created": 1}

        def fake_suwayomi(user_id_arg, _run_at=None, progress=None):
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

    def test_trusted_external_id_stages_without_calling_matcher(self) -> None:
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
                source_id="trusted-1",
                media_type=MediaTypeEnum.MOVIES,
                title="Trusted Movie",
                status="watched",
                external_id="tmdb-trusted",
                cover_url="https://image.tmdb.org/t/p/w500/trusted.jpg",
            )
            provider = self._provider([item])

            async def fail_match(*_args, **_kwargs):
                raise AssertionError("matcher não deveria ser chamado")

            with (
                patch.object(engine_module, "get_provider", return_value=provider),
                patch.object(engine_module.matcher, "match", new=fail_match),
            ):
                summary = asyncio.run(engine_module.engine.sync(db, connection))

            self.assertEqual(summary["created"], 0)
            self.assertEqual(summary["reviewStaged"], 1)
            suggestion = (
                db.query(LibrarySyncSuggestion)
                .filter_by(user_id=user_id, source_id="trusted-1")
                .one()
            )
            self.assertEqual(suggestion.review_state, "pending")
            self.assertIsNotNone(suggestion.candidate_json)
            self.assertIsNone(suggestion.error)
        finally:
            db.close()

    def test_item_without_cover_fetches_metadata_before_review(self) -> None:
        import asyncio
        from unittest.mock import patch

        from database import SessionLocal
        from models.enums import MediaTypeEnum
        from models.library_sync import LibrarySyncSuggestion
        from models.media import Media
        from schemas.import_schemas import (
            ImportCandidate,
            ImportMatchResponse,
            ImportMatchResult,
        )
        from services.library_sync import engine as engine_module
        from services.library_sync.providers.base import SourceItem

        user_id = self._user()
        db = SessionLocal()
        try:
            connection = self._connection(db, user_id)
            item = SourceItem(
                source_id="cover-1",
                media_type=MediaTypeEnum.MOVIES,
                title="Cover Movie",
                status="watched",
                external_id="tmdb-cover",
            )
            provider = self._provider([item])

            async def fake_match(_db, request, _tmdb_key, _igdb):
                candidate = ImportCandidate(
                    provider="tmdb",
                    external_id="tmdb-cover",
                    media_type=MediaTypeEnum.MOVIES,
                    title="Cover Movie",
                    year=2024,
                    cover_url="https://image.tmdb.org/t/p/w500/cover.jpg",
                    overview="Sinopse do filme.",
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

            with (
                patch.object(engine_module, "get_provider", return_value=provider),
                patch.object(engine_module.matcher, "match", new=fake_match),
            ):
                summary = asyncio.run(engine_module.engine.sync(db, connection))

            self.assertEqual(summary["created"], 0)
            self.assertEqual(summary["reviewStaged"], 1)
            suggestion = (
                db.query(LibrarySyncSuggestion)
                .filter_by(user_id=user_id, source_id="cover-1")
                .one()
            )
            self.assertEqual(suggestion.review_state, "pending")
            self.assertIn("cover.jpg", suggestion.candidate_json or "")

            response = self.client.post(
                "/api/integrations/suggestions/approve",
                json={"userId": user_id, "ids": [suggestion.id]},
            )
            self.assertEqual(response.status_code, 200, response.text)

            db.expire_all()
            media = (
                db.query(Media)
                .filter_by(user_id=user_id, external_id="tmdb-cover")
                .one()
            )
            self.assertEqual(
                media.cover_url, "https://image.tmdb.org/t/p/w500/cover.jpg"
            )
            self.assertEqual(media.description, "Sinopse do filme.")
        finally:
            db.close()

    def test_linked_media_without_cover_is_enriched_on_sync(self) -> None:
        import asyncio
        from unittest.mock import patch

        from database import SessionLocal
        from models.enums import MediaTypeEnum
        from models.library_sync import LibrarySyncLink, LibrarySyncSuggestion
        from models.media import Media
        from schemas.import_schemas import (
            ImportCandidate,
            ImportMatchResponse,
            ImportMatchResult,
        )
        from services.library_sync import engine as engine_module
        from services.library_sync.providers.base import SourceItem

        user_id = self._user()
        db = SessionLocal()
        try:
            connection = self._connection(db, user_id)
            media = Media(
                user_id=user_id,
                external_id="tmdb-backfill",
                title="Backfill Movie",
                type=MediaTypeEnum.MOVIES,
            )
            db.add(media)
            db.flush()
            db.add(
                LibrarySyncLink(
                    user_id=user_id,
                    provider="fake",
                    source_id="backfill-1",
                    media_id=media.id,
                    media_type=MediaTypeEnum.MOVIES,
                    title="Backfill Movie",
                )
            )
            db.commit()

            item = SourceItem(
                source_id="backfill-1",
                media_type=MediaTypeEnum.MOVIES,
                title="Backfill Movie",
                status="watched",
                external_id="tmdb-backfill",
            )
            provider = self._provider([item])

            async def fake_match(_db, request, _tmdb_key, _igdb):
                candidate = ImportCandidate(
                    provider="tmdb",
                    external_id="tmdb-backfill",
                    media_type=MediaTypeEnum.MOVIES,
                    title="Backfill Movie",
                    year=2024,
                    cover_url="https://image.tmdb.org/t/p/w500/backfill.jpg",
                    overview="Sinopse recuperada.",
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

            with (
                patch.object(engine_module, "get_provider", return_value=provider),
                patch.object(engine_module.matcher, "match", new=fake_match),
            ):
                summary = asyncio.run(engine_module.engine.sync(db, connection))

            db.expire_all()
            media = db.get(Media, media.id)
            self.assertEqual(
                media.cover_url, "https://image.tmdb.org/t/p/w500/backfill.jpg"
            )
            self.assertEqual(media.description, "Sinopse recuperada.")
            self.assertGreaterEqual(summary["notesUpdated"], 1)
            self.assertEqual(
                db.query(Media)
                .filter_by(user_id=user_id, external_id="tmdb-backfill")
                .count(),
                1,
            )
            self.assertEqual(
                db.query(LibrarySyncSuggestion)
                .filter_by(user_id=user_id, source_id="backfill-1")
                .count(),
                0,
            )
        finally:
            db.close()

    def test_first_sync_stages_even_legacy_baseline_missing(self) -> None:
        import asyncio
        import json
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
            source_id = f"legacy-{uuid.uuid4().hex}"
            connection.cursor_json = json.dumps({"baseline_missing": [source_id]})
            db.commit()
            item = SourceItem(
                source_id=source_id,
                media_type=MediaTypeEnum.MOVIES,
                title="Old Missing",
                status="watched",
                external_id=f"tmdb-{uuid.uuid4().hex}",
                cover_url="https://image.tmdb.org/t/p/w500/legacy.jpg",
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
            self.assertEqual(first["reviewStaged"], 1)
            self.assertEqual(second["missing"], 1)
            self.assertEqual(second["reviewStaged"], 0)
            self.assertEqual(calls["value"], 0)
            self.assertEqual(
                db.query(Media).filter_by(user_id=user_id).count(),
                0,
            )
            self.assertEqual(
                db.query(LibrarySyncSuggestion)
                .filter_by(user_id=user_id, source_id=source_id)
                .count(),
                1,
            )
            self.assertNotIn(
                "baseline_missing", json.loads(connection.cursor_json)
            )
        finally:
            db.close()

    def test_review_api_merges_manual_and_sync(self) -> None:
        import json
        from datetime import datetime

        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.import_job import ImportJob, ImportJobItem
        from models.library_sync import LibrarySyncSuggestion
        from schemas.import_schemas import ImportCandidate
        from services.import_jobs.serializers import candidate_to_json

        user_id = self._user()
        movie = ImportCandidate(
            provider="tmdb",
            external_id=f"tmdb-review-{uuid.uuid4().hex}",
            media_type=MediaTypeEnum.MOVIES,
            title="Review Movie",
            year=2024,
            score=1.0,
        )
        anime = ImportCandidate(
            provider="anilist",
            external_id=f"anilist-review-{uuid.uuid4().hex}",
            media_type=MediaTypeEnum.ANIME,
            title="Review Anime",
            score=1.0,
        )
        db = SessionLocal()
        try:
            job = ImportJob(
                user_id=user_id,
                provider="letterboxd",
                input_type="file",
                status="ready",
                progress_done=2,
                progress_total=2,
                counts_json="{}",
            )
            db.add(job)
            db.flush()
            manual_ready = ImportJobItem(
                job_id=job.id,
                key=f"lb-ready-{uuid.uuid4().hex}",
                media_type=MediaTypeEnum.MOVIES,
                title="Review Movie",
                source="letterboxd",
                match_status="matched",
                match_json=candidate_to_json(movie),
                candidates_json=json.dumps([movie.model_dump(mode="json")]),
            )
            manual_validation = ImportJobItem(
                job_id=job.id,
                key=f"lb-validation-{uuid.uuid4().hex}",
                media_type=MediaTypeEnum.MOVIES,
                title="Review Movie 2",
                source="letterboxd",
                match_status="ambiguous",
                match_json=candidate_to_json(movie),
                candidates_json=json.dumps([movie.model_dump(mode="json")]),
                match_error="Match não exato.",
            )
            sync_ready = LibrarySyncSuggestion(
                user_id=user_id,
                provider="anilist",
                source_id=f"anilist-ready-{uuid.uuid4().hex}",
                media_type=MediaTypeEnum.ANIME,
                title="Review Anime",
                source_status="CURRENT",
                action="in_progress",
                target_status=MediaStatusEnum.IN_PROGRESS,
                review_state="pending",
                candidate_json=candidate_to_json(anime),
                candidates_json=json.dumps([anime.model_dump(mode="json")]),
                sync_run_at=datetime(2026, 1, 1),
            )
            sync_validation = LibrarySyncSuggestion(
                user_id=user_id,
                provider="anilist",
                source_id=f"anilist-validation-{uuid.uuid4().hex}",
                media_type=MediaTypeEnum.ANIME,
                title="Review Anime 2",
                source_status="CURRENT",
                action="in_progress",
                target_status=MediaStatusEnum.IN_PROGRESS,
                review_state="pending",
                candidate_json=candidate_to_json(anime),
                candidates_json=json.dumps([anime.model_dump(mode="json")]),
                error="Match não exato; revise antes de adicionar.",
                sync_run_at=datetime(2026, 1, 1),
            )
            db.add_all(
                [manual_ready, manual_validation, sync_ready, sync_validation]
            )
            db.commit()
        finally:
            db.close()

        response = self.client.get(
            f"/api/review/items?user_id={user_id}&state=pending"
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["total"], 4)
        counts = body["counts"]
        self.assertEqual(counts["pending"], 4)
        self.assertEqual(counts["ready"], 2)
        self.assertEqual(counts["needsValidation"], 2)
        self.assertEqual(counts["manualJobs"], 1)
        self.assertEqual(counts["syncItems"], 2)
        self.assertEqual(counts["pendingTasks"], 3)
        self.assertEqual(
            {item["source"] for item in body["items"]}, {"manual", "sync"}
        )

        manual_only = self.client.get(
            f"/api/review/items?user_id={user_id}&state=pending&source=manual"
        )
        self.assertEqual(manual_only.status_code, 200, manual_only.text)
        self.assertEqual(manual_only.json()["total"], 2)

        ready_only = self.client.get(
            f"/api/review/items?user_id={user_id}&state=pending&match=ready"
        )
        self.assertEqual(ready_only.status_code, 200, ready_only.text)
        self.assertEqual(ready_only.json()["total"], 2)

        count_response = self.client.get(f"/api/review/counts?user_id={user_id}")
        self.assertEqual(count_response.status_code, 200, count_response.text)
        self.assertEqual(count_response.json()["pendingTasks"], 3)

    def test_review_api_patch_and_state(self) -> None:
        import json
        from datetime import datetime

        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.import_job import ImportJob, ImportJobItem
        from models.library_sync import LibrarySyncSuggestion
        from schemas.import_schemas import ImportCandidate
        from services.import_jobs.serializers import candidate_to_json

        user_id = self._user()
        candidate = ImportCandidate(
            provider="anilist",
            external_id=f"anilist-patch-{uuid.uuid4().hex}",
            media_type=MediaTypeEnum.ANIME,
            title="Patch Anime",
            score=1.0,
        )
        movie_candidate = ImportCandidate(
            provider="tmdb",
            external_id=f"tmdb-patch-{uuid.uuid4().hex}",
            media_type=MediaTypeEnum.MOVIES,
            title="Patch Movie",
            score=1.0,
        )
        db = SessionLocal()
        try:
            job = ImportJob(
                user_id=user_id,
                provider="letterboxd",
                input_type="file",
                status="ready",
                progress_done=1,
                progress_total=1,
            )
            db.add(job)
            db.flush()
            manual = ImportJobItem(
                job_id=job.id,
                key=f"lb-patch-{uuid.uuid4().hex}",
                media_type=MediaTypeEnum.MOVIES,
                title="Patch Movie",
                source="letterboxd",
                match_status="ambiguous",
                match_json=candidate_to_json(candidate),
                match_error="Match não exato.",
            )
            sync = LibrarySyncSuggestion(
                user_id=user_id,
                provider="anilist",
                source_id=f"anilist-patch-{uuid.uuid4().hex}",
                media_type=MediaTypeEnum.ANIME,
                title="Patch Anime",
                source_status="CURRENT",
                action="in_progress",
                target_status=MediaStatusEnum.IN_PROGRESS,
                review_state="pending",
                candidate_json=candidate_to_json(candidate),
                error="Match não exato.",
                sync_run_at=datetime(2026, 1, 1),
            )
            db.add_all([manual, sync])
            db.commit()
            manual_id = manual.id
            sync_id = sync.id
        finally:
            db.close()

        keys = self.client.get(
            f"/api/review/keys?user_id={user_id}&state=pending&source=all"
        )
        self.assertEqual(keys.status_code, 200, keys.text)
        self.assertIn(f"manual:{manual_id}", keys.json())
        self.assertIn(f"sync:{sync_id}", keys.json())

        keys = self.client.get(
            f"/api/review/keys?user_id={user_id}&state=pending&source=sync"
        )
        self.assertEqual(keys.status_code, 200, keys.text)
        self.assertEqual(keys.json(), [f"sync:{sync_id}"])

        response = self.client.patch(
            f"/api/review/items/manual:{manual_id}",
            json={"userId": user_id, "ignored": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "dismissed")

        response = self.client.patch(
            f"/api/review/items/sync:{sync_id}",
            json={"userId": user_id, "selected": candidate.model_dump(mode="json")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["match"], "ready")
        self.assertIsNone(response.json()["error"])

        response = self.client.post(
            "/api/review/state",
            json={
                "userId": user_id,
                "keys": [f"manual:{manual_id}"],
                "state": "pending",
            },
        )
        self.assertEqual(response.status_code, 204, response.text)

        response = self.client.patch(
            f"/api/review/items/manual:{manual_id}",
            json={
                "userId": user_id,
                "selected": movie_candidate.model_dump(mode="json"),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["match"], "ready")
        self.assertIsNone(response.json()["error"])
        self.assertEqual(
            response.json()["selected"]["externalId"],
            movie_candidate.external_id,
        )

    def test_review_state_batch_is_atomic(self) -> None:
        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.library_sync import LibrarySyncSuggestion

        user_id = self._user()
        db = SessionLocal()
        try:
            pending = LibrarySyncSuggestion(
                user_id=user_id,
                provider="anilist",
                source_id=f"atomic-pending-{uuid.uuid4().hex}",
                media_type=MediaTypeEnum.ANIME,
                title="Atomic Pending",
                source_status="CURRENT",
                action="in_progress",
                target_status=MediaStatusEnum.IN_PROGRESS,
                review_state="pending",
            )
            approved = LibrarySyncSuggestion(
                user_id=user_id,
                provider="anilist",
                source_id=f"atomic-approved-{uuid.uuid4().hex}",
                media_type=MediaTypeEnum.ANIME,
                title="Atomic Approved",
                source_status="COMPLETED",
                action="finished",
                target_status=MediaStatusEnum.FINISHED,
                review_state="approved",
            )
            db.add_all([pending, approved])
            db.commit()
            pending_id = pending.id
            approved_id = approved.id
        finally:
            db.close()

        response = self.client.post(
            "/api/review/state",
            json={
                "userId": user_id,
                "keys": [f"sync:{pending_id}", f"sync:{approved_id}"],
                "state": "dismissed",
            },
        )
        self.assertEqual(response.status_code, 400, response.text)

        db = SessionLocal()
        try:
            pending = db.get(LibrarySyncSuggestion, pending_id)
            self.assertEqual(pending.review_state, "pending")
        finally:
            db.close()

    def test_review_api_commit_mixed_sources(self) -> None:
        import json
        from datetime import date, datetime

        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.import_job import ImportJob, ImportJobItem
        from models.library_sync import LibrarySyncSuggestion
        from models.media import Media
        from schemas.import_schemas import ImportCandidate
        from services.import_jobs.serializers import candidate_to_json

        user_id = self._user()
        manual_external = f"tmdb-manual-{uuid.uuid4().hex}"
        sync_external = f"tmdb-sync-{uuid.uuid4().hex}"
        manual_candidate = ImportCandidate(
            provider="tmdb",
            external_id=manual_external,
            media_type=MediaTypeEnum.MOVIES,
            title="Manual Commit",
            year=2024,
            score=1.0,
        )
        sync_candidate = ImportCandidate(
            provider="tmdb",
            external_id=sync_external,
            media_type=MediaTypeEnum.MOVIES,
            title="Sync Commit",
            year=2024,
            score=1.0,
        )
        db = SessionLocal()
        try:
            job = ImportJob(
                user_id=user_id,
                provider="letterboxd",
                input_type="file",
                status="ready",
                progress_done=1,
                progress_total=1,
            )
            db.add(job)
            db.flush()
            manual = ImportJobItem(
                job_id=job.id,
                key=f"lb-commit-{uuid.uuid4().hex}",
                media_type=MediaTypeEnum.MOVIES,
                title="Manual Commit",
                source="letterboxd",
                status=MediaStatusEnum.FINISHED,
                match_status="matched",
                match_json=candidate_to_json(manual_candidate),
                candidates_json=json.dumps(
                    [manual_candidate.model_dump(mode="json")]
                ),
            )
            sync = LibrarySyncSuggestion(
                user_id=user_id,
                provider="letterboxd",
                source_id=f"lb-sync-{uuid.uuid4().hex}",
                media_type=MediaTypeEnum.MOVIES,
                title="Sync Commit",
                source_status="watched",
                action="finished",
                target_status=MediaStatusEnum.FINISHED,
                watched_date=date(2026, 1, 2),
                review_state="pending",
                candidate_json=candidate_to_json(sync_candidate),
                candidates_json=json.dumps(
                    [sync_candidate.model_dump(mode="json")]
                ),
                sync_run_at=datetime(2026, 1, 1),
            )
            db.add_all([manual, sync])
            db.commit()
            manual_id = manual.id
            sync_id = sync.id
            job_id = job.id
        finally:
            db.close()

        response = self.client.post(
            "/api/review/commit",
            json={
                "userId": user_id,
                "keys": [f"manual:{manual_id}", f"sync:{sync_id}"],
                "addToBacklog": False,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["failed"], 0)
        self.assertEqual(body["imported"], 2)

        db = SessionLocal()
        try:
            self.assertEqual(
                db.query(Media).filter_by(user_id=user_id, external_id=manual_external).count(),
                1,
            )
            self.assertEqual(
                db.query(Media).filter_by(user_id=user_id, external_id=sync_external).count(),
                1,
            )
            job = db.get(ImportJob, job_id)
            self.assertEqual(job.status, "done")
            manual = db.get(ImportJobItem, manual_id)
            self.assertEqual(manual.commit_outcome, "imported")
            sync = db.get(LibrarySyncSuggestion, sync_id)
            self.assertEqual(sync.review_state, "approved")
            self.assertIsNotNone(sync.media_id)
        finally:
            db.close()

    def test_scheduler_postpones_failed_sync(self) -> None:
        from datetime import datetime

        from sqlalchemy import select

        from database import SessionLocal
        from models.library_sync import LibrarySyncSettings
        from services.library_sync import scheduler as scheduler_module
        from services.library_sync.orchestrator import utcnow

        user_id = self._user()
        db = SessionLocal()
        try:
            settings = LibrarySyncSettings(
                user_id=user_id,
                enabled=True,
                updates_per_day=4,
                start_time="00:00",
                next_sync_at=datetime(2020, 1, 1),
            )
            db.add(settings)
            db.commit()
        finally:
            db.close()

        fake_run = {"id": "fake-run", "status": "running"}
        with patch.object(
            scheduler_module,
            "due_user_ids",
            return_value=[user_id],
        ), patch.object(
            scheduler_module.sync_runs,
            "start",
            return_value=(fake_run, True),
        ), patch.object(
            scheduler_module.sync_runs,
            "wait",
            return_value={"status": "failed"},
        ):
            asyncio.run(scheduler_module.scheduler._tick())

        db = SessionLocal()
        try:
            settings = db.execute(
                select(LibrarySyncSettings).where(
                    LibrarySyncSettings.user_id == user_id
                )
            ).scalar_one()
            self.assertGreater(settings.next_sync_at, utcnow())
        finally:
            db.close()

    def test_review_commit_only_selected_manual_items(self) -> None:
        import json

        from database import SessionLocal
        from models.enums import MediaStatusEnum, MediaTypeEnum
        from models.import_job import ImportJob, ImportJobItem
        from models.media import Media
        from schemas.import_schemas import ImportCandidate
        from services.import_jobs.serializers import candidate_to_json

        user_id = self._user()
        external_a = f"tmdb-partial-a-{uuid.uuid4().hex}"
        external_b = f"tmdb-partial-b-{uuid.uuid4().hex}"

        def candidate(external_id: str, title: str) -> ImportCandidate:
            return ImportCandidate(
                provider="tmdb",
                external_id=external_id,
                media_type=MediaTypeEnum.MOVIES,
                title=title,
                year=2024,
                score=1.0,
            )

        candidate_a = candidate(external_a, "Partial A")
        candidate_b = candidate(external_b, "Partial B")

        db = SessionLocal()
        try:
            job = ImportJob(
                user_id=user_id,
                provider="letterboxd",
                input_type="file",
                status="ready",
                progress_done=2,
                progress_total=2,
            )
            db.add(job)
            db.flush()
            item_a = ImportJobItem(
                job_id=job.id,
                key=f"partial-a-{uuid.uuid4().hex}",
                media_type=MediaTypeEnum.MOVIES,
                title="Partial A",
                source="letterboxd",
                status=MediaStatusEnum.FINISHED,
                match_status="matched",
                match_json=candidate_to_json(candidate_a),
                candidates_json=json.dumps(
                    [candidate_a.model_dump(mode="json")]
                ),
            )
            item_b = ImportJobItem(
                job_id=job.id,
                key=f"partial-b-{uuid.uuid4().hex}",
                media_type=MediaTypeEnum.MOVIES,
                title="Partial B",
                source="letterboxd",
                status=MediaStatusEnum.FINISHED,
                match_status="matched",
                match_json=candidate_to_json(candidate_b),
                candidates_json=json.dumps(
                    [candidate_b.model_dump(mode="json")]
                ),
            )
            db.add_all([item_a, item_b])
            db.commit()
            job_id = job.id
            item_a_id = item_a.id
            item_b_id = item_b.id
        finally:
            db.close()

        response = self.client.post(
            "/api/review/commit",
            json={
                "userId": user_id,
                "keys": [f"manual:{item_a_id}"],
                "addToBacklog": False,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["failed"], 0)
        self.assertEqual(body["imported"], 1)

        db = SessionLocal()
        try:
            self.assertEqual(
                db.query(Media)
                .filter_by(user_id=user_id, external_id=external_a)
                .count(),
                1,
            )
            self.assertEqual(
                db.query(Media)
                .filter_by(user_id=user_id, external_id=external_b)
                .count(),
                0,
            )
            job = db.get(ImportJob, job_id)
            self.assertEqual(job.status, "ready")
            item_a = db.get(ImportJobItem, item_a_id)
            item_b = db.get(ImportJobItem, item_b_id)
            self.assertEqual(item_a.commit_outcome, "imported")
            self.assertIsNone(item_b.commit_outcome)
        finally:
            db.close()

        response = self.client.post(
            "/api/review/commit",
            json={
                "userId": user_id,
                "keys": [f"manual:{item_b_id}"],
                "addToBacklog": False,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["imported"], 1)

        db = SessionLocal()
        try:
            self.assertEqual(
                db.query(Media)
                .filter_by(user_id=user_id, external_id=external_b)
                .count(),
                1,
            )
            job = db.get(ImportJob, job_id)
            self.assertEqual(job.status, "done")
            item_b = db.get(ImportJobItem, item_b_id)
            self.assertEqual(item_b.commit_outcome, "imported")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
