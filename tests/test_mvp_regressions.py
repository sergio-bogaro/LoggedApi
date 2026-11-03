"""Regressões do MVP local: datas, logs, tags e importação MAL."""

import asyncio
import os
import tempfile
import unittest
import uuid
from pathlib import Path

# Precisa rodar antes de importar config/database/main.
_TMP_DIR = Path(tempfile.mkdtemp(prefix="logged-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DIR.as_posix()}/test.db"
os.environ["UPLOAD_DIR"] = str(_TMP_DIR / "uploads")

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


if __name__ == "__main__":
    unittest.main()
