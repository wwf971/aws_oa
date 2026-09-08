# elasticsearch layer of the worker: the only entry the worker uses to talk
# to the local elasticsearch. one function per task action. actions whose
# behavior depends on the index config (body build at create, query build and
# match extraction at search) delegate to the config module picked by
# config_name; config-agnostic actions (check, delete, doc put/delete) live
# here. refer to local_es_impl.md#index-configs.

from elasticsearch import Elasticsearch

import index_config_char

# available index configs, by name. adding a config = one module + one entry.
INDEX_CONFIG_MAP = {
    "char": index_config_char,
}


def index_config_get(config_name):
    index_config = INDEX_CONFIG_MAP.get(config_name)
    if index_config is None:
        available = ", ".join(INDEX_CONFIG_MAP)
        raise ValueError(f"unknown index config: {config_name}, available: {available}")
    return index_config


def es_connect(config):
    es_config = config["elasticsearch"]
    endpoint = es_config[es_config.get("endpoint_use", "local")]
    server_url = (f"{endpoint.get('scheme', 'http')}://"
                  f"{endpoint.get('host', '127.0.0.1')}:{endpoint.get('port', 9200)}")
    return Elasticsearch(server_url, request_timeout=10)


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------

def _meta_build(config_name, field_config):
    return {"config_name": config_name, "field_config": field_config}


def index_meta_read(es, index_name):
    """the stamp written into mapping _meta at create, or None if the index
    was created by someone else (another service sharing this elasticsearch)."""
    mapping = es.indices.get_mapping(index=index_name)
    return mapping[index_name]["mappings"].get("_meta")


def _index_create(es, index_name, config_name, field_config):
    body = index_config_get(config_name).index_body_build(field_config)
    # stamp the index with what it was created from, so a later ensure can
    # tell this service's own index from an index of another service
    body.setdefault("mappings", {})["_meta"] = _meta_build(config_name, field_config)
    es.indices.create(index=index_name, body=body)


def index_ensure(es, index_name, config_name, field_config):
    if not es.indices.exists(index=index_name):
        _index_create(es, index_name, config_name, field_config)
        return {"is_created": True}
    # the local elasticsearch is shared by many services: accept an existing
    # index only when its stamp shows it was created from this same config,
    # otherwise fail instead of silently adopting a foreign index
    meta = index_meta_read(es, index_name)
    if meta != _meta_build(config_name, field_config):
        raise ValueError(
            f"index {index_name} already exists but was not created from this"
            f" config, refuse to adopt it (existing stamp: {meta})")
    return {"is_created": False}


def index_check(es, index_name):
    if not es.indices.exists(index=index_name):
        return {"is_existing": False}
    doc_count = int(es.count(index=index_name)["count"])
    return {"is_existing": True, "doc_count": doc_count}


def index_recreate(es, index_name, config_name, field_config):
    # dry-build the body first, so a bad config fails before anything is deleted
    index_config_get(config_name).index_body_build(field_config)
    if es.indices.exists(index=index_name):
        es.indices.delete(index=index_name)
    _index_create(es, index_name, config_name, field_config)


def index_delete(es, index_name):
    if not es.indices.exists(index=index_name):
        return {"is_deleted": False}
    es.indices.delete(index=index_name)
    return {"is_deleted": True}


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------

def doc_put(es, index_name, doc_id, doc):
    es.index(index=index_name, id=doc_id, document=doc, refresh="wait_for")


def doc_delete(es, index_name, doc_id):
    try:
        es.delete(index=index_name, id=doc_id, refresh="wait_for")
    except Exception as error:
        # deleting an already-missing doc is a no-op, so retried tasks stay safe
        if getattr(error, "status_code", None) == 404 or "NotFoundError" in type(error).__name__:
            return
        raise


def doc_put_batch(es, index_name, doc_list):
    # doc_list: [{doc_id, doc}]. refresh once at the end instead of per doc,
    # or a large batch would wait one refresh cycle (~1s) per doc.
    for entry in doc_list:
        es.index(index=index_name, id=entry["doc_id"], document=entry["doc"])
    es.indices.refresh(index=index_name)


def doc_delete_batch(es, index_name, doc_id_list):
    for doc_id in doc_id_list:
        try:
            es.delete(index=index_name, id=doc_id)
        except Exception as error:
            if getattr(error, "status_code", None) == 404 or "NotFoundError" in type(error).__name__:
                continue
            raise
    es.indices.refresh(index=index_name)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def search(es, index_name, config_name, query_tree, field_list, filter_exact, limit):
    # returns [{doc_id, match_list: [{field, index_start, index_end}]}]
    return index_config_get(config_name).search(
        es, index_name, query_tree, field_list, filter_exact, limit)
