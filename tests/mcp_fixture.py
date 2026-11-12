"""Subprocess test fixture: no real Spotify account or network access."""

import sys
from pathlib import Path

from test_albums import FakeSpotify, album, sid, tracks

from spotify_mcp.album_server import build_server
from spotify_mcp.albums import AlbumService
from spotify_mcp.storage import Store

api = FakeSpotify(count=30)
for index in range(2, 6):
    api.albums[sid(index)] = album(
        index, name=f"Album {index}", artist=f"Band {index}", count=30
    )
    api.tracks[sid(index)] = tracks(index, count=30)
build_server(AlbumService(Store(Path(sys.argv[1])), api)).run()
