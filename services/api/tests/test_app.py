"""The HTTP API, which was at zero percent — including the reconnect flush path.

THE PRODUCT CLAIM IS "IT KEEPS WORKING WHEN THE INTERNET DOES NOT". Everything downstream of that
rests on one endpoint: a console that lost the server reconnects and posts its entire queue to
`/api/sync/append`. The interesting case is not the happy one. It is the console that flushed,
lost the response, and flushes again — because it cannot know whether the first attempt landed.

Deduplication is what makes that safe, and it is what makes the retry the CORRECT behaviour rather
than a hazard. So the tests below send the same ops twice on purpose.

NO DEPENDENCE ON A `data/` DIRECTORY. `data/` and `*.db` are gitignored, so anything reading them
would pass here and fail on a fresh checkout — which is exactly how two builds went red in the
sibling repositories today. The op log is pointed at a temporary directory instead.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rainmaker import app as app_module


def op(op_id: str, actor: str = "alice", entity: str = "d1", *,
       op_type: str = "set", wall: int = 1000, counter: int = 0, **extra: Any) -> dict:
    """The same shape `test_sync.py` uses, so the two files cannot drift apart."""
    return {
        "id": op_id,
        "ts": {"wall": wall, "counter": counter, "actor": actor},
        "actor": actor,
        "kind": "deal",
        "entityId": entity,
        "type": op_type,
        **extra,
    }


def flush(client: TestClient, ops: list[dict], token: str, workspace: str = "demo"):
    """Post an outbox the way the console does: with a grant.

    A HELPER RATHER THAN A TOKEN ARGUMENT THREADED THROUGH EVERY CALL, because the interesting
    thing about these tests is deduplication and none of them are about the token. The one test
    that IS about the token calls `client.post` directly.
    """
    body: dict[str, Any] = {"ops": ops, "token": token}
    if workspace != "demo":
        body["workspace"] = workspace
    return client.post("/api/sync/append", json=body)


@pytest.fixture
def token(client: TestClient) -> str:
    """A grant for `alice`, who is the actor every op in this file is attributed to."""
    return client.post(
        "/api/sync/token", json={"workspace": "demo", "actor": "alice"}
    ).json()["token"]


@pytest.fixture
def token_a(client: TestClient) -> str:
    """A grant in workspace `a`. Deliberately separate from `token_b`: a grant is scoped to one
    workspace, and using one token for both would test nothing about isolation."""
    return client.post(
        "/api/sync/token", json={"workspace": "a", "actor": "alice"}
    ).json()["token"]


@pytest.fixture
def token_b(client: TestClient) -> str:
    return client.post(
        "/api/sync/token", json={"workspace": "b", "actor": "alice"}
    ).json()["token"]


@pytest.fixture
def client(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A fresh API over a throwaway op log.

    `DATA_DIR` is a module global read inside the lifespan handler, so patching the attribute is
    enough and no reimport is needed.
    """
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    with TestClient(app_module.create_app()) as client:
        yield client


class TestHealth:
    def test_it_answers(self, client: TestClient):
        assert client.get("/api/health").status_code == 200

    def test_it_names_the_research_backend(self, client: TestClient):
        """Which fetcher is live decides whether the numbers on screen came from the internet or
        from a fixture. A health check that omits it invites somebody to demo the wrong one."""
        payload = client.get("/api/health").json()
        assert payload["status"] == "ok"
        assert payload["research_backend"]

    def test_it_reports_the_log_head_and_the_live_client_count(self, client: TestClient):
        payload = client.get("/api/health").json()
        assert "live_clients" in payload
        assert payload["live_clients"] == 0


class TestTheOfflineFlush:
    def test_an_empty_flush_is_not_an_error(self, client: TestClient, token: str):
        """A console that reconnects with nothing queued still posts. Returning 4xx for that
        would make every clean reconnect look like a failure in the logs."""
        payload = flush(client, [], token).json()
        assert payload["stored"] == 0

    def test_a_queue_is_accepted_whole(self, client: TestClient, token: str):
        ops = [op(f"o{i}") for i in range(5)]
        payload = flush(client, ops, token).json()
        assert payload["stored"] == 5
        assert payload["head"] >= 5

    def test_flushing_the_same_queue_twice_stores_it_once(self, client: TestClient, token: str):
        """THE POINT OF THE ENDPOINT. A console that lost the response to its first flush must be
        able to send the whole thing again, because it has no way to find out whether the first
        one landed. If the second flush duplicated the ops, the safe behaviour would be to never
        retry — and then a dropped response would silently lose a salesperson's afternoon."""
        ops = [op(f"o{i}") for i in range(5)]
        first = flush(client, ops, token).json()
        second = flush(client, ops, token).json()

        assert first["stored"] == 5
        assert second["stored"] == 0
        assert second["duplicates"] == 5
        assert second["head"] == first["head"], "a duplicate flush must not advance the log"

    def test_a_partial_retry_stores_only_what_is_new(self, client: TestClient, token: str):
        """The realistic case: the first flush half-landed before the connection died."""
        flush(client, [op("o0"), op("o1")], token)
        payload = flush(
            client, [op("o0"), op("o1"), op("o2"), op("o3")], token
        ).json()
        assert payload["stored"] == 2
        assert payload["duplicates"] == 2

    def test_a_malformed_op_is_refused_with_422_not_500(self, client: TestClient, token: str):
        """A console on a bad build must get a rejection it can log, not a server error that
        looks like the backend fell over."""
        response = flush(client, [{"nonsense": True}], token)
        assert response.status_code == 422

    def test_one_bad_op_does_not_persist_the_good_ones_beside_it(self, client: TestClient, token: str):
        """Either the batch lands or it does not. A half-applied flush leaves the client's idea
        of the head wrong, and it has no way to discover that."""
        before = client.get("/api/health").json()
        flush(client, [op("good"), {"nonsense": True}], token)
        after = client.get("/api/health").json()
        assert after.get("head", 0) == before.get("head", 0)


class TestCatchUp:
    def test_a_client_at_zero_receives_everything(self, client: TestClient, token: str):
        flush(client, [op(f"o{i}") for i in range(4)], token)
        payload = client.get("/api/sync/since", params={"seq": 0}).json()
        assert len(payload.get("ops", payload.get("entries", []))) == 4

    def test_a_client_already_current_receives_nothing(self, client: TestClient, token: str):
        flush(client, [op(f"o{i}") for i in range(4)], token)
        head = client.get("/api/health").json().get("head", 4)
        payload = client.get("/api/sync/since", params={"seq": head}).json()
        assert not payload.get("ops", payload.get("entries", []))

    def test_catching_up_past_the_head_is_empty_rather_than_an_error(self, client: TestClient):
        payload = client.get("/api/sync/since", params={"seq": 10_000})
        assert payload.status_code == 200

    def test_an_unknown_workspace_is_empty_rather_than_an_error(self, client: TestClient):
        """Workspaces are created by being written to. Reading one that does not exist yet is
        what every new console does on first launch."""
        payload = client.get("/api/sync/since",
                             params={"workspace": "never-seen", "seq": 0})
        assert payload.status_code == 200


class TestTheFlushPathRefusesStrangers:
    """THE ENDPOINT USED TO TAKE ANYONE'S WORD FOR IT.

    `/api/sync/append` accepted a workspace, an actor and a list of ops from whoever posted
    them. These are the HTTP-level checks; `test_membership.py` covers the token itself.
    """

    def test_a_flush_without_a_token_is_refused(self, client: TestClient):
        response = client.post("/api/sync/append", json={"ops": [op("o0")]})
        assert response.status_code == 401

    def test_an_empty_flush_without_a_token_does_not_leak_the_head(self, client: TestClient):
        """Checked BEFORE the empty-body shortcut. Posting nothing used to return the
        workspace's head, which is a small disclosure and a free one to close."""
        response = client.post("/api/sync/append", json={"ops": []})
        assert response.status_code == 401
        assert "head" not in response.json()

    def test_a_token_for_another_workspace_does_not_open_this_one(
        self, client: TestClient, token: str
    ):
        response = client.post(
            "/api/sync/append",
            json={"workspace": "somewhere-else", "ops": [op("o0")], "token": token},
        )
        assert response.status_code == 401

    def test_ops_cannot_wear_another_actors_name(self, client: TestClient, token: str):
        """The grant is for `alice`. An op claiming `mallory` decides merges as mallory."""
        response = client.post(
            "/api/sync/append",
            json={"ops": [op("o0", actor="mallory")], "token": token},
        )
        assert response.status_code == 401

    def test_a_revoked_member_is_refused_on_the_next_flush(
        self, client: TestClient, token: str
    ):
        assert flush(client, [op("before")], token).status_code == 200
        client.delete("/api/sync/members/alice", params={"workspace": "demo"})
        assert flush(client, [op("after")], token).status_code == 401


class TestWorkspacesAreIsolated:
    def test_the_same_op_id_in_two_workspaces_is_not_a_duplicate(self, client: TestClient, token_a: str, token_b: str):
        """Two customers can generate the same client-side id. Treating that as a duplicate would
        silently drop one of their edits."""
        flush(client, [op("same")], token_a, workspace="a")
        payload = flush(client, [op("same")], token_b, workspace="b").json()
        assert payload["stored"] == 1

    def test_one_workspace_cannot_read_anothers_ops(self, client: TestClient, token_a: str):
        flush(client, [op("secret")], token_a, workspace="a")
        payload = client.get("/api/sync/since", params={"workspace": "b", "seq": 0}).json()
        assert not payload.get("ops", payload.get("entries", []))


class TestDeals:
    def test_the_board_is_served(self, client: TestClient):
        assert client.get("/api/deals").status_code == 200

    def test_an_appended_deal_appears_on_the_board(
        self, client: TestClient, token: str
    ):
        """The whole round trip: an op posted by a reconnecting console becomes a row a salesperson
        sees. Every layer in between is exercised by this one assertion."""
        flush(
            client,
            [op("d-create", entity="deal-1", op_type="set", field="name", value="Acme Corp")],
            token,
        )
        body = client.get("/api/deals").text
        assert "deal-1" in body or "Acme" in body


class TestPresence:
    def test_it_answers_with_nobody_connected(self, client: TestClient):
        response = client.get("/api/sync/presence")
        assert response.status_code == 200
