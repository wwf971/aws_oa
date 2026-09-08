<!-- This is a minimalist requirement document, aiming at letting reader get a overall grasp of core concepts/workflows, and design and implementation requirement, at a few glances-->

Elasticsearch on aws is expensive. This service keeps elasticsearch on a home server, while services on aws can still use it for CRUD of index, CRUD of documents in index, and search.

For safety, the home server must not be exposed to the internet. All connections should be initiated by the home server: it fetches tasks from aws, runs them on the local elasticsearch, and actively pushes results back to aws. THe home network does not expose any service to public or listens on a port exposed to Internet.. Nothing on aws ever connects in to the home server.

How an index is built and searched is decided by an index config, identified by name; the service should support multiple index configs, without hard-coding any single one as the only choice. Currently one index config exists, `char`, the char-level index: query text matches any substring of the indexed text, case-insensitive, and match positions are returned for highlight. Refer to the experiments in `elasticsearch-index/src/char_index`(search for this folder).

Write order per index must be kept: doc put then doc delete of the same doc must never be applied swapped.

The elasticsearch container is shared by many services on the home network, and they must not bother one another: creating an index whose name is already taken by another service must fail, instead of silently adopting that index. Re-ensuring one's own index (same config as at create) is not a failure.

One consumer goal: the tab-utils backend (general index api in `/2025/tab-manage/tab-utils-mv3/backend/tab_server_index.py`) should be able to run on this service, without a directly reachable elasticsearch.

The aws resource instances should be able to be ensured via python script, all named with the unified name prefix specified in `config.yaml`. Config follows the two-layer design (`config.yaml` example, `config.0.yaml` authentic and git-ignored).

The home server must know which task queue to listen to and which result table to write in a clear, explicit way, not by re-deriving resource names from config values duplicated by hand onto the home server.

The home server's config, including its aws iam keys, must be written by the deploy script from config on the deploying machine, not written by hand on the home server. Using a dedicated least-privilege worker credential (fetch tasks, write results, nothing else) is supported, by putting its keys in the service-local config layer.
