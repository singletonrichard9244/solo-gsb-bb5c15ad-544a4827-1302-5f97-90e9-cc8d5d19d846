"""设备可版本化标签测试：规范化、重复键拒绝、版本递增、乐观并发（409）、按标签筛选。"""


def ingest(client, device_id, events):
    return client.post("/ingest", json={"device_id": device_id, "events": events})


def make_device(client, device_id):
    return client.put(f"/devices/{device_id}/config", json={})


def put_labels(client, device_id, labels, if_version=None):
    body: dict = {"labels": labels}
    if if_version is not None:
        body["if_version"] = if_version
    return client.put(f"/devices/{device_id}/labels", json=body)


def test_get_labels_unknown_device_404(client):
    assert client.get("/devices/ghost/labels").status_code == 404
    assert put_labels(client, "ghost", []).status_code == 404


def test_first_put_starts_at_version_1_and_get_returns_empty_version_0(client):
    make_device(client, "d1")
    # 设备存在但从未设置过标签：version=0、空标签
    r = client.get("/devices/d1/labels")
    assert r.status_code == 200
    assert r.json() == {"device_id": "d1", "version": 0, "labels": {}, "updated_at": None}

    r = put_labels(client, "d1", [
        {"key": "site", "value": "shanghai"},
        {"key": "floor", "value": "3"},
    ])
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 1
    assert body["labels"] == {"site": "shanghai", "floor": "3"}
    assert body["updated_at"] is not None

    r = client.get("/devices/d1/labels")
    assert r.json()["version"] == 1
    assert r.json()["labels"] == {"site": "shanghai", "floor": "3"}


def test_put_is_full_replace_and_version_increments(client):
    make_device(client, "d1")
    assert put_labels(client, "d1", [{"key": "a", "value": "1"}]).json()["version"] == 1
    r = put_labels(client, "d1", [{"key": "b", "value": "2"}])
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 2
    # 全量替换：旧键 a 已不存在
    assert body["labels"] == {"b": "2"}

    # 空列表用于清空标签，版本仍递增
    r = put_labels(client, "d1", [])
    assert r.status_code == 200
    assert r.json()["version"] == 3
    assert r.json()["labels"] == {}
    assert client.get("/devices/d1/labels").json()["labels"] == {}


def test_keys_are_normalized_stripped(client):
    make_device(client, "d1")
    r = put_labels(client, "d1", [{"key": "  site  ", "value": "bj"}])
    assert r.status_code == 200
    assert r.json()["labels"] == {"site": "bj"}
    assert client.get(
        "/devices", params={"label_key": "site"}
    ).json()[0]["device_id"] == "d1"


def test_blank_key_rejected_400_and_data_unchanged(client):
    make_device(client, "d1")
    put_labels(client, "d1", [{"key": "a", "value": "1"}])

    r = put_labels(client, "d1", [{"key": "   ", "value": "x"}])
    assert r.status_code == 400
    # 拒绝后数据不变、版本不升
    body = client.get("/devices/d1/labels").json()
    assert body["version"] == 1
    assert body["labels"] == {"a": "1"}


def test_duplicate_keys_rejected_400(client):
    make_device(client, "d1")
    # 原样重复
    r = put_labels(client, "d1", [
        {"key": "site", "value": "a"},
        {"key": "site", "value": "b"},
    ])
    assert r.status_code == 400
    # 规范化后冲突（" x" 与 "x "）也算重复
    r = put_labels(client, "d1", [
        {"key": " x", "value": "a"},
        {"key": "x ", "value": "b"},
    ])
    assert r.status_code == 400
    assert client.get("/devices/d1/labels").json()["version"] == 0


def test_too_many_labels_rejected_422(client):
    make_device(client, "d1")
    r = put_labels(client, "d1", [{"key": f"k{i}", "value": "v"} for i in range(21)])
    assert r.status_code == 422
    # 恰好 20 个允许
    r = put_labels(client, "d1", [{"key": f"k{i}", "value": "v"} for i in range(20)])
    assert r.status_code == 200


def test_optimistic_concurrency_if_version_match_and_conflict(client):
    make_device(client, "d1")
    # if_version=0 首次创建成功
    r = put_labels(client, "d1", [{"key": "a", "value": "1"}], if_version=0)
    assert r.status_code == 200
    assert r.json()["version"] == 1

    # 带正确版本更新成功
    r = put_labels(client, "d1", [{"key": "a", "value": "2"}], if_version=1)
    assert r.status_code == 200
    assert r.json()["version"] == 2

    # 版本落后 → 409，数据不变
    r = put_labels(client, "d1", [{"key": "a", "value": "3"}], if_version=1)
    assert r.status_code == 409
    body = client.get("/devices/d1/labels").json()
    assert body["version"] == 2
    assert body["labels"] == {"a": "2"}

    # 版本超前同样 409
    r = put_labels(client, "d1", [{"key": "a", "value": "3"}], if_version=99)
    assert r.status_code == 409

    # 未设置过标签的设备带 if_version=1 → 409
    make_device(client, "d2")
    r = put_labels(client, "d2", [{"key": "a", "value": "1"}], if_version=1)
    assert r.status_code == 409
    assert client.get("/devices/d2/labels").json()["version"] == 0


def test_negative_if_version_rejected_422(client):
    make_device(client, "d1")
    r = put_labels(client, "d1", [{"key": "a", "value": "1"}], if_version=-1)
    assert r.status_code == 422


def test_list_devices_filtered_by_current_labels(client):
    make_device(client, "d1")
    make_device(client, "d2")
    make_device(client, "d3")
    put_labels(client, "d1", [{"key": "site", "value": "shanghai"}, {"key": "floor", "value": "3"}])
    put_labels(client, "d2", [{"key": "site", "value": "beijing"}])
    # d3 不设置标签

    # 无过滤：全部设备
    ids = [d["device_id"] for d in client.get("/devices").json()]
    assert ids == ["d1", "d2", "d3"]

    # 按键存在筛选
    ids = [d["device_id"] for d in client.get("/devices", params={"label_key": "site"}).json()]
    assert ids == ["d1", "d2"]
    ids = [d["device_id"] for d in client.get("/devices", params={"label_key": "floor"}).json()]
    assert ids == ["d1"]

    # 按键值对精确筛选
    ids = [
        d["device_id"]
        for d in client.get(
            "/devices", params={"label_key": "site", "label_value": "beijing"}
        ).json()
    ]
    assert ids == ["d2"]

    # 值不匹配：空结果
    assert client.get(
        "/devices", params={"label_key": "site", "label_value": "guangzhou"}
    ).json() == []

    # 只给 label_value 不给 label_key → 400
    r = client.get("/devices", params={"label_value": "beijing"})
    assert r.status_code == 400


def test_filter_uses_current_labels_not_history(client):
    make_device(client, "d1")
    put_labels(client, "d1", [{"key": "temp", "value": "1"}])
    put_labels(client, "d1", [{"key": "site", "value": "shanghai"}])
    # 旧键 temp 已被全量替换删除，按它筛选不到 d1
    assert client.get("/devices", params={"label_key": "temp"}).json() == []
    ids = [d["device_id"] for d in client.get(
        "/devices", params={"label_key": "site", "label_value": "shanghai"}
    ).json()]
    assert ids == ["d1"]


def test_labels_work_for_auto_registered_ingest_device(client):
    ingest(client, "d1", [{"event_id": "e1", "event_time": "2026-09-22T00:00:10Z", "value": 1.0}])
    r = put_labels(client, "d1", [{"key": "site", "value": "shanghai"}])
    assert r.status_code == 200
    assert client.get("/devices/d1/labels").json()["version"] == 1


def test_labels_persist_across_restarts(client):
    from fastapi.testclient import TestClient

    make_device(client, "d1")
    put_labels(client, "d1", [{"key": "site", "value": "shanghai"}], if_version=0)

    from app.main import app

    with TestClient(app) as c2:
        body = c2.get("/devices/d1/labels").json()
        assert body["version"] == 1
        assert body["labels"] == {"site": "shanghai"}
        # 重启后乐观并发仍以持久化版本为准
        r = put_labels(c2, "d1", [{"key": "site", "value": "beijing"}], if_version=1)
        assert r.status_code == 200
        assert r.json()["version"] == 2
