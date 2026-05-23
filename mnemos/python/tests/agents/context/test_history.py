"""Unit tests for :mod:`codereview.agents.context.history`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from codereview.agents.context.history import fetch_recent_history


@dataclass
class _Commit:
    sha: str
    message: str = ""
    author_login: str | None = None


@dataclass
class _Graph:
    """Test double backing ``file_by_path`` + ``recent_commits_touching``."""

    file_ids: dict[str, UUID]
    commits_by_file: dict[UUID, list[_Commit]]

    async def file_by_path(self, _repo_id: UUID, path: str) -> UUID | None:
        return self.file_ids.get(path)

    async def recent_commits_touching(
        self, file_id: UUID, limit: int = 5
    ) -> list[_Commit]:
        return self.commits_by_file.get(file_id, [])[:limit]


async def test_returns_empty_on_empty_file_list() -> None:
    graph = _Graph(file_ids={}, commits_by_file={})
    out = await fetch_recent_history(
        repo_id=uuid4(), file_paths=[], graph=graph
    )
    assert out == []


async def test_returns_empty_when_graph_missing_methods() -> None:
    class _Bare:
        pass

    out = await fetch_recent_history(
        repo_id=uuid4(), file_paths=["a.py"], graph=_Bare()
    )
    assert out == []


async def test_skips_files_missing_from_graph() -> None:
    file_id = uuid4()
    graph = _Graph(
        file_ids={"a.py": file_id},
        commits_by_file={file_id: [_Commit(sha="abc1234", message="fix a")]},
    )
    out = await fetch_recent_history(
        repo_id=uuid4(),
        file_paths=["missing.py", "a.py"],
        graph=graph,
    )
    assert [c.sha for c in out] == ["abc1234"]


async def test_records_file_path_per_commit() -> None:
    fa = uuid4()
    fb = uuid4()
    graph = _Graph(
        file_ids={"a.py": fa, "b.py": fb},
        commits_by_file={
            fa: [_Commit(sha="aaa", message="do a", author_login="alice")],
            fb: [_Commit(sha="bbb", message="do b", author_login="bob")],
        },
    )
    out = await fetch_recent_history(
        repo_id=uuid4(),
        file_paths=["a.py", "b.py"],
        graph=graph,
    )
    assert [(c.sha, c.file_path, c.author_login) for c in out] == [
        ("aaa", "a.py", "alice"),
        ("bbb", "b.py", "bob"),
    ]


async def test_enforces_per_file_limit() -> None:
    fid = uuid4()
    commits = [_Commit(sha=f"c{i:03d}", message=f"m{i}") for i in range(10)]
    graph = _Graph(
        file_ids={"a.py": fid}, commits_by_file={fid: commits}
    )
    out = await fetch_recent_history(
        repo_id=uuid4(),
        file_paths=["a.py"],
        graph=graph,
        per_file_limit=3,
    )
    assert len(out) == 3


async def test_enforces_total_limit_across_files() -> None:
    fa = uuid4()
    fb = uuid4()
    graph = _Graph(
        file_ids={"a.py": fa, "b.py": fb},
        commits_by_file={
            fa: [_Commit(sha=f"a{i}") for i in range(10)],
            fb: [_Commit(sha=f"b{i}") for i in range(10)],
        },
    )
    out = await fetch_recent_history(
        repo_id=uuid4(),
        file_paths=["a.py", "b.py"],
        graph=graph,
        per_file_limit=10,
        total_limit=5,
    )
    assert len(out) == 5


async def test_uses_first_line_of_multiline_message() -> None:
    fid = uuid4()
    graph = _Graph(
        file_ids={"a.py": fid},
        commits_by_file={
            fid: [_Commit(sha="x", message="subject line\n\nlonger body")]
        },
    )
    out = await fetch_recent_history(
        repo_id=uuid4(), file_paths=["a.py"], graph=graph
    )
    assert out[0].title == "subject line"


async def test_first_line_handles_empty_message() -> None:
    fid = uuid4()
    graph = _Graph(
        file_ids={"a.py": fid},
        commits_by_file={fid: [_Commit(sha="x", message="")]},
    )
    out = await fetch_recent_history(
        repo_id=uuid4(), file_paths=["a.py"], graph=graph
    )
    assert out[0].title is None


async def test_file_lookup_error_is_swallowed() -> None:
    class _Raising:
        async def file_by_path(self, _repo_id: UUID, _path: str) -> Any:
            raise RuntimeError("db down")

        async def recent_commits_touching(self, _fid: UUID, limit: int = 5) -> list[_Commit]:
            return []

    out = await fetch_recent_history(
        repo_id=uuid4(), file_paths=["a.py"], graph=_Raising()
    )
    assert out == []


async def test_commits_lookup_error_is_swallowed() -> None:
    fid = uuid4()

    class _PartiallyRaising:
        async def file_by_path(self, _repo_id: UUID, path: str) -> UUID | None:
            return fid if path == "a.py" else None

        async def recent_commits_touching(self, _fid: UUID, limit: int = 5) -> Any:
            raise RuntimeError("boom")

    out = await fetch_recent_history(
        repo_id=uuid4(), file_paths=["a.py"], graph=_PartiallyRaising()
    )
    assert out == []
