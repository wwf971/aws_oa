# index config "char": char-level substring match. text fields are tokenized
# one character per token, so a search term matches any substring of the
# indexed text, case-insensitive, with match positions returned for highlight.
# refer to local_es_impl.md#index-configs.
#
# field_config of this index config:
#   field_list_char    names of the char-level text fields, e.g. ["title", "url"]
#   field_list_exact   exact filter fields, [{name, type}], type keyword/boolean/long

HIGHLIGHT_TAG_START = "[[HL_START]]"
HIGHLIGHT_TAG_END = "[[HL_END]]"


# ---------------------------------------------------------------------------
# index body: char-level text fields + exact filter fields
# ---------------------------------------------------------------------------

def _field_char():
    # char-level, case-insensitive, term vectors for the fvh highlighter
    return {
        "type": "text",
        "analyzer": "char_analyzer",
        "term_vector": "with_positions_offsets",
    }


def index_body_build(field_config):
    properties = {}
    for field_name in field_config.get("field_list_char", []):
        properties[field_name] = _field_char()
    for field in field_config.get("field_list_exact", []):
        properties[field["name"]] = {"type": field["type"]}
    return {
        "settings": {
            "number_of_shards": 1,
            "analysis": {
                "analyzer": {
                    "char_analyzer": {
                        "type": "custom",
                        "tokenizer": "char_tokenizer",
                        "filter": ["lowercase"],
                    }
                },
                "tokenizer": {
                    "char_tokenizer": {
                        "type": "pattern",
                        "pattern": "",  # empty pattern splits on each character
                    }
                },
            },
        },
        "mappings": {"properties": properties},
    }


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def _term_query(term_text, field_list):
    conditions = [{"match_phrase": {field: term_text}} for field in field_list]
    if len(conditions) == 1:
        return conditions[0]
    return {"bool": {"should": conditions, "minimum_should_match": 1}}


def _tree_query(query_tree, field_list):
    # query tree: "text", or {"term": ...} / {"and": [...]} / {"or": [...]} / {"not": ...}
    if isinstance(query_tree, str):
        return _term_query(query_tree, field_list)
    if not isinstance(query_tree, dict):
        raise ValueError("invalid query tree")
    if "term" in query_tree:
        return _term_query(str(query_tree["term"]), field_list)
    if "and" in query_tree:
        return {"bool": {"must": [
            _tree_query(child, field_list) for child in query_tree["and"]]}}
    if "or" in query_tree:
        return {"bool": {
            "should": [_tree_query(child, field_list) for child in query_tree["or"]],
            "minimum_should_match": 1,
        }}
    if "not" in query_tree:
        return {"bool": {"must_not": [_tree_query(query_tree["not"], field_list)]}}
    raise ValueError("invalid query tree")


def search(es, index_name, query_tree, field_list, filter_exact, limit):
    # returns [{doc_id, match_list: [{field, index_start, index_end}]}]
    highlight_fields = {}
    for field in field_list:
        highlight_fields[field] = {
            "type": "fvh",  # phrase-aware whole-substring highlight, needs term vectors
            "pre_tags": [HIGHLIGHT_TAG_START],
            "post_tags": [HIGHLIGHT_TAG_END],
            "fragment_size": 999999,
            "number_of_fragments": 0,
        }
    filter_list = [
        {"term": {field_name: value}}
        for field_name, value in (filter_exact or {}).items()
    ]
    body = {
        "query": {
            "bool": {
                "must": [_tree_query(query_tree, field_list)],
                "filter": filter_list,
            }
        },
        "highlight": {"fields": highlight_fields},
        "size": limit,
    }
    response = es.search(index=index_name, body=body)
    results = []
    for hit in response["hits"]["hits"]:
        match_list = []
        highlight = hit.get("highlight", {})
        for field in field_list:
            for highlighted_text in highlight.get(field, []):
                for index_start, index_end in _positions_of_highlight(highlighted_text):
                    match_list.append({
                        "field": field,
                        "index_start": index_start,
                        "index_end": index_end,
                    })
        results.append({"doc_id": hit["_id"], "match_list": match_list})
    return results


def _positions_of_highlight(highlighted_text):
    # extract (start, end) char positions over the original text from the tags
    positions = []
    position_original = 0
    position_tagged = 0
    while True:
        index_start = highlighted_text.find(HIGHLIGHT_TAG_START, position_tagged)
        if index_start == -1:
            break
        position_original += index_start - position_tagged
        index_end = highlighted_text.find(HIGHLIGHT_TAG_END, index_start)
        if index_end == -1:
            break
        match_length = index_end - (index_start + len(HIGHLIGHT_TAG_START))
        positions.append((position_original, position_original + match_length))
        position_original += match_length
        position_tagged = index_end + len(HIGHLIGHT_TAG_END)
    return positions
