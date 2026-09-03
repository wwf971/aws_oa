// runtime config of the pages, generated and uploaded by ensure_frontend.py.
// fetched at runtime so redeploying config never requires a frontend rebuild.

let webConfigCache = null

export async function webConfigLoad() {
  if (webConfigCache) return webConfigCache
  const resp = await fetch('/web-config.json')
  if (!resp.ok) {
    throw new Error('failed to load /web-config.json (was ensure_frontend.py run?)')
  }
  webConfigCache = await resp.json()
  return webConfigCache
}
