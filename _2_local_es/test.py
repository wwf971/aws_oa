# end-to-end test of the local es service, run from the LOCAL machine (not
# from the raspi). every operation goes through the real pipeline:
#
#   test.py --es_client--> sqs task queue --> worker on raspi --> local es
#   test.py ------------ direct read of that same local es ----------^
#
# this script never writes to elasticsearch itself: it only sends tasks, and
# reads elasticsearch directly to verify. so every change it observes on the
# index proves that someone fetched the task from the queue and executed it,
# and the only task consumer in existence is the worker on the raspi.
#
# test pattern: create one timestamped throwaway index under this sub-project's
# configured prefix, run CRUD + search on it, then delete it. the elasticsearch
# container is shared by many services, so the test touches nothing but this
# one index, and cleans it up directly even when a step fails.
#
# run: python test.py

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aws_utils import TestFail, check, check_count, step, timestamp_make
from config import config_load
from es_client import es_client_make
from es_index_local import es_connect, index_meta_read

FIELD_CONFIG = {
    "field_list_char": ["title", "url"],
    "field_list_exact": [{"name": "user_id", "type": "keyword"}],
}

# what another service colliding on the same index name would send
FIELD_CONFIG_CONFLICT = {
    "field_list_char": ["body"],
    "field_list_exact": [],
}

DOC_HELLO = {"title": "Hello World", "url": "https://example.com/hello", "user_id": "u1"}
DOC_MAP = {"title": "world map", "url": "https://example.com/map", "user_id": "u2"}
DOC_LIST_BATCH = [
    {"doc_id": "d3", "doc": {"title": "alpha", "url": "https://example.com/a", "user_id": "u1"}},
    {"doc_id": "d4", "doc": {"title": "beta", "url": "https://example.com/b", "user_id": "u2"}},
]


# core flow: one throwaway index, create -> CRUD + search -> delete.
# start reading here; each step function sends tasks through the queue and
# verifies the outcome directly on elasticsearch.
def test_run():
    config = config_load()
    client = es_client_make()
    es = es_connect(config)
    if not es.ping():
        raise SystemExit("elasticsearch not reachable directly from this machine,"
                         " check the elasticsearch block in config")

    timestamp = timestamp_make()
    index_name = f"{config['name_prefix']}_temp_{timestamp}"
    print(f"test index: {index_name}")
    if es.indices.exists(index=index_name):
        raise SystemExit(f"test suspended: index already exists: {index_name}")
    try:
        test_index_create(client, es, index_name)
        test_index_ensure_existing(client, es, index_name)
        test_doc_put(client, es, index_name)
        test_doc_put_batch(client, es, index_name)
        test_search(client, index_name)
        test_doc_delete(client, es, index_name)
        test_index_delete(client, es, index_name)
    finally:
        residue_clean(es, index_name)
    print(f"\nall {check_count()} checks passed")


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------

def test_index_create(client, es, index_name):
    step("index_ensure: create the test index through the queue")
    check(not es.indices.exists(index=index_name), "index absent before the test")
    result = client.index_ensure(index_name, "char", FIELD_CONFIG)
    check(result.get("code") == 0 and result.get("data", {}).get("is_created") is True,
          f"task result is success with is_created true: {result}")
    check(es.indices.exists(index=index_name),
          "index now exists on elasticsearch (created by the worker, not by this script)")
    check(index_meta_read(es, index_name) == {"config_name": "char", "field_config": FIELD_CONFIG},
          "index is stamped with the config it was created from")


def test_index_ensure_existing(client, es, index_name):
    step("index_ensure on an existing index: same config passes, other config must fail")
    result = client.index_ensure(index_name, "char", FIELD_CONFIG)
    check(result.get("code") == 0 and result.get("data", {}).get("is_created") is False,
          f"same config: success without re-create: {result}")
    result = client.index_ensure(index_name, "char", FIELD_CONFIG_CONFLICT)
    check(result.get("code", 0) < 0,
          f"conflicting config: failed instead of adopting the index: {result}")
    check(es.indices.exists(index=index_name), "existing index untouched by the failed ensure")


def test_doc_put(client, es, index_name):
    step("doc_put: put docs one by one, read them back directly")
    for doc_id, doc in (("d1", DOC_HELLO), ("d2", DOC_MAP)):
        result = client.doc_put(index_name, doc_id, doc)
        check(result.get("code") == 0, f"doc_put {doc_id}: {result}")
        check(doc_source_direct(es, index_name, doc_id) == doc,
              f"doc {doc_id} readable directly on elasticsearch, content equal")


def test_doc_put_batch(client, es, index_name):
    step("doc_put_batch + index_check: doc count agrees with a direct count")
    result = client.doc_put_batch(index_name, DOC_LIST_BATCH)
    check(result.get("code") == 0, f"doc_put_batch: {result}")
    check(doc_count_direct(es, index_name) == 4, "direct doc count is 4 after the batch put")
    result = client.index_check(index_name)
    check(result.get("code") == 0 and result.get("data") == {"is_existing": True, "doc_count": 4},
          f"index_check reports existing with doc_count 4: {result}")


def test_search(client, index_name):
    step("search: case-insensitive substring with match positions")
    result = client.search(index_name, "char", "world", ["title"])
    check(result.get("code") == 0, f"search: {result}")
    match_map = {entry["doc_id"]: entry["match_list"] for entry in result.get("data", [])}
    check(set(match_map) == {"d1", "d2"},
          f"query 'world' hits d1 ('Hello World') and d2 ('world map') only: {sorted(match_map)}")
    check(match_map.get("d1") == [{"field": "title", "index_start": 6, "index_end": 11}],
          f"match position of 'World' inside 'Hello World': {match_map.get('d1')}")
    check(match_map.get("d2") == [{"field": "title", "index_start": 0, "index_end": 5}],
          f"match position of 'world' inside 'world map': {match_map.get('d2')}")

    result = client.search(index_name, "char", "world", ["title"],
                           filter_exact={"user_id": "u2"})
    check(result.get("code") == 0, f"search with filter_exact: {result}")
    doc_id_list = [entry["doc_id"] for entry in result.get("data", [])]
    check(doc_id_list == ["d2"], f"filter_exact user_id=u2 narrows to d2: {doc_id_list}")


def test_doc_delete(client, es, index_name):
    step("doc_delete + doc_delete_batch: docs disappear from elasticsearch")
    result = client.doc_delete(index_name, "d1")
    check(result.get("code") == 0, f"doc_delete d1: {result}")
    check(doc_source_direct(es, index_name, "d1") is None, "d1 gone")
    check(doc_source_direct(es, index_name, "d2") == DOC_MAP, "d2 still there")
    result = client.doc_delete_batch(index_name, ["d2", "d3", "d4"])
    check(result.get("code") == 0, f"doc_delete_batch: {result}")
    check(doc_count_direct(es, index_name) == 0, "direct doc count is 0 after the batch delete")


def test_index_delete(client, es, index_name):
    step("index_delete: remove the test index through the queue")
    result = client.index_delete(index_name)
    check(result.get("code") == 0 and result.get("data", {}).get("is_deleted") is True,
          f"task result is success with is_deleted true: {result}")
    check(not es.indices.exists(index=index_name), "index gone from elasticsearch")
    result = client.index_check(index_name)
    check(result.get("code") == 0 and result.get("data", {}).get("is_existing") is False,
          f"index_check reports not existing: {result}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def doc_source_direct(es, index_name, doc_id):
    if not es.exists(index=index_name, id=doc_id):
        return None
    return es.get(index=index_name, id=doc_id)["_source"]


def doc_count_direct(es, index_name):
    return int(es.count(index=index_name)["count"])


def residue_clean(es, index_name):
    # direct delete, only as cleanup of a failed run: the shared
    # elasticsearch must not keep the throwaway test index around
    if es.indices.exists(index=index_name):
        es.indices.delete(index=index_name)
        print(f"\nresidue index {index_name} cleaned up directly")


if __name__ == "__main__":
    try:
        test_run()
    except TestFail:
        raise SystemExit("\ntest FAILED")
